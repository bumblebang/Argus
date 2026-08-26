"""레거시 `python scripts/….py` 진입 경고."""
from __future__ import annotations

import sys
import warnings


def warn_legacy_script(canonical: str) -> None:
    """scripts 직접 실행 시 안내. `argus <cmd>` 가 정본(Phase 0+)."""
    msg = (
        f"[deprecated] `python scripts/...` 대신 `{canonical}` 을 쓰세요 "
        f"(repo에서: pip install -e .)"
    )
    print(msg, file=sys.stderr)
    warnings.warn(msg, DeprecationWarning, stacklevel=3)
