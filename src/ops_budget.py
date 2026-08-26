"""클코 세션/주간 한도 — 예산 계기판.

1) 소진 후 신호: mode / reset_at / quota_kind
2) 추정 %: 로컬 JSONL 최근 N시간 토큰 ÷ session_token_cap (공식 잔량 아님)
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

# Pro $20 · 5h 창 커뮤니티 추정치(배증 전후 바뀔 수 있음) — config 로 덮어쓰기
DEFAULT_SESSION_TOKEN_CAP = 44_000
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


def _cfg_budget(cfg: dict[str, Any] | None) -> tuple[int, float, bool]:
    """(cap, window_sec, enabled)."""
    cap = DEFAULT_SESSION_TOKEN_CAP
    window = float(DEFAULT_SESSION_WINDOW_SEC)
    enabled = True
    if not cfg:
        return cap, window, enabled
    agents = cfg.get("agents") if isinstance(cfg, dict) else None
    block = {}
    if isinstance(agents, dict):
        block = agents.get("claude_budget") or {}
    if not isinstance(block, dict):
        block = {}
    try:
        if block.get("session_token_cap") is not None:
            cap = max(1, int(block["session_token_cap"]))
    except (TypeError, ValueError):
        pass
    try:
        if block.get("session_window_sec") is not None:
            window = max(60.0, float(block["session_window_sec"]))
    except (TypeError, ValueError):
        pass
    if "enabled" in block:
        enabled = bool(block.get("enabled"))
    return cap, window, enabled


def estimate_session_pct(*, now: float | None = None,
                         cfg: dict[str, Any] | None = None,
                         usage_fn=None) -> dict[str, Any] | None:
    """JSONL 창 합 / cap → 추정 %. enabled=false 또는 조회 실패 시 None."""
    cap, window, enabled = _cfg_budget(cfg)
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
        return {
            "ok": False,
            "error": str(e)[:120],
            "used": 0,
            "cap": cap,
            "pct": None,
            "window_sec": window,
        }
    used = int(raw.get("used") or 0)
    pct = min(999.0, round(100.0 * used / cap, 1)) if cap else None
    return {
        "ok": True,
        "used": used,
        "cap": cap,
        "pct": pct,
        "window_sec": window,
        "n_events": int(raw.get("n_events") or 0),
        "approx": True,
        "label": (
            f"추정 {pct:.0f}% ({_fmt_tok(used)} / {_fmt_tok(cap)}, "
            f"최근 {int(window // 3600)}h)"
            if pct is not None else f"추정 — ({_fmt_tok(used)} / {_fmt_tok(cap)})"
        ),
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
    """대시/ALERT/푸시용 스냅샷. 추정 % 는 session_est 키."""
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

    session_est = estimate_session_pct(now=ts, cfg=cfg, usage_fn=usage_fn)
    est_bit = ""
    if session_est and session_est.get("label"):
        est_bit = f" · {session_est['label']}"

    if mode == "ok" and not kind:
        if session_est and session_est.get("pct") is not None:
            line = f"세션 예산{est_bit}"
        else:
            line = "한도 여유(추정) — 소진 전 잔량 미지"
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
        "session_est": session_est,
    }
    if consecutive_failures is not None:
        out["consecutive_failures"] = int(consecutive_failures)
    return out


def format_budget_push_line(gauge: dict[str, Any] | None) -> str:
    if not gauge:
        return ""
    line = str(gauge.get("line") or "").strip()
    return f"예산: {line}" if line else ""
