"""그림자 장부 v1 — 차단된 BUY 제안의 반사실 페이퍼 추적.

체결되지 않은 BUY를 제안 시점 가격(entry)으로 페이퍼 진입해, horizon 경과 후
종가로 채점한다. 매매 경로와 분리(실패해도 사이클은 계속).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .eval.trade_defs import roundtrip_cost_pct
from .logging_setup import get_logger

log = get_logger("shadow_ledger")

KST = timezone(timedelta(hours=9))

# v1: hard block만. armed/gap_armed/no_price/arm_skipped 제외.
SHADOW_BLOCK_STATUSES = frozenset({
    "vetoed", "gate_rejected", "gap_rejected", "no_dossier", "buy_blocked",
})
# v1.1: 미체결 대기 — horizon/실체결/해제 시 채점
SHADOW_SOFT_STATUSES = frozenset({"armed", "gap_armed"})
SHADOW_BOOK_STATUSES = SHADOW_BLOCK_STATUSES | SHADOW_SOFT_STATUSES

_DEFAULT_HORIZON_DAYS = {"day": 1, "swing": 20, "position": 120}
MIN_SAMPLE = 5


def reason_bucket(st: str, reason: str, concerns: list | None) -> str:
    """차단 사유 버킷 — _gate_postmortem 과 동일 분류."""
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
    if st == "buy_blocked":
        return "브로커:매수가드"
    if st == "no_dossier":
        return "코드:no_dossier"
    return st


def horizon_calendar_days(horizon: str | None,
                          cfg: dict | None = None) -> int:
    """horizon → 보유 캘린더 일수(exit_policy time_stop 과 정렬)."""
    hz = (horizon or "swing").strip().lower()
    if hz == "day":
        return 1
    block = (cfg or {}).get("exit_policy") or {}
    ts = block.get("time_stop") or {}
    by_hz = ts.get("by_horizon") or {}
    sub = by_hz.get(hz) if isinstance(by_hz, dict) else None
    if isinstance(sub, dict) and sub.get("max_days") is not None:
        try:
            return max(1, int(sub["max_days"]))
        except (TypeError, ValueError):
            pass
    return _DEFAULT_HORIZON_DAYS.get(hz, 20)


def _entry_price_at(data_dir: Path, store, symbol: str, ts: float,
                    daily_cache: dict | None = None) -> tuple[float | None, str]:
    """백필용 진입가: 당일(이전) 종가 → snapshots 폴백."""
    cache = daily_cache if daily_cache is not None else {}
    if symbol not in cache:
        cache[symbol] = load_daily_series(data_dir, symbol)
    series = cache[symbol]
    if series:
        t0 = datetime.fromtimestamp(ts, tz=KST).date()
        entry = None
        for d, c in series:
            if d.date() <= t0:
                entry = c
            else:
                break
        if entry is not None:
            return float(entry), "history"
    if store is not None:
        px = store.nearest_snapshot_price(symbol, ts, window_sec=3600)
        if px is not None:
            return px, "snapshot"
    return None, ""


def book_row(store, *, cycle_ts: float, cycle_ts_iso: str, sleeve: str,
             symbol: str, market: str, block_status: str, block_reason: str,
             verifier_reason: str | None, concerns: list | None,
             conviction: float | None, horizon: str | None,
             target_weight: float | None, thesis: str | None,
             strategy: str | None, proposal: dict | None,
             entry_price: float, cfg: dict | None = None,
             meta_extra: dict | None = None,
             state: str = "open") -> int | None:
    """단일 차단 BUY → shadow_positions. 중복이면 None."""
    if store is None or entry_price <= 0:
        return None
    hz = horizon or "swing"
    hdays = horizon_calendar_days(hz, cfg)
    meta = {"horizon_days": hdays, "price_source": "price_lookup"}
    if meta_extra:
        meta.update(meta_extra)
    bucket = reason_bucket(block_status, block_reason or verifier_reason or "",
                           concerns)
    return store.insert_shadow_position(
        cycle_ts=cycle_ts,
        cycle_ts_iso=cycle_ts_iso or None,
        sleeve=sleeve,
        symbol=symbol,
        market=market or "KR",
        block_status=block_status,
        block_bucket=bucket,
        block_reason=(block_reason or "")[:500],
        verifier_reason=(verifier_reason or "")[:500] or None,
        concerns=concerns or [],
        conviction=conviction,
        horizon=hz,
        target_weight=target_weight,
        thesis=(thesis or "")[:2000] or None,
        strategy=strategy,
        proposal_json=proposal,
        entry_price=float(entry_price),
        entry_ts=cycle_ts,
        state=state,
        meta=meta,
    )


def book_soft_pending(store, cycle_result, price_lookup: dict[str, float],
                      *, sleeve: str = "brain", cfg: dict | None = None) -> int:
    """armed/gap_armed BUY → state=pending 그림자(미체결 추적)."""
    if store is None:
        return 0
    n = 0
    try:
        props = {p.symbol: p for p in cycle_result.decision.proposals if p.side == "BUY"}
        cycle_ts = cycle_result.cycle_ts or 0.0
        cycle_ts_iso = cycle_result.cycle_ts_iso or ""
        for e in cycle_result.executed:
            if (e.get("action") or "").upper() != "BUY":
                continue
            st = e.get("status") or ""
            if st not in SHADOW_SOFT_STATUSES:
                continue
            sym = e.get("symbol") or ""
            price = price_lookup.get(sym)
            if not sym or not price or price <= 0:
                continue
            p = props.get(sym)
            rid = book_row(
                store, cycle_ts=cycle_ts, cycle_ts_iso=cycle_ts_iso, sleeve=sleeve,
                symbol=sym, market=(p.market if p else "KR"),
                block_status=st, block_reason=e.get("reason") or "",
                verifier_reason=None, concerns=[],
                conviction=(p.conviction if p else None),
                horizon=(p.horizon if p else None),
                target_weight=(p.target_weight if p else None),
                thesis=(p.thesis if p else None),
                strategy=(p.strategy if p else None),
                proposal=(p.model_dump() if p else None),
                entry_price=float(price), cfg=cfg, state="pending",
                meta_extra={"price_source": "price_lookup", "soft_pending": True})
            if rid:
                n += 1
    except Exception as ex:
        log.warning("그림자 pending 등록 실패: %s", ex)
    return n


def cancel_shadow_on_fill(store, symbol: str, *, after_ts: float | None = None) -> int:
    """실체결 시 동일 종목 pending/open 그림자 취소."""
    if store is None or not symbol:
        return 0
    return store.cancel_shadow_positions(symbol, after_ts=after_ts)


def book_blocked(store, cycle_result, price_lookup: dict[str, float],
                 *, sleeve: str = "brain",
                 cfg: dict | None = None) -> int:
    """CycleResult 에서 그림자 대상 BUY를 shadow_positions 에 등록. 등록 건수 반환."""
    if store is None:
        return 0
    n = 0
    try:
        props = {p.symbol: p for p in cycle_result.decision.proposals if p.side == "BUY"}
        verdicts = {v.symbol: v for v in cycle_result.validation.verdicts}
        cycle_ts = cycle_result.cycle_ts or 0.0
        cycle_ts_iso = cycle_result.cycle_ts_iso or ""

        for e in cycle_result.executed:
            act = (e.get("action") or e.get("side") or "").upper()
            if act != "BUY":
                continue
            st = e.get("status") or ""
            if st not in SHADOW_BLOCK_STATUSES:
                continue
            sym = e.get("symbol") or ""
            if not sym:
                continue
            price = price_lookup.get(sym)
            if not price or price <= 0:
                continue

            p = props.get(sym)
            v = verdicts.get(sym)
            reason = e.get("reason") or ""
            concerns: list = []
            verifier_reason = ""
            if v is not None:
                if st == "vetoed" and v.reason:
                    reason = v.reason
                concerns = list(v.concerns or [])
                verifier_reason = v.reason or ""

            rid = book_row(
                store, cycle_ts=cycle_ts, cycle_ts_iso=cycle_ts_iso, sleeve=sleeve,
                symbol=sym, market=(p.market if p else "KR"),
                block_status=st, block_reason=e.get("reason") or "",
                verifier_reason=verifier_reason, concerns=concerns,
                conviction=(p.conviction if p else None),
                horizon=(p.horizon if p else None),
                target_weight=(p.target_weight if p else None),
                thesis=(p.thesis if p else None),
                strategy=(p.strategy if p else None),
                proposal=(p.model_dump() if p else None),
                entry_price=float(price), cfg=cfg)
            if rid:
                n += 1
    except Exception as ex:
        log.warning("그림자 장부 등록 실패(사이클 계속): %s", ex)
    if n:
        log.info("그림자 장부 +%d (%s)", n, sleeve)
    return n


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


def backfill_from_jsonl(store, path: Path | str, *, sleeve: str = "brain",
                        data_dir: Path | str = "data",
                        cfg: dict | None = None,
                        limit: int | None = None) -> dict[str, int]:
    """과거 decisions.jsonl 재생 → shadow 등록. 진입가=히스토리/스냅샷."""
    path = Path(path)
    data_dir = Path(data_dir)
    out = {"booked": 0, "skipped_no_price": 0, "skipped_status": 0, "dup": 0, "lines": 0}
    if not path.exists() or store is None:
        return out
    daily_cache: dict = {}
    with path.open(encoding="utf-8") as f:
        for line in f:
            if limit is not None and out["lines"] >= limit:
                break
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            out["lines"] += 1
            cycle_ts = parse_ts(rec.get("ts"))
            if cycle_ts is None:
                continue
            props = {p["symbol"]: p for p in (rec.get("proposals") or [])
                     if (p.get("side") or "").upper() == "BUY"}
            verd = {v["symbol"]: v for v in (rec.get("verdicts") or [])}
            for e in rec.get("executed") or []:
                if (e.get("action") or e.get("side") or "").upper() != "BUY":
                    continue
                st = e.get("status") or ""
                if st not in SHADOW_BOOK_STATUSES:
                    out["skipped_status"] += 1
                    continue
                sym = e.get("symbol") or ""
                if not sym:
                    continue
                px, src = _entry_price_at(data_dir, store, sym, cycle_ts, daily_cache)
                if px is None:
                    out["skipped_no_price"] += 1
                    continue
                p = props.get(sym) or {}
                v = verd.get(sym) or {}
                st_state = "pending" if st in SHADOW_SOFT_STATUSES else "open"
                rid = book_row(
                    store, cycle_ts=cycle_ts,
                    cycle_ts_iso=str(rec.get("ts") or ""),
                    sleeve=sleeve, symbol=sym,
                    market=p.get("market") or "KR",
                    block_status=st, block_reason=e.get("reason") or "",
                    verifier_reason=v.get("reason"),
                    concerns=v.get("concerns") or [],
                    conviction=p.get("conviction"),
                    horizon=p.get("horizon"),
                    target_weight=p.get("target_weight"),
                    thesis=p.get("thesis"),
                    strategy=p.get("strategy"),
                    proposal=p or None,
                    entry_price=px, cfg=cfg,
                    meta_extra={"price_source": src, "backfill": True,
                                **({"soft_pending": True} if st_state == "pending" else {})},
                    state=st_state)
                if rid:
                    out["booked"] += 1
                else:
                    out["dup"] += 1
    log.info("그림자 백필 %s: %s", path.name, out)
    return out


def load_daily_series(data_dir: Path, symbol: str) -> list[tuple[datetime, float]]:
    """history CSV 에서 (datetime, close) 시계열."""
    candidates = sorted(data_dir.glob(f"history/{symbol}.KS_1d_*.csv"))
    if not candidates:
        candidates = sorted(data_dir.glob(f"history/{symbol}_1d_*.csv"))
    if not candidates:
        return []
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
            close = float(parts[4]) if len(parts) >= 6 else float(parts[-1])
            rows.append((d, close))
        except ValueError:
            continue
    return rows


def exit_close_on_calendar(series: list[tuple[datetime, float]],
                           entry_ts: float,
                           horizon_days: int) -> float | None:
    """entry_ts + horizon_days 캘린더 기준, 그 이전 마지막 거래일 종가."""
    if not series:
        return None
    target = entry_ts + horizon_days * 86400
    target_date = datetime.fromtimestamp(target, tz=KST).date()
    best = None
    for d, c in series:
        if d.date() <= target_date:
            best = c
        else:
            break
    return best


def snap_forward(store, symbol: str, ts0: float, days: float) -> float | None:
    target = ts0 + days * 86400
    return store.nearest_snapshot_price(symbol, target, window_sec=86400 * 2 + 7200)


def score_open_shadows(store, *, now: float | None = None,
                       data_dir: Path | str = "data",
                       cfg: dict | None = None) -> dict[str, int]:
    """state=open|pending 그림자 포지션 채점. {scored, skipped, pending, cancelled} 반환."""
    now = now or datetime.now(timezone.utc).timestamp()
    data_dir = Path(data_dir)
    stats = {"scored": 0, "skipped": 0, "pending": 0, "cancelled": 0}
    daily_cache: dict[str, list] = {}

    for row in store.get_scorable_shadow_positions():
        entry_ts = float(row["entry_ts"])
        sym = row["symbol"]
        st = row["state"]
        market = row["market"] if "market" in row.keys() else "KR"
        held = (store.had_position_since(sym, entry_ts)
                if hasattr(store, "had_position_since")
                else store.has_open_since(sym, entry_ts))
        if held:
            # pending 뿐 아니라 hard-block(open) 그림자도 실체결이 있으면 취소.
            # 안 그러면 '막아서 손해' 채점이 실제 산 거래를 유령으로 남긴다.
            store.cancel_shadow_positions(sym, after_ts=entry_ts)
            stats["cancelled"] += 1
            continue
        if st == "pending":
            if store.is_symbol_armed(sym):
                meta = {}
                if row["meta"]:
                    try:
                        meta = json.loads(row["meta"])
                    except (ValueError, TypeError):
                        pass
                hdays = int(meta.get("horizon_days") or
                            horizon_calendar_days(row["horizon"], cfg))
                if now < entry_ts + hdays * 86400:
                    stats["pending"] += 1
                    continue
                # armed 만료(horizon) — 미체결로 채점

        meta = {}
        if row["meta"]:
            try:
                meta = json.loads(row["meta"])
            except (ValueError, TypeError):
                pass
        hdays = int(meta.get("horizon_days") or
                    horizon_calendar_days(row["horizon"], cfg))
        if now < entry_ts + hdays * 86400:
            stats["pending"] += 1
            continue

        if sym not in daily_cache:
            daily_cache[sym] = load_daily_series(data_dir, sym)
        series = daily_cache[sym]
        exit_px = exit_close_on_calendar(series, entry_ts, hdays)
        price_source = "history"
        if exit_px is None:
            exit_px = snap_forward(store, sym, entry_ts, float(hdays))
            price_source = "snapshot"
        if exit_px is None:
            store.skip_shadow_position(row["id"], "no_price_data")
            stats["skipped"] += 1
            continue

        entry_px = float(row["entry_price"])
        cost_pct = roundtrip_cost_pct(market or "KR", cfg) * 100.0
        ret = round((exit_px / entry_px - 1) * 100 - cost_pct, 3)
        exit_ts = entry_ts + hdays * 86400
        store.score_shadow_position(
            row["id"], exit_price=exit_px, exit_ts=exit_ts,
            exit_reason="horizon_expired" if st != "pending" else "pending_timeout",
            ret_pct=ret)
        stats["scored"] += 1
        log.debug("shadow scored %s %s ret=%.2f%% src=%s",
                  sym, row["block_bucket"], ret, price_source)

    return stats


def _agg_rows(rows: list) -> dict:
    rets = [float(r["ret_pct"]) for r in rows if r["ret_pct"] is not None]
    if not rets:
        return {"n": 0}
    wins = sum(1 for v in rets if v > 0)
    return {
        "n": len(rets),
        "win_rate": round(wins / len(rets), 3),
        "avg_ret_pct": round(sum(rets) / len(rets), 3),
        "small_sample": len(rets) < MIN_SAMPLE,
    }


def shadow_stats(store, since_days: float = 90,
                 cfg: dict | None = None) -> dict:
    """그림자 장부 집계 — attribution 연동."""
    since = datetime.now(timezone.utc).timestamp() - since_days * 86400
    open_n = len(store.get_open_shadow_positions())
    pending_n = len(store.get_pending_shadow_positions())
    scored = store.get_scored_shadow_positions(since=since)
    rows = [dict(r) for r in scored]

    overall = _agg_rows(rows)
    overall_out = {"n_open": open_n, "n_pending": pending_n,
                   "n_scored": overall.get("n", 0)}
    if overall.get("n"):
        overall_out.update({k: v for k, v in overall.items() if k != "n"})
        overall_out["n_scored"] = overall["n"]

    by_bucket: dict[str, list] = {}
    by_sleeve: dict[str, list] = {}
    for r in rows:
        by_bucket.setdefault(r.get("block_bucket") or "?", []).append(r)
        by_sleeve.setdefault(r.get("sleeve") or "?", []).append(r)

    vetoed_agg = _agg_rows([r for r in rows if r.get("block_status") == "vetoed"])
    from .attribution import _trade_group_id
    trade_groups: dict[int, dict] = {}
    for row in store.get_closed_positions(since=since):
        gid = _trade_group_id(row)
        g = trade_groups.setdefault(gid, {"pnl": 0.0, "cost": 0.0})
        g["pnl"] += float(row["pnl"] or 0)
        g["cost"] += (row["avg_price"] or 0) * (row["qty"] or 0)
    filled_actual: list[float] = []
    for g in trade_groups.values():
        if g["cost"]:
            filled_actual.append(g["pnl"] / g["cost"] * 100)

    filled_avg = (round(sum(filled_actual) / len(filled_actual), 3)
                  if filled_actual else None)
    vetoed_avg = vetoed_agg.get("avg_ret_pct")
    delta = (round(filled_avg - vetoed_avg, 3)
             if filled_avg is not None and vetoed_avg is not None else None)

    recent = [{
        "symbol": r["symbol"],
        "sleeve": r["sleeve"],
        "bucket": r.get("block_bucket"),
        "ret_pct": r.get("ret_pct"),
        "thesis": (r.get("thesis") or "")[:80],
    } for r in rows[:10]]

    return {
        "note": (f"반사실 페이퍼. n<{MIN_SAMPLE}(small_sample) 과신·승격 금지. "
                 "뇌/검증 자동 변경에 사용하지 말 것."),
        "overall": overall_out,
        "by_bucket": {k: _agg_rows(v) for k, v in sorted(by_bucket.items())},
        "by_sleeve": {k: _agg_rows(v) for k, v in sorted(by_sleeve.items())},
        "verifier_value_add": {
            "vetoed_avg_ret_pct": vetoed_avg,
            "filled_actual_avg_ret_pct": filled_avg,
            "delta_pp": delta,
        },
        "recent_scored": recent,
    }
