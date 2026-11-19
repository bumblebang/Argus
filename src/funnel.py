"""매수 퍼널·현금 활용률 계측 — 현금을 왜 못 쓰는지(어느 단계에서 막히는지) 본다.

읽기 전용 집계. 새 기록을 만들지 않고 이미 쌓인 원천만 읽는다.
  - 뇌 트랙: events(wake·brain_skip·cycle) + decisions(판단·검증 판정).
      각성 → (쿨다운 등 스킵) → 사이클 완주 → 종목 판단 → BUY 제안 → 검증 통과
      → 집행 결과(cycle.executed status: filled/pending/vetoed/market_unknown/gap_armed…)
  - 밸류 트랙: data/value_funnel.jsonl(선별 → 비쌈/마진/타이밍 탈락 → 게이트 통과)
      + events(value_done) 의 시장별 proposed/filled/vetoed.
  - 현금 활용률: 일별 장 마감 시점 보유 원가(qty × 평단) ÷ 시장 자본(capital_sync).
      원가 기준이라 평가손익은 반영하지 않는다(자금이 '묶인' 정도를 본다).
"""
from __future__ import annotations

import json
import re
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

_KST = timezone(timedelta(hours=9))
_VALUE_DONE_RE = re.compile(
    r"'(KR|US)': \{'candidates': (\d+), 'gated': (\d+), 'proposed': (\d+), "
    r"'filled': (\d+), 'vetoed': (\d+)")
_VALUE_DROP_KEYS = ("too_expensive", "margin_fail", "timing_fail")


def _rows(store, sql: str, args: tuple) -> list:
    lock = getattr(store, "_lock", None)
    if lock is None:
        return store.conn.execute(sql, args).fetchall()
    with lock:
        return store.conn.execute(sql, args).fetchall()


def _json(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        v = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


# ── 뇌 트랙 ────────────────────────────────────────────────
def brain_funnel(store, since: float, until: float | None = None) -> dict:
    until = until if until is not None else time.time()
    span = (since, until)

    def count(kind: str) -> int:
        return int(_rows(store, "SELECT COUNT(*) FROM events WHERE kind=? AND ts>=? AND ts<?",
                         (kind, *span))[0][0])

    skips = Counter()
    for (raw,) in _rows(store, "SELECT payload FROM events WHERE kind='brain_skip'"
                        " AND ts>=? AND ts<?", span):
        skips[str(_json(raw).get("reason") or "?")] += 1

    verdicts = Counter()
    n_dec = 0
    syms: set[str] = set()
    for sym, action, verdict in _rows(
            store, "SELECT symbol, action, verdict FROM decisions WHERE ts>=? AND ts<?", span):
        n_dec += 1
        syms.add(str(sym))
        verdicts[(str(action or "").upper(), str(verdict or ""))] += 1

    executed = Counter()
    vetoed_examples: list[dict] = []
    for ts, raw in _rows(store, "SELECT ts, payload FROM events WHERE kind='cycle'"
                         " AND ts>=? AND ts<? ORDER BY ts", span):
        for e in _json(raw).get("executed") or []:
            if not isinstance(e, dict) or str(e.get("action") or "").upper() != "BUY":
                continue
            st = str(e.get("status") or "?")
            executed[st] += 1
            if st != "filled":
                vetoed_examples.append({"ts": ts, "symbol": e.get("symbol"), "status": st,
                                        "reason": str(e.get("reason") or "")[:160]})

    buy = {v: n for (a, v), n in verdicts.items() if a == "BUY"}
    return {
        "wakes": count("wake"),
        "skips": dict(skips),
        "cycles": count("cycle"),
        "decisions": n_dec,
        "symbols": len(syms),
        "holds": sum(n for (a, _), n in verdicts.items() if a == "HOLD"),
        "sells": sum(n for (a, _), n in verdicts.items() if a == "SELL"),
        "buy_proposed": sum(buy.values()),
        "buy_approved": int(buy.get("approved", 0)),
        "buy_vetoed": int(buy.get("vetoed", 0)),
        "buy_executed": dict(executed),
        "buy_filled": int(executed.get("filled", 0)),
        "not_filled_recent": vetoed_examples[-5:],
    }


# ── 밸류 트랙 ──────────────────────────────────────────────
def value_funnel(store, since: float, until: float | None = None,
                 funnel_path: Path | None = None) -> dict:
    until = until if until is not None else time.time()
    out: dict[str, dict] = {}

    def mk(m: str) -> dict:
        return out.setdefault(m, {"runs": 0, "selected": 0, **{k: 0 for k in _VALUE_DROP_KEYS},
                                  "gated": 0, "proposed": 0, "filled": 0, "vetoed": 0})

    if funnel_path is None:
        from .value_ops import FUNNEL_PATH
        funnel_path = FUNNEL_PATH
    try:
        lines = Path(funnel_path).read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for ln in lines:
        ev = _json(ln)
        ts = ev.get("ts")
        if not isinstance(ts, (int, float)) or not (since <= ts < until):
            continue
        d = mk(str(ev.get("market") or "?"))
        d["runs"] += 1
        for k in ("selected", *_VALUE_DROP_KEYS, "gated"):
            d[k] += int(ev.get(k) or 0)

    for (raw,) in _rows(store, "SELECT payload FROM events WHERE kind='value_done'"
                        " AND ts>=? AND ts<?", (since, until)):
        result = str((_json(raw).get("summary") or {}).get("result") or "")
        for m, _c, _g, prop, fill, veto in _VALUE_DONE_RE.findall(result):
            d = mk(m)
            d["proposed"] += int(prop)
            d["filled"] += int(fill)
            d["vetoed"] += int(veto)
    return out


# ── 현금 활용률 ────────────────────────────────────────────
def _capital_timeline(store, cfg_capital: dict) -> list[tuple[float, dict]]:
    tl: list[tuple[float, dict]] = [(0.0, {k: float(v) for k, v in (cfg_capital or {}).items()})]
    for ts, raw in _rows(store, "SELECT ts, payload FROM events WHERE kind='capital_sync'"
                         " ORDER BY ts", ()):
        cap = _json(raw).get("capital")
        if isinstance(cap, dict):
            merged = dict(tl[-1][1])
            for k, v in cap.items():
                try:
                    merged[str(k)] = float(v)
                except (TypeError, ValueError):
                    pass
            tl.append((float(ts), merged))
    return tl


def _capital_at(tl: list[tuple[float, dict]], ts: float) -> dict:
    cur = tl[0][1]
    for t, cap in tl:
        if t > ts:
            break
        cur = cap
    return cur


def cash_utilization(store, days: int = 30, *, cfg_capital: dict | None = None,
                     now: float | None = None, markets: Iterable[str] = ("KR", "US")) -> list[dict]:
    """일별(KST 자정 기준 그날 끝) 시장별 보유 원가 ÷ 자본. 최근 날짜가 마지막."""
    now = now if now is not None else time.time()
    tl = _capital_timeline(store, cfg_capital or {})
    pos = _rows(store, "SELECT market, qty, avg_price, opened_at, closed_at, strategy"
                " FROM positions WHERE opened_at IS NOT NULL", ())
    today = datetime.fromtimestamp(now, tz=_KST).date()
    out = []
    for i in range(days - 1, -1, -1):
        day = today - timedelta(days=i)
        end = datetime(day.year, day.month, day.day, tzinfo=_KST).timestamp() + 86400
        at = min(end, now)
        cap = _capital_at(tl, at)
        row: dict[str, Any] = {"date": day.isoformat()}
        for m in markets:
            cost = 0.0
            value_cost = 0.0
            n = 0
            for mk, qty, avg, opened, closed, strat in pos:
                if str(mk) != m or opened is None or float(opened) > at:
                    continue
                if closed is not None and float(closed) <= at:
                    continue
                c = float(qty or 0) * float(avg or 0)
                cost += c
                n += 1
                if str(strat or "").lower() == "value":
                    value_cost += c
            capital = float(cap.get(m) or 0)
            row[m] = {"cost": round(cost, 2), "value_cost": round(value_cost, 2),
                      "positions": n, "capital": capital,
                      "util": round(cost / capital, 4) if capital > 0 else None}
        out.append(row)
    return out


# ── 사람이 읽는 요약 ────────────────────────────────────────
def _pct(a: float, b: float) -> str:
    return f"{a / b * 100:.0f}%" if b else "–"


def format_report(brain: dict, value: dict, util: list[dict], days: int) -> str:
    b = brain
    lines = [f"## 매수 퍼널 (최근 {days}일)", "", "### 뇌 트랙"]
    skips = ", ".join(f"{k} {v}" for k, v in sorted(b["skips"].items(), key=lambda x: -x[1]))
    lines.append(f"- 각성 {b['wakes']} → 스킵 {sum(b['skips'].values())}"
                 f"{f' ({skips})' if skips else ''} → 사이클 완주 {b['cycles']}")
    lines.append(f"- 종목 판단 {b['decisions']}건({b['symbols']}종목): 유지 {b['holds']} · "
                 f"매도 {b['sells']} · 매수 제안 {b['buy_proposed']}"
                 f" ({_pct(b['buy_proposed'], b['decisions'])})")
    lines.append(f"- 매수 제안 {b['buy_proposed']} → 검증 통과 {b['buy_approved']}"
                 f" ({_pct(b['buy_approved'], b['buy_proposed'])}) · 거부 {b['buy_vetoed']}")
    ex = ", ".join(f"{k} {v}" for k, v in sorted(b["buy_executed"].items(), key=lambda x: -x[1]))
    lines.append(f"- 집행 결과: {ex or '없음'} → 체결 {b['buy_filled']}")
    lines += ["", "### 밸류 트랙"]
    if not value:
        lines.append("- 기록 없음")
    for m, d in sorted(value.items()):
        lines.append(f"- {m}: 실행 {d['runs']}회 · 선별 {d['selected']} → 비쌈 {d['too_expensive']}"
                     f" · 마진 미달 {d['margin_fail']} · 타이밍 미달 {d['timing_fail']}"
                     f" → 게이트 통과 {d['gated']} → 제안 {d['proposed']}"
                     f" → 체결 {d['filled']} (거부 {d['vetoed']})")
    lines += ["", "### 현금 활용률 (보유 원가 ÷ 자본, 일말 기준)"]
    if util:
        mkts = [k for k in util[-1] if k != "date"]
        for m in mkts:
            vals = [r[m]["util"] for r in util if r[m]["util"] is not None]
            last = util[-1][m]
            avg = sum(vals) / len(vals) if vals else None
            lines.append(
                f"- {m}: 오늘 {last['util'] * 100:.0f}%" if last["util"] is not None
                else f"- {m}: 자본 미상")
            if avg is not None:
                lines[-1] += (f" (보유 {last['positions']}종목, 밸류 원가 비중 "
                              f"{_pct(last['value_cost'], last['cost'])}) · {len(vals)}일 평균 "
                              f"{avg * 100:.0f}% · 최고 {max(vals) * 100:.0f}%")
    return "\n".join(lines)


def build(store, days: int = 30, *, cfg_capital: dict | None = None,
          now: float | None = None, funnel_path: Path | None = None) -> dict:
    now = now if now is not None else time.time()
    since = now - days * 86400
    return {
        "brain": brain_funnel(store, since, now),
        "value": value_funnel(store, since, now, funnel_path=funnel_path),
        "util": cash_utilization(store, days, cfg_capital=cfg_capital, now=now),
    }
