"""성과 귀속(attribution) — 기록을 지혜로 바꾸는 되먹임 루프의 집계부.

store 의 청산 완료 포지션(pnl 확정)과 결정 저널을 읽어, 전략별 라이브 성과와
최근 거래 결과를 집계한다. 이 출력이 뇌 컨텍스트(track_record)에 주입되어
뇌가 "내 과거 판단이 실제로 어땠는지"를 보고 다음 판단을 조정할 수 있게 한다.

전부 읽기전용·순수 집계 — 매매 경로에 영향 없음. pnl 은 수수료 제외 원장이므로
정밀 회계가 아니라 '전략/종목이 통하는가'의 신호로 쓴다.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from .logging_setup import get_logger

log = get_logger("attribution")

_DAY = 86400.0

# 표본이 이보다 작으면 통계를 과신하지 말라는 뜻으로 컨텍스트에 함께 실린다.
MIN_SAMPLE = 5


def _ret_pct(row) -> float | None:
    """거래 수익률(%) = pnl / 원가. 원가 0 이면 None."""
    cost = (row["avg_price"] or 0) * (row["qty"] or 0)
    if not cost:
        return None
    return round(row["pnl"] / cost * 100, 2)


def strategy_stats(store, since_days: float | None = 90) -> list[dict]:
    """전략×시장별 청산 거래 통계(거래수·승률·평균수익률·누적손익). 최신 성과 우선.

    since_days 이내 청산만 집계(None=전체). 표본 적은 전략도 내보내되 trades 로 판단.
    """
    since = (time.time() - since_days * _DAY) if since_days else None
    agg: dict[tuple, dict] = {}
    for row in store.get_closed_positions(since=since):
        key = (row["strategy"] or "?", row["market"] or "?")
        s = agg.setdefault(key, {"strategy": key[0], "market": key[1], "trades": 0,
                                 "wins": 0, "total_pnl": 0.0, "_rets": []})
        s["trades"] += 1
        s["wins"] += 1 if row["pnl"] > 0 else 0
        s["total_pnl"] += row["pnl"]
        r = _ret_pct(row)
        if r is not None:
            s["_rets"].append(r)
    out = []
    for s in agg.values():
        rets = s.pop("_rets")
        s["win_rate"] = round(s["wins"] / s["trades"], 2) if s["trades"] else None
        s["avg_ret_pct"] = round(sum(rets) / len(rets), 2) if rets else None
        s["total_pnl"] = round(s["total_pnl"], 2)
        s["small_sample"] = s["trades"] < MIN_SAMPLE
        out.append(s)
    out.sort(key=lambda x: x["trades"], reverse=True)
    return out


def recent_trades(store, limit: int = 10) -> list[dict]:
    """최근 청산 거래 — 종목/전략/수익률/보유일/청산사유. 뇌가 개별 판단을 복기할 재료."""
    out = []
    for row in store.get_closed_positions(limit=limit):
        held = ((row["closed_at"] or 0) - (row["opened_at"] or 0)) / _DAY
        out.append({
            "symbol": row["symbol"], "market": row["market"],
            "strategy": row["strategy"], "ret_pct": _ret_pct(row),
            "pnl": round(row["pnl"], 2), "held_days": round(held, 1),
            "exit_reason": row["exit_reason"],
            "closed": datetime.fromtimestamp(row["closed_at"] or 0,
                                             tz=timezone.utc).date().isoformat(),
        })
    return out


def decision_stats(store, since_days: float = 30) -> dict:
    """최근 결정 저널 요약 — 제안/승인/거부 건수(액션별). 검증 게이트의 체감 통과율."""
    since = time.time() - since_days * _DAY
    rows = store.decision_counts(since)     # store 락 경유(뇌 워커·감시 루프 동시접근 안전)
    out = {"proposed": 0, "approved": 0, "vetoed": 0, "by_action": {}}
    for r in rows:
        action, verdict, n = (r["action"] or "?"), (r["verdict"] or "?"), r["n"]
        out["proposed"] += n
        if verdict == "approved":
            out["approved"] += n
        elif verdict == "vetoed":
            out["vetoed"] += n
        a = out["by_action"].setdefault(action, {"approved": 0, "vetoed": 0})
        if verdict in a:
            a[verdict] += n
    return out


def dossier_ab(store, since_days: float | None = 90) -> dict:
    """도시에 기반 거래 vs 아닌 거래의 성과 비교 — Athena 의 알파 기여를 숫자로.

    positions.meta.dossier_id 유무로 버킷을 나눈다(진입 시 태그됨).
    """
    import json as _json
    since = (time.time() - since_days * _DAY) if since_days else None
    buckets = {"with_dossier": {"trades": 0, "wins": 0, "_rets": []},
               "without_dossier": {"trades": 0, "wins": 0, "_rets": []}}
    for row in store.get_closed_positions(since=since):
        try:
            meta = _json.loads(row["meta"]) if row["meta"] else {}
        except (ValueError, TypeError):
            meta = {}
        b = buckets["with_dossier" if meta.get("dossier_id") else "without_dossier"]
        b["trades"] += 1
        b["wins"] += 1 if row["pnl"] > 0 else 0
        r = _ret_pct(row)
        if r is not None:
            b["_rets"].append(r)
    out = {}
    for name, b in buckets.items():
        rets = b.pop("_rets")
        b["win_rate"] = round(b["wins"] / b["trades"], 2) if b["trades"] else None
        b["avg_ret_pct"] = round(sum(rets) / len(rets), 2) if rets else None
        out[name] = b
    return out


def track_record(store, *, stats_days: float = 90, trades_limit: int = 10,
                 decisions_days: float = 30) -> dict:
    """뇌 컨텍스트에 주입할 트랙레코드 묶음. store 실패 시 빈 dict(사이클은 계속)."""
    try:
        return {
            "note": (f"라이브 성과 귀속. trades<{MIN_SAMPLE}(small_sample) 인 통계는 "
                     "과신하지 말 것."),
            "strategy_stats": strategy_stats(store, since_days=stats_days),
            "recent_trades": recent_trades(store, limit=trades_limit),
            "decision_stats": decision_stats(store, since_days=decisions_days),
            "dossier_ab": dossier_ab(store, since_days=stats_days),
        }
    except Exception as e:                     # 귀속 실패가 사이클을 죽이면 안 된다.
        log.warning("track_record 집계 실패(빈 값으로 진행): %s", e)
        return {}
