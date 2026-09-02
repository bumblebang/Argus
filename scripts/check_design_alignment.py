"""CI/로컬: 설계 불변조건(manifest) ↔ config.example + 코드 정합."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.eval.design_alignment import (  # noqa: E402
    alignment_ok,
    format_report,
    resolve_config_path,
    run_alignment,
)


def main() -> int:
    ap = argparse.ArgumentParser(description="설계–구현 정합 점검")
    ap.add_argument("--manifest", default="tests/golden/design_invariants.yaml")
    ap.add_argument("--config", default=None,
                    help="미지정 시 config.yaml(있으면) → config.example.yaml")
    ap.add_argument("--root", default=str(ROOT))
    args = ap.parse_args()

    cfg_path = Path(args.config) if args.config else resolve_config_path(Path(args.root))
    results = run_alignment(
        Path(args.manifest),
        cfg_path,
        Path(args.root),
    )
    print(format_report(results))
    if alignment_ok(results):
        print("design alignment: OK")
        return 0
    print("design alignment: BLOCKED", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
