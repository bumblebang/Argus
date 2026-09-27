"""그림자 장부 v1 — 차단된 BUY 제안의 반사실 페이퍼 추적.

체결되지 않은 BUY를 제안 시점 가격(entry)으로 페이퍼 진입해, horizon 경과 후
종가로 채점한다. 매매 경로와 분리(실패해도 사이클은 계속).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

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
# 히스토리 종가를 쓸 때 목표일과 실제 봉 날짜의 최대 간격(주말·연휴 여유). 이보다
# 멀면 CSV 가 갱신 안 된 것 — 낡은 종가로 채점하지 않는다(09-27 실측 154행 중 44행이
# 몇 주 전 종가로 채점됨).
HIST_MAX_GAP_DAYS = 5
# 채점 가격을 못 구해도 이 기간(목표일 이후)까지는 다시 시도한다(CSV 갱신 대기).
SCORE_RETRY_DAYS = 7
# 기록 시점 진입가가 같은 시각 시세 스냅샷과 이보다 벌어지면 진입가 오류로 보고
# 스냅샷 가격으로 바꾼다(09-27: 005930 진입가 64,369 — 실제 ~25만).
ENTRY_SNAPSHOT_MAX_DEV = 0.15


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
        entry_d = None
        for d, c in series:
            if d.date() <= t0:
                entry, entry_d = c, d.date()
            else:
                break
        if entry is not None and (t0 - entry_d).days <= HIST_MAX_GAP_DAYS:
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
    """단일 차단 BUY → shadow_positions. 중복이면 None.

    같은 종목·슬리브의 그림자가 아직 채점 전이면 새로 넣지 않는다 — 뇌가 매 사이클
    같은 종목을 다시 제안·차단하면 한 번의 기회가 수십 표본으로 불어나 승률이
    왜곡된다(09-27 실측: 154행 중 독립 표본 73).
    """
    if store is None or entry_price <= 0:
        return None
    has_live = getattr(store, "has_live_shadow", None)
    if has_live is not None and has_live(symbol, sleeve):
        return None
    hz = horizon or "swing"
    hdays = horizon_calendar_days(hz, cfg)
    meta = {"horizon_days": hdays, "price_source": "price_lookup"}
    if meta_extra:
        meta.update(meta_extra)
    entry_price = _checked_entry_price(store, symbol, cycle_ts, float(entry_price), meta)
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


def _checked_entry_price(store, symbol: str, ts: float, entry_price: float,
                         meta: dict) -> float:
    """진입가를 같은 시각 시세 스냅샷과 대조. 크게 어긋나면 스냅샷 가격을 쓴다.

    price_lookup 은 라이브 시세가 없을 때 캐시 일봉 종가로 채워진다 — 캐시가 낡으면
    진입가가 수십 % 틀려 그 표본의 수익률이 통째로 가짜가 된다.
    """
    snap_fn = getattr(store, "nearest_snapshot_price", None)
    if snap_fn is None or not ts:
        return entry_price
    try:
        snap = snap_fn(symbol, float(ts), window_sec=1800)
    except Exception:
        return entry_price
    if not snap or snap <= 0:
        return entry_price
    if abs(entry_price / snap - 1) <= ENTRY_SNAPSHOT_MAX_DEV:
        return entry_price
    log.warning("그림자 진입가 교정 %s: %.4f → 스냅샷 %.4f", symbol, entry_price, snap)
    meta["entry_price_rejected"] = entry_price
    meta["price_source"] = "snapshot"
    return float(snap)


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


def _csv_last_date(path: Path) -> str:
    """CSV 마지막 데이터 줄의 날짜(YYYY-MM-DD). 못 읽으면 ""."""
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 512))
            tail = f.read().decode("utf-8", errors="ignore")
    except OSError:
        return ""
    for line in reversed(tail.splitlines()):
        head = line.strip()[:10]
        if len(head) == 10 and head[4] == "-" and head[7] == "-":
            return head
    return ""


def pick_history_csv(data_dir: Path, symbol: str) -> Path | None:
    """1d history CSV — .KS/.KQ/무접미사 모두. **가장 최근까지 있는** 파일 우선,
    같으면 긴 range. range 만 보면 몇 달 전에 받은 5y 가 어제까지 있는 6mo 를 이긴다."""
    root = Path(data_dir)
    found: list[Path] = []
    for pat in (
        f"history/{symbol}.KS_1d_*.csv",
        f"history/{symbol}.KQ_1d_*.csv",
        f"history/{symbol}_1d_*.csv",
    ):
        found.extend(root.glob(pat))
    if not found:
        return None
    rank = {"6mo": 1, "1y": 2, "2y": 3, "5y": 4}

    def _key(p: Path) -> tuple[str, int, str]:
        name = p.name
        rng = 0
        if "_1d_" in name:
            rng = rank.get(name.split("_1d_")[-1].replace(".csv", ""), 0)
        return (_csv_last_date(p), rng, name)

    return max(found, key=_key)


def load_daily_series(data_dir: Path, symbol: str) -> list[tuple[datetime, float]]:
    """history CSV 에서 (datetime, close) 시계열."""
    path = pick_history_csv(data_dir, symbol)
    if path is None:
        return []
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
                           horizon_days: int,
                           *, market: str = "KR") -> float | None:
    """entry 시장 로컬 날짜 + horizon_days 캘린더, 그 이전 마지막 거래일 종가."""
    if not series:
        return None
    from zoneinfo import ZoneInfo

    from .market_hours import _SESSIONS
    tzname = _SESSIONS.get(market, ("Asia/Seoul",))[0]
    entry_dt = datetime.fromtimestamp(entry_ts, tz=ZoneInfo(tzname))
    target_date = entry_dt.date() + timedelta(days=horizon_days)
    best = None
    best_d = None
    for d, c in series:
        if d.date() <= target_date:
            best, best_d = c, d.date()
        else:
            break
    # 낡은 CSV(목표일보다 한참 전에 끝남)·진입일 이전 봉은 청산가가 아니다.
    if best_d is None or best_d <= entry_dt.date():
        return None
    if (target_date - best_d).days > HIST_MAX_GAP_DAYS:
        return None
    return best


def snap_forward(store, symbol: str, ts0: float, days: float) -> float | None:
    target = ts0 + days * 86400
    return store.nearest_snapshot_price(symbol, target, window_sec=86400 * 2 + 7200)


def score_open_shadows(store, *, now: float | None = None,
                       data_dir: Path | str = "data",
                       cfg: dict | None = None) -> dict[str, int]:
    """state=open|pending 그림자 포지션 채점. {scored, skipped, pending, cancelled} 반환."""
    # 지연 import: 상단 from .eval.trade_defs 는 eval.__init__→labels→shadow_ledger
    # 순환을 만들고, score_shadow_ledger.py(프로덕션)가 shadow를 먼저 로드하면 ImportError.
    # pytest는 알파벳순으로 src.eval을 먼저 로드해 순환을 우회한다.
    from .eval.trade_defs import roundtrip_cost_pct

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
        exit_px = exit_close_on_calendar(series, entry_ts, hdays,
                                         market=market or "KR")
        price_source = "history"
        if exit_px is None:
            exit_px = snap_forward(store, sym, entry_ts, float(hdays))
            price_source = "snapshot"
        if exit_px is None:
            # CSV 가 아직 목표일까지 안 왔을 수 있다 — 유예 기간엔 다음 채점에서 재시도.
            if now < entry_ts + (hdays + SCORE_RETRY_DAYS) * 86400:
                stats["pending"] += 1
                continue
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


def rescore_shadow_exits(store, *, data_dir: Path | str = "data",
                         cfg: dict | None = None, apply: bool = True) -> dict[str, Any]:
    """이미 scored 된 horizon 채점 행의 청산가를 지금 히스토리로 다시 구한다.

    09-27 이전 채점은 가장 긴 range CSV 를 골라, 갱신 안 된 CSV 의 마지막 종가(목표일
    몇 주 전)를 청산가로 썼다. 고친 규칙으로 다시 구해 다르면 교정하고, 지금도 목표일
    근처 종가가 없으면 검증 불가로 표본에서 뺀다(skipped). apply=False 면 집계만.
    """
    from .eval.trade_defs import roundtrip_cost_pct

    data_dir = Path(data_dir)
    out: dict[str, Any] = {"checked": 0, "fixed": 0, "voided": 0, "same": 0, "rows": []}
    cache: dict[str, list] = {}
    for row in store.get_scored_shadow_positions():
        r = dict(row)
        if r.get("exit_reason") not in ("horizon_expired", "pending_timeout"):
            continue
        try:
            entry_px = float(r["entry_price"])
            old_exit = float(r["exit_price"])
        except (TypeError, ValueError, KeyError):
            continue
        out["checked"] += 1
        sym = r["symbol"]
        market = r.get("market") or "KR"
        if sym not in cache:
            cache[sym] = load_daily_series(data_dir, sym)
        hdays = int(_row_horizon_days(r, cfg))
        new_exit = exit_close_on_calendar(cache[sym], float(r["entry_ts"]), hdays,
                                          market=market)
        if new_exit is None:
            out["voided"] += 1
            out["rows"].append({"id": r["id"], "symbol": sym, "action": "void",
                                "old_exit": old_exit})
            if apply:
                store.void_shadow_score(int(r["id"]), "stale_history")
            continue
        if abs(new_exit - old_exit) <= 1e-9 * max(1.0, abs(old_exit)):
            out["same"] += 1
            continue
        cost_pct = roundtrip_cost_pct(market, cfg) * 100.0
        new_ret = round((new_exit / entry_px - 1) * 100 - cost_pct, 3)
        out["fixed"] += 1
        out["rows"].append({"id": r["id"], "symbol": sym, "action": "fix",
                            "old_exit": old_exit, "new_exit": new_exit,
                            "old_ret": r.get("ret_pct"), "new_ret": new_ret})
        if apply:
            store.rescore_shadow_exit(int(r["id"]), exit_price=new_exit, ret_pct=new_ret)
    return out


def rescore_shadow_costs(store, *, cfg: dict | None = None) -> dict[str, int]:
    """이미 scored 된 그림자의 ret_pct 에 왕복비용을 다시 깐다.

    J11 순환 import 기간에 채점된 행은 cost=0 시절 값이다. exit/entry 가격은
    그대로 두고 ret 만 재계산한다(히스토리 재조회 없음).
    """
    from .eval.trade_defs import roundtrip_cost_pct

    updated = unchanged = skipped = 0
    for row in store.get_scored_shadow_positions():
        try:
            entry_px = float(row["entry_price"])
            exit_px = float(row["exit_price"])
        except (TypeError, ValueError, KeyError):
            skipped += 1
            continue
        if entry_px <= 0 or exit_px <= 0:
            skipped += 1
            continue
        market = "KR"
        try:
            if row["market"]:
                market = str(row["market"])
        except (KeyError, IndexError, TypeError):
            pass
        cost_pct = roundtrip_cost_pct(market, cfg) * 100.0
        new_ret = round((exit_px / entry_px - 1) * 100 - cost_pct, 3)
        old = row["ret_pct"]
        try:
            old_f = float(old) if old is not None else None
        except (TypeError, ValueError):
            old_f = None
        if old_f is not None and abs(old_f - new_ret) < 1e-9:
            unchanged += 1
            continue
        store.update_shadow_ret_pct(int(row["id"]), new_ret)
        updated += 1
    return {"updated": updated, "unchanged": unchanged, "skipped": skipped}


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


def _row_horizon_days(r: dict, cfg: dict | None) -> float:
    meta = r.get("meta")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except (TypeError, ValueError):
            meta = {}
    hd = (meta or {}).get("horizon_days") if isinstance(meta, dict) else None
    try:
        return float(hd) if hd else float(horizon_calendar_days(r.get("horizon"), cfg))
    except (TypeError, ValueError):
        return float(horizon_calendar_days(r.get("horizon"), cfg))


def dedupe_episodes(rows: list[dict], cfg: dict | None = None) -> list[dict]:
    """같은 종목·슬리브가 첫 행의 보유기간 안에 또 기록된 행은 접는다(첫 행만 표본).

    기록 시점 중복 차단 이전에 쌓인 행을 집계에서 바로잡기 위한 것 — DB 는 건드리지 않는다.
    """
    out: list[dict] = []
    until: dict[tuple, float] = {}
    for r in sorted(rows, key=lambda x: float(x.get("entry_ts") or x.get("cycle_ts") or 0)):
        key = (r.get("symbol"), r.get("sleeve"))
        ts = float(r.get("entry_ts") or r.get("cycle_ts") or 0)
        if ts < until.get(key, float("-inf")):
            continue
        until[key] = ts + _row_horizon_days(r, cfg) * 86400
        out.append(r)
    return out


# 가격 오류 격리 — 09-27 실측 005930 진입가 64,369(실제 ~25만) → +285% 가 평균을 지배.
OUTLIER_RET_PCT = 50.0


def shadow_stats(store, since_days: float = 90,
                 cfg: dict | None = None) -> dict:
    """그림자 장부 집계 — attribution 연동.

    표본 = 에피소드(종목·슬리브·보유기간) 단위. |ret_pct| > OUTLIER_RET_PCT 는 가격
    오류로 보고 집계에서 빼고 건수만 보고한다.
    """
    since = datetime.now(timezone.utc).timestamp() - since_days * 86400
    open_n = len(store.get_open_shadow_positions())
    pending_n = len(store.get_pending_shadow_positions())
    scored = store.get_scored_shadow_positions(since=since)
    raw_rows = [dict(r) for r in scored]
    episodes = dedupe_episodes(raw_rows, cfg)
    outliers = [r for r in episodes
                if r.get("ret_pct") is not None and abs(float(r["ret_pct"])) > OUTLIER_RET_PCT]
    rows = [r for r in episodes if r not in outliers]

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
    } for r in sorted(rows, key=lambda x: float(x.get("scored_at") or 0),
                      reverse=True)[:10]]

    skipped = {}
    if hasattr(store, "count_shadow_skipped"):
        skipped = store.count_shadow_skipped(since=since)

    return {
        "note": (f"반사실 페이퍼. n<{MIN_SAMPLE}(small_sample) 과신·승격 금지. "
                 "뇌/검증 자동 변경에 사용하지 말 것. 표본=종목 에피소드(중복 접음)."),
        "sample": {
            "rows_raw": len(raw_rows),
            "episodes": len(episodes),
            "outliers_excluded": [
                {"symbol": r.get("symbol"), "ret_pct": r.get("ret_pct"),
                 "entry_price": r.get("entry_price")} for r in outliers],
        },
        "overall": overall_out,
        "by_bucket": {k: _agg_rows(v) for k, v in sorted(by_bucket.items())},
        "by_sleeve": {k: _agg_rows(v) for k, v in sorted(by_sleeve.items())},
        "skipped": skipped,
        "verifier_value_add": {
            "vetoed_avg_ret_pct": vetoed_avg,
            "filled_actual_avg_ret_pct": filled_avg,
            "delta_pp": delta,
        },
        "recent_scored": recent,
    }
