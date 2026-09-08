"""code_rev 워킹트리 지문 — 미커밋 코드를 들고 도는 프로세스를 드러낸다.

git HEAD 만 찍으면, 커밋하지 않은 수정본을 로드한 데몬이 heartbeat·로그에서 "HEAD 와
동기" 로 보인다(실제로 라이브 워처가 나흘 묵은 미커밋 코드를 돌면서 HEAD 를 보고하던
상황). watchdog 의 구코드 감지가 그 구간에서 통째로 무력화되므로 지문을 붙인다.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from src.code_rev import current_code_rev, split_rev, worktree_fingerprint

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import watchdog as wd  # noqa: E402


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """src/ 한 파일을 가진 최소 git 저장소."""
    r = tmp_path / "repo"
    (r / "src").mkdir(parents=True)
    (r / "src" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    (r / "data").mkdir()
    (r / "data" / "state.json").write_text("{}", encoding="utf-8")
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "init")
    return r


def test_clean_worktree_has_no_fingerprint(repo):
    assert worktree_fingerprint(repo) == ""
    rev = current_code_rev(repo)
    assert "+" not in rev and len(rev) == 12


def test_modified_runtime_file_changes_rev(repo):
    clean = current_code_rev(repo)
    (repo / "src" / "mod.py").write_text("x = 2\n", encoding="utf-8")
    current_code_rev.cache_clear()
    dirty = current_code_rev(repo)
    assert dirty != clean
    head, mark = split_rev(dirty)
    assert head == clean and len(mark) == 8


def test_fingerprint_tracks_content_not_just_dirtiness(repo):
    (repo / "src" / "mod.py").write_text("x = 2\n", encoding="utf-8")
    first = worktree_fingerprint(repo)
    (repo / "src" / "mod.py").write_text("x = 3\n", encoding="utf-8")
    assert worktree_fingerprint(repo) != first


def test_data_writes_do_not_move_fingerprint(repo):
    """봇이 매 틱 쓰는 data/ 가 지문에 섞이면 재기동이 폭주한다."""
    (repo / "data" / "state.json").write_text('{"tick": 99}', encoding="utf-8")
    assert worktree_fingerprint(repo) == ""


def test_untracked_runtime_module_counts(repo):
    (repo / "src" / "new.py").write_text("y = 1\n", encoding="utf-8")
    first = worktree_fingerprint(repo)
    assert first != ""
    (repo / "src" / "new.py").write_text("y = 2\n", encoding="utf-8")
    assert worktree_fingerprint(repo) != first     # 미추적 파일 내용 변경도 잡는다


def test_non_git_dir_is_unknown(tmp_path):
    current_code_rev.cache_clear()
    assert current_code_rev(tmp_path) == "unknown"


def test_split_rev():
    assert split_rev("abc123+ff00") == ("abc123", "ff00")
    assert split_rev("abc123") == ("abc123", "")
    assert split_rev("") == ("", "")


# ── watchdog 판정 ────────────────────────────────────────────
def _hb(rev: str) -> dict:
    return {"ts": time.time(), "code_rev": rev}


@pytest.fixture(autouse=True)
def _isolate_rev_state(tmp_path, monkeypatch):
    monkeypatch.setattr(wd, "REV_STATE", tmp_path / "watchdog_rev.json")


def _patch_live(monkeypatch, rev: str) -> None:
    import src.code_rev as CR
    monkeypatch.setattr(CR, "current_code_rev", lambda root=None: rev)


def test_same_rev_is_not_stale(monkeypatch):
    _patch_live(monkeypatch, "aaaaaaaaaaaa")
    assert wd.code_rev_stale(_hb("aaaaaaaaaaaa"))[0] is False


def test_head_mismatch_restarts_immediately(monkeypatch):
    _patch_live(monkeypatch, "bbbbbbbbbbbb")
    assert wd.code_rev_stale(_hb("aaaaaaaaaaaa"))[0] is True


def test_dirty_mismatch_waits_for_stable_fingerprint(monkeypatch):
    """편집 도중 라이브 데몬을 재기동하지 않도록 연속 관측을 요구한다."""
    _patch_live(monkeypatch, "aaaaaaaaaaaa+11111111")
    hb = _hb("aaaaaaaaaaaa")
    assert wd.code_rev_stale(hb)[0] is False          # 1회차 — 아직 안 재기동
    assert wd.code_rev_stale(hb)[0] is True           # 2회차 — 지문이 그대로면 재기동


def test_changing_fingerprint_resets_the_counter(monkeypatch):
    hb = _hb("aaaaaaaaaaaa")
    _patch_live(monkeypatch, "aaaaaaaaaaaa+11111111")
    assert wd.code_rev_stale(hb)[0] is False
    _patch_live(monkeypatch, "aaaaaaaaaaaa+22222222")  # 계속 편집 중
    assert wd.code_rev_stale(hb)[0] is False
    assert wd.code_rev_stale(hb)[0] is True            # 멈춘 뒤에야


def test_process_dirty_but_disk_clean_restarts(monkeypatch):
    """커밋/되돌림으로 디스크가 깨끗해졌는데 프로세스는 미커밋본 — 즉시."""
    _patch_live(monkeypatch, "aaaaaaaaaaaa")
    assert wd.code_rev_stale(_hb("aaaaaaaaaaaa+11111111"))[0] is True


def test_unknown_or_missing_rev_skips_check(monkeypatch):
    _patch_live(monkeypatch, "unknown")
    assert wd.code_rev_stale(_hb("aaaaaaaaaaaa"))[0] is False
    _patch_live(monkeypatch, "aaaaaaaaaaaa")
    assert wd.code_rev_stale({})[0] is False


def test_sync_clears_pending_counter(monkeypatch, tmp_path):
    hb = _hb("aaaaaaaaaaaa")
    _patch_live(monkeypatch, "aaaaaaaaaaaa+11111111")
    assert wd.code_rev_stale(hb)[0] is False
    _patch_live(monkeypatch, "aaaaaaaaaaaa")           # 되돌림 → 동기
    assert wd.code_rev_stale(hb)[0] is False
    assert json.loads(wd.REV_STATE.read_text(encoding="utf-8")) == {}
