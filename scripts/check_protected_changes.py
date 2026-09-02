"""CI: PROTECTED 파일·config 변경 시 eval 실험 또는 defect-fix 필수 (J13)."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.eval.protected_guard import check_protected_changes  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="PROTECTED 변경 가드")
    ap.add_argument("--base", default="origin/main", help="git diff base ref")
    ap.add_argument("--head", default="HEAD")
    ap.add_argument("--registry", default="data/eval_registry.json")
    ap.add_argument("--experiment-id", default=os.environ.get("ARGUS_EVAL_EXPERIMENT_ID"))
    ap.add_argument("--evidence-n", type=int, default=None,
                    help="can_promote 표본 n(미지정 시 registry experiment evidence_n)")
    args = ap.parse_args()

    pr_body = os.environ.get("GITHUB_PR_BODY") or os.environ.get("ARGUS_EVAL_PR_BODY")
    ok, msgs = check_protected_changes(
        base_ref=args.base,
        head_ref=args.head,
        registry_path=ROOT / args.registry,
        experiment_id=args.experiment_id,
        pr_body=pr_body,
        evidence_n=args.evidence_n,
    )
    for m in msgs:
        print(m)
    if ok:
        print("PROTECTED guard: OK")
        return 0
    print("PROTECTED guard: BLOCKED", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
