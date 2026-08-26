"""라이브 배선 + 주문 응답 방어 + 체결 대사 테스트.

핵심 불변식:
  - 원장 fill 은 **실체결 대사(get_order.execution)**로만 기록한다 — 거부/미체결/펜딩은
    원장에 남지 않고, 부분체결은 체결분만, 실체결가·실수수료로 기록한다.
  - 라이브 주문은 **지정가(마켓터블 리밋)**로 나간다(호가북 기반). SELL 은 실 매도가능
    수량으로 클램프한다(오버셀·고아 방지).
  - live_markets 밖·orderId 없음·킬스위치는 실주문/체결이 남지 않는다.
build_paper_core 는 live_client 를 명시 주입한 프로세스만 라이브로 켠다(심층 방어).
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from src.config import load_config
from src.broker import Broker
from src.paper_account import PaperAccount
from src.risk_gate import RiskGate, Order
from src.engine.store import Store
from src.agents.pipeline import build_paper_core


def _filled(qty, avg, commission="0", tax="0", status="FILLED"):
    """get_order 응답(Order) 스텁 — execution 블록 포함."""
    return {"status": status,
            "execution": {"filledQuantity": str(qty),
                          "averageFilledPrice": (None if avg is None else str(avg)),
                          "commission": commission, "tax": tax}}


class _MockClient:
    """place_order/get_order/orderbook/get_sellable 호출 기록 + 지정 응답/예외 반환.

    orderbook/order_detail/sellable=None 이면 그 조회는 미제공(브로커가 폴백/원장수량 사용).
    """
    def __init__(self, resp=None, exc=None, order_detail=None, orderbook=None,
                 sellable=None):
        self.resp = resp
        self.exc = exc
        self.order_detail = order_detail    # get_order 반환(Order dict) 또는 None
        self._orderbook = orderbook         # {"asks":[...], "bids":[...]} 또는 None
        self.sellable = sellable            # {"sellableQuantity": "..."} 또는 None
        self.calls = []
        self.get_order_calls = []

    def place_order(self, **kw):
        self.calls.append(kw)
        if self.exc is not None:
            raise self.exc
        return self.resp

    def orderbook(self, symbol):
        return self._orderbook              # None → 브로커가 견적가 폴백

    def get_order(self, account_seq, order_id):
        self.get_order_calls.append(order_id)
        return self.order_detail            # None → 미체결(UNKNOWN) 취급

    def get_sellable(self, account_seq, symbol):
        return self.sellable                # None → 원장 수량으로 진행


def _gate(tmp_path, capital=None, notional=None):
    return RiskGate({"capital": capital or {"KR": 1_000_000},
                     "max_position_pct": 0.5, "max_positions": 5,
                     "daily_loss_limit_pct": 0.05,
                     "max_order_notional": notional or {"KR": 500_000},
                     "kill_switch_file": str(tmp_path / "HALT")})


def _live_broker(tmp_path, client, store=None, live_markets=None, cash=None,
                 capital=None, notional=None, positions=None):
    acct = PaperAccount(cash=cash or {"KR": 1_000_000, "US": 0},
                        state_path=tmp_path / "pa.json")
    for sym, (qty, avg, market) in (positions or {}).items():   # 보유 세팅(SELL 테스트용)
        acct.apply_fill(sym, market, "BUY", qty, avg, 0.0, "seed")
    gate = _gate(tmp_path, capital=capital, notional=notional)
    # 테스트는 폴링 대기 없이(sleep 0) 빠르게.
    return Broker(account=acct, gate=gate, client=client, mode="live",
                  account_seq=1, live_markets=(live_markets or ["KR"]), store=store,
                  reconcile_poll_attempts=3, reconcile_poll_sec=0.0)


# ── 1) 성공: 지정가 발사 + 실체결 대사로 원장 기록 + live_order 이벤트 ──────
def test_live_success_records_real_fill(tmp_path):
    store = Store(tmp_path / "t.db")
    # 견적 70000 이지만 실체결 70050(평균), 수수료 10.5 — 원장은 실체결가로 기록돼야 한다.
    client = _MockClient(resp={"orderId": "ORD123", "clientOrderId": None},
                         orderbook={"asks": [{"price": "70000", "volume": "10"}],
                                    "bids": [{"price": "69900", "volume": "10"}]},
                         order_detail=_filled(1, 70050, commission="10.5"))
    b = _live_broker(tmp_path, client, store=store)
    ok = b.execute(Order("005930", "KR", "BUY", 1, 70000.0), "test")
    assert ok
    assert len(client.calls) == 1
    assert client.calls[0]["order_type"] == "LIMIT"        # 시장가 아님
    assert client.calls[0]["price"] == 70000.0             # 마켓터블 리밋가(최우선 매도호가)
    pos = b.account.position("005930")
    assert pos.qty == 1 and pos.avg_price == 70050.0       # 실체결가로 기록(견적가 아님)
    evs = store.recent_events("live_order", 0)
    assert len(evs) == 1
    p = json.loads(evs[0]["payload"])
    assert p["order_id"] == "ORD123" and p["symbol"] == "005930" and p["side"] == "BUY"
    assert p["price"] == 70050.0 and p["status"] == "FILLED" and p["fee"] == 10.5
    assert p.get("reason") == "test"


# ── 1b) 부분체결: 체결분만 원장에 기록 ───────────────────────────────────
def test_live_partial_fill_records_partial(tmp_path):
    store = Store(tmp_path / "t.db")
    client = _MockClient(resp={"orderId": "P1"},
                         order_detail=_filled(2, 70000, status="PARTIAL_FILLED"))
    b = _live_broker(tmp_path, client, store=store)
    ok = b.execute(Order("005930", "KR", "BUY", 5, 70000.0), "test")   # 5주 주문, 2주 체결
    assert ok
    assert b.account.position("005930").qty == 2                        # 체결분만
    p = json.loads(store.recent_events("live_order", 0)[0]["payload"])
    assert p["qty"] == 2 and p["status"] == "PARTIAL_FILLED"


# ── 1c) 펜딩(미체결): 원장 무변 + live_order_pending ─────────────────────
def test_live_pending_no_fill(tmp_path):
    store = Store(tmp_path / "t.db")
    client = _MockClient(resp={"orderId": "Q1"},
                         order_detail=_filled(0, None, status="PENDING"))
    b = _live_broker(tmp_path, client, store=store)
    ok = b.execute(Order("005930", "KR", "BUY", 1, 70000.0), "test")
    assert not ok
    assert b.account.position("005930").qty == 0                        # 원장 무변
    assert b.account.journal == []
    assert len(store.recent_events("live_order_pending", 0)) == 1
    assert store.recent_events("live_order", 0) == []


# ── 1d) 마켓터블 리밋: 얕은 호가를 슬리피지 상한 안에서 훑어 리밋가 산정 ────
def test_live_marketable_limit_walks_book_within_cap(tmp_path):
    # asks: 70000(3), 70500(10). qty 5 → 첫 레벨로 부족하나 70500 은 상한(70000*1.01=70700)
    # 안이라 70500 을 리밋가로. (커버 못한 잔량은 미체결로 남아도 됨)
    client = _MockClient(resp={"orderId": "X"},
                         orderbook={"asks": [{"price": "70000", "volume": "3"},
                                             {"price": "70500", "volume": "10"}],
                                    "bids": []},
                         order_detail=_filled(5, 70300))
    b = _live_broker(tmp_path, client)
    b.execute(Order("005930", "KR", "BUY", 5, 70000.0), "test")
    assert client.calls[0]["price"] == 70500.0


# ── 1e) SELL: 실 매도가능 수량으로 클램프 ────────────────────────────────
def test_live_sell_clamped_to_sellable(tmp_path):
    store = Store(tmp_path / "t.db")
    client = _MockClient(resp={"orderId": "S1"},
                         orderbook={"asks": [], "bids": [{"price": "70000", "volume": "100"}]},
                         sellable={"sellableQuantity": "3"},
                         order_detail=_filled(3, 70000))
    b = _live_broker(tmp_path, client, store=store,
                     positions={"005930": (10, 60000.0, "KR")})   # 원장엔 10주
    ok = b.execute(Order("005930", "KR", "SELL", 10, 70000.0), "test")  # 10주 매도 시도
    assert ok
    assert client.calls[0]["qty"] == 3                             # 실 매도가능 3주로 클램프
    assert client.calls[0]["side"] == "SELL"


# ── 1f) SELL: 매도가능 0 → 미발사 ────────────────────────────────────────
def test_live_sell_sellable_zero_skips(tmp_path):
    store = Store(tmp_path / "t.db")
    client = _MockClient(resp={"orderId": "S0"},
                         sellable={"sellableQuantity": "0"})
    b = _live_broker(tmp_path, client, store=store,
                     positions={"005930": (10, 60000.0, "KR")})
    ok = b.execute(Order("005930", "KR", "SELL", 10, 70000.0), "test")
    assert not ok
    assert len(client.calls) == 0                                  # 실주문 미발사
    assert len(store.recent_events("sell_skipped", 0)) == 1


# ── 2) 예외: return False, 원장 무변화, live_order_error ──────────────
def test_live_exception_no_fill_error_event(tmp_path):
    store = Store(tmp_path / "t.db")
    client = _MockClient(exc=RuntimeError("HTTP 400 rejected"))
    b = _live_broker(tmp_path, client, store=store)
    ok = b.execute(Order("005930", "KR", "BUY", 1, 70000.0), "test")
    assert not ok
    assert len(client.calls) == 1                       # 전송은 시도됨
    assert b.account.position("005930").qty == 0        # 체결 안 남음
    assert b.account.journal == []                       # 원장 무변화
    assert len(store.recent_events("live_order_error", 0)) == 1
    assert store.recent_events("live_order", 0) == []


# ── 3) 응답에 주문 식별자 없음 → 실패 처리(원장 무변화) ────────────────
def test_live_missing_order_id_fails(tmp_path):
    store = Store(tmp_path / "t.db")
    client = _MockClient(resp={"clientOrderId": "x"})   # orderId 없음
    b = _live_broker(tmp_path, client, store=store)
    ok = b.execute(Order("005930", "KR", "BUY", 1, 70000.0), "test")
    assert not ok
    assert b.account.position("005930").qty == 0
    assert b.account.journal == []
    assert len(store.recent_events("live_order_error", 0)) == 1


# ── 4) order.market 이 live_markets 밖(US) → 미발사 + False ───────────
def test_live_market_outside_live_markets_blocked(tmp_path):
    # US 를 자금·게이트로 승인 가능하게 열되, live_markets 은 KR 만 → live_markets 가 차단자.
    client = _MockClient(resp={"orderId": "X"})
    b = _live_broker(tmp_path, client,
                     cash={"KR": 1_000_000, "US": 1_000_000},
                     capital={"KR": 1_000_000, "US": 1_000_000},
                     notional={"KR": 500_000, "US": 500_000},
                     live_markets=["KR"])
    ok = b.execute(Order("AAPL", "US", "BUY", 1, 100.0), "test")
    assert not ok
    assert len(client.calls) == 0                       # 실주문 미발사
    assert b.account.position("AAPL").qty == 0          # 원장에도 안 남음


# ── 5) build_paper_core: live_client 미주입이면 config live 여도 paper ──
def test_build_paper_core_no_client_stays_paper(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)                          # data/*.json 격리
    cfg = load_config()
    cfg.raw["broker"] = {"mode": "live", "account_seq": 1, "live_markets": ["KR"]}
    cfg.dry_run = False
    broker, _ = build_paper_core(cfg)                    # live_client 미주입
    assert broker.mode == "paper"
    assert broker.client is None


# ── 6) build_paper_core: 주입 + config live + dry false → live / dry true → paper ──
def test_build_paper_core_live_when_injected_and_not_dry(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    cfg = load_config()
    cfg.raw["broker"] = {"mode": "live", "account_seq": 7, "live_markets": ["KR"]}
    cfg.dry_run = False
    client = _MockClient(resp={"orderId": "X"})
    broker, _ = build_paper_core(cfg, live_client=client, account_seq=7)
    assert broker.mode == "live"
    assert broker.account_seq == 7
    assert broker.client is client
    assert broker.live_markets == ["KR"]


def test_build_paper_core_dry_forces_paper_even_if_injected(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    cfg = load_config()
    cfg.raw["broker"] = {"mode": "live", "account_seq": 1, "live_markets": ["KR"]}
    cfg.dry_run = True                                   # dry → 무조건 페이퍼
    client = _MockClient(resp={"orderId": "X"})
    broker, _ = build_paper_core(cfg, live_client=client)
    assert broker.mode == "paper"
    assert broker.client is None


# ── 7) 킬스위치: live 모드에서도 HALT → 게이트 거부로 미발사 ────────────
def test_live_kill_switch_blocks_order(tmp_path):
    halt = tmp_path / "HALT"
    halt.write_text("halt", encoding="utf-8")
    acct = PaperAccount(cash={"KR": 1_000_000}, state_path=tmp_path / "pa.json")
    gate = RiskGate({"capital": {"KR": 1_000_000}, "max_position_pct": 0.5,
                     "max_positions": 5, "max_order_notional": {"KR": 500_000},
                     "kill_switch_file": str(halt)})
    client = _MockClient(resp={"orderId": "X"})
    b = Broker(account=acct, gate=gate, client=client, mode="live",
               account_seq=1, live_markets=["KR"])
    ok = b.execute(Order("005930", "KR", "BUY", 1, 70000.0), "test")
    assert not ok
    assert len(client.calls) == 0                       # 게이트가 먼저 막아 미발사
