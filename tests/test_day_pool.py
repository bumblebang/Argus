"""데이트레 풀(스윙 유니버스와 분리) — 토스 거래대금 랭킹 병합·폴백."""
from src.day_pool import merge_swing_and_day, refresh_day_pool, load_day_pool


def test_merge_swing_wins_on_overlap():
    swing = {"KR": [{"symbol": "005930", "name": "삼성전자", "layer": "core"}]}
    day = {"KR": [{"symbol": "005930", "name": "삼성전자"},
                  {"symbol": "000660", "name": "하이닉스"}]}
    out = merge_swing_and_day(swing, day)
    syms = [it["symbol"] for it in out["KR"]]
    assert syms == ["005930", "000660"]
    by = {it["symbol"]: it["pool"] for it in out["KR"]}
    assert by["005930"] == "swing" and by["000660"] == "day"


def test_refresh_uses_live_rankings(tmp_path):
    calls = []

    def fetch(rank_type, market, duration, count):
        calls.append(duration)
        assert rank_type == "MARKET_TRADING_AMOUNT"
        return {"rankings": [
            {"rank": 1, "symbol": "005930", "name": "삼성전자",
             "tradingAmount": "1000000000"},
            {"rank": 2, "symbol": "000660", "tradingAmount": "900000000"},
        ]}

    cfg = {"day_pool": {"enabled": True, "markets": ["KR"], "count": 50}}
    out = refresh_day_pool(fetch, cfg, "KR", path=tmp_path / "day_pool.yaml")
    assert calls == ["realtime"]
    assert [it["symbol"] for it in out["KR"]] == ["005930", "000660"]
    assert out["KR"][0]["pool"] == "day"
    assert out["KR"][0]["strategy"] == "volatility_breakout"
    loaded = load_day_pool(tmp_path / "day_pool.yaml")
    assert len(loaded["KR"]) == 2


def test_refresh_falls_back_to_1d_when_live_sparse(tmp_path):
    def fetch(rank_type, market, duration, count):
        if duration == "realtime":
            return {"rankings": []}
        return {"rankings": [
            {"rank": 1, "symbol": "035420", "tradingAmount": "1"},
        ]}

    cfg = {"day_pool": {"enabled": True, "count": 50}}
    out = refresh_day_pool(fetch, cfg, "KR", path=tmp_path / "day_pool.yaml")
    assert out["KR"][0]["symbol"] == "035420"


def test_refresh_keeps_file_on_empty(tmp_path):
    p = tmp_path / "day_pool.yaml"
    p.write_text("KR:\n- {symbol: '005930', name: '삼성전자'}\n", encoding="utf-8")

    def fetch(*a, **k):
        return {"rankings": []}

    assert refresh_day_pool(fetch, {"day_pool": {}}, "KR", path=p) is None
    assert load_day_pool(p)["KR"][0]["symbol"] == "005930"
