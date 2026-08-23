"""봇이 실제로 쓰는 claude.exe 경로 출력 + 인증 상태 점검.

  python scripts/which_claude.py           # 경로 1줄만 출력(런처가 파싱)
  python scripts/which_claude.py --check   # 그 exe 로 실제 호출해 인증 확인

왜 필요한가: Windows 에서 claude 설치본이 둘(가상화 Roaming / MSIX Packages)이라
대화형 세션의 claude 와 데몬이 쓰는 경로가 갈릴 수 있다. 로그인해도 봇은 만료 토큰을
보고 뇌가 멈춘다. 런처·점검은 봇과 같은 resolve_claude_command 를 거친다.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config
from src.agents.llm import resolve_claude_command


def bot_claude_path() -> str:
    """config.agents.claude_command 를 봇과 동일하게 해석한 실제 실행 경로."""
    cfg = load_config()
    configured = (cfg.raw.get("agents", {}) or {}).get("claude_command", "claude")
    return resolve_claude_command(configured)


def check(exe: str, timeout: int = 90) -> int:
    """그 exe 로 최소 호출 1회. 인증이 살아 있으면 0, 아니면 1."""
    try:
        p = subprocess.run([exe, "-p", "--model", "sonnet"], input="say OK",
                           capture_output=True, text=True, timeout=timeout)
    except Exception as e:                       # 실행 자체 실패(경로·권한 등)
        print(f"[실패] claude 실행 불가: {type(e).__name__}: {e}")
        return 1
    out = ((p.stdout or "") + (p.stderr or "")).strip()
    # CLI 는 인증 실패를 rc=0 + 본문 메시지로 흘리기도 한다 → 문자열로도 판정.
    bad = ("authenticate" in out.lower() or "expired" in out.lower()
           or "api error" in out.lower())
    if p.returncode != 0 or bad:
        print(f"[실패] 인증 안 됨: {out[:200]}")
        print("  -> 이 창에서 위 경로의 claude 를 실행하고 /login 하세요"
              " (Claude 구독으로 로그인, API 키 아님).")
        return 1
    print(f"[정상] 봇이 쓰는 claude 인증 OK. 응답: {out[:60]}")
    return 0


def main() -> int:
    exe = bot_claude_path()
    if "--check" in sys.argv:
        print(f"봇이 쓰는 claude: {exe}")
        return check(exe)
    print(exe)                                    # 런처가 이 한 줄을 파싱한다
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
