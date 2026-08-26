"""밸류 트랙 V2 — 시간 손절 / 분할 매수(트랜치) / 대시보드 밸류 탭.

전부 tmp·mock 격리(실계좌·실LLM·실 watchlist 무접촉). 하위호환이 최우선이라
tranches 기본값([1.0])에서 V1 동작이 한 톨도 안 달라지는지도 함께 증명한다.
"""
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import dashboard as dash  # noqa: E402

from src.config import load_config
from src.agents.llm import MockLLM
from src.agents.pipeline import CycleRunner, dry_llm_factory, synth_candles
from src.agents.decision_agent import SYSTEM as BRAIN_SYSTEM
from src.agents.schemas import (DecisionOutput, ValidationOutput, Proposal,
                                ValidationVerdict)
from src.agents.value_trade import (select_candidates, value_trade_cfg,
                                    ValueDecisionAgent, ValueRunner,
                                    VALUE_TRADE_SYSTEM)
from src.engine.store import Store
from src.paper_account import PaperAccount
from src.risk_gate import RiskGate
from src.broker import Broker
from src.risk import RiskManager

_KST = ZoneInfo("Asia/Seoul")
# 2026-07-13(월) 10:00 KST — KR 장중, 휴장일 아님(test_value_trade 와 동일 기준).
_OPEN_NOW = datetime(2026, 7, 13, 10, 0, tzinfo=_KST).timestamp()

_CFG_V = {"markets": ["KR", "US"], "dossier_ttl_hours": 400,
          "min_dossier_conviction": 0.4, "sleeve_pct": 0.60,
          "brain_reserve_pct": 0.30, "max_per_run": 2,
          "max_positions": 3, "hard_stop_pct": 0.20, "tranches": [1.0],
          "tranche_min_days": 7}


def _entry(**kw):
    base = {"name": "x", "market": "KR", "ts": _OPEN_NOW - 100,
            "stance": "undervalued", "conviction": 0.6, "fair_low_pct": 20.0,
            "metrics": {"price": 1000.0}}
    base.update(kw)
    return base


def _synth_uptrend(low=900.0, high=1100.0, n=120):
    closes = [low + (high - low) * (i / (n - 1)) for i in range(n)]
    return pd.DataFrame({"time": range(n), "open": closes, "high": closes,
                         "low": closes, "close": closes, "volume": [1000] * n})


class _FakeStore:
    """select_candidates 용 최소 store — 열린 포지션 행(dict)과 armed 심볼만 노출."""
    def __init__(self, open_rows=(), armed_syms=()):
        self._open = list(open_rows)
        self._armed = [{"symbol": s} for s in armed_syms]

    def get_open_positions(self):
        return self._open

    def get_armed(self):
        return self._armed


def _value_row(symbol, *, strategy="value", tranches=None, idx=None, last=None):
    """store 열린 포지션 행 모사(meta 는 실제와 같이 JSON 문자열)."""
    meta = {"source": "value"}
    if tranches is not None:
        meta["tranches"] = tranches
    if idx is not None:
        meta["tranche_idx"] = idx
    if last is not None:
        meta["last_tranche_at"] = last
    return {"symbol": symbol, "strategy": strategy, "meta": json.dumps(meta)}


# ── 1) 시간 손절 ───────────────────────────────────────────────────
def _runner(tmp_path, store, *, time_stop_days=None):
    cfg = load_config()
    cfg.raw.setdefault("agents", {})["require_dossier"] = False
    if time_stop_days is not None:
        cfg.raw.setdefault("value_trade", {})["time_stop_days"] = time_stop_days
    acct = PaperAccount(cash={"KR": 10_000_000, "US": 10_000},
                        state_path=tmp_path / "pa.json")
    gate = RiskGate({"capital": cfg.risk.get("capital", {}), "max_position_pct": 0.2,
                     "max_positions": 5, "daily_loss_limit_pct": 0.05,
                     "max_order_notional": {"KR": 5_000_000, "US": 50_000},
                     "kill_switch_file": str(tmp_path / "HALT")})
    broker = Broker(account=acct, gate=gate, mode="paper")
    risk = RiskManager(capital=cfg.risk.get("capital", {}), max_position_pct=0.2)
    return CycleRunner(cfg, llm_factory=dry_llm_factory, fetch_candles=synth_candles,
                       store=store, broker=broker, risk=risk,
                       journal_path=tmp_path / "dec.jsonl",
                       market_state_path=tmp_path / "ms.json")


def _seed_position(r, store, symbol="005930", *, strategy, days_ago):
    """계좌+store 양쪽에 보유를 만든다(_portfolio 는 계좌 보유를 store 행과 조인)."""
    from src.risk_gate import Order
    r.broker.execute(Order(symbol, "KR", "BUY", 1, 70000.0), reason="seed")
    pid = store.open_position(symbol, "KR", 1, 70000.0, strategy=strategy,
                              thesis="seed", meta={"source": "value"})
    store.update_position(pid, opened_at=time.time() - days_ago * 86400)
    return pid


def test_time_stop_attached_when_exceeded(tmp_path):
    store = Store(tmp_path / "t.db")
    r = _runner(tmp_path, store, time_stop_days=120)
    _seed_position(r, store, strategy="value", days_ago=200)
    held = r._portfolio()["positions"][0]
    assert held["time_stop"]["exceeded"] is True
    assert held["time_stop"]["threshold_days"] == 120
    # days_held 는 재계산이 아니라 같은 값을 재사용한다.
    assert held["time_stop"]["days_held"] == held["days_held"]


def test_time_stop_not_exceeded_below_threshold(tmp_path):
    store = Store(tmp_path / "t.db")
    r = _runner(tmp_path, store, time_stop_days=120)
    _seed_position(r, store, strategy="value", days_ago=10)
    held = r._portfolio()["positions"][0]
    assert held["time_stop"]["exceeded"] is False


def test_time_stop_disabled_omits_key(tmp_path):
    store = Store(tmp_path / "t.db")
    r = _runner(tmp_path, store, time_stop_days=0)
    _seed_position(r, store, strategy="value", days_ago=999)
    held = r._portfolio()["positions"][0]
    assert "time_stop" not in held                       # 비활성이면 컨텍스트 소음 없음


def test_time_stop_not_attached_to_non_value_position(tmp_path):
    store = Store(tmp_path / "t.db")
    r = _runner(tmp_path, store, time_stop_days=120)
    _seed_position(r, store, strategy="ma_crossover", days_ago=999)
    held = r._portfolio()["positions"][0]
    assert "time_stop" not in held                       # 밸류 트랙 전용


def test_brain_prompt_has_time_stop_section():
    assert "시간 손절(positions[].time_stop" in BRAIN_SYSTEM
    # 기존 섹션이 뒤에 그대로 남아 있어야(회귀 없음)
    assert BRAIN_SYSTEM.index("시간 손절(positions[].time_stop") < \
        BRAIN_SYSTEM.index("보유 종목 재평가(thesis 깨짐)")


# ── 2) 트랜치 셀렉터 ───────────────────────────────────────────────
def test_tranche_candidate_kept_when_remaining_and_gap_elapsed():
    wl = {"A": _entry()}
    store = _FakeStore([_value_row("A", tranches=[0.5, 0.5], idx=1,
                                   last=_OPEN_NOW - 8 * 86400)])
    out = select_candidates(wl, store, _CFG_V, _OPEN_NOW)
    assert [c["symbol"] for c in out] == ["A"]
    assert out[0]["_tranche"] == {"idx": 2, "total": 2, "weight": 0.5}


def test_tranche_candidate_excluded_before_min_days():
    wl = {"A": _entry()}
    store = _FakeStore([_value_row("A", tranches=[0.5, 0.5], idx=1,
                                   last=_OPEN_NOW - 3 * 86400)])
    assert select_candidates(wl, store, _CFG_V, _OPEN_NOW) == []


def test_tranche_candidate_excluded_when_exhausted():
    wl = {"A": _entry()}
    store = _FakeStore([_value_row("A", tranches=[0.5, 0.5], idx=2,
                                   last=_OPEN_NOW - 30 * 86400)])
    assert select_candidates(wl, store, _CFG_V, _OPEN_NOW) == []


def test_legacy_position_without_tranche_meta_excluded():
    """V1 시절 진입분(meta 에 트랜치 정보 없음)은 추가 진입 대상이 아니다."""
    wl = {"A": _entry()}
    store = _FakeStore([_value_row("A")])
    assert select_candidates(wl, store, _CFG_V, _OPEN_NOW) == []


def test_non_value_position_excluded_even_with_tranche_meta():
    wl = {"A": _entry()}
    store = _FakeStore([_value_row("A", strategy="ma_crossover", tranches=[0.5, 0.5],
                                   idx=1, last=_OPEN_NOW - 30 * 86400)])
    assert select_candidates(wl, store, _CFG_V, _OPEN_NOW) == []


def test_armed_still_excluded_even_with_remaining_tranche():
    wl = {"A": _entry()}
    store = _FakeStore([_value_row("A", tranches=[0.5, 0.5], idx=1,
                                   last=_OPEN_NOW - 30 * 86400)],
                       armed_syms=["A"])
    assert select_candidates(wl, store, _CFG_V, _OPEN_NOW) == []


# ── 3) 하위호환: tranches 기본값이면 V1 과 동일 ────────────────────
def test_default_tranches_is_single_shot():
    cfg = load_config()
    cfg.raw.setdefault("value_trade", {}).pop("tranches", None)
    assert value_trade_cfg(cfg)["tranches"] == [1.0]


def test_tranches_normalized_when_sum_not_one():
    cfg = load_config()
    cfg.raw.setdefault("value_trade", {})["tranches"] = [1.0, 1.0]
    assert value_trade_cfg(cfg)["tranches"] == [0.5, 0.5]


def test_default_tranches_keeps_holdings_excluded():
    """기본값([1.0])으로 진입한 포지션은 1회차에서 소진 → 셀렉터가 계속 제외한다."""
    wl = {"A": _entry()}
    store = _FakeStore([_value_row("A", tranches=[1.0], idx=1,
                                   last=_OPEN_NOW - 90 * 86400)])
    assert select_candidates(wl, store, _CFG_V, _OPEN_NOW) == []


def test_분할_켜지면_1회차부터_비중_제한():
    """분할의 핵심 — 1회차를 안 막으면 전량을 먹고 2회차가 비중 게이트에 막힌다.

    신규 진입(미보유)에도 첫 트랜치 비중이 실려야 실제로 쪼개진다.
    """
    wl = {"A": _entry()}
    cfg = dict(_CFG_V, tranches=[0.5, 0.5])
    cands = select_candidates(wl, _FakeStore([]), cfg, _OPEN_NOW)
    assert len(cands) == 1
    assert cands[0]["_tranche"] == {"idx": 1, "total": 2, "weight": 0.5}


def test_분할_꺼져있으면_신규진입에_트랜치_안붙음():
    """기본값([1.0])에선 _tranche 가 아예 안 붙어야 한다 — 클램프 경로 미진입(기존 동작)."""
    cands = select_candidates({"A": _entry()}, _FakeStore([]), _CFG_V, _OPEN_NOW)
    assert len(cands) == 1 and "_tranche" not in cands[0]


def test_분할_켜져도_보유분은_추가회차_비중을_쓴다():
    """1회차 신규 로직이 보유분의 남은 회차 정보를 덮어쓰면 안 된다."""
    cfg = dict(_CFG_V, tranches=[0.4, 0.6])
    store = _FakeStore([_value_row("A", tranches=[0.4, 0.6], idx=1,
                                   last=_OPEN_NOW - 30 * 86400)])
    cands = select_candidates({"A": _entry()}, store, cfg, _OPEN_NOW)
    assert cands[0]["_tranche"] == {"idx": 2, "total": 2, "weight": 0.6}


def _decide_with(weight, tranche_by_sym=None, symbol="A"):
    def responder(schema, system, user):
        return DecisionOutput(market_view="mv", proposals=[Proposal(
            symbol=symbol, market="KR", side="BUY", conviction=0.7,
            horizon="position", target_weight=weight, thesis="t", key_risks=[])])
    agent = ValueDecisionAgent(MockLLM(responder), max_position_pct=0.2,
                               tranche_by_sym=tranche_by_sym)
    return agent.decide("{}")


def test_no_clamp_without_tranche_info():
    """하위호환: _tranche 가 없으면 target_weight 는 LLM 값 그대로(기존 동작)."""
    out = _decide_with(0.9)
    assert out.proposals[0].target_weight == 0.9


def test_clamp_caps_target_weight_by_tranche_weight():
    out = _decide_with(0.9, {"A": {"idx": 2, "total": 2, "weight": 0.5}})
    assert out.proposals[0].target_weight == 0.2 * 0.5          # max_position_pct×비중


def test_clamp_leaves_smaller_weight_alone():
    out = _decide_with(0.05, {"A": {"idx": 2, "total": 2, "weight": 0.5}})
    assert out.proposals[0].target_weight == 0.05


def test_value_prompt_has_tranche_section():
    assert "분할 매수(candidates[].tranche" in VALUE_TRADE_SYSTEM
    assert "사이징에 쓰이지 않는다" in VALUE_TRADE_SYSTEM or "사이징에 미반영" in VALUE_TRADE_SYSTEM


# ── 4) 러너: 추가 트랜치 체결 미러링 ──────────────────────────────
def _mock_llm_factory(weight=0.05):
    """후보 전부에 BUY 를 내는 결정+검증 mock. weight 는 종목당 비중 상한(0.2) 아래로
    잡아 하드 게이트(체결후 종목 비중)에 걸리지 않게 한다 — 관심사는 미러링이다."""
    def factory(cands):
        def responder(schema, system, user):
            if schema is DecisionOutput:
                props = [Proposal(symbol=c["symbol"], market=c["market"], side="BUY",
                                  conviction=0.7, horizon="position",
                                  target_weight=weight, thesis="밸류 진입",
                                  key_risks=["r"]) for c in cands]
                return DecisionOutput(market_view="mv", proposals=props)
            syms = [p["symbol"] for p in json.loads(user)["proposals"]]
            return ValidationOutput(verdicts=[ValidationVerdict(
                symbol=s, approved=True, reason="ok") for s in syms])
        return MockLLM(responder)
    return factory


def _build_runner(tmp_path, watchlist, *, now=_OPEN_NOW, tranches=None,
                  max_positions=5, weight=0.05):
    cfg = load_config()
    v = cfg.raw.setdefault("value_trade", {})
    v["enabled"] = True
    v["max_positions"] = max_positions
    if tranches is not None:
        v["tranches"] = tranches
    acct = PaperAccount(cash={"KR": 10_000_000, "US": 10_000},
                        state_path=tmp_path / "pa.json")
    gate = RiskGate({"capital": cfg.risk.get("capital", {}), "max_position_pct": 0.2,
                     "max_positions": 5, "daily_loss_limit_pct": 0.05,
                     "max_order_notional": {"KR": 5_000_000, "US": 50_000},
                     "kill_switch_file": str(tmp_path / "HALT")})
    broker = Broker(account=acct, gate=gate, mode="paper")
    risk = RiskManager(capital=cfg.risk.get("capital", {}), max_position_pct=0.2)
    store = Store(tmp_path / "bot.db")
    wl_path = tmp_path / "wl.json"
    wl_path.write_text(json.dumps(watchlist, ensure_ascii=False), encoding="utf-8")
    # holder 로 시계(now)와 캔들 밴드를 사이클 사이에 바꿀 수 있게 한다(가격 변화 재현).
    holder = {"now": now, "hist": (900.0, 1100.0)}
    runner = ValueRunner(
        cfg, store, broker, risk, _mock_llm_factory(weight), None,
        fetch_history_fn=lambda s, m: _synth_uptrend(*holder["hist"]),
        price_fn=None, watchlist_path=wl_path,
        state_path=tmp_path / "state.json", now_fn=lambda: holder["now"])
    return runner, store, holder


def test_entry_records_tranche_meta(tmp_path):
    """신규 진입 meta 에 tranches/tranche_idx/last_tranche_at 이 기록된다."""
    wl = {"900001": _entry(name="밸류1", conviction=0.7, fair_low_pct=30.0)}
    runner, store, _ = _build_runner(tmp_path, wl, tranches=[0.5, 0.5])
    runner.run()
    meta = json.loads(store.get_open_positions()[0]["meta"])
    assert meta["tranches"] == [0.5, 0.5] and meta["tranche_idx"] == 1
    assert meta["last_tranche_at"] == _OPEN_NOW


def test_second_tranche_updates_row_qty_avg_and_stop(tmp_path):
    """추가 체결은 새 행이 아니라 기존 행 갱신 — 손절가는 새 평단 기준 재계산."""
    wl = {"900001": _entry(name="밸류1", conviction=0.7, fair_low_pct=30.0)}
    runner, store, holder = _build_runner(tmp_path, wl, tranches=[0.5, 0.5])
    runner.run()
    row1 = store.get_open_positions()[0]
    qty1, avg1, stop1 = row1["qty"], row1["avg_price"], row1["stop_price"]
    # 8일 뒤(최소 간격 7일 경과) + 가격 하락 상태로 다시 사이클(due 도 다시 열린다).
    holder["now"] = _OPEN_NOW + 8 * 86400
    holder["hist"] = (720.0, 880.0)
    runner.run()
    rows = store.get_open_positions()
    assert len(rows) == 1                                   # 행이 늘지 않는다
    row2 = rows[0]
    assert row2["qty"] > qty1                               # 수량 증가
    acct_pos = runner.broker.account.position("900001")
    assert row2["qty"] == acct_pos.qty                      # 브로커 계좌 실제값과 일치
    assert row2["avg_price"] == acct_pos.avg_price
    assert row2["avg_price"] < avg1                          # 낮은 가격에 추가 → 평단 하락
    # 손절가는 옛 평단이 아니라 **새 평단** 기준으로 재계산돼야 한다.
    assert abs(row2["stop_price"] - round(acct_pos.avg_price * 0.8, 2)) < 1e-6
    assert row2["stop_price"] < stop1
    meta = json.loads(row2["meta"])
    assert meta["tranche_idx"] == 2
    assert meta["last_tranche_at"] == holder["now"]
    kinds = [r["kind"] for r in store.conn.execute("SELECT kind FROM events")]
    assert "value_tranche" in kinds and kinds.count("value_entry") == 1


def test_max_positions_does_not_block_additional_tranche(tmp_path):
    """max_positions 는 신규 종목 수 상한 — 보유 종목의 추가 트랜치는 막지 않는다."""
    wl = {"900001": _entry(name="밸류1", conviction=0.7, fair_low_pct=30.0)}
    runner, store, holder = _build_runner(tmp_path, wl, tranches=[0.5, 0.5],
                                          max_positions=1)
    runner.run()
    assert len(store.get_open_positions()) == 1             # 상한 도달
    holder["now"] = _OPEN_NOW + 8 * 86400
    summary = runner.run()
    assert summary["markets"]["KR"].get("skip") is None     # 스킵되지 않음
    assert summary["markets"]["KR"]["filled"] == 1
    assert json.loads(store.get_open_positions()[0]["meta"])["tranche_idx"] == 2


def test_default_tranches_second_run_does_not_add(tmp_path):
    """하위호환: 기본값([1.0])이면 다음 사이클에 추가 매수가 일어나지 않는다."""
    wl = {"900001": _entry(name="밸류1", conviction=0.7, fair_low_pct=30.0)}
    runner, store, holder = _build_runner(tmp_path, wl)       # tranches 미설정
    runner.run()
    row1 = store.get_open_positions()[0]
    holder["now"] = _OPEN_NOW + 30 * 86400
    summary = runner.run()
    assert summary["markets"]["KR"]["candidates"] == 0        # 후보에서 제외 유지
    row2 = store.get_open_positions()[0]
    assert row2["qty"] == row1["qty"] and row2["avg_price"] == row1["avg_price"]


# ── 5) 대시보드 밸류 탭 ───────────────────────────────────────────
def _dash_data(now=None, positions=(), **kw):
    d = {"db": True, "now": now or time.time(), "hb": None, "hb_age": None,
         "kr_session": "closed", "us_session": "closed", "names": {},
         "positions": list(positions), "pos_px": {}, "events": [], "tally": {},
         "decisions": [], "dossiers": [], "disclosures": [], "live_trades": [],
         "athena_runs": [], "closed_pos": [], "paper": None, "trades": None,
         "alpha": [], "base_rates": None, "snapshot": None, "live_mode": False,
         "value_cfg": {"sleeve_pct": 0.6, "brain_reserve_pct": 0.3,
                       "time_stop_days": 120, "markets": ["KR", "US"],
                       "capital": {"KR": 1_000_000}, "max_gross_exposure": 0.9,
                       "exposure_base": "capital", "tranches": [1.0]},
         "value_watchlist": [], "value_decisions": []}
    d.update(kw)
    return d


def test_dashboard_renders_value_tab_wiring():
    html = dash.render(_dash_data())
    assert "id=t-value" in html                              # 라디오
    assert "page-value" in html                              # 페이지
    assert "#t-value:checked~.tabbar label[for=t-value]" in dash.CSS
    assert "#t-value:checked~.page-value" in dash.CSS
    assert "<label for=t-value>밸류</label>" in html


def test_dashboard_value_tab_shows_position_rows():
    now = time.time()
    pos = [{"symbol": "900001", "market": "KR", "state": "open", "strategy": "value",
            "qty": 10, "avg_price": 1000.0, "opened_at": now - 200 * 86400,
            "stop_price": 800.0, "target_price": 1300.0,
            "meta": json.dumps({"source": "value", "fair_low": 1300.0,
                                "fair_high": 1600.0, "tranches": [0.5, 0.5],
                                "tranche_idx": 1})}]
    d = _dash_data(now=now, positions=pos, pos_px={"900001": 1100.0})
    html = dash.render(d)
    assert "밸류 포지션" in html and "밸류 슬리브" in html
    assert "1,300~1,600" in html                              # 적정가 밴드
    assert "초과" in html                                     # 200일 > 120일 시간손절
    assert "1/2" in html                                      # 트랜치 진행


def test_dashboard_sleeve_card_is_dynamic_and_shows_brain_usage():
    """슬리브 카드는 러너와 같은 compute_sleeve 로 계산 — 뇌 사용액이 예산을 줄인다.

    픽스처: equity(현금+평가) · 뇌 포지션 · 밸류 포지션 → 예산/잔여가 러너와 같다.
    """
    now = time.time()
    pos = [{"symbol": "900001", "market": "KR", "state": "open", "strategy": "value",
            "qty": 1000, "avg_price": 95.76, "opened_at": now - 10 * 86400,
            "meta": json.dumps({"source": "value"})},
           {"symbol": "005930", "market": "KR", "state": "open", "strategy": "swing",
            "qty": 1000, "avg_price": 267.5, "opened_at": now - 3 * 86400,
            "meta": json.dumps({})}]
    vcfg = {"sleeve_pct": 0.6, "brain_reserve_pct": 0.3, "time_stop_days": 120,
            "markets": ["KR"], "capital": {"KR": 1_000_000},
            "max_gross_exposure": 0.9, "exposure_base": "equity",
            "tranches": [1.0]}
    snap = {"cash": {"KR": 637_006.0}, "market_value": {"KR": 357_000.0}}
    html = dash._value_html(_dash_data(now=now, positions=pos, value_cfg=vcfg,
                                       snapshot=snap))
    assert "동적 · 상한 60%" in html
    assert "뇌 사용 ₩267,500" in html
    assert "₩596,404" in html                                # 예산(동적)
    assert "잔여 ₩500,644" in html


def test_dashboard_value_tab_survives_missing_files(monkeypatch, tmp_path):
    """데이터 파일이 없어도 예외 없이 빈 섹션으로 렌더된다(read-only 관례)."""
    monkeypatch.setattr(dash, "VALUE_WATCHLIST", tmp_path / "none.json")
    monkeypatch.setattr(dash, "VALUE_DECISIONS", tmp_path / "none.jsonl")
    assert dash._read_value_watchlist() == []
    assert dash._read_value_decisions() == []
    html = dash.render(_dash_data(value_cfg={}))
    assert "저평가 종목 없음" in html and "밸류 판단 기록 없음" in html


def test_dashboard_value_readers_tolerate_broken_files(monkeypatch, tmp_path):
    bad_wl = tmp_path / "wl.json"
    bad_wl.write_text("{not json", encoding="utf-8")
    bad_vd = tmp_path / "vd.jsonl"
    bad_vd.write_text("{broken\n[]\n", encoding="utf-8")
    monkeypatch.setattr(dash, "VALUE_WATCHLIST", bad_wl)
    monkeypatch.setattr(dash, "VALUE_DECISIONS", bad_vd)
    assert dash._read_value_watchlist() == []
    assert dash._read_value_decisions() == []


def test_dashboard_value_watchlist_sorted_and_banded(monkeypatch, tmp_path):
    wl = {"A": {"market": "KR", "stance": "undervalued", "conviction": 0.5,
                "fair_low_pct": 20.0, "fair_high_pct": 40.0,
                "metrics": {"price": 1000.0}, "ts": _OPEN_NOW},
          "B": {"market": "KR", "stance": "undervalued", "conviction": 0.9,
                "metrics": {"price": 500.0}, "ts": _OPEN_NOW},
          "C": {"market": "KR", "stance": "fair", "conviction": 1.0}}
    path = tmp_path / "wl.json"
    path.write_text(json.dumps(wl), encoding="utf-8")
    monkeypatch.setattr(dash, "VALUE_WATCHLIST", path)
    out = dash._read_value_watchlist()
    assert [x["symbol"] for x in out] == ["B", "A"]          # 확신도 내림차순, fair 제외
    assert dash._fair_band(out[1]) == (1200.0, 1400.0)
    assert dash._fair_band(out[0]) == (None, None)           # pct 결손


def test_sort_dossiers_stance_then_newest():
    """강세→중립→약세, 같은 stance 안에서는 최신·고확신 우선."""
    rows = [
        {"symbol": "BEAR_OLD", "stance": "bearish", "created_at": 100.0, "conviction": 0.9},
        {"symbol": "NEUT", "evidence": json.dumps({"stance": "neutral"}),
         "created_at": 300.0, "conviction": 0.5},
        {"symbol": "BULL_OLD", "stance": "bullish", "created_at": 200.0, "conviction": 0.4},
        {"symbol": "BULL_NEW", "stance": "bullish", "created_at": 400.0, "conviction": 0.3},
        {"symbol": "BEAR_NEW", "stance": "bearish", "created_at": 350.0, "conviction": 0.2},
        {"symbol": "BULL_NEW_HI", "stance": "bullish", "created_at": 400.0, "conviction": 0.8},
    ]
    out = dash.sort_dossiers(rows)
    assert [r["symbol"] for r in out] == [
        "BULL_NEW_HI", "BULL_NEW", "BULL_OLD", "NEUT", "BEAR_NEW", "BEAR_OLD",
    ]


def test_name_shows_label_without_code():
    assert dash._name("005930", {"005930": "삼성전자"}) == "삼성전자"
    assert "005930" not in dash._name("005930", {"005930": "삼성전자"})
    assert dash._name("ZZZZ", {}) == "ZZZZ"          # 이름 없으면 코드 폴백


def test_load_names_merges_ranking_and_value(monkeypatch, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "base_universe_KR.txt").write_text("005930,삼성전자\n", encoding="utf-8")
    (data / "base_universe_US.txt").write_text("", encoding="utf-8")
    (data / "ranking_cache.json").write_text(json.dumps({
        "KR": {"rows": [{"symbol": "098460", "name": "고영"}]},
    }), encoding="utf-8")
    (data / "value_watchlist.json").write_text(json.dumps({
        "194700": {"name": "노바렉스", "stance": "undervalued"},
    }), encoding="utf-8")
    (data / "stock_info_cache.json").write_text("{}", encoding="utf-8")
    (data / "universe.yaml").write_text("KR: [{symbol: '005930', name: '삼성전자'}]\n",
                                        encoding="utf-8")
    monkeypatch.setattr(dash, "ROOT", tmp_path)
    m = dash._load_names()
    assert m["005930"] == "삼성전자"
    assert m["098460"] == "고영"
    assert m["194700"] == "노바렉스"


def test_dashboard_value_decisions_flattened(monkeypatch, tmp_path):
    rec = {"ts": "2026-07-21T01:01:27+00:00",
           "proposals": [{"symbol": "004370", "side": "HOLD"},
                         {"symbol": "005930", "side": "BUY"}],
           "verdicts": [{"symbol": "005930", "approved": False}],
           "executed": []}
    path = tmp_path / "vd.jsonl"
    path.write_text(json.dumps(rec) + "\n", encoding="utf-8")
    monkeypatch.setattr(dash, "VALUE_DECISIONS", path)
    out = dash._read_value_decisions()
    assert len(out) == 2 and out[0]["symbol"] == "005930"    # 최신 우선
    assert out[0]["approved"] is False and out[1]["approved"] is None
    html = dash.render(_dash_data(value_decisions=out))
    assert "거부" in html
