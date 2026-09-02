"""에이전트가 읽는 컴팩트 컨텍스트 조립.

market_state(시황·재무·수급·심리·매크로·뉴스) + 후보 종목 피처 + 포트폴리오 +
제약을 하나의 JSON 문자열로 만든다. '뇌'는 이걸 입력으로 받는다.
"""
from __future__ import annotations

import json
import os
import time
from collections import Counter
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ..strategies import strategy_catalog
from ..logging_setup import get_logger

log = get_logger("agents.context")

# scan: 배치 뉴스(~110건) 전부 + 여유. focus global headlines 는 select_headlines 가 macro 만.
HEADLINE_LIMIT = 200
_DEFAULT_FOCUS_MACRO_LIMIT = 12
_DEFAULT_FOCUS_MACRO_PAD = 8
_DEFAULT_HEADLINE_TTL_HOURS = 24.0
_TRIM_STATE = Path(__file__).resolve().parents[2] / "data" / "headline_trim_notify.json"
_TRIM_COOLDOWN_SEC = 6 * 3600


def infer_market_from_symbol(symbol: str | None) -> str:
    s = str(symbol or "").strip()
    if s.isdigit() and len(s) == 6:
        return "KR"
    return "US"


def classify_news_item(item: dict) -> str:
    """KR | US | macro_kr | macro_us — headlines 랭킹·시장 필터용."""
    sym = str(item.get("symbol") or "").strip()
    src = str(item.get("source") or "")
    if sym.isdigit() and len(sym) == 6:
        return "KR"
    if sym and not sym.isdigit():
        return "US"
    if "DART" in src or "dart" in src.lower():
        return "KR"
    if "Finnhub" in src:
        return "macro_us" if "/" not in src else "US"
    return "macro_kr"


def infer_wake_market(wake: dict | None, candidates: list[dict] | None,
                      triggers: list | None = None) -> str | None:
    """focus headlines 시장 필터 — wake.market > 트리거 > 후보 다수결."""
    w = wake or {}
    if w.get("market"):
        return str(w["market"]).upper()
    mkts: set[str] = set()
    for t in (triggers or w.get("triggers") or []):
        if hasattr(t, "symbol"):
            mkts.add(infer_market_from_symbol(getattr(t, "symbol", "")))
            continue
        if not isinstance(t, dict):
            continue
        if t.get("market"):
            mkts.add(str(t["market"]).upper())
        elif t.get("symbol") or t.get("stock_code"):
            mkts.add(infer_market_from_symbol(
                t.get("symbol") or t.get("stock_code")))
    if len(mkts) == 1:
        return next(iter(mkts))
    if candidates:
        counts = Counter(str(c.get("market") or "KR").upper() for c in candidates)
        if counts:
            return counts.most_common(1)[0][0]
    return None


def _parse_news_ts(item: dict) -> float | None:
    """published/pubDate/date/rcept_dt → epoch. 실패 시 None( TTL 통과 처리)."""
    for key in ("published", "pubDate", "date", "rcept_dt"):
        raw = item.get(key)
        if raw is None or raw == "":
            continue
        s = str(raw).strip()
        if len(s) >= 8 and s[:8].isdigit() and "T" not in s and " " not in s:
            try:
                dt = datetime.strptime(s[:8], "%Y%m%d").replace(tzinfo=timezone.utc)
                return dt.timestamp()
            except ValueError:
                pass
        try:
            if "T" in s or s.endswith("Z"):
                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.timestamp()
        except (TypeError, ValueError):
            pass
        try:
            dt = parsedate_to_datetime(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except (TypeError, ValueError, IndexError):
            pass
    return None


def _news_fresh(item: dict, ttl_hours: float, now_ts: float) -> bool:
    if ttl_hours <= 0:
        return True
    ts = _parse_news_ts(item)
    if ts is None:
        return True
    return (now_ts - ts) <= ttl_hours * 3600


def _compact_news_item(item: dict) -> dict:
    return {"source": item.get("source"), "title": item.get("title"),
            "symbol": item.get("symbol")}


def _dedupe_append(out: list[dict], seen: set[tuple], item: dict, cap: int) -> bool:
    """True if appended. cap 도달 시 False."""
    if len(out) >= cap:
        return False
    key = (str(item.get("title") or ""), str(item.get("source") or ""),
           str(item.get("symbol") or ""))
    if key in seen:
        return False
    seen.add(key)
    out.append(_compact_news_item(item))
    return True


def _priority_symbols(wake: dict | None, candidates: list[dict] | None) -> set[str]:
    syms: set[str] = set()
    w = wake or {}
    for t in w.get("triggers") or []:
        if isinstance(t, dict):
            s = t.get("symbol") or t.get("stock_code")
            if s:
                syms.add(str(s))
    for c in candidates or []:
        s = c.get("symbol")
        if s:
            syms.add(str(s))
    return syms


def select_headlines(news: list[dict], *,
                     tier: str = "scan",
                     limit: int | None = None,
                     wake: dict | None = None,
                     candidates: list[dict] | None = None,
                     ttl_hours: float = _DEFAULT_HEADLINE_TTL_HOURS,
                     focus_macro_pad: int = _DEFAULT_FOCUS_MACRO_PAD,
                     notify_trim: bool = True,
                     now_ts: float | None = None) -> list[dict]:
    """티어별 headlines 선택.

    scan: TTL 필터 후 배치 순서 상한(기본 200). 초과 시 notify_trim 이면 ntfy.
    focus: global 은 macro 위주(종목 뉴스는 candidates[].news·온디맨드).
      KR focus → macro_kr 우선 + macro_us pad(기본 8). US 종목 헤드라인 제외.
    """
    raw = list(news or [])
    now = time.time() if now_ts is None else now_ts
    fresh = [n for n in raw if _news_fresh(n, ttl_hours, now)]

    if tier == "focus":
        lim = _DEFAULT_FOCUS_MACRO_LIMIT if limit is None else int(limit)
        if lim <= 0:
            return []
        mkt = infer_wake_market(wake, candidates)
        pad = max(0, min(int(focus_macro_pad), lim))
        macro_kr = [n for n in fresh if classify_news_item(n) == "macro_kr"]
        macro_us = [n for n in fresh if classify_news_item(n) == "macro_us"]
        out: list[dict] = []
        seen: set[tuple] = set()
        if mkt == "US":
            primary, cross = macro_us, macro_kr
            cross_n = min(pad, max(0, lim // 4))
        elif mkt == "KR":
            primary, cross = macro_kr, macro_us
            cross_n = min(pad, lim)
        else:
            primary = macro_kr + macro_us
            cross, cross_n = [], 0
        room_primary = max(0, lim - cross_n)
        for n in primary:
            if len(out) >= room_primary:
                break
            _dedupe_append(out, seen, n, room_primary)
        for n in cross:
            if len(out) >= lim:
                break
            _dedupe_append(out, seen, n, lim)
        return out[:lim]

    lim = HEADLINE_LIMIT if limit is None else int(limit)
    if len(fresh) > lim and notify_trim:
        _notify_headline_trim(len(fresh), lim)
    return [_compact_news_item(n) for n in fresh[:lim]]


def _trim_news(news: list[dict], *, limit: int = HEADLINE_LIMIT) -> list[dict]:
    """scan-tier headlines 상한(레거시 테스트·호출 호환)."""
    return select_headlines(news, tier="scan", limit=limit)


def _notify_headline_trim(total: int, limit: int) -> None:
    """헤드라인이 한도에 잘렸을 때 로그 + ntfy(토픽 없으면 로그만). 6h 쿨다운."""
    log.warning("헤드라인 한도 초과 — 전체 %d건 중 앞 %d건만 뇌에 전달", total, limit)
    now = time.time()
    try:
        prev = json.loads(_TRIM_STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        prev = {}
    last_ts = float(prev.get("ts") or 0)
    last_total = prev.get("total")
    if last_ts and (now - last_ts) < _TRIM_COOLDOWN_SEC and last_total == total:
        return
    topic = (os.getenv("NTFY_TOPIC") or "").strip()
    if topic:
        try:
            import requests
            msg = (f"뇌 headlines 한도 초과: 전체 {total}건 → {limit}건만 전달. "
                   f"한도 상향 또는 소스 축소를 검토하세요.")
            requests.post(f"https://ntfy.sh/{topic}",
                          data=msg.encode("utf-8"),
                          headers={"Title": "Argus headlines trim"},
                          timeout=5)
        except Exception as e:
            log.warning("헤드라인 잘림 ntfy 실패: %s", e)
    try:
        _TRIM_STATE.parent.mkdir(parents=True, exist_ok=True)
        _TRIM_STATE.write_text(json.dumps(
            {"ts": now, "total": total, "limit": limit}, ensure_ascii=False),
            encoding="utf-8")
    except OSError:
        pass


def _parse_iso_epoch(raw) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (TypeError, ValueError):
        return None


def _slot_freshness(ms: dict) -> dict:
    """market_state 슬롯별 asof(있는 것만)."""
    slots: dict = {}
    regime = ms.get("regime") or {}
    if isinstance(regime, dict):
        for m, v in regime.items():
            if isinstance(v, dict) and v.get("asof"):
                slots[f"regime.{m}"] = v["asof"]
    fm = ms.get("flows_market") or {}
    if isinstance(fm, dict) and fm.get("asof"):
        slots["flows_market"] = fm["asof"]
    for key in ("macro", "macro_kr", "vkospi", "program_flows", "sentiment"):
        block = ms.get(key)
        if isinstance(block, dict) and block.get("asof"):
            slots[key] = block["asof"]
    return slots


def build_clock(markets: tuple[str, ...] = ("KR", "US"),
                now_ts: float | None = None) -> dict:
    """세션 phase + 정규장 minutes_to_close(가능할 때만)."""
    from ..market_hours import current_session, last_session_end_ts

    ts = time.time() if now_ts is None else float(now_ts)
    out: dict = {}
    for m in markets:
        phase = current_session(m, ts)
        entry: dict = {"phase": phase}
        if phase == "regular":
            end = last_session_end_ts(m, ("regular",), ts)
            if end is not None:
                entry["minutes_to_close"] = max(0, int((end - ts) // 60))
        out[m] = entry
    return out


def build_freshness(ms: dict, *,
                    strategy_scores_asof: float | None = None,
                    strategy_scores_stale: bool = False) -> dict:
    batch = ms.get("batch_asof") or ms.get("asof")
    fast = ms.get("fast_asof")
    out: dict = {
        "batch_asof": batch,
        "fast_asof": fast,
        "slots": _slot_freshness(ms or {}),
    }
    if strategy_scores_asof is not None:
        out["strategy_scores_asof"] = strategy_scores_asof
    if strategy_scores_stale:
        out["strategy_scores_stale"] = True
    return out


def build_context(market_state: dict, candidates: list[dict], portfolio: dict,
                  constraints: dict, track_record: dict | None = None,
                  recent_disclosures: list[dict] | None = None,
                  earnings_results: list[dict] | None = None,
                  focus: dict | None = None,
                  wake: dict | None = None,
                  *,
                  tier: str = "scan",
                  headline_limit: int | None = None,
                  headline_ttl_hours: float = _DEFAULT_HEADLINE_TTL_HOURS,
                  focus_macro_pad: int = _DEFAULT_FOCUS_MACRO_PAD,
                  notify_headline_trim: bool = True,
                  compact: bool = False,
                  now_ts: float | None = None,
                  strategy_scores_asof: float | None = None,
                  strategy_scores_stale: bool = False) -> str:
    """candidates: [{symbol,name,market,price,ma20,rsi,momentum,fundamentals,flows,news[],strategy_fit?}]

    track_record(선택): 라이브 성과 귀속(전략별 승률/최근 거래/결정 통계) — 뇌가 자기
    과거 판단의 실제 결과를 보고 다음 판단을 조정하게 하는 되먹임 입력.
    focus(선택): 주의층 렌즈(매크로 이벤트·수급 이상·포지셔닝 급변). 코드가 만든
    '오늘 무엇에 집중할지' — 없으면 평소처럼 regime·dossier·수급으로 판단.
    wake(선택): 이번 사이클을 깨운 사유(reason)와 트리거 요약 — periodic/vol_spike/
    regime_flip/disclosure 등. 없으면 정기 각성으로 보면 된다.
    tier: scan | focus — headlines 선택 정책.
    headline_limit: None 이면 scan=HEADLINE_LIMIT, focus=12(macro). 0 이면 focus global off.
    notify_headline_trim: focus 기본 False(config).
    compact: True 면 indent 없이 직렬화(토큰/바이트 절약, meaning 동일).
    """
    ms = market_state or {}
    ts = time.time() if now_ts is None else float(now_ts)
    if tier == "focus":
        lim = (_DEFAULT_FOCUS_MACRO_LIMIT if headline_limit is None
               else int(headline_limit))
    else:
        lim = HEADLINE_LIMIT if headline_limit is None else int(headline_limit)
    now_iso = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(
        ZoneInfo("Asia/Seoul")).isoformat()
    ctx = {
        "now": now_iso,
        "asof": ms.get("asof"),
        "clock": build_clock(now_ts=ts),
        "freshness": build_freshness(
            ms,
            strategy_scores_asof=strategy_scores_asof,
            strategy_scores_stale=strategy_scores_stale,
        ),
        "market": {
            "regime": ms.get("regime"),
            "sentiment": ms.get("sentiment"),
            "macro": ms.get("macro"),
            "macro_kr": ms.get("macro_kr"),
            "markets": ms.get("markets"),
            "sectors": ms.get("sectors"),
            "fx": ms.get("fx"),
            "flows_market": ms.get("flows_market"),
        },
        "headlines": select_headlines(
            ms.get("news", []),
            tier=tier,
            limit=lim,
            wake=wake,
            candidates=candidates,
            ttl_hours=headline_ttl_hours,
            focus_macro_pad=focus_macro_pad,
            notify_trim=notify_headline_trim,
        ),
        "strategies": strategy_catalog(),
        "candidates": candidates,
        "portfolio": portfolio,
        "constraints": constraints,
    }
    if focus:
        ctx["focus"] = focus
    if wake:
        ctx["wake"] = wake
    if track_record:
        ctx["track_record"] = track_record
    if recent_disclosures:
        ctx["recent_disclosures"] = recent_disclosures
    if earnings_results:
        ctx["earnings_results"] = earnings_results
    if compact:
        return json.dumps(ctx, ensure_ascii=False, separators=(",", ":"))
    return json.dumps(ctx, ensure_ascii=False, indent=2)
