"""종목별 과거 거래 회고(lessons) — 순수 파생 함수 + 뇌/밸류 후보 주입.

전부 tmp·fake 격리(실 store·실 LLM 무접촉). 청산 이력 → past_trades 요약이
정확한지, 이력 있는 후보에만 실리는지, config 스위치로 꺼지는지 검증한다.
"""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from src.config import load_config
from src.lessons import build_symbol_lessons
from src.agents.pipeline import CycleRunner, synth_candles
from src.agents.value_trade import ValueRunner
from src.agents import MockLLM, DecisionOutput, ValidationOutput
from src.paper_account import PaperAccount
from src.risk_gate import RiskGate
from src.broker import Broker
from src.risk import RiskManager
from src.engine.store import Store

_KST = ZoneInfo("Asia/Seoul")
_BASE = 1_700_000_000.0
_DAY = 86400.0
_LONG_THESIS = "돌파 재확인 진입 — 수급 유입과 20일선 회복 확인, RSI 과열 아님, 거래대금 급증으로 추세 지속 기대"


def _row(symbol, qty, avg, pnl, exit_reason, strategy, thesis, opened, closed):
    return {"symbol": symbol, "qty": qty, "avg_price": avg, "pnl": pnl,
            "exit_reason": exit_reason, "strategy": strategy, "thesis": thesis,
            "opened_at": opened, "closed_at": closed}


class _FakeStore:
    """closed_trades 만 노출하는 최소 store — symbol 필터 + 최신순(closed_at desc)."""
    def __init__(self, rows):
        self._rows = sorted(rows, key=lambda r: r["closed_at"], reverse=True)

    def closed_trades(self, symbols=None):
        rows = self._rows
        if symbols:
            s = set(symbols)
            rows = [r for r in rows if r["symbol"] in s]
        return rows


def _sample_rows():
    return [
        # AAA: 승/패/pnl결손 3건 — 최신순 r1>r2>r3
        _row("AAA", 10, 100.0, 50.0, "target_hit", "rsi_reversion", _LONG_THESIS,
             _BASE - 3 * _DAY, _BASE),                      # +5.0% 승
        _row("AAA", 10, 100.0, -52.0, "stop_hit", "momentum", None,
             _BASE - 12 * _DAY, _BASE - 10 * _DAY),         # -5.2% 패, thesis 없음
        _row("AAA", 10, 100.0, None, None, "macd", "x",
             None, _BASE - 20 * _DAY),                      # pnl 결손, exit/opened 결손
        # BBB: 2건 — b2 는 원가 0(0나눗셈 안전)
        _row("BBB", 5, 200.0, 100.0, "target_hit", "value", "저평가 해소 시작",
             _BASE - 5 * _DAY, _BASE - 2 * _DAY),           # +10.0% 승
        _row("BBB", 5, 0.0, 100.0, "session_end", None, None,
             _BASE - 8 * _DAY, _BASE - 6 * _DAY),           # 원가0 → pnl_pct None
    ]


# ── (a) 순수 파생 정확성 ────────────────────────────────────────────
def test_build_symbol_lessons_aggregates_and_recent():
    store = _FakeStore(_sample_rows())
    out = build_symbol_lessons(store, ["AAA", "BBB", "CCC"])

    assert set(out) == {"AAA", "BBB"}                    # 이력 없는 CCC 키 없음

    aaa = out["AAA"]
    assert aaa["n"] == 3 and aaa["wins"] == 1
    assert aaa["avg_pnl_pct"] == -0.1                    # (+5.0, -5.2) 평균, 결손 제외
    assert len(aaa["recent"]) == 3
    r1, r2, r3 = aaa["recent"]
    assert r1["pnl_pct"] == 5.0 and r1["exit"] == "target_hit"
    assert r1["strategy"] == "rsi_reversion" and r1["held_days"] == 3
    assert r1["closed"] == datetime.fromtimestamp(_BASE, _KST).strftime("%m-%d")
    assert r1["thesis_head"] == _LONG_THESIS[:60]        # 60자 컷
    assert "thesis_head" not in r2                       # thesis 없으면 생략
    assert r2["pnl_pct"] == -5.2 and r2["exit"] == "stop_hit"
    assert r3["pnl_pct"] is None and r3["exit"] == "unknown"   # 결손 안전
    assert r3["held_days"] is None                       # opened 결손 → None

    bbb = out["BBB"]
    assert bbb["n"] == 2 and bbb["wins"] == 2            # 둘 다 pnl>0
    assert bbb["avg_pnl_pct"] == 10.0                    # 원가0 건 제외
    assert bbb["recent"][1]["pnl_pct"] is None           # 0나눗셈 안전
    assert bbb["recent"][1]["strategy"] is None


def test_build_symbol_lessons_max_per_symbol_cut():
    store = _FakeStore(_sample_rows())
    out = build_symbol_lessons(store, ["AAA"], max_per_symbol=2)
    assert out["AAA"]["n"] == 3                          # 전체 건수는 유지
    assert len(out["AAA"]["recent"]) == 2                # recent 만 컷


def test_build_symbol_lessons_groups_partial_slices():
    """부분매도 slice(parent_id)는 1거래로 집계."""
    r1 = _row("AAA", 5, 100.0, 25.0, "partial", "rsi", "t", _BASE - 5 * _DAY, _BASE - 4 * _DAY)
    r2 = _row("AAA", 5, 100.0, 25.0, "target_hit", "rsi", "t", _BASE - 5 * _DAY, _BASE)
    r1["id"] = 1
    r1["parent_id"] = 10
    r2["id"] = 2
    r2["parent_id"] = 10
    store = _FakeStore([r1, r2])
    out = build_symbol_lessons(store, ["AAA"])
    assert out["AAA"]["n"] == 1
    assert out["AAA"]["wins"] == 1
    assert out["AAA"]["avg_pnl_pct"] == 5.0
    assert len(out["AAA"]["recent"]) == 1


def test_build_symbol_lessons_empty_symbols():
    assert build_symbol_lessons(_FakeStore(_sample_rows()), []) == {}


# ── (b) 뇌 후보 주입 ────────────────────────────────────────────────
def _capture_factory(seen):
    """결정 LLM 이 받은 후보 리스트(dict 그대로)를 seen 에 담고 제안은 안 냄."""
    def factory(candidates):
        seen.append(candidates)

        def responder(schema, system, user):
            if schema is DecisionOutput:
                return DecisionOutput(market_view="x", proposals=[])
            return ValidationOutput(verdicts=[])
        return MockLLM(responder)
    return factory


def _brain_runner(tmp_path, store, seen, lessons=True):
    cfg = load_config()
    agents = cfg.raw.setdefault("agents", {})
    agents["require_dossier"] = False
    agents["lessons"] = lessons
    # 후보를 소수로 고정(005930=이력, 000660=이력없음) — 주입 대상 판별을 명확히.
    cfg.raw["universe"] = {"KR": [{"symbol": "005930", "name": "삼성전자"},
                                  {"symbol": "000660", "name": "SK하이닉스"}]}
    acct = PaperAccount(cash={"KR": 10_000_000, "US": 10_000},
                        state_path=tmp_path / "pa.json")
    gate = RiskGate({"capital": cfg.risk.get("capital", {}), "max_position_pct": 0.2,
                     "max_positions": 5, "kill_switch_file": str(tmp_path / "H")})
    broker = Broker(account=acct, gate=gate, mode="paper")
    risk = RiskManager(capital=cfg.risk.get("capital", {}), max_position_pct=0.2)
    return CycleRunner(cfg, llm_factory=_capture_factory(seen), fetch_candles=synth_candles,
                       store=store, broker=broker, risk=risk,
                       journal_path=tmp_path / "d.jsonl",
                       market_state_path=tmp_path / "ms.json")


def _seed_closed_trade(store, symbol="005930", market="KR"):
    pid = store.open_position(symbol, market, qty=10, avg_price=1000.0,
                              strategy="rsi_reversion", thesis="과거 진입 사유")
    store.close_position(pid, exit_price=1100.0, reason="target_hit")


def test_brain_injects_past_trades_only_for_history(tmp_path):
    store = Store(tmp_path / "t.db")
    _seed_closed_trade(store, "005930")
    seen = []
    _brain_runner(tmp_path, store, seen).run()
    cands = {c["symbol"]: c for c in seen[0]}
    assert "past_trades" in cands["005930"]              # 이력 있는 후보에만
    assert cands["005930"]["past_trades"]["n"] == 1
    assert cands["005930"]["past_trades"]["wins"] == 1
    assert "past_trades" not in cands["000660"]          # 이력 없는 후보엔 미주입


def test_brain_lessons_false_skips_injection(tmp_path):
    store = Store(tmp_path / "t.db")
    _seed_closed_trade(store, "005930")
    seen = []
    _brain_runner(tmp_path, store, seen, lessons=False).run()
    cands = {c["symbol"]: c for c in seen[0]}
    assert "past_trades" not in cands["005930"]          # 스위치 off → 미주입


# ── (c) 밸류 후보 주입 ──────────────────────────────────────────────
def _gated(symbol, price=1000.0):
    return {"symbol": symbol, "name": symbol, "_current_price": price,
            "_timing": {"sma20": price, "ret_20d_pct": 1.0, "ret_5d_pct": 0.5},
            "metrics": {"price": price}, "fair_low_pct": 20.0, "fair_high_pct": 40.0,
            "stance": "undervalued", "conviction": 0.6, "thesis": "저평가",
            "risks": [], "evidence": [], "ts": _BASE}


def _value_runner(store, lessons=True):
    cfg = load_config()
    cfg.raw.setdefault("agents", {})["lessons"] = lessons
    return ValueRunner(cfg, store, broker=None, risk=None, llm_factory=None,
                       val_llm_factory=None)


def test_value_injects_past_trades_only_for_history():
    store = _FakeStore(_sample_rows())              # AAA 이력 있음, ZZZ 없음
    vr = _value_runner(store)
    ctx = json.loads(vr._build_context("KR", {}, {}, [_gated("AAA"), _gated("ZZZ")], [], _BASE))
    by = {c["symbol"]: c for c in ctx["candidates"]}
    assert by["AAA"]["past_trades"]["n"] == 3        # 이력 있는 후보에만
    assert "past_trades" not in by["ZZZ"]            # 이력 없는 후보엔 미주입


def test_value_lessons_false_skips_injection():
    store = _FakeStore(_sample_rows())
    vr = _value_runner(store, lessons=False)
    ctx = json.loads(vr._build_context("KR", {}, {}, [_gated("AAA")], [], _BASE))
    assert "past_trades" not in ctx["candidates"][0]  # 스위치 off → 미주입
