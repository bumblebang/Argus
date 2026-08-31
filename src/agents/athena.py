"""Athena — 종목 딥리서치 에이전트 (도시에 생성).

트레이딩 뇌(15분 주기, 24종목 일괄)가 못 하는 '종목당 깊은 분석'을 시장이 닫힌
리서치 창에서 종목당 1 LLM 호출로 수행한다. 출력은 반증 가능한 도시에:
진입존·무효화가·목표가·손익비·확신도·증거 — store.dossiers 에 저장되고,
장중 뇌가 이걸 근거 자료로 읽는다(P4에서 "신선 도시에 없으면 신규매수 금지" 연결).

원칙:
  - 하드스톱: 개장 전 무조건 정지(장중 뇌의 LLM 사용량 보호 — 무완주 재발 방지).
  - 우선순위: 보유/진입대기 > 도시에 없는 종목 > 오래된 도시에 순(커버리지 순환).
  - 토스 무접촉(Yahoo/market_state 파일만) — 게이트웨이·토큰 정책과 무관.
  - 레벨 없는 bullish 는 코드가 neutral 강등(채점 불가능한 도시에 금지).
"""
from __future__ import annotations

import json
import time
from typing import Callable

import pandas as pd

from ..baserate import analyze
from ..focus import build_focus, attach_krx_fields
from ..logging_setup import get_logger
from .features import technical_summary
from .schemas import DossierLevelOutput, DossierOutput
from .athena_phase2 import (
    ATHENA_LEVEL_SYSTEM, _athena_queued, build_level_context, merge_level_refresh,
    phase2_cfg, prices_from_market_state, should_level_only, sort_covered_by_zone,
    _parse_evidence,
)

log = get_logger("agents.athena")

ATHENA_SYSTEM = """\
당신은 자율 투자 시스템의 종목 딥리서치 애널리스트(Athena)다. 한 종목의 심층 자료
(기술적 요약·베이스레이트·재무·수급·뉴스·과거 이 종목 거래성과)를 받아, 트레이딩
매니저가 그대로 쓸 수 있는 '반증 가능한 계획'을 낸다.

원칙:
- 결론은 stance 로: bullish(진입 계획 제시) / neutral(관망) / bearish(회피·보유시 청산 고려).
- bullish 면 반드시 4개 레벨을 제시하라: entry_low < entry_high (진입 존),
  invalidation (이 가격 아래면 thesis 가 틀린 것 — 손절 근거), target (목표가).
  레벨은 현재가·지지/저항·무효화 논리에서 도출하고, invalidation < entry_low,
  target > entry_high 를 지켜라. 손익비(목표까지 거리/무효화까지 거리)가 1.5 미만이면
  bullish 를 재고하라.
- base_rates(이 종목의 셋업별 과거 승률/수익폭)는 실측 통계다. 활성 셋업의 승률·지평을
  진입 논리와 보유기간(horizon)에 직접 반영하고 evidence 에 수치로 인용하라.
- past_trades(이 시스템이 이 종목을 거래한 결과)가 있으면 복기하라 — 같은 논리로
  졌던 자리면 무엇이 달라졌는지 명시해야 한다.
- 증거(evidence)는 서로 독립적인 갈래로: 기술적 구조 / 베이스레이트 / 재무·펀더멘털 /
  수급 / 뉴스·재료 / 실적(예정·발표 결과). 갈래가 많이 정렬될수록 conviction 을 높여라.
  한 갈래뿐이면 0.5 이하.
- earnings(다음 실적 일정·컨센서스)가 있으면 dday·hour·consensus를 계획에 반영하라.
  임박만으로 기계적 neutral 하지 말고, 갭 리스크면 진입존을 더 보수적으로 그어라.
- earnings_results(이미 발표된 서프라이즈)가 있으면 eps/revenue surprise%를 evidence에
  수치로 인용하라. 상회·하회와 가격 반응을 교차해 stance를 정해라.
- 확신이 없으면 neutral 이 정답이다. 억지로 계획을 만들지 마라.
- 주어진 데이터에만 근거하라. 데이터에 없는 사실(실적 전망, 루머 등)을 지어내지 마라.

공포 국면(regime=risk_off, sentiment.fear_greed/fear_kr 이 fear·extreme_fear):
- stance 는 '지금 당장 사라'가 아니라 **'이 계획이 유효한가'**의 판정이다. 진입존은
  현재가와 무관하게 그을 수 있고, 코드가 가격이 그 존에 닿을 때까지 자동 대기(armed)
  시킨다. 그러니 국면이 공포여도 종목의 논리가 살아 있고 지지·무효화 레벨을 정직하게
  그을 수 있으면 **진입존을 현재가 아래 지지선에 둔 bullish** 가 정답이다 — 추격 없이
  공포 매수가 성립하는 자리다. 과매도·낙폭 자체는 회피 사유가 아니다.
- 반대로 논리가 깨졌거나(재무 악화·thesis 반증·중대 악재) 레벨을 그을 수 없으면
  neutral/bearish 다. **'무섭다'는 neutral 의 근거가 아니고, '많이 빠졌다'는 bullish 의
  근거가 아니다.** 둘 다 종목 고유의 증거로 말하라.
- 공포 국면 bullish 에는 **안정화 조건을 evidence 에 명시하라** — 20일선 회복, 20일
  수익률 양전환, 지지선에서 거래량 동반 반등, 수급 전환 중 무엇이 확인돼야 이 계획이
  발동하는지. 진입존은 그 조건이 충족될 만한 자리에 두어라.
- 시장 심리는 sentiment 로 주어진다(낮을수록 공포). VIX 는 미국 지표라 KR 종목의
  공포를 대변하지 않는다 — KR 은 fear_kr·regime 브레드스로 읽어라. fear_kr.rating 은
  rating_basis=percentile 이면 우리 이력 대비(50=평년)이고, incomplete/missing 이면
  장전 결측이니 그 등급을 확정 국면처럼 쓰지 마라. inputs.vkospi·put_call_ratio 는
  전일 KRX 부가입력이지 score 가중치가 아니다.

오늘의 렌즈(focus — 있으면 시황을 그 순서로 먼저 읽어라):
- focus.lenses 가 있으면 그 순서·hint·read 슬롯을 시황 배경으로 깔고, evidence 에
  해당 id·수치를 인용하라. 렌즈에 없는 매크로 이벤트를 지어내지 마라.
- 매크로 이벤트 렌즈가 켜져 있어도 기계적으로 neutral 하지 마라 — 진입존을 현재가
  아래에 두고 대기시키는 bullish 가 더 맞는 경우가 많다. rate_sensitive 태그가 있는
  종목은 금리 경로를 evidence 에 명시하라.
- flows_market(시장 전체 수급)이 있으면 종목 flows 와 교차해 장 방향 전달 여부를 봐라.

미장 → 한국장 배경(한국 종목일 때):
- 한국장은 간밤 미국장 위에서 열린다. markets 의 SP500·NASDAQ 등락과 USDKRW,
  sentiment 의 VIX 를 **배경·선행 흐름**으로 깔고 이 종목의 계획을 그어라.
- technical.gap_pct 는 당일 시가가 전일 종가 대비 몇 % 위/아래에서 열렸는지다
  (open·prev_close 동봉). 간밤 미장 방향과 함께 보면 그 흐름이 이 종목 가격에 **얼마나
  이미 반영됐는지**를 알려주는 참고 데이터다 — 진입존·무효화가를 그을 때 이 반영분을
  감안하라.
- 다만 '미장이 올랐으니 이 종목도 간다' 같은 기계적 추종은 근거가 아니다. 결론은 종목
  고유의 증거로 말하라.
- 한국 종목이면 macro_kr(한국은행 기준금리·국고채/CD 금리·물가·고용·심리)이 국내 거시
  배경으로 함께 주어진다 — KR 금리·물가의 정본은 이것이다(미국 지표로 대신하지 마라).
  금리 민감 업종이라면 evidence 에 수치로 인용하라.
- 미국 종목이면 macro(FRED: 기준금리·국채·달러인덱스·실업·CPI 등)가 거시 배경이다.
  macro_kr 로 US 금리·물가를 대신하지 마라. 금리 민감이면 evidence 에 수치로 인용하라."""


def build_research_context(symbol: str, name: str, market: str, *,
                           history_df: pd.DataFrame | None,
                           market_state: dict | None = None,
                           base_rates: dict | None = None,
                           past_trades: list[dict] | None = None,
                           focus: dict | None = None,
                           earnings: dict | None = None,
                           earnings_results: list[dict] | None = None) -> dict:
    """종목 1개의 딥리서치 입력 묶음(~수 KB). LLM 이 이걸 보고 도시에를 쓴다.

    focus: 주의층 렌즈. None 이면 이 호출에서 build_focus 로 계산한다.
    market_state.json 에는 focus 를 저장하지 않으므로(dday 신선도) 파일에서
    ms['focus'] 를 읽으면 항상 비었다 — 예전 dead wire. 배치(run_batch)는
    창마다 한 번 계산해 넘겨 중복 계산을 피한다.

    earnings / earnings_results: 뇌 cycle_runner 와 같은 실적 슬롯. 없으면 생략.
    macro(FRED)는 market_state 에서 항상 실어 US 거시 배경을 맞춘다.
    """
    ms = market_state or {}
    br = base_rates if base_rates is not None else (
        analyze(history_df) if history_df is not None else {})
    news = [n for n in (ms.get("news") or []) if n.get("symbol") == symbol][:10]
    cand = {"symbol": symbol, "name": name, "market": market}
    attach_krx_fields([cand], ms)
    if focus is None:
        focus = build_focus(ms, candidates=[cand])
    ctx = {
        "symbol": symbol, "name": name, "market": market,
        "technical": technical_summary(history_df),
        "base_rates": br,
        "fundamentals": (ms.get("fundamentals") or {}).get(symbol),
        "flows": (ms.get("flows") or {}).get(symbol),
        "positioning": cand.get("positioning") or (ms.get("positioning") or {}).get(symbol),
        "foreign_exhaustion": cand.get("foreign_exhaustion") or _pick_exhaustion(ms, symbol),
        "regime": (ms.get("regime") or {}).get(market),
        "sentiment": ms.get("sentiment"),
        "markets": ms.get("markets"),
        "macro": ms.get("macro"),
        "macro_kr": ms.get("macro_kr"),
        "flows_market": ms.get("flows_market"),
        "program_flows": ms.get("program_flows"),
        "short_market": ms.get("short_market"),
        "focus": focus,
        "news": [{"source": n.get("source"), "title": n.get("title")} for n in news],
        "past_trades": past_trades or [],
    }
    if earnings:
        ctx["earnings"] = earnings
    if earnings_results:
        ctx["earnings_results"] = earnings_results
    return ctx


def _pick_exhaustion(ms: dict, symbol: str) -> dict | None:
    row = (ms.get("foreign_exhaustion") or {}).get(symbol)
    if not isinstance(row, dict):
        return None
    return {k: v for k, v in row.items()
            if k not in ("source", "asof", "note")}


def compute_rr(entry_low: float | None, entry_high: float | None,
               invalidation: float | None, target: float | None) -> float | None:
    """기대 손익비 = (목표-진입중앙)/(진입중앙-무효화). LLM 산수 대신 코드가 계산."""
    if None in (entry_low, entry_high, invalidation, target):
        return None
    mid = (entry_low + entry_high) / 2
    risk = mid - invalidation
    if risk <= 0:
        return None
    return round((target - mid) / risk, 2)


def sanitize(d: DossierOutput, *, price: float | None = None
             ) -> tuple[DossierOutput, list[str]]:
    """bullish 도시에의 레벨 정합성 하드가드. 어긋나면 neutral 강등(+사유).

    양수·순서만이 아니라, 현재가가 있으면 무효화 거리·존 폭·목표 거리도 본다.
    밴드는 wiring 의 코드 손절/목표 하드 바운드와 같다 — 실행이 버릴 레벨을
    리서치 단계에서 이미 bullish 로 남기지 않는다.
    """
    from .wiring import (MAX_STOP_PCT, MAX_TARGET_PCT, MAX_ZONE_WIDTH_PCT,
                         MIN_STOP_PCT)

    notes: list[str] = []
    if d.stance != "bullish":
        return d, notes
    levels = (d.entry_low, d.entry_high, d.invalidation, d.target)
    if any(v is None or v <= 0 for v in levels):
        notes.append("bullish 인데 레벨 미제시 → neutral 강등")
    elif not (d.invalidation < d.entry_low <= d.entry_high < d.target):
        notes.append(f"레벨 순서 오류(inv {d.invalidation} < lo {d.entry_low} <= "
                     f"hi {d.entry_high} < tgt {d.target} 위반) → neutral 강등")
    elif price is not None:
        try:
            px = float(price)
        except (TypeError, ValueError):
            px = 0.0
        if px > 0:
            dist = (px - float(d.invalidation)) / px
            if dist < MIN_STOP_PCT:
                notes.append(
                    f"무효화가 너무 가까움 ({dist:.1%} < {MIN_STOP_PCT:.1%}) "
                    f"→ neutral 강등")
            elif dist > MAX_STOP_PCT:
                notes.append(
                    f"무효화가 너무 멀음 ({dist:.1%} > {MAX_STOP_PCT:.1%}) "
                    f"→ neutral 강등")
            width = (float(d.entry_high) - float(d.entry_low)) / px
            if width > MAX_ZONE_WIDTH_PCT:
                notes.append(
                    f"진입존이 너무 넓음 ({width:.1%} > {MAX_ZONE_WIDTH_PCT:.1%}) "
                    f"→ neutral 강등")
            tgt_dist = (float(d.target) - px) / px
            if tgt_dist > MAX_TARGET_PCT:
                notes.append(
                    f"목표가 너무 멀음 ({tgt_dist:.1%} > {MAX_TARGET_PCT:.1%}) "
                    f"→ neutral 강등")
    if notes:
        d = d.model_copy(update={"stance": "neutral"})
    return d, notes


class AthenaAgent:
    def __init__(self, llm):
        self.llm = llm

    def research(self, context: dict) -> DossierOutput:
        out = self.llm.structured(ATHENA_SYSTEM,
                                  json.dumps(context, ensure_ascii=False),
                                  DossierOutput)
        log.info("[%s] %s (conv %.2f): %s", context.get("symbol"), out.stance,
                 out.conviction, out.thesis[:60])
        return out

    def research_levels(self, context: dict) -> DossierLevelOutput:
        out = self.llm.structured(ATHENA_LEVEL_SYSTEM,
                                  json.dumps(context, ensure_ascii=False),
                                  DossierLevelOutput)
        log.info("[%s] level_refresh %s (conv %.2f)",
                 context.get("symbol"), out.stance, out.conviction)
        return out


# ── 배치 실행 ──────────────────────────────────────────────────────
def _disclosure_queued(store, market: str, since_hours: float = 24.0) -> list[str]:
    """공시 워처가 큐(route=queue)로 남긴 종목 — 새 재료가 뜬 종목을 우선 재리서치.

    KR=DART, US=EDGAR. 유니버스에서 빠졌어도 재료가 뜬 종목은 본다.
    payload.market 이 있으면 시장 필터, 없으면 심볼 형태(6자리=KR)로 추정.
    """
    mkt = str(market or "").upper()
    out: list[str] = []
    try:
        rows = store.recent_events("disclosure", time.time() - since_hours * 3600,
                                   limit=50)
        for r in rows:
            p = json.loads(r["payload"]) if r["payload"] else {}
            if p.get("route") != "queue":
                continue
            sym = r["symbol"]
            if not sym:
                continue
            pm = str(p.get("market") or "").upper()
            if pm:
                if pm != mkt:
                    continue
            else:
                looks_kr = str(sym).isdigit() and len(str(sym)) == 6
                if mkt == "KR" and not looks_kr:
                    continue
                if mkt == "US" and looks_kr:
                    continue
            out.append(sym)
    except Exception as e:
        log.warning("공시 큐 조회 실패(무시): %s", e)
    return list(dict.fromkeys(out))


def _earnings_result_queued(store, market: str, since_hours: float = 36.0
                            ) -> list[str]:
    """실적 결과 이벤트(route=queue|wake) 종목 — 발표 직후 도시레 재소환.

    US=Finnhub 워처, KR=DART 잠정실적 파싱. 뇌 earnings_results 창(36h)과 맞춤.
    payload.market 이 있으면 시장 필터, 없으면 심볼 형태(6자리=KR)로 추정.
    """
    mkt = str(market or "").upper()
    out: list[str] = []
    try:
        rows = store.recent_events(
            "earnings_result", time.time() - since_hours * 3600, limit=50)
        for r in rows:
            p = json.loads(r["payload"]) if r["payload"] else {}
            if p.get("route") not in ("queue", "wake"):
                continue
            sym = r["symbol"]
            if not sym:
                continue
            pm = str(p.get("market") or "").upper()
            if pm:
                if pm != mkt:
                    continue
            else:
                looks_kr = str(sym).isdigit() and len(str(sym)) == 6
                if mkt == "KR" and not looks_kr:
                    continue
                if mkt == "US" and looks_kr:
                    continue
            out.append(sym)
    except Exception as e:
        log.warning("실적 결과 큐 조회 실패(무시): %s", e)
    return list(dict.fromkeys(out))


def _load_earnings_calendar() -> dict:
    """data/earnings_calendar.json 종목맵. 없거나 깨지면 {}."""
    from .wiring import DATA
    p = DATA / "earnings_calendar.json"
    try:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8")).get("symbols", {}) or {}
    except (OSError, ValueError) as e:
        log.warning("earnings_calendar 로드 실패(생략): %s", e)
    return {}


def _earnings_results_by_symbol(store, since_hours: float = 36.0,
                                limit: int = 50) -> dict[str, list[dict]]:
    """최근 earnings_result 이벤트를 심볼→[compact…] 로 묶는다."""
    out: dict[str, list[dict]] = {}
    try:
        rows = store.recent_events(
            "earnings_result", time.time() - since_hours * 3600, limit=limit)
    except Exception as e:
        log.warning("실적 결과 로드 실패(생략): %s", e)
        return out
    for r in rows:
        sym = r["symbol"]
        if not sym:
            continue
        try:
            p = json.loads(r["payload"]) if r["payload"] else {}
        except (TypeError, ValueError):
            p = {}
        item = {"symbol": sym, "date": p.get("date"),
                "eps_estimate": p.get("eps_estimate"),
                "eps_actual": p.get("eps_actual"),
                "eps_surprise_pct": p.get("eps_surprise_pct"),
                "revenue_surprise_pct": p.get("revenue_surprise_pct"),
                "route": p.get("route")}
        for k in ("market", "parse_ok", "unit", "scope",
                  "revenue_actual", "op_profit_actual", "net_income_actual",
                  "op_profit_surprise_pct", "net_income_surprise_pct"):
            if p.get(k) is not None:
                item[k] = p[k]
        out.setdefault(sym, []).append(item)
    return out


def _min_refresh_hours(cfg) -> float:
    """athena.min_refresh_hours — covered 갱신 최소 간격(0=비활성)."""
    raw = getattr(cfg, "raw", cfg) if not isinstance(cfg, dict) else cfg
    if not isinstance(raw, dict):
        return 0.0
    return float((raw.get("athena") or {}).get("min_refresh_hours", 0))


def _covered_for_refresh(symbols: list[str], covered_at: dict[str, float], *,
                         min_refresh_hours: float, now: float) -> list[str]:
    """min_refresh_hours 안의 covered 는 순환 갱신에서 제외(보유·큐는 선행 단계)."""
    if min_refresh_hours <= 0:
        return list(symbols)
    cutoff = now - min_refresh_hours * 3600
    return [s for s in symbols if float(covered_at.get(s, 0)) <= cutoff]


def select_symbols(cfg, store, market: str, limit: int | None = None, *,
                   market_state: dict | None = None,
                   prices: dict[str, float] | None = None,
                   now_fn: Callable[[], float] = time.time) -> list[dict]:
    """리서치 우선순위: 보유 > 이벤트큐 > 공시 > 실적 > 미커버 > 존근접 covered.

    covered 회전은 존 근접(in→below→above→unknown) 우선, 동순위는 오래된 도시에 먼저.
    min_refresh_hours(기본 config) 이내 covered 는 순환에서 스킵 — 보유·큐·공시·실적은 예외.
    """
    p2 = phase2_cfg(cfg)
    min_refresh = _min_refresh_hours(cfg)
    now = now_fn()
    uni = {it["symbol"]: it.get("name", it["symbol"])
           for it in (cfg.universe or {}).get(market, []) if it.get("symbol")}
    held = [dict(r) for r in store.get_open_positions()] + \
           [dict(r) for r in store.get_armed()]
    held_syms = [p["symbol"] for p in held if (p.get("market") or "KR") == market]
    held_set = set(held_syms)
    covered = {r["symbol"]: r["created_at"] for r in store.dossier_coverage()}
    px = prices if prices is not None else prices_from_market_state(
        market_state, symbols=uni.keys())
    fresh_order: list[str] = []
    fresh_order += held_syms
    if p2.get("enabled", True):
        fresh_order += _athena_queued(store, market,
                                      since_hours=p2["queue_since_hours"])
    fresh_order += _disclosure_queued(store, market)              # 공시 재소환(KR/US)
    fresh_order += _earnings_result_queued(store, market)         # 실적결과 재소환
    fresh_order += [s for s in uni if s not in covered]           # 미커버
    covered_syms = _covered_for_refresh(
        [s for s in uni if s in covered], covered,
        min_refresh_hours=min_refresh, now=now)
    fresh_order += sort_covered_by_zone(covered_syms, store, px, covered)
    from ..security_filter import is_buy_ineligible
    seen: set[str] = set()
    out = []
    for s in fresh_order:
        if s in seen:
            continue
        seen.add(s)
        name = uni.get(s, s)
        # ETF 등 매수 불가 — 보유/armed 만 예외(청산·감시용 리서치).
        if s not in held_set:
            bad, reason = is_buy_ineligible(s, market, name)
            if bad:
                log.info("[%s] Athena 스킵 %s (%s)", market, s, reason)
                continue
        out.append({"symbol": s, "name": name})
    return out[:limit] if limit else out


def run_batch(cfg, store, llm, market: str, *,
              limit: int = 30, ttl_hours: float = 60.0,
              stop_at: float | None = None,
              fetch_df: Callable[[str, str], pd.DataFrame] | None = None,
              market_state: dict | None = None,
              base_rates: dict | None = None,
              only_symbols: list[str] | None = None,
              now_fn: Callable[[], float] = time.time) -> dict:
    """리서치 창 1회 실행: 우선순위 종목들에 도시레 생성. 반환: 요약 dict.

    stop_at(epoch) 도달 시 즉시 중단(하드스톱 — 개장 전 뇌 LLM 예산 보호).
    한 종목 실패는 로깅 후 다음 종목으로(배치가 죽지 않는다).
    """
    from ..datasources.earnings import with_fresh_dday

    agent = AthenaAgent(llm)
    ms = market_state or {}
    p2 = phase2_cfg(cfg)
    prices = prices_from_market_state(ms)
    if only_symbols:
        uni = {it["symbol"]: it.get("name", it["symbol"])
               for it in (cfg.universe or {}).get(market, []) if it.get("symbol")}
        targets = [{"symbol": s, "name": uni.get(s, s)} for s in only_symbols]
    else:
        targets = select_symbols(cfg, store, market, limit=limit,
                                 market_state=ms, prices=prices)
    # 주의층은 사이클마다 계산(파일 미저장). 창 1회만 만들어 전 종목에 공유.
    held = [dict(r) for r in store.get_open_positions()
            if (r["market"] or "KR") == market]
    focus = build_focus(
        ms,
        candidates=[{"symbol": t["symbol"], "name": t["name"], "market": market}
                    for t in targets],
        positions=held)
    earnings_cal = _load_earnings_calendar()
    ers_by_sym = _earnings_results_by_symbol(store)
    done, failed, stopped, level_only_n = 0, 0, False, 0
    for t in targets:
        if stop_at is not None and now_fn() >= stop_at:
            stopped = True
            log.warning("하드스톱 도달 — 남은 %d종목은 다음 창에서.", len(targets) - done - failed)
            break
        sym = t["symbol"]
        prev_row = None
        try:
            df = fetch_df(sym, market) if fetch_df else None
            br = (base_rates or {}).get(sym) if base_rates is not None else None
            past = [tr for tr in _past_trades(store) if tr["symbol"] == sym][:5]
            earn = with_fresh_dday(earnings_cal.get(sym))
            ers = ers_by_sym.get(sym) or None
            ctx = build_research_context(sym, t["name"], market, history_df=df,
                                         market_state=ms,
                                         base_rates=br, past_trades=past,
                                         focus=focus, earnings=earn,
                                         earnings_results=ers)
            tech = ctx.get("technical") or {}
            px = tech.get("price") if isinstance(tech, dict) else None
            if px is None:
                px = prices.get(sym)
            use_level, prev_row = should_level_only(
                store, sym, px, p2, now=now_fn())
            if use_level and prev_row:
                lvl_ctx = build_level_context(ctx, prev_row)
                level_out = agent.research_levels(lvl_ctx)
                out = merge_level_refresh(prev_row, level_out)
                mode = "level_only"
                level_only_n += 1
                level_note = level_out.level_note
            else:
                out = agent.research(ctx)
                mode = "full"
                level_note = None
            out, notes = sanitize(out, price=px)
            rr = compute_rr(out.entry_low, out.entry_high, out.invalidation, out.target)
            ev_payload = {"stance": out.stance, "horizon": out.horizon,
                          "evidence": out.evidence, "key_risks": out.key_risks,
                          "sanitize_notes": notes, "refresh_mode": mode}
            if level_note:
                ev_payload["level_note"] = level_note
            if mode == "full" and px and float(px) > 0:
                ev_payload["ref_price"] = round(float(px), 4)
            elif mode == "level_only" and prev_row:
                prev_ref = _parse_evidence(prev_row).get("ref_price")
                if prev_ref is not None:
                    ev_payload["ref_price"] = prev_ref
                elif px and float(px) > 0:
                    ev_payload["ref_price"] = round(float(px), 4)
            store.save_dossier(
                sym, market, thesis=out.thesis,
                entry_low=out.entry_low, entry_high=out.entry_high,
                invalidation=out.invalidation, target=out.target,
                rr=rr, conviction=out.conviction,
                evidence=ev_payload,
                ttl_hours=ttl_hours)
            store.log_event("dossier", sym,
                            {"stance": out.stance, "conviction": out.conviction,
                             "rr": rr, "thesis": out.thesis[:80], "mode": mode})
            done += 1
        except Exception as e:
            failed += 1
            log.error("[%s] 도시레 생성 실패: %s", sym, e)
            store.log_event("error", sym, {"where": "athena", "err": str(e)})
    summary = {"market": market, "targets": len(targets), "done": done,
               "failed": failed, "stopped_by_deadline": stopped,
               "level_only": level_only_n}
    store.log_event("athena_done", None, summary)
    log.info("Athena %s 창 종료: %s", market, summary)
    return summary


def _past_trades(store) -> list[dict]:
    """이 시스템의 청산 완료 거래(성과귀속과 같은 원천) — 종목 복기용."""
    from ..attribution import recent_trades
    try:
        return recent_trades(store, limit=50)
    except Exception:
        return []
