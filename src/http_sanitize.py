"""HTTP 오류·URL 로깅용 시크릿 마스킹 — query token/api_key 가 로그에 평문으로 남지 않게."""
from __future__ import annotations

import re

_SENSITIVE_QS = re.compile(
    r"([?&](?:token|crtfc_key|api_key|access_token|client_secret|AUTH_KEY)=)[^&\s\"']+",
    re.IGNORECASE,
)


def redact_secrets(text: str | None) -> str:
    """raise_for_status 등 예외/URL 문자열에서 query secret 값을 *** 로 치환."""
    if not text:
        return ""
    return _SENSITIVE_QS.sub(r"\1***", str(text))


def response_error_brief(resp) -> str:
    """requests.Response → 짧은 오류 문자열(마스킹)."""
    try:
        url = redact_secrets(getattr(resp, "url", "") or "")
        return f"HTTP {getattr(resp, 'status_code', '?')} {url}".strip()
    except Exception:
        return redact_secrets(str(resp))
