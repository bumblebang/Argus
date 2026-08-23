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
from ..indicators import rsi, sma
from ..logging_setup import get_logger
from .features import _gap_fields
from .schemas import DossierOutput

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
  수급 / 뉴스·재료. 갈래가 많이 정렬될수록 conviction 을 높여라. 한 갈래뿐이면 0.5 이하.
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
  금리 민감 업종이라면 evidence 에 수치로 인용하라."""


# ── 기술적 요약(코드 계산 — LLM 에 원시 캔들 대신 압축 수치를 준다) ──────
def technical_summary(df: pd.DataFrame) -> dict:
    """일봉 히스토리 → 위치/추세/모멘텀 요약. 도시에 컨텍스트의 뼈대."""
    if df is None or len(df) < 60:
        return {}
    close = df["close"].astype(float)
    last = float(close.iloc[-1])
    hi52 = float(close.tail(252).max()) if len(close) >= 252 else float(close.max())
    lo52 = float(close.tail(252).min()) if len(close) >= 252 else float(close.min())
    vol = df["volume"].astype(float)

    def _sma_gap(n: int) -> float | None:
        if len(close) < n:
            return None
        return round((last / float(sma(close, n).iloc[-1]) - 1) * 100, 1)

    return {
        "price": round(last, 2),
        "pct_from_52w_high": round((last / hi52 - 1) * 100, 1),
        "pct_from_52w_low": round((last / lo52 - 1) * 100, 1),
        "vs_sma20_pct": _sma_gap(20), "vs_sma60_pct": _sma_gap(60),
        "vs_sma120_pct": _sma_gap(120),
        "rsi14": round(float(rsi(close, 14).iloc[-1]), 1),
        "ret_20d_pct": round((last / float(close.iloc[-20]) - 1) * 100, 1),
        "ret_60d_pct": (round((last / float(close.iloc[-60]) - 1) * 100, 1)
                        if len(close) >= 60 else None),
        "volume_ratio_20v60": (round(float(vol.tail(20).mean())
                                     / float(vol.tail(60).mean()), 2)
                               if float(vol.tail(60).mean()) else None),
        **_gap_fields(df),   # gap_pct/open/prev_close — 후보 피처와 같은 계산
    }


def build_research_context(symbol: str, name: str, market: str, *,
                           history_df: pd.DataFrame | None,
                           market_state: dict | None = None,
                           base_rates: dict | None = None,
                           past_trades: list[dict] | None = None,
                           focus: dict | None = None) -> dict:
    """종목 1개의 딥리서치 입력 묶음(~수 KB). LLM 이 이걸 보고 도시에를 쓴다.

    focus: 주의층 렌즈. None 이면 이 호출에서 build_focus 로 계산한다.
    market_state.json 에는 focus 를 저장하지 않으므로(dday 신선도) 파일에서
    ms['focus'] 를 읽으면 항상 비었다 — 예전 dead wire. 배치(run_batch)는
    창마다 한 번 계산해 넘겨 중복 계산을 피한다.
    """
    ms = market_state or {}
    br = base_rates if base_rates is not None else (
        analyze(history_df) if history_df is not None else {})
    news = [n for n in (ms.get("news") or []) if n.get("symbol") == symbol][:10]
    cand = {"symbol": symbol, "name": name, "market": market}
    attach_krx_fields([cand], ms)
    if focus is None:
        focus = build_focus(ms, candidates=[cand])
    return {
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
        "macro_kr": ms.get("macro_kr"),
        "flows_market": ms.get("flows_market"),
        "program_flows": ms.get("program_flows"),
        "short_market": ms.get("short_market"),
        "focus": focus,
        "news": [{"source": n.get("source"), "title": n.get("title")} for n in news],
        "past_trades": past_trades or [],
    }


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


def sanitize(d: DossierOutput) -> tuple[DossierOutput, list[str]]:
    """bullish 도시에의 레벨 정합성 하드가드. 어긋나면 neutral 강등(+사유)."""
    notes: list[str] = []
    if d.stance != "bullish":
        return d, notes
    levels = (d.entry_low, d.entry_high, d.invalidation, d.target)
    if any(v is None or v <= 0 for v in levels):
        notes.append("bullish 인데 레벨 미제시 → neutral 강등")
    elif not (d.invalidation < d.entry_low <= d.entry_high < d.target):
        notes.append(f"레벨 순서 오류(inv {d.invalidation} < lo {d.entry_low} <= "
                     f"hi {d.entry_high} < tgt {d.target} 위반) → neutral 강등")
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


# ── 배치 실행 ──────────────────────────────────────────────────────
def _disclosure_queued(store, market: str, since_hours: float = 24.0) -> list[str]:
    """공시 워처가 큐(route=queue)로 남긴 종목 — 새 재료가 뜬 종목을 우선 재리서치.

    DART=KR 전용이라 KR 창에서만 소환한다. 유니버스에서 빠졌어도 재료가 뜬 종목은 본다.
    """
    if market != "KR":
        return []
    import json as _json
    out: list[str] = []
    try:
        rows = store.recent_events("disclosure", time.time() - since_hours * 3600,
                                   limit=50)
        for r in rows:
            p = _json.loads(r["payload"]) if r["payload"] else {}
            if p.get("route") == "queue" and r["symbol"]:
                out.append(r["symbol"])
    except Exception as e:
        log.warning("공시 큐 조회 실패(무시): %s", e)
    return list(dict.fromkeys(out))


def select_symbols(cfg, store, market: str, limit: int | None = None) -> list[dict]:
    """리서치 우선순위: 보유/진입대기 > 공시 큐 > 도시에 없음 > 오래된 도시에 순.

    보유/armed 는 유니버스 밖이어도 포함한다(들고 있는 걸 모르는 게 최악). 공시 큐
    (최근 24h 중대공시가 뜬 커버 종목)는 재료 반영을 위해 미커버보다 먼저 재소환.
    """
    uni = {it["symbol"]: it.get("name", it["symbol"])
           for it in (cfg.universe or {}).get(market, []) if it.get("symbol")}
    held = [dict(r) for r in store.get_open_positions()] + \
           [dict(r) for r in store.get_armed()]
    held_syms = [p["symbol"] for p in held if (p.get("market") or "KR") == market]
    held_set = set(held_syms)
    covered = {r["symbol"]: r["created_at"] for r in store.dossier_coverage()}
    fresh_order: list[str] = []
    fresh_order += held_syms
    fresh_order += _disclosure_queued(store, market)              # 공시 재소환
    fresh_order += [s for s in uni if s not in covered]           # 미커버
    fresh_order += sorted((s for s in uni if s in covered),       # 오래된 순
                          key=lambda s: covered[s])
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
              now_fn: Callable[[], float] = time.time) -> dict:
    """리서치 창 1회 실행: 우선순위 종목들에 도시에 생성. 반환: 요약 dict.

    stop_at(epoch) 도달 시 즉시 중단(하드스톱 — 개장 전 뇌 LLM 예산 보호).
    한 종목 실패는 로깅 후 다음 종목으로(배치가 죽지 않는다).
    """
    agent = AthenaAgent(llm)
    targets = select_symbols(cfg, store, market, limit=limit)
    ms = market_state or {}
    # 주의층은 사이클마다 계산(파일 미저장). 창 1회만 만들어 전 종목에 공유.
    held = [dict(r) for r in store.get_open_positions()
            if (r["market"] or "KR") == market]
    focus = build_focus(
        ms,
        candidates=[{"symbol": t["symbol"], "name": t["name"], "market": market}
                    for t in targets],
        positions=held)
    done, failed, stopped = 0, 0, False
    for t in targets:
        if stop_at is not None and now_fn() >= stop_at:
            stopped = True
            log.warning("하드스톱 도달 — 남은 %d종목은 다음 창에서.", len(targets) - done - failed)
            break
        sym = t["symbol"]
        try:
            df = fetch_df(sym, market) if fetch_df else None
            br = (base_rates or {}).get(sym) if base_rates is not None else None
            past = [tr for tr in _past_trades(store) if tr["symbol"] == sym][:5]
            ctx = build_research_context(sym, t["name"], market, history_df=df,
                                         market_state=ms,
                                         base_rates=br, past_trades=past,
                                         focus=focus)
            out, notes = sanitize(agent.research(ctx))
            rr = compute_rr(out.entry_low, out.entry_high, out.invalidation, out.target)
            store.save_dossier(
                sym, market, thesis=out.thesis,
                entry_low=out.entry_low, entry_high=out.entry_high,
                invalidation=out.invalidation, target=out.target,
                rr=rr, conviction=out.conviction,
                evidence={"stance": out.stance, "horizon": out.horizon,
                          "evidence": out.evidence, "key_risks": out.key_risks,
                          "sanitize_notes": notes},
                ttl_hours=ttl_hours)
            store.log_event("dossier", sym,
                            {"stance": out.stance, "conviction": out.conviction,
                             "rr": rr, "thesis": out.thesis[:80]})
            done += 1
        except Exception as e:
            failed += 1
            log.error("[%s] 도시에 생성 실패: %s", sym, e)
            store.log_event("error", sym, {"where": "athena", "err": str(e)})
    summary = {"market": market, "targets": len(targets), "done": done,
               "failed": failed, "stopped_by_deadline": stopped}
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
