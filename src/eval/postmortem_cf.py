"""게이트 반사실(CF) 수익 — 비용·보수적 손절 (J10 defer)."""
from __future__ import annotations

DEFAULT_CF_STOP_PCT = {"day": 0.05, "swing": 0.08, "position": 0.15, None: 0.08}


def cf_stop_pct(*, sleeve: str, horizon: str | None, cfg: dict | None) -> float:
    if sleeve == "value":
        vt = (cfg or {}).get("value_trade") or {}
        try:
            return float(vt.get("hard_stop_pct") or 0.20)
        except (TypeError, ValueError):
            return 0.20
    h = (horizon or "").lower() if horizon else None
    return DEFAULT_CF_STOP_PCT.get(h, DEFAULT_CF_STOP_PCT[None])


def forward_ret_pct_with_stop(
        *,
        entry: float,
        bars: list[tuple[float, float]],
        days: int,
        stop_pct: float,
        cost_pct: float,
) -> float | None:
    """일봉 (close, low). low 가 손절선 터치 시 stop 손실(+비용), 아니면 horizon 종가."""
    if not bars or entry <= 0 or len(bars) < days:
        return None
    stop_px = entry * (1.0 - stop_pct)
    for _close, low in bars[:days]:
        if low <= stop_px:
            return round((stop_px / entry - 1.0) * 100.0 - cost_pct * 100.0, 3)
    exit_close = bars[days - 1][0]
    return round((exit_close / entry - 1.0) * 100.0 - cost_pct * 100.0, 3)
