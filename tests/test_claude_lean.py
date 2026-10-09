"""경량 CLI 호출(agents.claude_lean) — 인자·시스템 프롬프트 파일·작업 폴더·비경량 불변."""
import json
import subprocess
from types import SimpleNamespace

from pydantic import BaseModel

from src.agents import llm as L


class _Out(BaseModel):
    ok: str


def _fake_run(captured):
    def run(args, input=None, cwd=None, **kw):
        captured.append({"args": list(args), "input": input, "cwd": cwd})
        return SimpleNamespace(returncode=0, stdout='{"ok": "yes"}', stderr="")
    return run


def test_lean_moves_system_to_file_and_strips_harness(tmp_path, monkeypatch):
    cap = []
    monkeypatch.setattr(L.subprocess, "run", _fake_run(cap))
    c = L.ClaudeCLIClient(command="claude", model="sonnet", lean=True, lean_dir=tmp_path)
    out = c.structured("지시문 SYS", '{"x":1}', _Out)
    assert out.ok == "yes"
    a = cap[0]["args"]
    i = a.index("--tools")
    assert a[i + 1] == ""                                   # 내장 도구 전부 끔
    for flag in ("--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence"):
        assert flag in a
    sp = a[a.index("--system-prompt-file") + 1]
    assert open(sp, encoding="utf-8").read() == "지시문 SYS"
    assert "지시문 SYS" not in cap[0]["input"]                # 지시문은 stdin 에 없음
    assert cap[0]["input"].startswith("--- 입력 데이터(JSON) ---")
    assert cap[0]["cwd"] == str(tmp_path / "cwd")            # 빈 작업 폴더
    # 같은 지시문은 같은 파일 재사용
    c.structured("지시문 SYS", '{"x":2}', _Out)
    assert cap[1]["args"][cap[1]["args"].index("--system-prompt-file") + 1] == sp


def test_non_lean_unchanged(monkeypatch, tmp_path):
    cap = []
    monkeypatch.setattr(L.subprocess, "run", _fake_run(cap))
    c = L.ClaudeCLIClient(command="claude", model="sonnet", lean_dir=tmp_path)
    c.structured("지시문 SYS", '{"x":1}', _Out)
    a = cap[0]["args"]
    assert a == ["claude", "-p", "--model", "sonnet"]
    assert cap[0]["input"] == L._build_prompt("지시문 SYS", '{"x":1}', _Out)
    assert cap[0]["cwd"] is None and not (tmp_path / "cwd").exists()


def test_lean_fallback_model_keeps_system_file(monkeypatch, tmp_path):
    calls = []

    def run(args, input=None, cwd=None, **kw):
        calls.append(list(args))
        if len(calls) == 1:
            return SimpleNamespace(returncode=1, stdout="", stderr="overloaded")
        return SimpleNamespace(returncode=0, stdout='{"ok": "y"}', stderr="")
    monkeypatch.setattr(L.subprocess, "run", run)
    c = L.ClaudeCLIClient(command="claude", model="opus", fallback_model="sonnet",
                          lean=True, lean_dir=tmp_path, error_dump_path=None)
    c.structured("S", "{}", _Out)
    assert "--system-prompt-file" in calls[1] and calls[1][calls[1].index("--model") + 1] == "sonnet"


def test_claude_lean_for_shapes():
    assert L.claude_lean_for({"claude_lean": True}, "brain") is True
    assert L.claude_lean_for({"claude_lean": {"athena": True}}, "athena") is True
    assert L.claude_lean_for({"claude_lean": {"athena": True}}, "brain") is False
    assert L.claude_lean_for({"claude_lean": ["value_scan"]}, "value_scan") is True
    assert L.claude_lean_for({}, "athena") is False and L.claude_lean_for(None, "x") is False
