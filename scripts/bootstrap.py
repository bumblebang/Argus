"""첫 실행 준비: 예시 설정 복사, data/logs 디렉터리.

이미 있는 config.yaml / .env 는 덮지 않는다 (운영 머신 보호).
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "logs").mkdir(exist_ok=True)
    copied = []
    pairs = [
        (ROOT / "config.example.yaml", ROOT / "config.yaml"),
        (ROOT / ".env.example", ROOT / ".env"),
    ]
    for src, dst in pairs:
        if not src.exists():
            print(f"없음: {src.name}", file=sys.stderr)
            return 1
        if dst.exists():
            print(f"유지: {dst.name} (이미 있음, 덮지 않음)")
            continue
        shutil.copyfile(src, dst)
        copied.append(dst.name)
        print(f"생성: {dst.name}")
    print("다음: .env 키를 채운 뒤  python scripts/doctor.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
