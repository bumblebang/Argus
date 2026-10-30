"""claude.exe 경로 해석 — 데스크톱 앱 `<ver>/<해시>/claude.exe` 배치·버전 핀 이탈 복구."""
import os
from pathlib import Path
from types import SimpleNamespace

from pydantic import BaseModel

from src.agents import llm as L


class _Out(BaseModel):
    ok: str


def _exe(base: Path, *parts: str, mtime: float | None = None) -> Path:
    p = base.joinpath(*parts, "claude.exe")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"")
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


def _pkg_base(tmp_path: Path, monkeypatch) -> Path:
    """LOCALAPPDATA/Packages/Claude_*/…/claude-code 를 tmp 에 만들고 환경변수를 돌린다."""
    local = tmp_path / "Local"
    base = local / "Packages" / "Claude_test" / "LocalCache" / "Roaming" / "Claude" / "claude-code"
    base.mkdir(parents=True)
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    return base


def test_hashed_layout_resolves_stale_pin_to_latest(tmp_path, monkeypatch):
    base = _pkg_base(tmp_path, monkeypatch)
    _exe(base, "2.1.308", "c17a819afb40")
    want = _exe(base, "2.1.309", "9078b9c1d466")
    stale = str(base / "2.1.237" / "claude.exe")       # 지워진 옛 핀
    assert L.resolve_claude_command(stale) == str(want)


def test_flat_layout_still_supported(tmp_path, monkeypatch):
    base = _pkg_base(tmp_path, monkeypatch)
    _exe(base, "2.1.187")
    want = _exe(base, "2.1.237")
    assert L.resolve_claude_command(str(base / "2.1.187" / "claude.exe")) == str(want)


def test_mixed_layout_picks_highest_version(tmp_path, monkeypatch):
    base = _pkg_base(tmp_path, monkeypatch)
    _exe(base, "2.1.237")                               # 옛 평면 배치
    want = _exe(base, "2.1.309", "9078b9c1d466")        # 새 해시 배치
    assert L.resolve_claude_command(str(base / "2.1.237" / "claude.exe")) == str(want)


def test_same_version_multiple_hashes_prefers_newest(tmp_path, monkeypatch):
    base = _pkg_base(tmp_path, monkeypatch)
    _exe(base, "2.1.309", "aaaa", mtime=1_000_000)
    want = _exe(base, "2.1.309", "bbbb", mtime=2_000_000)
    assert L.resolve_claude_command(str(base / "2.1.309" / "aaaa" / "claude.exe")) == str(want)


def test_hashed_config_path_infers_base_without_env(tmp_path, monkeypatch):
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    base = tmp_path / "claude-code"
    _exe(base, "2.1.308", "c17a819afb40")
    want = _exe(base, "2.1.309", "9078b9c1d466")
    cfg = str(base / "2.1.308" / "c17a819afb40" / "claude.exe")
    assert L.resolve_claude_command(cfg) == str(want)


def test_bare_and_unrelated_commands_untouched(tmp_path, monkeypatch):
    _pkg_base(tmp_path, monkeypatch)
    assert L.resolve_claude_command("claude") == "claude"
    assert L.resolve_claude_command("C:/tools/python.exe") == "C:/tools/python.exe"


def test_runtime_rediscovers_after_app_update(tmp_path, monkeypatch):
    """데몬 기동 후 앱이 업데이트돼 옛 exe 가 사라지면 다음 호출에서 새 해시 경로로 갈아탄다."""
    base = _pkg_base(tmp_path, monkeypatch)
    old = _exe(base, "2.1.308", "c17a819afb40")
    c = L.ClaudeCLIClient(command=str(old), model="sonnet", error_dump_path=None)
    assert c.command == str(old)
    # 업데이트: 새 버전 설치 + 옛 폴더 삭제
    new = _exe(base, "2.1.309", "9078b9c1d466")
    old.unlink()
    calls = []

    def run(args, **kw):
        calls.append(args[0])
        if not Path(args[0]).is_file():
            raise FileNotFoundError(args[0])
        return SimpleNamespace(returncode=0, stdout='{"ok": "yes"}', stderr="")

    monkeypatch.setattr(L.subprocess, "run", run)
    assert c.structured("SYS", "{}", _Out).ok == "yes"
    assert calls == [str(old), str(new)]
    assert c.command == str(new)
