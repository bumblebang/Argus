"""runner 보조 — 실시간가 패치(데이트레 1초 반응성)."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from src.market_hours import trading_date
from src.runner import last_bar_trading_date, patch_live_price

_KST = ZoneInfo("Asia/Seoul")


def _df():
    return pd.DataFrame({
        "open": [1.0, 2.0], "high": [1.5, 2.5], "low": [0.9, 1.8],
        "close": [1.2, 2.1], "volume": [10, 20],
    })


def _daily_df(day_iso: str, *, close: float = 100.0):
    ts = pd.Timestamp(day_iso, tz=_KST)
    return pd.DataFrame({
        "time": [ts],
        "open": [close], "high": [close * 1.01], "low": [close * 0.99],
        "close": [close], "volume": [1000],
    })


def test_patch_replaces_last_close():
    out = patch_live_price(_df(), 3.0)
    assert out["close"].iloc[-1] == 3.0
    assert out["high"].iloc[-1] == 3.0          # 고가가 가격 포함하도록 확장
    assert out["close"].iloc[0] == 1.2          # 이전 봉은 불변


def test_patch_extends_low_downward():
    out = patch_live_price(_df(), 1.0)          # 저가(1.8)보다 낮은 가격
    assert out["close"].iloc[-1] == 1.0
    assert out["low"].iloc[-1] == 1.0


def test_patch_none_or_empty_noop():
    assert patch_live_price(_df(), None)["close"].iloc[-1] == 2.1
    empty = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    assert len(patch_live_price(empty, 5.0)) == 0


def test_patch_skips_stale_daily_bar():
    """어제 봉 + 라이브가 → 어제 종가 유지(혼합봉 방지)."""
    yesterday = (datetime.now(_KST).date().toordinal() - 1)
    yday = datetime.fromordinal(yesterday).date().isoformat()
    df = _daily_df(yday, close=95.0)
    out = patch_live_price(df, 88.0, market="KR")
    assert out["close"].iloc[-1] == 95.0
    assert last_bar_trading_date(df, "KR") == yday


def test_patch_today_daily_bar():
    today = trading_date("KR")
    df = _daily_df(today, close=100.0)
    out = patch_live_price(df, 92.0, market="KR")
    assert out["close"].iloc[-1] == 92.0
    assert out["low"].iloc[-1] <= 92.0


def test_patch_append_if_new_day():
    yesterday = (datetime.now(_KST).date().toordinal() - 1)
    yday = datetime.fromordinal(yesterday).date().isoformat()
    df = _daily_df(yday, close=95.0)
    out = patch_live_price(df, 88.0, market="KR", append_if_new_day=True)
    assert len(out) == 2
    assert out["close"].iloc[-1] == 88.0
    assert last_bar_trading_date(out, "KR") == trading_date("KR")
