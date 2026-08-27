"""KR/US 원장(book) + ₩ 환산 총자산 파생.

account_snapshot 의 cash/MV/purchase/pnl 을 시장별 book 으로 묶고,
USDKRW 가 있으면 총자산을 원화로 합산한다. FX 는 참고용(환전 모델 아님).
"""
from __future__ import annotations

from typing import Any


_CCY = {"KR": "KRW", "US": "USD"}
_MARKETS = ("KR", "US")


def _f(v: Any, default: float | None = None) -> float | None:
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _rate(numer: float | None, denom: float | None) -> float | None:
    if numer is None or denom is None or denom <= 0:
        return None
    return float(numer) / float(denom)


def book_rates(snap: dict) -> tuple[dict[str, float], dict[str, float]]:
    """원장 자체 수익률: pnl/purchase, daily_pnl/equity(cash+mv)."""
    cash = snap.get("cash") or {}
    mv = snap.get("market_value") or {}
    purchase = snap.get("total_purchase") or {}
    profit = snap.get("profit") or {}
    daily = snap.get("daily_profit") or {}
    pnl_rate: dict[str, float] = {}
    daily_rate: dict[str, float] = {}
    markets = set(cash) | set(mv) | set(purchase) | set(profit) | set(daily)
    for mk in markets:
        m = str(mk).upper()
        pn = _f(profit.get(mk))
        pu = _f(purchase.get(mk))
        r = _rate(pn, pu)
        if r is not None:
            pnl_rate[m] = r
        c = _f(cash.get(mk), 0.0) or 0.0
        v = _f(mv.get(mk), 0.0) or 0.0
        eq = c + v
        dr = _rate(_f(daily.get(mk)), eq if eq > 0 else None)
        if dr is not None:
            daily_rate[m] = dr
    return pnl_rate, daily_rate


def build_books(
    snap: dict,
    fx_usdkrw: float | None = None,
    fx_ts: float | None = None,
) -> dict:
    """snap 원본 필드로 books/totals/fx 파생 dict 반환(원본은 수정하지 않음)."""
    cash = snap.get("cash") or {}
    mv = snap.get("market_value") or {}
    purchase = snap.get("total_purchase") or {}
    profit = snap.get("profit") or {}
    daily = snap.get("daily_profit") or {}
    pnl_rate, daily_rate = book_rates(snap)

    fx_ok = fx_usdkrw is not None and float(fx_usdkrw) > 0
    fx_val = float(fx_usdkrw) if fx_ok else None

    books: dict[str, dict] = {}
    for m in _MARKETS:
        c = _f(cash.get(m), 0.0) or 0.0
        v = _f(mv.get(m), 0.0) or 0.0
        pu = _f(purchase.get(m), 0.0) or 0.0
        # 원장에 흔적이 없으면 스킵(대시에서 빈 US 카드 강제 안 함 — apply 쪽에서 채움 가능)
        if m not in cash and m not in mv and m not in purchase and m not in profit and m not in daily:
            continue
        equity = c + v
        book: dict[str, Any] = {
            "cash": c,
            "mv": v,
            "purchase": pu,
            "equity": equity,
            "pnl": _f(profit.get(m)),
            "pnl_rate": pnl_rate.get(m),
            "daily_pnl": _f(daily.get(m)),
            "daily_pnl_rate": daily_rate.get(m),
            "ccy": _CCY[m],
        }
        if m == "US" and fx_ok:
            book["equity_krw"] = equity * fx_val  # type: ignore[operator]
            if book["pnl"] is not None:
                book["pnl_krw"] = float(book["pnl"]) * fx_val  # type: ignore[operator]
            if book["daily_pnl"] is not None:
                book["daily_pnl_krw"] = float(book["daily_pnl"]) * fx_val  # type: ignore[operator]
        elif m == "KR":
            book["equity_krw"] = equity
            if book["pnl"] is not None:
                book["pnl_krw"] = float(book["pnl"])
            if book["daily_pnl"] is not None:
                book["daily_pnl_krw"] = float(book["daily_pnl"])
        books[m] = book

    totals: dict[str, Any] = {
        "equity_krw": None,
        "pnl_krw": None,
        "daily_pnl_krw": None,
        "fx_note": "USDKRW estimate" if fx_ok else "FX unavailable",
    }
    if books:
        # KR 은 항상 원화면 합산. US 는 FX 있을 때만.
        eq_parts: list[float] = []
        pnl_parts: list[float] = []
        daily_parts: list[float] = []
        can_total = True
        for m, b in books.items():
            if m == "US" and not fx_ok:
                can_total = False
                continue
            if b.get("equity_krw") is not None:
                eq_parts.append(float(b["equity_krw"]))
            if b.get("pnl_krw") is not None:
                pnl_parts.append(float(b["pnl_krw"]))
            if b.get("daily_pnl_krw") is not None:
                daily_parts.append(float(b["daily_pnl_krw"]))
        if can_total and eq_parts:
            totals["equity_krw"] = sum(eq_parts)
            totals["pnl_krw"] = sum(pnl_parts) if pnl_parts else None
            totals["daily_pnl_krw"] = sum(daily_parts) if daily_parts else None
        elif not can_total:
            # US 있는데 FX 없음 → 합산 null (KR-only 부분합도 숨김 — 오해 방지)
            totals["equity_krw"] = None
            totals["pnl_krw"] = None
            totals["daily_pnl_krw"] = None

    out: dict[str, Any] = {"books": books, "totals": totals}
    if fx_ok:
        out["fx"] = {"USDKRW": fx_val, "ts": fx_ts}
    else:
        out["fx"] = {"USDKRW": None, "ts": fx_ts}
    return out


def apply_books(
    snap: dict,
    fx_usdkrw: float | None = None,
    fx_ts: float | None = None,
    *,
    ensure_markets: tuple[str, ...] = (),
) -> dict:
    """snap 에 books/totals/fx 를 붙이고, 원장 rate 키를 book 기준으로 덮어쓴다."""
    pnl_rate, daily_rate = book_rates(snap)
    snap["profit_rate"] = pnl_rate
    snap["daily_profit_rate"] = daily_rate
    derived = build_books(snap, fx_usdkrw=fx_usdkrw, fx_ts=fx_ts)
    # ensure_markets: cash 키만 있어도 빈 book 생성(대시 2열)
    books = derived["books"]
    cash = snap.get("cash") or {}
    for m in ensure_markets:
        mu = str(m).upper()
        if mu in books:
            continue
        if mu not in cash and mu not in (snap.get("market_value") or {}):
            # 관제 대상 시장이면 0 원장
            c = _f(cash.get(mu), 0.0) or 0.0
            books[mu] = {
                "cash": c, "mv": 0.0, "purchase": 0.0, "equity": c,
                "pnl": None, "pnl_rate": None, "daily_pnl": None,
                "daily_pnl_rate": None, "ccy": _CCY.get(mu, mu),
                "equity_krw": c if mu == "KR" else (
                    c * float(fx_usdkrw) if fx_usdkrw and float(fx_usdkrw) > 0 else None),
            }
    snap["books"] = books
    snap["totals"] = derived["totals"]
    snap["fx"] = derived["fx"]
    return snap


def read_fx_usdkrw(market_state: dict | None) -> tuple[float | None, float | None]:
    """market_state.json 형태에서 USDKRW·ts 추출."""
    if not isinstance(market_state, dict):
        return None, None
    fx = market_state.get("fx") or {}
    if not isinstance(fx, dict):
        return None, None
    rate = _f(fx.get("USDKRW"))
    ts = _f(fx.get("ts") or market_state.get("ts"))
    if rate is None or rate <= 0:
        return None, ts
    return rate, ts
