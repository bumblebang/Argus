"""Phase 4 — research lab 경계.

런타임(src/scripts 상주·배치)은 research 를 import 하지 않는다.
data/quant_review/ 잔여는 research/quant_review/data/ 로 이관(기본 dry-run).
"""
from __future__ import annotations

import shutil
from pathlib import Path

from .config import ROOT

RESIDUE_REL = "data/quant_review"
LAB_DATA_REL = "research/quant_review/data"
ARCHIVE_REL = "data/archive/quant_review"


def residue_dir(*, root: Path | None = None) -> Path:
    return (ROOT if root is None else Path(root)) / RESIDUE_REL


def lab_data_dir(*, root: Path | None = None) -> Path:
    return (ROOT if root is None else Path(root)) / LAB_DATA_REL


def archive_dir(*, root: Path | None = None) -> Path:
    return (ROOT if root is None else Path(root)) / ARCHIVE_REL


def residue_status(*, root: Path | None = None) -> dict:
    """data/quant_review 존재·파일 수. 런타임 미사용."""
    base = ROOT if root is None else Path(root)
    path = base / RESIDUE_REL
    if not path.is_dir():
        return {"present": False, "files": 0, "path": str(path)}
    files = [p for p in path.rglob("*") if p.is_file()]
    return {
        "present": True,
        "files": len(files),
        "path": str(path),
        "lab_dest": str(base / LAB_DATA_REL),
    }


def plan_migrate(*, root: Path | None = None,
                 dest: str = "lab") -> list[dict]:
    """잔여 파일 → lab data 또는 archive. dest: lab|archive."""
    base = ROOT if root is None else Path(root)
    src_root = base / RESIDUE_REL
    dst_root = base / (LAB_DATA_REL if dest == "lab" else ARCHIVE_REL)
    rows: list[dict] = []
    if not src_root.is_dir():
        return [{"action": "skip_missing", "src": RESIDUE_REL, "dst": str(dst_root)}]
    for path in sorted(src_root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(src_root).as_posix()
        dst = dst_root / rel
        if dst.exists():
            action = "conflict" if dst.read_bytes() != path.read_bytes() else "skip_same"
        else:
            action = "move"
        rows.append({
            "action": action,
            "src": f"{RESIDUE_REL}/{rel}",
            "dst": f"{dst_root.relative_to(base).as_posix()}/{rel}",
            "src_abs": str(path),
            "dst_abs": str(dst),
        })
    return rows


def apply_migrate(*, root: Path | None = None, dest: str = "lab",
                  dry_run: bool = True) -> list[dict]:
    base = ROOT if root is None else Path(root)
    plan = plan_migrate(root=base, dest=dest)
    out: list[dict] = []
    for row in plan:
        r = dict(row)
        act = row["action"]
        if act.startswith("skip") or act == "conflict":
            r["result"] = act
            out.append(r)
            continue
        if dry_run:
            r["result"] = f"dry:{act}"
            out.append(r)
            continue
        src, dst = Path(row["src_abs"]), Path(row["dst_abs"])
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        r["result"] = "moved"
        out.append(r)
    # 빈 잔여 디렉터리 정리(apply 시)
    if not dry_run:
        residue = base / RESIDUE_REL
        if residue.is_dir() and not any(residue.rglob("*")):
            try:
                residue.rmdir()
            except OSError:
                pass
        elif residue.is_dir():
            # 남은 빈 하위 폴더만 걷어내기 시도
            for p in sorted(residue.rglob("*"), reverse=True):
                if p.is_dir():
                    try:
                        p.rmdir()
                    except OSError:
                        pass
            try:
                residue.rmdir()
            except OSError:
                pass
    return out


def format_plan(rows: list[dict]) -> str:
    lines = ["research residue migrate plan:"]
    for r in rows:
        tag = r.get("result") or r["action"]
        lines.append(f"  [{tag}] {r.get('src')} -> {r.get('dst')}")
    if len(rows) == 1 and rows[0].get("action") == "skip_missing":
        lines.append("  (data/quant_review 없음 — 이미 깨끗)")
    return "\n".join(lines)
