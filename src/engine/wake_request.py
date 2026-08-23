"""외부 프로세스 → watch 뇌 각성 요청 (파일 신호).

Athena 배치가 끝난 뒤 `request_brain_wake()` 를 쓰면, 감시 루프가 다음 틱에서
읽어 `on_wake` 로 BrainWorker 를 깨운다. 시장이 아직 안 열린 시각(07:30대)에도
루프 idle 틱에서 소비한다(open_markets 게이트와 무관).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path("data/brain_wake_request.json")


def request_brain_wake(*, reason: str = "athena_done", market: str | None = None,
                       path: str | Path | None = None,
                       extra: dict[str, Any] | None = None,
                       now: float | None = None) -> Path:
    """각성 요청 파일을 atomic 기록. Athena 등 배치 프로세스에서 호출."""
    p = Path(path) if path is not None else DEFAULT_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
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
    """요청이 있으면 읽고 파일을 지운 뒤 payload 반환. 없거나 만료면 None.

    max_age_sec: 낡은 요청(데몬 장기 정지 후 재기동 등)은 무시.
    """
    p = Path(path) if path is not None else DEFAULT_PATH
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        ts = float(data.get("ts") or 0)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    try:
        p.unlink(missing_ok=True)
    except OSError:
        return None
    if ts <= 0 or (time.time() - ts) > float(max_age_sec):
        return None
    if not isinstance(data, dict):
        return None
    data.setdefault("reason", "external")
    return data
