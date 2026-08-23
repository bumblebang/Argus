"""공유 KRX 정보데이터 클라이언트 (로그인 + bld JSON).

자격(KRX_USER/KRX_PASS) 없거나 로그인 실패 시 None — 호출부는 fail-open.

로그인: 회원제 전환 후 MDCCOMS001D1.cmd (구 loginProc.cmd 폐기).
중복 로그인(CD011) 시 skipDup=Y 로 기존 세션 끊고 재시도.
"""
from __future__ import annotations

import json
import os
import time
from datetime import date
from pathlib import Path
from typing import Any

import requests

from ..logging_setup import get_logger

log = get_logger("src.krx_client")

KRX_HOME = "https://data.krx.co.kr/contents/MDC/MAIN/main/index.cmd"
KRX_LOGIN_PAGE = "https://data.krx.co.kr/contents/MDC/COMS/client/MDCCOMS001.cmd"
KRX_LOGIN_IFRAME = "https://data.krx.co.kr/contents/MDC/COMS/client/view/login.jsp?site=mdc"
KRX_LOGIN = "https://data.krx.co.kr/contents/MDC/COMS/client/MDCCOMS001D1.cmd"
KRX_JSON = "https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"
CACHE_ROOT = Path(__file__).resolve().parents[2] / "data" / "krx_cache"

# CD001=성공, CD011=중복로그인(skipDup), CD006=비번불일치 등
_LOGIN_FAIL = frozenset({"CD005", "CD006", "CD007"})
_LOGIN_OK = frozenset({"CD001", "0000", ""})

MARKET_ID = {"KOSPI": "STK", "KOSDAQ": "KSQ", "KONEX": "KNX", "ALL": "ALL"}
MKT_TP = {"KOSPI": "1", "KOSDAQ": "2", "KONEX": "3"}


def ymd(d: date | None = None) -> str:
    return (d or date.today()).strftime("%Y%m%d")


def ymd_dash(s: str | None) -> str | None:
    if not s:
        return None
    raw = str(s).replace("/", "").replace("-", "").strip()
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    return str(s)[:10] if len(str(s)) >= 10 else None


def num(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", "").replace("+", "").strip())
    except (TypeError, ValueError):
        return None


def rows_of(body: dict | None) -> list[dict]:
    if not isinstance(body, dict):
        return []
    for key in ("OutBlock_1", "output", "block1", "outBlock1", "DATA"):
        v = body.get(key)
        if isinstance(v, list):
            return [r for r in v if isinstance(r, dict)]
    return []


def load_catalog(path: Path | None = None) -> dict:
    p = path or Path(__file__).resolve().parents[2] / "data" / "krx_bld_catalog.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {"entries": []}


def bld_for(entry_id: str, catalog: dict | None = None) -> str | None:
    cat = catalog if catalog is not None else load_catalog()
    for e in cat.get("entries") or []:
        if e.get("id") == entry_id:
            return e.get("bld")
    return None


def cache_get(slot: str, day: str) -> Any | None:
    path = CACHE_ROOT / slot / f"{day}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None


def cache_put(slot: str, day: str, payload: Any) -> None:
    path = CACHE_ROOT / slot / f"{day}.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError as e:
        log.warning("krx cache 저장 실패 %s: %s", path, e)


def _login_payload(user: str, password: str, *, skip_dup: bool = False) -> dict:
    data = {
        "mbrNm": "", "telNo": "", "di": "", "certType": "",
        "mbrId": user, "pw": password, "mbrNo": "",
        "isUseRuleOk": "Y",
    }
    if skip_dup:
        data["skipDup"] = "Y"
    return data


def _login_ok(body: dict) -> bool:
    code = str(body.get("_error_code") or "").strip()
    if code in _LOGIN_FAIL or code == "CD011":
        return False
    if code in _LOGIN_OK or body.get("MBR_NO"):
        return True
    return "_error_message" not in body


class KrxClient:
    """세션 로그인 + get_json. 실패 시 ok=False."""

    def __init__(self, user: str | None = None, password: str | None = None,
                 spacing_sec: float = 0.25):
        self.user = (user if user is not None else (os.getenv("KRX_USER") or "")).strip()
        self.password = (password if password is not None
                         else (os.getenv("KRX_PASS") or "")).strip()
        self.spacing = spacing_sec
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/122.0.0.0 Safari/537.36"),
            "Accept-Language": "ko-KR,ko;q=0.9",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": KRX_HOME,
            "Origin": "https://data.krx.co.kr",
        })
        self.ok = False
        self._last_call = 0.0

    @property
    def has_creds(self) -> bool:
        return bool(self.user and self.password)

    def _post_login(self, *, skip_dup: bool = False) -> dict | None:
        r = self.s.post(
            KRX_LOGIN,
            data=_login_payload(self.user, self.password, skip_dup=skip_dup),
            timeout=20,
            headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Referer": KRX_LOGIN_IFRAME,
                "Origin": "https://data.krx.co.kr",
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        text = (r.text or "").strip()
        if text == "LOGOUT" or r.status_code >= 400:
            log.warning("KRX 로그인 HTTP 실패 status=%s", r.status_code)
            return None
        try:
            body = r.json()
        except ValueError:
            log.warning("KRX 로그인 비JSON 응답 head=%s", text[:80])
            return None
        return body if isinstance(body, dict) else None

    def login(self) -> bool:
        if not self.has_creds:
            return False
        try:
            # 브라우저와 동일: 홈 → 로그인페이지 → iframe → MDCCOMS001D1
            self.s.get(KRX_HOME, timeout=15)
            self.s.get(KRX_LOGIN_PAGE, timeout=15)
            self.s.get(KRX_LOGIN_IFRAME, timeout=15)
            body = self._post_login(skip_dup=False)
            if body is None:
                self.ok = False
                return False
            if str(body.get("_error_code") or "") == "CD011":
                body = self._post_login(skip_dup=True)
                if body is None:
                    self.ok = False
                    return False
            if not _login_ok(body):
                log.warning("KRX 로그인 거절 code=%s msg=%s",
                            body.get("_error_code"),
                            str(body.get("_error_message") or "")[:60])
                self.ok = False
                return False
            self.ok = True
            log.info("KRX 로그인 성공 mbr=%s", body.get("MBR_NO"))
            return True
        except Exception as e:
            log.warning("KRX 로그인 예외: %s", e)
            self.ok = False
            return False

    def _throttle(self) -> None:
        if self.spacing <= 0:
            return
        elapsed = time.monotonic() - self._last_call
        if elapsed < self.spacing:
            time.sleep(self.spacing - elapsed)

    def get_json(self, bld: str, *, relogin: bool = True, **params) -> dict | None:
        if not self.ok:
            return None
        self._throttle()
        data = {"bld": bld, "locale": "ko_KR", "csvxls_isNo": "false", **params}
        try:
            r = self.s.post(KRX_JSON, data=data, timeout=30)
            self._last_call = time.monotonic()
            text = (r.text or "").strip()
            if text == "LOGOUT":
                log.warning("KRX LOGOUT — 세션 만료 bld=%s", bld)
                self.ok = False
                if relogin and self.login():
                    return self.get_json(bld, relogin=False, **params)
                return None
            r.raise_for_status()
            body = r.json()
            return body if isinstance(body, dict) else None
        except Exception as e:
            log.warning("KRX get_json 실패 bld=%s: %s", bld, e)
            return None

    def get_rows(self, bld: str, **params) -> list[dict]:
        return rows_of(self.get_json(bld, **params))


def connect(user: str | None = None, password: str | None = None,
            spacing_sec: float = 0.25) -> KrxClient | None:
    """자격 있고 로그인 성공하면 client, 아니면 None."""
    c = KrxClient(user=user, password=password, spacing_sec=spacing_sec)
    if not c.has_creds:
        return None
    return c if c.login() else None


__all__ = ["KrxClient", "connect", "ymd", "ymd_dash", "num", "rows_of",
           "load_catalog", "bld_for", "cache_get", "cache_put",
           "MARKET_ID", "MKT_TP", "CACHE_ROOT", "_login_ok", "_login_payload"]
