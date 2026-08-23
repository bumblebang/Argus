"""실적발표 캘린더 + 컨센서스 소스 (KR 네이버 / US Finnhub).

"언제 발표하고, 시장은 뭘 기대하는가"를 코드가 미리 가져다 놓는다. 판단(회피할지
오히려 노릴지)은 전부 뇌(LLM)가 한다 — 이 모듈은 게이트를 만들지 않는다.

표준형(마켓 무관):
  {"market", "symbol", "date", "dday", "title", "hour",
   "consensus": {...}|None, "analyst": {...}|None, "surprise_history": [...]}

모든 함수는 실패해도 예외를 던지지 않는다(None/{}/[] + log.warning) — 한 종목 실패가
장전 배치나 워처를 죽이면 안 된다. finnhub.py 의 방어 스타일과 동일.
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from statistics import median

import requests

from ..logging_setup import get_logger

log = get_logger("src.earnings")

NAVER_BASE = "https://m.stock.naver.com/api/stock"
FINNHUB_BASE = "https://finnhub.io/api/v1"
_UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://m.stock.naver.com/"}

# 네이버 분기 재무 표에서 뽑을 행 제목 → 표준 키
_KR_ROWS = {"매출액": "revenue", "영업이익": "op_profit",
            "당기순이익": "net_income", "EPS": "eps"}
# 컨센서스 매출이 직전 실적 분기 중앙값의 이 배수를 넘으면 단위·기간 의심(suspect)
_SUSPECT_RATIO = 2.0
_CAL_WINDOW_DAYS = 7      # Finnhub 캘린더 1콜당 조회 창(피크 주 ~1400행)
_CAL_MAX_ROWS = 1500      # 응답 행 상한 — 넘으면 '앞 날짜부터' 잘려 나간다(실측)


def dday_of(e: dict | None) -> int | None:
    """캘린더 항목의 **오늘 기준** D-day. 없으면 None.

    저장된 dday 는 장전 배치 시점의 스냅샷이라 배치가 하루라도 밀리면 낡는다
    (임박 판정이 정작 D-1 에 안 켜지거나, 반대로 켜진 채 DART 쿼터를 태운다).
    date 가 있으면 읽는 쪽에서 매번 다시 계산한다 — 저장값은 폴백일 뿐.
    """
    if not e:
        return None
    d = e.get("date")
    if d:
        try:
            return (datetime.strptime(str(d), "%Y-%m-%d").date() - date.today()).days
        except (ValueError, TypeError):
            pass
    v = e.get("dday")
    return int(v) if isinstance(v, (int, float)) else None


def with_fresh_dday(e: dict | None) -> dict | None:
    """dday 를 오늘 기준으로 갱신한 사본. 원본(캐시된 캘린더)은 건드리지 않는다."""
    if not e:
        return e
    return dict(e, dday=dday_of(e))


def _num(v) -> float | None:
    """네이버 문자열 숫자("1,738,644", "513,958", "-") → float. 결측이면 None."""
    if v is None:
        return None
    s = str(v).replace(",", "").strip()
    if not s or s in ("-", "N/A"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _get_json(url: str, *, params: dict | None = None, timeout: int = 15,
              headers: dict | None = None):
    r = requests.get(url, params=params, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _kr_consensus(fin: dict, code: str) -> dict | None:
    """financeInfo(분기) → 컨센서스 컬럼 파싱. 컨센서스 컬럼이 없으면 None.

    trTitleList 의 isConsensus=="Y" 인 key 가 다음 분기 추정 컬럼이다. 값은 억원 단위
    콤마 문자열. 매출 컨센서스가 직전 실적 분기 중앙값의 2배를 넘으면(누적치·단위 불일치
    의심) 값은 살리되 suspect 플래그를 켠다 — 봇이 틀린 숫자로 판단하지 않게.
    """
    titles = fin.get("trTitleList") or []
    rows = fin.get("rowList") or []
    ckey = next((t.get("key") for t in titles if t.get("isConsensus") == "Y"), None)
    if not ckey:
        return None
    by_title = {r.get("title"): (r.get("columns") or {}) for r in rows}
    out: dict = {"period": ckey, "unit": "억원", "suspect": False}
    for title, key in _KR_ROWS.items():
        cols = by_title.get(title) or {}
        out[key] = _num((cols.get(ckey) or {}).get("value"))
    if all(out.get(k) is None for k in _KR_ROWS.values()):
        return None                     # 컬럼만 있고 값은 전무(추정 미제공) → 없는 것으로

    # 직전 실적 분기(컨센서스 아님) 매출 4개의 중앙값과 비교
    hist_keys = [t.get("key") for t in titles if t.get("isConsensus") != "Y"][-4:]
    rev_cols = by_title.get("매출액") or {}
    hist = [v for v in ((_num((rev_cols.get(k) or {}).get("value"))) for k in hist_keys)
            if v]
    if out.get("revenue") and hist and out["revenue"] > _SUSPECT_RATIO * median(hist):
        out["suspect"] = True
        log.warning("[%s] 컨센서스 매출 %s(%s)이 직전 %d분기 중앙값 %s의 %.1f배 — "
                    "단위·기간 불일치 의심(suspect)", code, out["revenue"], ckey,
                    len(hist), median(hist), out["revenue"] / median(hist))
    return out


def fetch_kr_earnings(code: str, timeout: int = 15) -> dict | None:
    """한국 종목(6자리)의 다음 실적발표 일정 + 컨센서스 + 애널리스트 지표.

    네이버 모바일 무인증 API 2콜(integration = IR 일정·컨센서스 요약,
    finance/quarter = 분기 재무 추정치). 일정(irScheduleInfo)은 null 일 수 있으므로
    date/dday/title 이 전부 None 이어도 컨센서스만으로 유효한 dict 를 돌려준다.
    실패 시 None(호출측이 그 종목만 건너뛰게).
    """
    try:
        integ = _get_json(f"{NAVER_BASE}/{code}/integration", headers=_UA, timeout=timeout)
    except Exception as e:
        log.warning("[%s] 네이버 실적일정 조회 실패: %s", code, e)
        return None
    if not isinstance(integ, dict):
        return None

    sched = integ.get("irScheduleInfo") or {}          # null 가능 — 반드시 방어
    out: dict = {"market": "KR", "symbol": code,
                 "date": sched.get("irScheduleDate"),
                 "dday": sched.get("irScheduleDday"),
                 "title": sched.get("title"), "hour": None,
                 "consensus": None, "analyst": None, "surprise_history": []}

    cons_info = integ.get("consensusInfo") or {}
    recomm = _num(cons_info.get("recommMean"))
    target = _num(cons_info.get("priceTargetMean"))
    if recomm is not None or target is not None:
        out["analyst"] = {"recomm_mean": recomm, "target_price": target}

    try:
        fq = _get_json(f"{NAVER_BASE}/{code}/finance/quarter", headers=_UA, timeout=timeout)
        fin = (fq or {}).get("financeInfo") or {}
        out["consensus"] = _kr_consensus(fin, code)
    except Exception as e:
        log.warning("[%s] 네이버 분기 컨센서스 조회 실패(일정만 사용): %s", code, e)
    return out


def _us_row_to_std(row: dict, today: date) -> dict:
    d = row.get("date") or None
    dday = None
    if d:
        try:
            dday = (datetime.strptime(d, "%Y-%m-%d").date() - today).days
        except ValueError:
            d = None
    year, quarter = row.get("year"), row.get("quarter")
    eps, rev = row.get("epsEstimate"), row.get("revenueEstimate")
    cons = ({"eps": eps, "revenue": rev, "unit": "USD", "suspect": False}
            if (eps is not None or rev is not None) else None)
    return {"market": "US", "symbol": row.get("symbol"), "date": d, "dday": dday,
            "title": (f"{year} Q{quarter} 실적발표" if year and quarter else None),
            "hour": (row.get("hour") or None),
            "consensus": cons, "analyst": None, "surprise_history": []}


def _calendar_window(api_key: str, frm: date, to: date, timeout: int,
                     spacing_sec: float, depth: int = 0) -> list[dict]:
    """캘린더 한 창 조회. 실패 시 [].

    Finnhub 응답은 1500행 상한이고 **초과분을 앞 날짜부터 잘라낸다**(60일 범위를
    한 번에 부르면 오늘~3주치가 통째로 사라진다). 상한에 닿으면 창을 반으로 쪼개 재조회해
    가까운 날짜를 잃지 않게 한다.
    """
    try:
        data = _get_json(FINNHUB_BASE + "/calendar/earnings", timeout=timeout, params={
            "from": frm.isoformat(), "to": to.isoformat(), "token": api_key})
    except Exception as e:
        log.warning("Finnhub 실적 캘린더 조회 실패(%s~%s): %s", frm, to, e)
        return []
    rows = (data or {}).get("earningsCalendar") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return []
    if len(rows) >= _CAL_MAX_ROWS and frm < to and depth < 3:
        mid = frm + timedelta(days=(to - frm).days // 2)
        log.warning("실적 캘린더 응답 상한(%d행) — %s~%s 창 분할 재조회", len(rows), frm, to)
        time.sleep(spacing_sec)
        left = _calendar_window(api_key, frm, mid, timeout, spacing_sec, depth + 1)
        time.sleep(spacing_sec)
        return left + _calendar_window(api_key, mid + timedelta(days=1), to, timeout,
                                       spacing_sec, depth + 1)
    return rows


def fetch_us_earnings(api_key: str, symbols: list[str], days_ahead: int = 60,
                      days_back: int = 3, timeout: int = 20,
                      spacing_sec: float = 1.1) -> dict[str, dict]:
    """미국 종목들의 다음 실적발표 일정 + EPS/매출 컨센서스. 반환 {symbol: 표준형}.

    종목별 조회(무료 60콜/분 소진)를 하지 않고 **범위 조회 + 로컬 필터**를 쓴다. 다만
    응답 1500행 상한 때문에 범위를 통째로 한 콜에 부를 수 없어(앞 날짜가 잘림) 7일 창으로
    끊어 가까운 날부터 훑고, 대상 심볼 전부가 미래 일정을 얻으면 조기 종료한다(범위 안에
    발표가 아예 없는 종목이 있으면 창 수만큼 = 60일 기준 10콜, 사이 1.1초 간격).
    한 심볼에 여러 행이 오면 오늘 이후 가장 가까운 날짜, 없으면 가장 최근 과거 행.
    """
    if not api_key or not symbols:
        return {}
    today = date.today()
    wanted = set(symbols)
    start, end = today - timedelta(days=days_back), today + timedelta(days=days_ahead)
    out: dict[str, dict] = {}
    frm = start
    while frm <= end:
        to = min(frm + timedelta(days=_CAL_WINDOW_DAYS - 1), end)
        for row in _calendar_window(api_key, frm, to, timeout, spacing_sec):
            sym = row.get("symbol")
            if sym not in wanted or not row.get("date"):
                continue
            std = _us_row_to_std(row, today)
            if std["dday"] is None:
                continue
            prev = out.get(sym)
            if prev is None or _us_better(std, prev):
                out[sym] = std
        # 미래 일정을 전부 확보했으면 뒤 창은 더 먼 날짜뿐 — 조기 종료(콜 절약)
        if all((out.get(s) or {}).get("dday", -1) >= 0 for s in wanted):
            break
        frm = to + timedelta(days=1)
        if frm <= end:
            time.sleep(spacing_sec)
    return out


def _us_better(new: dict, cur: dict) -> bool:
    """더 적합한 행인가 — 미래(dday>=0) 중 가장 가까운 날, 없으면 가장 최근 과거."""
    a, b = new["dday"], cur["dday"]
    if a >= 0 and b >= 0:
        return a < b
    if a >= 0:                # 미래가 과거를 이긴다
        return True
    if b >= 0:
        return False
    return a > b              # 둘 다 과거면 더 최근(덜 음수)


def _surprise_pct(actual, estimate) -> float | None:
    """컨센서스 대비 편차%. 계산 불가면 None — 절대 0이나 추정치로 채우지 않는다.

    estimate 가 없거나 0이면(적자전환 컨센서스 등) 편차율 자체가 무의미하다. 여기서
    억지로 숫자를 만들면 뇌가 없는 서프라이즈를 근거로 매매한다.
    """
    if actual is None or estimate is None:
        return None
    try:
        if float(estimate) == 0:
            return None
        return round((float(actual) - float(estimate)) / abs(float(estimate)) * 100, 2)
    except (TypeError, ValueError):
        return None


def _us_result_to_std(row: dict) -> dict:
    """Finnhub 캘린더 행(actual 채워진) → 실적 결과 표준형."""
    eps_e, eps_a = row.get("epsEstimate"), row.get("epsActual")
    rev_e, rev_a = row.get("revenueEstimate"), row.get("revenueActual")
    return {"symbol": row.get("symbol"), "date": row.get("date"),
            "quarter": row.get("quarter"), "year": row.get("year"),
            "hour": (row.get("hour") or None),
            "eps_estimate": eps_e, "eps_actual": eps_a,
            "revenue_estimate": rev_e, "revenue_actual": rev_a,
            "eps_surprise_pct": _surprise_pct(eps_a, eps_e),
            "revenue_surprise_pct": _surprise_pct(rev_a, rev_e)}


def fetch_us_results(api_key: str, symbols: list[str], days_back: int = 3,
                     days_ahead: int = 1, timeout: int = 20) -> dict[str, dict]:
    """미국 종목들의 **이미 발표된** 실적(컨센서스 대비 실제 편차). 반환 {symbol: 표준형}.

    봇은 발표 예정일과 컨센서스는 이미 안다(fetch_us_earnings) — 모르는 건 '실제 숫자'다.
    Finnhub /calendar/earnings 는 발표 당일 epsActual/revenueActual 을 채우므로(실측),
    최근 창을 훑어 actual 이 들어온 행만 골라낸다. 창이 최대 5일(=_CAL_WINDOW_DAYS 이내)
    이라 캘린더 1콜이면 끝난다.

    한 심볼에 여러 행이면 가장 최근 날짜 행. 실패 시 {}(호출측·워처가 죽지 않게).
    """
    if not api_key or not symbols:
        return {}
    today = date.today()
    wanted = set(symbols)
    frm, to = today - timedelta(days=days_back), today + timedelta(days=days_ahead)
    out: dict[str, dict] = {}
    try:
        for row in _calendar_window(api_key, frm, to, timeout, 1.1):
            sym = row.get("symbol")
            if sym not in wanted or not row.get("date"):
                continue
            if row.get("epsActual") is None and row.get("revenueActual") is None:
                continue                      # 아직 발표 전(예정만) → 결과가 아니다
            std = _us_result_to_std(row)
            prev = out.get(sym)
            if prev is None or str(std["date"]) > str(prev["date"]):
                out[sym] = std
    except Exception as e:
        log.warning("Finnhub 실적 결과 조회 실패: %s", e)
        return {}
    return out


def fetch_us_surprise_history(api_key: str, symbol: str, n: int = 4,
                              timeout: int = 20) -> list[dict]:
    """미국 종목의 과거 분기 서프라이즈 이력 최근 n개. 실패 시 [].

    뇌가 "이 종목이 습관적으로 컨센서스를 상회하는지"를 읽는 근거. 무료 티어에서
    /stock/earnings 는 열려 있다(eps-estimate·price-target 은 403이라 쓰지 않는다).
    """
    if not api_key:
        return []
    try:
        data = _get_json(FINNHUB_BASE + "/stock/earnings", timeout=timeout,
                         params={"symbol": symbol, "token": api_key})
    except Exception as e:
        log.warning("[%s] 서프라이즈 이력 조회 실패: %s", symbol, e)
        return []
    if not isinstance(data, list):
        return []
    out: list[dict] = []
    for d in data[:n]:
        out.append({"period": d.get("period"), "estimate": d.get("estimate"),
                    "actual": d.get("actual"), "surprise_pct": d.get("surprisePercent")})
    return out
