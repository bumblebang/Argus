"""engine.gateway — 배치 폴링/청크 분할/snapshot 기록 검증 (fake client)."""
from src.engine.gateway import TossGateway
from src.engine.store import Store


class FakeClient:
    """get_prices 만 흉내. 호출 인자(청크)를 기록한다."""
    def __init__(self):
        self.calls = []

    def get_prices(self, symbols):
        self.calls.append(list(symbols))
        return [{"symbol": s, "lastPrice": "100.5"} for s in symbols]


def test_poll_prices_normalizes_and_returns():
    g = TossGateway(FakeClient())
    out = g.poll_prices(["005930", "000660"])
    assert len(out) == 2
    assert out[0]["symbol"] == "005930"
    assert out[0]["price"] == 100.5          # 문자열 -> float 정규화
    assert "payload" in out[0]


def test_poll_prices_chunks_over_200():
    client = FakeClient()
    g = TossGateway(client)
    syms = [f"S{i}" for i in range(450)]
    out = g.poll_prices(syms)
    assert len(out) == 450
    assert len(client.calls) == 3            # 200 + 200 + 50
    assert len(client.calls[0]) == 200
    assert len(client.calls[2]) == 50


def test_poll_prices_records_snapshots(tmp_path):
    store = Store(tmp_path / "t.db")
    g = TossGateway(FakeClient(), store=store)
    g.poll_prices(["A", "B", "C"], record=True)
    cur = store.conn.execute("SELECT COUNT(*) AS n FROM snapshots")
    assert cur.fetchone()["n"] == 3


def test_poll_prices_skips_empty_symbols():
    g = TossGateway(FakeClient())
    out = g.poll_prices(["A", "", None])
    assert len(out) == 1


class _CandleClient:
    def __init__(self):
        self.calls = 0

    def get_candles(self, symbol, interval="1m", count=200):
        self.calls += 1
        return [{"close": self.calls}]


def test_candle_ttl_cache(monkeypatch):
    import src.engine.gateway as gwmod
    clock = [1000.0]
    monkeypatch.setattr(gwmod.time, "time", lambda: clock[0])
    client = _CandleClient()
    g = TossGateway(client, candle_ttl_sec=10)
    a = g.candles("005930", "1m", 5)
    assert client.calls == 1
    b = g.candles("005930", "1m", 5)
    assert client.calls == 1 and b == a          # TTL 내 -> 캐시 적중(재호출 안 함)
    clock[0] = 1011.0
    g.candles("005930", "1m", 5)
    assert client.calls == 2                      # TTL 만료 -> 재호출
    g.candles("005930", "1m", 9)
    assert client.calls == 3                      # 다른 count -> 다른 키


def test_candle_no_cache_when_ttl_zero():
    client = _CandleClient()
    g = TossGateway(client, candle_ttl_sec=0)     # 기본=캐시 안 함
    g.candles("005930", "1m", 5)
    g.candles("005930", "1m", 5)
    assert client.calls == 2                      # 매번 실호출


def test_cancel_order_delegates_with_lock():
    class _CancelClient:
        def __init__(self):
            self.calls = []

        def cancel_order(self, account_seq, order_id):
            self.calls.append((account_seq, order_id))
            return {"orderId": order_id, "status": "CANCELED"}

    client = _CancelClient()
    g = TossGateway(client)
    out = g.cancel_order(1, "ORD-99")
    assert out["status"] == "CANCELED"
    assert client.calls == [(1, "ORD-99")]
