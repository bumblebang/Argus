"""J5 — 싱글턴 락 경로가 레이아웃 전환 중 갈라져 중복 기동을 허용하던 결함.

재현(수정 전): SingleInstance 가 lockfile 을 pidfile 이름에서 파생시키고,
paths.resolve("watch_pid") 는 "존재 우선"이라 컷오버 도중 프로세스마다 다른
pidfile → 다른 lockfile 을 잡는다. 락이 서로 다르니 둘 다 acquire 성공.
"""
import os

import pytest

from src import paths as p
from src.engine.singleton import AlreadyRunning, SingleInstance


# ── 락 키는 존재 여부와 무관하게 LAYOUT 고정 ──────────────────────
def test_watch_lock_is_layout_fixed_even_if_legacy_exists(tmp_path):
    legacy = tmp_path / "data" / "watch.pid.lock"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("x")                      # 레거시가 '존재'해도
    got = p.resolve("watch_lock", root=tmp_path)
    assert got == (tmp_path / "data" / "state" / "watch.pid.lock").resolve()


def test_watch_lock_ignores_configured_override(tmp_path):
    got = p.resolve("watch_lock", root=tmp_path, configured="data/watch.pid.lock")
    assert got.as_posix().endswith("data/state/watch.pid.lock")


def test_watch_pid_still_dual_searches(tmp_path):
    """pid(관측용)는 기존 존재우선 동작 유지 — 락만 고정한다."""
    legacy = tmp_path / "data" / "watch.pid"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("123")
    assert p.resolve("watch_pid", root=tmp_path) == legacy.resolve()


def test_watch_lock_not_in_migrate_moves():
    """Windows 는 잠긴 파일 move 가 실패한다 — 이동 계획에 넣지 않는다."""
    srcs = [s for s, _ in p.MIGRATE_MOVES]
    assert not any(s.endswith(".lock") for s in srcs)


def test_watch_lock_registered_in_both_maps():
    assert p.rel("watch_lock") == "data/watch.pid.lock"
    assert p.layout_rel("watch_lock") == "data/state/watch.pid.lock"


# ── 컷오버 중 중복 기동 재현 ────────────────────────────────────
def test_split_pidfile_shares_one_lock(tmp_path):
    """pidfile 이 갈라져도 락이 공유되면 두 번째 기동이 막힌다."""
    lock_path = tmp_path / "state" / "watch.pid.lock"
    first = SingleInstance(tmp_path / "legacy.pid", lockfile=lock_path).acquire()
    try:
        with pytest.raises(AlreadyRunning):
            SingleInstance(tmp_path / "new.pid", lockfile=lock_path).acquire()
    finally:
        first.release()


def test_derived_lockfile_would_not_block(tmp_path):
    """(대조군) 파생 락이면 둘 다 통과한다 — 이게 원래 결함이다."""
    a = SingleInstance(tmp_path / "legacy.pid").acquire()
    b = SingleInstance(tmp_path / "new.pid").acquire()
    assert a._acquired and b._acquired
    a.release()
    b.release()


def test_explicit_lockfile_creates_parent(tmp_path):
    lock_path = tmp_path / "deep" / "nested" / "w.lock"
    inst = SingleInstance(tmp_path / "w.pid", lockfile=lock_path).acquire()
    assert lock_path.exists()
    inst.release()


def test_default_lockfile_unchanged(tmp_path):
    inst = SingleInstance(tmp_path / "w.pid")
    assert inst.lockfile == tmp_path / "w.pid.lock"


def test_pidfile_still_written(tmp_path):
    inst = SingleInstance(tmp_path / "w.pid",
                          lockfile=tmp_path / "other.lock").acquire()
    assert (tmp_path / "w.pid").read_text().strip() == str(os.getpid())
    inst.release()


# ── migrate --apply 가드 ───────────────────────────────────────
def test_migrate_apply_blocked_while_watch_running(monkeypatch, tmp_path):
    import sys
    sys.path.insert(0, str(p.ROOT / "scripts"))
    import doctor

    lock_path = tmp_path / "w.lock"
    monkeypatch.setattr(p, "resolve",
                        lambda key, **kw: (lock_path if key == "watch_lock"
                                           else tmp_path / "w.pid"))
    monkeypatch.setattr("src.market_hours.is_open", lambda m, now=None: False)

    holder = SingleInstance(tmp_path / "w.pid", lockfile=lock_path).acquire()
    try:
        blockers = doctor._migrate_blockers()
    finally:
        holder.release()
    assert any("watch 실행 중" in b for b in blockers)


def test_migrate_apply_blocked_during_market_hours(monkeypatch, tmp_path):
    import sys
    sys.path.insert(0, str(p.ROOT / "scripts"))
    import doctor

    monkeypatch.setattr(p, "resolve",
                        lambda key, **kw: (tmp_path / "w.lock" if key == "watch_lock"
                                           else tmp_path / "w.pid"))
    monkeypatch.setattr("src.market_hours.is_open", lambda m, now=None: m == "KR")
    blockers = doctor._migrate_blockers()
    assert any("장중" in b for b in blockers)


def test_migrate_apply_allowed_when_idle_and_closed(monkeypatch, tmp_path):
    import sys
    sys.path.insert(0, str(p.ROOT / "scripts"))
    import doctor

    monkeypatch.setattr(p, "resolve",
                        lambda key, **kw: (tmp_path / "w.lock" if key == "watch_lock"
                                           else tmp_path / "w.pid"))
    monkeypatch.setattr("src.market_hours.is_open", lambda m, now=None: False)
    assert doctor._migrate_blockers() == []
