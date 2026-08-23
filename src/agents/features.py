"""후보 종목 피처 조립 — market_state + 캔들 지표를 종목별로 묶는다.

각 후보: {symbol,name,market,price,ma20,rsi,momentum_20d,drawdown_pct,drawdown_lookback,
stabilizing,gap_pct,open,prev_close,volume,fundamentals,flows,news[]}. 가격/지표는 캔들에서
계산(없으면 None). 결정 에이전트가 이 피처를 읽는다.

drawdown_pct/stabilizing 은 "공포에 사되 떨어지는 칼은 피한다"의 종목층 —
낙폭이 클수록 싸지만, 20일선 위 + 20일 수익률 플러스(stabilizing.ok)여야 바닥이 잡힌 것.

gap_pct/open/prev_close 는 간밤 미장·환율이 이 종목 시가에 얼마나 반영됐는지 읽는
로그형 데이터(당일 시가 vs 전일 종가).
"""
from __future__ import annotations

from typing import Callable

import pandas as pd

from ..baserate import brief as baserate_brief
from ..indicators import sma, rsi
from ..runner import candles_to_df
from ..logging_setup import get_logger
from ..security_filter import is_buy_ineligible
from .tools import recommend_strategy

log = get_logger("agents.features")


def _gap_fields(df: pd.DataFrame | None) -> dict:
    """일봉 df → {gap_pct, open, prev_close}. 계산 불가면 빈 dict(필드 생략).

    gap_pct = (당일 시가/전일 종가 - 1)*100. 간밤 미장·환율이 시가에 얼마나 반영됐는지
    보는 참고 수치이지 그 자체로 매매 신호가 아니다. Athena technical_summary 와 공용.
    """
    if df is None or len(df) < 2:
        return {}
    if "open" not in df.columns or "close" not in df.columns:
        return {}
    op, prev = df["open"].iloc[-1], df["close"].iloc[-2]
    if pd.isna(op) or pd.isna(prev):
        return {}
    op, prev = float(op), float(prev)
    if not op or not prev:
        return {}
    return {"gap_pct": round((op / prev - 1) * 100, 2),
            "open": round(op, 2), "prev_close": round(prev, 2)}


def assemble(items: list[dict], market_state: dict,
             fetch_raw: Callable[[str, str], list[dict]] | None,
             enrich_strategy: bool = False,
             base_rates: dict | None = None) -> tuple[list[dict], dict]:
    """items: [{symbol,name,market,...}]. fetch_raw(symbol,market)->캔들 리스트(None 가능).
    enrich_strategy=True 면 후보별 전략추천(도구 계산)을 strategy_fit 으로 덧붙인다.
    base_rates: {symbol: analyze 결과}(data/base_rates.json) — 지금 활성인 셋업의
    과거 승률/수익폭 압축본(brief)을 base_rates 피처로 붙인다(활성 없으면 생략).
    반환: (candidates, price_lookup{symbol:price})."""
    funds = (market_state or {}).get("fundamentals", {})
    flows = (market_state or {}).get("flows", {})
    positioning = (market_state or {}).get("positioning", {})
    news = (market_state or {}).get("news", [])
    candidates: list[dict] = []
    price_lookup: dict[str, float] = {}

    for it in items:
        sym, market = it["symbol"], it.get("market", "KR")
        name = it.get("name", sym)
        bad, reason = is_buy_ineligible(sym, market, name)
        if bad:
            log.info("[%s] 후보 제외 %s (%s)", market, sym, reason)
            continue
        feat: dict = {"symbol": sym, "name": name, "market": market,
                      "fundamentals": funds.get(sym), "flows": flows.get(sym),
                      "news": [n for n in news if n.get("symbol") == sym][:5]}
        if it.get("pool"):
            feat["pool"] = it["pool"]
        if it.get("rank") is not None:
            feat["rank"] = it["rank"]
        if it.get("trading_amount") is not None:
            feat["trading_amount"] = it["trading_amount"]
        if sym in positioning:
            feat["positioning"] = positioning[sym]
        if it.get("sector"):
            feat["sector"] = it["sector"]
        if base_rates and sym in base_rates:
            b = baserate_brief(base_rates[sym])
            if b:
                feat["base_rates"] = b   # 지금 활성 셋업의 과거 승률/수익폭
        try:
            raw = fetch_raw(sym, market) if fetch_raw else None
            df = candles_to_df(raw) if raw else None
            feat.update(_gap_fields(df))   # 시가 갭(간밤 흐름의 반영 정도)
            if df is not None and "volume" in df.columns and len(df) >= 1:
                vol = df["volume"].iloc[-1]
                try:
                    vol_f = float(vol)
                except (TypeError, ValueError):
                    vol_f = None
                if vol_f is not None and vol_f == vol_f and vol_f > 0:
                    feat["volume"] = round(vol_f, 2)
            if df is not None and len(df) >= 20:
                close = df["close"].astype(float)
                price = float(close.iloc[-1])
                feat["price"] = round(price, 2)
                feat["ma20"] = round(float(sma(close, 20).iloc[-1]), 2)
                feat["rsi"] = round(float(rsi(close, 14).iloc[-1]), 1)
                feat["momentum_20d"] = round(float(close.iloc[-1] / close.iloc[-20] - 1), 4)
                # 낙폭(최근 lb봉 고점 대비)과 안정화 신호 — 공포 매수의 진입 타이밍 판단용.
                lb = min(60, len(close))
                hi = float(close.tail(lb).max())
                feat["drawdown_pct"] = round((price / hi - 1) * 100, 1) if hi else None
                feat["drawdown_lookback"] = lb
                ret5 = (price / float(close.iloc[-5]) - 1) if len(close) >= 5 else None
                feat["stabilizing"] = {
                    "above_ma20": bool(price > feat["ma20"]),
                    "ret_20d_pct": round(feat["momentum_20d"] * 100, 1),
                    "ret_5d_pct": round(ret5 * 100, 1) if ret5 is not None else None,
                    # bool() 필수 — numpy bool 이면 json.dumps 가 깨진다.
                    "ok": bool(price > feat["ma20"] and feat["momentum_20d"] > 0),
                }
                price_lookup[sym] = price
                if enrich_strategy:   # 도구: 후보 캔들에 전략 적합도 랭킹
                    rec = recommend_strategy(df)
                    feat["strategy_fit"] = {"best": rec["best"],
                                            "ranking": rec["ranking"][:3]}
        except Exception as e:
            log.warning("[%s] 피처 조립 실패: %s", sym, e)
        candidates.append(feat)
    return candidates, price_lookup
