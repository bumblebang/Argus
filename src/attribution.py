"""성과 귀속(attribution) — 기록을 지혜로 바꾸는 되먹임 루프의 집계부.

store 의 청산 완료 포지션(pnl 확정)과 결정 저널을 읽어, 전략별 라이브 성과와
최근 거래 결과를 집계한다. 이 출력이 뇌 컨텍스트(track_record)에 주입되어
뇌가 "내 과거 판단이 실제로 어땠는지"를 보고 다음 판단을 조정할 수 있게 한다.

전부 읽기전용·순수 집계 — 매매 경로에 영향 없음. pnl 은 mirror 경로에서
수수료를 반영해 account.realized_pnl 과 맞춘다.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from .logging_setup import get_logger

log = get_logger("attribution")

_DAY = 86400.0

# 표본이 이보다 작으면 통계를 과신하지 말라는 뜻으로 컨텍스트에 함께 실린다.
MIN_SAMPLE = 5


def _trade_group_id(row) -> int:
    """부분매도 slice 는 parent_id 로 한 거래로 묶는다."""
    pid = row["parent_id"] if "parent_id" in row.keys() else None
    return int(pid) if pid is not None else int(row["id"])


def _ret_pct_from_pnl(pnl: float, cost: float) -> float | None:
    if not cost:
        return None
    return round(pnl / cost * 100, 2)


def strategy_stats(store, since_days: float | None = 90) -> list[dict]:
    """전략×시장별 청산 거래 통계. 부분매도 slice 는 parent_id 로 1거래로 집계."""
    since = (time.time() - since_days * _DAY) if since_days else None
    trades: dict[tuple, dict] = {}
    for row in store.get_closed_positions(since=since):
        gid = _trade_group_id(row)
        tkey = (gid, row["strategy"] or "?", row["market"] or "?")
        t = trades.setdefault(tkey, {"strategy": tkey[1], "market": tkey[2],
                                     "pnl": 0.0, "cost": 0.0})
        t["pnl"] += row["pnl"]
        t["cost"] += (row["avg_price"] or 0) * (row["qty"] or 0)
    agg: dict[tuple, dict] = {}
    for t in trades.values():
        key = (t["strategy"], t["market"])
        s = agg.setdefault(key, {"strategy": key[0], "market": key[1], "trades": 0,
                                 "wins": 0, "total_pnl": 0.0, "_rets": []})
        s["trades"] += 1
        s["wins"] += 1 if t["pnl"] > 0 else 0
        s["total_pnl"] += t["pnl"]
        r = _ret_pct_from_pnl(t["pnl"], t["cost"])
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
    """최근 청산 거래 — 부분 slice 는 parent_id 로 묶어 1건으로 표시."""
    groups: dict[int, dict] = {}
    order: list[int] = []
    for row in store.get_closed_positions(limit=limit * 4):
        gid = _trade_group_id(row)
        if gid not in groups:
            groups[gid] = {
                "symbol": row["symbol"], "market": row["market"],
                "strategy": row["strategy"], "pnl": 0.0, "_cost": 0.0,
                "opened_at": row["opened_at"], "closed_at": row["closed_at"],
                "exit_reason": row["exit_reason"],
            }
            order.append(gid)
        g = groups[gid]
        g["pnl"] += row["pnl"]
        g["_cost"] += (row["avg_price"] or 0) * (row["qty"] or 0)
        g["closed_at"] = max(g["closed_at"] or 0, row["closed_at"] or 0)
    out = []
    for gid in order[:limit]:
        g = groups[gid]
        cost = g.pop("_cost")
        held = ((g["closed_at"] or 0) - (g["opened_at"] or 0)) / _DAY
        out.append({
            "symbol": g["symbol"], "market": g["market"],
            "strategy": g["strategy"],
            "ret_pct": _ret_pct_from_pnl(g["pnl"], cost),
            "pnl": round(g["pnl"], 2), "held_days": round(held, 1),
            "exit_reason": g["exit_reason"],
            "closed": datetime.fromtimestamp(g["closed_at"] or 0,
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
    """도시에 기반 거래 vs 아닌 거래의 성과 비교 — Athena 의 알파 기여를 숫자로."""
    import json as _json
    since = (time.time() - since_days * _DAY) if since_days else None
    groups: dict[int, dict] = {}
    for row in store.get_closed_positions(since=since):
        gid = _trade_group_id(row)
        g = groups.setdefault(gid, {"pnl": 0.0, "cost": 0.0, "has_dossier": False})
        g["pnl"] += row["pnl"]
        g["cost"] += (row["avg_price"] or 0) * (row["qty"] or 0)
        try:
            meta = _json.loads(row["meta"]) if row["meta"] else {}
        except (ValueError, TypeError):
            meta = {}
        if meta.get("dossier_id"):
            g["has_dossier"] = True
    buckets = {"with_dossier": {"trades": 0, "wins": 0, "_rets": []},
               "without_dossier": {"trades": 0, "wins": 0, "_rets": []}}
    for g in groups.values():
        b = buckets["with_dossier" if g["has_dossier"] else "without_dossier"]
        b["trades"] += 1
        b["wins"] += 1 if g["pnl"] > 0 else 0
        r = _ret_pct_from_pnl(g["pnl"], g["cost"])
        if r is not None:
            b["_rets"].append(r)
    out = {}
    for name, b in buckets.items():
        rets = b.pop("_rets")
        b["win_rate"] = round(b["wins"] / b["trades"], 2) if b["trades"] else None
        b["avg_ret_pct"] = round(sum(rets) / len(rets), 2) if rets else None
        out[name] = b
    return out


def manager_epochs(store, since_days: float = 90) -> dict:
    """결정 payload.manager.epoch 별 건수 — 폴백은 별도 키."""
    import json as _json
    since = time.time() - since_days * _DAY
    with store._lock:
        rows = store.conn.execute(
            "SELECT payload FROM decisions WHERE ts >= ?", (since,)).fetchall()
    epochs: dict[str, int] = {}
    fallback_n = 0
    for r in rows:
        try:
            pay = _json.loads(r["payload"]) if r["payload"] else {}
        except (ValueError, TypeError):
            continue
        mgr = pay.get("manager") or {}
        ep = mgr.get("epoch") or "unknown"
        epochs[ep] = epochs.get(ep, 0) + 1
        if (mgr.get("decision") or {}).get("used_fallback"):
            fallback_n += 1
    return {"by_epoch": dict(sorted(epochs.items(), key=lambda x: -x[1])),
            "fallback_n": fallback_n, "n": sum(epochs.values())}


def track_record(store, *, stats_days: float = 90, trades_limit: int = 10,
                 decisions_days: float = 30) -> dict:
    """뇌 컨텍스트에 주입할 트랙레코드 묶음. store 실패 시 빈 dict(사이클은 계속)."""
    try:
        from .calibration import conviction_calibration
        from .shadow_ledger import shadow_stats
        out = {
            "note": (f"라이브 성과 귀속(pnl=수수료 반영). trades<{MIN_SAMPLE}(small_sample) "
                     "인 통계는 과신하지 말 것."),
            "strategy_stats": strategy_stats(store, since_days=stats_days),
            "recent_trades": recent_trades(store, limit=trades_limit),
            "decision_stats": decision_stats(store, since_days=decisions_days),
            "dossier_ab": dossier_ab(store, since_days=stats_days),
            "manager_epochs": manager_epochs(store, since_days=decisions_days),
            "conviction_calibration": conviction_calibration(
                store, since_days=stats_days),
        }
        sh = shadow_stats(store, since_days=stats_days)
        n_scored = (sh.get("overall") or {}).get("n_scored") or 0
        log.info("그림자 장부 scored=%s open=%s",
                 n_scored, (sh.get("overall") or {}).get("n_open"))
        # 뇌 주입은 표본 20건 이상일 때만 (thin-sample 가드)
        if n_scored >= 20:
            out["shadow_stats"] = sh
        return out
    except Exception as e:                     # 귀속 실패가 사이클을 죽이면 안 된다.
        log.warning("track_record 집계 실패(빈 값으로 진행): %s", e)
        return {}
