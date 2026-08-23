"""종목별 과거 거래 회고(lessons) — 청산 이력에서 결정적 파생(LLM 0콜).

같은 종목을 다시 검토할 때 "과거에 어떻게 사서 어떻게 끝났나"를 뇌에 보여준다
(TradingAgents 벤치마킹). 별도 생성·저장·비동기 없이 주입 시점에 store 의 청산
포지션 이력에서 요약을 만든다. 자유 문장 생성 금지 — 구조화 요약만(토큰 절약).
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

_KST = ZoneInfo("Asia/Seoul")


def _pnl_pct(pnl, qty, avg_price) -> float | None:
    """실현손익률(%) — 원가(qty*avg_price) 대비. 결손/0원가면 None(0나눗셈 안전)."""
    if pnl is None:
        return None
    cost = (qty or 0) * (avg_price or 0)
    if not cost:
        return None
    return round(float(pnl) / float(cost) * 100, 1)


def _held_days(opened_at, closed_at) -> int | None:
    """보유일수(일 단위 반올림). 타임스탬프 결손이면 None."""
    if not opened_at or not closed_at:
        return None
    return round((float(closed_at) - float(opened_at)) / 86400)


def _summarize(row) -> dict:
    """청산 포지션 1행 → 회고 항목. thesis 없으면 thesis_head 키 자체를 생략."""
    item = {
        "closed": (datetime.fromtimestamp(row["closed_at"], _KST).strftime("%m-%d")
                   if row["closed_at"] else None),
        "held_days": _held_days(row["opened_at"], row["closed_at"]),
        "pnl_pct": _pnl_pct(row["pnl"], row["qty"], row["avg_price"]),
        "exit": row["exit_reason"] or "unknown",
        "strategy": row["strategy"],
    }
    thesis = row["thesis"]
    if thesis:
        item["thesis_head"] = thesis[:60]
    return item


def build_symbol_lessons(store, symbols, *, max_per_symbol=3, now=None) -> dict[str, dict]:
    """청산 이력 → 종목별 회고 요약. 이력 없는 심볼은 키 자체가 없다(토큰 절약).

    반환(심볼당): {n, wins, avg_pnl_pct, recent}
      n            = 실제 진입됐다 청산된 거래 수(armed 만 됐다 해제된 건 제외)
      wins         = 실현이익(pnl>0) 거래 수
      avg_pnl_pct  = 손익률 계산 가능한 거래의 평균(%), 없으면 None
      recent       = 최근 max_per_symbol개 회고 항목(최신순, _summarize)
    now 는 시그니처 호환용(현재 미사용) — 회고는 저장된 청산 시점만 참조한다.
    """
    syms = list(dict.fromkeys(symbols or []))       # 중복 제거, 순서 보존
    if not syms:
        return {}
    rows = store.closed_trades(syms)                # 최신순, qty>0
    by_sym: dict[str, list] = {}
    for r in rows:
        by_sym.setdefault(r["symbol"], []).append(r)
    out: dict[str, dict] = {}
    for sym, srows in by_sym.items():
        pcts = [p for p in (_pnl_pct(r["pnl"], r["qty"], r["avg_price"]) for r in srows)
                if p is not None]
        wins = sum(1 for r in srows if r["pnl"] is not None and float(r["pnl"]) > 0)
        out[sym] = {
            "n": len(srows), "wins": wins,
            "avg_pnl_pct": (round(sum(pcts) / len(pcts), 1) if pcts else None),
            "recent": [_summarize(r) for r in srows[:max_per_symbol]],
        }
    return out
