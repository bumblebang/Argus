"""밸류 트랙 V1 — 셀렉터/타이밍게이트/사전가드/슬리브/러너 E2E/due/kind_prefix/검증프롬프트.

전부 tmp·mock 격리(실계좌·실LLM·실 watchlist 무접촉). now_fn 을 KR 장중 고정 시각으로
주입해 due/신선도를 결정적으로 처리한다.
"""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from src.config import load_config
from src.agents.llm import MockLLM
from src.agents.schemas import (DecisionOutput, ValidationOutput, Proposal,
                                ValidationVerdict)
from src.agents.validation_agent import SYSTEM as VAL_SYSTEM
from src.agents.value_trade import (select_candidates, timing_gate, fair_price_low,
                                    passes_margin_guard, value_trade_cfg, ValueRunner,
                                    compute_sleeve)
from src.engine.brain import BrainWorker
from src.engine.store import Store
from src.paper_account import PaperAccount
from src.risk_gate import RiskGate
from src.broker import Broker
from src.risk import RiskManager

_KST = ZoneInfo("Asia/Seoul")
# 2026-07-13(월) 10:00 KST — KR 장중, 휴장일 아님. due/신선도 결정적 처리용.
_OPEN_NOW = datetime(2026, 7, 13, 10, 0, tzinfo=_KST).timestamp()

_CFG_V = {"markets": ["KR", "US"], "dossier_ttl_hours": 400,
          "min_dossier_conviction": 0.4, "sleeve_pct": 0.60,
          "brain_reserve_pct": 0.30, "max_per_run": 2,
          "max_positions": 3, "hard_stop_pct": 0.20}


class _FakeStore:
    """select_candidates 용 최소 store — 보유/진입대기 심볼만 노출."""
    def __init__(self, open_syms=(), armed_syms=()):
        self._open = [{"symbol": s} for s in open_syms]
        self._armed = [{"symbol": s} for s in armed_syms]

    def get_open_positions(self):
        return self._open

    def get_armed(self):
        return self._armed


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


# ── 1) 셀렉터 ──────────────────────────────────────────────────────
def test_selector_filters_stance_conviction_freshness_and_held():
    wl = {
        "A": _entry(conviction=0.8),                              # 통과(가장 높은 확신)
        "B": _entry(conviction=0.5),                              # 통과
        "C": _entry(conviction=0.3),                              # 확신도 미달 → 제외
        "D": _entry(stance="fair", conviction=0.9),              # stance 부적합 → 제외
        "E": _entry(conviction=0.9, ts=_OPEN_NOW - 400 * 3600 - 1),  # 신선도 만료 → 제외
        "F": _entry(conviction=0.9, market="JP"),                # 시장 밖 → 제외
        "G": _entry(conviction=0.9),                             # 보유중 → 제외
        "H": _entry(conviction=0.9),                             # 진입대기 → 제외
    }
    store = _FakeStore(open_syms=["G"], armed_syms=["H"])
    out = select_candidates(wl, store, _CFG_V, _OPEN_NOW)
    assert [c["symbol"] for c in out] == ["A", "B"]              # 확신도 내림차순
    assert out[0]["metrics"]["price"] == 1000.0                  # 항목 전체 담김


def test_selector_rejects_scan_older_than_trade_ttl():
    cfg = {**_CFG_V, "dossier_ttl_hours": 168}
    wl = {
        "A": _entry(ts=_OPEN_NOW - 100 * 3600),
        "B": _entry(ts=_OPEN_NOW - 168 * 3600 - 1),
    }
    out = select_candidates(wl, _FakeStore(), cfg, _OPEN_NOW)
    assert [c["symbol"] for c in out] == ["A"]


# ── 2) 타이밍 게이트 ───────────────────────────────────────────────
def test_timing_gate_passes_on_stabilization():
    df = _synth_uptrend()
    t = timing_gate(df, current_price=1100.0)
    assert t is not None and t["ret_20d_pct"] > 0 and "sma20" in t


def test_timing_gate_fails_below_sma_or_negative_momentum():
    df = _synth_uptrend()
    # 현재가가 SMA20 아래 & 20일 전보다 낮음 → 탈락
    assert timing_gate(df, current_price=800.0) is None


def test_timing_gate_fails_on_insufficient_candles():
    df = _synth_uptrend(n=40)                                    # <60봉
    assert timing_gate(df, current_price=1100.0) is None


# ── 3) 사전 가드 ───────────────────────────────────────────────────
def test_margin_guard_excludes_when_at_or_above_fair_low():
    c = _entry(fair_low_pct=20.0)                                # fair_low = 1200
    assert fair_price_low(c) == 1200.0
    assert passes_margin_guard(c, 1100.0) is True               # 1100 < 1200 → 통과
    assert passes_margin_guard(c, 1200.0) is False              # 도달 → 제외
    assert passes_margin_guard(c, 1300.0) is False              # 초과 → 제외


def test_margin_guard_excludes_when_fair_low_pct_missing():
    c = _entry()
    c.pop("fair_low_pct")
    assert fair_price_low(c) is None
    assert passes_margin_guard(c, 1000.0) is False


# ── 러너 E2E 헬퍼 ──────────────────────────────────────────────────
def _mock_llm_factory(cands):
    def responder(schema, system, user):
        if schema is DecisionOutput:
            props = [Proposal(symbol=c["symbol"], market=c["market"], side="BUY",
                              conviction=0.7, horizon="position", target_weight=0.15,
                              thesis="밸류 진입", key_risks=["r"]) for c in cands]
            return DecisionOutput(market_view="mv", proposals=props)
        syms = [p["symbol"] for p in json.loads(user)["proposals"]]
        return ValidationOutput(verdicts=[ValidationVerdict(
            symbol=s, approved=True, reason="ok") for s in syms])
    return MockLLM(responder)


def _build_runner(tmp_path, watchlist, *, now=_OPEN_NOW, fetch_fn=None):
    cfg = load_config()
    cfg.raw.setdefault("value_trade", {})["enabled"] = True
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
    runner = ValueRunner(
        cfg, store, broker, risk, _mock_llm_factory, None,
        fetch_history_fn=(fetch_fn or (lambda s, m: _synth_uptrend())),
        price_fn=None, watchlist_path=wl_path,
        state_path=tmp_path / "state.json", now_fn=lambda: now)
    return runner, store


# ── 5) 러너 E2E(dry) ──────────────────────────────────────────────
def test_runner_e2e_fills_and_mirrors_to_store(tmp_path):
    wl = {"900001": _entry(name="밸류1", conviction=0.7, fair_low_pct=30.0,
                           metrics={"price": 1000.0})}   # fair_low = 1300, 현재 1100
    runner, store = _build_runner(tmp_path, wl)
    summary = runner.run()
    assert summary["markets"]["KR"]["filled"] == 1
    rows = store.get_open_positions()
    assert len(rows) == 1
    row = rows[0]
    assert row["symbol"] == "900001" and row["strategy"] == "value"
    meta = json.loads(row["meta"])
    assert meta["source"] == "value" and meta["horizon"] == "position"
    entry = row["avg_price"]
    assert abs(row["stop_price"] - entry * 0.8) < 1e-6          # 하드스톱 -20%
    assert row["target_price"] == 1300.0                        # target = fair_price_low
    # 결정 저널(tmp) 기록
    assert (tmp_path / "value_decisions.jsonl").exists()


# ── 6) due 판정: 같은 날 두 번째 run() 은 no-op ──────────────────
def test_second_run_same_day_is_noop(tmp_path):
    wl = {"900001": _entry(name="밸류1", conviction=0.7, fair_low_pct=30.0,
                           metrics={"price": 1000.0})}
    runner, store = _build_runner(tmp_path, wl)
    runner.run()
    assert len(store.get_open_positions()) == 1
    summary2 = runner.run()                                     # 같은 now → due 없음
    assert summary2["due"] == [] and summary2["markets"] == {}
    assert len(store.get_open_positions()) == 1                 # 중복 진입 없음


# ── 4) 슬리브: 예산 소진 / max_positions 스킵 ─────────────────────
def test_sleeve_skips_when_budget_exhausted(tmp_path):
    wl = {"900001": _entry(name="밸류1", conviction=0.7, fair_low_pct=30.0,
                           metrics={"price": 1000.0})}
    runner, store = _build_runner(tmp_path, wl)
    # base = 게이트 기준(exposure_base=capital) 1,000,000. 뇌 유휴라 예산은 절대 상한
    # 1,000,000×0.60 = 600,000(동적: 900,000 − 예비금 300,000 = 600,000). value 로 소진.
    store.open_position("000001", "KR", 600, 1000.0, strategy="value",
                        meta={"source": "value"})
    summary = runner.run()
    assert summary["markets"]["KR"].get("skip") == "sleeve_full"
    assert runner.broker.account.position("900001").qty == 0    # 진입 안 함


def test_sleeve_skips_when_max_positions_reached(tmp_path):
    wl = {"900001": _entry(name="밸류1", conviction=0.7, fair_low_pct=30.0,
                           metrics={"price": 1000.0})}
    runner, store = _build_runner(tmp_path, wl)
    runner.cfg.raw["value_trade"]["max_positions"] = 1          # 상한 1(기본 3)
    # 잔여 예산은 있게(소액) + value 포지션 1개 → count 상한 도달.
    store.open_position("000001", "KR", 1, 1000.0, strategy="value",
                        meta={"source": "value"})
    summary = runner.run()
    assert summary["markets"]["KR"].get("skip") == "max_positions"


def test_value_trade_cfg_sleeve_defaults():
    """슬리브 기본값: 절대 상한 0.60 / 뇌 활주로 0.30 / 밸류 보유 상한 3."""
    cfg = load_config()
    cfg.raw["value_trade"] = {"enabled": True}                  # 값 미지정 → 기본값
    v = value_trade_cfg(cfg)
    assert v["sleeve_pct"] == 0.60
    assert v["brain_reserve_pct"] == 0.30
    assert v["max_positions"] == 3


# ── 9) 동적 슬리브(compute_sleeve) ────────────────────────────────
_SLEEVE_KW = {"sleeve_pct": 0.60, "brain_reserve_pct": 0.30,
              "max_gross_exposure": 0.90}


def test_compute_sleeve_brain_idle_uses_absolute_cap():
    """뇌 유휴 → 절대 상한(base×0.60)까지. 이 설정에선 동적값과 동률이다."""
    s = compute_sleeve(**_SLEEVE_KW, base=1_000_000, value_invested=0,
                       brain_invested=0)
    assert s["budget"] == 600_000.0                             # base×0.60
    assert s["gross_limit"] - s["brain_reserve"] == 600_000.0   # 동적값과 동률
    assert s["room"] == 600_000.0 and s["base"] == 1_000_000.0
    assert s["brain_reserve"] == 300_000.0 and s["gross_limit"] == 900_000.0


def test_compute_sleeve_shrinks_when_brain_exceeds_reserve():
    """뇌가 예비금 초과 사용 → 실사용액이 그대로 차감된다(base×0.90 − base×0.50)."""
    s = compute_sleeve(**_SLEEVE_KW, base=1_000_000, value_invested=0,
                       brain_invested=500_000)
    assert s["budget"] == 400_000.0
    assert s["brain_invested"] == 500_000.0


def test_compute_sleeve_heavy_brain_leaves_negative_room():
    s = compute_sleeve(**_SLEEVE_KW, base=1_000_000, value_invested=60_000,
                       brain_invested=850_000)
    assert s["budget"] == 50_000.0                              # base×0.05
    assert s["room"] == -10_000.0                               # 잔여 음수 → 러너는 스킵


def test_compute_sleeve_absolute_cap_wins_over_dynamic():
    """절대 상한 우선 — 뇌가 유휴여도 sleeve_pct 를 넘지 않는다."""
    s = compute_sleeve(sleeve_pct=0.20, brain_reserve_pct=0.30,
                       max_gross_exposure=0.90, base=1_000_000,
                       value_invested=0, brain_invested=0)
    assert s["budget"] == 200_000.0


def test_compute_sleeve_zero_base_blocks_entry():
    """base<=0(US capital 0) → 예산 0 · 잔여 0 이하 → 러너 스킵 경로."""
    s = compute_sleeve(**_SLEEVE_KW, base=0, value_invested=0, brain_invested=0)
    assert s["budget"] == 0.0 and s["room"] <= 0


def test_compute_sleeve_none_gross_treated_as_one():
    s = compute_sleeve(sleeve_pct=0.60, brain_reserve_pct=0.30,
                       max_gross_exposure=None, base=1_000_000,
                       value_invested=0, brain_invested=0)
    assert s["gross_limit"] == 1_000_000.0                      # gross=1.0
    assert s["budget"] == min(600_000.0, 1_000_000.0 - 300_000.0)


def test_compute_sleeve_matches_live_numbers():
    """슬리브 산식 검산 — 기존 뇌·밸류 포지션이 있을 때 예산·잔여."""
    s = compute_sleeve(sleeve_pct=0.60, brain_reserve_pct=0.30,
                       max_gross_exposure=0.90, base=994_006,
                       value_invested=95_760, brain_invested=267_500)
    assert s["budget"] == 596_403.6
    assert s["room"] == 500_643.6


def test_runner_sleeve_uses_brain_usage_and_skips(tmp_path):
    """러너 통합: 뇌가 base×0.85 를 쓰면 밸류 예산이 base×0.05 로 줄어 LLM 무접촉 스킵."""
    wl = {"900001": _entry(name="밸류1", conviction=0.7, fair_low_pct=30.0,
                           metrics={"price": 1000.0})}
    runner, store = _build_runner(tmp_path, wl)
    # 뇌(비밸류) 850,000 + 밸류 60,000 → 예산 50,000 < 투자 60,000 → 잔여 음수.
    store.open_position("005930", "KR", 850, 1000.0, strategy="swing", meta={})
    store.open_position("000001", "KR", 60, 1000.0, strategy="value",
                        meta={"source": "value"})
    sleeve = runner._sleeve("KR", value_trade_cfg(runner.cfg),
                            runner._value_positions())
    assert sleeve["base"] == 1_000_000.0 and sleeve["brain_invested"] == 850_000.0
    assert sleeve["budget"] == 50_000.0 and sleeve["room"] == -10_000.0
    summary = runner.run()
    assert summary["markets"]["KR"].get("skip") == "sleeve_full"
    assert runner.broker.account.position("900001").qty == 0


# ── 7) BrainWorker kind_prefix ────────────────────────────────────
def test_brainworker_kind_prefix_value(tmp_path):
    store = Store(tmp_path / "t.db")
    bw = BrainWorker(lambda: "ok", store=store, kind_prefix="value")
    bw.wake(); bw.run_pending()
    kinds = [r["kind"] for r in store.conn.execute("SELECT kind FROM events").fetchall()]
    assert "value_start" in kinds and "value_done" in kinds
    assert "brain_start" not in kinds and "brain_done" not in kinds


def test_brainworker_default_prefix_unchanged(tmp_path):
    store = Store(tmp_path / "t.db")
    bw = BrainWorker(lambda: "ok", store=store)                 # 기본 "brain"
    bw.wake(); bw.run_pending()
    kinds = [r["kind"] for r in store.conn.execute("SELECT kind FROM events").fetchall()]
    assert "brain_start" in kinds and "brain_done" in kinds


# ── 8) 검증 프롬프트: track=value 단락 삽입 + 기존 규칙 유지 ──────
def test_validation_prompt_has_value_paragraph_and_keeps_rules():
    assert "track=='value'" in VAL_SYSTEM
    # 밸류는 약세 역행이 전제 + 타이밍 게이트가 안정화를 코드로 강제 → 규칙3(a) 면제.
    assert "안정화 요건은 코드가 이미 강제" in VAL_SYSTEM
    assert "규칙3(a)의 안정화 근거를 다시 요구하지 마라" in VAL_SYSTEM
    # 기존 규칙 텍스트 회귀 없음(대표 규칙 잔존 확인)
    assert "단일 지표 의존" in VAL_SYSTEM
    assert "확신도 미달" in VAL_SYSTEM


# ── 9) 밸류 컨텍스트 시황 배선(국면·공포·focus) ───────────────────
def test_value_build_context_includes_market_state_and_focus(tmp_path):
    """프롬프트 4번(국면)이 읽을 market_state·focus 가 컨텍스트에 실려야 한다."""
    from src.agents.value_trade import VALUE_TRADE_SYSTEM

    ms_path = tmp_path / "market_state.json"
    ms_path.write_text(json.dumps({
        "asof": "2026-08-02T00:00:00+00:00",
        "regime": {"KR": {"label": "risk_off", "n": 12}},
        "sentiment": {
            "fear_greed": {"score": 22, "rating": "extreme_fear"},
            "fear_kr": {"score": 17, "rating": "extreme_fear"},
        },
        "macro_kr": {"bok_base_rate": 2.75},
        "markets": {"KS11": {"change_pct": -1.2}},
        "macro": {}, "sectors": {}, "fx": {}, "flows_market": {},
    }, ensure_ascii=False), encoding="utf-8")

    wl = {"900001": _entry(name="밸류1", conviction=0.7, fair_low_pct=30.0)}
    runner, _store = _build_runner(tmp_path, wl)
    runner.market_state_path = ms_path
    gated = [{
        "symbol": "900001", "name": "밸류1", "market": "KR",
        "_current_price": 1000.0, "stance": "undervalued", "conviction": 0.7,
        "thesis": "저평가", "risks": [], "evidence": [], "fundamentals": {},
        "recent_news": [], "metrics": {"price": 1000.0}, "_timing": {"ok": True},
        "ts": _OPEN_NOW, "fair_low_pct": 30.0, "fair_high_pct": 50.0,
    }]
    ctx = json.loads(runner._build_context(
        "KR", value_trade_cfg(runner.cfg),
        {"budget": 100000, "room": 50000, "invested": 0},
        gated, [], _OPEN_NOW))
    assert ctx["market"] == "KR"
    assert ctx["market_state"]["regime"]["KR"]["label"] == "risk_off"
    assert ctx["market_state"]["sentiment"]["fear_kr"]["rating"] == "extreme_fear"
    assert ctx["market_state"]["macro_kr"]["bok_base_rate"] == 2.75
    assert "focus" in ctx and "lenses" in ctx["focus"]
    assert "market_state" in VALUE_TRADE_SYSTEM
    assert "fear_kr" in VALUE_TRADE_SYSTEM
    assert "fear_kr.incomplete" in VALUE_TRADE_SYSTEM
