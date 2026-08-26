"""BrainWorker — 느린 뇌(LLM 사이클)를 감시 루프에서 떼어내는 비동기 단일실행 워커.

감시 루프(빠른손, 초단위)는 절대 막히면 안 된다. 그런데 LLM 사이클은 2~4분 걸린다.
그래서 act 트리거가 뜨면 wake() 로 '깨우기 신호'만 던지고, 별도 스레드의 워커가
실제 사이클(cycle_fn)을 돌린다. 루프는 즉시 폴링을 계속한다.

규칙:
  - 단일실행(single-flight): 한 번에 한 사이클만. 도중에 들어온 wake 는 합쳐서(coalesce)
    끝난 뒤 최대 한 번만 더 돈다(중복 폭주 방지).
  - 쿨다운(cooldown_sec): 직전 사이클 종료 후 이 시간 안에 온 wake 는 무시(과호출 방지).
  - 단일 프로세스·단일 Gateway 원칙 유지(토스 토큰 1개) — 워커도 같은 Gateway 를 쓰는
    cycle_fn 을 받으므로 RateLimiter 가 감시 폴링과 캔들 호출을 함께 직렬화한다.
  - 예외는 삼켜 로깅(워커 스레드가 죽지 않게).
  - 뇌 모드(ok/bridge/circuit_open/auth_needed): 한도·브릿지 실패 시 회로 OPEN 으로
    wake 를 스킵해 240s 타임아웃 스팸을 막는다(brain_mode.json 영속).
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable

from ..agents.llm import BrainQuotaError, is_bridge_armed
from ..logging_setup import get_logger
from . import brain_mode as bm

log = get_logger("engine.brain")


class BrainWorker:
    def __init__(self, cycle_fn: Callable[[], object], store=None, *,
                 cooldown_sec: float = 0.0,
                 now_fn: Callable[[], float] = time.time,
                 kind_prefix: str = "brain",
                 mode_path: str | Path | None = None,
                 bridge_inbox_dir: str | Path | None = None,
                 bridge_armed_max_age_sec: float = 90.0,
                 circuit_fail_threshold: int = 2,
                 source_fn: Callable[[], str | None] | None = None,
                 quota_info_fn: Callable[[], dict] | None = None) -> None:
        self.cycle_fn = cycle_fn
        self.store = store
        self.cooldown_sec = float(cooldown_sec)
        self._now = now_fn
        # 이벤트 kind 접두사(기본 "brain" → 거동 불변). 밸류 워커는 "value" 로 감사추적 분리.
        self.kind_prefix = kind_prefix
        self.mode_path = Path(mode_path) if mode_path else None
        self.bridge_inbox_dir = Path(bridge_inbox_dir) if bridge_inbox_dir else None
        self.bridge_armed_max_age_sec = float(bridge_armed_max_age_sec)
        self.circuit_fail_threshold = max(1, int(circuit_fail_threshold))
        # 직전 사이클 LLM 출처("cli"|"bridge"|None). watch 가 ClaudeCLIClient.last_source 연결.
        self.source_fn = source_fn
        # 한도 폴백 메타(kind/reset_at/error) — ClaudeCLIClient.last_quota_* .
        self.quota_info_fn = quota_info_fn
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_done = 0.0
        self._pending: dict | None = None
        self._lock = threading.Lock()
        self.runs = 0          # 실제 사이클 실행 횟수(테스트/모니터링)
        self.skipped = 0       # 쿨다운으로 건너뛴 wake 수
        # 뇌 가용성 관측 — 세션 한도 등으로 사이클이 계속 실패하면 루프(하트비트)는 멀쩡한데
        # 판단만 멈춘다(무음 실패). 워치독이 못 잡는 이 상태를 대시보드가 보게 카운터를 둔다.
        self.consecutive_failures = 0
        self.last_ok_ts = 0.0
        self.last_error: str | None = None
        # bridge 모드에서의 연속 실패(타임아웃 등) — 임계치면 circuit_open.
        self._bridge_fail_streak = 0

    # 감시 루프가 호출(on_wake 콜백). 신호만 세팅하고 즉시 반환(논블로킹).
    def wake(self, reason: str = "", triggers: list | None = None) -> None:
        """대기 중 wake 가 있으면 덮어쓰지 않고 reason/triggers 를 합친다(재료 유실 방지)."""
        from ..agents.serve_policy import merge_wake_pending
        serialized = _serialize_triggers(triggers)
        with self._lock:
            self._pending = merge_wake_pending(
                self._pending, reason or "", serialized)
        self._wake.set()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_loop, name="brain", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()                       # 대기 중인 워커 깨워 종료시킴
        if self._thread:
            self._thread.join(timeout=timeout)

    def current_mode(self) -> dict:
        return bm.load_mode(self.mode_path)

    def _bridge_armed_now(self) -> bool:
        if self.bridge_inbox_dir is None:
            return False
        return is_bridge_armed(self.bridge_inbox_dir,
                               max_age_sec=self.bridge_armed_max_age_sec,
                               now=self._now())

    def _apply_mode(self, mode: str, *, reason: str = "",
                    reset_at: float | None = None,
                    last_error: str | None = None,
                    bridge_armed: bool | None = None,
                    quota_kind: str | None = None) -> dict:
        state = bm.set_mode(
            self.mode_path, mode, reason=reason, reset_at=reset_at,
            last_error=last_error, bridge_armed=bridge_armed,
            quota_kind=quota_kind, now=self._now())
        if state.pop("_changed", False):
            self._log(f"{self.kind_prefix}_mode", {
                "mode": state["mode"], "reason": state.get("reason"),
                "reset_at": state.get("reset_at"),
                "bridge_armed": state.get("bridge_armed"),
                "quota_kind": state.get("quota_kind"),
            })
            log.warning("뇌 모드 → %s (%s)", state["mode"], reason or "")
        return state

    # ── 워커 스레드 ───────────────────────────────────────────
    def _run_loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait()
            if self._stop.is_set():
                break
            # clear 를 실행 '전에' 한다 → 사이클 도중 들어온 wake 는 다시 set 되어
            # 끝난 뒤 한 번 더 돈다(coalesce, 최신 1건만).
            self._wake.clear()
            self.run_pending()

    def run_pending(self) -> bool:
        """대기 중인 wake 1건을 (쿨다운·회로 통과 시) 실행. 실행했으면 True.

        테스트에서 스레드 없이 직접 호출할 수 있게 분리.
        """
        now = self._now()
        if self.cooldown_sec and (now - self._last_done) < self.cooldown_sec:
            self.skipped += 1
            self._log(f"{self.kind_prefix}_skip", {"reason": "cooldown",
                                     "since_last": round(now - self._last_done, 1)})
            return False

        armed = self._bridge_armed_now()
        state = bm.load_mode(self.mode_path)
        skip, skip_why = bm.should_skip_wake(state, now=now, bridge_armed=armed)
        if skip:
            self.skipped += 1
            self._log(f"{self.kind_prefix}_skip", {
                "reason": skip_why,
                "mode": state.get("mode"),
                "reset_at": state.get("reset_at"),
                "bridge_armed": armed,
            })
            # 헬스만 갱신(모드 유지)
            self._log_health(state, bridge_armed=armed)
            return False

        with self._lock:
            ctx = self._pending or {}
            self._pending = None
        self._log(f"{self.kind_prefix}_start", ctx)
        try:
            result = self._invoke_cycle(ctx)
            self.runs += 1
            self.consecutive_failures = 0
            self._bridge_fail_streak = 0
            self.last_ok_ts = self._now()
            self.last_error = None
            src = None
            if self.source_fn is not None:
                try:
                    src = self.source_fn()
                except Exception:
                    src = None
            if src == "bridge":
                qinfo: dict = {}
                if self.quota_info_fn is not None:
                    try:
                        qinfo = dict(self.quota_info_fn() or {})
                    except Exception:
                        qinfo = {}
                state = self._apply_mode(
                    "bridge", reason="cursor_bridge", bridge_armed=True,
                    reset_at=qinfo.get("reset_at"),
                    last_error=qinfo.get("error"),
                    quota_kind=qinfo.get("kind") or "unknown")
            else:
                state = self._apply_mode("ok", reason="cli_ok", bridge_armed=armed)
            self._log(f"{self.kind_prefix}_done", {"summary": _summarize(result),
                                                   "source": src or "cli"})
            self._log_health(state, bridge_armed=armed)
            return True
        except Exception as e:                  # 워커가 죽지 않게 삼킨다.
            self.consecutive_failures += 1
            self.last_error = str(e)[:300]
            log.exception("뇌 사이클 실패(연속 %d회): %s", self.consecutive_failures, e)
            self._log("error", {"where": self.kind_prefix, "err": str(e)})
            state = self._handle_failure(e, armed=armed)
            self._log_health(state, bridge_armed=armed)
            return False
        finally:
            self._last_done = self._now()

    def _handle_failure(self, e: BaseException, *, armed: bool) -> dict:
        err_s = str(e)[:300]
        if bm.is_auth_error(e):
            self._bridge_fail_streak = 0
            return self._apply_mode(
                "auth_needed", reason="auth_expired", last_error=err_s,
                bridge_armed=armed)

        if isinstance(e, BrainQuotaError):
            from ..ops_budget import classify_quota_kind
            reset_at = e.reset_at
            qk = classify_quota_kind(err_s) or "unknown"
            if not e.bridge_armed:
                return self._apply_mode(
                    "circuit_open", reason="quota_no_bridge",
                    reset_at=reset_at, last_error=err_s, bridge_armed=False,
                    quota_kind=qk)
            # armed 인데 QuotaError 는 이례 — circuit 으로
            return self._apply_mode(
                "circuit_open", reason="quota", reset_at=reset_at,
                last_error=err_s, bridge_armed=True, quota_kind=qk)

        if bm.is_bridge_timeout(e) or "cursor_bridge" in err_s.lower():
            self._bridge_fail_streak += 1
            cur = bm.load_mode(self.mode_path)
            # bridge 운용 중이거나 이번이 브릿지 경로 실패면 streak 누적
            if (cur.get("mode") == "bridge"
                    or self._bridge_fail_streak >= self.circuit_fail_threshold):
                if self._bridge_fail_streak >= self.circuit_fail_threshold:
                    return self._apply_mode(
                        "circuit_open", reason="bridge_fail",
                        reset_at=cur.get("reset_at"), last_error=err_s,
                        bridge_armed=armed)
            # 아직 임계 미만 — bridge 모드 유지(또는 진입)
            return self._apply_mode(
                "bridge", reason="bridge_error", last_error=err_s,
                bridge_armed=armed)

        # 기타 오류 — 모드 유지, 연속 실패만 기록. 이미 circuit 이면 유지.
        cur = bm.load_mode(self.mode_path)
        if cur.get("mode") in ("circuit_open", "auth_needed", "bridge"):
            cur["last_error"] = err_s
            bm.save_mode(self.mode_path, cur)
            return cur
        return cur

    def _log_health(self, state: dict, *, bridge_armed: bool) -> None:
        self._log(f"{self.kind_prefix}_health", {
            "consecutive_failures": self.consecutive_failures,
            "runs": self.runs,
            "last_ok_ts": self.last_ok_ts,
            "last_error": self.last_error,
            "mode": state.get("mode"),
            "reset_at": state.get("reset_at"),
            "bridge_armed": bridge_armed,
        })

    def _invoke_cycle(self, wake_ctx: dict) -> object:
        """cycle_fn(wake=...) 지원 시 각성 사유를 넘기고, 아니면 무인자 호출(밸류 워커 등)."""
        try:
            return self.cycle_fn(wake=wake_ctx)
        except TypeError:
            return self.cycle_fn()

    def _log(self, kind: str, payload: dict) -> None:
        if self.store:
            try:
                self.store.log_event(kind, None, payload)
            except Exception as e:
                log.warning("brain 이벤트 로깅 실패: %s", e)


def _serialize_triggers(triggers: list | None, *, limit: int = 20) -> list[dict]:
    """Trigger dataclass / 공시·실적 dict 페이로드를 LLM·이벤트용으로 압축."""
    out: list[dict] = []
    for t in (triggers or [])[:limit]:
        if t is None:
            continue
        if hasattr(t, "kind") and hasattr(t, "symbol"):
            payload = getattr(t, "payload", None) or {}
            if not isinstance(payload, dict):
                payload = {}
            out.append({
                "kind": t.kind,
                "symbol": t.symbol,
                "urgency": getattr(t, "urgency", None),
                "reason": str(getattr(t, "reason", "") or "")[:200],
                "payload": {k: payload[k] for k in list(payload)[:12]},
            })
        elif isinstance(t, dict):
            # disclosure/earnings_result wake 페이로드
            row = {k: t[k] for k in list(t)[:16]}
            if "title" in row and isinstance(row["title"], str):
                row["title"] = row["title"][:120]
            out.append(row)
        else:
            out.append({"repr": str(t)[:120]})
    return out


def _summarize(result: object) -> dict:
    """CycleResult(또는 임의 결과)를 이벤트용으로 압축."""
    executed = getattr(result, "executed", None)
    if executed is None:
        return {"result": str(result)[:200]}
    return {"executed": len(executed),
            "filled": sum(1 for e in executed if e.get("status") == "filled"),
            "vetoed": sum(1 for e in executed if e.get("status") == "vetoed")}
