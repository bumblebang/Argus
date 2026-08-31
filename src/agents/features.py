"""후보 종목 피처 조립 — market_state + 캔들 지표를 종목별로 묶는다.

각 후보: {symbol,name,market,price,ma20,rsi,momentum_20d,drawdown_pct,drawdown_lookback,
pct_from_52w_high,pct_from_52w_low,vs_sma60_pct,vs_sma120_pct,ret_60d_pct,...}.
가격/지표는 캔들에서 계산(없으면 None). 결정 에이전트가 이 피처를 읽는다.

drawdown_pct/stabilizing 은 "공포에 사되 떨어지는 칼은 피한다"의 종목층 —
낙폭이 클수록 싸지만, 20일선 위 + 20일 수익률 플러스(stabilizing.ok)여야 바닥이 잡힌 것.

gap_pct/open/prev_close 는 간밤 미장·환율이 이 종목 시가에 얼마나 반영됐는지 읽는
로그형 데이터(당일 시가 vs 전일 종가).

intraday_ret_pct 는 당일 시가 대비 현재가(또는 당일 종가) — 장중 과매도 참고.
갭반등 close_scan 전용 pre-filter 플로어(-5%)는 코드가 적용, 단독 매수 신호는 아니다.
"""
from __future__ import annotations

from typing import Callable, Iterable

import pandas as pd

from ..baserate import brief as baserate_brief
from ..gap_rebound_features import gap_shape_fields
from ..indicators import sma, rsi
from ..runner import candles_to_df, last_bar_trading_date, patch_live_price
from ..market_hours import trading_date
from ..logging_setup import get_logger
from ..security_filter import is_buy_ineligible
from .tools import recommend_strategy

log = get_logger("agents.features")

GAP_SCAN_REASONS = frozenset({"gap_rebound_scan", "nxt_gap_scan"})
GAP_REBOUND_INTRADAY_FLOOR = -5.0


def _passes_intraday_floor(c: dict, floor: float) -> bool:
    """intraday_ret_pct 우선, 없으면 풀 랭킹 fluctuation/decline_pct 로 1차 컷."""
    ir = c.get("intraday_ret_pct")
    if isinstance(ir, (int, float)) and ir <= floor:
        return True
    for key in ("decline_pct", "fluctuation"):
        v = c.get(key)
        if isinstance(v, (int, float)) and v <= floor:
            return True
    return False


def filter_gap_rebound_candidates(candidates: list[dict], *,
                                  held: Iterable[str] | None = None,
                                  floor: float = GAP_REBOUND_INTRADAY_FLOOR) -> list[dict]:
    """갭반등 scan: 보유는 유지, 신규 후보는 intraday_ret_pct<=floor 만."""
    keep = {str(s) for s in (held or [])}
    out: list[dict] = []
    for c in candidates:
        sym = str(c.get("symbol") or "")
        if sym in keep:
            out.append(c)
            continue
        if _passes_intraday_floor(c, floor):
            out.append(c)
    return out


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


def _intraday_ret_fields(df: pd.DataFrame | None, price: float | None = None) -> dict:
    """당일 시가 대비 현재가(또는 일봉 종가) → {intraday_ret_pct, intraday_open}.

    일봉 마지막 봉의 open 이 당일 시가. price 가 있으면 라이브가 우선(장중).
    """
    if df is None or len(df) < 1:
        return {}
    if "open" not in df.columns:
        return {}
    op = df["open"].iloc[-1]
    if pd.isna(op):
        return {}
    op = float(op)
    if not op:
        return {}
    px = price
    if px is None and "close" in df.columns:
        cl = df["close"].iloc[-1]
        if not pd.isna(cl):
            px = float(cl)
    if px is None or not px:
        return {}
    return {"intraday_ret_pct": round((px / op - 1) * 100, 2),
            "intraday_open": round(op, 2)}


# Athena 도시에·뇌 후보 공용 — 일봉 60봉+ 위치/추세 요약(원시 OHLCV 대신 스칼라).
_LONG_HORIZON_KEYS = (
    "pct_from_52w_high", "pct_from_52w_low",
    "vs_sma20_pct", "vs_sma60_pct", "vs_sma120_pct",
    "ret_60d_pct", "volume_ratio_20v60",
)


def technical_summary(df: pd.DataFrame) -> dict:
    """일봉 히스토리 → 위치/추세/모멘텀 요약. 60봉 미만이면 {}."""
    if df is None or len(df) < 60:
        return {}
    close = df["close"].astype(float)
    last = float(close.iloc[-1])
    hi52 = float(close.tail(252).max()) if len(close) >= 252 else float(close.max())
    lo52 = float(close.tail(252).min()) if len(close) >= 252 else float(close.min())
    vol = df["volume"].astype(float)

    def _sma_gap(n: int) -> float | None:
        if len(close) < n:
            return None
        return round((last / float(sma(close, n).iloc[-1]) - 1) * 100, 1)

    return {
        "price": round(last, 2),
        "pct_from_52w_high": round((last / hi52 - 1) * 100, 1),
        "pct_from_52w_low": round((last / lo52 - 1) * 100, 1),
        "vs_sma20_pct": _sma_gap(20), "vs_sma60_pct": _sma_gap(60),
        "vs_sma120_pct": _sma_gap(120),
        "rsi14": round(float(rsi(close, 14).iloc[-1]), 1),
        "ret_20d_pct": round((last / float(close.iloc[-20]) - 1) * 100, 1),
        "ret_60d_pct": (round((last / float(close.iloc[-60]) - 1) * 100, 1)
                        if len(close) >= 60 else None),
        "volume_ratio_20v60": (round(float(vol.tail(20).mean())
                                     / float(vol.tail(60).mean()), 2)
                               if float(vol.tail(60).mean()) else None),
        **_gap_fields(df),
    }


def _long_horizon_fields(df: pd.DataFrame | None) -> dict:
    """technical_summary 에서 뇌 후보용 장기 필드만 추출(중복 price/rsi 제외)."""
    tech = technical_summary(df) if df is not None else {}
    return {k: tech[k] for k in _LONG_HORIZON_KEYS if tech.get(k) is not None}


def _has_today_daily_bar(df: pd.DataFrame | None, market: str) -> bool:
    """마지막 일봉이 오늘 거래일인지. time 없으면 True(판정 불가·1m 등)."""
    bar_day = last_bar_trading_date(df, market)
    if bar_day is None:
        return True
    return bar_day == trading_date(market)


def _prepare_daily_df(sym: str, market: str, df: pd.DataFrame | None,
                      live_px: float | None, *,
                      daily_fetch_fresh: bool) -> tuple[pd.DataFrame | None, bool]:
    """라이브가 + 일봉 df → 패치 후 (df, gap/intraday 필드 사용 가능 여부).

    daily_fetch_fresh=True 면 호출측이 이미 fresh Yahoo 를 썼으므로 stale 시 재조회 생략.
    """
    if df is None or live_px is None or live_px <= 0:
        return df, True
    if _has_today_daily_bar(df, market):
        return patch_live_price(df, live_px, market=market), True
    if not daily_fetch_fresh:
        try:
            from .wiring import history_candles_1y
            fresh_raw = history_candles_1y(sym, market, fresh=True)
            if fresh_raw:
                df = candles_to_df(fresh_raw)
        except Exception as e:
            log.warning("[%s] stale 일봉 fresh 재조회 실패: %s", sym, e)
    if _has_today_daily_bar(df, market):
        return patch_live_price(df, live_px, market=market), True
    bar_day = last_bar_trading_date(df, market)
    log.warning("[%s] 당일 일봉 없음(bar_day=%s) — gap/intraday/gap_shape 생략",
                sym, bar_day)
    return df, False


def assemble(items: list[dict], market_state: dict,
             fetch_raw: Callable[[str, str], list[dict]] | None,
             enrich_strategy: bool = False,
             base_rates: dict | None = None,
             live_prices: dict[str, float] | None = None,
             daily_fetch_fresh: bool = False) -> tuple[list[dict], dict]:
    """items: [{symbol,name,market,...}]. fetch_raw(symbol,market)->캔들 리스트(None 가능).
    enrich_strategy=True 면 후보별 전략추천(도구 계산)을 strategy_fit 으로 덧붙인다.
    base_rates: {symbol: analyze 결과}(data/base_rates.json) — 지금 활성인 셋업의
    과거 승률/수익폭 압축본(brief)을 base_rates 피처로 붙인다(활성 없으면 생략).
    live_prices: {symbol: 현재가} — 토스 배치 시세. 있으면 마지막 봉 종가·price_lookup·
    intraday_ret_pct 가 라이브가 우선(Yahoo 캐시 종가 대체). 갭반등·수량 산정 필수.
    daily_fetch_fresh: True 면 fetch_raw 가 이미 fresh Yahoo — stale 시 재조회 1회 생략.
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
        if bad and not it.get("force_include"):
            log.info("[%s] 후보 제외 %s (%s)", market, sym, reason)
            continue
        feat: dict = {"symbol": sym, "name": name, "market": market,
                      "fundamentals": funds.get(sym), "flows": flows.get(sym),
                      "news": [n for n in news if n.get("symbol") == sym][:5]}
        if it.get("force_include"):
            feat["force_include"] = True
        if it.get("serve_stub"):
            feat["serve_stub"] = True
        if bad and it.get("force_include"):
            feat["buy_ineligible"] = reason  # 뇌가 신규매수 금지를 알도록
        if it.get("pool"):
            feat["pool"] = it["pool"]
        if it.get("rank") is not None:
            feat["rank"] = it["rank"]
        if it.get("trading_amount") is not None:
            feat["trading_amount"] = it["trading_amount"]
        if it.get("fluctuation") is not None:
            try:
                feat["fluctuation"] = round(float(it["fluctuation"]), 2)
            except (TypeError, ValueError):
                pass
        if it.get("decline_pct") is not None:
            try:
                feat["decline_pct"] = round(float(it["decline_pct"]), 2)
            except (TypeError, ValueError):
                pass
        if it.get("source"):
            feat["source"] = it["source"]
        if it.get("nxt_supported") is not None:
            feat["nxt_supported"] = it["nxt_supported"]
        if it.get("added_at") is not None:
            feat["added_at"] = it["added_at"]
        if it.get("pool_date"):
            feat["pool_date"] = it["pool_date"]
        if it.get("layer"):
            feat["layer"] = it["layer"]
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
            live_px = None
            if live_prices:
                try:
                    v = live_prices.get(sym)
                    if v is not None:
                        live_px = float(v)
                except (TypeError, ValueError):
                    live_px = None
            daily_ok = True
            if df is not None and live_px is not None and live_px > 0:
                df, daily_ok = _prepare_daily_df(
                    sym, market, df, live_px, daily_fetch_fresh=daily_fetch_fresh)
                if not daily_ok:
                    feat["daily_bar_stale"] = True
            if daily_ok:
                feat.update(_gap_fields(df))   # 시가 갭(간밤 흐름의 반영 정도)
                if df is not None and len(df) >= 1:
                    feat.update(_intraday_ret_fields(df, live_px))
                    feat.update(gap_shape_fields(df, price=live_px))
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
                price = live_px if live_px is not None and live_px > 0 else float(close.iloc[-1])
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
                feat.update(_long_horizon_fields(df))
                price_lookup[sym] = price
                if enrich_strategy:   # 도구: 후보 캔들에 전략 적합도 랭킹
                    rec = recommend_strategy(df)
                    feat["strategy_fit"] = {"best": rec["best"],
                                            "ranking": rec["ranking"][:3]}
        except Exception as e:
            log.warning("[%s] 피처 조립 실패: %s", sym, e)
        candidates.append(feat)
    return candidates, price_lookup
