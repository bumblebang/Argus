import pandas as pd

from src.indicators import sma, rsi, crossed_up, crossed_down


def test_sma():
    s = pd.Series([1, 2, 3, 4, 5], dtype=float)
    assert sma(s, 3).iloc[-1] == 4.0


def test_rsi_bounds():
    s = pd.Series(range(1, 50), dtype=float)  # 단조 증가 -> RSI 100 근처
    val = rsi(s, 14).iloc[-1]
    assert 95 <= val <= 100


def test_crosses():
    fast = pd.Series([1, 1, 1, 3], dtype=float)
    slow = pd.Series([2, 2, 2, 2], dtype=float)
    assert crossed_up(fast, slow)
    assert not crossed_down(fast, slow)

    fast2 = pd.Series([3, 3, 3, 1], dtype=float)
    assert crossed_down(fast2, slow)
