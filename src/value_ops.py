"""밸류 트랙 S1–S4 공통 헬퍼 — Score 병기·자카드·쿨다운·슬롯/엔트리 캡."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Iterable

from .config import ROOT
from .logging_setup import get_logger
from .value_score import (
    DEFAULT_AGE_DECAY_FLOOR, DEFAULT_DD_DEEP_FLOOR, DEFAULT_DD_PEAK,
    DEFAULT_HALF_LIFE_DAYS, DEFAULT_QUINTILE_N_MIN, DEFAULT_WEIGHTS,
    composite_scores,
)

log = get_logger("value_ops")

JACCARD_PATH = ROOT / "data" / "value_top10_jaccard.json"
COOLDOWN_PATH = ROOT / "data" / "value_trade_cooldown.json"
FUNNEL_PATH = ROOT / "data" / "value_funnel.jsonl"


def _load_json(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log.warning("[value_ops] load %s 실패: %s", path.name, e)
    return default


def _save_json(path: Path, data) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)
    except OSError as e:
        log.warning("[value_ops] save %s 실패: %s", path.name, e)


def watchlist_rows_for_score(watchlist: dict) -> list[dict]:
    """watchlist → composite_scores 입력 rows."""
    rows = []
    for sym, e in (watchlist or {}).items():
        if not isinstance(e, dict):
            continue
        metrics = e.get("metrics") if isinstance(e.get("metrics"), dict) else {}
        fund = e.get("fundamentals") if isinstance(e.get("fundamentals"), dict) else {}
        mcap = metrics.get("market_cap")
        if mcap is None:
            mcap = fund.get("market_cap")
        if mcap is None and fund.get("market_cap_busd") is not None:
            # US Yahoo 키 — 시장별 분위라 단위(B USD) 그대로 상대화
            try:
                mcap = float(fund["market_cap_busd"])
            except (TypeError, ValueError):
                mcap = None
        rows.append({
            "symbol": sym,
            "market": e.get("market"),
            "market_cap": mcap,
            "drawdown_1y_pct": metrics.get("drawdown_1y_pct"),
            "first_seen_at": e.get("first_seen_at"),
            "fundamentals": fund,
            "stance": e.get("stance"),
        })
    return rows


def annotate_watchlist_scores(
    watchlist: dict,
    *,
    now: float,
    n_min: int = DEFAULT_QUINTILE_N_MIN,
    weights: dict | None = None,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
    age_decay_floor: float = DEFAULT_AGE_DECAY_FLOOR,
    dd_peak: tuple[float, float] = DEFAULT_DD_PEAK,
    dd_deep_floor: float = DEFAULT_DD_DEEP_FLOOR,
) -> dict:
    """watchlist 항목에 value_factor/composite_value 등 병기. 정렬은 호출측 몫.

    시총분위는 시장(KR/US)별로 분리 — 통화·단위 혼입 방지.
    """
    rows = watchlist_rows_for_score(watchlist)
    if not rows:
        return watchlist
    by_mkt: dict[str, list[dict]] = {}
    for r in rows:
        by_mkt.setdefault(str(r.get("market") or "_"), []).append(r)
    for mkt_rows in by_mkt.values():
        scored = composite_scores(
            mkt_rows, now=now, weights=weights or DEFAULT_WEIGHTS,
            half_life_days=half_life_days, age_decay_floor=age_decay_floor,
            n_min=n_min, dd_peak=dd_peak, dd_deep_floor=dd_deep_floor)
        for r in scored:
            sym = r["symbol"]
            if sym not in watchlist:
                continue
            for k in ("value_factor", "quality_tilt", "dd_component",
                      "age_decay", "composite_value"):
                watchlist[sym][k] = r.get(k)
    return watchlist


def jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def top_n_symbols(watchlist: dict, *, n: int = 10, market: str | None = None,
                  key: str = "composite_value") -> list[str]:
    items = []
    for sym, e in (watchlist or {}).items():
        if not isinstance(e, dict) or e.get("stance") != "undervalued":
            continue
        if market and e.get("market") != market:
            continue
        items.append((float(e.get(key) or 0), sym))
    items.sort(reverse=True)
    return [s for _, s in items[:n]]


def _kst_date(ts: float) -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.fromtimestamp(float(ts), ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d")


def update_jaccard_state(
    watchlist: dict,
    *,
    market: str,
    now: float,
    n: int = 10,
    path: Path = JACCARD_PATH,
) -> dict:
    """상위 N 자카드 갱신 — 기준선은 **하루 단위**.

    스캔은 하루 2~3회 도는데 매 스캔마다 기준선을 갱신하면 '몇 시간 전 대비'가 되어
    거의 안 바뀐 것처럼 보인다. 이 지표를 넣은 이유가 "같은 종목이 계속 상위를
    차지하는가"라서, 기준선은 **전일 마지막 스냅샷**으로 고정한다.

    반환 {jaccard, prev, curr, baseline_date}.
    """
    state = _load_json(path, {})
    curr = top_n_symbols(watchlist, n=n, market=market, key="composite_value")
    # composite 전부 0이면 conviction 폴백(S1 초기)
    if not any((watchlist.get(s) or {}).get("composite_value") for s in curr):
        curr = top_n_symbols(watchlist, n=n, market=market, key="conviction")

    today = _kst_date(now)
    base_key, base_date_key = f"{market}_baseline", f"{market}_baseline_date"
    latest_key = f"{market}_symbols"
    baseline = list(state.get(base_key) or [])
    seeded = False
    if state.get(base_date_key) != today:
        # 날짜가 바뀌었다 — 전일 마지막 스냅샷을 오늘의 기준선으로 승격.
        prior = state.get(latest_key)
        if prior:
            baseline = list(prior)
        else:                       # 최초 실행 — 오늘 첫 스냅샷을 기준선으로 심는다
            baseline = list(curr)
            seeded = True
        state[base_key] = baseline
        state[base_date_key] = today
    jac = None if (seeded or not baseline) else jaccard(baseline, curr)
    state[latest_key] = curr
    state[f"{market}_ts"] = now
    state[f"{market}_jaccard"] = jac
    _save_json(path, state)
    return {"jaccard": jac, "prev": baseline, "curr": curr, "baseline_date": today}


def append_funnel(event: dict, path: Path = FUNNEL_PATH) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError as e:
        log.warning("[value_ops] funnel append 실패: %s", e)


def load_cooldown(path: Path | None = None) -> dict:
    return _load_json(path or COOLDOWN_PATH, {})


def save_cooldown(data: dict, path: Path | None = None) -> None:
    _save_json(path or COOLDOWN_PATH, data)


def in_cooldown(cooldown: dict, symbol: str, now: float) -> bool:
    ent = cooldown.get(symbol) or {}
    until = ent.get("until")
    return until is not None and float(until) > now


def record_cooldown_event(
    cooldown: dict,
    symbol: str,
    *,
    kind: str,
    now: float,
    hold_n: int = 3,
    cool_days: float = 5.0,
    streak_ttl_days: float | None = None,
) -> dict:
    """화이트리스트 이벤트 가산. N회면 until 설정.

    의도는 "요즘 **연속으로** 안 되는 종목을 잠시 쉬게 한다"이다. 그래서 마지막
    이벤트가 streak_ttl_days(기본=cool_days)보다 오래됐으면 연속이 끊긴 것으로 보고
    카운트를 0에서 다시 센다 — 안 그러면 한 달에 한 번씩 걸린 종목이 언젠가 유배된다.
    """
    ent = dict(cooldown.get(symbol) or {})
    ttl = float(cool_days if streak_ttl_days is None else streak_ttl_days)
    last = ent.get("last_ts")
    if last is not None and ttl > 0 and (float(now) - float(last)) > ttl * 86400:
        ent["streak"] = 0
    streak = int(ent.get("streak") or 0) + 1
    ent["streak"] = streak
    ent["last_kind"] = kind
    ent["last_ts"] = now
    if streak >= hold_n:
        ent["until"] = now + cool_days * 86400
        ent["streak"] = 0
    cooldown[symbol] = ent
    return cooldown


def reset_cooldown_streak(cooldown: dict, symbol: str) -> dict:
    ent = dict(cooldown.get(symbol) or {})
    ent["streak"] = 0
    cooldown[symbol] = ent
    return cooldown


# 검증(LLM validation) 거부만 쿨다운. gate_rejected·no_price 등 하드게이트는 제외.
COOLDOWN_REJECT_STATUSES = frozenset({"vetoed"})


def apply_post_cycle_cooldown(
    executed: list[dict] | None,
    *,
    now: float,
    hold_n: int = 3,
    cool_days: float = 5.0,
    streak_ttl_days: float | None = None,
    path: Path | None = None,
) -> dict:
    """체결 시 streak 리셋, 화이트리스트 reject 시만 가산.

    path 는 호출측(ValueRunner)이 state 디렉터리 기준으로 넘긴다 — 기본값(모듈 상수)을
    쓰면 테스트가 라이브 data/ 를 덮어쓴다.
    """
    cd = load_cooldown(path)
    for e in executed or []:
        if e.get("action") != "BUY":
            continue
        sym = e.get("symbol")
        if not sym:
            continue
        st = e.get("status")
        if st in ("filled", "partial"):
            reset_cooldown_streak(cd, sym)
        elif st in COOLDOWN_REJECT_STATUSES:
            record_cooldown_event(
                cd, sym, kind="reject_veto", now=now,
                hold_n=hold_n, cool_days=cool_days,
                streak_ttl_days=streak_ttl_days)
    save_cooldown(cd, path)
    return cd


def effective_new_entries_cap(
    *,
    ceiling: int,
    remaining_slots: int | None,
    sleeve_room: float,
    min_ticket: float,
) -> int:
    """정상 티켓 기준 신규 진입 상한.

    remaining_slots=None 이면 칸 제약 없음(개수는 자본이 정한다).
    min_ticket<=0 또는 room 부족 → 0.
    """
    if ceiling <= 0:
        return 0
    if remaining_slots is not None and remaining_slots <= 0:
        return 0
    if min_ticket <= 0 or sleeve_room < min_ticket:
        return 0
    by_room = int(sleeve_room // min_ticket)
    caps = [int(ceiling), by_room]
    if remaining_slots is not None:
        caps.append(int(remaining_slots))
    return max(0, min(caps))


def truncate_buys_by_score(
    proposals: list,
    gated: list[dict],
    *,
    cap: int,
) -> tuple[list, list[dict]]:
    """BUY proposals 를 composite_value 순으로 cap개만 남김.

    반환 (kept_proposals, drop_log) — drop_log에 code_rank/llm_rank 병기.
    """
    score_by = {c["symbol"]: float(c.get("composite_value") or 0) for c in gated}
    buys = [p for p in proposals if getattr(p, "side", None) == "BUY"]
    holds = [p for p in proposals if getattr(p, "side", None) != "BUY"]
    # LLM 순서 = proposals 내 BUY 등장 순
    llm_rank = {getattr(p, "symbol", None): i + 1 for i, p in enumerate(buys)}
    buys_sorted = sorted(
        buys,
        key=lambda p: score_by.get(getattr(p, "symbol", ""), 0.0),
        reverse=True,
    )
    kept = buys_sorted[: max(0, cap)]
    dropped = buys_sorted[max(0, cap):]
    drop_log = []
    for i, p in enumerate(dropped):
        sym = getattr(p, "symbol", None)
        drop_log.append({
            "symbol": sym,
            "code_pick_rank": i + 1 + len(kept),
            "llm_proposal_rank": llm_rank.get(sym),
            "composite_value": score_by.get(sym),
            "reason": "new_entries_cap",
        })
    # kept 도 code rank 기록용으로 정렬 유지; holds 뒤에 붙이지 않고 BUY+HOLD
    return kept + holds, drop_log


def apply_first_seen(
    entry: dict,
    *,
    stance: str,
    now: float,
    clear_after_days: float = 3.0,
) -> dict:
    """undervalued 최초 시각 보존. 이탈이 clear_after_days 지속 시에만 클리어."""
    e = dict(entry)
    if stance == "undervalued":
        if e.get("first_seen_at") is None:
            e["first_seen_at"] = now
        e.pop("left_undervalued_at", None)
    else:
        left = e.get("left_undervalued_at")
        if left is None:
            e["left_undervalued_at"] = now
        elif e.get("first_seen_at") is not None and (
                now - float(left)) >= clear_after_days * 86400:
            e.pop("first_seen_at", None)
            e.pop("left_undervalued_at", None)
    return e
