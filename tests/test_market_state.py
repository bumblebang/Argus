import numpy as np
import pandas as pd

from src.market_state import MarketState
from src.datasources import (SourceContext, BreadthSource, EdgarSource,
                             DartSource, SectorSource, NewsSource, SentimentSource)
from src.datasources.sentiment import _vix_label


def test_merge_and_roundtrip(tmp_path):
    s = MarketState()
    s.merge({"fx": {"USDKRW": 1380.0}})
    s.merge({"regime": {"KR": {"label": "risk_on"}}})
    s.merge({"fx": {"EURKRW": 1500.0}})  # dict 는 누적 갱신
    assert s.fx == {"USDKRW": 1380.0, "EURKRW": 1500.0}
    s.save(tmp_path / "ms.json")
    loaded = MarketState.load(tmp_path / "ms.json")
    assert loaded.regime["KR"]["label"] == "risk_on"


def _rising(symbol, market):
    close = np.linspace(100, 130, 30)  # 꾸준한 상승 -> MA20 상회
    return pd.DataFrame({"open": close, "high": close * 1.01, "low": close * 0.99,
                         "close": close, "volume": [1000] * 30})


def _falling(symbol, market):
    close = np.linspace(130, 100, 30)  # 하락 -> MA20 하회
    return pd.DataFrame({"open": close, "high": close * 1.01, "low": close * 0.99,
                         "close": close, "volume": [1000] * 30})


def test_breadth_risk_on():
    src = BreadthSource(synthetic_fetch=_rising)
    ctx = SourceContext(symbols_by_market={"KR": ["A", "B", "C"]}, dry=True)
    out = src.fetch(ctx)
    assert out["regime"]["KR"]["label"] == "risk_on"
    assert out["regime"]["KR"]["breadth_above_ma20"] == 1.0


def test_breadth_risk_off():
    src = BreadthSource(synthetic_fetch=_falling)
    ctx = SourceContext(symbols_by_market={"US": ["A", "B"]}, dry=True)
    out = src.fetch(ctx)
    assert out["regime"]["US"]["label"] == "risk_off"


def test_edgar_dry_shape():
    src = EdgarSource(tickers=["AAPL", "MSFT"], user_agent="t test@example.com")
    out = src.fetch(SourceContext(dry=True))
    assert set(out["fundamentals"]) == {"AAPL", "MSFT"}
    assert "net_margin" in out["fundamentals"]["AAPL"]


def test_dart_dry_shape():
    src = DartSource(api_key="x", symbols=["005930", "000660"])
    out = src.fetch(SourceContext(dry=True))
    assert set(out["fundamentals"]) == {"005930", "000660"}


def test_sector_ranks_leaders():
    def up(sym, market):  # 섹터마다 다른 모멘텀
        base = {"A": 1.0, "B": 1.2, "C": 1.5}[sym]
        close = np.linspace(100, 100 * base, 30)
        return pd.DataFrame({"open": close, "high": close, "low": close,
                             "close": close, "volume": [1] * 30})
    src = SectorSource({"US": {"S1": "A", "S2": "B", "S3": "C"}}, lookback=20,
                       synthetic_fetch=up)
    out = src.fetch(SourceContext(dry=True))
    assert out["sectors"]["US"]["leaders"][0] == "S3"  # 가장 강한 모멘텀


def test_vix_labels():
    assert _vix_label(12) == "calm"
    assert _vix_label(18) == "normal"
    assert _vix_label(25) == "elevated"
    assert _vix_label(40) == "fear"


def test_news_and_sentiment_dry():
    assert "news" in NewsSource({"x": "u"}).fetch(SourceContext(dry=True))
    out = SentimentSource().fetch(SourceContext(dry=True))
    assert "vix" in out["sentiment"]


def test_flows_num_parsing():
    from src.datasources.flows import _num
    assert _num("+9,298,204") == 9298204
    assert _num("-5,975,701") == -5975701
    assert _num(None) is None


def test_macro_dry():
    from src.datasources import MacroSource
    out = MacroSource(api_key="x").fetch(SourceContext(dry=True))
    assert "yield_curve_10y_2y" in out["macro"]


def test_finnhub_dry():
    from src.datasources import FinnhubNewsSource
    out = FinnhubNewsSource(api_key="x", symbols=["AAPL"]).fetch(SourceContext(dry=True))
    assert "news" in out


# ── 휴장일 캘린더 ──────────────────────────────────────────────────
def test_holiday_us_july3_closed_kr_open():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from src.market_hours import is_open, is_holiday
    # 2026-07-03(금): US 는 독립기념일 대체휴장, KR 은 정상 개장
    kst_noon = datetime(2026, 7, 3, 12, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    assert is_open("KR", kst_noon) is True
    ny_noon = datetime(2026, 7, 3, 12, 0, tzinfo=ZoneInfo("America/New_York"))
    assert is_open("US", ny_noon) is False
    assert is_holiday("US", ny_noon) is True


def test_holiday_regular_friday_still_open():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from src.market_hours import is_open
    ny = datetime(2026, 7, 10, 12, 0, tzinfo=ZoneInfo("America/New_York"))  # 평범한 금요일
    assert is_open("US", ny) is True


def test_holiday_blocks_session_end_and_after_close():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from src.market_hours import near_session_end, within_after_close
    # 추석(2026-09-24, 목) — 마감 관련 헬퍼도 전부 비활성이어야 함
    kst = datetime(2026, 9, 24, 15, 28, tzinfo=ZoneInfo("Asia/Seoul"))
    assert near_session_end("KR", 5, kst) is False
    kst2 = datetime(2026, 9, 24, 16, 30, tzinfo=ZoneInfo("Asia/Seoul"))
    assert within_after_close("KR", 2, kst2) is False
