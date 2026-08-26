"""지정가·재대사·프리/애프터 체결 실측 요약 (대시보드 Ops/오늘).

events 테이블 payload 만 읽는다. 라이브 store 쓰기는 없음.
"""
from __future__ import annotations

from typing import Any

from .market_hours import current_session

_SESSION_KEYS = ("premarket", "regular", "aftermarket", "daymarket", "closed")
_SESSION_LABEL = {
    "premarket": "프리",
    "regular": "정규",
    "aftermarket": "애프터",
    "daymarket": "주간",
    "closed": "휴장",
    "unknown": "?",
}


def _num(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _payload(row: dict) -> dict:
    p = row.get("payload")
    if isinstance(p, dict):
        return p
    if isinstance(p, str) and p.strip():
        try:
            import json
            d = json.loads(p)
            return d if isinstance(d, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}


def slip_pct(side: str | None, limit: float | None, fill: float | None) -> float | None:
    """BUY: (fill-limit)/limit, SELL: (limit-fill)/limit. + = 불리."""
    if limit is None or fill is None or limit <= 0:
        return None
    side_u = (side or "").upper()
    if side_u == "BUY":
        return (fill - limit) / limit * 100.0
    if side_u == "SELL":
        return (limit - fill) / limit * 100.0
    return abs(fill - limit) / limit * 100.0


def summarize_exec(rows: list[dict], *, market: str = "KR",
                   now: float | None = None) -> dict[str, Any]:
    """오늘(또는 전달 rows) 실측 요약.

    rows: {ts, kind, symbol, payload} — payload dict 또는 JSON 문자열.
    """
    del now
    fills: list[dict] = []
    pending = 0
    errors = 0
    spread_skips = 0
    sell_skips = 0
    by_sess = {k: 0 for k in _SESSION_KEYS}
    by_sess["unknown"] = 0
    slips: list[float] = []
    recon = {
        "n": 0, "adopted": 0, "closed": 0, "updated": 0,
        "noisy": 0, "last_ts": None, "last": None,
    }

    for row in rows:
        kind = str(row.get("kind") or "")
        ts = float(row.get("ts") or 0)
        pl = _payload(row)

        if kind == "live_order":
            limit = _num(pl.get("limit_price"))
            fill = _num(pl.get("price"))
            side = pl.get("side")
            slip = slip_pct(str(side) if side else None, limit, fill)
            if slip is not None:
                slips.append(slip)
            try:
                sess = current_session(market, now=ts)
            except Exception:
                sess = "unknown"
            if sess not in by_sess:
                sess = "unknown"
            by_sess[sess] = by_sess.get(sess, 0) + 1
            fills.append({
                "ts": ts,
                "symbol": row.get("symbol") or pl.get("symbol"),
                "side": side,
                "qty": pl.get("qty"),
                "limit": limit,
                "fill": fill,
                "slip_pct": round(slip, 4) if slip is not None else None,
                "session": sess,
                "status": pl.get("status"),
            })
        elif kind == "live_order_pending":
            pending += 1
        elif kind == "live_order_error":
            errors += 1
        elif kind == "wide_spread_skip":
            spread_skips += 1
        elif kind == "sell_skipped":
            sell_skips += 1
        elif kind == "reconcile":
            recon["n"] += 1
            adopted = pl.get("adopted") or []
            closed = pl.get("closed") or []
            updated = pl.get("updated") or []
            n_a = len(adopted) if isinstance(adopted, list) else int(adopted or 0)
            n_c = len(closed) if isinstance(closed, list) else int(closed or 0)
            n_u = len(updated) if isinstance(updated, list) else int(updated or 0)
            recon["adopted"] += n_a
            recon["closed"] += n_c
            recon["updated"] += n_u
            if n_a or n_c:
                recon["noisy"] += 1
            recon["last_ts"] = ts
            recon["last"] = {
                "adopted": n_a, "closed": n_c, "updated": n_u,
                "holdings": pl.get("holdings"),
                "error": pl.get("error"),
            }

    avg_slip = (sum(slips) / len(slips)) if slips else None
    worst = max(slips) if slips else None
    # 건강: 재대사 noisy 비율, pending/error
    recon_health = "quiet"
    if recon["n"] == 0:
        recon_health = "none"
    elif recon["noisy"] >= 3 or (recon["n"] and recon["noisy"] / recon["n"] >= 0.3):
        recon_health = "noisy"
    elif recon["noisy"] > 0:
        recon_health = "drift"

    line_parts = [
        f"체결 {len(fills)}",
        f"미체결 {pending}",
        f"실패 {errors}",
    ]
    if slips:
        line_parts.append(f"슬립평균 {avg_slip:+.2f}%")
    sess_bits = []
    for k in ("premarket", "regular", "aftermarket"):
        if by_sess.get(k):
            sess_bits.append(f"{_SESSION_LABEL[k]} {by_sess[k]}")
    if sess_bits:
        line_parts.append("·".join(sess_bits))
    if recon["n"]:
        line_parts.append(
            f"재대사 {recon['n']}회"
            + (f"(채택{recon['adopted']}/청산{recon['closed']})"
               if recon["adopted"] or recon["closed"] else "(조용)")
        )
    if spread_skips:
        line_parts.append(f"스프레드스킵 {spread_skips}")

    return {
        "n_fills": len(fills),
        "n_pending": pending,
        "n_errors": errors,
        "n_spread_skip": spread_skips,
        "n_sell_skip": sell_skips,
        "by_session": by_sess,
        "avg_slip_pct": round(avg_slip, 3) if avg_slip is not None else None,
        "worst_slip_pct": round(worst, 3) if worst is not None else None,
        "reconcile": recon,
        "reconcile_health": recon_health,
        "fills_preview": fills[:8],
        "line": " · ".join(line_parts),
        "session_labels": _SESSION_LABEL,
    }
