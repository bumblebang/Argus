"""외부 프로세스 → watch 뇌 각성 요청 (파일 신호).

`request_brain_wake()` 로 요청 파일을 남기면, 감시 루프가 다음 틱에서 읽어
`on_wake` 로 BrainWorker 를 깨운다(시장 개장 여부와 무관하게 idle 틱에서도 소비).

Athena 배치(scripts/athena.py)는 장전 주문실패를 피하려고 이 경로를 쓰지 않는다.
신선한 bullish 도시에는 KR extra(08:00/09:00) scan must 로 들어간다.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from .. import paths as _paths

DEFAULT_PATH = "data/brain_wake_request.json"


def _wake_path(path: str | Path | None = None) -> Path:
    return _paths.ensure_parent("wake_request", configured=path or DEFAULT_PATH)


def request_brain_wake(*, reason: str = "athena_done", market: str | None = None,
                       path: str | Path | None = None,
                       extra: dict[str, Any] | None = None,
                       now: float | None = None) -> Path:
    """각성 요청 파일을 atomic 기록. Athena 등 배치 프로세스에서 호출."""
    p = _wake_path(path)
    payload: dict[str, Any] = {
        "ts": float(now if now is not None else time.time()),
        "reason": reason or "external",
    }
    if market:
        payload["market"] = market
    if extra:
        payload["extra"] = extra
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)
    return p


def consume_brain_wake(path: str | Path | None = None, *,
                       max_age_sec: float = 6 * 3600) -> dict[str, Any] | None:
    """요청 파일을 읽고 삭제. 없거나 오래됐으면 None."""
    p = _paths.resolve("wake_request", configured=path or DEFAULT_PATH)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    try:
        p.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        ts = float(data.get("ts") or 0)
    except (TypeError, ValueError):
        return None
    if ts <= 0 or (time.time() - ts) > float(max_age_sec):
        return None
    return data if isinstance(data, dict) else None
