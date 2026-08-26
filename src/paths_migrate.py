"""Phase 2 data 레이아웃 이동 — doctor --migrate-data 용.

장후·watch 중지 후에만 --apply. 기본은 dry-run.
inbox: data/llm_inbox → data/inbox 이동 후 레거시 경로에 junction/symlink.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from .config import ROOT
from .paths import MIGRATE_MOVES, layout_rel, rel


def _same_file(a: Path, b: Path) -> bool:
    try:
        return a.exists() and b.exists() and a.resolve() == b.resolve()
    except OSError:
        return False


def plan_moves(*, root: Path | None = None) -> list[dict]:
    """이동/스킵 계획 목록 (apply 전 미리보기)."""
    base = ROOT if root is None else Path(root)
    rows: list[dict] = []
    for src_rel, dst_rel in MIGRATE_MOVES:
        src, dst = base / src_rel, base / dst_rel
        if _same_file(src, dst):
            action = "skip_same"
        elif dst.exists() and not src.exists():
            action = "skip_dst_present"
        elif not src.exists():
            action = "skip_missing"
        elif dst.exists():
            action = "conflict"
        else:
            action = "move"
        rows.append({
            "src": src_rel, "dst": dst_rel, "action": action,
            "src_abs": str(src), "dst_abs": str(dst),
        })

    # inbox 특수
    inbox_legacy = base / rel("inbox")
    inbox_new = base / layout_rel("inbox")
    if _same_file(inbox_legacy, inbox_new):
        iaction = "skip_same"
    elif inbox_new.exists() and not inbox_legacy.exists():
        iaction = "alias_only"  # 신만 있음 → 레거시 junction
    elif inbox_new.exists() and inbox_legacy.exists():
        iaction = "skip_both_present"
    elif inbox_legacy.exists() and not inbox_new.exists():
        iaction = "move_inbox"
    else:
        iaction = "skip_missing"
    rows.append({
        "src": rel("inbox"), "dst": layout_rel("inbox"),
        "action": iaction,
        "src_abs": str(inbox_legacy), "dst_abs": str(inbox_new),
    })
    return rows


def _link_dir(link: Path, target: Path) -> None:
    """디렉터리 별칭: Windows=junction, 그 외=symlink."""
    if link.exists():
        return
    link.parent.mkdir(parents=True, exist_ok=True)
    target = target.resolve()
    if sys.platform == "win32":
        import subprocess
        # mklink /J 는 관리자 불필요
        r = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True, text=True, check=False,
        )
        if r.returncode != 0:
            raise OSError(f"junction failed: {r.stderr or r.stdout}")
    else:
        os.symlink(target, link, target_is_directory=True)


def apply_moves(*, root: Path | None = None, dry_run: bool = True) -> list[dict]:
    """계획 실행. dry_run=True 면 파일시스템 변경 없음."""
    base = ROOT if root is None else Path(root)
    plan = plan_moves(root=base)
    results: list[dict] = []
    for row in plan:
        action = row["action"]
        out = dict(row)
        if action.startswith("skip") or action == "conflict":
            out["result"] = action
            results.append(out)
            continue
        if dry_run:
            out["result"] = f"dry:{action}"
            results.append(out)
            continue

        src = Path(row["src_abs"])
        dst = Path(row["dst_abs"])
        if action == "move":
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            out["result"] = "moved"
        elif action == "move_inbox":
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            _link_dir(src, dst)  # data/llm_inbox → data/inbox
            out["result"] = "moved+alias"
        elif action == "alias_only":
            _link_dir(src, dst)
            out["result"] = "aliased"
        else:
            out["result"] = f"noop:{action}"
        results.append(out)
    return results


def format_plan(rows: list[dict]) -> str:
    lines = ["data migrate plan:"]
    for r in rows:
        tag = r.get("result") or r["action"]
        lines.append(f"  [{tag}] {r['src']} -> {r['dst']}")
    return "\n".join(lines)
