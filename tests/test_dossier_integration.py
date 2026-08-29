"""P4 뇌 통합 — 도시에 우선 원칙·확신도 사이징·무효화가 손절·A/B 태그·갭필 창."""
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from src.agents import (DecisionAgent, ValidationAgent, MockLLM, DecisionOutput,
                        Proposal, ValidationOutput, ValidationVerdict)
from src.agents.cycle import CycleResult, run_cycle
from src.agents.pipeline import CycleRunner, dry_llm_factory, synth_candles
from src.attribution import dossier_ab
from src.broker import Broker
from src.config import load_config
from src.engine.store import Store
from src.paper_account import PaperAccount
from src.risk import RiskManager
from src.risk_gate import RiskGate

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import athena as athena_cli  # noqa: E402


def _broker(tmp_path):
    acct = PaperAccount(cash={"KR": 1_000_000}, fee_rate={"KR": 0.0},
                        slippage_bps={"KR": 0.0}, state_path=tmp_path / "acct.json")
    gate = RiskGate({"capital": {"KR": 1_000_000}, "max_position_pct": 0.2,
                     "max_positions": 5, "daily_loss_limit_pct": 0.05,
                     "max_order_notional": {"KR": 500_000},
                     "kill_switch_file": str(tmp_path / "HALT")})
    return Broker(account=acct, gate=gate, client=None, mode="paper")


def _buy(conviction=0.8, horizon="swing"):
    return DecisionOutput(market_view="중립", proposals=[Proposal(
        symbol="005930", market="KR", side="BUY", conviction=conviction,
        horizon=horizon, target_weight=0.2, thesis="t", key_risks=[])])


def _responder(decision):
    def r(schema, system, user):
        if schema is DecisionOutput:
            return decision
        if schema is ValidationOutput:
            syms = [p["symbol"] for p in json.loads(user)["proposals"]]
            return ValidationOutput(verdicts=[ValidationVerdict(
                symbol=s, approved=True, reason="ok") for s in syms])
        raise AssertionError(schema)
    return r


def _cycle(tmp_path, decision, **kw):
    llm = MockLLM(_responder(decision))
    broker = _broker(tmp_path)
    res = run_cycle(context_json="{}", decision_agent=DecisionAgent(llm),
                    validation_agent=ValidationAgent(llm, min_conviction=0.6),
                    broker=broker,
                    risk=RiskManager(capital={"KR": 1_000_000}, max_position_pct=0.5),
                    price_lookup={"005930": 1000},
                    journal_path=tmp_path / "d.jsonl", **kw)
    return res, broker


# ── 도시에 우선 원칙 (run_cycle 하드가드) ──────────────────────────
def test_swing_buy_blocked_without_dossier(tmp_path):
    res, broker = _cycle(tmp_path, _buy(), dossier_fn=lambda s: False)
    assert res.executed[0]["status"] == "no_dossier"
    assert broker.account.position("005930").qty == 0            # 체결 안 됨


def test_swing_buy_allowed_with_dossier(tmp_path):
    res, broker = _cycle(tmp_path, _buy(), dossier_fn=lambda s: True)
    assert res.executed[0]["status"] == "filled"


def test_day_buy_exempt_from_dossier_rule(tmp_path):
    armed = []
    res, _ = _cycle(tmp_path, _buy(horizon="day"), dossier_fn=lambda s: False,
                    arm_fn=lambda p, price: armed.append(p.symbol) or True)
    assert res.executed[0]["status"] == "armed" and armed == ["005930"]


def test_close_scan_buy_exempt_from_dossier(tmp_path):
    res, broker = _cycle(
        tmp_path, _buy(horizon="close_scan"), dossier_fn=lambda s: False,
        wake_reason="gap_rebound_scan",
        features_by_sym={"005930": {"pool": "gap_decline", "source": "gap_rebound",
                                    "pool_date": "2026-08-29", "market": "KR"}})
    assert res.executed[0]["status"] == "filled"
    assert broker.account.position("005930").qty > 0


def test_gap_scan_coerces_swing_to_close_scan(tmp_path):
    res, broker = _cycle(
        tmp_path, _buy(horizon="swing"), dossier_fn=lambda s: False,
        wake_reason="gap_rebound_scan",
        features_by_sym={"005930": {"pool": "gap_decline", "pool_date": "2026-08-29",
                                    "market": "KR"}})
    assert res.executed[0]["status"] == "filled"
    assert res.decision.proposals[0].horizon == "close_scan"


def test_gap_scan_rejects_day_horizon(tmp_path):
    res, broker = _cycle(
        tmp_path, _buy(horizon="day"), dossier_fn=lambda s: False,
        wake_reason="gap_rebound_scan",
        features_by_sym={"005930": {"pool": "gap_decline", "pool_date": "2026-08-29",
                                    "market": "KR"}})
    assert res.executed[0]["status"] == "wrong_horizon"
    assert broker.account.position("005930").qty == 0


def test_close_scan_horizon_rejected_off_gap_track(tmp_path):
    """periodic 등 — horizon=close_scan 만으로 도시레 면제 우회 금지."""
    res, broker = _cycle(
        tmp_path, _buy(horizon="close_scan"), dossier_fn=lambda s: False,
        wake_reason="periodic",
        features_by_sym={"005930": {"pool": "universe"}})
    assert res.executed[0]["status"] == "wrong_horizon"
    assert broker.account.position("005930").qty == 0


def test_gap_pool_tag_does_not_block_swing_on_periodic(tmp_path):
    """전일 pool_date — active 갭 트랙 아님 → periodic 스윙 허용."""
    res, broker = _cycle(
        tmp_path, _buy(horizon="swing"), dossier_fn=lambda s: True,
        wake_reason="periodic",
        features_by_sym={"005930": {"pool": "gap_decline", "source": "gap_rebound",
                                  "pool_date": "2026-08-28", "market": "KR"}})
    assert res.executed[0]["status"] == "filled"
    assert broker.account.position("005930").qty > 0


def test_fresh_gap_pool_blocks_swing_on_periodic(tmp_path):
    """당일 pool_date 갭풀 — gap scan 외 스윙 BUY 금지."""
    res, broker = _cycle(
        tmp_path, _buy(horizon="swing"), dossier_fn=lambda s: True,
        wake_reason="periodic",
        features_by_sym={"005930": {"pool": "gap_decline", "source": "gap_rebound",
                                  "pool_date": "2026-08-29", "market": "KR"}})
    assert res.executed[0]["status"] == "wrong_horizon"
    assert broker.account.position("005930").qty == 0


def test_fresh_gap_pool_blocks_position_and_day_on_extra(tmp_path):
    for hz in ("position", "day", "close_scan"):
        res, broker = _cycle(
            tmp_path, _buy(horizon=hz), dossier_fn=lambda s: True,
            wake_reason="extra",
            features_by_sym={"005930": {"pool": "gap_decline", "pool_date": "2026-08-29",
                                        "market": "KR"}})
        assert res.executed[0]["status"] == "wrong_horizon", hz
        assert broker.account.position("005930").qty == 0


def test_fresh_gap_pool_blocks_swing_overlap_tag_on_periodic(tmp_path):
    """pool=swing 이라도 당일 갭풀 overlay 면 periodic BUY 차단."""
    res, broker = _cycle(
        tmp_path, _buy(horizon="swing"), dossier_fn=lambda s: True,
        wake_reason="periodic",
        features_by_sym={"005930": {"pool": "swing", "pool_date": "2026-08-29",
                                    "source": "gap_rebound", "market": "KR"}})
    assert res.executed[0]["status"] == "wrong_horizon"
    assert broker.account.position("005930").qty == 0


def test_no_dossier_fn_keeps_old_behavior(tmp_path):
    res, _ = _cycle(tmp_path, _buy())                            # dossier_fn 미주입
    assert res.executed[0]["status"] == "filled"                 # 하위호환


# ── 확신도 사이징 ──────────────────────────────────────────────────
def test_conviction_sizing_scales_qty(tmp_path):
    _, b_plain = _cycle(tmp_path, _buy(conviction=0.8))
    plain_qty = b_plain.account.position("005930").qty           # 0.2 → 200주
    tmp2 = tmp_path / "conv"
    tmp2.mkdir()
    _, b_conv = _cycle(tmp2, _buy(conviction=0.8), conviction_sizing=True)
    conv_qty = b_conv.account.position("005930").qty             # 0.2×(0.75+0.25×0.8)=0.19 → 190주
    assert plain_qty == 200 and conv_qty == 190


# ── CycleRunner 도시에 헬퍼 + 무효화가 손절 + A/B 태그 ─────────────
def _runner(tmp_path, store, broker=None):
    cfg = load_config()
    return CycleRunner(cfg, llm_factory=dry_llm_factory, fetch_candles=synth_candles,
                       store=store, broker=broker,
                       journal_path=tmp_path / "d.jsonl",
                       market_state_path=tmp_path / "ms.json")


def test_has_bullish_dossier_checks_stance_and_freshness(tmp_path):
    store = Store(tmp_path / "t.db")
    r = _runner(tmp_path, store)
    assert not r._has_bullish_dossier("005930")                  # 도시에 없음
    store.save_dossier("005930", "KR", thesis="x", evidence={"stance": "neutral"})
    assert not r._has_bullish_dossier("005930")                  # neutral 은 매수 근거 아님
    store.save_dossier("005930", "KR", thesis="y", evidence={"stance": "bullish"})
    assert r._has_bullish_dossier("005930")


def test_sync_uses_dossier_levels_and_tags(tmp_path):
    store = Store(tmp_path / "t.db")
    broker = _broker(tmp_path)
    did = store.save_dossier("005930", "KR", thesis="업사이클", entry_low=950,
                             entry_high=1050, invalidation=880, target=1400, rr=2.5,
                             conviction=0.7, evidence={"stance": "bullish"})
    r = _runner(tmp_path, store, broker=broker)
    broker.account.fill("005930", "KR", "BUY", 100, 1000)        # 진입 체결
    res = CycleResult(decision=_buy(), validation=ValidationOutput(verdicts=[]),
                      executed=[])
    r.sync_store_positions(res)
    row = store.get_open_positions()[0]
    assert row["stop_price"] == 880 and row["target_price"] == 1400   # 도시에 레벨 사용
    meta = json.loads(row["meta"])
    assert meta["dossier_id"] == did                             # A/B 귀속 태그


def test_candidates_get_dossier_attached(tmp_path):
    store = Store(tmp_path / "t.db")
    store.save_dossier("005930", "KR", thesis="테스트", entry_low=900, entry_high=1000,
                       invalidation=850, target=1200, rr=2.0, conviction=0.6,
                       evidence={"stance": "bullish"})
    r = _runner(tmp_path, store)
    b = r._dossier_brief("005930")
    assert b["stance"] == "bullish" and b["invalidation"] == 850
    assert b["age_hours"] < 1


# ── 도시에 A/B 귀속 ────────────────────────────────────────────────
def test_dossier_ab_buckets(tmp_path):
    store = Store(tmp_path / "t.db")
    p1 = store.open_position("AAA", "KR", 10, 100, meta={"dossier_id": 7})
    store.close_position(p1, exit_price=120, reason="target_hit")     # +200 (도시에)
    p2 = store.open_position("BBB", "KR", 10, 100, meta={})
    store.close_position(p2, exit_price=90, reason="stop_hit")        # -100 (비도시에)
    ab = dossier_ab(store)
    assert ab["with_dossier"]["trades"] == 1 and ab["with_dossier"]["win_rate"] == 1.0
    assert ab["without_dossier"]["trades"] == 1 and ab["without_dossier"]["win_rate"] == 0.0


# ── KR 갭필 창(윈도우 복수형) ──────────────────────────────────────
def test_window_list_form_with_gap():
    KST = ZoneInfo("Asia/Seoul")
    acfg = {"windows": {"KR": [{"start": "05:30", "stop": "08:10"},
                               {"start": "08:20", "stop": "08:55"}],
                        "US": [{"start": "15:40", "stop": "21:50"}]}}
    main_w = datetime(2026, 7, 3, 6, 0, tzinfo=KST)
    assert athena_cli._window_of(acfg, main_w)[0] == "KR"
    gap = datetime(2026, 7, 3, 8, 35, tzinfo=KST)                 # 갭필 창
    m, stop = athena_cli._window_of(acfg, gap)
    assert m == "KR"
    assert datetime.fromtimestamp(stop, KST).strftime("%H:%M") == "08:55"
    between = datetime(2026, 7, 3, 8, 15, tzinfo=KST)             # 창 사이 틈
    assert athena_cli._window_of(acfg, between) == (None, None)


# ── 공시 큐 재소환 우선순위 ────────────────────────────────────────
def test_select_symbols_prioritizes_disclosure_queue(tmp_path):
    from src.agents.athena import select_symbols
    cfg = load_config()
    cfg.universe["KR"] = [{"symbol": "AAA", "name": "a"}, {"symbol": "BBB", "name": "b"},
                          {"symbol": "CCC", "name": "c"}]
    store = Store(tmp_path / "t.db")
    for s in ("AAA", "BBB", "CCC"):                               # 전부 커버된 상태
        store.save_dossier(s, "KR", thesis="old")
    store.log_event("disclosure", "BBB", {"route": "queue", "report_nm": "공급계약",
                                          "market": "KR"})
    order = [t["symbol"] for t in select_symbols(cfg, store, "KR")]
    assert order[0] == "BBB"                                      # 공시 뜬 종목 최우선


def test_select_symbols_prioritizes_us_earnings_result_queue(tmp_path):
    """US 실적 결과 큐 종목이 미커버/오래된 커버보다 먼저 재소환된다."""
    from src.agents.athena import select_symbols
    cfg = load_config()
    cfg.universe["US"] = [{"symbol": "AAA", "name": "a"}, {"symbol": "NVDA", "name": "n"},
                          {"symbol": "CCC", "name": "c"}]
    store = Store(tmp_path / "t.db")
    for s in ("AAA", "NVDA", "CCC"):
        store.save_dossier(s, "US", thesis="old")
    store.log_event("earnings_result", "NVDA", {
        "market": "US", "route": "queue", "eps_surprise_pct": 12.0})
    order = [t["symbol"] for t in select_symbols(cfg, store, "US")]
    assert order[0] == "NVDA"
    # KR 창에는 US 실적 큐가 섞이지 않는다
    cfg.universe["KR"] = [{"symbol": "005930", "name": "삼성"}]
    store.save_dossier("005930", "KR", thesis="old")
    kr_order = [t["symbol"] for t in select_symbols(cfg, store, "KR")]
    assert "NVDA" not in kr_order
