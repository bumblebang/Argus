"""Phase 2 data 레이아웃 이동 — doctor --migrate-data 용.

장후·watch 중지 후에만 --apply. 기본은 dry-run.
inbox: data/llm_inbox → data/inbox 이동 후 레거시 경로에 junction/symlink.

SQLite(bot.db)는 WAL 모드라 -wal/-shm 사이드카에 최근 커밋이 남아 있을 수 있다.
메인 파일만 옮기면 원장이 과거로 롤백된다 — 이동 직전 wal_checkpoint(TRUNCATE).
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
from pathlib import Path

from .config import ROOT
from .paths import MIGRATE_MOVES, layout_rel, rel


def _same_file(a: Path, b: Path) -> bool:
    try:
        return a.exists() and b.exists() and a.resolve() == b.resolve()
    except OSError:
        return False


def _is_sqlite_db(path: Path) -> bool:
    return path.suffix.lower() == ".db"


def _wal_sidecars(db_path: Path) -> tuple[Path, Path]:
    s = str(db_path)
    return Path(s + "-wal"), Path(s + "-shm")


def checkpoint_sqlite(db_path: Path) -> None:
    """WAL 프레임을 메인 DB 로 합치고 사이드카를 truncate.

    컷오버·강제 종료 뒤 -wal 만 남은 상태에서 메인 파일만 옮기면 원장 꼬리가
    사라진다. 이동 전 한 번 호출하면 된다.
    """
    if not db_path.is_file():
        return
    try:
        if db_path.stat().st_size == 0:
            return
        header = db_path.read_bytes()[:16]
    except OSError:
        return
    if not header.startswith(b"SQLite format 3"):
        return  # 가짜/빈 바이트 — opaque 이동
    try:
        conn = sqlite3.connect(str(db_path), timeout=60.0)
    except sqlite3.Error as e:
        raise OSError(f"sqlite open 실패 ({db_path}): {e}") from e
    try:
        mode = conn.execute("PRAGMA journal_mode;").fetchone()
        if mode and str(mode[0]).lower() == "wal":
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        conn.commit()
    except sqlite3.Error as e:
        raise OSError(f"wal_checkpoint 실패 ({db_path}): {e}") from e
    finally:
        conn.close()


def _cleanup_sidecars(db_path: Path) -> None:
    """checkpoint 후 남은 빈/잔여 -wal/-shm 삭제(고아 방지)."""
    for side in _wal_sidecars(db_path):
        if not side.exists():
            continue
        try:
            side.unlink()
        except OSError:
            pass


def _move_path(src: Path, dst: Path) -> None:
    """일반 파일 이동. .db 는 checkpoint 후 이동·사이드카 정리."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if _is_sqlite_db(src):
        checkpoint_sqlite(src)
        shutil.move(str(src), str(dst))
        # 사이드카는 원 경로 이름(bot.db-wal)에 남음 — 메인만 옮겨진 뒤 삭제.
        _cleanup_sidecars(src)
        _cleanup_sidecars(dst)
        return
    shutil.move(str(src), str(dst))


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
            _move_path(src, dst)
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
