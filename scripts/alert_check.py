"""Argus 경보 훅 — 데몬 죽음·뇌 모드 전이를 감지해 data/ALERT.json + alerts.jsonl 로 남긴다.

작업스케줄러가 5분마다 호출한다.

감지:
  A. 데몬 하트비트 끊김(age>300s 또는 없음)     -> 데몬 죽음/행
  B. brain_mode.json 모드 전이
       bridge        -> 클코 한도, Cursor 브릿지 운용
       circuit_open  -> 뇌 회로차단(브릿지 미준비/실패)
       auth_needed   -> 인증 만료(재로그인 필요)
  C. (레거시 폴백) DB 최근 인증 에러 — mode 파일 없을 때

제거된 플리핑 경보(2026-08):
  - 최근 20분 brain 에러 N건
  - 장중 40분 무완주
  → 슬라이딩 창이 '정상 복구' 거짓 신호를 만들던 원인.

휴장 시에도 뇌 모드 경보는 유지한다(장 마감 ≠ 복구). CLEARED 푸시는
mode 가 ok 로 돌아왔을 때만 '뇌 정상 재개' 로 보낸다.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from src.engine import brain_mode as bm  # noqa: E402
from src.market_hours import is_open  # noqa: E402

DB = ROOT / "data" / "bot.db"
HEARTBEAT = ROOT / "data" / "watch.heartbeat"
BRAIN_MODE = ROOT / "data" / "brain_mode.json"
ALERT = ROOT / "data" / "ALERT.json"
ALERTS_LOG = ROOT / "data" / "alerts.jsonl"
_PUSH_STATE = ROOT / "data" / "alert_push_state.json"

HB_STALE_SEC = 300
_SEOUL = ZoneInfo("Asia/Seoul")

# 인증 만료 마커(DB 폴백용). 세션/주간 한도와 구분.
_AUTH_MARKERS = ("access token", "oauth", "expired", "authenticate",
                 "refresh_token", "not logged in")


def _fmt_reset(reset_at: float | None) -> str:
    if reset_at is None:
        return "?"
    try:
        return datetime.fromtimestamp(float(reset_at), tz=_SEOUL).strftime("%m/%d %H:%M KST")
    except (ValueError, OSError, TypeError):
        return "?"


def mode_reasons(brain_mode: str, *, reset_at: float | None = None,
                 reason: str = "") -> list[str]:
    """뇌 모드 → 경보 문구. ok 면 빈 리스트."""
    mode = brain_mode or "ok"
    if mode == "ok":
        return []
    if mode == "bridge":
        return [f"클코 한도 — Cursor 브릿지 운용 중 (리셋 {_fmt_reset(reset_at)})"]
    if mode == "circuit_open":
        detail = reason or "브릿지 미준비/실패"
        return [f"뇌 회로차단 — {detail}, 리셋 {_fmt_reset(reset_at)}까지 정지"]
    if mode == "auth_needed":
        return ["claude 인증 만료 — 뇌 전면 정지. scripts\\claude_login.bat 로 재로그인 필요"]
    return [f"뇌 모드 이상: {mode}"]


def evaluate(now: float, hb_age: float | None, market_open: bool = True,
             brain_errors_recent: int = 0, last_brain_done_age: float | None = None,
             auth_expired: bool = False, *,
             brain_mode: str = "ok", reset_at: float | None = None,
             mode_reason: str = "") -> list[str]:
    """순수 판정 — 경보 사유 리스트(빈 리스트=정상).

    brain_errors_recent / last_brain_done_age 는 하위호환으로 받지만 **무시**한다
    (슬라이딩 창 플리핑 제거). market_open 도 뇌 모드 경보를 막지 않는다.
    """
    del brain_errors_recent, last_brain_done_age, market_open  # 명시적 미사용
    reasons: list[str] = []
    if hb_age is None:
        reasons.append("데몬 하트비트 없음 — 감시 루프 미가동")
    elif hb_age > HB_STALE_SEC:
        reasons.append(f"데몬 하트비트 끊김 {hb_age:.0f}s (>{HB_STALE_SEC}s) — 죽음/행 의심")

    mode = brain_mode or "ok"
    if auth_expired and mode == "ok":
        mode = "auth_needed"
    reasons.extend(mode_reasons(mode, reset_at=reset_at, reason=mode_reason))
    return reasons


def _read_heartbeat_age(now: float) -> float | None:
    try:
        d = json.loads(HEARTBEAT.read_text(encoding="utf-8"))
        return now - float(d.get("ts", 0))
    except (OSError, ValueError, TypeError):
        return None


def _load_brain_mode() -> dict:
    return bm.load_mode(BRAIN_MODE)


def _auth_expired_recent(now: float, window: float = 3600.0) -> bool:
    """mode 파일 없을 때 DB 인증 에러 폴백. 세션 한도는 False."""
    if not DB.exists():
        return False
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=2.0)
    try:
        rows = con.execute(
            "select payload from events where kind='error' and ts>? "
            "and payload like '%\"where\": \"brain\"%' order by ts desc limit 5",
            (now - window,)).fetchall()
    except sqlite3.Error:
        return False
    finally:
        con.close()
    for (payload,) in rows:
        low = (payload or "").lower()
        if any(m in low for m in _AUTH_MARKERS):
            # 한도 메시지는 제외
            if "limit" in low and "expired" not in low and "oauth" not in low:
                continue
            if "session limit" in low or "weekly limit" in low:
                continue
            return True
    return False


@lru_cache(maxsize=1)
def _ntfy_topic() -> str:
    try:
        from src.config import load_config
        cfg_topic = str((load_config().raw.get("alerts") or {}).get("ntfy_topic") or "").strip()
    except Exception:
        cfg_topic = ""
    env_topic = (os.getenv("NTFY_TOPIC") or "").strip()
    return env_topic or cfg_topic


def _push(title: str, message: str) -> None:
    topic = _ntfy_topic()
    if not topic:
        return
    try:
        safe_title = title.encode("ascii", "replace").decode("ascii")
        requests.post(f"https://ntfy.sh/{topic}",
                      data=message.encode("utf-8"),
                      headers={"Title": safe_title}, timeout=5)
    except Exception:
        pass


def _push_title_for(reasons: list[str], mode: str) -> str:
    if mode == "bridge":
        return "Argus brain bridge"
    if mode == "circuit_open":
        return "Argus brain circuit"
    if mode == "auth_needed" or any("인증" in r for r in reasons):
        return "Argus auth"
    if any("하트비트" in r for r in reasons):
        return "Argus daemon"
    return "Argus alert"


def _load_push_state() -> dict:
    try:
        return json.loads(_PUSH_STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_push_state(state: dict) -> None:
    try:
        _PUSH_STATE.parent.mkdir(parents=True, exist_ok=True)
        _PUSH_STATE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _push_live_orders(now: float) -> None:
    if not _ntfy_topic() or not DB.exists():
        return
    since = float(_load_push_state().get("last_order_ts", now - 3600))
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=2.0)
    try:
        rows = con.execute(
            "select ts, kind, symbol, payload from events "
            "where kind in ('live_order','live_order_error') and ts>? order by ts",
            (since,)).fetchall()
    finally:
        con.close()
    if not rows:
        return
    max_ts = since
    for ts, kind, symbol, payload in rows:
        max_ts = max(max_ts, ts)
        try:
            p = json.loads(payload) if payload else {}
        except (ValueError, TypeError):
            p = {}
        if kind == "live_order":
            _push("Argus 체결",
                  f"[LIVE] {p.get('side','?')} {symbol} x{p.get('qty','?')} "
                  f"@ {p.get('price','?')} id={p.get('order_id','?')}")
        else:
            _push("Argus 주문실패", f"[LIVE-ERR] {symbol}: {p.get('error','?')}")
    _save_push_state({"last_order_ts": max_ts})


def _load_prev() -> dict:
    try:
        return json.loads(ALERT.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _log(event: str, reasons: list[str], **extra) -> None:
    rec = {"ts": time.time(), "event": event, "reasons": reasons, **extra}
    with ALERTS_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def main() -> int:
    now = time.time()
    hb_age = _read_heartbeat_age(now)
    market_open = is_open("KR") or is_open("US")
    mode_state = _load_brain_mode()
    mode = str(mode_state.get("mode") or "ok")
    reset_at = mode_state.get("reset_at")
    auth_expired = (mode == "auth_needed") or _auth_expired_recent(now)
    reasons = evaluate(
        now, hb_age, market_open,
        auth_expired=auth_expired,
        brain_mode=mode,
        reset_at=reset_at if isinstance(reset_at, (int, float)) else None,
        mode_reason=str(mode_state.get("reason") or ""),
    )

    prev = _load_prev()
    was_active = bool(prev.get("active"))
    prev_mode = prev.get("brain_mode") or "ok"
    prev_reasons = list(prev.get("reasons") or [])

    if reasons:
        since = prev.get("since") if was_active else now
        payload = {
            "active": True, "since": since, "reasons": reasons, "ts": now,
            "brain_mode": mode, "reset_at": reset_at,
            "market_open": market_open,
        }
        ALERT.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        should_fire = (not was_active) or (prev_mode != mode) or (prev_reasons != reasons)
        if should_fire:
            _log("FIRED", reasons, brain_mode=mode)
            _push(_push_title_for(reasons, mode), " | ".join(reasons))
        print("[alert] ACTIVE:", " | ".join(reasons))
        if not _ntfy_topic():
            print("[alert] ⚠ 푸시 통로 없음(NTFY_TOPIC 미설정) — 이 경보는 무음입니다. "
                  ".env 에 NTFY_TOPIC=<임의문자열> 를 넣고 폰 ntfy 앱에서 같은 토픽을 구독하세요.")
    else:
        ALERT.write_text(json.dumps({
            "active": False, "ts": now, "brain_mode": mode,
            "market_open": market_open,
        }, ensure_ascii=False), encoding="utf-8")
        if was_active:
            _log("CLEARED", [], brain_mode=mode)
            # 진짜 복구만 — 휴장으로 꺼진 것처럼 보이게 하지 않음(모드 ok 일 때만 여기 옴)
            _push("Argus brain OK", "뇌 정상 재개")
        print("[alert] ok" + (" (휴장)" if not market_open else ""))

    _push_live_orders(now)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
