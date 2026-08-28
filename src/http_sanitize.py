"""HTTP·로그 시크릿 마스킹 — query/path/env 값이 로그에 평문으로 남지 않게.

logging_setup.RedactingFormatter 가 모든 핸들러에 적용해 호출부 redact 누락을 막는다.
"""
from __future__ import annotations

import os
import re
import time

# Query param: token, crtfc_key(DART), api_key(FRED), …
_SENSITIVE_QS = re.compile(
    r"([?&](?:token|crtfc_key|api_key|access_token|client_secret|client_id|"
    r"refresh_token|AUTH_KEY)=)[^&\s\"']+",
    re.IGNORECASE,
)

# ECOS: /api/KeyStatisticList/{key}/json/… — 키가 path segment
_ECOS_PATH_KEY = re.compile(
    r"(https?://ecos\.bok\.or\.kr/api/KeyStatisticList/)[^/\s\"'?&]+",
    re.IGNORECASE,
)

# 헤더·JSON 문자열에 키가 그대로 찍힌 경우
_HEADERish = re.compile(
    r"((?:AUTH_KEY|Authorization|Bearer)[\"'\s:=]+)[^\s\"',}\]]+",
    re.IGNORECASE,
)

_ENV_KEY_NAMES = (
    "TOSS_CLIENT_SECRET", "TOSS_CLIENT_ID", "DART_API_KEY", "FRED_API_KEY",
    "ECOS_API_KEY", "FINNHUB_API_KEY", "KRX_API_KEY", "KRX_PASS", "KRX_USER",
    "ANTHROPIC_API_KEY", "NTFY_TOPIC",
)

_ENV_CACHE: tuple[tuple[str, ...], float] = ((), 0.0)
_ENV_CACHE_TTL = 30.0
_ENV_MIN_LEN = 6


def _env_secrets() -> tuple[str, ...]:
    """.env 에 로드된 API 키 평문 — path/query 어디에든 치환."""
    global _ENV_CACHE
    now = time.monotonic()
    cached, ts = _ENV_CACHE
    if cached and (now - ts) < _ENV_CACHE_TTL:
        return cached
    vals: list[str] = []
    for name in _ENV_KEY_NAMES:
        v = (os.getenv(name) or "").strip()
        if len(v) >= _ENV_MIN_LEN:
            vals.append(v)
    vals.sort(key=len, reverse=True)
    out = tuple(vals)
    _ENV_CACHE = (out, now)
    return out


def invalidate_env_secret_cache() -> None:
    """테스트용 — env 변경 후 캐시 무효화."""
    global _ENV_CACHE
    _ENV_CACHE = ((), 0.0)


def redact_secrets(text: str | None) -> str:
    """URL·예외·임의 문자열에서 secret 값을 *** 로 치환."""
    if not text:
        return ""
    s = str(text)
    s = _SENSITIVE_QS.sub(r"\1***", s)
    s = _ECOS_PATH_KEY.sub(r"\1***", s)
    s = _HEADERish.sub(r"\1***", s)
    for secret in _env_secrets():
        if secret in s:
            s = s.replace(secret, "***")
    return s


def response_error_brief(resp) -> str:
    """requests.Response → 짧은 오류 문자열(마스킹)."""
    try:
        url = redact_secrets(getattr(resp, "url", "") or "")
        return f"HTTP {getattr(resp, 'status_code', '?')} {url}".strip()
    except Exception:
        return redact_secrets(str(resp))
