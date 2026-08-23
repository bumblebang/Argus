"""기술적 지표. 모두 pandas Series 입력 -> Series 출력."""
from __future__ import annotations

import pandas as pd


def sma(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window=window, min_periods=window).mean()


def ema(series: pd.Series, window: int) -> pd.Series:
    return series.ewm(span=window, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Wilder RSI."""
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, pd.NA)
    out = 100 - (100 / (1 + rs))
    # avg_loss 가 0 이면 RSI=100
    out = out.where(avg_loss != 0, 100.0)
    return out


def roc(series: pd.Series, period: int) -> pd.Series:
    """Rate of Change(모멘텀): 현재 / period 전 - 1. 추세 강도/방향."""
    return series / series.shift(period) - 1.0


def bollinger(series: pd.Series, period: int = 20,
              num_std: float = 2.0) -> tuple[pd.Series, pd.Series, pd.Series]:
    """볼린저 밴드 (중심선, 상단, 하단). 변동성 기반 과열/과매도·돌파 판단."""
    mid = sma(series, period)
    sd = series.rolling(window=period, min_periods=period).std(ddof=0)
    return mid, mid + num_std * sd, mid - num_std * sd


def macd(series: pd.Series, fast: int = 12, slow: int = 26,
         signal: int = 9) -> tuple[pd.Series, pd.Series, pd.Series]:
    """MACD (macd선, 시그널선, 히스토그램). 추세/모멘텀 전환."""
    macd_line = ema(series, fast) - ema(series, slow)
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return macd_line, signal_line, macd_line - signal_line


def donchian_high(high: pd.Series, period: int) -> pd.Series:
    """돈치안 상단 채널: 최근 period 최고가(당봉 제외는 호출측에서 shift)."""
    return high.rolling(window=period, min_periods=period).max()


def donchian_low(low: pd.Series, period: int) -> pd.Series:
    """돈치안 하단 채널: 최근 period 최저가."""
    return low.rolling(window=period, min_periods=period).min()


def stochastic(high: pd.Series, low: pd.Series, close: pd.Series, k_period: int = 14,
               d_period: int = 3) -> tuple[pd.Series, pd.Series]:
    """스토캐스틱 (%K, %D). 0~100, 과매수(>80)/과매도(<20)."""
    lowest = low.rolling(window=k_period, min_periods=k_period).min()
    highest = high.rolling(window=k_period, min_periods=k_period).max()
    k = 100 * (close - lowest) / (highest - lowest).replace(0.0, pd.NA)
    return k, k.rolling(window=d_period, min_periods=d_period).mean()


def crossed_up(fast: pd.Series, slow: pd.Series) -> bool:
    """직전봉에서 fast<=slow 였다가 마지막봉에서 fast>slow (골든크로스)."""
    if len(fast) < 2 or fast.iloc[-2:].isna().any() or slow.iloc[-2:].isna().any():
        return False
    return fast.iloc[-2] <= slow.iloc[-2] and fast.iloc[-1] > slow.iloc[-1]


def crossed_down(fast: pd.Series, slow: pd.Series) -> bool:
    """데드크로스."""
    if len(fast) < 2 or fast.iloc[-2:].isna().any() or slow.iloc[-2:].isna().any():
        return False
    return fast.iloc[-2] >= slow.iloc[-2] and fast.iloc[-1] < slow.iloc[-1]
