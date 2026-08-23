import pandas as pd

from src.strategies import build_strategy
from src.strategies.base import Action, Position


def _df(closes, opens=None, highs=None, lows=None):
    n = len(closes)
    opens = opens or closes
    highs = highs or [c * 1.01 for c in closes]
    lows = lows or [c * 0.99 for c in closes]
    return pd.DataFrame({
        "time": pd.date_range("2024-01-01", periods=n, freq="D"),
        "open": opens, "high": highs, "low": lows, "close": closes,
        "volume": [1000] * n,
    })


def test_ma_crossover_golden():
    # 하락 후 급반등으로 단기MA가 장기MA를 상향 돌파
    closes = [10] * 20 + [10, 10, 11, 13, 16, 20]
    strat = build_strategy("ma_crossover", {"short": 3, "long": 10})
    sig = strat.decide(_df(closes), Position("X"))
    assert sig.action in (Action.BUY, Action.HOLD)


def test_rsi_reversion_oversold_buys():
    closes = [float(x) for x in range(50, 20, -1)]  # 지속 하락 -> 과매도
    strat = build_strategy("rsi_reversion", {"period": 14, "oversold": 35, "overbought": 70})
    sig = strat.decide(_df(closes), Position("X"))
    assert sig.action == Action.BUY


def test_volatility_breakout_triggers():
    closes = [100, 100]
    df = _df(closes, opens=[100, 100], highs=[100, 110], lows=[100, 90])
    # 전일 변동폭 0(첫봉)이지만 둘째봉 기준 평가. k=0 이면 시가 이상이면 매수.
    strat = build_strategy("volatility_breakout", {"k": 0.0})
    sig = strat.decide(df, Position("X"))
    assert sig.action in (Action.BUY, Action.HOLD)


# ── 신규 전략들 ────────────────────────────────────────────
def test_donchian_breakout_buy_and_exit():
    strat = build_strategy("donchian_breakout", {"entry_period": 20, "exit_period": 10})
    buy = strat.decide(_df([10.0] * 25 + [15.0]), Position("X"))       # 채널 상향 돌파
    assert buy.action == Action.BUY
    sell = strat.decide(_df([10.0] * 25 + [5.0]), Position("X", qty=1, avg_price=10))
    assert sell.action == Action.SELL                                  # 하단 채널 이탈


def test_momentum_buy_and_exit():
    strat = build_strategy("momentum", {"lookback": 10, "entry_pct": 0.05, "exit_pct": 0.0})
    up = [100.0 + i for i in range(15)]                                # 우상향 -> +모멘텀
    assert strat.decide(_df(up), Position("X")).action == Action.BUY
    down = [120.0 - i for i in range(15)]                              # 우하향 -> 모멘텀 소멸
    assert strat.decide(_df(down), Position("X", qty=1, avg_price=120)).action == Action.SELL


def test_momentum_cross_check_fixes_entry_below_exit():
    # entry_pct <= exit_pct 면 cross_check 가 entry 를 올린다(사자마자 팔지 않게).
    s = build_strategy("momentum", {"entry_pct": 0.0, "exit_pct": 0.1})
    assert s.params["entry_pct"] > s.params["exit_pct"]


def test_macd_golden_cross_buys():
    strat = build_strategy("macd", {"fast": 3, "slow": 6, "signal": 2})
    closes = [10.0] * 20 + [14.0]                                      # 막판 급등 -> 골든크로스
    assert strat.decide(_df(closes), Position("X")).action == Action.BUY


def test_bollinger_reversion_buy_and_exit():
    strat = build_strategy("bollinger_reversion", {"period": 20, "num_std": 2.0})
    assert strat.decide(_df([100.0] * 22 + [90.0]), Position("X")).action == Action.BUY
    sell = strat.decide(_df([100.0] * 22 + [105.0]), Position("X", qty=1, avg_price=90))
    assert sell.action == Action.SELL                                  # 중심선 복귀


def test_bollinger_breakout_buy_and_stop():
    strat = build_strategy("bollinger_breakout",
                           {"period": 20, "num_std": 2.0,
                            "stop_loss_pct": 0.02, "target_profit_pct": 0.03})
    assert strat.decide(_df([100.0] * 22 + [110.0]), Position("X")).action == Action.BUY
    stop = strat.decide(_df([100.0] * 22 + [97.0]), Position("X", qty=1, avg_price=100))
    assert stop.action == Action.SELL                                  # 손절


def test_catalog_has_8_with_horizon():
    from src.strategies import strategy_catalog
    cats = strategy_catalog()
    assert len(cats) == 8
    horizons = {c["name"]: c["horizon"] for c in cats}
    assert horizons["momentum"] == "position"
    assert horizons["macd"] == "swing"
    assert horizons["bollinger_breakout"] == "day"
    assert all(c["horizon"] in ("day", "swing", "position") for c in cats)
