"""갭 진입 가드 — cycle.run_cycle 존 라우팅 + pipeline 배선 + EntryExecutor 존 모드.

설계: 스윙/장투 BUY 는 도시에 진입존(entry_low~entry_high) 안에서만 즉시 시장가.
존 위(갭상승)/존 아래(무효화가 위)는 존 재진입 대기(gap_armed), 무효화가 하회는
진입 거부(gap_rejected). armed 로 넘어간 종목은 감시 루프가 EntryExecutor._evaluate_zone
으로 라이브가만 보고 존 진입/해제를 판정한다(캔들 미사용).
"""
import json
import time

from src.agents import (DecisionAgent, ValidationAgent, MockLLM, DecisionOutput,
                        ValidationOutput, Proposal, ValidationVerdict)
from src.agents.cycle import run_cycle
from src.agents.pipeline import CycleRunner, dry_llm_factory, synth_candles
from src.config import load_config
from src.paper_account import PaperAccount
from src.risk_gate import RiskGate
from src.risk import RiskManager
from src.broker import Broker
from src.engine.store import Store
from src.engine.execution import EntryExecutor


# ── (A) cycle.run_cycle 갭 가드 회귀 ─────────────────────────────
def _broker(tmp_path):
    acct = PaperAccount(cash={"KR": 1_000_000}, fee_rate={"KR": 0.0}, slippage_bps={"KR": 0.0},
                        state_path=tmp_path / "acct.json")
    gate = RiskGate({"capital": {"KR": 1_000_000}, "max_position_pct": 0.2, "max_positions": 5,
                     "daily_loss_limit_pct": 0.05, "max_order_notional": {"KR": 500_000},
                     "kill_switch_file": str(tmp_path / "HALT")})
    return Broker(account=acct, gate=gate, client=None, mode="paper")


def _decision_swing_buy(conviction=0.8):
    return DecisionOutput(market_view="중립", proposals=[Proposal(
        symbol="005930", market="KR", side="BUY", conviction=conviction,
        horizon="swing", target_weight=0.2, thesis="수급+모멘텀 정렬", key_risks=["변동성"])])


def _responder(decision, approve=True):
    def r(schema, system, user):
        if schema is DecisionOutput:
            return decision
        if schema is ValidationOutput:
            syms = [p["symbol"] for p in json.loads(user)["proposals"]]
            return ValidationOutput(verdicts=[ValidationVerdict(
                symbol=s, approved=approve, reason="ok") for s in syms])
        raise AssertionError(schema)
    return r


_ZONE = {"entry_low": 950.0, "entry_high": 1050.0, "invalidation": 900.0,
         "target": 1200.0, "expires_at": None}


def _run(tmp_path, price, *, zone_fn=None, arm_records=None, broker=None):
    """swing BUY 1건을 주어진 price 로 돌린다. arm_records 가 주어지면 arm_fn 호출을 기록."""
    broker = broker or _broker(tmp_path)
    llm = MockLLM(_responder(_decision_swing_buy(0.8), approve=True))

    def arm_fn(p, price, zone=None):
        if arm_records is not None:
            arm_records.append({"symbol": p.symbol, "price": price, "zone": zone})
        return True

    return run_cycle(
        context_json="{}", decision_agent=DecisionAgent(llm),
        validation_agent=ValidationAgent(llm, min_conviction=0.6), broker=broker,
        risk=RiskManager(capital={"KR": 1_000_000}, max_position_pct=0.2),
        price_lookup={"005930": price}, journal_path=tmp_path / "d.jsonl",
        arm_fn=arm_fn, zone_fn=zone_fn, entry_zone_tolerance_pct=0.005)


def test_gap_above_zone_arms_with_zone_kwarg(tmp_path):
    """존 위 갭상승(price > entry_high 허용오차): 시장가 미체결, arm_fn 이 zone= 로 호출."""
    records = []
    broker = _broker(tmp_path)
    res = _run(tmp_path, 1100.0, zone_fn=lambda s: dict(_ZONE), arm_records=records,
               broker=broker)
    assert res.executed[0]["status"] == "gap_armed"
    assert broker.position("005930").qty == 0                # 추격 매수 안 함
    assert len(records) == 1 and records[0]["zone"] == _ZONE  # zone= 키워드로 전달됨


def test_below_invalidation_rejected_no_order_no_arm(tmp_path):
    """무효화가 하회: 주문도 arm 도 없음, status=gap_rejected."""
    records = []
    broker = _broker(tmp_path)
    res = _run(tmp_path, 850.0, zone_fn=lambda s: dict(_ZONE), arm_records=records,
               broker=broker)
    assert res.executed[0]["status"] == "gap_rejected"
    assert broker.position("005930").qty == 0
    assert records == []                                     # arm_fn 호출 없음


def test_inside_zone_fills(tmp_path):
    """존 안(entry_low<=price<=entry_high): 기존처럼 즉시 시장가 체결."""
    records = []
    broker = _broker(tmp_path)
    res = _run(tmp_path, 1000.0, zone_fn=lambda s: dict(_ZONE), arm_records=records,
               broker=broker)
    assert res.executed[0]["status"] == "filled"
    assert broker.position("005930").qty > 0
    assert records == []                                     # 존 안이라 arm 안 함


def test_below_zone_above_invalidation_arms(tmp_path):
    """존 아래·무효화가 위(invalidation <= price < entry_low): gap_armed(회복 대기)."""
    records = []
    broker = _broker(tmp_path)
    res = _run(tmp_path, 920.0, zone_fn=lambda s: dict(_ZONE), arm_records=records,
               broker=broker)
    assert res.executed[0]["status"] == "gap_armed"
    assert broker.position("005930").qty == 0
    assert len(records) == 1 and records[0]["zone"] == _ZONE


def test_zone_fn_none_fills_immediately(tmp_path):
    """zone_fn=None: 갭 가드 비활성 — 기존 즉시 체결 그대로."""
    broker = _broker(tmp_path)
    res = _run(tmp_path, 5000.0, zone_fn=None, broker=broker)   # 존 없으면 가격 무관 체결
    assert res.executed[0]["status"] == "filled"
    assert broker.position("005930").qty > 0


def test_day_buy_ignores_zone_fn(tmp_path):
    """day BUY 는 zone_fn 이 있어도 기존 armed 라우팅(status=armed), zone 없이 arm."""
    records = []
    broker = _broker(tmp_path)
    llm = MockLLM(_responder(DecisionOutput(market_view="중립", proposals=[Proposal(
        symbol="005930", market="KR", side="BUY", conviction=0.8, horizon="day",
        target_weight=0.2, thesis="데이트레", key_risks=[])]), approve=True))

    def arm_fn(p, price, zone=None):
        records.append({"symbol": p.symbol, "zone": zone})
        return True

    res = run_cycle(
        context_json="{}", decision_agent=DecisionAgent(llm),
        validation_agent=ValidationAgent(llm, min_conviction=0.6), broker=broker,
        risk=RiskManager(capital={"KR": 1_000_000}, max_position_pct=0.2),
        price_lookup={"005930": 1100.0}, journal_path=tmp_path / "d.jsonl",
        arm_fn=arm_fn, zone_fn=lambda s: dict(_ZONE), entry_zone_tolerance_pct=0.005)
    assert res.executed[0]["status"] == "armed"              # day = 기존 armed 라우팅
    assert len(records) == 1 and records[0]["zone"] is None  # day 는 zone 없이 arm


# ── (B) pipeline._entry_zone / _arm(zone=...) / run() 배선 ──────────
def _pipeline_runner(tmp_path, store, factory=None, *, entry_zone_guard=True):
    cfg = load_config()
    cfg.raw.setdefault("agents", {})["require_dossier"] = False
    cfg.raw["agents"]["entry_zone_guard"] = entry_zone_guard
    acct = PaperAccount(cash={"KR": 10_000_000, "US": 10_000},
                        state_path=tmp_path / "pa.json")
    gate = RiskGate({"capital": cfg.risk.get("capital", {}), "max_position_pct": 0.2,
                     "max_positions": 5, "daily_loss_limit_pct": 0.05,
                     "max_order_notional": {"KR": 5_000_000, "US": 50_000},
                     "kill_switch_file": str(tmp_path / "HALT")})
    broker = Broker(account=acct, gate=gate, mode="paper")
    risk = RiskManager(capital=cfg.risk.get("capital", {}), max_position_pct=0.2)
    return CycleRunner(cfg, llm_factory=(factory or dry_llm_factory),
                       fetch_candles=synth_candles, store=store, broker=broker, risk=risk,
                       journal_path=tmp_path / "dec.jsonl",
                       market_state_path=tmp_path / "ms.json")


def test_entry_zone_returns_dict_when_bullish_complete(tmp_path):
    """bullish + 진입존·무효화가 전부 있으면 _entry_zone 이 dict(정확한 키) 반환."""
    store = Store(tmp_path / "t.db")
    store.save_dossier("005930", "KR", thesis="t", entry_low=950, entry_high=1050,
                       invalidation=900, target=1200, evidence={"stance": "bullish"})
    r = _pipeline_runner(tmp_path, store)
    z = r._entry_zone("005930")
    assert z == {"entry_low": 950, "entry_high": 1050, "invalidation": 900,
                 "target": 1200, "expires_at": z["expires_at"]}
    assert z["expires_at"] is not None                       # ttl 기반 만료 실림


def test_entry_zone_none_when_not_bullish(tmp_path):
    """stance 가 bullish 아니면 None(가드 비활성)."""
    store = Store(tmp_path / "t.db")
    store.save_dossier("005930", "KR", entry_low=950, entry_high=1050, invalidation=900,
                       evidence={"stance": "neutral"})
    r = _pipeline_runner(tmp_path, store)
    assert r._entry_zone("005930") is None


def test_entry_zone_none_when_level_missing(tmp_path):
    """레벨(invalidation) 하나라도 None 이면 None."""
    store = Store(tmp_path / "t.db")
    store.save_dossier("005930", "KR", entry_low=950, entry_high=1050, invalidation=None,
                       evidence={"stance": "bullish"})
    r = _pipeline_runner(tmp_path, store)
    assert r._entry_zone("005930") is None


class _ArmProp:
    def __init__(self):
        self.symbol = "005930"
        self.market = "KR"
        self.horizon = "swing"
        self.target_weight = 0.2
        self.thesis = "존 재진입 대기"
        self.strategy = None
        self.params = None


def test_arm_stores_entry_zone_in_meta(tmp_path):
    """_arm(proposal, price, zone=...) 호출 시 meta.entry_zone(low/high/invalidation) 저장."""
    store = Store(tmp_path / "t.db")
    r = _pipeline_runner(tmp_path, store)
    zone = {"entry_low": 950, "entry_high": 1050, "invalidation": 900,
            "target": 1200, "expires_at": 111.0}
    assert r._arm(_ArmProp(), 1100.0, zone=zone) is True
    row = store.get_armed()[0]
    meta = json.loads(row["meta"])
    assert meta["entry_zone"] == {"low": 950, "high": 1050, "invalidation": 900,
                                  "target": 1200, "expires_at": 111.0}
    assert meta["horizon"] == "swing"                        # day 아님 → 종가청산 제외


def test_arm_without_zone_has_no_entry_zone_key(tmp_path):
    """zone 미지정(day 경로 등)이면 meta 에 entry_zone 키 없음(하위호환)."""
    store = Store(tmp_path / "t.db")
    r = _pipeline_runner(tmp_path, store)
    assert r._arm(_ArmProp(), 1000.0) is True
    meta = json.loads(store.get_armed()[0]["meta"])
    assert "entry_zone" not in meta


def _gap_swing_factory(symbol="005930"):
    """swing BUY 1건(승인) 팩토리 — 갭 가드 배선 통합 검증용."""
    def factory(candidates):
        def responder(schema, system, user):
            if schema is DecisionOutput:
                return DecisionOutput(market_view="x", proposals=[Proposal(
                    symbol=symbol, market="KR", side="BUY", conviction=0.8,
                    horizon="swing", target_weight=0.2, thesis="갭 가드 통합",
                    key_risks=[])])
            syms = [p["symbol"] for p in json.loads(user)["proposals"]]
            return ValidationOutput(verdicts=[ValidationVerdict(
                symbol=s, approved=True, reason="ok") for s in syms])
        return MockLLM(responder)
    return factory


def _seed_gap_dossier(store):
    """synth_candles(005930, KR) 첫 가격 근처보다 훨씬 낮은 진입존 → 항상 존 위 갭."""
    store.save_dossier("005930", "KR", thesis="t", entry_low=100, entry_high=110,
                       invalidation=90, target=200, evidence={"stance": "bullish"})


def test_run_wires_zone_fn_when_guard_on(tmp_path):
    """entry_zone_guard=true: swing BUY 가 존 위라 즉시 체결 대신 gap_armed 로 라우팅."""
    store = Store(tmp_path / "t.db")
    _seed_gap_dossier(store)
    r = _pipeline_runner(tmp_path, store, _gap_swing_factory(), entry_zone_guard=True)
    res = r.run()
    e = next(x for x in res.executed if x["symbol"] == "005930")
    assert e["status"] == "gap_armed"                        # 존 위 → 재진입 대기
    assert r.account.position("005930").qty == 0             # 시장가 미체결
    meta = json.loads(store.get_armed()[0]["meta"])
    assert meta["entry_zone"]["invalidation"] == 90          # 존 정보가 armed 에 실림
    assert meta.get("conviction_sizing") is True
    assert meta.get("conviction") is not None
    assert meta.get("min_lot_conviction") == 0.6


def test_run_no_zone_fn_when_guard_off_fills(tmp_path):
    """entry_zone_guard=false: zone_fn 미배선 → 존 위여도 기존 즉시 체결."""
    store = Store(tmp_path / "t.db")
    _seed_gap_dossier(store)
    r = _pipeline_runner(tmp_path, store, _gap_swing_factory(), entry_zone_guard=False)
    res = r.run()
    e = next(x for x in res.executed if x["symbol"] == "005930")
    assert e["status"] == "filled"                           # 가드 꺼짐 → 즉시 체결
    assert r.account.position("005930").qty > 0


# ── (C) EntryExecutor 존 모드 ────────────────────────────────────
class _CountingGW:
    """FakeGW + candles 호출 카운터(존 모드는 캔들 미사용을 검증)."""
    def __init__(self, candles=None):
        self._c = candles or [{"open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}]
        self.calls = 0

    def candles(self, sym, interval="1m", count=20):
        self.calls += 1
        return self._c


def _ee_broker(tmp_path):
    acct = PaperAccount(cash={"KR": 10_000_000, "US": 10_000},
                        state_path=tmp_path / "pa.json")
    gate = RiskGate({"capital": {"KR": 1_000_000}, "max_order_notional": {},
                     "kill_switch_file": str(tmp_path / "HALT")})
    return Broker(account=acct, gate=gate, mode="paper")


def _arm_zone(store, *, low=950, high=1050, invalidation=900, target=1200, expires_at=None):
    return store.arm_candidate(
        "005930", "KR", strategy="rsi_reversion",
        meta={"horizon": "swing", "target_weight": 0.2,
              "entry_zone": {"low": low, "high": high, "invalidation": invalidation,
                             "target": target, "expires_at": expires_at}})


def _ee(tmp_path, gw, store):
    return EntryExecutor(gw, _ee_broker(tmp_path), RiskManager(capital={"KR": 1_000_000},
                         max_position_pct=0.2), store)


def test_zone_mode_buys_inside_zone(tmp_path):
    """존 안 라이브가 → 진입, promote_armed 로 stop=invalidation·target=target 확정."""
    store = Store(tmp_path / "t.db")
    _arm_zone(store)
    gw = _CountingGW()
    ex = _ee(tmp_path, gw, store)
    r = ex.evaluate(dict(store.get_armed()[0]), "KR", price=1000.0)
    assert r["executed"] is True and r["action"] == "buy"
    opens = store.get_open_positions()
    assert len(opens) == 1
    assert opens[0]["stop_price"] == 900 and opens[0]["target_price"] == 1200
    assert store.get_armed() == []                           # armed → open 승격
    assert gw.calls == 0                                     # 존 모드는 캔들 미사용
    kinds = {e["kind"] for e in store.conn.execute("SELECT kind FROM events").fetchall()}
    assert "entry" in kinds


def test_zone_mode_conviction_sizing_reduces_qty(tmp_path):
    """스윙 갭대기: conviction_sizing 이면 즉시 체결과 같은 비중 식."""
    store = Store(tmp_path / "t.db")
    store.arm_candidate(
        "005930", "KR", strategy="rsi_reversion",
        meta={"horizon": "swing", "target_weight": 0.2, "conviction": 0.56,
              "conviction_sizing": True,
              "entry_zone": {"low": 950, "high": 1050, "invalidation": 900,
                             "target": 1200, "expires_at": None}})
    gw = _CountingGW()
    ex = _ee(tmp_path, gw, store)
    r = ex.evaluate(dict(store.get_armed()[0]), "KR", price=1000.0)
    assert r["executed"] is True
    # 0.2 × (0.5+0.5×0.56) × 1_000_000 / 1000 = 156 → floor
    assert ex.broker.position("005930").qty == 156


def test_zone_mode_legacy_meta_keeps_full_weight(tmp_path):
    """옛 armed(conviction 키 없음)는 비중 그대로."""
    store = Store(tmp_path / "t.db")
    _arm_zone(store)
    gw = _CountingGW()
    ex = _ee(tmp_path, gw, store)
    r = ex.evaluate(dict(store.get_armed()[0]), "KR", price=1000.0)
    assert r["executed"] is True
    assert ex.broker.position("005930").qty == 200   # 0.2 × 1_000_000 / 1000


def test_day_armed_ignores_conviction_sizing(tmp_path):
    """데이트레는 사이클이 사이징 전에 arm — 확신도가 있어도 비중 그대로."""
    store = Store(tmp_path / "t.db")
    store.arm_candidate(
        "005930", "KR", strategy="rsi_reversion",
        meta={"horizon": "day", "target_weight": 0.2, "conviction": 0.38,
              "conviction_sizing": True,
              "entry_zone": {"low": 950, "high": 1050, "invalidation": 900,
                             "target": 1200, "expires_at": None}})
    gw = _CountingGW()
    ex = _ee(tmp_path, gw, store)
    r = ex.evaluate(dict(store.get_armed()[0]), "KR", price=1000.0)
    assert r["executed"] is True
    assert ex.broker.position("005930").qty == 200


def test_zone_mode_min_lot_fills_one_share(tmp_path):
    """스윙 갭대기 고단가: 확신도 OK 면 즉시 체결과 같이 qty=1."""
    store = Store(tmp_path / "t.db")
    store.arm_candidate(
        "004370", "KR", strategy="rsi_reversion",
        meta={"horizon": "swing", "target_weight": 0.15, "conviction": 0.62,
              "conviction_sizing": True, "min_lot_conviction": 0.6,
              "entry_zone": {"low": 350_000, "high": 360_000, "invalidation": 300_000,
                             "target": 400_000, "expires_at": None}})
    acct = PaperAccount(cash={"KR": 1_000_000}, fee_rate={"KR": 0.0},
                        slippage_bps={"KR": 0.0}, state_path=tmp_path / "pa.json")
    gate = RiskGate({"capital": {"KR": 1_000_000}, "max_position_pct": 0.2,
                     "max_positions": 5, "max_order_notional": {"KR": 200_000},
                     "allow_min_lot": True,
                     "kill_switch_file": str(tmp_path / "HALT")})
    broker = Broker(account=acct, gate=gate, mode="paper")
    ex = EntryExecutor(_CountingGW(), broker,
                       RiskManager(capital={"KR": 1_000_000}, max_position_pct=0.2),
                       store)
    r = ex.evaluate(dict(store.get_armed()[0]), "KR", price=357_000)
    assert r["executed"] is True
    assert broker.position("004370").qty == 1


def test_zone_mode_min_lot_skipped_when_conviction_low(tmp_path):
    """갭대기 고단가라도 확신도 문턱 미달이면 사이징 0."""
    store = Store(tmp_path / "t.db")
    store.arm_candidate(
        "004370", "KR", strategy="rsi_reversion",
        meta={"horizon": "swing", "target_weight": 0.15, "conviction": 0.55,
              "conviction_sizing": True, "min_lot_conviction": 0.6,
              "entry_zone": {"low": 350_000, "high": 360_000, "invalidation": 300_000,
                             "target": 400_000, "expires_at": None}})
    gw = _CountingGW()
    ex = _ee(tmp_path, gw, store)
    r = ex.evaluate(dict(store.get_armed()[0]), "KR", price=357_000)
    assert r["executed"] is False and r["reason"] == "사이징 0"
    assert ex.broker.position("004370").qty == 0


def test_day_armed_ignores_min_lot(tmp_path):
    """데이트레 고단가는 min_lot 을 열지 않는다."""
    store = Store(tmp_path / "t.db")
    store.arm_candidate(
        "004370", "KR", strategy="rsi_reversion",
        meta={"horizon": "day", "target_weight": 0.15, "conviction": 0.80,
              "conviction_sizing": True, "min_lot_conviction": 0.6,
              "entry_zone": {"low": 350_000, "high": 360_000, "invalidation": 300_000,
                             "target": 400_000, "expires_at": None}})
    gw = _CountingGW()
    ex = _ee(tmp_path, gw, store)
    r = ex.evaluate(dict(store.get_armed()[0]), "KR", price=357_000)
    assert r["executed"] is False and r["reason"] == "사이징 0"


def test_zone_mode_disarms_below_invalidation(tmp_path):
    """price < invalidation → disarm, 보유·armed 둘 다 사라짐, disarm 이벤트 기록."""
    store = Store(tmp_path / "t.db")
    _arm_zone(store)
    gw = _CountingGW()
    ex = _ee(tmp_path, gw, store)
    r = ex.evaluate(dict(store.get_armed()[0]), "KR", price=850.0)
    assert r["action"] == "disarm" and r["executed"] is False
    assert "무효화" in r["reason"]
    assert store.get_open_positions() == [] and store.get_armed() == []
    kinds = {e["kind"] for e in store.conn.execute("SELECT kind FROM events").fetchall()}
    assert "disarm" in kinds


def test_zone_mode_disarms_when_expired(tmp_path):
    """expires_at 과거 → disarm(dossier_expired)."""
    store = Store(tmp_path / "t.db")
    _arm_zone(store, expires_at=time.time() - 3600)          # 1시간 전 만료
    gw = _CountingGW()
    ex = _ee(tmp_path, gw, store)
    r = ex.evaluate(dict(store.get_armed()[0]), "KR", price=1000.0)   # 존 안이어도 만료 우선
    assert r["action"] == "disarm" and r["executed"] is False
    assert "만료" in r["reason"]
    assert store.get_armed() == []
    payloads = [json.loads(e["payload"]) for e in store.conn.execute(
        "SELECT payload FROM events WHERE kind='disarm'").fetchall()]
    assert any(p.get("reason") == "dossier_expired" for p in payloads)


def test_zone_mode_holds_when_price_none_no_candles(tmp_path):
    """price=None → hold, 캔들 미호출(gw.calls==0)."""
    store = Store(tmp_path / "t.db")
    _arm_zone(store)
    gw = _CountingGW()
    ex = _ee(tmp_path, gw, store)
    r = ex.evaluate(dict(store.get_armed()[0]), "KR", price=None)
    assert r["action"] == "hold" and r["executed"] is False
    assert gw.calls == 0
    assert len(store.get_armed()) == 1                       # 대기 유지


def test_zone_mode_holds_above_zone(tmp_path):
    """price > high(존 위) → hold(대기)."""
    store = Store(tmp_path / "t.db")
    _arm_zone(store)
    gw = _CountingGW()
    ex = _ee(tmp_path, gw, store)
    r = ex.evaluate(dict(store.get_armed()[0]), "KR", price=1100.0)
    assert r["action"] == "hold" and r["executed"] is False
    assert len(store.get_armed()) == 1
    assert gw.calls == 0
