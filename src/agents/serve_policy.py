"""뇌 데이터 서빙 정책 — 정기(scan) vs 이벤트(focus).

설계 고정 (API 비용):
  - 느린 슬롯(fundamentals / 종목 flows / positioning / sectors / macro / RSS·DART news)을
    유니버스 전량·장중 주기로 돌리면 콜·시간이 폭증한다.
    예: FlowsSource 심볼당 네이버 1콜 + ~0.15s → 30~50종 = 수 초~십수 초·수십 콜/회.
    EDGAR/DART 재무는 티커당 다콜인데 일중 거의 안 바뀐다. KRX 확장은 쿼터 민감.
  - 따라서 전량 느린 재빌드는 하지 않는다. 장중 신선도는 기존 빠른 슬라이스
    (regime/sentiment/markets/flows_market) + 배치 2회/일에 맡긴다.
  - focus(이벤트) shortlist(보유∪wake, 상한~십수)에만 FlowsSource(+옵션 종목뉴스 KR·US)
    온디맨드 — 콜 ≈ shortlist 크기, 토스 Gateway 미사용, market_state 파일 미오염.

토큰:
  - scan(정기: periodic/extra/athena_done): scan shortlist(~scan_cap) — must(held∪armed∪bullish) + pad(scores).
  - focus(이벤트): day_pool 전량 제외가 본체 — 보유∪wake(+pad)만 뇌에 실.
"""
from __future__ import annotations

from typing import Any, Iterable

# 정기 각성 — scan tier(느린 슬롯 전량 vs shortlist 분기).
SCAN_REASONS = frozenset({
    "periodic", "extra", "athena_done", "gap_rebound_scan", "nxt_gap_scan", "",
})

# 이벤트 — 기본 focus. 설정 agents.serve.focus_reasons 로 덮어쓸 수 있다.
DEFAULT_FOCUS_REASONS = frozenset({
    "wake_triggers", "disclosure", "earnings_result", "movers", "act_triggers",
})

# coalesce 시 reason 우선순위(높을수록 채택). 동점이면 기존 유지 후 "+" 조인하지 않음 —
# 트리거 합집합이 재료이고, reason 은 대표 1개면 충분.
_REASON_PRIORITY: dict[str, int] = {
    "disclosure": 100,
    "earnings_result": 90,
    "wake_triggers": 80,
    "act_triggers": 75,
    "movers": 70,
    "gap_rebound_scan": 65,
    "nxt_gap_scan": 65,
    "extra": 20,
    "periodic": 10,
    "athena_done": 5,
}


def serve_cfg(agents_cfg: dict | None) -> dict:
    """agents.serve 블록 + 기본값."""
    raw = (agents_cfg or {}).get("serve") or {}
    focus_reasons = raw.get("focus_reasons")
    if focus_reasons is None:
        fr = set(DEFAULT_FOCUS_REASONS)
    else:
        fr = {str(x) for x in focus_reasons}
    return {
        "enabled": bool(raw.get("enabled", True)),
        "focus_cap": int(raw.get("focus_cap", 16)),
        "focus_pad": int(raw.get("focus_pad", 0)),
        "ondemand_flows": bool(raw.get("ondemand_flows", True)),
        "ondemand_news": bool(raw.get("ondemand_news", False)),
        "focus_headline_limit": (
            int(raw["focus_headline_limit"])
            if raw.get("focus_headline_limit") is not None else None),
        "focus_reasons": fr,
        "compact_json": bool(raw.get("compact_json", True)),
        "enrich_fundamentals": bool(raw.get("enrich_fundamentals", True)),
        "enrich_flows": bool(raw.get("enrich_flows", True)),
        "gap_enrich_max": int(raw.get("gap_enrich_max", 25)),
        "patch_missing_fundamentals_max": int(raw.get("patch_missing_fundamentals_max", 5)),
        "scan_enabled": bool(raw.get("scan_enabled", True)),
        "scan_cap": int(raw.get("scan_cap", 40)),
    }


def scan_shortlist_exempt(wake: dict | None) -> bool:
    """갭반등 각성 등 — pool 선별 후이므로 scan_cap 을 씌우지 않는다."""
    from .features import wake_has_gap_scan

    return wake_has_gap_scan(str((wake or {}).get("reason") or ""))


def classify_tier(wake: dict | None, *, cfg: dict | None = None) -> str:
    """wake.reason → 'scan' | 'focus'.

    serve.enabled=false 이면 항상 scan(현행 경로).
    reason 이 focus_reasons 에 있으면 focus, SCAN_REASONS 또는 미지·빈 값은 scan.
    복합 reason('disclosure+wake_triggers')은 focus 토큰이 하나라도 있으면 focus.
    """
    c = cfg or serve_cfg(None)
    if not c.get("enabled", True):
        return "scan"
    reason = str((wake or {}).get("reason") or "").strip()
    parts = [p.strip() for p in reason.replace("|", "+").split("+") if p.strip()]
    if not parts:
        # triggers 만 있고 reason 빈 경우 — 이벤트 취급
        if wake and (wake.get("triggers") or wake.get("n")):
            return "focus"
        return "scan"
    focus_set = c.get("focus_reasons") or DEFAULT_FOCUS_REASONS
    if any(p in focus_set for p in parts):
        return "focus"
    if all(p in SCAN_REASONS for p in parts):
        return "scan"
    # 미등록 reason: triggers 있으면 focus, 없으면 scan(보수적 전량)
    if wake and wake.get("triggers"):
        return "focus"
    return "scan"


def wake_symbols(wake: dict | None) -> list[str]:
    """wake.triggers 에서 심볼 추출(순서 유지, 중복 제거)."""
    out: list[str] = []
    seen: set[str] = set()
    for t in (wake or {}).get("triggers") or []:
        sym = None
        if isinstance(t, dict):
            sym = t.get("symbol") or t.get("stock_code")
        else:
            sym = getattr(t, "symbol", None)
        if sym is None:
            continue
        s = str(sym).strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def held_symbols(positions: Iterable[Any] | dict | None) -> list[str]:
    """열린 포지션 심볼. positions 는 account.positions dict 또는 [{symbol}, ...]."""
    out: list[str] = []
    seen: set[str] = set()
    if positions is None:
        return out
    if isinstance(positions, dict):
        for sym, p in positions.items():
            if p is not None and hasattr(p, "is_open") and not p.is_open:
                continue
            s = str(sym).strip()
            if s and s not in seen:
                seen.add(s)
                out.append(s)
        return out
    for p in positions:
        if isinstance(p, dict):
            s = str(p.get("symbol") or "").strip()
        else:
            s = str(getattr(p, "symbol", "") or "").strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _market_hint_from_wake(wake: dict | None, symbol: str) -> str:
    """wake.triggers 에 실린 market 힌트(없으면 KR 6자리 / 그 외 US)."""
    for t in (wake or {}).get("triggers") or []:
        if not isinstance(t, dict):
            continue
        sym = str(t.get("symbol") or t.get("stock_code") or "").strip()
        if sym != symbol:
            continue
        m = t.get("market")
        if m:
            return str(m).upper()
    if symbol.isdigit() and len(symbol) == 6:
        return "KR"
    return "US"


def select_scan_candidates(
    items: list[dict],
    *,
    held: list[str] | None = None,
    armed: list[str] | None = None,
    bullish: list[str] | None = None,
    scores: dict[str, dict] | None = None,
    cfg: dict | None = None,
) -> list[dict]:
    """scan tier shortlist — must(held∪armed∪bullish∩universe) + pad(strategy_scores 순).

    must 가 scan_cap 을 넘으면 must 전원 유지, pad 만 trim.
    """
    from ..strategy_scores import pad_score

    c = cfg or serve_cfg(None)
    cap = max(0, int(c.get("scan_cap", 40)))
    by_sym = {str(it.get("symbol")): it for it in items if it.get("symbol") is not None}
    uni_syms = set(by_sym)

    must: list[str] = []
    seen: set[str] = set()
    for s in list(held or []) + list(armed or []):
        if s and s not in seen:
            seen.add(s)
            must.append(s)
    for s in bullish or []:
        if s and s not in seen and s in uni_syms:
            seen.add(s)
            must.append(s)

    chosen: list[dict] = []
    chosen_syms: set[str] = set()
    for s in must:
        it = by_sym.get(s)
        if it is not None:
            row = dict(it)
            row["serve_must"] = True
            chosen.append(row)
            chosen_syms.add(s)
        else:
            mkt = "KR" if s.isdigit() and len(s) == 6 else "US"
            chosen.append({
                "symbol": s, "name": s, "market": mkt,
                "serve_must": True, "serve_stub": True, "force_include": True,
            })
            chosen_syms.add(s)

    ranked_pad = sorted(
        (s for s in by_sym if s not in chosen_syms),
        key=lambda sym: pad_score(scores or {}, sym),
        reverse=True,
    )
    if cap:
        room = max(0, cap - len(chosen))
    else:
        room = len(ranked_pad)
    for s in ranked_pad[:room]:
        chosen.append(by_sym[s])
        chosen_syms.add(s)

    return chosen


def select_candidates(items: list[dict], wake: dict | None, *,
                      held: list[str] | None = None,
                      armed: list[str] | None = None,
                      bullish: list[str] | None = None,
                      scores: dict[str, dict] | None = None,
                      cfg: dict | None = None,
                      tier: str | None = None) -> tuple[list[dict], str]:
    """티어에 따라 items 를 그대로 또는 focus shortlist 로 반환.

    반환: (items_out, tier).
    focus: 필수 = held ∪ wake 심볼 → pad → focus_cap.
    items 에 없는 필수 심볼은 stub(force_include)로 넣어 뇌에 항상 보이게 한다.
    필수만으로 cap 초과 시 필수 전원 유지(캡은 pad 에만 적용).
    """
    c = cfg or serve_cfg(None)
    t = tier or classify_tier(wake, cfg=c)
    if t != "focus":
        if (t == "scan" and c.get("scan_enabled", True)
                and not scan_shortlist_exempt(wake)):
            return select_scan_candidates(
                items, held=held, armed=armed, bullish=bullish,
                scores=scores, cfg=c), t
        return list(items), t

    must = []
    seen: set[str] = set()
    for s in list(held or []) + wake_symbols(wake):
        if s not in seen:
            seen.add(s)
            must.append(s)

    by_sym = {str(it.get("symbol")): it for it in items if it.get("symbol") is not None}
    chosen: list[dict] = []
    chosen_syms: set[str] = set()
    for s in must:
        it = by_sym.get(s)
        if it is not None:
            # 필수 종목은 assemble 의 buy_ineligible 탈락을 막는다(공시/트리거 재평가).
            row = dict(it)
            row["force_include"] = True
            chosen.append(row)
            chosen_syms.add(s)
        else:
            mkt = _market_hint_from_wake(wake, s)
            chosen.append({
                "symbol": s, "name": s, "market": mkt,
                "force_include": True, "serve_stub": True,
            })
            chosen_syms.add(s)

    cap = max(0, int(c.get("focus_cap", 16)))
    pad = max(0, int(c.get("focus_pad", 0)))
    must_set = set(must)
    # pad: 원본 순서대로 미포함 종목. room = cap - len(must포함분) (cap=0 이면 pad만).
    if cap:
        room = max(0, cap - len(chosen))
        pad_n = min(pad, room)
    else:
        pad_n = pad
    added = 0
    for it in items:
        if added >= pad_n:
            break
        s = str(it.get("symbol"))
        if s in chosen_syms:
            continue
        chosen.append(it)
        chosen_syms.add(s)
        added += 1

    # 필수(must)는 절대 자르지 않음. pad 분이 cap 을 넘기면 pad 만 trim.
    if cap and len(chosen) > cap:
        must_items = [it for it in chosen if str(it.get("symbol")) in must_set]
        pad_items = [it for it in chosen if str(it.get("symbol")) not in must_set]
        room = max(0, cap - len(must_items))
        chosen = must_items + pad_items[:room]

    return chosen, t


def reason_priority(reason: str) -> int:
    parts = [p.strip() for p in str(reason or "").replace("|", "+").split("+") if p.strip()]
    if not parts:
        return 0
    return max(_REASON_PRIORITY.get(p, 40) for p in parts)


def merge_wake_pending(prev: dict | None, reason: str,
                       triggers_serialized: list[dict]) -> dict:
    """BrainWorker coalesce: 덮어쓰기 대신 reason·triggers 합집합.

    triggers 는 (kind, symbol) 키로 dedupe(나중 값이 이김). reason 은 우선순위 높은 쪽.
    둘 다 이벤트급이면 높은 쪽만 남기되, 낮은 쪽이 다른 계열이면 'a+b' 조인.
    """
    new_reason = reason or ""
    new_tr = list(triggers_serialized or [])
    if not prev:
        return {"reason": new_reason, "n": len(new_tr), "triggers": new_tr}

    old_reason = str(prev.get("reason") or "")
    old_tr = list(prev.get("triggers") or [])
    merged = _dedupe_triggers(old_tr + new_tr)

    pr_old, pr_new = reason_priority(old_reason), reason_priority(new_reason)
    if not old_reason:
        picked = new_reason
    elif not new_reason:
        picked = old_reason
    elif pr_new > pr_old:
        # 다른 계열이면 조인(재료 유실 방지용 표기)
        if old_reason and old_reason not in new_reason and pr_old >= 70:
            picked = f"{new_reason}+{old_reason}"
        else:
            picked = new_reason
    elif pr_old > pr_new:
        if new_reason and new_reason not in old_reason and pr_new >= 70:
            picked = f"{old_reason}+{new_reason}"
        else:
            picked = old_reason
    else:
        if new_reason and new_reason != old_reason:
            picked = f"{old_reason}+{new_reason}" if old_reason else new_reason
        else:
            picked = old_reason or new_reason

    return {"reason": picked, "n": len(merged), "triggers": merged}


def _dedupe_triggers(rows: list[dict]) -> list[dict]:
    out: list[dict] = []
    idx: dict[tuple, int] = {}
    for t in rows:
        if not isinstance(t, dict):
            out.append(t)  # type: ignore[arg-type]
            continue
        kind = str(t.get("kind") or t.get("report_nm") or "")
        sym = str(t.get("symbol") or t.get("stock_code") or "")
        key = (kind, sym)
        if key in idx and (kind or sym):
            out[idx[key]] = t
        else:
            idx[key] = len(out)
            out.append(t)
    return out


def patch_candidate_flows(candidates: list[dict], flows_by_sym: dict[str, dict]) -> int:
    """assemble 이후 후보 dict 에만 flows 덮어쓰기. 반환: 패치된 종목 수."""
    n = 0
    for c in candidates:
        sym = str(c.get("symbol") or "")
        if sym and sym in flows_by_sym:
            c["flows"] = flows_by_sym[sym]
            n += 1
    return n


def patch_candidate_news(candidates: list[dict], news_by_sym: dict[str, list]) -> int:
    n = 0
    for c in candidates:
        sym = str(c.get("symbol") or "")
        if sym and sym in news_by_sym and news_by_sym[sym]:
            c["news"] = news_by_sym[sym][:5]
            n += 1
    return n


def fetch_ondemand_flows(symbols: list[str], *, dry: bool = False) -> dict[str, dict]:
    """KR 종목만 FlowsSource. 실패·빈 심볼은 생략. 파일 기록 없음."""
    kr = [s for s in symbols if s and str(s).isdigit() and len(str(s)) == 6]
    if not kr:
        return {}
    from ..datasources.flows import FlowsSource
    from ..datasources.base import SourceContext
    src = FlowsSource(kr)
    raw = src.fetch(SourceContext(client=None, symbols_by_market={}, dry=dry))
    flows = raw.get("flows") or {}
    return {str(k): v for k, v in flows.items() if isinstance(v, dict)}


def fetch_ondemand_news(
    symbols: list[str],
    *,
    per: int = 3,
    finnhub_key: str | None = None,
    spacing_sec: float = 1.1,
) -> dict[str, list]:
    """종목 헤드라인 온디맨드. KR=네이버, US=Finnhub company-news. 실패 심볼 생략.

    finnhub_key 가 None 이면 FINNHUB_API_KEY 환경변수. 키 없으면 US 는 스킵.
    spacing_sec 는 US 연속 콜 pacing(무료 60콜/분).
    """
    import os
    import time

    from ..datasources.finnhub import fetch_company_news
    from ..datasources.news import fetch_kr_stock_news

    key = (finnhub_key if finnhub_key is not None
           else (os.getenv("FINNHUB_API_KEY") or "").strip())
    out: dict[str, list] = {}
    us: list[str] = []
    for s in symbols:
        if not s:
            continue
        sym = str(s)
        if sym.isdigit() and len(sym) == 6:
            rows = fetch_kr_stock_news(sym, per=per)
            if rows:
                out[sym] = [{"source": r.get("source"), "title": r.get("title"),
                             "symbol": sym} for r in rows]
        else:
            us.append(sym)
    if not key:
        return out
    for i, sym in enumerate(us):
        rows = fetch_company_news(key, sym, per=per)
        if rows:
            out[sym] = [{"source": r.get("source"), "title": r.get("title"),
                         "symbol": sym, "date": r.get("date")} for r in rows]
        if spacing_sec and i < len(us) - 1:
            time.sleep(spacing_sec)
    return out
