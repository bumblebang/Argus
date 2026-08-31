"""과거 캔들 데이터 (백테스트용) — Yahoo Finance 차트 API.

토스 캔들은 최대 200봉이라 백테스트엔 부족하다 → Yahoo 로 **긴 히스토리·다중 타임프레임**
(주/일/시간/분봉)을 가져온다. 역할 분리: **라이브 매매는 토스, 백테스트 데이터는 Yahoo.**
(Yahoo KR 시세는 토스와 미세하게 다를 수 있으나, 전략 *상대비교/검증*엔 충분.)

반환은 backtest.backtest() 가 먹는 표준 OHLCV: time,open,high,low,close,volume (시간 오름차순).
캐시: data/history/{yahoo}_{interval}_{range}.csv — 반복 백테스트를 빠르게·API 호출 절약.

지원 interval(별칭 포함): 1m/2m/5m/15m/30m, 60m(=1h), 1d, 1wk(=weekly), 1mo
지원 range: 1d/5d/1mo/3mo/6mo/1y/2y/5y/10y/max (분봉은 Yahoo 가 최근 며칠만 제공)
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import requests

from ..config import ROOT
from ..logging_setup import get_logger

log = get_logger("src.history")

_UA = {"User-Agent": "Mozilla/5.0 argus"}
YF = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
CACHE = ROOT / "data" / "history"

# people-readable aliases → Yahoo notation
_INTERVAL_ALIAS = {"1h": "60m", "hourly": "60m", "daily": "1d",
                   "weekly": "1wk", "1w": "1wk", "monthly": "1mo"}

COLUMNS = ["time", "open", "high", "low", "close", "volume"]


def _utc_day(epoch) -> date:
    return datetime.fromtimestamp(float(epoch), timezone.utc).date()


def _kr_yahoo_suffix(symbol: str, info_cache: dict | None) -> str:
    """stock_info 캐시 market 으로 .KS/.KQ 확정. 미스면 .KS(코스피 기본)."""
    if not info_cache:
        return ".KS"
    entry = info_cache.get(symbol) or {}
    info = entry.get("info") if isinstance(entry.get("info"), dict) else entry
    mkt = str((info or {}).get("market") or "").upper()
    if "KOSDAQ" in mkt or mkt in ("KSQ", "KQ"):
        return ".KQ"
    return ".KS"


def to_yahoo(symbol: str, market: str = "KR", *,
             info_cache: dict | None = None) -> str:
    """KR 6자리 코드 → Yahoo 티커. US/이미 티커면 그대로.

    KR 은 data/stock_info_cache.json 의 market(KOSPI/KOSDAQ)으로 .KS/.KQ 를 확정한다.
    캐시에 없으면 .KS — fetch_history 가 빈 결과일 때만 .KQ 폴백(레거시·미캐시 종목).
    """
    s = str(symbol).strip()
    if market == "US" or not s.isdigit():
        return s
    return f"{s}{_kr_yahoo_suffix(s, info_cache)}"


def _supplement_daily_from_meta(res: dict, df: pd.DataFrame) -> pd.DataFrame:
    """일봉: Yahoo 가 당일 봉을 빼거나 null 이면 meta.regularMarket* 로 한 봉 보충.

    markets._closes_with_time 과 동일 계약 — 15:20 갭스캔 fresh fetch 가 어제까지만
    오는 창 밀림을 막는다.
    """
    if df.empty:
        return df
    meta = res.get("meta") or {}
    mt, mp = meta.get("regularMarketTime"), meta.get("regularMarketPrice")
    if mt is None or mp is None:
        return df
    try:
        # markets._day(epoch) 과 동일 — Yahoo epoch UTC 거래일 비교
        ts = pd.Timestamp(df["time"].iloc[-1])
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        last_epoch = int(ts.timestamp())
        if _utc_day(mt) <= _utc_day(last_epoch):
            return df
        mp_f = float(mp)
        mo = meta.get("regularMarketOpen")
        mh = meta.get("regularMarketDayHigh")
        ml = meta.get("regularMarketDayLow")
        mv = meta.get("regularMarketVolume")
        o = float(mo) if mo is not None else mp_f
        h = float(mh) if mh is not None else max(o, mp_f)
        lo = float(ml) if ml is not None else min(o, mp_f)
        vol = float(mv) if mv is not None else 0.0
        row = pd.DataFrame([{
            "time": pd.to_datetime(int(mt), unit="s", utc=True),
            "open": o, "high": h, "low": lo, "close": mp_f, "volume": vol,
        }])
        return pd.concat([df, row], ignore_index=True)
    except (TypeError, ValueError, OSError):
        return df


def _fetch_yahoo(ysym: str, interval: str, range_: str) -> pd.DataFrame:
    url = YF.format(symbol=ysym.replace("^", "%5E"))
    r = requests.get(url, params={"range": range_, "interval": interval},
                     headers=_UA, timeout=15)
    res = (r.json().get("chart") or {}).get("result")
    if not res:
        return pd.DataFrame(columns=COLUMNS)
    res = res[0]
    ts = res.get("timestamp") or []
    quote = (res.get("indicators", {}).get("quote") or [{}])[0]
    o, h = quote.get("open", []), quote.get("high", [])
    lo, c, v = quote.get("low", []), quote.get("close", []), quote.get("volume", [])
    rows = []
    for i, t in enumerate(ts):
        try:
            row = (o[i], h[i], lo[i], c[i], v[i])
        except IndexError:
            continue
        if any(x is None for x in row[:4]):           # OHLC 결측 봉은 버린다
            continue
        rows.append((pd.to_datetime(t, unit="s", utc=True), row[0], row[1], row[2], row[3],
                     row[4] or 0))
    df = pd.DataFrame(rows, columns=COLUMNS)
    if interval == "1d":
        df = _supplement_daily_from_meta(res, df)
    return df


def fetch_history(symbol: str, interval: str = "1d", range_: str = "2y",
                  market: str = "KR", *, use_cache: bool = True,
                  refresh: bool = False, max_age_hours: float | None = None) -> pd.DataFrame:
    """과거 캔들 DataFrame(OHLCV) 반환. 캐시 우선, 없으면 Yahoo 호출 후 캐시.

    KR 6자리는 stock_info 캐시로 .KS/.KQ 를 확정하고, 빈 결과면 .KQ/.KS 반대 접미사로
    한 번 더 시도한다(캐시 미스·신규 상장 방어).
    max_age_hours 지정 시 캐시가 그보다 오래됐으면 재조회(없으면 캐시 무기한 — 기존 동작).
    """
    interval = _INTERVAL_ALIAS.get(interval, interval)
    info_cache = None
    if market == "KR":
        from .stock_info import load_info_cache
        info_cache = load_info_cache()
    ysym = to_yahoo(symbol, market, info_cache=info_cache)
    cache = CACHE / f"{ysym}_{interval}_{range_}.csv"
    if use_cache and not refresh and cache.exists():
        import time as _time
        fresh = (max_age_hours is None
                 or (_time.time() - cache.stat().st_mtime) < max_age_hours * 3600)
        if fresh:
            return pd.read_csv(cache, parse_dates=["time"])

    try:
        df = _fetch_yahoo(ysym, interval, range_)
        if df.empty and market == "KR" and ysym.endswith((".KS", ".KQ")):
            alt = ".KQ" if ysym.endswith(".KS") else ".KS"
            df = _fetch_yahoo(ysym[:-3] + alt, interval, range_)   # stock_info 미스 폴백
    except requests.RequestException as e:
        log.warning("Yahoo 조회 실패 %s: %s", ysym, e)
        df = pd.DataFrame(columns=COLUMNS)
    if df.empty:
        if use_cache and cache.exists():          # 갱신 실패 시 낡은 캐시가 없는 것보다 낫다
            log.warning("갱신 실패 → 기존 캐시 사용: %s", cache.name)
            return pd.read_csv(cache, parse_dates=["time"])
        log.warning("과거 캔들 없음: %s %s %s", symbol, interval, range_)
        return df
    if use_cache:
        cache.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(cache, index=False)
    log.info("과거 캔들 %s(%s) %s %s -> %d봉", symbol, ysym, interval, range_, len(df))
    return df
