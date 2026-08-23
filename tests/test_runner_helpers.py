"""runner 보조 — 실시간가 패치(데이트레 1초 반응성)."""
import pandas as pd

from src.runner import patch_live_price


def _df():
    return pd.DataFrame({
        "open": [1.0, 2.0], "high": [1.5, 2.5], "low": [0.9, 1.8],
        "close": [1.2, 2.1], "volume": [10, 20],
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
