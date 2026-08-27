"""단일 인스턴스 보장(OS 파일 락). 상주 데몬의 중복 기동을 원자적으로 막는다.

배경: 작업스케줄러 재기동/재시작-온-실패가 순간적으로 pythonw 를 2개 띄우면 watch
루프가 2개가 되어 토스 토큰을 두고 경합한다(→401 스로틀, claude 동시 스폰 경합으로
뇌 타임아웃). pidfile 을 '읽고→없으면 쓴다' 식으로 검사하면 두 프로세스가 같은 순간
동시에 통과하는 경쟁 조건(TOCTOU)이 생긴다. 그래서 여기서는 **OS 배타 파일 락**을 쓴다:

  - 락은 커널이 원자적으로 중재 → 동시 기동 경쟁에도 정확히 하나만 획득한다.
  - 획득한 프로세스가 살아있는 동안 파일 핸들을 계속 쥔다.
  - 프로세스가 어떤 식으로 죽든(정상 종료·예외·taskkill /F) OS 가 락을 자동 해제한다
    → stale 락이 남지 않는다(생존 확인 불필요).

락은 `<pidfile>.lock` 의 1바이트에 건다(데이터와 분리). 관측·워치독용으로 `<pidfile>` 에는
현재 pid 를 평문으로 남긴다(락 없음 → 자유롭게 읽힌다). 의존성 없음(msvcrt/fcntl 표준).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _lock_nb(fh) -> None:
    """파일 핸들에 비블로킹 배타 락. 이미 잡혀 있으면 OSError."""
    if sys.platform == "win32":
        import msvcrt
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fh) -> None:
    if sys.platform == "win32":
        import msvcrt
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
    else:
        import fcntl
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass


def _pid_alive(pid: int) -> bool:
    """pid 프로세스가 살아있으면 True(워치독/대시보드 관측용 유틸)."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        k = ctypes.windll.kernel32
        handle = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not k.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            k.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class AlreadyRunning(Exception):
    """다른 인스턴스가 이미 락을 쥐고 있을 때."""

    def __init__(self, pid: int):
        super().__init__(f"이미 실행 중(pid={pid})")
        self.pid = pid


class SingleInstance:
    """OS 파일 락 기반 단일 인스턴스.

        lock = SingleInstance("data/watch.pid")
        lock.acquire()          # 다른 인스턴스가 살아있으면 AlreadyRunning
        ...                     # 상주 작업 (이 동안 락 핸들 유지)
        lock.release()

    컨텍스트 매니저(`with SingleInstance(path): ...`)로도 쓸 수 있다.

    also_lockfiles: 구 코드가 잡던 파생 락 등 **추가 경로**도 함께 잡아,
    레이아웃/버전이 다른 데몬과 서로 다른 락을 쥐고 이중 기동하는 구멍을 막는다.
    """

    def __init__(self, pidfile: str | os.PathLike,
                 lockfile: str | os.PathLike | None = None,
                 also_lockfiles: list[str | os.PathLike] | None = None):
        self.pidfile = Path(pidfile)
        # 락 경로를 pidfile 에서 파생시키면, pidfile 이 레이아웃 전환으로 이동하는
        # 순간 락 파일도 같이 바뀌어 **서로 다른 락을 잡은 두 인스턴스**가 생긴다.
        # 호출측이 고정 경로(paths.resolve("watch_lock"))를 넘길 수 있게 한다.
        self.lockfile = (Path(lockfile) if lockfile is not None
                         else self.pidfile.with_name(self.pidfile.name + ".lock"))
        extras: list[Path] = []
        seen = {self.lockfile.resolve()}
        for raw in (also_lockfiles or []):
            p = Path(raw)
            try:
                key = p.resolve()
            except OSError:
                key = p
            if key in seen:
                continue
            seen.add(key)
            extras.append(p)
        self._also_lockfiles = extras
        self._fh = None
        self._extra_fhs: list = []
        self._acquired = False

    def _read_pid(self) -> int | None:
        try:
            return int(self.pidfile.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None

    def _try_lock(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(path, "a+")
        try:
            _lock_nb(fh)
        except OSError:
            fh.close()
            raise AlreadyRunning(self._read_pid() or -1)
        return fh

    def acquire(self) -> "SingleInstance":
        self.pidfile.parent.mkdir(parents=True, exist_ok=True)
        fh = self._try_lock(self.lockfile)
        extra_fhs = []
        try:
            for path in self._also_lockfiles:
                extra_fhs.append(self._try_lock(path))
        except AlreadyRunning:
            _unlock(fh)
            try:
                fh.close()
            except OSError:
                pass
            for efh in extra_fhs:
                _unlock(efh)
                try:
                    efh.close()
                except OSError:
                    pass
            raise
        self._fh = fh
        self._extra_fhs = extra_fhs
        self._acquired = True
        # 관측용 pid(락 파일과 별개, 락 없음 → 워치독이 자유롭게 읽음).
        try:
            self.pidfile.write_text(str(os.getpid()), encoding="utf-8")
        except OSError:
            pass
        return self

    def release(self) -> None:
        if not self._acquired:
            return
        for efh in self._extra_fhs:
            _unlock(efh)
            try:
                efh.close()
            except OSError:
                pass
        self._extra_fhs = []
        if self._fh is not None:
            _unlock(self._fh)
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None
        # 내 pid 를 담고 있을 때만 삭제(다른 인스턴스 pidfile 을 지우지 않게).
        if self._read_pid() == os.getpid():
            try:
                self.pidfile.unlink()
            except OSError:
                pass
        self._acquired = False

    def __enter__(self) -> "SingleInstance":
        return self.acquire()

    def __exit__(self, *exc) -> bool:
        self.release()
        return False


def legacy_watch_lockfiles(root: Path | None = None) -> list[Path]:
    """구 코드가 잡던 파생 락 경로들 — 신 코드가 같이 잡아 이중 기동을 막는다."""
    from src import paths as _paths
    base = Path(root) if root is not None else _paths.ROOT
    return [
        base / "data" / "watch.pid.lock",
        base / "data" / "state" / "watch.pid.lock",  # 중복은 SingleInstance 가 걸러냄
    ]