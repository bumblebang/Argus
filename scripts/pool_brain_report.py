"""풀·뇌 게이트 측정 — B안 선행조건(G1~G6) 판정 기준 엔진.

설계·게이트 정의: `docs/POOL_BRAIN_B.md`. 관측 전용(읽기만, 승격 판단 아님).

  python scripts/pool_brain_report.py
  python scripts/pool_brain_report.py --days 10 --strict   # 미통과 시 exit 1
  python scripts/pool_brain_report.py --json               # 기계 판독용

G6(보유 도시어 커버리지)는 시점 측정이라 **3 거래일 연속 재실행**으로 판정한다.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.agents import serve_policy as serve  # noqa: E402
from src.config import load_config  # noqa: E402
from src.engine.store import Store  # noqa: E402
from src.market_hours import HOLIDAYS  # noqa: E402
from src.strategy_scores import (  # noqa: E402
    load_strategy_scores, pad_score, strategy_scores_stale,
)

GAP_WAKE_REASONS = ("gap_rebound_scan", "nxt_gap_scan")


def _trading_days(n: int, *, now: float, market: str = "KR") -> list[str]:
    """최근 n 거래일(로컬 날짜 문자열, 과거→현재). 주말·정적 휴장일 제외."""
    hol = HOLIDAYS.get(market, set())
    out: list[str] = []
    d = datetime.fromtimestamp(now)
    while len(out) < n and len(out) < 400:
        iso = d.date().isoformat()
        if d.weekday() < 5 and iso not in hol:
            out.append(iso)
        d -= timedelta(days=1)
    return list(reversed(out))


def _day_of(ts: float) -> str:
    return datetime.fromtimestamp(ts).date().isoformat()


def _pct(part: int, whole: int) -> float:
    return 100.0 * part / whole if whole else 0.0


def _p(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    i = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[i]


def collect(store: Store, cfg, *, days: int, now: float) -> dict:
    tdays = _trading_days(days, now=now)
    since = time.mktime(datetime.fromisoformat(tdays[0]).timetuple())

    # ── brain_serve ────────────────────────────────────────────
    serves: dict[str, list[dict]] = defaultdict(list)
    reasons: Counter = Counter()
    for r in store.recent_events("brain_serve", since, limit=100_000):
        try:
            p = json.loads(r["payload"] or "{}")
        except ValueError:
            continue
        p["_ts"] = r["ts"]
        serves[_day_of(r["ts"])].append(p)
        reasons[p.get("reason")] += 1

    scan_cap = int(serve.serve_cfg(getattr(cfg, "raw", cfg)).get("scan_cap", 40))
    per_day = []
    for d in tdays:
        evs = serves.get(d, [])
        scans = [e for e in evs if e.get("tier") == "scan" and e.get("scan_shortlist")]
        items = [int(e.get("n_items") or 0) for e in scans]
        per_day.append({
            "day": d,
            "n": len(evs),
            "scan": sum(1 for e in evs if e.get("tier") == "scan"),
            "focus": sum(1 for e in evs if e.get("tier") == "focus"),
            "shortlist_n": len(scans),
            "items_min": min(items) if items else None,
            "items_max": max(items) if items else None,
            "at_cap": bool(items) and all(abs(i - scan_cap) <= 2 for i in items),
            "ctx_p95_kb": round(_p([(e.get("context_bytes") or 0) / 1024
                                    for e in evs], 0.95), 1),
            "ss_stale": sum(1 for e in evs if e.get("strategy_scores_stale")),
            "gap_wake": sum(1 for e in evs
                            if e.get("reason") in GAP_WAKE_REASONS),
        })

    # ── 에러 ───────────────────────────────────────────────────
    n_cycle_error = 0
    key_errors = 0
    for r in store.recent_events("error", since, limit=100_000):
        pay = r["payload"] or ""
        if '"where": "tick"' in pay or '"where": "cycle"' in pay:
            n_cycle_error += 1
        if "KeyError" in pay:
            key_errors += 1

    # ── athena_queue 일별 ──────────────────────────────────────
    queue_day: Counter = Counter()
    with store._lock:
        for r in store.conn.execute(
                "SELECT ts FROM events WHERE kind='athena_queue' AND ts>=?",
                (since,)):
            queue_day[_day_of(r["ts"])] += 1

    # ── 샷리스트 must/pad ─────────────────────────────────────
    items = [dict(it, market=it.get("market") or mkt)
             for mkt, rows in (cfg.universe or {}).items()
             for it in (rows or []) if it.get("symbol")]
    held = list(dict.fromkeys(str(r["symbol"])
                              for r in store.get_open_positions()))
    armed = list(dict.fromkeys(str(r["symbol"]) for r in store.get_armed()))
    bullish = store.list_fresh_bullish_symbols()
    fresh_any = {str(r["symbol"]) for r in store.list_fresh_dossiers()}
    stale = strategy_scores_stale()
    scores = {} if stale else load_strategy_scores()
    scfg = serve.serve_cfg(getattr(cfg, "raw", cfg))
    chosen = serve.select_scan_candidates(items, held=held, armed=armed,
                                          bullish=bullish, scores=scores, cfg=scfg)
    n_must = sum(1 for c in chosen if c.get("serve_must"))
    uni_syms = {str(i["symbol"]) for i in items}

    # ── 도시어 만료 절벽 ──────────────────────────────────────
    rows = [dict(r) for r in store.conn.execute(
        "SELECT symbol, market, created_at, expires_at, evidence FROM dossiers"
        " ORDER BY created_at")]
    latest: dict[str, dict] = {str(r["symbol"]): r for r in rows}

    def _stance(r: dict) -> str | None:
        try:
            return (json.loads(r.get("evidence") or "{}") or {}).get("stance")
        except ValueError:
            return None

    cliff = []
    for hrs in (0, 12, 24, 36, 48):
        t = now + hrs * 3600
        fr = [r for r in latest.values() if float(r.get("expires_at") or 0) > t]
        cliff.append({"in_hours": hrs, "fresh": len(fr),
                      "bullish": sum(1 for r in fr if _stance(r) == "bullish")})

    return {
        "now": now,
        "days": tdays,
        "scan_cap": scan_cap,
        "per_day": per_day,
        "reasons": reasons.most_common(10),
        "cycle_error": n_cycle_error,
        "key_error": key_errors,
        "queue_per_day": [{"day": d, "n": queue_day.get(d, 0)} for d in tdays],
        "shortlist": {
            "universe": len(items), "chosen": len(chosen),
            "must": n_must, "pad": len(chosen) - n_must,
            "stub": sum(1 for c in chosen if c.get("serve_stub")),
            "pad_scored": sum(1 for i in items
                              if pad_score(scores or {}, str(i["symbol"]))
                              > float("-inf")),
            "strategy_scores_stale": stale,
        },
        "dossier": {
            "held": len(held), "armed": len(armed),
            "held_fresh": len(set(held) & fresh_any),
            "held_bullish": len(set(held) & set(bullish)),
            "fresh_any": len(fresh_any), "bullish": len(bullish),
            "bullish_in_universe": len(set(bullish) & uni_syms),
            "cliff": cliff,
        },
    }


def gates(m: dict) -> list[dict]:
    pd = m["per_day"]
    days_with_serve = [d for d in pd if d["n"] >= 3]
    ctx_p95 = max((d["ctx_p95_kb"] for d in pd), default=0.0)
    at_cap = [d for d in pd if d["shortlist_n"] and d["at_cap"]]
    qmax = max((q["n"] for q in m["queue_per_day"]), default=0)
    dos = m["dossier"]
    held_cov = _pct(dos["held_fresh"], dos["held"])
    return [
        {"id": "G1", "name": "cycle_error 0 · 거래일마다 brain_serve ≥3",
         "ok": m["cycle_error"] == 0 and len(days_with_serve) == len(pd),
         "detail": f"cycle_error={m['cycle_error']} "
                   f"충족일={len(days_with_serve)}/{len(pd)}"},
        {"id": "G2", "name": f"scan shortlist n_items = cap±2 (cap={m['scan_cap']})",
         "ok": bool(at_cap) and len(at_cap) == sum(1 for d in pd if d["shortlist_n"]),
         "detail": f"충족일={len(at_cap)}/"
                   f"{sum(1 for d in pd if d['shortlist_n'])} (shortlist 있는 날 기준)"},
        {"id": "G3", "name": "context_bytes p95 < 220KB",
         "ok": ctx_p95 < 220.0,
         "detail": f"일별 p95 최대={ctx_p95:.1f}KB"},
        {"id": "G4", "name": "athena_queue 일 건수 < 1,000",
         "ok": qmax < 1000,
         "detail": f"최대={qmax:,}/일"},
        {"id": "G5", "name": "갭 각성 진행 · KeyError 0",
         "ok": m["key_error"] == 0 and any(d["gap_wake"] for d in pd),
         "detail": f"KeyError={m['key_error']} "
                   f"갭각성={sum(d['gap_wake'] for d in pd)}건"},
        {"id": "G6", "name": "보유 도시어 커버리지 ≥80% (3 거래일 연속 재실행 필요)",
         "ok": held_cov >= 80.0,
         "detail": f"{dos['held_fresh']}/{dos['held']} = {held_cov:.0f}% "
                   f"(bullish {dos['held_bullish']})"},
    ]


def render(m: dict, gs: list[dict]) -> None:
    print(f"풀·뇌 게이트 리포트 — {datetime.fromtimestamp(m['now']):%Y-%m-%d %H:%M} "
          f"· 거래일 {m['days'][0]}~{m['days'][-1]}")

    print("\n[brain_serve 일별]")
    print(f"{'day':<12}{'n':>4}{'scan':>6}{'focus':>6}{'short':>6}"
          f"{'items':>12}{'ctx p95':>9}{'gapwake':>8}{'ss_stale':>9}")
    for d in m["per_day"]:
        rng = ("-" if d["items_min"] is None
               else (f"{d['items_min']}" if d["items_min"] == d["items_max"]
                     else f"{d['items_min']}~{d['items_max']}"))
        print(f"{d['day']:<12}{d['n']:>4}{d['scan']:>6}{d['focus']:>6}"
              f"{d['shortlist_n']:>6}{rng:>12}{d['ctx_p95_kb']:>8.0f}K"
              f"{d['gap_wake']:>8}{d['ss_stale']:>9}")
    print(f"reason: {m['reasons']}")

    print("\n[athena_queue 일별]")
    print("  " + "  ".join(f"{q['day'][5:]}={q['n']:,}" for q in m["queue_per_day"]))

    s = m["shortlist"]
    print(f"\n[샷리스트] 유니버스 {s['universe']} → {s['chosen']} "
          f"= must {s['must']} + pad {s['pad']}  (stub {s['stub']})")
    print(f"  pad 점수 유효 {s['pad_scored']}/{s['universe']}"
          f"{'  ※ strategy_scores stale → 유니버스순 폴백' if s['strategy_scores_stale'] else ''}")

    d = m["dossier"]
    print(f"\n[도시어] fresh {d['fresh_any']} · bullish {d['bullish']}"
          f"(유니버스 내 {d['bullish_in_universe']})")
    print(f"  보유 {d['held']} · armed {d['armed']} — "
          f"보유 중 fresh {d['held_fresh']} / bullish {d['held_bullish']}")
    print("  만료 절벽: " + " → ".join(
        f"+{c['in_hours']}h {c['fresh']}/{c['bullish']}" for c in d["cliff"])
        + "  (fresh/bullish)")

    print("\n[게이트]")
    for g in gs:
        print(f"  {'PASS' if g['ok'] else 'FAIL'}  {g['id']} {g['name']}")
        print(f"           {g['detail']}")
    n_ok = sum(1 for g in gs if g["ok"])
    print(f"\n{n_ok}/{len(gs)} 통과. G1~G5 전부 통과 전 B1 착수 금지 "
          f"(docs/POOL_BRAIN_B.md §D3).")


def main() -> int:
    ap = argparse.ArgumentParser(description="풀·뇌 B안 게이트 측정")
    ap.add_argument("--db", default=None, help="기본 config 경로")
    ap.add_argument("--days", type=int, default=7, help="관측 거래일 수(기본 7)")
    ap.add_argument("--json", action="store_true", help="JSON 출력")
    ap.add_argument("--strict", action="store_true", help="미통과 시 exit 1")
    args = ap.parse_args()

    cfg = load_config()
    store = (Store(args.db, readonly=True) if args.db
             else Store(ROOT / "data" / "state" / "bot.db", readonly=True))
    try:
        m = collect(store, cfg, days=max(1, args.days), now=time.time())
    finally:
        store.conn.close()
    gs = gates(m)
    if args.json:
        print(json.dumps({"metrics": m, "gates": gs}, ensure_ascii=False, indent=2))
    else:
        render(m, gs)
    return 1 if args.strict and not all(g["ok"] for g in gs) else 0


if __name__ == "__main__":
    raise SystemExit(main())
