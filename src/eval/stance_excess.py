"""판정 라벨별 지수 대비 전방 초과수익 — Tier 0 측정(승격 근거 아님).

Athena 도시에 stance(bullish/neutral/bearish)와 밸류 스캔 stance(undervalued/fair/
value_trap)가 실제로 종목을 가르는지 본다. 라벨이 정보를 담고 있다면 '좋은' 라벨의
초과수익이 다른 라벨보다 높아야 한다. 절대수익은 상승장 착시라 지수 대비로만 본다.

표본 = 종목 에피소드: 같은 (종목, 라벨)이 보유 창 안에 다시 찍히면 첫 건만 쓴다
(도시에는 48h, 밸류는 매 스캔 갱신이라 중복이 대부분이다).
창이 아직 안 끝난 관측은 뺀다(미성숙).

10-02 실측(첫 집계): KR bullish 도시에 20거래일 초과 −5.4%p(n=28, 8월 편중),
밸류 undervalued vs fair 차이 +0.8%p — 둘 다 라벨 변별력이 약했다.
"""
from __future__ import annotations

import json
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .labels import MIN_N, asof_local_date, symbol_market
from ..shadow_ledger import horizon_calendar_days, load_daily_series

# 시장별 벤치마크 — data/history 에 일봉이 있는 ETF.
BENCHMARKS = {"KR": "069500", "US": "SPY"}


def _close_on_or_before(series: list[tuple[datetime, float]], day) -> tuple[Any, float] | None:
    best = None
    for d, c in series:
        if d.date() <= day:
            best = (d.date(), c)
        else:
            break
    return best


def _window_return(series, start, end) -> float | None:
    """start 이전 마지막 종가 → end 이전 마지막 종가. 데이터가 end 까지 없으면 None."""
    if not series or series[-1][0].date() < end:
        return None
    a = _close_on_or_before(series, start)
    b = _close_on_or_before(series, end)
    if not a or not b or a[1] <= 0 or b[0] <= a[0]:
        return None
    return b[1] / a[1] - 1.0


def stance_excess(observations: Iterable[dict], *, data_dir: Path | str,
                  horizon: str = "swing", cfg: dict | None = None,
                  benchmarks: dict[str, str] | None = None) -> dict[str, Any]:
    """observations: [{symbol, ts(epoch|iso), label}] → 시장×라벨 초과수익 요약.

    excess = 종목 창 수익 − 같은 창 벤치마크 수익 (%p).
    """
    from .labels import parse_asof

    data_dir = Path(data_dir)
    days = horizon_calendar_days(horizon, cfg)
    bench_syms = benchmarks or BENCHMARKS
    cache: dict[str, list] = {}

    def series(sym: str) -> list:
        if sym not in cache:
            cache[sym] = load_daily_series(data_dir, sym)
        return cache[sym]

    rows = []
    for o in observations:
        dt = parse_asof(o.get("ts"))
        sym = str(o.get("symbol") or "")
        label = o.get("label")
        if dt is None or not sym or not label:
            continue
        rows.append((dt, sym, str(label)))
    rows.sort(key=lambda r: r[0])

    until: dict[tuple[str, str], Any] = {}
    groups: dict[str, dict[str, list]] = {}
    skipped: dict[str, int] = {}

    def skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    for dt, sym, label in rows:
        mkt = symbol_market(sym)
        start = asof_local_date(dt, mkt)
        key = (sym, label)
        if key in until and start < until[key]:
            skip("dup_episode")
            continue
        end = start + timedelta(days=days)
        bench = bench_syms.get(mkt)
        b_ret = _window_return(series(bench), start, end) if bench else None
        s_ret = _window_return(series(sym), start, end)
        if s_ret is None or b_ret is None:
            skip("immature_or_no_data")
            continue
        until[key] = end
        g = groups.setdefault(mkt, {}).setdefault(label, [])
        g.append(((s_ret - b_ret) * 100, sym))

    out: dict[str, Any] = {}
    for mkt, by_label in sorted(groups.items()):
        out[mkt] = {}
        for label, vals in sorted(by_label.items()):
            ex = [v for v, _ in vals]
            out[mkt][label] = {
                "n": len(ex),
                "n_symbols": len({s for _, s in vals}),
                "excess_avg_pp": round(statistics.mean(ex), 2),
                "excess_median_pp": round(statistics.median(ex), 2),
                "beat_rate": round(sum(1 for v in ex if v > 0) / len(ex), 3),
                "small_sample": len(ex) < MIN_N,
            }
    return {
        "horizon": horizon,
        "window_days": days,
        "benchmarks": bench_syms,
        "by_market": out,
        "skipped": skipped,
        "note": ("라벨별 지수 대비 초과수익(종목 에피소드). 좋은 라벨이 나쁜 라벨보다 "
                 "높아야 라벨이 정보를 담은 것. 같은 기간 표본이라 국면 편중 주의 — "
                 "승격·튜닝 단독 근거 금지."),
    }


def dossier_observations(store, *, since: float, now: float | None = None) -> list[dict]:
    """store.dossiers → [{symbol, ts, label=stance}]."""
    from .dossier_quality import dossier_stance

    now = now or datetime.now(timezone.utc).timestamp()
    with store._lock:
        hist = store.conn.execute(
            "SELECT symbol, created_at, evidence FROM dossiers "
            "WHERE created_at >= ? AND created_at <= ?", (since, now)).fetchall()
    return [{"symbol": r["symbol"], "ts": float(r["created_at"]),
             "label": dossier_stance(dict(r))} for r in hist]


def load_value_history(path: Path | str, *, since: float | None = None) -> list[dict]:
    """value_stance_history.jsonl → [{symbol, ts, label=stance}]."""
    p = Path(path)
    if not p.is_file():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        ts = rec.get("ts")
        if since is not None and isinstance(ts, (int, float)) and ts < since:
            continue
        out.append({"symbol": rec.get("symbol"), "ts": ts, "label": rec.get("stance")})
    return out
