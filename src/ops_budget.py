"""클코 세션/주간 한도 — 예산 계기판.

1) 소진 후 신호: mode / reset_at / quota_kind
2) 사용량: 로컬 JSONL 최근 N시간 토큰 합(한도 대비 % 아님 — cap 을 알 수 없다)
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

_SEOUL = ZoneInfo("Asia/Seoul")

# 표시용 라벨
KIND_LABEL = {
    "session": "세션 한도",
    "weekly": "주간 한도",
    "unknown": "한도(종류 미상)",
}

DEFAULT_SESSION_WINDOW_SEC = 5 * 3600


def classify_quota_kind(text: str | None) -> str | None:
    """에러/reason 문구 → session|weekly|unknown. 한도 아니면 None."""
    if not text:
        return None
    low = str(text).lower()
    if "weekly limit" in low or "weekly usage" in low:
        return "weekly"
    if "session limit" in low:
        return "session"
    if any(m in low for m in (
            "usage limit", "limit resets", "quota", "클코 한도",
            "cursor_bridge", "quota_no_bridge")):
        if "weekly" in low:
            return "weekly"
        if "session" in low:
            return "session"
        return "unknown"
    return None


def format_countdown(reset_at: float | None, *, now: float | None = None) -> str:
    """리셋까지 남은 시간. 없으면 '—' / 지났으면 '리셋 시각 지남'."""
    if reset_at is None:
        return "—"
    try:
        ra = float(reset_at)
    except (TypeError, ValueError):
        return "—"
    ts = time.time() if now is None else float(now)
    delta = ra - ts
    if delta <= 0:
        return "리셋 시각 지남"
    mins = int(delta // 60)
    if mins < 60:
        return f"{mins}분"
    hours, rem_m = divmod(mins, 60)
    if hours < 48:
        return f"{hours}시간 {rem_m}분" if rem_m else f"{hours}시간"
    days, rem_h = divmod(hours, 24)
    return f"{days}일 {rem_h}시간"


def format_reset_clock(reset_at: float | None) -> str:
    if reset_at is None:
        return "—"
    try:
        return datetime.fromtimestamp(float(reset_at), tz=_SEOUL).strftime(
            "%m/%d %H:%M KST")
    except (ValueError, OSError, TypeError):
        return "—"


def _cfg_budget(cfg: dict[str, Any] | None) -> tuple[float, bool]:
    """(window_sec, enabled)."""
    window = float(DEFAULT_SESSION_WINDOW_SEC)
    enabled = True
    agents = cfg.get("agents") if isinstance(cfg, dict) else None
    block = agents.get("claude_budget") if isinstance(agents, dict) else None
    if not isinstance(block, dict):
        return window, enabled
    try:
        if block.get("session_window_sec") is not None:
            window = max(60.0, float(block["session_window_sec"]))
    except (TypeError, ValueError):
        pass
    if "enabled" in block:
        enabled = bool(block.get("enabled"))
    return window, enabled


def session_usage(*, now: float | None = None,
                  cfg: dict[str, Any] | None = None,
                  usage_fn=None) -> dict[str, Any] | None:
    """최근 창의 로컬 JSONL 사용량. enabled=false 면 None.

    한도 대비 % 는 내지 않는다 — 구독 cap 은 공개되지 않고, 합의 대부분이 캐시 읽기라
    고정 cap(예전 44K)으로 나누면 한도와 무관하게 늘 '>100%' 였다. 출력 토큰과 호출 수를
    그대로 보여 주고, 실제 소진 여부는 mode/reset_at(소진 후 신호)이 말한다.
    """
    window, enabled = _cfg_budget(cfg)
    if not enabled:
        return None
    ts = time.time() if now is None else float(now)
    try:
        if usage_fn is not None:
            raw = usage_fn(window_sec=window, now=ts)
        else:
            from .claude_local_usage import tokens_in_window
            raw = tokens_in_window(window_sec=window, now=ts)
    except Exception as e:
        return {"ok": False, "error": str(e)[:120], "window_sec": window}
    used = int(raw.get("used") or 0)
    output = int(raw.get("output") or 0)
    n_events = int(raw.get("n_events") or 0)
    label = (f"최근 {int(window // 3600)}h 출력 {_fmt_tok(output)} · "
             f"캐시 포함 {_fmt_tok(used)} · {n_events}회")
    return {
        "ok": True,
        "used": used,
        "output": output,
        "cache_read": int(raw.get("cache_read") or 0),
        "window_sec": window,
        "n_events": n_events,
        "label": label,
    }


def _fmt_tok(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def budget_gauge(state: dict[str, Any] | None, *, now: float | None = None,
                 consecutive_failures: int | None = None,
                 cfg: dict[str, Any] | None = None,
                 usage_fn=None) -> dict[str, Any]:
    """대시/ALERT/푸시용 스냅샷. 최근 창 사용량은 session_usage 키."""
    ts = time.time() if now is None else float(now)
    st = state or {}
    mode = str(st.get("mode") or "ok")
    reset_at = st.get("reset_at")
    try:
        reset_at_f = float(reset_at) if reset_at is not None else None
    except (TypeError, ValueError):
        reset_at_f = None

    kind = st.get("quota_kind")
    if kind not in ("session", "weekly", "unknown"):
        kind = classify_quota_kind(st.get("last_error")) or classify_quota_kind(
            st.get("reason"))
    if mode in ("bridge", "circuit_open") and not kind:
        kind = "unknown"
    if mode == "ok":
        if reset_at_f is None or reset_at_f <= ts:
            kind = None

    label = KIND_LABEL.get(kind or "", "")
    countdown = format_countdown(reset_at_f, now=ts) if reset_at_f else "—"
    clock = format_reset_clock(reset_at_f)

    usage = session_usage(now=ts, cfg=cfg, usage_fn=usage_fn)
    est_bit = ""
    if usage and usage.get("label"):
        est_bit = f" · {usage['label']}"

    if mode == "ok" and not kind:
        line = f"한도 정상{est_bit}" if est_bit else "한도 정상 — 사용량 미집계"
        status = "ok"
    elif mode == "auth_needed":
        line = f"인증 만료 — 한도 예산과 별개{est_bit}"
        status = "auth"
    elif mode == "bridge":
        line = (f"{label or '한도'} · 브릿지 운용 · 리셋까지 {countdown} "
                f"({clock}){est_bit}")
        status = "bridge"
    elif mode == "circuit_open":
        line = (f"{label or '한도'} · 회로차단 · 리셋까지 {countdown} "
                f"({clock}){est_bit}")
        status = "circuit"
    else:
        line = f"mode={mode}{est_bit}"
        status = mode

    out: dict[str, Any] = {
        "status": status,
        "mode": mode,
        "quota_kind": kind,
        "quota_label": label or None,
        "reset_at": reset_at_f,
        "reset_clock": clock,
        "countdown": countdown,
        "line": line,
        "session_usage": usage,
    }
    if consecutive_failures is not None:
        out["consecutive_failures"] = int(consecutive_failures)
    return out


def format_budget_push_line(gauge: dict[str, Any] | None) -> str:
    if not gauge:
        return ""
    line = str(gauge.get("line") or "").strip()
    return f"예산: {line}" if line else ""
