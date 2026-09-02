"""agents.pipeline — 전략/손절·목표 계획 + 체결→store positions 미러링 + 데이트레 arm."""
from src.config import load_config
from src.agents.pipeline import (CycleRunner, position_plan, resolve_strategy,
                                 dry_llm_factory, synth_candles)
from src.agents import (MockLLM, DecisionOutput, ValidationOutput, Proposal, ValidationVerdict)
from src.paper_account import PaperAccount
from src.risk_gate import RiskGate, Order
from src.broker import Broker
from src.risk import RiskManager
from src.engine.store import Store


def test_position_plan_strategy_params():
    cfg = load_config()
    # 005930 -> volatility_breakout (config: stop 0.02, target 0.03)
    strat, stop, target = position_plan(cfg, "005930", 100000, "day")
    assert strat == "volatility_breakout"
    assert stop == 98000.0 and target == 103000.0


def test_position_plan_horizon_default_for_unknown():
    cfg = load_config()
    strat, stop, target = position_plan(cfg, "ZZZZ", 100, "swing")
    assert stop == 95.0 and target == 110.0       # 보유기간 기본 5%/10%


class _Prop:
    def __init__(self, strategy=None, params=None):
        self.strategy, self.params = strategy, params


def test_resolve_strategy_brain_choice_wins_and_clamps():
    cfg = load_config()
    # 005930 config=volatility_breakout 인데 뇌가 rsi_reversion + 범위밖 period 지정
    name, params = resolve_strategy(cfg, "005930", _Prop("rsi_reversion",
                                                         {"period": 999, "oversold": 25}))
    assert name == "rsi_reversion"                 # 뇌 선택이 config 매핑을 이김
    assert params["period"] == 50                  # period 999 -> 하드바운드 50 클램프
    assert params["oversold"] == 25.0              # 범위 내는 그대로


def test_resolve_strategy_fallback_to_config():
    cfg = load_config()
    name, params = resolve_strategy(cfg, "005930", None)
    assert name == "volatility_breakout" and "k" in params


def test_resolve_strategy_invalid_choice_ignores_brain_params():
    cfg = load_config()
    # 000660 config=ma_crossover. 뇌가 무효 전략명 -> 폴백 + 뇌 파라미터 무시
    name, params = resolve_strategy(cfg, "000660", _Prop("nonexistent", {"period": 5}))
    assert name == "ma_crossover"
    assert "short" in params and "period" not in params


def _strategy_buy_factory(symbol, strategy, params, horizon="swing"):
    def factory(candidates):
        def responder(schema, system, user):
            if schema is DecisionOutput:
                return DecisionOutput(market_view="x", proposals=[Proposal(
                    symbol=symbol, market="KR", side="BUY", conviction=0.8,
                    horizon=horizon, target_weight=0.2, thesis="전략 직접배정 테스트",
                    key_risks=[], strategy=strategy, params=params)])
            import json as _j
            syms = [p["symbol"] for p in _j.loads(user)["proposals"]]
            return ValidationOutput(verdicts=[ValidationVerdict(
                symbol=s, approved=True, reason="ok") for s in syms])
        return MockLLM(responder)
    return factory


def test_brain_strategy_stored_on_open(tmp_path):
    import json as _j
    store = Store(tmp_path / "t.db")
    # 005930 config=volatility_breakout 인데 뇌가 rsi_reversion 지정 → 그게 저장돼야
    r = _runner_with(tmp_path, store,
                     _strategy_buy_factory("005930", "rsi_reversion", {"period": 10}, "swing"))
    r.run()
    row = store.get_open_positions()[0]
    assert row["strategy"] == "rsi_reversion"
    assert _j.loads(row["meta"])["params"]["period"] == 10      # 뇌 파라미터 반영


def test_brain_strategy_stored_on_arm(tmp_path):
    import json as _j
    store = Store(tmp_path / "t.db")
    r = _runner_with(tmp_path, store,
                     _strategy_buy_factory("005930", "ma_crossover",
                                           {"short": 3, "long": 15}, "day"))
    r.run()
    armed = store.get_armed()[0]
    assert armed["strategy"] == "ma_crossover"
    meta = _j.loads(armed["meta"])
    assert meta["params"]["short"] == 3 and meta["params"]["long"] == 15


def _runner(tmp_path, store):
    cfg = load_config()
    # 이 테스트들은 store 미러링이 관심사 — 도시에 우선 원칙(P4)은 끄고 검증한다
    # (규칙 자체는 test_dossier_integration 에서 별도 검증).
    cfg.raw.setdefault("agents", {})["require_dossier"] = False
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
                       market_state_path=tmp_path / "ms.json")   # 없음 -> ms={}


def test_cycle_mirrors_open_position_to_store(tmp_path):
    store = Store(tmp_path / "t.db")
    r = _runner(tmp_path, store)
    res = r.run()
    assert any(e["status"] == "filled" for e in res.executed)   # dry BUY 체결
    rows = store.get_open_positions()
    assert len(rows) == 1
    row = rows[0]
    assert row["thesis"] and row["strategy"]
    assert row["stop_price"] and row["target_price"]
    assert row["stop_price"] < row["avg_price"] < row["target_price"]


def test_cycle_sync_closes_when_flat(tmp_path):
    store = Store(tmp_path / "t.db")
    r = _runner(tmp_path, store)
    res = r.run()
    assert store.get_open_positions()                            # 보유 생김
    # 전량 매도 후 재동기화 -> store 포지션 닫힘
    for sym, p in list(r.account.positions.items()):
        if p.is_open:
            r.broker.execute(Order(sym, r.account.symbol_market[sym], "SELL",
                                   p.qty, p.avg_price), "test sell")
    r.sync_store_positions(res)
    assert store.get_open_positions() == []


def _day_buy_factory(symbol="005930"):
    """day-horizon BUY 1건을 내고 검증은 승인하는 MockLLM 팩토리."""
    def factory(candidates):
        def responder(schema, system, user):
            if schema is DecisionOutput:
                return DecisionOutput(market_view="day", proposals=[Proposal(
                    symbol=symbol, market="KR", side="BUY", conviction=0.8,
                    horizon="day", target_weight=0.2, thesis="데이트레 진입대기",
                    key_risks=["변동성"])])
            import json as _j
            syms = [p["symbol"] for p in _j.loads(user)["proposals"]]
            return ValidationOutput(verdicts=[ValidationVerdict(
                symbol=s, approved=True, reason="ok") for s in syms])
        return MockLLM(responder)
    return factory


def _runner_with(tmp_path, store, factory):
    cfg = load_config()
    cfg.raw.setdefault("agents", {})["require_dossier"] = False   # P4 규칙은 별도 테스트
    acct = PaperAccount(cash={"KR": 10_000_000, "US": 10_000},
                        state_path=tmp_path / "pa.json")
    gate = RiskGate({"capital": cfg.risk.get("capital", {}), "max_position_pct": 0.2,
                     "max_positions": 5, "daily_loss_limit_pct": 0.05,
                     "max_order_notional": {"KR": 5_000_000, "US": 50_000},
                     "kill_switch_file": str(tmp_path / "HALT")})
    broker = Broker(account=acct, gate=gate, mode="paper")
    risk = RiskManager(capital=cfg.risk.get("capital", {}), max_position_pct=0.2)
    return CycleRunner(cfg, llm_factory=factory, fetch_candles=synth_candles,
                       store=store, broker=broker, risk=risk,
                       journal_path=tmp_path / "dec.jsonl",
                       market_state_path=tmp_path / "ms.json")


def test_cycle_day_proposal_arms_not_fills(tmp_path):
    store = Store(tmp_path / "t.db")
    r = _runner_with(tmp_path, store, _day_buy_factory("005930"))
    res = r.run()
    assert any(e["status"] == "armed" for e in res.executed)
    assert [row["symbol"] for row in store.get_armed()] == ["005930"]
    assert store.get_open_positions() == []                      # 즉시 체결 안 함
    assert r.account.position("005930").qty == 0
    # armed 종목은 전략(005930->volatility_breakout)이 배정됨
    assert store.get_armed()[0]["strategy"] == "volatility_breakout"
    arm_kinds = {e["kind"] for e in store.conn.execute("SELECT kind FROM events").fetchall()}
    assert "arm" in arm_kinds


def test_arm_is_idempotent(tmp_path):
    store = Store(tmp_path / "t.db")
    r = _runner_with(tmp_path, store, _day_buy_factory("005930"))
    r.run()
    r.run()                                                      # 두 번째 사이클
    assert len(store.get_armed()) == 1                           # 중복 arm 안 함


def test_entry_regime_stamped_in_meta(tmp_path):
    """진입 시점 국면이 positions.meta.entry_regime 에 기록된다(regime_flip 트리거 재료)."""
    import json as _j
    (tmp_path / "ms.json").write_text(
        _j.dumps({"regime": {"KR": {"label": "risk_on"}, "US": {"label": "risk_on"}}}),
        encoding="utf-8")
    store = Store(tmp_path / "t.db")
    r = _runner(tmp_path, store)                   # market_state_path=tmp/ms.json
    r.run()
    meta = _j.loads(store.get_open_positions()[0]["meta"])
    assert meta["entry_regime"] == "risk_on"


def test_arm_stamps_entry_regime(tmp_path):
    import json as _j
    (tmp_path / "ms.json").write_text(
        _j.dumps({"regime": {"KR": {"label": "risk_off"}}}), encoding="utf-8")
    store = Store(tmp_path / "t.db")
    r = _runner_with(tmp_path, store, _day_buy_factory("005930"))
    r.run()
    meta = _j.loads(store.get_armed()[0]["meta"])
    assert meta["entry_regime"] == "risk_off"


def test_portfolio_carries_entry_thesis(tmp_path):
    """보유 종목에 store 의 진입 thesis/전략/보유기간이 붙어 뇌 컨텍스트로 전달된다."""
    store = Store(tmp_path / "t.db")
    r = _runner(tmp_path, store)                                 # dry: swing BUY 체결
    r.run()
    pf = r._portfolio()
    assert pf["positions"], "보유가 생겨야 함"
    held = pf["positions"][0]
    assert held["entry_thesis"]                                  # 진입 사유 주입됨
    assert held["strategy"] and "days_held" in held
    assert held["stop_price"] and held["target_price"]


# ── 역할별 모델 분리: 결정 vs 검증 LLM 팩토리 ─────────────────────
def test_val_llm_factory_routes_validation_separately(tmp_path):
    """val_llm_factory 주입 시 검증만 별도 LLM 을 쓴다(결정은 llm_factory)."""
    import json as _j
    cfg = load_config()
    cfg.raw.setdefault("agents", {})["require_dossier"] = False
    used = {"decision": 0, "validation": 0}

    def dec_resp(schema, system, user):
        if schema is DecisionOutput:
            used["decision"] += 1
            return DecisionOutput(market_view="x", proposals=[Proposal(
                symbol="005930", market="KR", side="BUY", conviction=0.8,
                horizon="swing", target_weight=0.1, thesis="t", key_risks=[])])
        raise AssertionError("결정 LLM 이 검증을 처리하면 안 됨")

    def val_resp(schema, system, user):
        if schema is ValidationOutput:
            used["validation"] += 1
            syms = [p["symbol"] for p in _j.loads(user)["proposals"]]
            return ValidationOutput(verdicts=[ValidationVerdict(
                symbol=s, approved=True, reason="ok") for s in syms])
        raise AssertionError("검증 LLM 이 결정을 처리하면 안 됨")

    store = Store(tmp_path / "t.db")
    acct = PaperAccount(cash={"KR": 10_000_000}, state_path=tmp_path / "pa.json")
    gate = RiskGate({"capital": {"KR": 1_000_000}, "max_position_pct": 0.2,
                     "max_positions": 5, "kill_switch_file": str(tmp_path / "H")})
    broker = Broker(account=acct, gate=gate, mode="paper")
    runner = CycleRunner(cfg, llm_factory=lambda c: MockLLM(dec_resp),
                         val_llm_factory=lambda c: MockLLM(val_resp),
                         fetch_candles=synth_candles, store=store, broker=broker,
                         risk=RiskManager(capital={"KR": 1_000_000}, max_position_pct=0.2),
                         journal_path=tmp_path / "d.jsonl",
                         market_state_path=tmp_path / "ms.json")
    runner.run()
    assert used["decision"] == 1 and used["validation"] == 1   # 각자 자기 몫만 처리


# ── 런타임 유니버스 재독: universe_fn 주면 run() 후보가 그 시점 값을 반영 ──────
def _capture_symbols_factory(seen):
    """결정 LLM 이 받은 후보 심볼을 seen 에 기록(제안은 안 냄)하는 팩토리."""
    def factory(candidates):
        seen.append(sorted(c["symbol"] for c in candidates))

        def responder(schema, system, user):
            if schema is DecisionOutput:
                return DecisionOutput(market_view="x", proposals=[])
            return ValidationOutput(verdicts=[])
        return MockLLM(responder)
    return factory


def _runner_with_universe(tmp_path, store, factory, universe_fn):
    cfg = load_config()
    cfg.raw.setdefault("agents", {})["require_dossier"] = False
    acct = PaperAccount(cash={"KR": 10_000_000, "US": 10_000},
                        state_path=tmp_path / "pa.json")
    gate = RiskGate({"capital": cfg.risk.get("capital", {}), "max_position_pct": 0.2,
                     "max_positions": 5, "kill_switch_file": str(tmp_path / "H")})
    broker = Broker(account=acct, gate=gate, mode="paper")
    risk = RiskManager(capital=cfg.risk.get("capital", {}), max_position_pct=0.2)
    return CycleRunner(cfg, llm_factory=factory, fetch_candles=synth_candles,
                       store=store, broker=broker, risk=risk, universe_fn=universe_fn,
                       journal_path=tmp_path / "d.jsonl",
                       market_state_path=tmp_path / "ms.json")


def test_universe_fn_reflected_per_run(tmp_path):
    """universe_fn 이 순차로 다른 유니버스를 반환하면 run() 후보도 그에 맞게 바뀐다."""
    store = Store(tmp_path / "t.db")
    universes = iter([{"KR": [{"symbol": "005930"}]},
                      {"KR": [{"symbol": "000660"}]}])
    seen = []
    r = _runner_with_universe(tmp_path, store, _capture_symbols_factory(seen),
                              universe_fn=lambda: next(universes))
    r.run()
    r.run()
    assert seen == [["005930"], ["000660"]]        # 매 run 마다 그 시점 유니버스 반영


def test_universe_fn_absent_uses_frozen_items(tmp_path):
    """universe_fn 미지정 시 기동 시 고정된 self.items 를 그대로 씀(하위호환)."""
    store = Store(tmp_path / "t.db")
    seen = []
    r = _runner(tmp_path, store)
    r.llm_factory = _capture_symbols_factory(seen)    # 후보 심볼만 캡처
    frozen = sorted(it["symbol"] for it in r.items)
    r.run()
    r.run()
    assert seen == [frozen, frozen]                   # 두 run 모두 기동 시 유니버스


# ── 열린 시장 필터: open_markets_fn 주면 닫힌 시장 후보 제외(opt-in) ──────────
def _runner_open_filter(tmp_path, store, factory, universe_fn, open_markets_fn):
    cfg = load_config()
    cfg.raw.setdefault("agents", {})["require_dossier"] = False
    acct = PaperAccount(cash={"KR": 10_000_000, "US": 10_000},
                        state_path=tmp_path / "pa.json")
    gate = RiskGate({"capital": cfg.risk.get("capital", {}), "max_position_pct": 0.2,
                     "max_positions": 5, "kill_switch_file": str(tmp_path / "H")})
    broker = Broker(account=acct, gate=gate, mode="paper")
    risk = RiskManager(capital=cfg.risk.get("capital", {}), max_position_pct=0.2)
    return CycleRunner(cfg, llm_factory=factory, fetch_candles=synth_candles,
                       store=store, broker=broker, risk=risk, universe_fn=universe_fn,
                       open_markets_fn=open_markets_fn,
                       journal_path=tmp_path / "d.jsonl",
                       market_state_path=tmp_path / "ms.json")


def test_open_markets_fn_excludes_closed_market(tmp_path):
    """open_markets_fn 이 ['US'] 만 주면 KR 후보는 뇌에게 안 감(닫힌 시장 제외)."""
    store = Store(tmp_path / "t.db")
    seen = []
    uni = lambda: {"KR": [{"symbol": "005930"}], "US": [{"symbol": "AAPL"}]}
    r = _runner_open_filter(tmp_path, store, _capture_symbols_factory(seen),
                            universe_fn=uni, open_markets_fn=lambda: ["US"])
    r.run()
    assert seen == [["AAPL"]]                # KR 제외, US 만


def test_open_markets_fn_none_no_filter(tmp_path):
    """open_markets_fn=None(기본) 이면 무필터 — 모든 시장 후보가 그대로(하위호환)."""
    store = Store(tmp_path / "t.db")
    seen = []
    uni = lambda: {"KR": [{"symbol": "005930"}], "US": [{"symbol": "AAPL"}]}
    r = _runner_open_filter(tmp_path, store, _capture_symbols_factory(seen),
                            universe_fn=uni, open_markets_fn=None)
    r.run()
    assert seen == [["005930", "AAPL"]]      # 무필터: 정렬상 005930 < AAPL


def test_open_markets_fn_empty_excludes_all(tmp_path):
    """양 시장 다 닫히면(빈 리스트) 후보 0 — 뇌는 신규 진입 후보를 못 받는다."""
    store = Store(tmp_path / "t.db")
    seen = []
    uni = lambda: {"KR": [{"symbol": "005930"}], "US": [{"symbol": "AAPL"}]}
    r = _runner_open_filter(tmp_path, store, _capture_symbols_factory(seen),
                            universe_fn=uni, open_markets_fn=lambda: [])
    r.run()
    assert seen == [[]]


def test_athena_done_keeps_live_market_when_closed(tmp_path):
    """athena_done + wake.market=KR 이면 프리 전·US 후보도 KR 만 남긴다."""
    store = Store(tmp_path / "t.db")
    seen = []
    uni = lambda: {"KR": [{"symbol": "005930"}], "US": [{"symbol": "AAPL"}]}
    r = _runner_open_filter(tmp_path, store, _capture_symbols_factory(seen),
                            universe_fn=uni, open_markets_fn=lambda: [])
    r.cfg.raw.setdefault("broker", {})["live_markets"] = ["KR", "US"]
    r.run(wake={"reason": "athena_done", "market": "KR"})
    assert seen == [["005930"]]


def test_athena_done_us_market_only(tmp_path):
    """US Athena 직후 wake 는 US 종목만."""
    store = Store(tmp_path / "t.db")
    seen = []
    uni = lambda: {"KR": [{"symbol": "005930"}], "US": [{"symbol": "AAPL"}]}
    r = _runner_open_filter(tmp_path, store, _capture_symbols_factory(seen),
                            universe_fn=uni, open_markets_fn=lambda: [])
    r.run(wake={"reason": "athena_done", "market": "US"})
    assert seen == [["AAPL"]]


# ── 유동성 필터: illiquid_fn 주면 시간외 체결정지 종목을 후보에서 제외(opt-in) ──
def _runner_illiquid(tmp_path, store, factory, universe_fn, illiquid_fn):
    cfg = load_config()
    cfg.raw.setdefault("agents", {})["require_dossier"] = False
    acct = PaperAccount(cash={"KR": 10_000_000, "US": 10_000},
                        state_path=tmp_path / "pa.json")
    gate = RiskGate({"capital": cfg.risk.get("capital", {}), "max_position_pct": 0.2,
                     "max_positions": 5, "kill_switch_file": str(tmp_path / "H")})
    broker = Broker(account=acct, gate=gate, mode="paper")
    risk = RiskManager(capital=cfg.risk.get("capital", {}), max_position_pct=0.2)
    return CycleRunner(cfg, llm_factory=factory, fetch_candles=synth_candles,
                       store=store, broker=broker, risk=risk, universe_fn=universe_fn,
                       illiquid_fn=illiquid_fn,
                       journal_path=tmp_path / "d.jsonl",
                       market_state_path=tmp_path / "ms.json")


def test_illiquid_fn_excludes_stale_symbols(tmp_path):
    """프리장에 체결이 없는 종목(삼성전자우 등)은 뇌 신규진입 후보에서 빠진다."""
    store = Store(tmp_path / "t.db")
    seen = []
    uni = lambda: {"KR": [{"symbol": "005930"}, {"symbol": "005935"}]}
    r = _runner_illiquid(tmp_path, store, _capture_symbols_factory(seen),
                         universe_fn=uni, illiquid_fn=lambda: {"005935"})
    r.run()
    assert seen == [["005930"]]


def test_illiquid_fn_none_or_empty_no_filter(tmp_path):
    """illiquid_fn=None(기본)이거나 빈 집합이면 무필터(하위호환)."""
    uni = lambda: {"KR": [{"symbol": "005930"}, {"symbol": "005935"}]}
    for fn in (None, lambda: set()):
        store = Store(tmp_path / f"t{fn is None}.db")
        seen = []
        r = _runner_illiquid(tmp_path, store, _capture_symbols_factory(seen),
                             universe_fn=uni, illiquid_fn=fn)
        r.run()
        assert seen == [["005930", "005935"]]


def test_build_live_llm_model_override():
    """model_override 가 cli 클라이언트 모델을 바꾼다(검증 전용 모델 분리)."""
    from src.agents.pipeline import build_live_llm
    cfg = load_config()
    base = (cfg.raw.get("agents") or {}).get("claude_model")   # config 기본(결정 모델)
    dec = build_live_llm(cfg, use_cli=True, subscription=False, api_key=None)
    val = build_live_llm(cfg, use_cli=True, subscription=False, api_key=None,
                         model_override="sonnet")
    # override 없으면 config 기본, 있으면 그 모델 — config 값에 무관하게 분리 검증
    assert dec.model == base and val.model == "sonnet"


# ── source 태그 배관: universe item 의 source → positions/armed meta ──────────
# gem 발굴 종목은 source:"gem" 태그를 달고 유니버스에 실린다. _arm/sync_store_positions
# 가 이 source 를 meta 에 심어 성과 귀속(gem vs 메인)이 가능해야 한다. source 없는 기존
# item 은 meta 에 "source" 키가 아예 없어야 한다(하위호환).
def _runner_with_universe_items(tmp_path, store, factory, universe):
    """universe(={market:[item,...]})를 cfg.raw 에 심고 runner 구성.

    cfg.universe 는 raw['universe'] 를 읽는 읽기전용 property 라 raw 를 직접 덮어쓴다.
    universe_fn 미지정 → _universe_item 이 이 cfg.universe 를 본다.
    """
    cfg = load_config()
    cfg.raw.setdefault("agents", {})["require_dossier"] = False
    cfg.raw["universe"] = universe
    acct = PaperAccount(cash={"KR": 10_000_000, "US": 10_000},
                        state_path=tmp_path / "pa.json")
    gate = RiskGate({"capital": cfg.risk.get("capital", {}), "max_position_pct": 0.2,
                     "max_positions": 5, "daily_loss_limit_pct": 0.05,
                     "max_order_notional": {"KR": 5_000_000, "US": 50_000},
                     "kill_switch_file": str(tmp_path / "HALT")})
    broker = Broker(account=acct, gate=gate, mode="paper")
    risk = RiskManager(capital=cfg.risk.get("capital", {}), max_position_pct=0.2)
    return CycleRunner(cfg, llm_factory=factory, fetch_candles=synth_candles,
                       store=store, broker=broker, risk=risk,
                       journal_path=tmp_path / "dec.jsonl",
                       market_state_path=tmp_path / "ms.json")


_GEM_UNIVERSE = {"KR": [{"symbol": "005930", "name": "삼성전자",
                        "strategy": "volatility_breakout", "source": "gem"}]}
_PLAIN_UNIVERSE = {"KR": [{"symbol": "005930", "name": "삼성전자",
                          "strategy": "volatility_breakout"}]}   # source 없음


def test_arm_stamps_source_from_universe(tmp_path):
    """day BUY → _arm 경로: universe item 의 source:"gem" 이 armed meta 에 실린다."""
    import json as _j
    store = Store(tmp_path / "t.db")
    r = _runner_with_universe_items(tmp_path, store, _day_buy_factory("005930"),
                                    _GEM_UNIVERSE)
    r.run()
    armed = store.get_armed()
    assert [row["symbol"] for row in armed] == ["005930"]
    assert _j.loads(armed[0]["meta"])["source"] == "gem"


def test_fill_stamps_source_from_universe(tmp_path):
    """swing 즉시체결 → sync_store_positions 경로: source:"gem" 이 position meta 에 실린다."""
    import json as _j
    store = Store(tmp_path / "t.db")
    # swing BUY(즉시 체결) — _strategy_buy_factory 는 horizon 기본 swing.
    r = _runner_with_universe_items(
        tmp_path, store,
        _strategy_buy_factory("005930", "volatility_breakout", {}, "swing"),
        _GEM_UNIVERSE)
    r.run()
    rows = store.get_open_positions()
    assert [row["symbol"] for row in rows] == ["005930"]
    assert _j.loads(rows[0]["meta"])["source"] == "gem"


def test_arm_omits_source_when_universe_has_none(tmp_path):
    """source 필드 없는 기존 item → armed meta 에 "source" 키 자체가 없다(하위호환)."""
    import json as _j
    store = Store(tmp_path / "t.db")
    r = _runner_with_universe_items(tmp_path, store, _day_buy_factory("005930"),
                                    _PLAIN_UNIVERSE)
    r.run()
    meta = _j.loads(store.get_armed()[0]["meta"])
    assert "source" not in meta


def test_fill_omits_source_when_universe_has_none(tmp_path):
    """source 필드 없는 기존 item → position meta 에 "source" 키 자체가 없다(하위호환)."""
    import json as _j
    store = Store(tmp_path / "t.db")
    r = _runner_with_universe_items(
        tmp_path, store,
        _strategy_buy_factory("005930", "volatility_breakout", {}, "swing"),
        _PLAIN_UNIVERSE)
    r.run()
    meta = _j.loads(store.get_open_positions()[0]["meta"])
    assert "source" not in meta
