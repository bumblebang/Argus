"""Gate-blocked BUY postmortem + counterfactual win rates (one-shot)."""
from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUT = DATA / "gate_postmortem.json"
KST = timezone(timedelta(hours=9))

BLOCK_STATUSES = {
    "vetoed", "gate_rejected", "gap_rejected", "no_dossier", "no_price",
    "arm_skipped", "buy_blocked",
}
SOFT_MISS = {"gap_armed"}  # waiting, never filled as of journal


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return rows


def parse_ts(ts) -> float | None:
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return float(ts)
    s = str(ts)
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def reason_bucket(st: str, reason: str, concerns: list) -> str:
    r = reason or ""
    c = ",".join(concerns or [])
    if st == "vetoed":
        if "rule6:conviction" in c or "확신도 미달" in r:
            return "검증:확신도미달"
        if "rule" in c or "규칙" in r:
            return "검증:규칙거부"
        return "검증:LLM거부"
    if st == "gate_rejected":
        if any(k in r for k in ("경보", "관리", "위험", "주의", "blocked")):
            return "게이트:경보차단"
        if any(k in r for k in ("보유", "익스포저", "비중", "여력", "주문금액", "드로다운", "손실")):
            return "게이트:자본/한도"
        if "HALT" in r or "킬" in r:
            return "게이트:HALT"
        return "게이트:기타"
    if st == "gap_rejected":
        return "갭:무효화/존이탈"
    if st == "gap_armed":
        return "갭:대기(미체결)"
    if st == "buy_blocked":
        return "브로커:매수가드"
    if st in ("no_dossier", "no_price", "arm_skipped"):
        return f"코드:{st}"
    return st


def collect(rows: list[dict], sleeve: str) -> tuple[list[dict], int]:
    out: list[dict] = []
    n_prop = 0
    for r in rows:
        props = {p["symbol"]: p for p in (r.get("proposals") or [])
                 if p.get("side") == "BUY"}
        n_prop += len(props)
        verd = {v["symbol"]: v for v in (r.get("verdicts") or [])}
        for e in r.get("executed") or []:
            act = (e.get("action") or e.get("side") or "").upper()
            if act != "BUY":
                continue
            st = e.get("status") or "?"
            v = verd.get(e.get("symbol") or {}, {})
            reason = e.get("reason") or ""
            if st == "vetoed" and v.get("reason"):
                reason = v["reason"]
            p = props.get(e.get("symbol") or {}, {})
            out.append({
                "sleeve": sleeve,
                "ts": r.get("ts"),
                "ts_epoch": parse_ts(r.get("ts")),
                "symbol": e.get("symbol"),
                "status": st,
                "bucket": reason_bucket(st, reason, v.get("concerns") or []),
                "reason": reason,
                "concerns": v.get("concerns") or [],
                "conviction": p.get("conviction"),
                "horizon": p.get("horizon"),
                "thesis": (p.get("thesis") or "")[:240],
                "market": p.get("market") or "KR",
            })
    return out, n_prop


def load_daily(symbol: str) -> list[tuple[datetime, float]]:
    """Load 1d close series from history CSV if present."""
    candidates = sorted(DATA.glob(f"history/{symbol}.KS_1d_*.csv"))
    if not candidates:
        candidates = sorted(DATA.glob(f"history/{symbol}_1d_*.csv"))
    if not candidates:
        return []
    # prefer 1y or longest
    path = candidates[-1]
    rows: list[tuple[datetime, float]] = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        if i == 0 and ("Date" in line or "date" in line):
            continue
        parts = line.split(",")
        if len(parts) < 5:
            continue
        try:
            d = datetime.fromisoformat(parts[0][:10]).replace(tzinfo=KST)
            close = float(parts[4] if len(parts) > 5 else parts[-1])
            # typical OHLCV: Date,Open,High,Low,Close,Volume
            if len(parts) >= 6:
                close = float(parts[4])
            rows.append((d, close))
        except ValueError:
            continue
    return rows


def snap_price(con: sqlite3.Connection, symbol: str, ts: float,
               window: float = 3600) -> float | None:
    row = con.execute(
        "SELECT price FROM snapshots WHERE symbol=? AND ts BETWEEN ? AND ? "
        "ORDER BY ABS(ts-?) LIMIT 1",
        (symbol, ts - window, ts + window, ts),
    ).fetchone()
    return float(row[0]) if row else None


def snap_forward(con: sqlite3.Connection, symbol: str, ts0: float,
                 days: float) -> float | None:
    target = ts0 + days * 86400
    row = con.execute(
        "SELECT price FROM snapshots WHERE symbol=? AND ts BETWEEN ? AND ? "
        "ORDER BY ABS(ts-?) LIMIT 1",
        (symbol, target - 7200, target + 86400 * 2, target),
    ).fetchone()
    return float(row[0]) if row else None


def daily_forward(series: list[tuple[datetime, float]], ts_epoch: float,
                  days: int) -> tuple[float | None, float | None]:
    if not series or ts_epoch is None:
        return None, None
    t0 = datetime.fromtimestamp(ts_epoch, tz=KST)
    # entry = last close on or before day
    entry = None
    for d, c in series:
        if d.date() <= t0.date():
            entry = c
        else:
            break
    if entry is None:
        return None, None
    # forward close N trading days later
    after = [c for d, c in series if d.date() > t0.date()]
    # 부족한 구간은 결측 — 짧은 히스토리로 d20을 마지막봉에 끼워 넣지 않음
    if len(after) < days:
        return entry, None
    return entry, after[days - 1]


def main() -> None:
    brain, n_b = collect(load_jsonl(DATA / "decisions.jsonl"), "brain")
    value, n_v = collect(load_jsonl(DATA / "value_decisions.jsonl"), "value")
    all_exec = brain + value

    con = sqlite3.connect(str(DATA / "bot.db"))
    for row in con.execute(
            "SELECT ts, symbol, payload FROM events WHERE kind='buy_blocked'"):
        pay = json.loads(row[2] or "{}")
        all_exec.append({
            "sleeve": "broker",
            "ts": row[0],
            "ts_epoch": float(row[0]),
            "symbol": row[1],
            "status": "buy_blocked",
            "bucket": reason_bucket("buy_blocked", pay.get("reason") or "", []),
            "reason": pay.get("reason") or "",
            "concerns": [],
            "conviction": None,
            "horizon": None,
            "thesis": "",
            "market": "KR",
        })

    blocked = [x for x in all_exec if x["status"] in BLOCK_STATUSES | SOFT_MISS]
    filled = [x for x in all_exec if x["status"] in ("filled", "armed")]

    # closed positions (actual)
    closed = list(con.execute(
        "SELECT symbol, avg_price, exit_price, pnl, exit_reason, opened_at, "
        "closed_at, strategy, thesis FROM positions WHERE state='closed'"))
    actual_wins = sum(1 for r in closed if (r[3] or 0) > 0)

    # counterfactual on blocked
    daily_cache: dict[str, list] = {}
    horizons = (1, 5, 20)
    cf_rows = []
    for x in blocked:
        if x["status"] == "buy_blocked" and "ETF" in (x.get("reason") or ""):
            continue  # not a stock buy signal
        sym = x["symbol"]
        if not sym or x["ts_epoch"] is None:
            continue
        if sym not in daily_cache:
            daily_cache[sym] = load_daily(sym)
        series = daily_cache[sym]
        entry_px = None
        fwd = {}
        if series:
            for h in horizons:
                e, f = daily_forward(series, x["ts_epoch"], h)
                entry_px = e
                if e and f:
                    fwd[f"d{h}"] = round((f / e - 1) * 100, 3)
        else:
            entry_px = snap_price(con, sym, x["ts_epoch"])
            if entry_px:
                for h in horizons:
                    f = snap_forward(con, sym, x["ts_epoch"], float(h))
                    if f:
                        fwd[f"d{h}"] = round((f / entry_px - 1) * 100, 3)
        if not fwd:
            continue
        cf_rows.append({
            **{k: x[k] for k in ("sleeve", "symbol", "status", "bucket",
                                 "conviction", "horizon", "ts")},
            "entry_px": entry_px,
            "rets": fwd,
            "thesis": x["thesis"][:120],
            "reason": (x["reason"] or "")[:120],
        })

    def cf_stats(rows: list[dict], key: str) -> dict:
        vals = [r["rets"][key] for r in rows if key in r["rets"]]
        if not vals:
            return {"n": 0}
        wins = sum(1 for v in vals if v > 0)
        return {
            "n": len(vals),
            "win_rate": round(wins / len(vals), 3),
            "avg_ret_pct": round(sum(vals) / len(vals), 3),
            "med_ret_pct": round(sorted(vals)[len(vals) // 2], 3),
        }

    by_bucket = Counter(x["bucket"] for x in blocked)
    by_status = Counter(x["status"] for x in all_exec)

    # samples per bucket (thesis why + block why)
    samples = defaultdict(list)
    for x in blocked:
        if len(samples[x["bucket"]]) >= 5:
            continue
        samples[x["bucket"]].append({
            "ts": x["ts"], "symbol": x["symbol"], "sleeve": x["sleeve"],
            "conviction": x["conviction"], "horizon": x["horizon"],
            "why_buy": x["thesis"],
            "why_block": (x["reason"] or "")[:160],
            "concerns": x["concerns"],
        })

    # cf by bucket
    cf_by_bucket = {}
    for b in sorted(set(r["bucket"] for r in cf_rows)):
        sub = [r for r in cf_rows if r["bucket"] == b]
        cf_by_bucket[b] = {f"d{h}": cf_stats(sub, f"d{h}") for h in horizons}

    report = {
        "asof": datetime.now(tz=KST).isoformat(),
        "source": "decisions.jsonl + value_decisions.jsonl + events.buy_blocked",
        "proposals": {"brain_buy": n_b, "value_buy": n_v},
        "executed_status": dict(by_status),
        "blocked_buckets": dict(by_bucket.most_common()),
        "actual_closed": {
            "n": len(closed),
            "wins": actual_wins,
            "win_rate": round(actual_wins / len(closed), 3) if closed else None,
            "rows": [
                {"symbol": r[0], "pnl": r[3], "exit_reason": r[4],
                 "strategy": r[7], "avg": r[1], "exit": r[2]}
                for r in closed
            ],
        },
        "counterfactual": {
            "definition": "blocked BUY entry=prior daily close; "
                          "win=forward close > entry; horizons=1/5/20 trading days",
            "n_with_price": len(cf_rows),
            "overall": {f"d{h}": cf_stats(cf_rows, f"d{h}") for h in horizons},
            "by_bucket": cf_by_bucket,
            "by_sleeve": {
                s: {f"d{h}": cf_stats([r for r in cf_rows if r["sleeve"] == s], f"d{h}")
                    for h in horizons}
                for s in ("brain", "value")
            },
        },
        "samples_by_bucket": dict(samples),
        "cf_examples": sorted(
            cf_rows,
            key=lambda r: r["rets"].get("d5", -999),
            reverse=True,
        )[:12] + sorted(
            cf_rows,
            key=lambda r: r["rets"].get("d5", 999),
        )[:8],
    }
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "wrote": str(OUT),
        "blocked": len(blocked),
        "cf_n": len(cf_rows),
        "buckets": report["blocked_buckets"],
        "cf_overall": report["counterfactual"]["overall"],
        "actual": report["actual_closed"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
