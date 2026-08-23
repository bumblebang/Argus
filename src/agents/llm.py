"""LLM 클라이언트 래퍼 (Anthropic) + 테스트용 MockLLM.

structured(system, user, schema) -> schema 인스턴스. 에이전트는 이 인터페이스에만
의존하므로 MockLLM 으로 API 없이 전체 파이프라인을 테스트할 수 있다.

인증 두 가지 모두 지원:
  - API 키 (ANTHROPIC_API_KEY, 종량제)
  - 구독 로그인 (`ant auth login` 프로필 → bare 클라이언트, oauth 헤더). subscription=True.

구조화 출력은 output_config 대신 '프롬프트로 JSON 요청 + 파싱' 방식이라, 구독 OAuth 등
어떤 인증/모델에서도 동작한다.

클코 구독 한도 소진 시(옵선) FileInboxLLM — Cursor Auto 가 data/llm_inbox 로
Decision/Validation JSON 을 채워 주는 3단 폴백. 브릿지는 bridge.heartbeat 가
신선할 때만(armed) 진입하고, 미준비면 BrainQuotaError 로 즉시 회로차단한다.
"""
from __future__ import annotations

import glob
import json
import os
import re
import subprocess
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Sequence, Type, TypeVar
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from ..logging_setup import get_logger

log = get_logger("agents.llm")

T = TypeVar("T", bound=BaseModel)

# 클코 사용량/세션 한도 — Cursor 브리지로 넘길 때만 매칭(타임아웃·경로오류는 제외).
_USAGE_LIMIT_MARKERS = (
    "session limit",
    "weekly limit",
    "limit resets",
    "usage limit",
    "usage limit reached",
)

# 한도 메시지 리셋 시각 파싱 실패 시 보수적 대기(초).
_DEFAULT_QUOTA_COOLDOWN_SEC = 6 * 3600
_BRIDGE_HEARTBEAT_NAME = "bridge.heartbeat"
_SEOUL = ZoneInfo("Asia/Seoul")
_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"), 1)}


def _extract_json(text: str) -> dict | None:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.lstrip().startswith("json"):
            text = text.lstrip()[4:]
    s, e = text.find("{"), text.rfind("}")
    if s >= 0 and e > s:
        try:
            return json.loads(text[s:e + 1])
        except json.JSONDecodeError:
            return None
    return None


class LLMClient:
    def __init__(self, model: str = "claude-opus-4-8", api_key: str | None = None,
                 max_tokens: int = 8000, subscription: bool = False, thinking: bool = True):
        import anthropic  # 지연 import (없어도 MockLLM 으로 동작)
        kwargs: dict = {}
        if api_key:
            kwargs["api_key"] = api_key
        if subscription:
            # 빈 ANTHROPIC_API_KEY/AUTH_TOKEN 이 환경에 있으면 OAuth 프로필보다 우선해
            # 인증이 깨진다 → 비어있으면 제거해서 ant 로그인 프로필을 쓰게 한다.
            for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
                if not os.environ.get(k):
                    os.environ.pop(k, None)
            # 구독(OAuth) 인증은 /v1/messages 에 이 베타 헤더가 필요
            kwargs["default_headers"] = {"anthropic-beta": "oauth-2025-04-20"}
        self.client = anthropic.Anthropic(**kwargs)
        self.model = model
        self.max_tokens = max_tokens
        self.thinking = thinking
        self.last_model: str | None = model
        self.used_fallback: bool = False
        self.last_source: str | None = "api"

    def structured(self, system: str, user: str, schema: Type[T]) -> T:
        self.last_model = self.model
        self.used_fallback = False
        self.last_source = "api"
        sys_prompt = (system + "\n\n출력 형식: 아래 JSON 스키마에 정확히 맞는 JSON 객체 "
                      "하나만 출력하라. 코드블록·설명·머리말 없이 순수 JSON만.\n스키마:\n"
                      + json.dumps(schema.model_json_schema(), ensure_ascii=False))
        kwargs: dict = {"model": self.model, "max_tokens": self.max_tokens,
                        "system": sys_prompt, "messages": [{"role": "user", "content": user}]}
        if self.thinking:
            kwargs["thinking"] = {"type": "adaptive"}

        last_err = ""
        for attempt in range(2):
            resp = self.client.messages.create(**kwargs)
            text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
            data = _extract_json(text)
            if data is not None:
                try:
                    return schema.model_validate(data)
                except Exception as e:  # 스키마 불일치 -> 재시도
                    last_err = str(e)
            else:
                last_err = "JSON 추출 실패"
            kwargs["messages"] = [{"role": "user", "content":
                                   user + "\n\n(직전 출력이 유효한 JSON이 아니었다. 순수 JSON만 다시 출력)"}]
        raise ValueError(f"LLM 구조화 출력 실패: {last_err}")


def _build_prompt(system: str, user: str, schema: Type[BaseModel], retry: bool = False) -> str:
    p = (system + "\n\n--- 입력 데이터(JSON) ---\n" + user
         + "\n\n--- 출력 지시 ---\n아래 JSON 스키마에 정확히 맞는 JSON 객체 하나만 출력하라. "
         "코드블록·설명·머리말 없이 순수 JSON만.\n스키마:\n"
         + json.dumps(schema.model_json_schema(), ensure_ascii=False))
    if retry:
        p += "\n\n(직전 출력이 유효한 JSON이 아니었다. 순수 JSON만 다시 출력하라.)"
    return p


def _ver_key(name: str) -> list[int]:
    """'2.1.187' -> [2,1,187] (버전 내림차순 정렬용). 숫자 아닌 토큰은 0."""
    return [int(p) if p.isdigit() else 0 for p in name.split(".")]


def _latest_claude_in(claude_code_dir: Path) -> Path | None:
    """claude-code/<ver>/claude.exe 중 가장 높은 버전의 실재 exe 경로를 반환."""
    if not claude_code_dir.is_dir():
        return None
    found = []
    for vdir in claude_code_dir.iterdir():
        exe = vdir / "claude.exe"
        if exe.is_file():
            found.append((_ver_key(vdir.name), exe))
    if not found:
        return None
    found.sort()
    return found[-1][1]


def resolve_claude_command(command: str) -> str:
    """설정된 claude 경로를 데몬 컨텍스트에서도 보이는 실제 경로 + 최신 버전으로 해석.

    이유: config 의 `AppData/Roaming/Claude/...` 는 MSIX **가상화** 경로라 대화형
    세션에선 보이지만 작업스케줄러가 띄운 pythonw 데몬에선 FileNotFoundError 가 난다.
    실제 패키지 경로(`AppData/Local/Packages/Claude_*/LocalCache/...`)는 비가상화라
    모든 프로세스에서 보인다. 동시에 버전 폴더를 스캔해 핀(2.1.187) 의존도 제거한다.
    """
    # bare 명령(예: 'claude')은 PATH 에 맡긴다.
    if not command or ("/" not in command and os.path.sep not in command):
        return command
    # Claude Code 설치 경로처럼 생긴 것만 건드린다(테스트의 가짜 명령·임의 인터프리터는 그대로).
    low = command.replace("\\", "/").lower()
    if "claude-code/" not in low and Path(command).name.lower() not in ("claude.exe", "claude"):
        return command
    bases: list[Path] = []
    # 1) 실제 MSIX 패키지 경로(비가상화, 데몬에서도 보임) — 패밀리명 변해도 글롭으로 잡음.
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        for d in glob.glob(os.path.join(
                local, "Packages", "Claude_*", "LocalCache",
                "Roaming", "Claude", "claude-code")):
            bases.append(Path(d))
    # 2) 설정 경로에서 유추한 claude-code 디렉터리(…/claude-code/<ver>/claude.exe)
    p = Path(command)
    if p.name.lower() == "claude.exe" and p.parent.parent.name == "claude-code":
        bases.append(p.parent.parent)
    # 3) 가상화 Roaming 경로(최후순위)
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        bases.append(Path(appdata) / "Claude" / "claude-code")
    for base in bases:
        exe = _latest_claude_in(base)
        if exe is not None:
            return str(exe)
    return command  # 못 찾으면 설정값 그대로(에러 메시지로 드러나게)


class ClaudeCLIError(RuntimeError):
    """claude CLI 가 실행은 됐으나 실패(rc!=0 또는 timeout). 모델 폴백 대상."""

    def __init__(self, msg: str, rc=None):
        super().__init__(msg)
        self.rc = rc


class BrainQuotaError(RuntimeError):
    """클코 한도 소진 + (브릿지 미준비/거부). BrainWorker 가 회로 OPEN 으로 올린다.

    reset_at: 예상 한도 리셋 epoch(초). None 이면 호출측이 now+6h 등으로 채운다.
    bridge_armed: 게이트 판정 당시 heartbeat 신선 여부(진단용).
    """

    def __init__(self, msg: str, *, reset_at: float | None = None,
                 bridge_armed: bool = False):
        super().__init__(msg)
        self.reset_at = reset_at
        self.bridge_armed = bridge_armed


def is_usage_limit(err: BaseException) -> bool:
    """클코 구독 세션/주간 한도 메시지인지. 타임아웃(rc='timeout')·비 CLI 오류는 False."""
    if not isinstance(err, ClaudeCLIError):
        return False
    if err.rc == "timeout":
        return False
    text = str(err).lower()
    return any(m in text for m in _USAGE_LIMIT_MARKERS)


def parse_reset_at(text: str, *, now: float | None = None) -> float | None:
    """한도 메시지에서 리셋 epoch 을 뽑는다. 못 찾으면 None(호출측이 +6h 폴백).

    예: 'resets Aug 10, 6pm (Asia/Seoul)' · 'resets 5:20am' · 'resets 6pm'
    """
    if not text:
        return None
    now = time.time() if now is None else float(now)
    base = datetime.fromtimestamp(now, tz=_SEOUL)
    low = text.lower().replace("\u00b7", " ").replace("·", " ")

    # resets Aug 10, 6pm / resets Aug 10 6:00pm
    m = re.search(
        r"resets\s+([a-z]{3})\s+(\d{1,2}),?\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)",
        low)
    if m:
        mon = _MONTHS.get(m.group(1)[:3])
        if mon:
            day = int(m.group(2))
            hour = int(m.group(3)) % 12
            if m.group(5) == "pm":
                hour += 12
            minute = int(m.group(4) or 0)
            year = base.year
            cand = datetime(year, mon, day, hour, minute, tzinfo=_SEOUL)
            if cand.timestamp() <= now - 60:
                cand = datetime(year + 1, mon, day, hour, minute, tzinfo=_SEOUL)
            return cand.timestamp()

    # resets 5:20am / resets 6pm (다음 도래 시각, 서울)
    m = re.search(r"resets\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)", low)
    if m:
        hour = int(m.group(1)) % 12
        if m.group(3) == "pm":
            hour += 12
        minute = int(m.group(2) or 0)
        cand = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if cand.timestamp() <= now:
            cand = cand + timedelta(days=1)
        return cand.timestamp()

    return None


def bridge_heartbeat_path(inbox_dir: str | Path) -> Path:
    return Path(inbox_dir) / _BRIDGE_HEARTBEAT_NAME


def write_bridge_heartbeat(inbox_dir: str | Path, *, now: float | None = None) -> Path:
    """Cursor /loop 가 매 틱 호출 — 브릿지 armed 신호."""
    ts = time.time() if now is None else float(now)
    path = bridge_heartbeat_path(inbox_dir)
    _atomic_write_json(path, {"ts": ts, "source": "cursor_loop"})
    return path


def is_bridge_armed(inbox_dir: str | Path, *, max_age_sec: float = 90.0,
                    now: float | None = None) -> bool:
    """bridge.heartbeat 의 ts 가 max_age_sec 이내면 armed."""
    path = bridge_heartbeat_path(inbox_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        hb = float(data.get("ts", 0))
    except (OSError, ValueError, TypeError):
        return False
    now = time.time() if now is None else float(now)
    return hb > 0 and (now - hb) <= float(max_age_sec)


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _notify_cursor_bridge_once(schema_name: str) -> None:
    """첫 Cursor 폴백 진입 시 ntfy 1회(프로세스당). NTFY_TOPIC 없으면 no-op."""
    topic = (os.getenv("NTFY_TOPIC") or "").strip()
    if not topic:
        return
    try:
        import requests
        requests.post(
            f"https://ntfy.sh/{topic}",
            data=(f"Claude CLI usage limit — Cursor bridge handling {schema_name}"
                  ).encode("utf-8"),
            headers={"Title": "Argus cursor_bridge"},
            timeout=5,
        )
    except Exception:
        pass


class FileInboxLLM:
    """파일 inbox LLM — Cursor Auto(/loop)가 request.json 을 읽고 response.json 을 씀.

    데몬(BrainWorker 스레드)만 블로킹한다. 감시 루프·하트비트는 그대로 돈다.
    응답 형식: {"id": "<request id>", "result": { ... schema ... }}
    """

    def __init__(self, inbox_dir: str | Path = "data/llm_inbox",
                 timeout_sec: float = 240, poll_sec: float = 1.0,
                 *, sleep_fn: Callable[[float], None] = time.sleep,
                 now_fn: Callable[[], float] = time.time,
                 notify_fn: Callable[[str], None] | None = None):
        self.inbox_dir = Path(inbox_dir)
        self.timeout_sec = float(timeout_sec)
        self.poll_sec = float(poll_sec)
        self._sleep = sleep_fn
        self._now = now_fn
        self._notify = notify_fn if notify_fn is not None else _notify_cursor_bridge_once
        self._notified = False
        self.last_request_id: str | None = None

    @property
    def request_path(self) -> Path:
        return self.inbox_dir / "request.json"

    @property
    def response_path(self) -> Path:
        return self.inbox_dir / "response.json"

    def structured(self, system: str, user: str, schema: Type[T]) -> T:
        req_id = uuid.uuid4().hex
        self.last_request_id = req_id
        schema_name = schema.__name__
        payload = {
            "id": req_id,
            "schema": schema_name,
            "system": system,
            "user": user,
            "ts": self._now(),
            "source": "cursor_bridge",
            "hint": ("Respond with JSON file response.json: "
                     '{"id":"<same id>","result":{...}}. '
                     "BUY thesis must start with [CURSOR_FALLBACK]."),
        }
        # 이전 응답이 새 요청과 섞이지 않게 지운다.
        try:
            self.response_path.unlink(missing_ok=True)
        except OSError:
            pass
        _atomic_write_json(self.request_path, payload)
        log.warning("cursor_bridge: inbox 요청 id=%s schema=%s timeout=%ss",
                    req_id, schema_name, int(self.timeout_sec))
        if not self._notified:
            self._notified = True
            try:
                self._notify(schema_name)
            except Exception:
                pass

        deadline = self._now() + self.timeout_sec
        last = "응답 없음"
        while self._now() < deadline:
            try:
                raw = self.response_path.read_text(encoding="utf-8")
            except OSError:
                self._sleep(self.poll_sec)
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as e:
                last = f"response.json 파싱 실패: {e}"
                self._sleep(self.poll_sec)
                continue
            if data.get("id") != req_id:
                last = f"id 불일치(want={req_id}, got={data.get('id')})"
                self._sleep(self.poll_sec)
                continue
            result = data.get("result")
            if result is None and isinstance(data.get("data"), dict):
                result = data["data"]
            if not isinstance(result, dict):
                # result 래퍼 없이 스키마 본문만 온 경우(id 제외)
                result = {k: v for k, v in data.items() if k != "id"}
            try:
                out = schema.model_validate(result)
            except Exception as e:
                last = f"스키마 검증 실패: {e}"
                self._sleep(self.poll_sec)
                continue
            log.info("cursor_bridge: 응답 수신 id=%s schema=%s", req_id, schema_name)
            try:
                self.request_path.unlink(missing_ok=True)
            except OSError:
                pass
            return out
        raise TimeoutError(
            f"cursor_bridge 타임아웃({self.timeout_sec}s) schema={schema_name}: {last}")


class ClaudeCLIClient:
    """`claude` CLI(Claude Code) 를 호출하는 백엔드 — API 크레딧 대신 구독을 사용.

    봇의 뇌가 `claude -p <프롬프트>` 를 subprocess 로 실행해 판단을 받는다. 인터페이스는
    LLMClient 와 동일(.structured). 단, 구독 사용한도를 나눠 쓰고 CLI 스폰이라 느리다.

    폴백 체인: 주모델 → fallback_model(sonnet 등) → (한도성 실패만, bridge armed 시) cursor_bridge.
    한도인데 브릿지 미준비면 BrainQuotaError(즉시) — 240s 타임아웃 스팸 방지.
    """

    def __init__(self, command: str = "claude", base_args: Sequence[str] = ("-p",),
                 model: str | None = None, extra_args: Sequence[str] = (),
                 timeout: int = 120, cwd: str | None = None,
                 fallback_model: str | None = None,
                 error_dump_path: str | Path | None = "data/claude_cli_error.json",
                 cursor_bridge: Any | None = None,
                 *,
                 require_bridge_armed: bool = True,
                 bridge_armed_max_age_sec: float = 90.0):
        self._command_cfg = command
        self.command = resolve_claude_command(command)
        if self.command != command:
            log.info("claude 경로 해석: %s -> %s", command, self.command)
        self.base_args = list(base_args)
        self.model = model
        self.extra_args = list(extra_args)
        self.timeout = timeout
        self.cwd = cwd
        self.fallback_model = fallback_model
        self.error_dump_path = Path(error_dump_path) if error_dump_path else None
        self.cursor_bridge = cursor_bridge
        self.require_bridge_armed = bool(require_bridge_armed)
        self.bridge_armed_max_age_sec = float(bridge_armed_max_age_sec)
        # 직전 structured() 가 쓴 백엔드 — BrainWorker 가 mode=bridge|ok 판정에 사용.
        self.last_source: str | None = None
        self.last_model: str | None = model
        self.used_fallback: bool = False

    def _invoke(self, prompt: str, model: str | None) -> str:
        def _args(cmd: str) -> list[str]:
            a = [cmd, *self.base_args]
            if model:
                a += ["--model", model]
            a += self.extra_args
            return a

        # Windows: pythonw(무콘솔) 데몬이 콘솔 앱(claude.exe)을 subprocess 로 부르면 매 호출마다
        # 콘솔 창이 깜빡인다 → CREATE_NO_WINDOW 로 숨긴다(무인 데몬 운영 시 창 튐 방지).
        _kw = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        command = self.command
        for attempt in range(2):
            try:
                proc = subprocess.run(_args(command), input=prompt, capture_output=True, text=True,
                                      timeout=self.timeout, cwd=self.cwd,
                                      encoding="utf-8", errors="replace", **_kw)
                break
            except subprocess.TimeoutExpired:
                raise ClaudeCLIError(f"claude CLI 응답 시간 초과({self.timeout}s)", rc="timeout")
            except FileNotFoundError:
                if attempt == 0:
                    fresh = resolve_claude_command(self._command_cfg)
                    if fresh == command:
                        raise RuntimeError(
                            f"`{command}` 실행 파일을 찾을 수 없습니다. "
                            "claude CLI 가 PATH 에 있는지 확인하세요.")
                    log.info("claude 경로 재탐색: %s -> %s", command, fresh)
                    command = fresh
                    self.command = fresh
                    continue
                raise RuntimeError(
                    f"`{command}` 실행 파일을 찾을 수 없습니다. "
                    "claude CLI 가 PATH 에 있는지 확인하세요.")
        if proc.returncode != 0:
            # 원인이 stderr 대신 stdout 으로 오는 경우가 많다(사용량 한도 등) → 둘 다 본다.
            detail = (proc.stderr or "").strip() or (proc.stdout or "").strip()
            if self.error_dump_path is not None:
                try:  # 데몬 진단용 전체 덤프(폴백 전 '주모델' 실패 원인이 남는다)
                    self.error_dump_path.write_text(json.dumps(
                        {"ts": time.time(), "rc": proc.returncode, "model": model,
                         "stdout": (proc.stdout or "")[:3000], "stderr": (proc.stderr or "")[:3000]},
                        ensure_ascii=False, indent=2), encoding="utf-8")
                except OSError:
                    pass
            raise ClaudeCLIError(
                f"claude CLI 오류(rc={proc.returncode}): {detail[:400] or '(빈 출력)'}",
                rc=proc.returncode)
        return proc.stdout

    def _run(self, prompt: str) -> str:
        try:
            out = self._invoke(prompt, self.model)
            self.last_model = self.model
            self.used_fallback = False
            return out
        except ClaudeCLIError as e:
            if self.fallback_model and self.fallback_model != self.model:
                log.warning("claude(%s) 실패 → 폴백 모델 %s 재시도: %s",
                            self.model or "default", self.fallback_model, e)
                out = self._invoke(prompt, self.fallback_model)
                self.last_model = self.fallback_model
                self.used_fallback = True
                return out
            raise

    def _bridge_inbox_dir(self) -> Path | None:
        b = self.cursor_bridge
        if b is None:
            return None
        d = getattr(b, "inbox_dir", None)
        return Path(d) if d is not None else None

    def structured(self, system: str, user: str, schema: Type[T]) -> T:
        last = ""
        self.last_source = None
        try:
            for attempt in range(2):
                out = self._run(_build_prompt(system, user, schema, retry=attempt > 0))
                data = _extract_json(out)
                if data is not None:
                    try:
                        result = schema.model_validate(data)
                        self.last_source = "cli"
                        return result
                    except Exception as e:
                        last = str(e)
                else:
                    last = "JSON 추출 실패"
            raise ValueError(f"claude CLI 구조화 출력 실패: {last}")
        except ClaudeCLIError as e:
            if self.cursor_bridge is not None and is_usage_limit(e):
                reset_at = parse_reset_at(str(e))
                if reset_at is None:
                    reset_at = time.time() + _DEFAULT_QUOTA_COOLDOWN_SEC
                inbox = self._bridge_inbox_dir()
                armed = True
                if self.require_bridge_armed:
                    if inbox is None:
                        armed = False
                    else:
                        armed = is_bridge_armed(
                            inbox, max_age_sec=self.bridge_armed_max_age_sec)
                if not armed:
                    log.warning("claude 한도 소진 + bridge 미무장 → BrainQuotaError: %s", e)
                    raise BrainQuotaError(
                        f"클코 한도 + Cursor 브릿지 미준비: {e}",
                        reset_at=reset_at, bridge_armed=False) from e
                log.warning("claude 한도 소진 → cursor_bridge 폴백: %s", e)
                out = self.cursor_bridge.structured(system, user, schema)
                self.last_source = "bridge"
                # 브릿지 모델은 외부 — 폴백 집계에 넣되 last_model 은 bridge 로 표기
                self.last_model = getattr(self.cursor_bridge, "model", None) or "cursor_bridge"
                self.used_fallback = True
                return out
            raise


class MockLLM:
    """테스트/드라이런용. responder(schema, system, user) -> schema 인스턴스."""

    def __init__(self, responder: Callable[[Type[BaseModel], str, str], BaseModel],
                 model: str = "mock"):
        self._responder = responder
        self.model = model
        self.last_model = model
        self.used_fallback = False
        self.last_source = "mock"

    def structured(self, system: str, user: str, schema: Type[T]) -> T:
        self.last_model = self.model
        self.used_fallback = False
        out = self._responder(schema, system, user)
        if not isinstance(out, schema):
            raise TypeError(f"MockLLM responder 가 {schema.__name__} 를 반환해야 합니다")
        return out
