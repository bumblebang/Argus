"""engine.singleton — OS 파일 락 단일 인스턴스 검증."""
import os

import pytest

from src.engine.singleton import SingleInstance, AlreadyRunning, _pid_alive


def test_acquire_creates_pidfile(tmp_path):
    pf = tmp_path / "w.pid"
    lock = SingleInstance(pf)
    lock.acquire()
    assert pf.read_text().strip() == str(os.getpid())
    lock.release()
    assert not pf.exists()


def test_second_instance_blocked_while_first_holds_lock(tmp_path):
    # OS 락은 같은 프로세스의 다른 핸들끼리도 충돌하므로 인프로세스로 경쟁을 재현할 수 있다.
    pf = tmp_path / "w.pid"
    first = SingleInstance(pf).acquire()
    with pytest.raises(AlreadyRunning) as ei:
        SingleInstance(pf).acquire()
    assert ei.value.pid == os.getpid()            # pidfile 이 알려주는 현 보유자
    first.release()


def test_leftover_pidfile_without_lock_is_acquirable(tmp_path):
    # 크래시로 pidfile 만 남고 락은 OS 가 이미 해제한 상황 → 새 인스턴스가 획득 가능해야.
    pf = tmp_path / "w.pid"
    pf.write_text("999999999")
    lock = SingleInstance(pf).acquire()
    assert pf.read_text().strip() == str(os.getpid())
    lock.release()


def test_reacquire_after_release(tmp_path):
    pf = tmp_path / "w.pid"
    SingleInstance(pf).acquire().release()
    SingleInstance(pf).acquire().release()        # 락이 제대로 풀려 재획득 성공


def test_release_only_removes_own_pidfile(tmp_path):
    pf = tmp_path / "w.pid"
    lock = SingleInstance(pf).acquire()
    pf.write_text("12345")                         # 다른 인스턴스가 덮어쓴 상황 모사
    lock.release()                                 # 내 pid 아니므로 삭제하지 않음
    assert pf.exists() and pf.read_text().strip() == "12345"


def test_context_manager(tmp_path):
    pf = tmp_path / "w.pid"
    with SingleInstance(pf):
        assert pf.exists()
        with pytest.raises(AlreadyRunning):
            SingleInstance(pf).acquire()
    assert not pf.exists()


def test_pid_alive_self():
    assert _pid_alive(os.getpid()) is True


def test_pid_alive_invalid():
    assert _pid_alive(0) is False
    assert _pid_alive(-1) is False
