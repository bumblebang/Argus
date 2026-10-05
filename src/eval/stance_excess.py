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


def stance_episodes(observations: Iterable[dict], *, data_dir: Path | str,
                    horizon: str = "swing", cfg: dict | None = None,
                    benchmarks: dict[str, str] | None = None
                    ) -> tuple[list[dict], dict[str, int]]:
    """observations: [{symbol, ts(epoch|iso), label}] → (성숙 에피소드 행, skipped).

    행: {symbol, market, label, start(date), excess_pp}.
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

    obs = []
    for o in observations:
        dt = parse_asof(o.get("ts"))
        sym = str(o.get("symbol") or "")
        label = o.get("label")
        if dt is None or not sym or not label:
            continue
        obs.append((dt, sym, str(label)))
    obs.sort(key=lambda r: r[0])

    until: dict[tuple[str, str], Any] = {}
    rows: list[dict] = []
    skipped: dict[str, int] = {}

    def skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    for dt, sym, label in obs:
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
        rows.append({"symbol": sym, "market": mkt, "label": label, "start": start,
                     "excess_pp": round((s_ret - b_ret) * 100, 2)})
    return rows, skipped


def summarize_episodes(rows: Iterable[dict]) -> dict[str, dict[str, dict]]:
    """에피소드 행 → {market: {label: {n, n_symbols, excess_*, beat_rate, small_sample}}}."""
    groups: dict[str, dict[str, list]] = {}
    for r in rows:
        groups.setdefault(r["market"], {}).setdefault(r["label"], []).append(r)
    out: dict[str, dict[str, dict]] = {}
    for mkt, by_label in sorted(groups.items()):
        out[mkt] = {}
        for label, vals in sorted(by_label.items()):
            ex = [v["excess_pp"] for v in vals]
            out[mkt][label] = {
                "n": len(ex),
                "n_symbols": len({v["symbol"] for v in vals}),
                "excess_avg_pp": round(statistics.mean(ex), 2),
                "excess_median_pp": round(statistics.median(ex), 2),
                "beat_rate": round(sum(1 for v in ex if v > 0) / len(ex), 3),
                "small_sample": len(ex) < MIN_N,
            }
    return out


def stance_excess(observations: Iterable[dict], *, data_dir: Path | str,
                  horizon: str = "swing", cfg: dict | None = None,
                  benchmarks: dict[str, str] | None = None) -> dict[str, Any]:
    """observations → 시장×라벨 초과수익 요약."""
    rows, skipped = stance_episodes(observations, data_dir=data_dir, horizon=horizon,
                                    cfg=cfg, benchmarks=benchmarks)
    return {
        "horizon": horizon,
        "window_days": horizon_calendar_days(horizon, cfg),
        "benchmarks": benchmarks or BENCHMARKS,
        "by_market": summarize_episodes(rows),
        "skipped": skipped,
        "note": ("라벨별 지수 대비 초과수익(종목 에피소드). 좋은 라벨이 나쁜 라벨보다 "
                 "높아야 라벨이 정보를 담은 것. 같은 기간 표본이라 국면 편중 주의 — "
                 "승격·튜닝 단독 근거 금지."),
    }


def stance_track_record(store, *, data_dir: Path | str, cfg: dict | None = None,
                        since_days: float = 120.0, now: float | None = None,
                        per_symbol: int = 3, prompt_rev: str | None = None,
                        min_current: int = MIN_N) -> dict[str, Any]:
    """Athena 컨텍스트용 자기 판정 성적 — 시장별 stance 성적 + 종목별 최근 결과.

    prompt_rev: 현재 프롬프트 판본. 그 판본의 bullish 성숙 에피소드가 시장별로
      min_current 이상이면 그 시장 성적은 현재 판본 표본만으로 낸다(basis=current_prompt).
      아니면 최근 since_days 전체(basis=rolling). 옛 판본 성적이 몇 달씩 남아 새 판본을
      계속 '헐겁다'고 몰아 bullish 를 과하게 줄이는 지연 과보정을 막는다.

    반환: {window_days, since_days, by_market: {mkt: {label: 요약}},
           basis: {mkt: current_prompt|rolling}, by_symbol: {sym: [...]}}.
    """
    now = now or datetime.now(timezone.utc).timestamp()
    obs = dossier_observations(store, since=now - since_days * 86400, now=now)
    rows, _ = stance_episodes(obs, data_dir=data_dir, cfg=cfg)
    by_market = summarize_episodes(rows)
    basis = {m: "rolling" for m in by_market}
    if prompt_rev:
        cur_rows, _ = stance_episodes([o for o in obs if o.get("prompt_rev") == prompt_rev],
                                      data_dir=data_dir, cfg=cfg)
        for mkt, labels in summarize_episodes(cur_rows).items():
            if (labels.get("bullish") or {}).get("n", 0) >= min_current:
                by_market[mkt] = labels
                basis[mkt] = "current_prompt"
    by_symbol: dict[str, list] = {}
    for r in sorted(rows, key=lambda x: x["start"], reverse=True):
        lst = by_symbol.setdefault(r["symbol"], [])
        if len(lst) < per_symbol:
            lst.append({"date": r["start"].isoformat(), "stance": r["label"],
                        "excess_pp": r["excess_pp"]})
    return {
        "window_days": horizon_calendar_days("swing", cfg),
        "since_days": since_days,
        "by_market": by_market,
        "basis": basis,
        "by_symbol": by_symbol,
    }


def dossier_observations(store, *, since: float, now: float | None = None,
                         prompt_rev: str | None = None) -> list[dict]:
    """store.dossiers → [{symbol, ts, label=stance}].

    prompt_rev: 주면 evidence.prompt_rev 가 같은 도시에만(프롬프트 변경 전후 분리).
    """
    from .dossier_quality import dossier_stance

    now = now or datetime.now(timezone.utc).timestamp()
    with store._lock:
        hist = store.conn.execute(
            "SELECT symbol, created_at, evidence FROM dossiers "
            "WHERE created_at >= ? AND created_at <= ?", (since, now)).fetchall()
    out = []
    for r in hist:
        row = dict(r)
        if prompt_rev is not None:
            try:
                ev = json.loads(row.get("evidence") or "{}")
            except (TypeError, ValueError):
                ev = {}
            if not isinstance(ev, dict) or ev.get("prompt_rev") != prompt_rev:
                continue
        rev = None
        if prompt_rev is None:
            try:
                ev = json.loads(row.get("evidence") or "{}")
                rev = ev.get("prompt_rev") if isinstance(ev, dict) else None
            except (TypeError, ValueError):
                rev = None
        else:
            rev = prompt_rev
        out.append({"symbol": row["symbol"], "ts": float(row["created_at"]),
                    "label": dossier_stance(row), "prompt_rev": rev})
    return out


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
