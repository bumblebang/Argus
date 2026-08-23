"""Anthropic 인증 연결 확인 — 봇이 실제로 Claude를 호출할 수 있는지 1회 테스트.

  python scripts/check_auth.py

ANTHROPIC_API_KEY 가 있으면 API 키 인증을, 없으면 구독(ant auth login) 인증을 테스트한다.
아주 작은 호출(max_tokens=16)이라 비용/사용량은 미미하다.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config  # noqa: F401  (.env 로드)
from src.logging_setup import setup_logging, get_logger


def main() -> int:
    setup_logging("INFO")
    log = get_logger("check_auth")
    cfg = load_config()
    model = cfg.raw.get("agents", {}).get("model", "claude-opus-4-8")

    import anthropic
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if api_key:
        mode = "API 키(종량제)"
        client = anthropic.Anthropic(api_key=api_key)
    else:
        mode = "구독(ant auth login)"
        # 빈 키가 프로필보다 우선하지 않도록 제거
        for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            if not os.environ.get(k):
                os.environ.pop(k, None)
        client = anthropic.Anthropic(default_headers={"anthropic-beta": "oauth-2025-04-20"})

    log.info("인증 방식: %s | 모델: %s", mode, model)
    try:
        resp = client.messages.create(
            model=model, max_tokens=16,
            messages=[{"role": "user", "content": "연결 확인용. 'OK'라고만 답해."}])
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        print("\n" + "=" * 56)
        print(f"  [성공] 인증 연결 OK  ({mode})")
        print(f"  모델 응답: {resp.model} -> {text.strip()[:40]}")
        print("=" * 56 + "\n")
        return 0
    except anthropic.AuthenticationError as e:
        print(f"\n[실패] 인증 거부({mode}): {e}")
        print("  → 구독이면 `ant auth login` 재시도, 또는 .env 에 ANTHROPIC_API_KEY 입력.\n")
        return 1
    except Exception as e:
        print(f"\n[실패] 호출 오류({mode}): {type(e).__name__}: {e}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
