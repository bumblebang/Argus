"""뇌 가용성 모드 영속 상태(brain_mode.json).

모드:
  ok            — 클코 CLI 정상
  bridge        — 한도 소진, Cursor 브릿지로 운용 중
  circuit_open  — 브릿지 미준비/실패 → wake 차단(리셋·재무장까지)
  auth_needed   — 인증 만료(사람 재로그인 필요)

alert_check / 대시보드 / BrainWorker 가 같은 파일을 읽는다.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

MODES = ("ok", "bridge", "circuit_open", "auth_needed")
DEFAULT_PATH_NAME = "brain_mode.json"

_AUTH_MARKERS = ("access token", "oauth", "expired", "authenticate",
                 "refresh_token", "not logged in")


def default_state(*, now: float | None = None) -> dict[str, Any]:
    ts = time.time() if now is None else float(now)
    return {
        "mode": "ok",
        "reason": "",
        "since": ts,
        "reset_at": None,
        "last_error": None,
        "bridge_armed": False,
        "quota_kind": None,  # session|weekly|unknown — 예산 계기판
        "ts": ts,
    }


def load_mode(path: str | Path | None) -> dict[str, Any]:
    if path is None:
        return default_state()
    p = Path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default_state()
    if not isinstance(data, dict):
        return default_state()
    out = default_state()
    out.update({k: data[k] for k in out if k in data})
    mode = str(out.get("mode") or "ok")
    out["mode"] = mode if mode in MODES else "ok"
    return out


def save_mode(path: str | Path | None, state: dict[str, Any]) -> None:
    if path is None:
        return
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(state)
    payload["ts"] = time.time()
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def is_auth_error(err: BaseException | str) -> bool:
    low = str(err).lower()
    return any(m in low for m in _AUTH_MARKERS)


def is_bridge_timeout(err: BaseException | str) -> bool:
    return "cursor_bridge" in str(err).lower() and "타임아웃" in str(err)


def set_mode(path: str | Path | None, mode: str, *, reason: str = "",
             reset_at: float | None = None, last_error: str | None = None,
             bridge_armed: bool | None = None,
             quota_kind: str | None = None,
             clear_quota: bool = False,
             now: float | None = None,
             prev: dict[str, Any] | None = None) -> dict[str, Any]:
    """모드 기록. 모드가 바뀌면 since 를 갱신. 반환=새 상태(+ changed bool 키는 별도).

    quota_kind: session|weekly|unknown. clear_quota=True 또는 mode=ok 복귀 시 한도 필드 비움.
    """
    if mode not in MODES:
        raise ValueError(f"unknown brain mode: {mode}")
    ts = time.time() if now is None else float(now)
    cur = prev if prev is not None else load_mode(path)
    changed = cur.get("mode") != mode
    nxt = dict(cur)
    nxt["mode"] = mode
    nxt["reason"] = reason or nxt.get("reason") or ""
    if changed:
        nxt["since"] = ts
    if reset_at is not None:
        nxt["reset_at"] = reset_at
    if last_error is not None:
        nxt["last_error"] = last_error[:300]
    if bridge_armed is not None:
        nxt["bridge_armed"] = bool(bridge_armed)
    if clear_quota or mode == "ok":
        nxt["quota_kind"] = None
        if mode == "ok":
            nxt["reset_at"] = None
    elif quota_kind in ("session", "weekly", "unknown"):
        nxt["quota_kind"] = quota_kind
    nxt["ts"] = ts
    save_mode(path, nxt)
    nxt["_changed"] = changed
    return nxt


def should_skip_wake(state: dict[str, Any], *, now: float | None = None,
                     bridge_armed: bool = False) -> tuple[bool, str]:
    """회로/auth 에서 wake 실행을 막을지. (skip, reason)

    circuit_open: reset_at 전이면 skip. reset 지났거나 bridge 재무장이면 probe 허용.
    auth_needed: 스킵하지 않음(로그인 후 다음 사이클이 복구 가능해야 함).
    """
    mode = state.get("mode") or "ok"
    if mode == "ok" or mode == "bridge":
        return False, ""
    if mode == "auth_needed":
        return False, ""   # 계속 시도 — 성공 시 ok 로 복귀
    if mode != "circuit_open":
        return False, ""
    ts = time.time() if now is None else float(now)
    reset_at = state.get("reset_at")
    if bridge_armed:
        return False, ""   # 재무장 → probe
    if reset_at is not None and ts >= float(reset_at):
        return False, ""   # 리셋 시각 지남 → probe
    return True, "circuit_open"
