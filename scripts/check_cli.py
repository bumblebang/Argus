"""claude CLI(구독) 백엔드 연결 확인 — 봇이 `claude` 로 판단을 받아오는지 1회 테스트.

  python scripts/check_cli.py

성공하면 API 크레딧 없이 구독으로 봇을 돌릴 수 있다는 뜻.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pydantic import BaseModel

from src.config import load_config
from src.logging_setup import setup_logging, get_logger
from src.agents import ClaudeCLIClient


class Ping(BaseModel):
    ok: str
    note: str = ""


def main() -> int:
    setup_logging("INFO")
    log = get_logger("check_cli")
    cfg = load_config()
    a = cfg.raw.get("agents", {})
    cli = ClaudeCLIClient(command=a.get("claude_command", "claude"),
                          model=(a.get("claude_model") or None),
                          timeout=int(a.get("claude_timeout", 120)))
    log.info("claude 명령: %s | 모델: %s", cli.command, cli.model or "(CLI 기본)")
    try:
        r = cli.structured("너는 연결 확인용 도우미다.",
                           '{"task":"ping"}  ok 필드에 "connected" 라고 넣어라.', Ping)
        print("\n" + "=" * 56)
        print(f"  [성공] claude CLI 백엔드 OK -> ok={r.ok!r}")
        print("  => 구독으로 봇 운영 가능 (API 크레딧 불필요)")
        print("=" * 56 + "\n")
        return 0
    except Exception as e:
        print(f"\n[실패] {type(e).__name__}: {e}")
        print("  → `claude` 가 PATH 에 있고 로그인돼 있는지, 헤드리스(`claude -p \"hi\"`) 가 "
              "되는지 확인하세요. config.yaml 의 agents.claude_command 로 전체 경로 지정 가능.\n")
        return 1


if __name__ == "__main__":
    from src.cli.legacy import warn_legacy_script
    warn_legacy_script("argus doctor --check-cli")
    sys.exit(main())
