"""저평가 주 스캔(#19b v1-lite) — 낙폭과대 KR/US 후보 발굴 + LLM 밸류에이션 도시에.

매매 유휴기(토스 토큰 중단 등)에 남는 LLM 예산으로 '밸류+타이밍 이중 열쇠'의
밸류 쪽 지도를 쌓는다. 역할분리: 풀 발굴=Naver 시총상위(KR)+Yahoo 저평가 스크리너(US),
히스토리=Yahoo, 밸류 판단=sonnet(구독 CLI). 토스 무접촉 — 데몬·토큰 정책과 무관.

출력=data/value_watchlist.json (누적 밸류에이션 지도). TTL 안의 심볼은 재스캔을
건너뛰므로 매 실행이 랭킹 아래로 자연 순환한다(시총 사다리 v1). 매매 게이트 신설
없음 — 소비는 후속(gem 가산점·Athena 리서치 컨텍스트 주입).
"""
from __future__ import annotations

import json
import math
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

import pandas as pd

from .agents.schemas import ValueDossier
from .config import ROOT
from .logging_setup import get_logger

log = get_logger("value_scan")
_KST = ZoneInfo("Asia/Seoul")

WATCHLIST = ROOT / "data" / "value_watchlist.json"
# KR 재무 캐시(DART 쿼터 보호 — 연간 재무라 갱신 잦지 않음). 구조:
# {corp_code: {"<year>": {revenue, operating_income, net_income, equity, total_assets,
#  total_liabilities, current_assets, current_liabilities, fiscal_year}}}.
# 과거 항목은 새 계정 키가 없을 수 있음 → _kr_fundamentals 가 None 안전 처리.
FIN_CACHE = ROOT / "data" / "value_financials_cache.json"

# 한국 ETF/ETN 브랜드 — security_filter 와 동일(하위호환 alias).
from .security_filter import KR_ETF_BRANDS as _KR_ETF_BRANDS, is_kr_etf_name as _is_kr_etf

VALUE_SYSTEM = """\
당신은 자율 투자 시스템의 가치투자 밸류에이션 분석가다. 저평가 후보 풀에서 낙폭과대
필터를 통과한 한 종목(한국/미국 시장, market 필드로 구분)의 가격 지표를 받아, 이 종목이
'싸진 것'인지 '싼 이유가 있는 것'인지 판정한다.

원칙:
- 순서대로 점검하라: ①왜 싸졌나(하락 원인 진단 — 업황/실적/수급/일회성 악재)
  ②그 원인이 해소 가능한가(시기·촉매) ③밸류 트랩 여부 — 만년 저평가(만성 저PBR),
  지주사 디스카운트, 사양산업, 지배구조 문제를 명시적으로 점검하라.
- 결론은 stance 로: undervalued(저평가이고 해소 가능) / fair(하락이 정당한 적정가) /
  value_trap(싼 이유가 구조적이라 해소 기대 곤란).
- undervalued 면 적정가 밴드를 **지금 가격(price)** 대비 %로 제시하라(fair_low_pct/
  fair_high_pct, 예: +20 은 현재가보다 20% 위). fair_low_pct < fair_high_pct.
  prior 가 있으면 직전 스캔이다. 그 사이 가격이 움직였으면 옛 밴드를 복사하지 말고
  지금 가격 기준으로 다시 매기고, stance 도 재평가하라.
- 재무제표 원본은 제공되지 않는다. 다만 밸류에이션 지표는 시장별로 다르게 주어진다:
  · 미국(market=US) 종목은 fundamentals(pe_trailing/pe_forward/pb/eps_ttm/market_cap_busd)가
    함께 제공된다. 밸류에이션 **절대 수준**을 판단에 반영하라 — 저PER·저PBR 이면 저평가
    근거로, PER 이 null 이거나 eps_ttm 이 음수면 적자(밸류트랩 경계)로, 고PER 인데 낙폭이면
    성장 프리미엄 소멸로 읽어라.
  · 한국(market=KR) 종목도 fundamentals 가 제공된다 — DART **연결 재무제표** 기반이며
    **최근 사업보고서(연간)라 시차가 있다**. 밸류에이션: pb/pe_trailing/net_margin(저PBR·
    저PER 이면 저평가 근거, pe 가 null 이면 적자, 만성 저PBR 지주사는 밸류트랩 경계).
    건전성·수익성·성장성 지표도 함께 온다:
      - debt_ratio(부채비율=부채/자본): **과다(예: 2.0=200% 초과)면 저PBR 이어도 재무
        레드플래그** → 밸류트랩 경계.
      - roe/roa: **낮거나 음수면 자본을 못 굴리는 것** → 저평가에 정당한 이유가 있을 수 있음.
      - op_margin(영업이익률)·current_ratio(유동비율): 본업 수익성·단기 지급능력 확인.
      - revenue_growth/op_income_growth/net_income_growth(전년 대비): **역성장이면 쇠퇴
        기업의 싼 값(밸류트랩) 신호** → 촉매 없이는 진입 신중.
      - revenue_eok: 매출 규모(단위 **억원**).
    각 지표가 null(미제공)이면 그 지표는 무시하고 나머지로 판단하라. 시총은 현재가, 재무는
    작년(fiscal_year) 기준이라 시차가 있음을 evidence 에 명시하라.
  지표가 낡거나 결측일 수 있음을 evidence 에 명시하고, 확실치 않은 사실을 지어내지 마라.
- recent_news(최근 헤드라인)가 주어지면 하락의 실제 촉매를 파악하라 — 일회성 악재
  (실적 미스·소송 등 해소 가능)인지 구조적 악재(수요 붕괴·규제·지배구조)인지 뉴스로
  구분해 stance 판단에 반영하라. 뉴스가 없거나 무관하면 종전대로 판단하고, 헤드라인은
  제목뿐이라 확대해석하지 마라.
- 핵심 리스크는 최대 3개. 확신이 없으면 stance 를 fair 로 두고 conviction 을 낮춰라."""


def _norm_turnover(v) -> dict:
    """유동성 플로어를 시장별 dict 로 정규화(하위호환).

    스칼라면 {"KR": 값, "US": 3e6} 으로 감싸고(기존 KR 스칼라 설정 그대로 수용),
    dict 면 기본값 위에 병합. 기본 {"KR": 5e8, "US": 3e6}.
    """
    base = {"KR": 5e8, "US": 3e6}
    if isinstance(v, dict):
        return {**base, **{k: float(x) for k, x in v.items()}}
    return {"KR": float(v), "US": base["US"]}


def _vcfg(cfg) -> dict:
    """config value_scan 블록 + 기본값."""
    raw = (cfg.raw.get("value_scan") or {})
    return {
        "pool": int(raw.get("pool", 200)),
        "pool_us": int(raw.get("pool_us", 400)),
        "markets": list(raw.get("markets", ["KR", "US"])),
        "min_avg_turnover": _norm_turnover(raw.get("min_avg_turnover", 5e8)),
        "max_per_run": int(raw.get("max_per_run", 6)),
        "ttl_hours": float(raw.get("ttl_hours", 400)),
        # 보유만 재검증 예약. 0 이면 강제 재스캔 없음. undervalued 지도는 평일 슬롯을 안 먹는다.
        "refresh_hours": float(raw.get("refresh_hours", 20)),
        "refresh_slots": int(raw.get("refresh_slots", 3)),
        "weekend_refresh": bool(raw.get("weekend_refresh", True)),
        # 토·일 지도 전량. 5.5h×주말~8회×24 ≈ 192 > 저평가 ~170.
        "weekend_max_per_run": int(raw.get("weekend_max_per_run", 24)),
        "dd_floor": float(raw.get("dd_floor", -0.70)),   # 이보다 깊으면 제외(빈사)
        "dd_ceil": float(raw.get("dd_ceil", -0.25)),     # 이보다 얕으면 제외(낙폭 부족)
        "freefall_ret20": float(raw.get("freefall_ret20", -0.15)),
        "news_per_symbol": int(raw.get("news_per_symbol", 3)),  # 후보당 주입 헤드라인 수
    }


def candidate_metrics(df: pd.DataFrame, vcfg: dict, market: str = "KR") -> dict | None:
    """1종목 게이트(유동성/낙폭 밴드/자유낙하) + 지표. 탈락이면 None.

    낙폭 기준은 캔들 범위(1y 요청) 내 고점 — gem v1 과 같은 한계(52주 보장은 아님).
    유동성 플로어는 시장별(min_avg_turnover[market], KR=원·US=달러).
    """
    if df is None or len(df) < 120:
        return None
    close = df["close"].astype(float)
    vol = df["volume"].astype(float)
    price = float(close.iloc[-1])
    if price <= 0:
        return None

    # 유동성 플로어 — 못 사는 보석 감정 금지(#19b 가드①). 시장별 통화 기준.
    min_turnover = vcfg["min_avg_turnover"][market]
    turnover = float((close * vol).tail(20).mean())
    if turnover < min_turnover:
        return None

    # 낙폭 밴드 — 얕으면 저평가 아님, 너무 깊으면 빈사(위생).
    dd = price / float(close.max()) - 1
    if not (vcfg["dd_floor"] <= dd <= vcfg["dd_ceil"]):
        return None

    # 자유낙하 가드 — 아직 떨어지는 칼은 잡지 않는다.
    ret20 = price / float(close.iloc[-20]) - 1
    if ret20 <= vcfg["freefall_ret20"]:
        return None

    stab = max(0.0, 1.0 - abs(ret20) / 0.15)            # 최근 20일이 평평할수록 ↑
    liq = (min(1.0, math.log10(turnover / min_turnover) / 2)
           if turnover > min_turnover else 0.0)
    score = (-dd) * 0.5 + stab * 0.3 + liq * 0.2         # 깊은 낙폭 + 안정화 + 유동성

    ret60 = (price / float(close.iloc[-60]) - 1) if len(close) >= 60 else None
    # 거래대금은 시장 통화가 다르다(KR=원·US=달러) → 시장별 단위로 표기하고 단위를
    # 명시해 LLM 이 US 유동성을 '억원'으로 오해하지 않게 한다.
    if market == "US":
        turnover_disp, turnover_unit = round(turnover / 1e6, 1), "M USD"
    else:
        turnover_disp, turnover_unit = round(turnover / 1e8, 1), "억원"
    return {
        "price": round(price, 2),
        "drawdown_1y_pct": round(dd * 100, 1),
        "ret_20d_pct": round(ret20 * 100, 1),
        "ret_60d_pct": round(ret60 * 100, 1) if ret60 is not None else None,
        "avg_turnover_20d": turnover_disp,
        "turnover_unit": turnover_unit,
        "score": round(score, 4),
    }


def _kr_pool(fetch_ranking_fn: Callable, pool: int) -> list[dict]:
    """KOSPI+KOSDAQ 각 절반씩 Naver 시총상위 풀(심볼 중복 제거). market="KR"."""
    pool_rows: list[dict] = []
    seen: set[str] = set()
    skipped_etf = 0
    for nm in ("KOSPI", "KOSDAQ"):
        try:
            rows = fetch_ranking_fn(nm, pool=pool // 2)
        except Exception as e:
            log.warning("[value][%s] 풀 발굴 실패(스킵): %s", nm, e)
            continue
        for i, r in enumerate(rows):
            sym = r.get("symbol")
            if not sym or sym in seen:
                continue
            seen.add(sym)
            name = r.get("name", sym)
            if _is_kr_etf(name):        # ETF/ETN 제외(개별 저평가주 발굴 대상 아님)
                skipped_etf += 1
                continue
            pool_rows.append({"symbol": sym, "name": name,
                              "market": "KR", "mcap_rank": f"{nm}#{i + 1}",
                              "market_cap": r.get("market_cap")})
    if skipped_etf:
        log.debug("[value][KR] ETF/ETN %d종목 제외", skipped_etf)
    return pool_rows


def _us_pool(fetch_us_value_fn: Callable, pool: int) -> list[dict]:
    """Yahoo 저평가 스크리너 합집합 풀(심볼 중복 제거). market="US", 순위는 소스순."""
    pool_rows: list[dict] = []
    seen: set[str] = set()
    try:
        rows = fetch_us_value_fn(pool=pool)
    except Exception as e:
        log.warning("[value][US] 풀 발굴 실패(스킵): %s", e)
        return pool_rows
    for i, r in enumerate(rows):
        sym = r.get("symbol")
        if not sym or sym in seen:
            continue
        seen.add(sym)
        pool_rows.append({"symbol": sym, "name": r.get("name", sym),
                          "market": "US", "mcap_rank": f"US#{i + 1}",
                          "fundamentals": r.get("fundamentals")})
    return pool_rows


def scan_candidates(cfg, fetch_ranking_fn: Callable, fetch_history_fn: Callable,
                    *, exclude: set[str] | None = None,
                    fetch_us_value_fn: Callable | None = None) -> list[dict]:
    """KR(Naver 시총상위)+US(Yahoo 저평가 스크리너) 풀 → 게이트 통과분을 score 내림차순으로.

    KR 풀은 시총 순 그대로 쓴다(거래대금 재정렬 아님 — 눌린 중위권을 배제하지 않기 위해).
    US 풀은 저평가 스크리너 합집합 순. 시장은 vcfg["markets"] 로 결정. 심볼 단위 실패는
    스킵(스캔 전체가 죽지 않게). exclude(TTL-신선 심볼)는 히스토리 조회 전에 걸러 호출을
    아낀다. KR·US 후보를 한 리스트로 합쳐 score 내림차순 반환(가장 저평가된 것 우선).
    """
    vcfg = _vcfg(cfg)
    exclude = exclude or set()
    if fetch_us_value_fn is None:
        from .datasources.discovery import fetch_us_value_pool as fetch_us_value_fn

    pool_rows: list[dict] = []
    for mkt in vcfg["markets"]:
        if mkt == "KR":
            pool_rows += _kr_pool(fetch_ranking_fn, vcfg["pool"])
        elif mkt == "US":
            pool_rows += _us_pool(fetch_us_value_fn, vcfg["pool_us"])
        else:
            log.warning("[value] 미지원 시장 스킵: %s", mkt)

    scored: list[dict] = []
    sub_pool: dict[str, int] = {}
    sub_pass: dict[str, int] = {}
    for row in pool_rows:
        mkt = row["market"]
        sub_pool[mkt] = sub_pool.get(mkt, 0) + 1
        sym = row["symbol"]
        if sym in exclude:
            continue
        try:
            df = fetch_history_fn(sym, mkt)
        except Exception as e:
            log.debug("[value][%s] 히스토리 실패(스킵): %s", sym, e)
            continue
        m = candidate_metrics(df, vcfg, mkt)
        if m is None:
            continue
        sub_pass[mkt] = sub_pass.get(mkt, 0) + 1
        cand = {"symbol": sym, "name": row["name"], "market": mkt,
                "mcap_rank": row["mcap_rank"], **m}
        if row.get("fundamentals"):     # US 후보만(KR 은 키 없음/None) LLM 프롬프트에 실림
            cand["fundamentals"] = row["fundamentals"]
        if row.get("market_cap"):       # KR 후보만 — run_scan 의 KR 펀더멘털 계산 입력
            cand["market_cap"] = row["market_cap"]
        scored.append(cand)
    scored.sort(key=lambda c: c["score"], reverse=True)
    subtotals = ", ".join(f"{m}: 풀 {sub_pool.get(m, 0)}→통과 {sub_pass.get(m, 0)}"
                          for m in vcfg["markets"])
    log.info("[value] 풀 %d → 게이트 통과 %d (제외 %d) [%s]",
             len(pool_rows), len(scored), len(exclude), subtotals)
    return scored


# ── watchlist 영속(누적 밸류에이션 지도) ──────────────────────────────
def load_watchlist(path: str | Path = WATCHLIST) -> dict:
    p = Path(path)
    try:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError) as e:
        log.warning("watchlist 로드 실패(빈 지도로 진행): %s", e)
    return {}


def save_watchlist(data: dict, path: str | Path = WATCHLIST) -> None:
    """원자적 쓰기(tmp + replace) — 스캔 도중 크래시에도 지도가 깨지지 않게."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def fresh_symbols(watchlist: dict, ttl_hours: float,
                  now: float | None = None) -> set[str]:
    """TTL 안의 심볼 — 이번 스캔에서 건너뛴다(재검증은 TTL 만료 후 = 사다리 순환)."""
    now = now if now is not None else time.time()
    return {s for s, e in watchlist.items()
            if now - float((e or {}).get("ts", 0)) < ttl_hours * 3600}


def priority_refresh_symbols(watchlist: dict, *, held: set[str],
                             refresh_hours: float, now: float,
                             limit: int) -> list[str]:
    """보유만 재검증 예약. 지도의 undervalued 는 신규 슬롯을 먹지 않는다.

    보유가 없거나 아직 refresh_hours 안이면 빈 리스트 → max_per_run 전부 발굴.
    지도는 주말 전량 재검증(weekend_map_symbols) + ttl(400h) 사다리.
    """
    if refresh_hours <= 0 or limit <= 0 or not held:
        return []
    cutoff = refresh_hours * 3600
    ranked: list[tuple[float, str]] = []
    for sym in held:
        entry = (watchlist or {}).get(sym)
        if not isinstance(entry, dict):
            continue
        age = now - float(entry.get("ts") or 0)
        if age < cutoff:
            continue
        ranked.append((-age, sym))
    ranked.sort()
    return [s for _, s in ranked[:limit]]


def is_kst_weekend(now: float) -> bool:
    """KST 토(5)·일(6)."""
    return datetime.fromtimestamp(now, tz=_KST).weekday() >= 5


def kst_weekend_start(now: float) -> float:
    """이번 주 토요일 00:00 KST. 토·일에만 호출."""
    dt = datetime.fromtimestamp(now, tz=_KST)
    sat0 = (dt.replace(hour=0, minute=0, second=0, microsecond=0)
            - timedelta(days=dt.weekday() - 5))
    return sat0.timestamp()


def weekend_map_symbols(watchlist: dict, *, markets: list[str], now: float,
                        skip: set[str], limit: int) -> list[str]:
    """이번 주말 아직 안 돈 undervalued. 오래된 순. 토 0시 이후 스캔분은 스킵."""
    if limit <= 0:
        return []
    start = kst_weekend_start(now)
    mset = set(markets or ())
    ranked: list[tuple[float, str]] = []
    for sym, entry in (watchlist or {}).items():
        if sym in skip or not isinstance(entry, dict):
            continue
        if entry.get("stance") != "undervalued":
            continue
        if mset and (entry.get("market") or "KR") not in mset:
            continue
        ts = float(entry.get("ts") or 0)
        if ts >= start:
            continue
        ranked.append((ts, sym))
    ranked.sort()
    return [s for _, s in ranked[:limit]]


def _metrics_from_history(df, vcfg: dict, market: str) -> dict | None:
    """게이트 통과분이면 candidate_metrics, 아니면 현재가만(재검증은 낙폭 밖이어도 한다)."""
    m = candidate_metrics(df, vcfg, market)
    if m:
        return m
    if df is None or len(df) < 1:
        return None
    close = df["close"].astype(float)
    price = float(close.iloc[-1])
    if price <= 0:
        return None
    dd = (price / float(close.max()) - 1) if len(close) else None
    ret20 = (price / float(close.iloc[-20]) - 1) if len(close) >= 20 else None
    ret60 = (price / float(close.iloc[-60]) - 1) if len(close) >= 60 else None
    return {
        "price": round(price, 2),
        "drawdown_1y_pct": round(dd * 100, 1) if dd is not None else None,
        "ret_20d_pct": round(ret20 * 100, 1) if ret20 is not None else None,
        "ret_60d_pct": round(ret60 * 100, 1) if ret60 is not None else None,
        "avg_turnover_20d": None, "turnover_unit": None, "score": 0.0,
        "gate_pass": False,
    }


def refresh_candidate(sym: str, entry: dict, fetch_history_fn: Callable,
                      vcfg: dict, now: float) -> dict | None:
    """기존 도시예를 현재 시세로 다시 감정할 후보 dict. 히스토리 실패면 None."""
    mkt = entry.get("market") or "KR"
    try:
        df = fetch_history_fn(sym, mkt)
    except Exception as e:
        log.debug("[value][%s] 재검증 히스토리 실패(스킵): %s", sym, e)
        return None
    metrics = _metrics_from_history(df, vcfg, mkt)
    if not metrics:
        return None
    ts = float(entry.get("ts") or now)
    cand = {
        "symbol": sym, "name": entry.get("name") or sym, "market": mkt,
        "mcap_rank": (entry.get("metrics") or {}).get("mcap_rank"),
        **metrics,
        "prior": {
            "stance": entry.get("stance"),
            "conviction": entry.get("conviction"),
            "fair_low_pct": entry.get("fair_low_pct"),
            "fair_high_pct": entry.get("fair_high_pct"),
            "thesis": (entry.get("thesis") or "")[:240],
            "scan_age_hours": round((now - ts) / 3600, 1),
        },
    }
    if entry.get("fundamentals"):
        cand["fundamentals"] = entry["fundamentals"]
    return cand


# ── KR 재무 캐시 + 펀더멘털 계산 ──────────────────────────────────
def _load_fin_cache(path: str | Path = FIN_CACHE) -> dict:
    """KR 재무 캐시 로드(실패는 빈 dict — 스캔을 막지 않는다)."""
    p = Path(path)
    try:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError) as e:
        log.warning("[value] 재무 캐시 로드 실패(빈 캐시로 진행): %s", e)
    return {}


def _save_fin_cache(data: dict, path: str | Path = FIN_CACHE) -> None:
    """원자적 쓰기(tmp + replace). 실패는 무시(캐시는 최적화일 뿐)."""
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, p)
    except Exception as e:
        log.warning("[value] 재무 캐시 저장 실패(무시): %s", e)


# fetch_financials 반환에서 캐시에 담는 계정 키(BS+IS) + fiscal_year.
_FIN_CACHE_KEYS = ("revenue", "operating_income", "net_income", "equity",
                   "total_assets", "total_liabilities",
                   "current_assets", "current_liabilities", "fiscal_year")


def _growth(cur, prev) -> float | None:
    """전년 대비 성장률(prev 기준, |prev|로 나눠 부호 보존). 분모 0/None 안전 → None."""
    if cur is None or prev is None or prev == 0:
        return None
    return round((cur - prev) / abs(prev), 4)


def _kr_fundamentals(api_key: str, corp_map: dict, code: str, market_cap: float | None,
                     cache: dict, *, fetch_fn: Callable | None = None,
                     years: tuple[int, ...] | None = None) -> dict | None:
    """DART 연결 재무로 KR 밸류에이션·건전성·성장 지표를 계산. 계산 불가면 None.

    반환 {pb, pe_trailing, net_margin, debt_ratio, current_ratio, roe, roa, op_margin,
    revenue_growth, op_income_growth, net_income_growth, revenue_eok, fiscal_year}.
    market_cap(현재 시총, 원)이 없거나 당해 재무를 못 구하면 None. 재무는 cache(hit 면 DART
    미접촉) → miss 면 years 순으로 fetch_fn 시도(2025 우선, 013 등으로 없으면 2024 폴백)하고
    성공분을 cache 에 적재한다. 성장률용 전년(당해 직전 연도) 재무도 캐시 우선 확보하되,
    없으면 성장률만 None(당해로 나머지 지표는 계산). 분모 0/None 은 전부 None 안전.
    자본잠식(equity<=0)이면 pb·debt_ratio·roe=None, 적자(net_income<=0)면 pe_trailing=None.
    """
    if years is None:                      # 최근 사업보고서(전년도) 우선, 그 전년 폴백
        y0 = date.today().year - 1
        years = (y0, y0 - 1)
    if not market_cap or market_cap <= 0:
        return None
    corp = corp_map.get(code)
    if not corp:
        return None
    if fetch_fn is None:
        from .datasources.dart import fetch_financials as fetch_fn
    ce = cache.setdefault(corp, {})

    def _year_fin(y: int) -> dict | None:
        """해당 연도 재무 — 캐시 hit 우선(DART 미호출), miss 면 조회 후 적재. 없으면 None."""
        if str(y) in ce:
            return ce[str(y)]
        got = fetch_fn(api_key, corp, y)
        if got:
            entry = {k: got.get(k) for k in _FIN_CACHE_KEYS}
            ce[str(y)] = entry
            return entry
        return None

    fin = None
    for y in years:                        # 당해(최근 사업보고서) — 캐시 hit 우선(DART 미호출)
        if str(y) in ce:
            fin = ce[str(y)]
            break
    if fin is None:                        # miss → DART 조회(2025 우선, 폴백 2024)
        for y in years:
            fin = _year_fin(y)
            if fin:
                break
    if fin is None:
        return None
    fy = fin.get("fiscal_year")
    prev = _year_fin(fy - 1) if fy else None   # 성장률용 전년(없으면 성장률만 None)

    revenue, net_income, equity = fin.get("revenue"), fin.get("net_income"), fin.get("equity")
    op_income, total_assets = fin.get("operating_income"), fin.get("total_assets")
    total_liab = fin.get("total_liabilities")
    cur_assets, cur_liab = fin.get("current_assets"), fin.get("current_liabilities")

    pb = round(market_cap / equity, 2) if (equity and equity > 0) else None
    pe = round(market_cap / net_income, 2) if (net_income and net_income > 0) else None
    margin = (net_income / revenue) if (revenue and net_income is not None) else None
    # 부채비율·ROE 는 자본이 양(+)일 때만(자본잠식이면 왜곡 → None). ROA·유동비율·영업이익률.
    debt_ratio = (round(total_liab / equity, 4)
                  if (equity and equity > 0 and total_liab is not None) else None)
    current_ratio = (round(cur_assets / cur_liab, 4)
                     if (cur_liab and cur_assets is not None) else None)
    roe = (round(net_income / equity, 4)
           if (equity and equity > 0 and net_income is not None) else None)
    roa = (round(net_income / total_assets, 4)
           if (total_assets and net_income is not None) else None)
    op_margin = (round(op_income / revenue, 4)
                 if (revenue and op_income is not None) else None)

    prev = prev or {}
    return {
        "pb": pb, "pe_trailing": pe,
        "net_margin": round(margin, 4) if margin is not None else None,
        "debt_ratio": debt_ratio, "current_ratio": current_ratio,
        "roe": roe, "roa": roa, "op_margin": op_margin,
        "revenue_growth": _growth(revenue, prev.get("revenue")),
        "op_income_growth": _growth(op_income, prev.get("operating_income")),
        "net_income_growth": _growth(net_income, prev.get("net_income")),
        "revenue_eok": round(revenue / 1e8) if revenue else None,
        "fiscal_year": fy,
    }


# ── 실행 ─────────────────────────────────────────────────────────
def run_scan(cfg, llm, *, limit: int | None = None,
             fetch_ranking_fn: Callable | None = None,
             fetch_history_fn: Callable | None = None,
             fetch_us_value_fn: Callable | None = None,
             news_us_fn: Callable | None = None,
             news_kr_fn: Callable | None = None,
             fin_kr_fn: Callable | None = None,
             watchlist_path: str | Path = WATCHLIST,
             held_symbols: set[str] | None = None,
             now_fn: Callable[[], float] = time.time) -> dict:
    """스캔 1회: 후보 상위 limit개에 밸류에이션 도시에 생성 → watchlist 병합.

    LLM 이 사용량 한도('limit' 포함 예외)로 실패하면 남은 후보를 중단하고 정상
    종료한다(다음 주기가 재시도). 도시에는 1건 생성될 때마다 즉시 저장한다
    (한도 중단·크래시에도 이미 쓴 예산의 결과는 남게).

    도시에 생성 직전 후보 market 별로 최근 헤드라인(recent_news)을 주입해 LLM 이 하락
    촉매를 실제 뉴스로 판단하게 한다(US=Finnhub company-news, KR=네이버 종목뉴스).
    뉴스 조회 실패는 삼켜 스캔을 죽이지 않는다.
    """
    if fetch_ranking_fn is None:
        from .datasources.discovery import fetch_ranking as fetch_ranking_fn
    if fetch_us_value_fn is None:
        from .datasources.discovery import fetch_us_value_pool as fetch_us_value_fn
    if fetch_history_fn is None:
        from .datasources.history import fetch_history as _fh

        def fetch_history_fn(sym: str, mkt: str):
            return _fh(sym, interval="1d", range_="1y", market=mkt, max_age_hours=20)

    # 뉴스 조회 함수(테스트용 주입, 기본은 실함수). 실함수일 때만 finnhub pacing sleep 적용.
    us_news_is_default = news_us_fn is None
    if news_us_fn is None:
        from .datasources.finnhub import fetch_company_news as news_us_fn
    if news_kr_fn is None:
        from .datasources.news import fetch_kr_stock_news as news_kr_fn
    if fin_kr_fn is None:
        fin_kr_fn = _kr_fundamentals

    vcfg = _vcfg(cfg)
    now = now_fn()
    weekend = bool(vcfg["weekend_refresh"]) and is_kst_weekend(now)
    if limit is None:
        limit = vcfg["weekend_max_per_run"] if weekend else vcfg["max_per_run"]
    news_per = vcfg["news_per_symbol"]
    finnhub_key = os.getenv("FINNHUB_API_KEY", "")
    if not finnhub_key and "US" in vcfg["markets"]:
        log.warning("[value] FINNHUB_API_KEY 없음 — US 뉴스 스킵(KR 만 주입)")

    # KR 펀더멘털(#19b 3단계) 준비 — DART 키·corp_map 1회 로드(실패 시 비활성). 시총은
    # 현재가·재무는 작년 사업보고서라 시차가 있지만 pb/pe 절대수준은 판단에 유용.
    dart_key = os.getenv("DART_API_KEY", "")
    corp_map: dict | None = None
    if "KR" in vcfg["markets"]:
        if dart_key:
            try:
                from .datasources.dart import load_corp_map
                corp_map = load_corp_map(dart_key)
            except Exception as e:
                log.warning("[value] DART corp_map 로드 실패 — KR 펀더멘털 비활성: %s", e)
        else:
            log.warning("[value] DART_API_KEY 없음 — KR 펀더멘털 스킵")
    kr_fund_on = bool(dart_key) and corp_map is not None
    fin_cache = _load_fin_cache() if kr_fund_on else {}
    fin_cache_orig = json.dumps(fin_cache, ensure_ascii=False, sort_keys=True)

    watchlist = load_watchlist(watchlist_path)
    fresh = fresh_symbols(watchlist, vcfg["ttl_hours"], now=now)
    held = set(held_symbols or ())
    force = priority_refresh_symbols(
        watchlist, held=held,
        refresh_hours=vcfg["refresh_hours"], now=now,
        limit=int(vcfg["refresh_slots"]))
    if weekend:
        extra = weekend_map_symbols(
            watchlist, markets=list(vcfg["markets"]), now=now,
            skip=set(force), limit=max(0, limit - len(force)))
        force = force + extra
    fresh -= set(force)
    refresh_cands: list[dict] = []
    for sym in force:
        entry = watchlist.get(sym) or {}
        c = refresh_candidate(sym, entry, fetch_history_fn, vcfg, now)
        if c:
            refresh_cands.append(c)
    cands = scan_candidates(cfg, fetch_ranking_fn, fetch_history_fn, exclude=fresh,
                            fetch_us_value_fn=fetch_us_value_fn)
    force_set = {c["symbol"] for c in refresh_cands}
    ladder = [c for c in cands if c["symbol"] not in force_set]
    remain = max(0, limit - len(refresh_cands))
    cand_list = refresh_cands + ladder[:remain]
    done, failed, aborted = 0, 0, None
    for idx, c in enumerate(cand_list):
        sym = c["symbol"]
        # 최근 뉴스 주입(#19b 2단계) — 조회 예외는 삼켜 빈 리스트로.
        try:
            if c["market"] == "US":
                news = news_us_fn(finnhub_key, sym, per=news_per) if finnhub_key else []
                if news_per and finnhub_key and us_news_is_default and idx < len(cand_list) - 1:
                    time.sleep(1.1)          # Finnhub 60콜/분 보호(종목 간 pacing)
            elif c["market"] == "KR":
                news = news_kr_fn(sym, per=news_per)
            else:
                news = []
        except Exception as e:
            log.debug("[value][%s] 뉴스 조회 실패(스킵): %s", sym, e)
            news = []
        if news:
            c["recent_news"] = news
        # KR 펀더멘털 주입(#19b 3단계) — US 는 이미 scan_candidates 에서 fundamentals 를
        # 받았으니 건드리지 않는다("아직 없으면" 가드). 예외는 삼켜 스캔을 죽이지 않는다.
        if kr_fund_on and c["market"] == "KR" and "fundamentals" not in c:
            try:
                f = fin_kr_fn(dart_key, corp_map, sym, c.get("market_cap"), fin_cache)
                if f:
                    c["fundamentals"] = f
            except Exception as e:
                log.debug("[value][%s] KR 펀더멘털 조회 실패(스킵): %s", sym, e)
        try:
            out = llm.structured(VALUE_SYSTEM,
                                 json.dumps(c, ensure_ascii=False), ValueDossier)
        except Exception as e:
            if "limit" in str(e).lower():        # 세션/주간 한도 — 다음 주기 재시도
                aborted = "limit"
                log.warning("[value] LLM 한도 도달 — 남은 %d종목 중단: %s",
                            limit - done - failed, e)
                break
            failed += 1
            log.error("[value][%s] 도시에 생성 실패(스킵): %s", sym, e)
            continue
        watchlist[sym] = {
            "name": c["name"], "market": c["market"], "ts": now_fn(),
            "stance": out.stance, "conviction": out.conviction,
            "fair_low_pct": out.fair_low_pct, "fair_high_pct": out.fair_high_pct,
            "thesis": out.thesis, "risks": out.risks, "evidence": out.evidence,
            "metrics": {k: c[k] for k in ("price", "drawdown_1y_pct", "ret_20d_pct",
                                          "ret_60d_pct", "avg_turnover_20d",
                                          "turnover_unit", "mcap_rank", "score")},
        }
        if c.get("fundamentals"):       # US 밸류에이션 지표 — 후속(대시보드/gem/Athena)용
            watchlist[sym]["fundamentals"] = c["fundamentals"]
        if c.get("recent_news"):        # 주입된 하락 촉매 헤드라인 — 후속 활용용 보존
            watchlist[sym]["recent_news"] = c["recent_news"]
        save_watchlist(watchlist, watchlist_path)
        done += 1
        log.info("[value][%s] %s (conv %.2f): %s",
                 sym, out.stance, out.conviction, out.thesis[:60])

    # 새로 조회한 KR 재무가 있으면 캐시 영속(변화 없으면 파일 미접촉 — 테스트 격리에도 유리).
    if kr_fund_on and json.dumps(fin_cache, ensure_ascii=False, sort_keys=True) != fin_cache_orig:
        _save_fin_cache(fin_cache)

    summary = {"eligible": len(cands), "skipped_fresh": len(fresh),
               "refreshed": len(refresh_cands), "weekend": weekend,
               "done": done, "failed": failed, "aborted": aborted,
               "watchlist_size": len(watchlist)}
    log.info("[value] 스캔 종료: %s", summary)
    return summary
