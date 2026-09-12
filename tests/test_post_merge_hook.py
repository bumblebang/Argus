"""post-merge 훅 / 워치독 — 출력이 재기동을 막지 못한다.

2026-09-11 PR #51 머지: 안내 문구의 em dash(U+2014)가 cp949 콘솔에서
UnicodeEncodeError 를 던져 훅이 죽었고, 그 print 가 restart_watch() 앞이라
자동 재기동이 유실됐다(데몬이 구 커밋 코드로 계속 운행).
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import post_merge_restart as pmr  # noqa: E402
import watchdog as wd  # noqa: E402


class _Cp949Stream:
    """cp949 콘솔 흉내 — em dash 등 인코딩 불가 문자에 UnicodeEncodeError."""
    def __init__(self):
        self.written = []

    def write(self, s: str) -> int:
        s.encode("cp949")          # 인코딩 불가 문자면 여기서 터진다
        self.written.append(s)
        return len(s)

    def flush(self) -> None:
        pass


_MSG = "post-merge: watch 코드 변경 감지 — ArgusWatch 재기동"


def test_say_survives_console_encoding_failure(monkeypatch):
    out = _Cp949Stream()
    monkeypatch.setattr(pmr.sys, "stdout", out)
    pmr.say(_MSG)                                   # 예외 없이 통과
    joined = "".join(out.written)
    assert "post-merge" in joined and "ArgusWatch" in joined
    assert "\\u2014" in joined                      # em dash 는 ASCII 로 강등

    # 한글이 찍히는 콘솔이면 원문 그대로
    ok = _Cp949Stream()
    monkeypatch.setattr(pmr.sys, "stdout", ok)
    pmr.say("post-merge: 재기동")
    assert "재기동" in "".join(ok.written)


def test_say_tolerates_missing_stdout(monkeypatch):
    monkeypatch.setattr(pmr.sys, "stdout", None)    # pythonw 무콘솔
    pmr.say("아무거나")                              # 예외 없음


def test_restart_runs_even_when_output_fails(monkeypatch):
    calls = []
    monkeypatch.setattr(pmr, "paths_changed", lambda prev: True)
    monkeypatch.setattr(pmr, "restart_watch", lambda: calls.append("restart") or True)
    monkeypatch.setattr(pmr.sys, "stdout", _Cp949Stream())
    monkeypatch.setattr(pmr.sys, "argv", ["post_merge_restart.py", "abc123"])
    assert pmr.main() == 0
    assert calls == ["restart"]                     # 출력 실패에도 재기동은 나갔다


def test_no_restart_when_watch_paths_untouched(monkeypatch):
    calls = []
    monkeypatch.setattr(pmr, "paths_changed", lambda prev: False)
    monkeypatch.setattr(pmr, "restart_watch", lambda: calls.append("restart") or True)
    monkeypatch.setattr(pmr.sys, "argv", ["post_merge_restart.py", "abc123"])
    assert pmr.main() == 0 and calls == []


def test_watchdog_log_does_not_raise_on_bad_console(monkeypatch, tmp_path):
    monkeypatch.setattr(wd, "LOG", tmp_path / "watchdog.log")
    monkeypatch.setattr(wd.sys, "stdout", _Cp949Stream())
    wd.log("[watchdog] poll unhealthy — alert-check 담당")   # 예외 없음
    assert "alert-check" in (tmp_path / "watchdog.log").read_text(encoding="utf-8")


def test_restart_watch_hides_powershell_on_windows(monkeypatch):
    """재기동 powershell 이 콘솔 창을 띄우지 않는다(CREATE_NO_WINDOW + Hidden)."""
    if not sys.platform.startswith("win"):
        return
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = list(argv)
        seen["kw"] = kw
        class R:
            returncode = 0
            stderr = ""
            stdout = ""
        return R()

    monkeypatch.setattr(pmr.subprocess, "run", fake_run)
    assert pmr.restart_watch() is True
    assert seen["argv"][0] == "powershell"
    assert "-WindowStyle" in seen["argv"] and "Hidden" in seen["argv"]
    assert seen["kw"].get("creationflags") == pmr.subprocess.CREATE_NO_WINDOW


def test_watchdog_restart_uses_no_window_on_windows(monkeypatch):
    if not sys.platform.startswith("win"):
        return
    flags = []

    def fake_run(argv, **kw):
        flags.append(kw.get("creationflags"))

    monkeypatch.setattr(wd.subprocess, "run", fake_run)
    monkeypatch.setattr(wd.sys, "platform", "win32")
    wd.restart()
    assert flags and all(f == wd.subprocess.CREATE_NO_WINDOW for f in flags)
