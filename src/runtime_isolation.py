"""실주문 안전: dry뇌×live 가드 + 테스트/배치 상태 격리.

격리 트리거(정확히 4개):
  CLI --dry / 선택된 dry 백엔드(자동 폴백 포함) / AgentCycle / run_bot.bat 경로.
broker.mode=paper · dry_run · DRY_RUN · --no-brain 은 트리거가 아니다.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from . import paths as _paths
from .logging_setup import get_logger

log = get_logger("runtime_isolation")

OPS_HEARTBEAT_FRESH_SEC = 300.0


class ConfigError(RuntimeError):
    """dry 뇌 + live broker + dry_run=false 등 설정 오류."""


def resolve_brain_dry(*, args_dry: bool = False, args_cli: bool = False,
                      args_live: bool = False, brain_backend: str | None = None,
                      api_key: str | None = None) -> bool:
    """watch/orchestrator 와 동일한 백엔드 선택 → dry 여부."""
    from .agents.wiring import select_backend
    bk = ("dry" if args_dry else "live" if args_live else "cli" if args_cli
          else (brain_backend or "cli"))
    dry, _, _ = select_backend(dry=(bk == "dry"), cli=(bk == "cli"),
                               live=(bk == "live"), api_key=api_key)
    return bool(dry)


def assert_dry_brain_not_live(*, brain_dry: bool, broker_mode: str,
                              dry_run: bool) -> None:
    """dry 뇌 + broker.mode=live + dry_run=false → ConfigError."""
    if not brain_dry:
        return
    if str(broker_mode or "").lower() != "live":
        return
    if dry_run:
        return
    raise ConfigError(
        "설정 오류: dry 뇌(MockLLM/자동 폴백) + broker.mode=live + dry_run=false. "
        "실주문 경로와 테스트 뇌가 동시에 켜질 수 없습니다. "
        "brain_backend 를 cli/live 로 바꾸거나 dry_run=true / broker.mode=paper 로 맞추세요.")


def isolation_needed(*, cli_dry: bool = False, brain_dry: bool = False,
                     agent_cycle: bool = False, run_bot: bool = False) -> bool:
    return bool(cli_dry or brain_dry or agent_cycle or run_bot)


def begin_isolation(*, force_paper_cfg: Any = None) -> Path:
    """tempfile 루트로 data root 전환. broker.mode 강제 paper(가능하면)."""
    root = Path(tempfile.mkdtemp(prefix="argus-isolate-"))
    for sub in ("data/state", "data/ledgers", "data/inbox"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    _paths.set_data_root(root)
    if force_paper_cfg is not None:
        raw = getattr(force_paper_cfg, "raw", None)
        if isinstance(raw, dict):
            broker = dict(raw.get("broker") or {})
            broker["mode"] = "paper"
            raw["broker"] = broker
    log.warning("실행 상태 격리 — data root=%s (account/Store/decisions/heartbeat/PID/락)",
                root)
    return root


def ops_token_contention(fresh_sec: float = OPS_HEARTBEAT_FRESH_SEC) -> str | None:
    """운영(비격리) PID 생존 + heartbeat 신선하면 토큰 경합 사유 문자열."""
    # 격리 중이면 ops 경로는 repo ROOT 기준.
    ops_root = _paths.ROOT
    pid_path = _paths.resolve("watch_pid", root=ops_root, configured="data/watch.pid")
    hb_path = _paths.resolve("watch_hb", root=ops_root, configured="data/watch.heartbeat")
    pid = _read_pid(pid_path)
    if pid is None or not _pid_alive(pid):
        return None
    age = _heartbeat_age(hb_path)
    if age is None or age > fresh_sec:
        return None
    return (f"운영 watch(pid={pid}) 생존·heartbeat {age:.0f}s 이내 — "
            f"격리 실행이 토큰을 경합할 수 있어 종료합니다")


def _read_pid(path: Path) -> int | None:
    try:
        text = path.read_text(encoding="utf-8").strip()
        return int(text.split()[0]) if text else None
    except (OSError, ValueError, IndexError):
        return None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            import ctypes
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            kernel32.CloseHandle(handle)
            return True
        os.kill(pid, 0)
        return True
    except (OSError, AttributeError, ValueError):
        return False


def _heartbeat_age(path: Path) -> float | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        ts = float(raw.get("ts") or 0)
        if ts <= 0:
            return None
        return time.time() - ts
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def is_run_bot_env() -> bool:
    return os.getenv("ARGUS_RUN_BOT", "").strip() in ("1", "true", "yes")
