"""브레인 분석 도구 — 뇌(LLM)가 더 나은 전략·파라미터 결정을 하도록 돕는 계산 도구.

현재 LLM 경로는 cli(claude -p) 원샷 구조화출력이라 'LLM 이 도구를 반복 호출'하는
tool-use 루프는 API(크레딧)가 필요하다. 그래서 지금은 이 도구들을 **사전 계산해
컨텍스트에 주입**하는 방식으로 쓴다(cli 호환·무료). TOOLKIT 레지스트리로 묶어 두어,
나중에 API tool-use 루프로 전환할 때 그대로 노출할 수 있다.

도구:
  indicator_snapshot(df) — 멀티 지표 스냅샷(MA/RSI/모멘텀/변동성).
  recommend_strategy(df) — 후보 캔들에 전략 3종 간이 백테스트 → 적합 전략 랭킹.
"""
from __future__ import annotations

from typing import Callable

import pandas as pd

from ..indicators import sma, rsi
from ..logging_setup import get_logger
from ..backtest import backtest
from ..strategies import build_strategy, strategy_catalog, REGISTRY

log = get_logger("agents.tools")


def indicator_snapshot(df: pd.DataFrame) -> dict:
    """최신 캔들 기준 멀티 지표. 캔들 부족 시 가능한 것만."""
    if df is None or len(df) == 0:
        return {}
    close = df["close"].astype(float)
    out: dict = {"price": round(float(close.iloc[-1]), 2)}
    n = len(close)
    if n >= 5:
        out["ma5"] = round(float(sma(close, 5).iloc[-1]), 2)
    if n >= 20:
        out["ma20"] = round(float(sma(close, 20).iloc[-1]), 2)
        out["rsi14"] = round(float(rsi(close, 14).iloc[-1]), 1)
        out["momentum_20d"] = round(float(close.iloc[-1] / close.iloc[-20] - 1), 4)
    if n >= 60:
        out["ma60"] = round(float(sma(close, 60).iloc[-1]), 2)
    # 변동성(일간 수익률 표준편차)
    if n >= 10:
        out["volatility"] = round(float(close.pct_change().std()), 4)
    return out


def recommend_strategy(df: pd.DataFrame, fee: float = 0.0005) -> dict:
    """후보 캔들에 전략 3종(기본 파라미터) 간이 백테스트 → 적합 전략 랭킹.

    반환: {best, ranking:[{strategy, return_pct, win_rate, n_trades}]}.
    캔들이 짧으면(전략 min_candles 미만) 그 전략은 제외. 실패는 건너뜀.
    """
    ranking: list[dict] = []
    for name in REGISTRY:
        try:
            strat = build_strategy(name, {})
            if len(df) < strat.min_candles:
                continue
            r = backtest(strat, df, fee=fee)
            ranking.append({"strategy": name, "return_pct": round(r.return_pct, 4),
                            "win_rate": round(r.win_rate, 3), "n_trades": r.n_trades})
        except Exception as e:                  # 한 전략 실패가 추천 전체를 막지 않게
            log.debug("[%s] 추천 백테스트 실패: %s", name, e)
    ranking.sort(key=lambda x: x["return_pct"], reverse=True)
    return {"best": ranking[0]["strategy"] if ranking else None, "ranking": ranking}


# 도구 레지스트리 — 사전계산 주입 + (추후) LLM tool-use 노출 공용.
TOOLKIT: dict[str, dict] = {
    "indicator_snapshot": {
        "fn": indicator_snapshot,
        "desc": "캔들 시계열의 멀티 지표 스냅샷(MA/RSI/모멘텀/변동성)을 반환.",
    },
    "recommend_strategy": {
        "fn": recommend_strategy,
        "desc": "후보 캔들에 전략 3종을 간이 백테스트해 적합 전략을 랭킹.",
    },
    "strategy_catalog": {
        "fn": lambda: strategy_catalog(),
        "desc": "사용 가능한 전략과 파라미터 하드바운드 목록.",
    },
}


def toolkit_manifest() -> list[dict]:
    """도구 이름+설명 목록(컨텍스트에 '이런 도구로 계산된 값'임을 알리거나 tool-use 정의용)."""
    return [{"name": k, "desc": v["desc"]} for k, v in TOOLKIT.items()]
