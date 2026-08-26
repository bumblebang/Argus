"""레거시 안내. 상주 루프는 `argus watch` (또는 scripts/watch.py).

구 TradingBot 루프(게이트웨이 없이 토스에 붙던 경로)는 제거했다.
"""
from __future__ import annotations

import sys


def main() -> int:
    print(
        "Argus 상주 진입점: argus watch  (pip install -e .)\n"
        "레거시: python scripts/watch.py\n"
        "  argus bootstrap\n"
        "  argus doctor\n"
        "  argus watch --dry --ticks 1\n"
        "라이브는 docs/SETUP_LIVE.md",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
