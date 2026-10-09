"""지연 체결 BUY — 09-30 삼화콘덴서(001820) 고아 채택 재발 방지.

재현(수정 전): close_scan BUY 를 15:25 종가 동시호가에 지정가로 냄 → PENDING(거부로
기록, 뇌 계획 폐기) → 15:30 단일가 체결 → sweep 은 settled 만 찍고 live_order 없음
(체결 알림 X) → 재대사가 BUY applied 보정으로 working 행을 먼저 지우고 보유를
reconcile_adopted 고아로 채택 → 뇌가 '미추적 보유'로 보고 당일 매도.
동시에 대기 동안 매수여력 홀드(−298,845)를 출금으로 오인해 SoD 기준·원금이 이동.
"""
import json
import time
from datetime import datetime

from src.agents import (DecisionAgent, ValidationAgent, MockLLM, DecisionOutput,
                        Proposal, ValidationOutput, ValidationVerdict)
from src.agents.cycle import run_cycle
from src.broker import Broker, _KST, _call_auction_hold_until
from src.broker_sync import apply_reconcile_from_live, apply_sync_from_live
from src.engine.entry_basis import BASIS_THESIS
from src.engine.store import Store
from src.paper_account import PaperAccount
from src.risk import RiskManager
from src.risk_gate import RiskGate
from src.store_sync import (ENTRY_PLAN_MAX_AGE_SEC, RECONCILE_THESIS,
                            adopt_live_position, is_orphan_store_row,
                            plan_position_fields)

SYM = "001820"


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


class _Client:
    """place → PENDING. 이후 status/filled/avg 를 바꿔 단일가 체결을 흉내."""

    def __init__(self):
        self.status, self.filled, self.avg = "PENDING", 0.0, None
        self.calls, self.canceled = [], []

    def orderbook(self, symbol, market=None):
        return {"asks": [{"price": "1000", "volume": "1000"}],
                "bids": [{"price": "999", "volume": "1000"}]}

    def place_order(self, **kw):
        self.calls.append(kw)
        return {"orderId": f"L{len(self.calls)}"}

    def get_order(self, account_seq, order_id):
        ex = {"filledQuantity": str(self.filled), "commission": "0", "tax": "0"}
        if self.avg is not None:
            ex["averageFilledPrice"] = str(self.avg)
        return {"status": self.status, "execution": ex}

    def get_sellable(self, account_seq, symbol):
        return None

    def cancel_order(self, account_seq, order_id):
        self.canceled.append(order_id)
        return {"ok": True}


def _plan_fn(p):
    return {"strategy": "macd", "thesis": p.thesis,
            "invalidation": None, "target": None,
            "meta": {"horizon": p.horizon, "params": {},
                     "entry_basis": BASIS_THESIS, "dossier_id": None,
                     "conviction": p.conviction}}


def _setup(tmp_path):
    store = Store(tmp_path / "t.db")
    acct = PaperAccount(cash={"KR": 10_000_000}, fee_rate={"KR": 0.0},
                        slippage_bps={"KR": 0.0}, state_path=tmp_path / "a.json")
    gate = RiskGate({"capital": {"KR": 10_000_000}, "max_position_pct": 0.50,
                     "max_positions": 10, "daily_loss_limit_pct": 0.05,
                     "max_order_notional": {},
                     "kill_switch_file": str(tmp_path / "HALT")})
    client = _Client()
    broker = Broker(account=acct, gate=gate, client=client, mode="live",
                    account_seq=1, live_markets=["KR"], store=store,
                    reconcile_poll_attempts=1, reconcile_poll_sec=0.0,
                    max_spread_pct_extended=0.0)
    return store, acct, client, broker


def _run(tmp_path, broker, store, plan_fn=_plan_fn):
    # close_scan 은 갭풀·갭 wake 가 필요해 스윙으로 흉내 — 계획 운반 경로는 같다.
    decision = DecisionOutput(market_view="x", proposals=[Proposal(
        symbol=SYM, market="KR", side="BUY", conviction=0.7,
        horizon="swing", target_weight=0.2, thesis="갭반등 종가 진입",
        key_risks=[])])
    llm = MockLLM(_responder(decision))
    risk = RiskManager(capital={"KR": 10_000_000}, base_position_pct=0.02,
                       max_position_pct=0.50)
    return run_cycle(
        context_json="{}", decision_agent=DecisionAgent(llm),
        validation_agent=ValidationAgent(llm, min_conviction=0.0),
        broker=broker, risk=risk, store=store, price_lookup={SYM: 1000.0},
        journal_path=tmp_path / "d.jsonl", entry_plan_fn=plan_fn)


def _events(store, kind):
    return [json.loads(r["payload"]) for r in store.conn.execute(
        "SELECT payload FROM events WHERE kind=? ORDER BY id", (kind,)).fetchall()]


def _holdings(qty, avg, cash=9_800_000):
    return {"cash": {"KR": cash}, "holdings_ok": True,
            "items": [{"symbol": SYM, "quantity": str(qty),
                       "averagePurchasePrice": str(avg), "marketCountry": "KR"}]}


# ── 1) 사이클: PENDING 은 거부가 아니다 + 계획이 working meta 에 실린다 ──────────
def test_pending_buy_is_pending_and_carries_entry_plan(tmp_path):
    store, _acct, _client, broker = _setup(tmp_path)
    res = _run(tmp_path, broker, store)
    ex = res.executed[0]
    assert ex["status"] == "pending"
    assert ex["order_id"] == "L1"
    rows = store.get_working_orders(SYM, side="BUY")
    assert len(rows) == 1
    plan = json.loads(rows[0]["meta"])["entry_plan"]
    assert plan["thesis"] == "갭반등 종가 진입"
    assert plan["meta"]["horizon"] == "swing"
    assert "manager_epoch" in plan and plan["manager_epoch"]


def test_pending_buy_not_booked_as_blocked_shadow():
    from src.shadow_ledger import SHADOW_BOOK_STATUSES
    assert "pending" not in SHADOW_BOOK_STATUSES


# ── 2) 끝-끝: 단일가 체결 → 알림 → 계획대로 개설(고아 아님) ─────────────────────
def test_deferred_fill_opens_planned_position_not_orphan(tmp_path):
    store, acct, client, broker = _setup(tmp_path)
    _run(tmp_path, broker, store)
    qty = float(client.calls[0]["qty"])

    client.status, client.filled, client.avg = "FILLED", qty, 998.0
    out = broker.sweep_working_orders()
    assert out["settled"] == 1

    fills = _events(store, "live_order")
    assert len(fills) == 1
    assert fills[0]["deferred"] is True
    assert fills[0]["side"] == "BUY" and fills[0]["qty"] == qty
    assert fills[0]["price"] == 998.0

    res = apply_reconcile_from_live(acct, store, _holdings(qty, 998.0),
                                    markets=("KR",))
    assert res["adopted"] == [SYM]
    row = store.get_open_positions()[0]
    meta = json.loads(row["meta"])
    assert row["thesis"] == "갭반등 종가 진입" != RECONCILE_THESIS
    assert row["strategy"] == "macd"
    assert meta["horizon"] == "swing"
    assert meta["entry_basis"] == BASIS_THESIS
    assert not meta.get("provisional_stop")
    assert meta["deferred_fill"]["order_id"] == "L1"
    assert not is_orphan_store_row(row)
    # 손절·목표는 실체결 평단 기준
    assert row["stop_price"] < 998.0 < row["target_price"]
    # 전량 반영 후 working 행은 정리된다
    assert store.get_working_orders() == []
    assert len(_events(store, "deferred_entry_opened")) == 1


def test_startup_sync_also_uses_entry_plan(tmp_path):
    store, acct, client, broker = _setup(tmp_path)
    _run(tmp_path, broker, store)
    qty = float(client.calls[0]["qty"])
    client.status, client.filled, client.avg = "FILLED", qty, 1000.0
    broker.sweep_working_orders()

    apply_sync_from_live(acct, store, _holdings(qty, 1000.0), markets=("KR",))
    row = store.get_open_positions()[0]
    assert row["thesis"] == "갭반등 종가 진입"
    assert json.loads(row["meta"])["horizon"] == "swing"
    assert store.get_working_orders() == []


def test_no_plan_still_falls_back_to_orphan(tmp_path):
    store = Store(tmp_path / "t.db")
    assert adopt_live_position(store, SYM, "KR", 2, 1000.0,
                               source="reconcile_adopted",
                               thesis=RECONCILE_THESIS) == "opened"
    assert is_orphan_store_row(store.get_open_positions()[0])


def test_stale_plan_is_ignored(tmp_path):
    store = Store(tmp_path / "t.db")
    store.upsert_working_order(
        order_id="OLD", symbol=SYM, market="KR", side="BUY", qty=2, price=1000,
        status="FILLED", filled_qty=2, filled_avg=1000,
        placed_at=time.time() - ENTRY_PLAN_MAX_AGE_SEC - 60,
        meta={"entry_plan": _plan_fn(Proposal(
            symbol=SYM, market="KR", side="BUY", conviction=0.7,
            horizon="close_scan", target_weight=0.2, thesis="옛 논거", key_risks=[]))})
    assert adopt_live_position(store, SYM, "KR", 2, 1000.0,
                               source="reconcile_adopted",
                               thesis=RECONCILE_THESIS) == "opened"
    assert store.get_open_positions()[0]["thesis"] == RECONCILE_THESIS


# ── 3) 알림: 즉시 체결분은 중복 알림 안 함 ────────────────────────────────────
def test_deferred_fill_notifies_only_increment(tmp_path):
    store, _acct, client, broker = _setup(tmp_path)
    # 즉시 체결 1주(_finish_live 가 이미 live_order 를 냄 → applied=1)
    store.upsert_working_order(
        order_id="P1", symbol=SYM, market="KR", side="BUY", qty=3, price=1000,
        status="PARTIAL_FILLED", filled_qty=1, filled_avg=1000,
        applied_qty=1, applied_notional=1000)
    client.status, client.filled, client.avg = "FILLED", 3, 1000.0
    broker.sweep_working_orders()
    fills = _events(store, "live_order")
    assert len(fills) == 1 and fills[0]["qty"] == 2.0


def test_no_notification_when_nothing_new_filled(tmp_path):
    store, _acct, client, broker = _setup(tmp_path)
    store.upsert_working_order(
        order_id="P1", symbol=SYM, market="KR", side="BUY", qty=3, price=1000,
        status="PENDING", filled_qty=0)
    client.status, client.filled = "CANCELED", 0
    broker.sweep_working_orders()
    assert _events(store, "live_order") == []


# ── 4) 동시호가 접수분은 60s TTL 로 취소하지 않는다 ────────────────────────────
def _kst(h, m, s=0):
    return datetime(2026, 9, 30, h, m, s, tzinfo=_KST).timestamp()


def test_call_auction_hold_windows():
    assert _call_auction_hold_until("KR", _kst(15, 25)) == _kst(15, 32)
    assert _call_auction_hold_until("KR", _kst(8, 55)) == _kst(9, 2)
    assert _call_auction_hold_until("KR", _kst(15, 30)) is None
    assert _call_auction_hold_until("KR", _kst(14, 0)) is None
    assert _call_auction_hold_until("US", _kst(15, 25)) is None


def test_sweep_keeps_closing_auction_order_until_match(tmp_path, monkeypatch):
    store, _acct, client, broker = _setup(tmp_path)
    store.upsert_working_order(
        order_id="A1", symbol=SYM, market="KR", side="BUY", qty=2, price=1000,
        status="PENDING", filled_qty=0, placed_at=_kst(15, 21))
    monkeypatch.setattr("src.broker.time.time", lambda: _kst(15, 25))
    out = broker.sweep_working_orders()
    assert client.canceled == [] and out["working"] == 1

    # 체결 시각+여유 뒤에도 안 채워졌으면 TTL 정상 적용
    monkeypatch.setattr("src.broker.time.time", lambda: _kst(15, 33))
    broker.sweep_working_orders()
    assert client.canceled == ["A1"]


def test_sweep_still_cancels_regular_session_order_after_ttl(tmp_path, monkeypatch):
    store, _acct, client, broker = _setup(tmp_path)
    store.upsert_working_order(
        order_id="R1", symbol=SYM, market="KR", side="BUY", qty=2, price=1000,
        status="PENDING", filled_qty=0, placed_at=_kst(14, 0))
    monkeypatch.setattr("src.broker.time.time", lambda: _kst(14, 5))
    broker.sweep_working_orders()
    assert client.canceled == ["R1"]


# ── 5) 취소 요청은 JSON 본문(Content-Type)을 싣는다(415 방지) ────────────────────
def test_cancel_order_sends_json_body(tmp_path, monkeypatch):
    from unittest.mock import MagicMock
    from src import toss_client as tc
    from src.config import TossCredentials
    monkeypatch.setattr(tc, "_TOKEN_CACHE", tmp_path / ".token.json")
    client = tc.TossClient(TossCredentials(base_url="https://x", client_id="i",
                                           client_secret="s", account_no="1"))
    client._token, client._token_exp = "tok", time.time() + 3600
    client._token_issued_at = time.time() - 600
    sent = []

    def fake(method, url, json=None, **kw):
        sent.append((method, url, json))
        r = MagicMock()
        r.status_code, r.headers, r.text = 200, {}, "{}"
        r.content = b"{}"
        r.json.return_value = {"result": {"orderId": "O1"}}
        return r

    client.session.request = fake
    client.cancel_order(1, "O1")
    method, url, body = sent[0]
    assert method == "POST" and url.endswith("/api/v1/orders/O1/cancel")
    assert body == {}                    # None 이면 Content-Type 누락 → 415


# ── 6) 매수여력 홀드는 출금이 아니다 ─────────────────────────────────────────
def _acct_sod(tmp_path, cash=2_000_000):
    acct = PaperAccount(cash={"KR": cash}, fee_rate={"KR": 0.0},
                        slippage_bps={"KR": 0.0}, state_path=tmp_path / "a.json")
    acct.ensure_sod_equity("KR")
    return acct


def test_pending_buy_hold_is_not_withdrawal_and_equity_kept(tmp_path):
    store, acct = Store(tmp_path / "t.db"), _acct_sod(tmp_path)
    base = acct.ensure_sod_equity("KR")
    store.upsert_working_order(
        order_id="H1", symbol=SYM, market="KR", side="BUY", qty=2, price=149_300,
        status="PENDING", filled_qty=0)
    data = {"cash": {"KR": 2_000_000 - 298_845}, "holdings_ok": True, "items": []}
    res = apply_reconcile_from_live(acct, store, data, markets=("KR",))
    assert res["external_cash"] == {}
    assert acct.ensure_sod_equity("KR") == base          # SoD 기준 불변
    assert acct.cash_hold["KR"] == 298_600
    # equity 가 주문금액만큼 빠져 보이지 않는다(홀드 수수료 여유 245 만 차이)
    assert abs(acct.equity("KR") - 2_000_000) < 300
    assert abs(acct.start_cash["KR"] - 2_000_000) < 1


def test_unknown_buy_is_not_withdrawal_even_if_equity_hold_skipped(tmp_path):
    """UNKNOWN BUY: equity cash_hold 는 안 잡되, 입출금 판정은 skip (start_cash 보호).

    get_order 상태 누락·표기 흔들림으로 PENDING→UNKNOWN 이 되면, 예전엔 매수여력
    감소를 출금으로 오인해 원금대비 수익률이 부풀었다(10-02 ISC).
    """
    store, acct = Store(tmp_path / "t.db"), _acct_sod(tmp_path)
    base = acct.ensure_sod_equity("KR")
    seed = acct.start_cash["KR"]
    store.upsert_working_order(
        order_id="H1", symbol=SYM, market="KR", side="BUY", qty=2, price=206_000,
        status="UNKNOWN", filled_qty=0)
    data = {"cash": {"KR": 2_000_000 - 412_061}, "holdings_ok": True, "items": []}
    res = apply_reconcile_from_live(acct, store, data, markets=("KR",))
    assert res["external_cash"] == {}
    assert acct.start_cash["KR"] == seed
    assert acct.ensure_sod_equity("KR") == base
    assert acct.cash_hold == {}          # equity 가산은 보수적으로 비움


def test_hold_release_after_cancel_is_not_deposit(tmp_path):
    store, acct = Store(tmp_path / "t.db"), _acct_sod(tmp_path)
    base = acct.ensure_sod_equity("KR")
    store.upsert_working_order(
        order_id="H1", symbol=SYM, market="KR", side="BUY", qty=2, price=149_300,
        status="PENDING", filled_qty=0)
    apply_reconcile_from_live(
        acct, store, {"cash": {"KR": 1_701_155}, "holdings_ok": True, "items": []},
        markets=("KR",))
    store.delete_working_order("H1")                      # sweep 이 취소 확인 후 삭제
    res = apply_reconcile_from_live(
        acct, store, {"cash": {"KR": 2_000_000}, "holdings_ok": True, "items": []},
        markets=("KR",))
    assert res["external_cash"] == {}
    assert acct.ensure_sod_equity("KR") == base
    assert acct.cash_hold == {}
    assert abs(acct.start_cash["KR"] - 2_000_000) < 1


def test_real_withdrawal_still_detected_without_hold(tmp_path):
    store, acct = Store(tmp_path / "t.db"), _acct_sod(tmp_path)
    res = apply_reconcile_from_live(
        acct, store, {"cash": {"KR": 1_500_000}, "holdings_ok": True, "items": []},
        markets=("KR",))
    assert res["external_cash"] == {"KR": -500_000}
    assert acct.start_cash["KR"] == 1_500_000


# ── 7) 밸류 계획: 하드손절 %·적정가 목표 ─────────────────────────────────────
def test_value_plan_uses_fixed_stop_pct_and_fair_target():
    plan = {"strategy": "value", "thesis": "저평가", "stop_pct": 0.15,
            "target_price": 130.0,
            "meta": {"source": "value", "horizon": "position"}}
    strat, thesis, stop, target, meta = plan_position_fields(plan, 100.0)
    assert (strat, thesis, stop, target) == ("value", "저평가", 85.0, 130.0)
    assert meta["source"] == "value" and meta["horizon"] == "position"
