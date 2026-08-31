"""청산 거래의 공통 정의 — 측정층이 같은 표본을 쓰게.

지금 청산 정의가 세 갈래다:
  get_closed_positions : state=closed AND pnl IS NOT NULL
  closed_trades        : state=closed AND qty>0
  _gate_postmortem     : scored_trades (actual) + CF 별도 (J10)

scored_trades 는 둘을 교집합하고 parent_id 로 묶어, 부분매도 slice 가
거래 여러 건으로 세어지지 않게 한다. 캘리브·사후분석·그림자가 이걸 쓴다.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping


# config.example.yaml paper.* 와 같은 기본값 — cfg 없이 호출해도 왕복비용이 0 이 되지 않게.
_DEFAULT_PAPER = {
    "fee_rate": {"KR": 0.00015, "US": 0.001},
    "slippage_bps": {"KR": 5, "US": 5},
    "sell_tax_rate": {"KR": 0.0015, "US": 0.0},
}


def _row_get(row: Mapping[str, Any], key: str, default=None):
    try:
        if hasattr(row, "keys") and key not in row.keys():
            return default
        v = row[key]
        return default if v is None else v
    except (KeyError, IndexError, TypeError):
        return default


def _as_float(v, default: float | None = None) -> float | None:
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def trade_group_id(row: Mapping[str, Any], *, fallback: int | None = None) -> int:
    """부분매도 slice 는 parent_id 로 한 거래. 없으면 행 id, 그것도 없으면 fallback."""
    pid = _row_get(row, "parent_id")
    if pid is not None:
        try:
            return int(pid)
        except (TypeError, ValueError):
            pass
    rid = _row_get(row, "id")
    if rid is not None:
        try:
            return int(rid)
        except (TypeError, ValueError):
            pass
    if fallback is not None:
        return int(fallback)
    raise ValueError("trade_group_id: parent_id/id 없음")


def group_scored_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict]:
    """pnl 확정 + qty>0 행을 parent_id 로 묶는다. qty 결손은 1 로 본다(테스트 스텁)."""
    groups: dict[int, dict] = {}
    order: list[int] = []
    for i, row in enumerate(rows):
        pnl = _as_float(_row_get(row, "pnl"))
        if pnl is None:
            continue
        qty = _as_float(_row_get(row, "qty"), default=1.0) or 0.0
        if qty <= 0:
            continue
        gid = trade_group_id(row, fallback=i)
        if gid not in groups:
            groups[gid] = {
                "id": gid,
                "symbol": _row_get(row, "symbol"),
                "market": _row_get(row, "market") or "KR",
                "strategy": _row_get(row, "strategy"),
                "pnl": 0.0,
                "qty": 0.0,
                "cost": 0.0,
                "meta": _row_get(row, "meta"),
                "avg_price": _as_float(_row_get(row, "avg_price")),
                "exit_price": _as_float(_row_get(row, "exit_price")),
                "exit_reason": _row_get(row, "exit_reason"),
                "opened_at": _row_get(row, "opened_at"),
                "closed_at": _row_get(row, "closed_at"),
            }
            order.append(gid)
        g = groups[gid]
        g["pnl"] += pnl
        g["qty"] += qty
        avg = _as_float(_row_get(row, "avg_price")) or 0.0
        g["cost"] += avg * qty
        # 마지막 slice 의 청산가·사유를 대표값으로
        ex = _as_float(_row_get(row, "exit_price"))
        if ex is not None:
            g["exit_price"] = ex
        reason = _row_get(row, "exit_reason")
        if reason:
            g["exit_reason"] = reason
        meta = _row_get(row, "meta")
        if meta and not g["meta"]:
            g["meta"] = meta
    return [groups[i] for i in order]


def scored_trades(store, since: float | None = None) -> list[dict]:
    """채점용 청산 거래. store.get_closed_positions 위에 qty>0 + parent_id 그룹."""
    rows = store.get_closed_positions(since=since)
    return group_scored_rows(rows)


def _paper_block(cfg: dict | None) -> dict:
    if not cfg:
        return dict(_DEFAULT_PAPER)
    paper = cfg.get("paper") if "paper" in cfg else cfg
    if not isinstance(paper, dict):
        return dict(_DEFAULT_PAPER)
    out = {k: dict(v) for k, v in _DEFAULT_PAPER.items()}
    for key in ("fee_rate", "slippage_bps", "sell_tax_rate"):
        raw = paper.get(key)
        if isinstance(raw, dict):
            out[key].update({str(k): v for k, v in raw.items()})
        elif raw is not None:
            try:
                v = float(raw)
            except (TypeError, ValueError):
                continue
            for m in out[key]:
                out[key][m] = v
    return out


def roundtrip_cost_pct(market: str, cfg: dict | None = None) -> float:
    """왕복 비용 비율(소수). fee*2 + 매도세 + slippage_bps*2.

    그림자가 비용 0 으로 채점되면 '막아서 손해' 쪽으로 기울고,
    실거래(수수료·거래세·슬리피지)와 비교가 안 된다.
    """
    paper = _paper_block(cfg)
    m = market or "KR"
    fee = _as_float((paper.get("fee_rate") or {}).get(m), 0.0) or 0.0
    tax = _as_float((paper.get("sell_tax_rate") or {}).get(m), 0.0) or 0.0
    slip_bps = _as_float((paper.get("slippage_bps") or {}).get(m), 0.0) or 0.0
    return fee * 2.0 + tax + (slip_bps / 10000.0) * 2.0
