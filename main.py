"""레거시 안내. 상주 루프는 scripts/watch.py 다.

구 TradingBot 루프(게이트웨이 없이 토스에 붙던 경로)는 제거했다.
"""
from __future__ import annotations

import sys


def main() -> int:
    print(
        "Argus 상주 진입점은 scripts/watch.py 입니다.\n"
        "  python scripts/bootstrap.py\n"
        "  python scripts/doctor.py\n"
        "  python scripts/watch.py --dry --ticks 1\n"
        "라이브는 docs/SETUP_LIVE.md",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
