"""KRX Open API — fear_kr 부가입력(VKOSPI·풋콜). 전일 종가만, fail-open.

인증: .env 의 KRX_API_KEY (헤더 AUTH_KEY). positioning 의 KRX_USER/PASS 웹 로그인과 별개.
일별 데이터만 제공(장중 실시간 없음). 캐시로 장중 5분 슬라이스가 네트워크를 안 치게 한다.
"""
from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

from ..logging_setup import get_logger

log = get_logger("src.krx_open")

BASE = "https://data-dbg.krx.co.kr/svc/apis"
PATH_DRVPROD = "/idx/drvprod_dd_trd"
PATH_OPT = "/drv/opt_bydd_trd"
PATH_KOSPI = "/idx/kospi_dd_trd"

DEFAULT_CACHE_PATH = "data/krx_fear_cache.json"
DEFAULT_TTL_SEC = 21600  # 6h — 장중 슬라이스는 캐시 hit

# 공식 VKOSPI 표기. 레버리지·저/고변동성 테마지수는 제외.
_VKOSPI_EXACT = ("코스피 200 변동성지수", "KOSPI 200 변동성지수", "VKOSPI")
_VKOSPI_EXCLUDE = ("레버리지", "목표", "저변동", "고변동", "중저", "중고", "커버드콜",
                   "변동성매도", "변동성매수")


def _num(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _api_key() -> str:
    return (os.getenv("KRX_API_KEY") or "").strip()


def recent_bas_dds(n: int = 10, today: date | None = None) -> list[str]:
    """최근 평일 YYYYMMDD (오늘 제외 — Open API 는 전일까지)."""
    d = today or date.today()
    out: list[str] = []
    i = 1
    while len(out) < n and i < 30:
        cand = d - timedelta(days=i)
        if cand.weekday() < 5:
            out.append(cand.strftime("%Y%m%d"))
        i += 1
    return out


def get_json(path: str, bas_dd: str, api_key: str | None = None,
             timeout: float = 45.0) -> dict | None:
    """GET Open API. 실패·401·비JSON 은 None (예외 안 던짐)."""
    key = (api_key if api_key is not None else _api_key()).strip()
    if not key:
        return None
    try:
        r = requests.get(
            BASE + path,
            headers={"AUTH_KEY": key, "User-Agent": "argus-krx-open"},
            params={"basDd": bas_dd},
            timeout=timeout,
        )
        try:
            data = r.json()
        except ValueError:
            log.warning("KRX Open API 비JSON %s http=%s", path, r.status_code)
            return None
        if not isinstance(data, dict):
            return None
        code = str(data.get("respCode") or "")
        if r.status_code == 401 or code == "401":
            log.warning("KRX Open API 미승인/거부 %s: %s",
                        path, data.get("respMsg") or code)
            return None
        if r.status_code != 200:
            log.warning("KRX Open API HTTP %s %s: %s",
                        r.status_code, path, data.get("respMsg"))
            return None
        return data
    except Exception as e:
        log.warning("KRX Open API 조회 실패 %s: %s", path, e)
        return None


def rows_of(payload: dict | None) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    rows = payload.get("OutBlock_1")
    return rows if isinstance(rows, list) else []


def pick_vkospi(rows: list[dict]) -> tuple[float | None, str | None]:
    """파생상품지수 행에서 VKOSPI 종가. (값, 지수명) 또는 (None, None)."""
    exact: list[tuple[float, str]] = []
    fuzzy: list[tuple[float, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        nm = str(row.get("IDX_NM") or "").strip()
        if not nm:
            continue
        if any(x in nm for x in _VKOSPI_EXCLUDE):
            continue
        px = _num(row.get("CLSPRC_IDX"))
        if px is None:
            continue
        if nm in _VKOSPI_EXACT or nm.upper() == "VKOSPI":
            exact.append((px, nm))
            continue
        # '코스피 200 변동성지수' 변형 — 끝부분이 변동성지수이고 군더더기 없음
        if nm.endswith("변동성지수") and "200" in nm:
            exact.append((px, nm))
            continue
        if "변동성지수" in nm and "200" in nm:
            fuzzy.append((px, nm))
    if exact:
        return exact[0]
    if fuzzy:
        return fuzzy[0]
    return None, None


def put_call_from_rows(rows: list[dict]) -> dict | None:
    """옵션 일별행 → put_vol / call_vol / put_call_ratio."""
    put_vol = call_vol = 0.0
    for row in rows:
        if not isinstance(row, dict):
            continue
        tp = str(row.get("RGHT_TP_NM") or "")
        vol = _num(row.get("ACC_TRDVOL")) or 0.0
        up = tp.upper()
        if "PUT" in up or "풋" in tp:
            put_vol += vol
        elif "CALL" in up or "콜" in tp:
            call_vol += vol
    if call_vol <= 0 and put_vol <= 0:
        return None
    out: dict = {
        "put_vol": int(put_vol),
        "call_vol": int(call_vol),
    }
    if call_vol > 0:
        out["put_call_ratio"] = round(put_vol / call_vol, 3)
    return out


def fetch_vkospi(bas_dd: str, api_key: str | None = None,
                 get_fn=None) -> tuple[float | None, str | None, str | None]:
    """(vkospi, name, bas_dd_used). 빈 응답이면 (None, None, None)."""
    getter = get_fn or get_json
    data = getter(PATH_DRVPROD, bas_dd, api_key=api_key)
    rows = rows_of(data)
    if not rows:
        return None, None, None
    px, nm = pick_vkospi(rows)
    if px is None:
        return None, None, bas_dd
    return px, nm, bas_dd


def fetch_put_call(bas_dd: str, api_key: str | None = None,
                   get_fn=None) -> dict | None:
    getter = get_fn or get_json
    data = getter(PATH_OPT, bas_dd, api_key=api_key)
    rows = rows_of(data)
    if not rows:
        return None
    out = put_call_from_rows(rows)
    if out:
        out["bas_dd"] = bas_dd
    return out


def load_cache(path: str | Path = DEFAULT_CACHE_PATH) -> dict | None:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def save_cache(payload: dict, path: str | Path = DEFAULT_CACHE_PATH) -> None:
    """원자적 저장. 실패해도 예외 안 던짐."""
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(p)
    except OSError as e:
        log.warning("KRX fear 캐시 저장 실패: %s", e)


def cache_fresh(cache: dict | None, ttl_sec: float,
                now: float | None = None) -> bool:
    if not isinstance(cache, dict):
        return False
    try:
        ts = float(cache.get("ts") or 0)
    except (TypeError, ValueError):
        return False
    t = time.time() if now is None else now
    return ts > 0 and (t - ts) <= ttl_sec


def refresh_fear_cache(
    path: str | Path = DEFAULT_CACHE_PATH,
    ttl_sec: float = DEFAULT_TTL_SEC,
    api_key: str | None = None,
    force: bool = False,
    get_fn=None,
    now_fn=time.time,
    today: date | None = None,
) -> dict | None:
    """VKOSPI+풋콜을 묶어 캐시. TTL hit 이면 기존 반환. 키 없거나 전부 실패면 기존/None.

    force=True 이면 TTL 무시하고 재조회(장전 배치용).
    """
    cached = load_cache(path)
    if not force and cache_fresh(cached, ttl_sec, now=now_fn()):
        return cached

    key = (api_key if api_key is not None else _api_key()).strip()
    if not key:
        return cached

    vkospi = name = bas_used = None
    pc: dict | None = None
    for bas in recent_bas_dds(today=today):
        if vkospi is None:
            px, nm, used = fetch_vkospi(bas, api_key=key, get_fn=get_fn)
            if px is not None:
                vkospi, name, bas_used = px, nm, used
        if pc is None:
            pc = fetch_put_call(bas, api_key=key, get_fn=get_fn)
        if vkospi is not None and pc is not None:
            break

    if vkospi is None and pc is None:
        log.warning("KRX fear enrich 실패 — 캐시 유지")
        return cached

    out: dict = {
        "ts": now_fn(),
        "asof": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "krx_open",
        "bas_dd": (pc or {}).get("bas_dd") or bas_used,
    }
    if vkospi is not None:
        out["vkospi"] = round(float(vkospi), 2)
        if name:
            out["vkospi_name"] = name
    if pc:
        for k in ("put_call_ratio", "put_vol", "call_vol", "bas_dd"):
            if k in pc and pc[k] is not None:
                out[k] = pc[k]
        if not out.get("bas_dd") and pc.get("bas_dd"):
            out["bas_dd"] = pc["bas_dd"]

    save_cache(out, path)
    return out


def merge_into_fear_kr(kr: dict, cache: dict | None) -> dict:
    """fear_kr.inputs 에 KRX 부가필드 부착. score/components/incomplete 는 건드리지 않음."""
    if not isinstance(kr, dict) or not isinstance(cache, dict):
        return kr
    inp = kr.get("inputs")
    if not isinstance(inp, dict):
        inp = {}
        kr["inputs"] = inp
    if cache.get("vkospi") is not None:
        try:
            inp["vkospi"] = float(cache["vkospi"])
        except (TypeError, ValueError):
            pass
    if cache.get("put_call_ratio") is not None:
        try:
            inp["put_call_ratio"] = float(cache["put_call_ratio"])
        except (TypeError, ValueError):
            pass
    for k in ("put_vol", "call_vol"):
        if cache.get(k) is not None:
            try:
                inp[k] = int(cache[k])
            except (TypeError, ValueError):
                pass
    if cache.get("bas_dd"):
        inp["krx_bas_dd"] = str(cache["bas_dd"])
    if cache.get("vkospi_name"):
        inp["vkospi_name"] = str(cache["vkospi_name"])
    return kr


def probe_services(api_key: str | None = None, bas_dd: str | None = None) -> dict:
    """doctor/스모크용 — 키·서비스 승인만. 값은 요약 플래그."""
    key = (api_key if api_key is not None else _api_key()).strip()
    out = {"key": bool(key), "kospi": False, "vkospi": False, "opt": False}
    if not key:
        return out
    bas = bas_dd or (recent_bas_dds(1)[0] if recent_bas_dds(1) else None)
    if not bas:
        return out
    if rows_of(get_json(PATH_KOSPI, bas, api_key=key)):
        out["kospi"] = True
    px, _, _ = fetch_vkospi(bas, api_key=key)
    out["vkospi"] = px is not None
    out["opt"] = fetch_put_call(bas, api_key=key) is not None
    return out
