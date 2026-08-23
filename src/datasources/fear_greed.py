"""공포지수 레이어 — "공포에 사고 탐욕에 판다"의 **측정층**.

  - fear_greed : CNN Fear & Greed Index(미국). 무료·무인증. 0~100, **낮을수록 공포**.
  - fear_kr    : 한국판 무료 대체 합성(브레드스·KOSPI 낙폭·5일수익률). 같은 방향/스케일.
    등급(rating)은 이 대리치 **자기 이력 퍼센타일**(50=우리 평년). 이력이 짧으면
    합성 원점수에 CNN 구간을 임시로 붙이고 rating_basis=absolute 로 표시한다.
    성분이 빠지면 incomplete/missing — 장전과 장중 점수를 같은 척도로 읽지 말 것.

VKOSPI·풋콜은 KRX Open API(전일)로 inputs 에만 붙인다 — score 가중치에는 넣지 않는다.
VIX 는 미국 지표다.

이 모듈은 **절대 예외를 밖으로 던지지 않는다**(fail-open). 실패는 None/키 생략으로 표현하고
log.warning 만 남긴다 — 장중 5분 슬라이스와 장전 배치가 이걸 부르므로 죽으면 안 된다.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import requests

from .history import fetch_history
from ..logging_setup import get_logger

log = get_logger("src.fear_greed")

_UA = {"User-Agent": "Mozilla/5.0 argus"}
FEAR_GREED_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"

# CNN 응답의 하위지표 중 우리가 담는 7개. sp125/vix_50 은 sp500/vix 와 중복,
# fear_and_greed_historical 은 시계열이라 제외한다.
_CNN_COMPONENTS = ("market_momentum_sp500", "stock_price_strength", "stock_price_breadth",
                   "put_call_options", "market_volatility_vix", "junk_bond_demand",
                   "safe_haven_demand")
_CNN_PREV = (("prev_close", "previous_close"), ("prev_1w", "previous_1_week"),
             ("prev_1m", "previous_1_month"), ("prev_1y", "previous_1_year"))

# KR 대리지표 성분 가중치. 있는 성분만 쓰고 합으로 재정규화한다.
# ※ 3번째 성분은 '일간등락'이 아니라 5일 수익률이다 — 하루 등락은 폭락장의 반등일에
#   +18% 같은 값이 찍혀 성분이 포화(=탐욕)돼버린다. 5일이면 "아직 떨어지는 중인가,
#   멈췄는가"를 보므로 공포 로직의 '안정화' 축과도 결이 맞는다.
# VKOSPI·풋콜은 Open API 부가입력(inputs)만 — 여기 가중치에 넣지 않는다(얇은 표본).
_KR_WEIGHTS = {"breadth": 0.5, "drawdown": 0.3, "ret_5d": 0.2}
_KR_COMPONENTS = tuple(_KR_WEIGHTS)
_KR_NOTE = ("브레드스·지수낙폭·5일수익률 합성. 등급은 우리 이력 대비(50=평년). "
            "VKOSPI·풋콜은 전일 KRX 부가입력(점수 가중치 아님). 성분 결측이면 과신 금지")
_KR_RATING_MIN_N = 20     # 이보다 짧은 이력은 퍼센타일 등급을 붙이지 않는다

# 대시보드 스파크라인용 시계열. market_state.sentiment(=뇌 프롬프트)에는 절대 싣지 않고
# 별도 파일에만 쓴다 — 배열이 들어가면 LLM 컨텍스트가 노이즈로 부푼다.
HISTORY_PATH = "data/fear_history.json"
_HISTORY_KEEP = 120     # CNN 1년치(251포인트) 중 대시보드에 쓸 최근 구간

# 장중 5분 슬라이스가 매번 CNN 을 때리지 않도록 모듈 레벨 TTL 캐시를 둔다.
_cache: dict = {"ts": 0.0, "value": None}


def rating_of(score: float) -> str:
    """0~100 점수 → CNN 과 같은 어휘의 라벨(공백 대신 언더스코어로 정규화)."""
    if score < 25:
        return "extreme_fear"
    if score < 45:
        return "fear"
    if score < 55:
        return "neutral"
    if score < 75:
        return "greed"
    return "extreme_greed"


def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


def percentile_rank(score: float, hist: list[float]) -> float:
    """이력 대비 중위순위 퍼센타일 0~100. 전원이 같으면 50(평년)."""
    n = len(hist)
    if n <= 0:
        return 50.0
    less = sum(1 for s in hist if s < score)
    eq = sum(1 for s in hist if s == score)
    return (less + 0.5 * eq) / n * 100.0


def load_kr_scores(path: str | Path | None = None) -> list[float]:
    """fear_history.json 의 KR 점수만. 없거나 깨지면 []."""
    try:
        raw = json.loads(Path(path if path is not None else HISTORY_PATH)
                         .read_text(encoding="utf-8"))
        rows = raw.get("kr") if isinstance(raw, dict) else None
        if not isinstance(rows, list):
            return []
        out = []
        for pt in rows:
            if not isinstance(pt, (list, tuple)) or len(pt) < 2:
                continue
            try:
                out.append(float(pt[1]))
            except (TypeError, ValueError):
                continue
        return out
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return []


def apply_kr_rating(kr: dict, hist: list[float] | None,
                    min_n: int = _KR_RATING_MIN_N) -> dict:
    """fear_kr 에 등급을 붙인다. hist 가 충분하면 퍼센타일, 아니면 원점수 구간.

    score(합성 원점수)는 바꾸지 않는다. rating / rating_basis / score_pct 만 갱신.
    """
    score = float(kr["score"])
    series = [float(x) for x in (hist or [])]
    if len(series) >= min_n:
        pct = round(percentile_rank(score, series), 1)
        kr["score_pct"] = pct
        kr["rating"] = rating_of(pct)
        kr["rating_basis"] = "percentile"
    else:
        kr.pop("score_pct", None)
        kr["rating"] = rating_of(score)
        kr["rating_basis"] = "absolute"
    return kr


def _parse_history(hist: dict | None) -> list:
    """CNN 의 fear_and_greed_historical.data(1년치 일별) → [[epoch_sec, score], ...].

    시간 오름차순 최근 _HISTORY_KEEP 포인트만. 없거나 깨졌으면 빈 목록.
    """
    rows = (hist or {}).get("data") if isinstance(hist, dict) else None
    if not isinstance(rows, list):
        return []
    out = []
    for r in rows:
        try:
            out.append([int(float(r["x"]) / 1000), round(float(r["y"]), 1)])
        except (TypeError, ValueError, KeyError, IndexError):
            continue
    out.sort(key=lambda p: p[0])
    return out[-_HISTORY_KEEP:]


def record_history(us_series: list | None, kr_score: float | None,
                   path: str | Path = HISTORY_PATH, min_gap_sec: float = 1500,
                   cap: int = 400, now_fn=time.time) -> None:
    """공포지수 시계열을 별도 파일에 적재(대시보드 스파크라인 전용).

      us : CNN 이 권위 있는 1년치 시계열을 주므로 통째로 교체(없으면 기존 유지).
      kr : 그런 소스가 없어 우리가 누적한다. 장중 5분 슬라이스가 매번 쌓지 않도록
           마지막 포인트로부터 min_gap_sec 이상 지났을 때만 append.

    쓰기는 tmp+os.replace 로 원자적 — 비원자적 쓰기로 원장이 0바이트가 나 데몬이 멈춘
    전례가 있다. 이 함수도 예외를 밖으로 던지지 않는다(fail-open).
    """
    try:
        p = Path(path)
        data: dict = {}
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8")) or {}
            except json.JSONDecodeError as e:
                log.warning("공포지수 이력 파일 손상 — 빈 상태에서 재시작: %s", e)
                data = {}
        if not isinstance(data, dict):
            data = {}
        us = data.get("us") if isinstance(data.get("us"), list) else []
        kr = data.get("kr") if isinstance(data.get("kr"), list) else []
        if us_series:
            us = [[int(t), round(float(s), 1)] for t, s in us_series]
        if kr_score is not None:
            now = float(now_fn())
            last = float(kr[-1][0]) if kr else None
            if last is None or (now - last) >= min_gap_sec:
                kr.append([int(now), round(float(kr_score), 1)])
        if cap > 0:
            us, kr = us[-cap:], kr[-cap:]
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps({"us": us, "kr": kr}, ensure_ascii=False),
                       encoding="utf-8")
        os.replace(tmp, p)
    except Exception as e:
        log.warning("공포지수 이력 적재 실패: %s", e)


def fetch_cnn_fear_greed(ttl_sec: float = 1800, timeout: float = 12,
                         fetch_fn=None) -> dict | None:
    """CNN 공포탐욕지수 스냅샷. TTL 캐시. 실패 시 낡은 캐시(stale=True) 또는 None.

    fetch_fn: 테스트 주입용. 주면 파싱된 JSON dict 를 반환하는 무인자 콜러블로 취급한다.
    """
    now = time.time()
    if _cache["value"] is not None and (now - _cache["ts"]) < ttl_sec:
        return _cache["value"]
    try:
        if fetch_fn is not None:
            data = fetch_fn()
        else:
            r = requests.get(FEAR_GREED_URL, headers=_UA, timeout=timeout)
            r.raise_for_status()
            data = r.json()
        fg = (data or {}).get("fear_and_greed") or {}
        score = float(fg["score"])
        out: dict = {"score": round(score, 1), "rating": rating_of(score)}
        for key, src in _CNN_PREV:
            v = fg.get(src)
            if v is not None:
                out[key] = round(float(v), 1)
        comps = {}
        for k in _CNN_COMPONENTS:
            v = data.get(k)
            if isinstance(v, dict) and v.get("score") is not None:
                comps[k] = round(float(v["score"]), 1)
        if comps:
            out["components"] = comps
        if fg.get("timestamp") is not None:
            out["asof"] = fg["timestamp"]          # 원본 문자열 그대로
        hist = _parse_history(data.get("fear_and_greed_historical"))
        if hist:
            out["_history"] = hist                 # 언더스코어 = 내부용(assess 가 떼낸다)
        out["market"] = "US"
        out["source"] = "cnn"
        _cache["ts"], _cache["value"] = now, out
        return out
    except Exception as e:
        log.warning("CNN 공포탐욕지수 조회 실패: %s", e)
        # 낡은 값이라도 없는 것보다 낫다 — 소비자가 알 수 있게 stale 을 붙인다.
        if _cache["value"] is not None:
            return {**_cache["value"], "stale": True}
        return None


def index_stats(symbol: str = "^KS11", market: str = "KR", lookback: int = 20,
                ret_days: int = 5, max_age_hours: float = 6.0,
                history_fn=None) -> dict:
    """지수 일봉 1회 조회로 낙폭·최근 수익률을 함께 뽑는다(같은 캔들에서 계산).

      drawdown_pct : 최근 lookback 봉 종가 고점 대비 현재 낙폭(%). 항상 ≤ 0.
      ret_5d_pct   : ret_days 봉 전 종가 대비 현재 수익률(%).

    데이터가 부족한 항목은 키를 생략한다(둘 다 없으면 빈 dict).
    history_fn: 테스트 주입용 — history_fn(symbol, market) -> DataFrame.
    (^KS11 같은 지수 티커는 to_yahoo 가 그대로 통과시키므로 market="KR" 로 넘겨도 된다.)
    """
    try:
        if history_fn is not None:
            df = history_fn(symbol, market)
        else:
            df = fetch_history(symbol, interval="1d", range_="3mo", market=market,
                               max_age_hours=max_age_hours)
        if df is None or len(df) == 0 or "close" not in df:
            return {}
        close = df["close"].astype(float)
        last = float(close.iloc[-1])
        out: dict = {}
        if len(close) >= lookback:
            hi = float(close.tail(lookback).max())
            if hi:
                out["drawdown_pct"] = round((last / hi - 1) * 100, 1)
        if len(close) > ret_days:
            prev = float(close.iloc[-1 - ret_days])
            if prev:
                out["ret_5d_pct"] = round((last / prev - 1) * 100, 1)
        return out
    except Exception as e:
        log.warning("지수 통계 계산 실패 %s: %s", symbol, e)
        return {}


def index_drawdown_pct(symbol: str = "^KS11", market: str = "KR",
                       lookback: int = 20, max_age_hours: float = 6.0,
                       history_fn=None) -> float | None:
    """index_stats 의 낙폭만 뽑는 얇은 래퍼(단독 조회용)."""
    return index_stats(symbol, market, lookback, max_age_hours=max_age_hours,
                       history_fn=history_fn).get("drawdown_pct")


def vkospi_to_score(close: float) -> float:
    """VKOSPI 종가 → 0~100(낮을수록 공포). 표시/섀도용. score 가중치에는 쓰지 않는다."""
    return _clamp((40.0 - float(close)) / 30.0 * 100.0)


def kr_fear_proxy(regime_kr: dict | None, drawdown_pct: float | None,
                  ret_5d_pct: float | None, breadth_min_n: int = 10,
                  vkospi_close: float | None = None) -> dict | None:
    """KR 공포 대리지표 0~100(낮을수록 공포). 성분은 있는 것만 쓰고 가중치를 재정규화한다.

      breadth  0.5 : 유니버스 20일선 상회 비율 ×100 (표본 n 이 얇으면 성분에서 제외)
      drawdown 0.3 : 지수 낙폭 -15% → 0, 0% → 100 선형
      ret_5d   0.2 : 지수 5일 수익률 -10% → 0, +10% → 100 선형

    vkospi_close 가 와도 **점수에 넣지 않는다** — inputs.vkospi 만 남긴다(Open API 경로와 동일).
    """
    try:
        parts: dict = {}
        inputs: dict = {}
        r = regime_kr or {}
        breadth = r.get("breadth_above_ma20")
        try:
            n = int(r.get("n") or 0)
        except (TypeError, ValueError):
            n = 0
        if breadth is not None and n >= breadth_min_n:
            parts["breadth"] = _clamp(float(breadth) * 100)
            inputs["breadth_above_ma20"] = float(breadth)
            inputs["n"] = n
        if drawdown_pct is not None:
            parts["drawdown"] = _clamp((1 + float(drawdown_pct) / 15) * 100)
            inputs["index_drawdown_pct"] = float(drawdown_pct)
        if ret_5d_pct is not None:
            parts["ret_5d"] = _clamp((float(ret_5d_pct) + 10) / 20 * 100)
            inputs["index_ret_5d_pct"] = float(ret_5d_pct)
        if vkospi_close is not None:
            inputs["vkospi"] = float(vkospi_close)
        if not parts:
            return None
        wsum = sum(_KR_WEIGHTS[k] for k in parts)
        score = round(sum(v * _KR_WEIGHTS[k] for k, v in parts.items()) / wsum, 1)
        missing = [k for k in _KR_COMPONENTS if k not in parts]
        out = {"score": score, "rating": rating_of(score),
               "rating_basis": "absolute",
               "components": {k: round(v, 1) for k, v in parts.items()},
               "inputs": inputs, "market": "KR", "source": "proxy_kr",
               "note": _KR_NOTE, "missing": missing,
               "incomplete": bool(missing)}
        return out
    except Exception as e:
        log.warning("KR 공포 대리지표 계산 실패: %s", e)
        return None


def assess(state: dict, cfg=None) -> dict:
    """market_state(부분) dict → {"fear_greed": {...}, "fear_kr": {...}} 조립.

    실패한 쪽 키는 생략하고, 전부 실패하면 {} 를 준다. 어떤 경우에도 예외를 던지지 않는다.
    """
    try:
        ms_cfg: dict = {}
        if cfg is not None:
            ms_cfg = cfg.raw.get("market_state", {}) or {}
        fg_cfg: dict = ms_cfg.get("fear_greed", {}) or {}
        if not fg_cfg.get("enabled", True):
            return {}

        out: dict = {}
        us_hist = None
        us = fetch_cnn_fear_greed(ttl_sec=float(fg_cfg.get("ttl_sec", 1800)))
        if us:
            # 캐시 원본을 건드리지 않게 복사한 뒤 시계열만 떼낸다 — sentiment(=뇌 프롬프트)에
            # 배열이 실리면 컨텍스트가 노이즈로 부푼다. 시계열은 별도 파일로만 나간다.
            us = dict(us)
            us_hist = us.pop("_history", None)
            out["fear_greed"] = us
        kr_score = None
        if fg_cfg.get("kr_proxy", True):
            st = state or {}
            regime_kr = (st.get("regime") or {}).get("KR")
            stats = index_stats(
                symbol=fg_cfg.get("kr_index", "^KS11"),
                lookback=int(fg_cfg.get("kr_drawdown_lookback", 20)),
                max_age_hours=float(fg_cfg.get("kr_drawdown_max_age_hours", 6)))
            # breadth_min_n 은 전용 키가 없으면 regime 채택 기준(기존 키)을 그대로 쓴다.
            min_n = int(fg_cfg.get("breadth_min_n", ms_cfg.get("breadth_min_n", 10)))
            # 웹 로그인 VkospiSource 가 채운 슬롯은 inputs 폴백용(점수 미반영).
            vk = (st.get("vkospi") or {}).get("close")
            try:
                vk_f = float(vk) if vk is not None else None
            except (TypeError, ValueError):
                vk_f = None
            kr = kr_fear_proxy(regime_kr, stats.get("drawdown_pct"),
                               stats.get("ret_5d_pct"), breadth_min_n=min_n,
                               vkospi_close=vk_f)
            if kr:
                hist_path = fg_cfg.get("history_path", HISTORY_PATH)
                apply_kr_rating(kr, load_kr_scores(hist_path))
                if fg_cfg.get("krx_enrich", True):
                    try:
                        from .krx_open import (DEFAULT_CACHE_PATH, DEFAULT_TTL_SEC,
                                               merge_into_fear_kr, refresh_fear_cache)
                        cache = refresh_fear_cache(
                            path=fg_cfg.get("krx_cache_path", DEFAULT_CACHE_PATH),
                            ttl_sec=float(fg_cfg.get("krx_ttl_sec", DEFAULT_TTL_SEC)),
                            force=bool(fg_cfg.get("krx_force_refresh", False)),
                        )
                        merge_into_fear_kr(kr, cache)
                    except Exception as e:
                        log.warning("KRX fear enrich 스킵: %s", e)
                out["fear_kr"] = kr
                kr_score = kr.get("score")
        if fg_cfg.get("history", True):
            record_history(us_hist, kr_score,
                           path=fg_cfg.get("history_path", HISTORY_PATH))
        return out
    except Exception as e:
        log.warning("공포지수 조립 실패: %s", e)
        return {}


def summary_line(sentiment: dict | None) -> str:
    """로그용 한 줄 요약. 값이 하나도 없으면 빈 문자열(호출부가 로그를 생략)."""
    bits = []
    for key, tag in (("fear_greed", "US"), ("fear_kr", "KR")):
        v = (sentiment or {}).get(key)
        if isinstance(v, dict) and v.get("score") is not None:
            stale = "/stale" if v.get("stale") else ""
            inc = "/inc" if v.get("incomplete") else ""
            basis = v.get("rating_basis")
            tag_b = "/pct" if basis == "percentile" else ("/abs" if basis == "absolute" else "")
            bits.append(f"{tag}={v['score']}({v.get('rating')}{stale}{inc}{tag_b})")
    return f"공포지수 {' '.join(bits)}" if bits else ""
