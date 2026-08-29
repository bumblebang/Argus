"""Argus 경보 훅 — 데몬 죽음·뇌 모드 전이를 감지해 data/ALERT.json + alerts.jsonl 로 남긴다.

작업스케줄러가 5분마다 호출한다.

감지:
  A. 데몬 하트비트 끊김(age>300s 또는 없음)     -> 데몬 죽음/행
  B. brain_mode.json 모드 전이
       bridge        -> 클코 한도, Cursor 브릿지 운용
       circuit_open  -> 뇌 회로차단(브릿지 미준비/실패)
       auth_needed   -> 인증 만료(재로그인 필요)
  B2. bridge 모드 + bridge.heartbeat 미무장     -> circuit 위험(조기 경보)
  B3. 장 상태 교차검증(정규장) — 토스 캐시 vs US Finnhub/Yahoo, KR 정규장 달력
  C. (레거시 폴백) DB 최근 인증 에러 — mode 파일 없을 때

푸시/대시보드 '다음:' 액션은 src.ops_playbook.

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

from src.agents.llm import is_bridge_armed  # noqa: E402
from src.engine import brain_mode as bm  # noqa: E402
from src.market_status_crosscheck import crosscheck_reasons  # noqa: E402
from src.session_policy import any_market_tradable, trading_sessions_from_raw  # noqa: E402
from src.ops_playbook import actions_for, format_push_body  # noqa: E402
from src import paths as _paths  # noqa: E402

DB = _paths.resolve("db", configured="data/bot.db")
HEARTBEAT = _paths.resolve("watch_hb", configured="data/watch.heartbeat")
BRAIN_MODE = _paths.resolve("brain_mode", configured="data/brain_mode.json")
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
                 reason: str = "",
                 quota_kind: str | None = None) -> list[str]:
    """뇌 모드 → 경보 문구. ok 면 빈 리스트."""
    from src.ops_budget import KIND_LABEL, format_countdown, format_reset_clock
    mode = brain_mode or "ok"
    if mode == "ok":
        return []
    kind_l = KIND_LABEL.get(quota_kind or "", "")
    kind_bit = f"{kind_l} · " if kind_l else ""
    cd = format_countdown(reset_at)
    clock = format_reset_clock(reset_at)
    if mode == "bridge":
        return [f"클코 {kind_bit}Cursor 브릿지 운용 중 (리셋까지 {cd}, {clock})"]
    if mode == "circuit_open":
        detail = reason or "브릿지 미준비/실패"
        return [f"뇌 회로차단 — {kind_bit}{detail}, 리셋까지 {cd} ({clock})"]
    if mode == "auth_needed":
        return ["claude 인증 만료 — 뇌 전면 정지. scripts\\claude_login.bat 로 재로그인 필요"]
    return [f"뇌 모드 이상: {mode}"]


def evaluate(now: float, hb_age: float | None, market_open: bool = True,
             brain_errors_recent: int = 0, last_brain_done_age: float | None = None,
             auth_expired: bool = False, *,
             brain_mode: str = "ok", reset_at: float | None = None,
             mode_reason: str = "",
             bridge_armed: bool | None = None,
             quota_kind: str | None = None,
             hb_ok: bool | None = None,
             hb_polled: int | None = None,
             hb_markets_open: list | None = None,
             hb_should_be_open: list | None = None,
             expects_polling: bool = False) -> list[str]:
    """순수 판정 — 경보 사유 리스트(빈 리스트=정상).

    brain_errors_recent / last_brain_done_age 는 하위호환으로 받지만 **무시**한다
    (슬라이딩 창 플리핑 제거). market_open 도 뇌 모드 경보를 막지 않는다.
    bridge_armed=False 이고 mode=bridge 이면 미무장 조기 경보.

    hb_ok/hb_polled/hb_should_be_open: 하트비트 JSON. should_be_open 비어있지 않은데
    polled=0·markets_open 비어있음·ok=False 면 가짜 초록을 경보로 올린다.
    expects_polling 은 구 하트비트(should_be_open 없음) 폴백용.
    """
    del brain_errors_recent, last_brain_done_age, market_open  # 명시적 미사용
    reasons: list[str] = []
    if hb_age is None:
        reasons.append("데몬 하트비트 없음 — 감시 루프 미가동")
    elif hb_age > HB_STALE_SEC:
        reasons.append(f"데몬 하트비트 끊김 {hb_age:.0f}s (>{HB_STALE_SEC}s) — 죽음/행 의심")
    else:
        mkts = list(hb_markets_open or [])
        should = list(hb_should_be_open or [])
        poll_fail = (
            hb_ok is False
            or (should and ((hb_polled or 0) <= 0 or not mkts))
            or (mkts and (hb_polled or 0) <= 0)
        )
        if poll_fail:
            reasons.append(
                f"장중 시세 폴링 실패(should={','.join(should) or '?'}"
                f", polled={hb_polled if hb_polled is not None else '?'}"
                f", markets={','.join(mkts) or '?'}) — 하트비트만 살아 있음")
        elif expects_polling and not should and not mkts:
            reasons.append(
                "거래 세션인데 markets_open 비어 있음 — 감시 루프 미동작 의심")

    mode = brain_mode or "ok"
    if auth_expired and mode == "ok":
        mode = "auth_needed"
    reasons.extend(mode_reasons(mode, reset_at=reset_at, reason=mode_reason,
                                quota_kind=quota_kind))
    if mode == "bridge" and bridge_armed is False:
        reasons.append(
            "브릿지 모드인데 미무장 — bridge.heartbeat 만료/없음 → circuit 위험")
    return reasons


def _bridge_inbox_and_max_age() -> tuple[Path, float]:
    """config agents.cursor_bridge → inbox · armed_max_age_sec."""
    max_age = 90.0
    configured = "data/llm_inbox"
    try:
        from src.config import load_config
        cb = (load_config().raw.get("agents") or {}).get("cursor_bridge") or {}
        max_age = float(cb.get("armed_max_age_sec", 90) or 90)
        configured = str(cb.get("inbox_dir") or configured)
    except Exception:
        pass
    inbox = _paths.resolve("inbox", configured=configured)
    return Path(inbox), max_age


def _read_heartbeat(now: float) -> tuple[float | None, dict]:
    """(age, payload). 파일이 없으면 (None, {})."""
    try:
        hb = _paths.resolve("watch_hb", configured="data/watch.heartbeat")
        d = json.loads(hb.read_text(encoding="utf-8"))
        return now - float(d.get("ts", 0)), d if isinstance(d, dict) else {}
    except (OSError, ValueError, TypeError):
        return None, {}


def _read_heartbeat_age(now: float) -> float | None:
    age, _ = _read_heartbeat(now)
    return age

def _load_brain_mode() -> dict:
    return bm.load_mode(_paths.resolve("brain_mode", configured="data/brain_mode.json"))

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


def _push(title: str, message: str) -> bool:
    """ntfy POST. 2xx 만 성공. 4xx/5xx·예외는 False — 호출측이 dedupe 를 밀면 영구무음."""
    topic = _ntfy_topic()
    if not topic:
        return False
    try:
        safe_title = title.encode("ascii", "replace").decode("ascii")
        r = requests.post(f"https://ntfy.sh/{topic}",
                          data=message.encode("utf-8"),
                          headers={"Title": safe_title}, timeout=5)
        if 200 <= int(r.status_code) < 300:
            return True
        return False
    except Exception:
        return False


def _push_title_for(reasons: list[str], mode: str) -> str:
    if mode == "bridge":
        return "Argus brain bridge"
    if mode == "circuit_open":
        return "Argus brain circuit"
    if mode == "auth_needed" or any("인증" in r for r in reasons):
        return "Argus auth"
    if (any("하트비트" in r for r in reasons)
            or any("폴링 실패" in r for r in reasons)
            or any("장 상태 불일치" in r or "세션 캐시" in r for r in reasons)):
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


_EXIT_REASON_KO = {
    "stop_hit": "손절",
    "target_hit": "목표가 도달",
    "session_end": "종가 청산",
    "time_stop": "시간손절",
    "brain": "뇌 판단",
    "partial_exit": "부분 청산",
    "exit": "청산",
}
_SIDE_KO = {"BUY": "매수", "SELL": "매도"}


def _load_symbol_names() -> dict[str, str]:
    """대시보드와 같은 캐시 소스 — 심볼→종목명. 실패해도 빈 dict."""
    m: dict[str, str] = {}

    def _put(sym, name, *, overwrite: bool = False) -> None:
        s = str(sym or "").strip()
        n = str(name or "").strip()
        if not s or not n:
            return
        if overwrite or s not in m:
            m[s] = n

    for fn in ("base_universe_KR.txt", "base_universe_US.txt"):
        try:
            for line in (ROOT / "data" / fn).read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split(",", 1)
                if len(parts) == 2:
                    _put(parts[0], parts[1])
        except OSError:
            pass
    try:
        info = json.loads((ROOT / "data" / "stock_info_cache.json").read_text(encoding="utf-8")) or {}
        for sym, v in info.items() if isinstance(info, dict) else []:
            row = (v or {}).get("info") if isinstance(v, dict) else None
            if isinstance(row, dict):
                _put(sym, row.get("name"), overwrite=True)
    except (OSError, ValueError, TypeError):
        pass
    try:
        import yaml
        u = yaml.safe_load((ROOT / "data" / "universe.yaml").read_text(encoding="utf-8")) or {}
        for lst in u.values():
            for it in (lst or []):
                if isinstance(it, dict) and it.get("symbol"):
                    _put(it["symbol"], it.get("name"), overwrite=True)
    except Exception:
        pass
    return m


def _display_name(symbol: str, names: dict[str, str]) -> str:
    s = str(symbol or "").strip()
    return names.get(s) or s or "?"


def _order_why(p: dict) -> str:
    """exit_reason·broker reason → 사람이 읽는 근거 한 줄."""
    er = str(p.get("exit_reason") or "").strip()
    raw = str(p.get("reason") or "").strip()
    if er:
        label = _EXIT_REASON_KO.get(er, er)
        if er.startswith("strategy:"):
            label = f"전략신호 ({er.split(':', 1)[1]})"
        if raw and raw not in (er, f"[exit] {er}") and not raw.startswith(f"[exit] {er}"):
            extra = raw[7:].strip() if raw.startswith("[exit] ") else raw
            if extra and extra != er:
                return f"{label} — {extra}"
        return label
    if not raw:
        return ""
    if raw.startswith("[exit] "):
        kind = raw[7:].strip()
        return _EXIT_REASON_KO.get(kind, kind)
    return raw


def _format_live_order_msg(kind: str, symbol: str, p: dict, names: dict[str, str]) -> str:
    name = _display_name(symbol, names)
    why = _order_why(p)
    if kind == "live_order":
        side = str(p.get("side") or "?").upper()
        side_ko = _SIDE_KO.get(side, side)
        body = (f"[LIVE] {side_ko} {name} x{p.get('qty', '?')} "
                f"@ {p.get('price', '?')}")
    else:
        body = f"[LIVE-ERR] {name}: {p.get('error', '?')}"
    if why:
        body += f"\n근거: {why}"
    return body


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
    names = _load_symbol_names()
    # 성공한 이벤트만 커서로 전진 — 실패분을 성공으로 찍어 영구 스킵하지 않는다.
    advanced_to = since
    for ts, kind, symbol, payload in rows:
        try:
            p = json.loads(payload) if payload else {}
        except (ValueError, TypeError):
            p = {}
        msg = _format_live_order_msg(kind, symbol, p, names)
        title = "Argus 체결" if kind == "live_order" else "Argus 주문실패"
        ok = _push(title, msg)
        if not ok:
            break
        advanced_to = max(advanced_to, float(ts))
    if advanced_to > since:
        st = _load_push_state()
        st["last_order_ts"] = advanced_to
        _save_push_state(st)

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
    hb_age, hb = _read_heartbeat(now)
    tsess: dict[str, tuple[str, ...]] = {}
    try:
        from src.config import load_config
        from src.session_policy import any_market_tradable, trading_sessions_from_raw
        tsess = trading_sessions_from_raw(load_config().raw)
    except Exception:
        from src.session_policy import DEFAULT_TRADING_SESSIONS
        tsess = dict(DEFAULT_TRADING_SESSIONS)
    expects_polling = any_market_tradable(["KR", "US"], tsess)
    market_open = expects_polling
    mode_state = _load_brain_mode()
    mode = str(mode_state.get("mode") or "ok")
    reset_at = mode_state.get("reset_at")
    auth_expired = (mode == "auth_needed") or _auth_expired_recent(now)
    inbox, max_age = _bridge_inbox_and_max_age()
    armed = is_bridge_armed(inbox, max_age_sec=max_age, now=now)
    from src.ops_budget import budget_gauge, format_budget_push_line
    cfg_raw = None
    try:
        from src.config import load_config
        cfg_raw = load_config().raw
    except Exception:
        cfg_raw = None
    gauge = budget_gauge(mode_state, now=now, cfg=cfg_raw)
    qk = gauge.get("quota_kind") or mode_state.get("quota_kind")
    hb_ok = hb.get("ok") if hb else None
    if isinstance(hb_ok, bool) or hb_ok is None:
        pass
    else:
        hb_ok = bool(hb_ok)
    reasons = evaluate(
        now, hb_age, market_open,
        auth_expired=auth_expired,
        brain_mode=mode,
        reset_at=reset_at if isinstance(reset_at, (int, float)) else None,
        mode_reason=str(mode_state.get("reason") or ""),
        bridge_armed=armed,
        quota_kind=qk if isinstance(qk, str) else None,
        hb_ok=hb_ok,
        hb_polled=int(hb["polled"]) if hb.get("polled") is not None else None,
        hb_markets_open=list(hb.get("markets_open") or []) if hb else None,
        hb_should_be_open=list(hb.get("should_be_open") or []) if hb else None,
        expects_polling=expects_polling if not hb.get("should_be_open") else False,
    )
    reasons.extend(crosscheck_reasons(now))
    next_actions = actions_for(reasons, brain_mode=mode)
    budget_line = format_budget_push_line(gauge)

    prev = _load_prev()
    was_active = bool(prev.get("active"))
    prev_mode = prev.get("brain_mode") or "ok"
    prev_reasons = list(prev.get("reasons") or [])
    prev_push_ok = prev.get("push_ok", True)

    if reasons:
        since = prev.get("since") if was_active else now
        should_fire = ((not was_active) or (prev_mode != mode)
                       or (prev_reasons != reasons) or (not prev_push_ok))
        push_ok = True
        if should_fire:
            _log("FIRED", reasons, brain_mode=mode, actions=next_actions,
                 budget=gauge)
            if _ntfy_topic():
                push_ok = _push(
                    _push_title_for(reasons, mode),
                    format_push_body(reasons, next_actions, budget_line=budget_line))
            else:
                push_ok = False
        else:
            push_ok = bool(prev_push_ok)
        payload = {
            "active": True, "since": since, "reasons": reasons, "ts": now,
            "brain_mode": mode, "reset_at": reset_at,
            "market_open": market_open,
            "bridge_armed": armed,
            "actions": next_actions,
            "budget": gauge,
            "push_ok": push_ok,
        }
        ALERT.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        print("[alert] ACTIVE:", " | ".join(reasons))
        if gauge.get("line"):
            print("[alert] 예산:", gauge["line"])
        if next_actions:
            print("[alert] 다음:", " / ".join(next_actions))
        if not _ntfy_topic():
            print("[alert] ⚠ 푸시 통로 없음(NTFY_TOPIC 미설정) — 이 경보는 무음입니다. "
                  ".env 에 NTFY_TOPIC=<임의문자열> 를 넣고 폰 ntfy 앱에서 같은 토픽을 구독하세요.")
        elif should_fire and not push_ok:
            print("[alert] ⚠ 푸시 실패 — 다음 주기에 재시도(dedupe 보류)")
    else:
        ALERT.write_text(json.dumps({
            "active": False, "ts": now, "brain_mode": mode,
            "market_open": market_open,
            "bridge_armed": armed,
            "budget": gauge,
            "push_ok": True,
        }, ensure_ascii=False), encoding="utf-8")
        if was_active:
            _log("CLEARED", [], brain_mode=mode)
            # 진짜 복구만 — 휴장으로 꺼진 것처럼 보이게 하지 않음(모드 ok 일 때만 여기 옴)
            if _ntfy_topic():
                _push("Argus brain OK", "뇌 정상 재개")
        print("[alert] ok" + (" (휴장)" if not market_open else ""))
        if gauge.get("line"):
            print("[alert] 예산:", gauge["line"])

    _push_live_orders(now)
    return 0

if __name__ == "__main__":
    from src.cli.legacy import warn_legacy_script
    warn_legacy_script("argus alert-check")
    raise SystemExit(main())
