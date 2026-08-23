"""주의층(focus) — 사실층 숫자를 '오늘 무엇에 집중할지'로 압축한다.

뇌(LLM)는 평탄한 market_state 만으로는 FOMC D-2·외국인 이상 수급을 스스로
렌즈로 승격하지 못한다. 이 모듈이 결정적으로 lenses[] 를 만들고, 프롬프트가
그 순서로 읽게 한다. 하드게이트는 만들지 않는다(실적 캘린더와 동일 철학).

호출: CycleRunner 가 build_context 직전 — dday 가 매번 신선해야 하므로
배치 JSON 에 focus 를 고정 저장하지 않는다.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

from .datasources.earnings import dday_of
from .logging_setup import get_logger

log = get_logger("src.focus")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
MACRO_CAL_PATH = DATA_DIR / "macro_calendar.json"

# 매크로 이벤트 렌즈 창 — 실적(-3..21)보다 짧게(임박만 의미).
MACRO_DDAY_MIN, MACRO_DDAY_MAX = -5, 1
# D-1~D+1 은 priority=high.
MACRO_HIGH_DDAY = {-1, 0, 1}

# 섹터 → 매크로 민감 태그(정적). LLM 이 만들지 않는다.
_SECTOR_TAGS: dict[str, tuple[str, ...]] = {
    "은행": ("rate_sensitive", "domestic"),
    "증권": ("rate_sensitive", "domestic"),
    "보험": ("rate_sensitive", "domestic"),
    "부동산": ("rate_sensitive", "domestic"),
    "건설": ("rate_sensitive", "domestic"),
    "유틸리티": ("rate_sensitive", "domestic"),
    "유틸": ("rate_sensitive", "domestic"),
    "반도체": ("export",),
    "IT": ("export",),
    "하드웨어": ("export",),
    "자동차": ("export",),
    "배터리": ("export",),
    "화학": ("export",),
    "철강": ("export",),
    "조선": ("export",),
    "내수": ("domestic",),
    "유통": ("domestic",),
    "음식료": ("domestic",),
    "통신": ("domestic",),
}

_MACRO_HINTS: dict[str, str] = {
    "fomc": "금리·달러·VIX·외국인 수급을 먼저 교차. 갭 추격 경계. 확신 상한 권고.",
    "bok_mpc": "한국 기준금리·국고채·원달러·국내 수급을 먼저 교차. 금리민감 업종 확신을 조절.",
    "cpi_us": "미국 물가→금리 경로 재가격. VIX·달러·성장주 민감도 점검.",
    "cpi_kr": "국내 물가→금통위 경로. macro_kr CPI·금리민감 업종을 교차.",
    "nfp": "고용→금리 경로. 미장 방향·VIX를 KR 배경으로 깔아라.",
}

_MACRO_READ = ["macro", "macro_kr", "sentiment.vix", "markets", "fx", "flows_market"]


def macro_tags_for_sector(sector: str | None) -> list[str]:
    """universe sector 문자열 → macro_tags. 매칭 없으면 []."""
    if not sector:
        return []
    s = str(sector).strip()
    if s in _SECTOR_TAGS:
        return list(_SECTOR_TAGS[s])
    for key, tags in _SECTOR_TAGS.items():
        if key in s:
            return list(tags)
    return []


def attach_macro_tags(candidates: list[dict],
                      sector_map: dict[str, str] | None = None) -> None:
    """후보에 macro_tags 를 in-place 부착(있으면). sector 는 후보 또는 sector_map."""
    sm = sector_map or {}
    for c in candidates or []:
        sector = c.get("sector") or sm.get(c.get("symbol") or "")
        tags = macro_tags_for_sector(sector)
        if tags:
            c["macro_tags"] = tags


_META_EXH = frozenset({"source", "asof", "note"})


def attach_krx_fields(candidates: list[dict],
                      market_state: dict | None = None) -> None:
    """market_state 의 positioning·foreign_exhaustion 을 후보에 in-place 부착.

    focus positioning 렌즈·athena 컨텍스트가 후보 dict 를 읽도록.
    """
    ms = market_state or {}
    pos = ms.get("positioning") or {}
    exh = ms.get("foreign_exhaustion") or {}
    for c in candidates or []:
        if not isinstance(c, dict):
            continue
        sym = c.get("symbol")
        if not sym:
            continue
        if "positioning" not in c and isinstance(pos.get(sym), dict):
            c["positioning"] = pos[sym]
        row = exh.get(sym)
        if isinstance(row, dict) and "foreign_exhaustion" not in c:
            c["foreign_exhaustion"] = {
                k: v for k, v in row.items() if k not in _META_EXH}


def _today() -> date:
    return datetime.now(ZoneInfo("Asia/Seoul")).date()


def _dday_from_date(d: str | date | None, today: date | None = None) -> int | None:
    if d is None:
        return None
    today = today or _today()
    if isinstance(d, date) and not isinstance(d, datetime):
        return (d - today).days
    try:
        return (datetime.strptime(str(d)[:10], "%Y-%m-%d").date() - today).days
    except (ValueError, TypeError):
        return None


def load_macro_events(path: Path | None = None) -> list[dict]:
    """data/macro_calendar.json → events[]. 없거나 깨지면 []."""
    p = path or MACRO_CAL_PATH
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as e:
        log.debug("macro_calendar 없음/실패: %s", e)
        return []
    ev = raw.get("events") if isinstance(raw, dict) else None
    return list(ev) if isinstance(ev, list) else []


def _macro_lens(ev: dict, dday: int) -> dict:
    eid = str(ev.get("id") or "macro").lower()
    label = ev.get("label") or eid.upper()
    hint = _MACRO_HINTS.get(eid) or (
        f"{label} 임박. 금리·환율·수급·VIX를 먼저 교차하고 갭 추격을 경계하라.")
    return {
        "id": eid,
        "kind": "macro_event",
        "dday": dday,
        "label": label,
        "priority": "high" if dday in MACRO_HIGH_DDAY else "medium",
        "read": list(_MACRO_READ),
        "hint": hint,
    }


def _flows_regime_lens(flows_market: dict) -> dict | None:
    """외국인 이상 수급 / 3일 동방향이면 flows_regime 렌즈.

    신호: (a) 한 시장의 |foreign_net| 이 foreign_net_p90 이상
         (b) foreign_net_3d 가 0이 아니고 당일과 동부호(3일 동방향)
    """
    if not isinstance(flows_market, dict):
        return None
    hits: list[str] = []
    for mkt in ("KOSPI", "KOSDAQ"):
        row = flows_market.get(mkt)
        if not isinstance(row, dict):
            continue
        fn = row.get("foreign_net")
        if not isinstance(fn, (int, float)):
            continue
        p90 = row.get("foreign_net_p90")
        if isinstance(p90, (int, float)) and p90 > 0 and abs(fn) >= p90:
            hits.append(f"{mkt} 외국인 순매수 {fn:+.0f}(≥p90 {p90:.0f})")
        fn3 = row.get("foreign_net_3d")
        if isinstance(fn3, (int, float)) and fn3 != 0 and fn != 0 and (fn3 > 0) == (fn > 0):
            hits.append(f"{mkt} 외국인 3일 동방향({fn3:+.0f})")
    if not hits:
        return None
    return {
        "id": "flows_regime",
        "kind": "flows",
        "dday": None,
        "label": "시장 수급 이상",
        "priority": "medium",
        "read": ["flows_market", "fx", "candidates.flows"],
        "hint": "시장 전체 수급→종목 수급으로 전달 여부 확인. " + " · ".join(hits[:4]),
        "detail": hits,
    }


def _program_flows_lens(program_flows: dict) -> dict | None:
    """프로그램매매 순매수 절댓값이 크면 렌즈(시장 방향 수급)."""
    if not isinstance(program_flows, dict):
        return None
    hits: list[str] = []
    for mkt in ("KOSPI", "KOSDAQ"):
        row = program_flows.get(mkt)
        if not isinstance(row, dict):
            continue
        total = row.get("total_net")
        if not isinstance(total, (int, float)) or abs(total) < 1e9:  # 10억 미만 무시
            continue
        arb = row.get("arb_net")
        bits = [f"{mkt} 프로그램 순매수 {total:+.0f}"]
        if isinstance(arb, (int, float)):
            bits.append(f"차익 {arb:+.0f}")
        hits.append(" ".join(bits))
    if not hits:
        return None
    return {
        "id": "program_flows",
        "kind": "program",
        "dday": None,
        "label": "프로그램매매",
        "priority": "medium",
        "read": ["program_flows", "flows_market"],
        "hint": "차익/비차익 프로그램 수급→지수·대형주 방향. " + " · ".join(hits[:4]),
        "detail": hits,
    }


def _short_market_lens(short_market: dict) -> dict | None:
    """시장 공매도 대금이 두드러지면 렌즈."""
    if not isinstance(short_market, dict):
        return None
    hits = []
    for mkt in ("KOSPI", "KOSDAQ"):
        row = short_market.get(mkt)
        if not isinstance(row, dict):
            continue
        total = row.get("total")
        if isinstance(total, (int, float)) and total >= 1e10:
            hits.append(f"{mkt} 공매도대금 {total:,.0f}")
    if not hits:
        return None
    return {
        "id": "short_market",
        "kind": "positioning",
        "dday": None,
        "label": "시장 공매도",
        "priority": "low",
        "read": ["short_market", "candidates.positioning"],
        "hint": "시장 전체 공매도 대금 주목. " + " · ".join(hits[:3]),
        "detail": hits,
    }


def _positioning_lens(symbols: Iterable[dict]) -> dict | None:
    """후보/보유 중 positioning.spike 가 켜진 종목이 있으면 렌즈."""
    spiked = []
    for row in symbols or []:
        if not isinstance(row, dict):
            continue
        pos = row.get("positioning")
        if isinstance(pos, dict) and pos.get("spike"):
            spiked.append(row.get("symbol") or "?")
    if not spiked:
        return None
    return {
        "id": "positioning",
        "kind": "positioning",
        "dday": None,
        "label": "신용·공매도 급변",
        "priority": "medium",
        "read": ["candidates.positioning"],
        "hint": ("신용잔고·공매도잔고 급변 종목 — 숏커버/반대매매 리스크를 thesis에 수치로 "
                 f"인용하라: {', '.join(spiked[:8])}"),
        "symbols": spiked[:12],
    }


def _priority_rank(p: str) -> int:
    return {"high": 0, "medium": 1, "low": 2}.get(p, 9)


def build_focus(market_state: dict | None = None, *,
                candidates: list[dict] | None = None,
                positions: list[dict] | None = None,
                macro_events: list[dict] | None = None,
                today: date | None = None) -> dict:
    """사실층 → focus{asof,lenses,summary}.

    macro_events 가 None 이면 data/macro_calendar.json 을 읽는다.
    """
    today = today or _today()
    ms = market_state or {}
    lenses: list[dict] = []

    events = macro_events if macro_events is not None else load_macro_events()
    for ev in events:
        if not isinstance(ev, dict):
            continue
        dday = _dday_from_date(ev.get("date"), today)
        if dday is None:
            # 저장된 dday 폴백(배치 직후)
            raw = ev.get("dday")
            dday = int(raw) if isinstance(raw, (int, float)) else None
        if dday is None or not (MACRO_DDAY_MIN <= dday <= MACRO_DDAY_MAX):
            continue
        lenses.append(_macro_lens(ev, dday))

    fl = _flows_regime_lens(ms.get("flows_market") or {})
    if fl:
        lenses.append(fl)

    pf = _program_flows_lens(ms.get("program_flows") or {})
    if pf:
        lenses.append(pf)

    sm = _short_market_lens(ms.get("short_market") or {})
    if sm:
        lenses.append(sm)

    # 후보+보유에서 positioning spike
    bag: list[dict] = []
    bag.extend(candidates or [])
    # portfolio.positions 형태 또는 리스트
    if isinstance(positions, list):
        bag.extend(positions)
    elif isinstance(positions, dict):
        bag.extend(positions.get("positions") or [])
    pl = _positioning_lens(bag)
    if pl:
        lenses.append(pl)

    lenses.sort(key=lambda x: (_priority_rank(x.get("priority") or ""),
                               x.get("dday") if isinstance(x.get("dday"), int) else 99,
                               x.get("id") or ""))

    parts = []
    for ln in lenses:
        if ln.get("kind") == "macro_event" and ln.get("dday") is not None:
            parts.append(f"{ln.get('label')} D{ln['dday']:+d}")
        elif ln.get("id") == "flows_regime":
            parts.append("외국인 수급 이상")
        elif ln.get("id") == "program_flows":
            parts.append("프로그램매매")
        elif ln.get("id") == "short_market":
            parts.append("시장 공매도")
        elif ln.get("id") == "positioning":
            parts.append("신용·공매도 급변")
        else:
            parts.append(str(ln.get("label") or ln.get("id")))

    return {
        "asof": datetime.now(ZoneInfo("Asia/Seoul")).isoformat(),
        "lenses": lenses,
        "summary": " · ".join(parts) if parts else "",
    }


# dday_of 재수출 — 테스트/매크로 캘린더 배치가 earnings 와 같은 헬퍼를 쓰게
__all__ = ["build_focus", "macro_tags_for_sector", "attach_macro_tags",
           "attach_krx_fields",
           "load_macro_events", "MACRO_DDAY_MIN", "MACRO_DDAY_MAX", "dday_of"]
