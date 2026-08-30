"""갭반등 프록시 백테스트 — Yahoo 일봉 캐시로 풀·구간별 승률 추정.

실전 gap_decline_pool(거래대금 top → 하락 top → ETF 제거)과
intraday_ret_pct<=-5%% pre-filter를 일봉으로 근사한다.

한계(명시):
- 15:20 시점 대신 **당일 종가**로 intraday_ret 근사(장중 -5%% 후 반등한 종목 누락).
- 거래대금 = close×volume (Naver 누적 거래대금·시총 풀과 다름).
- 캐시된 종목만 유니버스(전 종목 KRX 아님).
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Callable, Iterable

import pandas as pd

from .config import ROOT
from .datasources.stock_info import load_info_cache
from .gap_rebound_features import (
    CLOSE_LOC_LOW_MAX,
    GAP_DOWN_DEEP_PCT,
    VOL_RATIO_SPIKE,
    day_close_loc,
)
from .eval.trade_defs import roundtrip_cost_pct
from .security_filter import is_buy_ineligible

PRIOR_PATH = ROOT / "data" / "gap_rebound_prior.json"
PRIOR_MIN_N = 200
# 국장 왕복 비용 하한(%p). cfg/paper 기본(≈0.28%%)과 max — 0 비용 착시 방지.
KR_MIN_ROUNDTRIP_COST_PCT = 0.2

_HISTORY = ROOT / "data" / "history"
_CACHE_RE = re.compile(r"^(\d{6})\.(KS|KQ)_1d_(1y|2y|5y|6mo)\.csv$", re.I)
_RANGE_RANK = {"6mo": 1, "1y": 2, "2y": 3, "5y": 4}

EVENT_COLS = [
    "date", "symbol", "name", "intraday_ret_pct", "daily_ret_pct",
    "trading_value", "decline_rank", "entry_px", "exit_open_px", "exit_close_px",
    "ret_next_open_pct", "ret_next_close_pct",
]


def parse_symbol_from_history_name(name: str) -> tuple[str, str] | None:
    m = _CACHE_RE.match(name)
    if not m:
        return None
    return m.group(1), m.group(2).upper()


def pick_history_files(history_dir: Path | None = None) -> dict[str, Path]:
    """종목당 가장 긴 range 캐시 1개."""
    root = history_dir or _HISTORY
    best: dict[str, tuple[int, Path]] = {}
    if not root.is_dir():
        return {}
    for p in root.glob("*.csv"):
        parsed = parse_symbol_from_history_name(p.name)
        if not parsed:
            continue
        sym, _suffix = parsed
        rng = p.name.split("_1d_")[1].replace(".csv", "")
        rank = _RANGE_RANK.get(rng, 0)
        prev = best.get(sym)
        if prev is None or rank > prev[0]:
            best[sym] = (rank, p)
    return {sym: path for sym, (_r, path) in best.items()}


def load_symbol_frame(path: Path) -> pd.DataFrame:
    try:
        df = pd.read_csv(path, parse_dates=["time"])
    except pd.errors.EmptyDataError:
        return pd.DataFrame()
    if df.empty:
        return df
    df = df.sort_values("time").drop_duplicates(subset=["time"], keep="last")
    for c in ("open", "high", "low", "close", "volume"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df["date"] = df["time"].dt.normalize()
    return df


def build_panel(files: dict[str, Path] | None = None,
                history_dir: Path | None = None) -> pd.DataFrame:
    """symbol×date 패널 — OHLCV + 파생."""
    paths = files if files is not None else pick_history_files(history_dir)
    chunks: list[pd.DataFrame] = []
    for sym, path in paths.items():
        df = load_symbol_frame(path)
        if len(df) < 3:
            continue
        df = df.assign(symbol=sym)
        df["prev_close"] = df["close"].shift(1)
        df["next_open"] = df["open"].shift(-1)
        df["next_close"] = df["close"].shift(-1)
        df["intraday_ret_pct"] = (df["close"] / df["open"] - 1) * 100
        df["daily_ret_pct"] = (df["close"] / df["prev_close"] - 1) * 100
        df["trading_value"] = df["close"] * df["volume"]
        df["gap_pct"] = (df["open"] / df["prev_close"] - 1) * 100
        df["close_loc"] = [
            day_close_loc(h, lo, c)
            for h, lo, c in zip(df["high"], df["low"], df["close"])
        ]
        vol = df["volume"].astype(float)
        df["vol_ratio_20d"] = vol / vol.rolling(20, min_periods=5).mean()
        chunks.append(df)
    if not chunks:
        return pd.DataFrame()
    panel = pd.concat(chunks, ignore_index=True)
    panel = panel.dropna(subset=["open", "close", "prev_close", "next_open"])
    return panel


def roundtrip_cost_pct_points(market: str = "KR", cfg: dict | None = None) -> float:
    """왕복 비용(%p). trade_defs 기본값과 KR 하한(0.2%%) 중 큰 값."""
    return max(roundtrip_cost_pct(market, cfg) * 100.0, KR_MIN_ROUNDTRIP_COST_PCT)


def _eligible_symbols(symbols: Iterable[str], info_cache: dict | None) -> set[str]:
    out: set[str] = set()
    for sym in symbols:
        blocked, _ = is_buy_ineligible(sym, "KR", name="", info_cache=info_cache)
        if not blocked:
            out.add(sym)
    return out


def enrich_events(events: pd.DataFrame, panel: pd.DataFrame) -> pd.DataFrame:
    """이벤트에 패널 파생(gap_pct·close_loc·vol_ratio) 병합."""
    if events.empty:
        return events
    cols = ["date", "symbol", "gap_pct", "close_loc", "vol_ratio_20d",
            "high", "low", "open", "close"]
    feat = panel[cols].copy()
    feat["date"] = pd.to_datetime(feat["date"]).dt.normalize()
    out = events.copy()
    out["date"] = pd.to_datetime(out["date"]).dt.normalize()
    return out.merge(feat, on=["date", "symbol"], how="left", suffixes=("", "_bar"))


def events_for_day(day: pd.DataFrame, *,
                   liq_top: int = 300,
                   decline_top: int = 100,
                   intraday_floor: float = -5.0,
                   use_decline_pool: bool = True,
                   eligible: set[str] | None = None) -> pd.DataFrame:
    """하루 단면 → 진입 이벤트(종가 매수, 익일 시가/종가 청산)."""
    df = day.copy()
    if eligible is not None:
        df = df[df["symbol"].isin(eligible)]
    df = df[df["trading_value"] > 0]
    if df.empty:
        return pd.DataFrame(columns=EVENT_COLS)

    liq = df.nlargest(liq_top, "trading_value")
    if use_decline_pool:
        pool = liq.nsmallest(decline_top, "daily_ret_pct")
    else:
        pool = liq

    pool = pool[pool["intraday_ret_pct"] <= intraday_floor]
    if pool.empty:
        return pd.DataFrame(columns=EVENT_COLS)

    pool = pool.copy()
    pool["decline_rank"] = (
        pool["daily_ret_pct"].rank(method="first").astype(int)
    )
    cost = roundtrip_cost_pct_points("KR")
    pool["entry_px"] = pool["close"]
    pool["exit_open_px"] = pool["next_open"]
    pool["exit_close_px"] = pool["next_close"]
    pool["ret_next_open_pct"] = (pool["next_open"] / pool["close"] - 1) * 100 - cost
    pool["ret_next_close_pct"] = (pool["next_close"] / pool["close"] - 1) * 100 - cost
    pool["name"] = pool["symbol"]
    return pool[EVENT_COLS]


def collect_events(panel: pd.DataFrame, *,
                   liq_top: int = 300,
                   decline_top: int = 100,
                   intraday_floor: float = -5.0,
                   use_decline_pool: bool = True,
                   min_symbols: int = 200,
                   info_cache: dict | None = None) -> pd.DataFrame:
    if panel.empty:
        return pd.DataFrame(columns=EVENT_COLS)
    cache = info_cache if info_cache is not None else load_info_cache()
    eligible = _eligible_symbols(panel["symbol"].unique(), cache)
    parts: list[pd.DataFrame] = []
    for dt, day in panel.groupby("date", sort=True):
        if day["symbol"].nunique() < min_symbols:
            continue
        ev = events_for_day(
            day, liq_top=liq_top, decline_top=decline_top,
            intraday_floor=intraday_floor, use_decline_pool=use_decline_pool,
            eligible=eligible,
        )
        if not ev.empty:
            parts.append(ev)
    if not parts:
        return pd.DataFrame(columns=EVENT_COLS)
    return pd.concat(parts, ignore_index=True)


def _bucket_label(lo: float, hi: float | None) -> str:
    if hi is None:
        return f"<={lo:.0f}%"
    return f"({lo:.0f}%, {hi:.0f}%]"


def bucket_edges(floor: float = -5.0, step: float = 1.0,
                 tail: float = -15.0) -> list[tuple[float, float | None, str]]:
    """floor 이하 구간: (-6,-5], (-7,-6], … tail 이하는 한 묶음."""
    edges: list[tuple[float, float | None, str]] = []
    hi = floor
    lo = floor - step
    while lo >= tail:
        edges.append((lo, hi, _bucket_label(lo, hi)))
        hi = lo
        lo -= step
    edges.append((None, hi, _bucket_label(tail, None)))
    return edges


def assign_bucket(intraday: float, edges: list[tuple[float, float | None, str]]) -> str | None:
    for lo, hi, label in edges:
        if lo is None:
            if intraday <= hi:
                return label
        elif lo < intraday <= hi:
            return label
    return None


def summarize_by_bucket(events: pd.DataFrame, *,
                        floor: float = -5.0,
                        step: float = 1.0,
                        tail: float = -15.0) -> pd.DataFrame:
    if events.empty:
        return pd.DataFrame()
    edges = bucket_edges(floor=floor, step=step, tail=tail)
    df = events.copy()
    df["bucket"] = df["intraday_ret_pct"].map(lambda x: assign_bucket(float(x), edges))
    df = df.dropna(subset=["bucket"])

    rows = []
    for label, grp in df.groupby("bucket", sort=False):
        n = len(grp)
        rows.append({
            "bucket": label,
            "n": n,
            "win_open_pct": round((grp["ret_next_open_pct"] > 0).mean() * 100, 1),
            "win_close_pct": round((grp["ret_next_close_pct"] > 0).mean() * 100, 1),
            "avg_ret_open": round(grp["ret_next_open_pct"].mean(), 2),
            "avg_ret_close": round(grp["ret_next_close_pct"].mean(), 2),
            "med_ret_open": round(grp["ret_next_open_pct"].median(), 2),
            "med_ret_close": round(grp["ret_next_close_pct"].median(), 2),
            "avg_intraday": round(grp["intraday_ret_pct"].mean(), 2),
            "avg_daily": round(grp["daily_ret_pct"].mean(), 2),
        })
    order = [e[2] for e in edges]
    out = pd.DataFrame(rows)
    out["bucket"] = pd.Categorical(out["bucket"], categories=order, ordered=True)
    return out.sort_values("bucket").reset_index(drop=True)


def summarize_overall(events: pd.DataFrame) -> dict:
    if events.empty:
        return {}
    return {
        "n_events": len(events),
        "n_days": int(events["date"].nunique()),
        "n_symbols": int(events["symbol"].nunique()),
        "date_from": str(events["date"].min().date()),
        "date_to": str(events["date"].max().date()),
        "win_open_pct": round((events["ret_next_open_pct"] > 0).mean() * 100, 1),
        "win_close_pct": round((events["ret_next_close_pct"] > 0).mean() * 100, 1),
        "avg_ret_open": round(events["ret_next_open_pct"].mean(), 2),
        "avg_ret_close": round(events["ret_next_close_pct"].mean(), 2),
    }


def _cond_row(id_: str, label: str, grp: pd.DataFrame, *,
              min_n: int = 50) -> dict | None:
    n = len(grp)
    if n < min_n:
        return None
    return {
        "id": id_,
        "label": label,
        "n": n,
        "win_open_pct": round((grp["ret_next_open_pct"] > 0).mean() * 100, 1),
        "win_close_pct": round((grp["ret_next_close_pct"] > 0).mean() * 100, 1),
        "avg_ret_open": round(grp["ret_next_open_pct"].mean(), 2),
        "avg_ret_close": round(grp["ret_next_close_pct"].mean(), 2),
        "small_sample": n < PRIOR_MIN_N,
    }


def condition_specs() -> list[tuple[str, str, Callable[[pd.DataFrame], pd.Series]]]:
    """조건부 승률 테이블용 (id, label, mask)."""
    return [
        ("gap_down_deep", f"gap<={GAP_DOWN_DEEP_PCT:.0f}%",
         lambda d: d["gap_pct"] <= GAP_DOWN_DEEP_PCT),
        ("gap_flat_up", f"gap>{GAP_DOWN_DEEP_PCT:.0f}%",
         lambda d: d["gap_pct"] > GAP_DOWN_DEEP_PCT),
        ("close_near_low", f"close_loc<={CLOSE_LOC_LOW_MAX}",
         lambda d: d["close_loc"] <= CLOSE_LOC_LOW_MAX),
        ("close_not_low", f"close_loc>{CLOSE_LOC_LOW_MAX}",
         lambda d: d["close_loc"] > CLOSE_LOC_LOW_MAX),
        ("vol_spike", f"vol_ratio>={VOL_RATIO_SPIKE}x",
         lambda d: d["vol_ratio_20d"] >= VOL_RATIO_SPIKE),
        ("vol_normal", f"vol_ratio<{VOL_RATIO_SPIKE}x",
         lambda d: d["vol_ratio_20d"] < VOL_RATIO_SPIKE),
        ("gap_down_and_low_close",
         f"gap<={GAP_DOWN_DEEP_PCT:.0f}% & close_loc<={CLOSE_LOC_LOW_MAX}",
         lambda d: (d["gap_pct"] <= GAP_DOWN_DEEP_PCT)
         & (d["close_loc"] <= CLOSE_LOC_LOW_MAX)),
    ]


def summarize_conditional(events: pd.DataFrame, *,
                          min_n: int = 50) -> pd.DataFrame:
    if events.empty:
        return pd.DataFrame()
    rows = []
    for id_, label, mask_fn in condition_specs():
        try:
            sub = events[mask_fn(events).fillna(False)]
        except (KeyError, TypeError):
            continue
        row = _cond_row(id_, label, sub, min_n=min_n)
        if row:
            rows.append(row)
    return pd.DataFrame(rows)


def summarize_winner_loser(events: pd.DataFrame) -> dict:
    """익일 시가 승/패 그룹 평균 피처 비교."""
    if events.empty:
        return {}
    cols = ["intraday_ret_pct", "daily_ret_pct", "gap_pct", "close_loc",
            "vol_ratio_20d", "decline_rank"]
    cols = [c for c in cols if c in events.columns]
    win = events[events["ret_next_open_pct"] > 0]
    lose = events[events["ret_next_open_pct"] <= 0]

    def _means(df: pd.DataFrame) -> dict:
        if df.empty:
            return {}
        return {c: round(float(df[c].mean()), 2) for c in cols if c in df.columns}

    return {
        "win_open": {"n": len(win), **_means(win)},
        "lose_open": {"n": len(lose), **_means(lose)},
        "top25_bounce": _means(
            events[events["ret_next_open_pct"] >= events["ret_next_open_pct"].quantile(0.75)]),
        "bottom25_bounce": _means(
            events[events["ret_next_open_pct"] <= events["ret_next_open_pct"].quantile(0.25)]),
    }


def build_prior(events: pd.DataFrame, overall: dict | None = None, *,
                caveats: list[str] | None = None) -> dict:
    """focus 렌즈·LLM용 압축 prior."""
    cond = summarize_conditional(events)
    wl = summarize_winner_loser(events)
    prior_conds = []
    if not cond.empty:
        for _, row in cond.iterrows():
            if row.get("small_sample"):
                continue
            prior_conds.append({
                "id": row["id"],
                "label": row["label"],
                "n": int(row["n"]),
                "win_open_pct": row["win_open_pct"],
                "avg_ret_open": row["avg_ret_open"],
            })
    return {
        "asof": time.strftime("%Y-%m-%d"),
        "caveats": caveats or [
            "Yahoo 일봉 프록시; 15:20=종가 근사",
            "거래대금=close*volume; 캐시 유니버스",
            f"수익률=왕복비용 차감(≥{KR_MIN_ROUNDTRIP_COST_PCT:.1f}%p, fee*2+세+slip)",
        ],
        "overall": overall or summarize_overall(events),
        "conditions": prior_conds,
        "winner_loser": wl,
    }


def save_prior(prior: dict, path: Path | None = None) -> Path:
    p = path or PRIOR_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(prior, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)
    return p


def load_prior(path: Path | None = None) -> dict | None:
    p = path or PRIOR_PATH
    try:
        if not p.exists():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def prior_hint_block(prior: dict | None) -> str:
    """focus hint 에 붙일 한 줄 요약."""
    if not prior or not isinstance(prior.get("conditions"), list):
        return ""
    parts = []
    for c in prior["conditions"]:
        if not isinstance(c, dict):
            continue
        cid = c.get("id")
        if cid == "close_not_low":
            parts.append(
                f"close_loc>{CLOSE_LOC_LOW_MAX} win_open={c.get('win_open_pct')}% "
                f"(n={c.get('n')}, caution)"
            )
        elif cid in ("gap_down_deep", "close_near_low", "gap_down_and_low_close"):
            parts.append(
                f"{c.get('label')} win_open={c.get('win_open_pct')}% (n={c.get('n')})"
            )
    if not parts:
        return ""
    return " 백테 prior(프록시): " + "; ".join(parts[:4]) + "."
