"""datasources.history — Yahoo 과거 캔들 fetch/파싱/캐시 (네트워크는 mock)."""
import src.datasources.history as H
from src.datasources.history import to_yahoo, fetch_history, COLUMNS


def test_to_yahoo_mapping():
    assert to_yahoo("005930", "KR") == "005930.KS"   # KR 6자리 -> .KS
    assert to_yahoo("AAPL", "US") == "AAPL"           # US 티커 그대로
    assert to_yahoo("^KS11", "KR") == "^KS11"         # 비숫자 -> 그대로


def test_to_yahoo_kosdaq_from_stock_info():
    from src.datasources.history import _kr_yahoo_suffix
    cache = {"035720": {"info": {"market": "KOSDAQ", "status": "ACTIVE"}}}
    assert to_yahoo("035720", "KR", info_cache=cache) == "035720.KQ"
    assert _kr_yahoo_suffix("035720", cache) == ".KQ"
    assert _kr_yahoo_suffix("005930", cache) == ".KS"


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p


def _payload(ts, o, h, lo, c, v):
    return {"chart": {"result": [{"timestamp": ts, "indicators": {"quote": [
        {"open": o, "high": h, "low": lo, "close": c, "volume": v}]}}]}}


def test_fetch_history_parses_and_caches(tmp_path, monkeypatch):
    payload = _payload([1700000000, 1700086400, 1700173200],
                       [100, 101, 102], [103, 104, 105], [99, 100, 101],
                       [102, 103, 104], [10, 11, 12])
    monkeypatch.setattr(H.requests, "get", lambda *a, **k: _Resp(payload))
    monkeypatch.setattr(H, "CACHE", tmp_path)
    df = fetch_history("005930", "1d", "1mo")
    assert list(df.columns) == COLUMNS
    assert len(df) == 3 and df["close"].iloc[-1] == 104
    assert (tmp_path / "005930.KS_1d_1mo.csv").exists()        # 캐시 생성

    # 캐시 적중: requests 를 막아도(예외) 캐시에서 읽어야 함
    monkeypatch.setattr(H.requests, "get", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    df2 = fetch_history("005930", "1d", "1mo")
    assert len(df2) == 3


def test_fetch_history_drops_incomplete_bars(tmp_path, monkeypatch):
    payload = _payload([1, 2, 3], [100, None, 102], [103, 104, 105],
                       [99, 100, 101], [102, 103, 104], [10, 11, 12])
    monkeypatch.setattr(H.requests, "get", lambda *a, **k: _Resp(payload))
    monkeypatch.setattr(H, "CACHE", tmp_path)
    df = fetch_history("AAPL", "1d", "1mo", market="US", use_cache=False)
    assert len(df) == 2                                        # OHLC 결측 봉 제거


def test_fetch_history_empty_result(tmp_path, monkeypatch):
    monkeypatch.setattr(H.requests, "get", lambda *a, **k: _Resp({"chart": {"result": None}}))
    monkeypatch.setattr(H, "CACHE", tmp_path)
    df = fetch_history("AAPL", "1d", "1mo", market="US", use_cache=False)
    assert df.empty


def test_interval_alias_in_cache_name(tmp_path, monkeypatch):
    payload = _payload([1, 2], [1, 2], [2, 3], [0.5, 1], [1.5, 2.5], [5, 6])
    monkeypatch.setattr(H.requests, "get", lambda *a, **k: _Resp(payload))
    monkeypatch.setattr(H, "CACHE", tmp_path)
    fetch_history("AAPL", "1h", "5d", market="US")            # 1h -> 60m 별칭
    assert (tmp_path / "AAPL_60m_5d.csv").exists()
