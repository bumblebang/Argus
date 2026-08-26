"""실계좌 스냅샷 조회·정규화·캐시(account_snapshot) + 대시보드 자산 패널 렌더 스모크."""
import json
import sys
import time
from pathlib import Path

from src.datasources import account_snapshot as acc

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import dashboard as dash  # noqa: E402


# 실호출로 확인한 응답 구조(모든 숫자 문자열, krw/usd 통화 분리).
_HOLDINGS = {
    "totalPurchaseAmount": {"krw": "267500", "usd": None},
    "marketValue": {"amount": {"krw": "273500", "usd": None},
                    "amountAfterCost": {"krw": "272875", "usd": None}},
    "profitLoss": {"amount": {"krw": "6000"}, "rate": "0.0224", "rateAfterCost": "0.02"},
    "dailyProfitLoss": {"amount": {"krw": "10500"}, "rate": "0.0392"},
    "items": [{"symbol": "005930", "name": "삼성전자", "marketCountry": "KR",
               "quantity": "1", "lastPrice": "273500", "averagePurchasePrice": "267500",
               "marketValue": {"purchaseAmount": "267500", "amount": "273500",
                               "amountAfterCost": "272875"},
               "profitLoss": {"amount": "6000", "rate": "0.0224"},
               "cost": {"commission": "78", "tax": "547"}}],
}


class _FakeClient:
    """get_buying_power/get_holdings mock. buying[market] 반환, holdings 고정."""

    def __init__(self, buying, holdings, bp_fail=(), hold_fail=False):
        self.buying = buying
        self.holdings = holdings
        self.bp_fail = set(bp_fail)
        self.hold_fail = hold_fail
        self.bp_calls = []

    def get_buying_power(self, account_seq, market):
        self.bp_calls.append(market)
        if market in self.bp_fail:
            raise RuntimeError(f"{market} bp 조회 실패")
        return self.buying[market]

    def get_holdings(self, account_seq, symbol=None):
        if self.hold_fail:
            raise RuntimeError("holdings 조회 실패")
        return self.holdings


# ── fetch: 정규화(문자열→float, items 파싱, 통화 분리) ──────────────────
def test_fetch_normalizes_strings_to_float():
    client = _FakeClient({"KR": {"currency": "KRW", "cashBuyingPower": "732463"}}, _HOLDINGS)
    snap = acc.fetch_account_snapshot(client, 1, markets=("KR",))
    assert snap["cash"] == {"KR": 732463.0}
    assert snap["total_purchase"] == {"KR": 267500.0}
    assert snap["market_value"] == {"KR": 273500.0}
    assert snap["profit"] == {"KR": 6000.0}
    assert snap["profit_rate"] == {"KR": 0.0224}
    assert snap["daily_profit"] == {"KR": 10500.0}
    assert snap["daily_profit_rate"] == {"KR": 0.0392}
    assert isinstance(snap["ts"], float)
    assert client.bp_calls == ["KR"]


def test_fetch_item_fields_parsed():
    client = _FakeClient({"KR": {"cashBuyingPower": "732463"}}, _HOLDINGS)
    snap = acc.fetch_account_snapshot(client, 1)
    assert len(snap["items"]) == 1
    it = snap["items"][0]
    assert it == {"symbol": "005930", "name": "삼성전자", "market": "KR", "qty": 1.0,
                  "avg": 267500.0, "last": 273500.0, "value": 273500.0,
                  "pnl": 6000.0, "pnl_rate": 0.0224}


def test_fetch_us_item_kept_and_currency_split():
    """US 종목이 있으면 그대로 담고, 종합은 통화별로 KR/US 분리."""
    holdings = {
        "totalPurchaseAmount": {"krw": "100000", "usd": "500"},
        "marketValue": {"amount": {"krw": "110000", "usd": "550"}},
        "profitLoss": {"amount": {"krw": "10000", "usd": "50"}, "rate": "0.1"},
        "dailyProfitLoss": {"amount": {"krw": "1000"}, "rate": "0.01"},
        "items": [{"symbol": "AAPL", "name": "Apple", "marketCountry": "US",
                   "quantity": "2", "lastPrice": "275", "averagePurchasePrice": "250",
                   "marketValue": {"amount": "550"},
                   "profitLoss": {"amount": "50", "rate": "0.1"}}],
    }
    client = _FakeClient({"KR": {"cashBuyingPower": "0"},
                          "US": {"cashBuyingPower": "0"}}, holdings)
    snap = acc.fetch_account_snapshot(client, 1, markets=("KR", "US"))
    assert snap["total_purchase"] == {"KR": 100000.0, "US": 500.0}
    assert snap["market_value"] == {"KR": 110000.0, "US": 550.0}
    assert snap["profit"] == {"KR": 10000.0, "US": 50.0}
    # rate 는 스칼라 → 손익이 있는 시장 키에 매단다(KR·US 둘 다)
    assert snap["profit_rate"] == {"KR": 0.1, "US": 0.1}
    assert snap["items"][0]["market"] == "US" and snap["items"][0]["qty"] == 2.0


# ── 안전성: 파싱 실패·빈 items·조회 실패 market ───────────────────────
def test_fetch_bad_numbers_are_safe():
    holdings = {
        "totalPurchaseAmount": {"krw": "not-a-number", "usd": None},
        "marketValue": {"amount": {"krw": None}},
        "profitLoss": {"amount": {"krw": "abc"}, "rate": None},
        "dailyProfitLoss": {},
        "items": [{"symbol": "X", "marketCountry": "KR", "quantity": "bad",
                   "lastPrice": None, "averagePurchasePrice": "xx",
                   "marketValue": {"amount": "oops"}, "profitLoss": {}}],
    }
    client = _FakeClient({"KR": {"cashBuyingPower": "bad"}}, holdings)
    snap = acc.fetch_account_snapshot(client, 1)
    # 파싱 실패 종합값은 키 자체가 빠지고, 캐시 저장/렌더가 깨지지 않는다.
    assert snap["total_purchase"] == {} and snap["market_value"] == {}
    assert snap["profit"] == {} and snap["profit_rate"] == {}
    assert snap["cash"] == {"KR": 0.0}   # 파싱 실패 현금은 0.0 안전값
    it = snap["items"][0]
    assert it["qty"] == 0.0 and it["avg"] is None and it["value"] is None


def test_fetch_empty_holdings_and_no_items():
    client = _FakeClient({"KR": {"cashBuyingPower": "500"}}, {})
    snap = acc.fetch_account_snapshot(client, 1)
    assert snap["cash"] == {"KR": 500.0}
    assert snap["items"] == []
    assert snap["market_value"] == {} and snap["profit"] == {}


def test_fetch_failed_market_skipped_but_others_continue():
    client = _FakeClient({"KR": {"cashBuyingPower": "500"},
                          "US": {"cashBuyingPower": "10"}}, _HOLDINGS, bp_fail=("KR",))
    snap = acc.fetch_account_snapshot(client, 1, markets=("KR", "US"))
    assert "KR" not in snap["cash"]      # KR 조회 실패 → 스킵
    assert snap["cash"] == {"US": 10.0}  # US 는 계속
    # holdings 는 정상 → 종합/보유는 그대로
    assert snap["market_value"] == {"KR": 273500.0}


def test_fetch_holdings_failure_yields_empty_holdings():
    client = _FakeClient({"KR": {"cashBuyingPower": "500"}}, _HOLDINGS, hold_fail=True)
    snap = acc.fetch_account_snapshot(client, 1)
    assert snap["cash"] == {"KR": 500.0}   # 현금은 살아있고
    assert snap["items"] == [] and snap["market_value"] == {}  # 보유는 빈 채로 진행


# ── save/load 라운드트립 ──────────────────────────────────────────────
def test_save_load_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(acc, "CACHE_PATH", tmp_path / "account_snapshot.json")
    client = _FakeClient({"KR": {"cashBuyingPower": "732463"}}, _HOLDINGS)
    snap = acc.fetch_account_snapshot(client, 1)
    acc.save_snapshot(snap)
    loaded = acc.load_snapshot()
    assert loaded == snap


def test_load_missing_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(acc, "CACHE_PATH", tmp_path / "nope.json")
    assert acc.load_snapshot() is None


# ── 대시보드 자산 패널 렌더 스모크(예외 없이 렌더) ─────────────────────
def _base_d(snap, live_trades=None, live_mode=True):
    return {"now": time.time(), "snapshot": snap, "live_mode": live_mode,
            "live_trades": live_trades or [], "names": {"005930": "삼성전자"}}


def test_asset_html_renders_snapshot():
    client = _FakeClient({"KR": {"cashBuyingPower": "732463"}}, _HOLDINGS)
    snap = acc.fetch_account_snapshot(client, 1)
    html = dash._asset_html(_base_d(snap))
    assert "실계좌 자산 관제" in html
    assert "LIVE" in html                    # live_mode=True 배지
    assert "삼성전자" in html                 # 보유 종목
    # 총자산 = 현금 732,463 + 평가 273,500 = 1,005,963
    assert "1,005,963" in html
    assert "+2.24%" in html                  # 평가손익률
    assert "10,500" in html                  # 일손익 금액


def test_asset_html_empty_snapshot_graceful():
    html = dash._asset_html(_base_d(None, live_mode=False))
    assert "스냅샷 대기중" in html
    assert "PAPER" in html


def test_asset_html_live_trades_render():
    client = _FakeClient({"KR": {"cashBuyingPower": "732463"}}, _HOLDINGS)
    snap = acc.fetch_account_snapshot(client, 1)
    trades = [
        {"ts": time.time(), "kind": "live_order", "symbol": "005930",
         "payload": json.dumps({
             "side": "BUY", "qty": 1, "price": 273500, "order_id": "OID123",
             "reason": "[entry:zone] 존 진입",
         })},
        {"ts": time.time(), "kind": "live_order", "symbol": "005930",
         "payload": json.dumps({
             "side": "SELL", "qty": 1, "price": 252000, "order_id": "OID999",
             "reason": "[exit] stop_hit", "exit_reason": "stop_hit",
         })},
        {"ts": time.time(), "kind": "buy_blocked", "symbol": "900110",
         "payload": '{"reason":"관리종목"}'},
        {"ts": time.time(), "kind": "live_order_error", "symbol": "005930",
         "payload": '{"side":"BUY","error":"응답에 orderId 없음"}'},
    ]
    html = dash._asset_html(_base_d(snap, live_trades=trades))
    assert "매수" in html and "273,500" in html
    assert "매도" in html and "손절" in html and "252,000" in html
    assert ">수량<" in html and ">체결가<" in html
    assert "OID123" not in html and "OID999" not in html
    assert "매수차단" in html and "관리종목" in html
    assert "매수실패" in html
    # 내용 칸에 수량·가격 문구가 섞이지 않음
    assert "x1 @" not in html


def test_summarize_trade_note_first_sentence():
    long = (
        "이번 각성의 트리거 종목이고 thesis가 가격·시간 양쪽으로 무효화됐다. "
        "①가격: 현재 251,250원이 무효화가 254,125원을 하회했다. "
        "②시간: 보유 40일로 시한을 넘겼다."
    )
    s = dash._summarize_trade_note(long)
    assert "무효화됐다" in s
    assert "①" not in s
    assert len(s) < len(long)


def test_asset_html_live_trades_timestop_enrichment():
    """구 live_order 에 reason 없어도 closed_pos.exit_reason=time_stop 을 표시."""
    client = _FakeClient({"KR": {"cashBuyingPower": "732463"}}, _HOLDINGS)
    snap = acc.fetch_account_snapshot(client, 1)
    ts = time.time()
    trades = [
        {"ts": ts, "kind": "live_order", "symbol": "005930",
         "payload": json.dumps({
             "side": "SELL", "qty": 1, "price": 252000, "order_id": "LONGID",
         })},
    ]
    d = _base_d(snap, live_trades=trades)
    d["closed_pos"] = [{
        "symbol": "005930", "exit_reason": "time_stop",
        "closed_at": ts + 0.5, "pnl": -15500,
    }]
    long_thesis = (
        "이번 각성의 트리거 종목이고 thesis가 가격·시간 양쪽으로 무효화됐다. "
        "①가격: 현재 251,250원이 무효화가 254,125원을 하회(평단 267,500). "
        "②시간: 보유 40일로 시한 20일을 넘겼다."
    )
    d["trade_theses"] = [{
        "ts": ts - 3600, "symbol": "005930", "action": "SELL",
        "thesis": long_thesis,
    }]
    html = dash._asset_html(d)
    assert "매도" in html
    assert "시간손절" in html
    assert "무효화됐다." in html
    assert "class=tr-tip" in html
    assert "class=tr-brief" in html
    brief = html.split("class=tr-brief>", 1)[1].split("</span>", 1)[0]
    assert "①" not in brief
    assert brief.endswith("무효화됐다.")
    tip = html.split("class=tr-tip>", 1)[1].split("</span>", 1)[0]
    assert "①" in tip  # 전문은 툴팁
    assert "LONGID" not in html
    assert "x1 @" not in html


def test_asset_html_no_holdings():
    snap = {"ts": time.time(), "cash": {"KR": 500.0}, "total_purchase": {},
            "market_value": {}, "profit": {}, "profit_rate": {}, "daily_profit": {},
            "daily_profit_rate": {}, "items": []}
    html = dash._asset_html(_base_d(snap))
    assert "보유 없음" in html
    assert "오늘 라이브 거래 없음" in html
