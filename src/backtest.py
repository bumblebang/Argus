"""백테스트 엔진 (라이브러리) — 전략 템플릿을 실데이터 없이 검증·튜닝한다.

토스엔 모의투자(샌드박스)가 없으므로 백테스트가 실거래 전 유일한 검증 수단이다.
여기서:
  - backtest(strategy, df): 한 전략을 OHLCV 시계열에 돌려 손익·승률·MDD 등 산출.
  - param_grid / sweep: #1 의 ParamSpec 하드바운드를 탐색공간으로 파라미터 스윕(튜닝).
  - synthetic: 국면(추세/횡보/변동성)별 합성 시계열 생성(전략 특성 검증용).

scripts/backtest.py 가 이걸 호출한다(라이브러리/CLI 분리 → 테스트 가능).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product

import numpy as np
import pandas as pd

from .strategies import build_strategy, validate_params, REGISTRY
from .strategies.base import Action, Position


@dataclass
class BacktestResult:
    strategy: str
    params: dict
    pnl: float
    return_pct: float
    n_trades: int               # 완료된 라운드트립(매수→매도) 수
    win_rate: float
    mdd: float                  # 최대낙폭(음수)
    trades: list = field(default_factory=list)

    def summary(self) -> dict:
        return {"strategy": self.strategy, "params": self.params,
                "return_pct": round(self.return_pct, 4), "n_trades": self.n_trades,
                "win_rate": round(self.win_rate, 3), "mdd": round(self.mdd, 4)}


def synthetic(n: int, seed: int = 7, drift: float = 0.0005,
              vol: float = 0.02) -> pd.DataFrame:
    """합성 OHLCV. drift=추세, vol=변동성 → 국면별 검증.

    추세장: drift↑ / 횡보장: drift≈0 / 변동성장: vol↑.
    """
    rng = np.random.default_rng(seed)
    rets = rng.normal(drift, vol, n)
    close = 100 * np.cumprod(1 + rets)
    high = close * (1 + np.abs(rng.normal(0, vol / 2, n)))
    low = close * (1 - np.abs(rng.normal(0, vol / 2, n)))
    open_ = np.concatenate([[close[0]], close[:-1]])
    return pd.DataFrame({
        "time": pd.date_range("2024-01-01", periods=n, freq="D"),
        "open": open_, "high": high, "low": low, "close": close,
        "volume": rng.integers(1000, 10000, n),
    })


def backtest(strategy, df: pd.DataFrame, fee: float = 0.0005) -> BacktestResult:
    """전략 1개를 시계열에 적용. 1주 단위 단순 시뮬, 라운드트립 단위 승률 집계."""
    pos = Position(symbol="BT")
    start_cash = float(df["close"].iloc[0]) * 1.5     # 1주+여유 기준 자본
    cash = start_cash
    trades: list = []
    round_trips: list[float] = []                     # 라운드트립별 실현손익
    equity: list[float] = []
    min_n = max(strategy.min_candles, 2)

    for i in range(min_n, len(df) + 1):
        window = df.iloc[:i].reset_index(drop=True)
        price = float(window["close"].iloc[-1])
        sig = strategy.decide(window, pos)
        if sig.action == Action.BUY and not pos.is_open:
            pos.qty, pos.avg_price = 1.0, price
            cash -= price * (1 + fee)
            trades.append(("BUY", str(window["time"].iloc[-1]), price, sig.reason))
        elif sig.action == Action.SELL and pos.is_open:
            cash += price * (1 - fee)
            round_trips.append((price * (1 - fee)) - (pos.avg_price * (1 + fee)))
            trades.append(("SELL", str(window["time"].iloc[-1]), price, sig.reason))
            pos.qty, pos.avg_price = 0.0, 0.0
        equity.append(cash + pos.qty * price)

    if pos.is_open:                                   # 종료 청산
        last = float(df["close"].iloc[-1])
        cash += last * (1 - fee)
        round_trips.append((last * (1 - fee)) - (pos.avg_price * (1 + fee)))
        trades.append(("SELL(EOD)", str(df["time"].iloc[-1]), last, "백테스트 종료 청산"))

    eq = pd.Series(equity, dtype="float64")
    peak = eq.cummax()
    mdd = float(((eq - peak) / peak.replace(0, np.nan)).min()) if len(eq) else 0.0
    wins = sum(1 for r in round_trips if r > 0)
    win_rate = wins / len(round_trips) if round_trips else 0.0
    pnl = cash - start_cash
    return BacktestResult(
        strategy=getattr(strategy, "name", "?"), params=dict(strategy.params),
        pnl=pnl, return_pct=pnl / start_cash if start_cash else 0.0,
        n_trades=len(round_trips), win_rate=win_rate,
        mdd=mdd if not np.isnan(mdd) else 0.0, trades=trades)


def param_grid(name: str, steps: int = 3) -> list[dict]:
    """전략의 ParamSpec 하드바운드를 steps 등분해 파라미터 조합 목록 생성(탐색공간)."""
    cls = REGISTRY[name]
    axes: list[list] = []
    keys: list[str] = []
    for spec in cls.PARAMS:
        if spec.kind == "int":
            vals = sorted({int(round(v)) for v in np.linspace(spec.min, spec.max, steps)})
        else:
            vals = [round(float(v), 4) for v in np.linspace(spec.min, spec.max, steps)]
        keys.append(spec.name)
        axes.append(vals)
    combos = []
    seen = set()
    for combo in product(*axes):
        raw = dict(zip(keys, combo))
        clamped, _ = validate_params(name, raw)   # cross_check(예: short<long) 반영
        key = tuple(sorted((k, clamped[k]) for k in keys))
        if key not in seen:
            seen.add(key)
            combos.append({k: clamped[k] for k in keys})
    return combos


def sweep(name: str, df: pd.DataFrame, steps: int = 3, fee: float = 0.0005,
          metric: str = "return_pct") -> list[BacktestResult]:
    """파라미터 스윕: 그리드 전 조합을 백테스트해 metric 내림차순 정렬. 첫 항목=최적."""
    results = [backtest(build_strategy(name, p), df, fee=fee) for p in param_grid(name, steps)]
    results.sort(key=lambda r: getattr(r, metric), reverse=True)
    return results


def buy_and_hold(df: pd.DataFrame) -> float:
    """매수 후 보유 수익률 — 전략의 '우위(edge)' 판단 기준선.

    전략 수익률이 이걸 못 넘으면 그 전략은 그 구간에서 가치가 없다(그냥 들고 있는 게 나음).
    """
    if len(df) < 2:
        return 0.0
    first, last = float(df["close"].iloc[0]), float(df["close"].iloc[-1])
    return (last / first - 1) if first else 0.0


def best_per_strategy(df: pd.DataFrame, steps: int = 3, fee: float = 0.0005,
                      metric: str = "return_pct") -> dict[str, BacktestResult]:
    """등록된 모든 전략을 df 에 스윕해 전략별 최적 1개를 반환(리서치 매트릭스용)."""
    out: dict[str, BacktestResult] = {}
    for name in REGISTRY:
        ranked = sweep(name, df, steps=steps, fee=fee, metric=metric)
        if ranked:
            out[name] = ranked[0]
    return out
