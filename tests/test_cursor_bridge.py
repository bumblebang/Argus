"""Cursor Auto 뇌 폴백(FileInboxLLM + CLI 한도→bridge) 회귀."""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

from src.agents.llm import (ClaudeCLIClient, ClaudeCLIError, FileInboxLLM,
                            is_usage_limit, _atomic_write_json)
from src.agents import DecisionOutput, ValidationOutput
from src.agents.pipeline import build_cursor_bridge, build_live_llm
from src.config import load_config


def test_is_usage_limit_markers():
    assert is_usage_limit(ClaudeCLIError("weekly limit resets 6pm", rc=1))
    assert is_usage_limit(ClaudeCLIError("session limit resets 5:20am", rc=1))
    assert is_usage_limit(ClaudeCLIError("Usage limit reached. Try again later.", rc=1))
    assert not is_usage_limit(ClaudeCLIError("claude CLI 응답 시간 초과(120s)", rc="timeout"))
    assert not is_usage_limit(ClaudeCLIError("some other failure", rc=1))
    assert not is_usage_limit(ValueError("weekly limit"))  # ClaudeCLIError 만 timeout 가드


def test_file_inbox_round_trip(tmp_path):
    inbox = FileInboxLLM(tmp_path / "inbox", timeout_sec=5, poll_sec=0.05,
                         notify_fn=lambda _s: None)

    def responder():
        deadline = time.time() + 5
        while time.time() < deadline:
            if inbox.request_path.is_file():
                req = json.loads(inbox.request_path.read_text(encoding="utf-8"))
                _atomic_write_json(inbox.response_path, {
                    "id": req["id"],
                    "result": {"market_view": "cursor-ok", "proposals": []},
                })
                return
            time.sleep(0.05)
        raise AssertionError("request never appeared")

    t = threading.Thread(target=responder, daemon=True)
    t.start()
    out = inbox.structured("sys", '{"candidates":[]}', DecisionOutput)
    t.join(timeout=2)
    assert out.market_view == "cursor-ok"
    assert inbox.last_request_id


def test_file_inbox_validation_schema(tmp_path):
    inbox = FileInboxLLM(tmp_path / "inbox", timeout_sec=5, poll_sec=0.05,
                         notify_fn=lambda _s: None)

    def responder():
        deadline = time.time() + 5
        while time.time() < deadline:
            if inbox.request_path.is_file():
                req = json.loads(inbox.request_path.read_text(encoding="utf-8"))
                assert req["schema"] == "ValidationOutput"
                _atomic_write_json(inbox.response_path, {
                    "id": req["id"],
                    "result": {"verdicts": [{"symbol": "005930", "approved": True,
                                             "reason": "[CURSOR_FALLBACK] ok"}]},
                })
                return
            time.sleep(0.05)

    threading.Thread(target=responder, daemon=True).start()
    out = inbox.structured("sys", '{"proposals":[{"symbol":"005930"}]}', ValidationOutput)
    assert out.verdicts[0].approved is True


def test_file_inbox_timeout(tmp_path):
    inbox = FileInboxLLM(tmp_path / "inbox", timeout_sec=0.2, poll_sec=0.05,
                         notify_fn=lambda _s: None)
    with pytest.raises(TimeoutError, match="cursor_bridge 타임아웃"):
        inbox.structured("sys", "{}", DecisionOutput)


def test_cli_limit_uses_cursor_bridge(tmp_path):
    """양쪽 CLI 모델이 한도로 죽으면 FileInboxLLM 이 Decision 을 완주한다.

    require_bridge_armed=False 로 게이트를 끄거나 heartbeat 를 심어 armed 상태로 둔다.
    """
    inbox_dir = tmp_path / "inbox"
    from src.agents.llm import write_bridge_heartbeat
    write_bridge_heartbeat(inbox_dir, now=time.time())
    inbox = FileInboxLLM(inbox_dir, timeout_sec=5, poll_sec=0.05,
                         notify_fn=lambda _s: None)

    def responder():
        deadline = time.time() + 5
        while time.time() < deadline:
            if inbox.request_path.is_file():
                req = json.loads(inbox.request_path.read_text(encoding="utf-8"))
                _atomic_write_json(inbox.response_path, {
                    "id": req["id"],
                    "result": {"market_view": "from-bridge", "proposals": []},
                })
                return
            time.sleep(0.05)

    threading.Thread(target=responder, daemon=True).start()
    fake = Path(__file__).parent / "fake_claude_limit.py"
    cli = ClaudeCLIClient(command=sys.executable, base_args=[str(fake)],
                          model="opus", fallback_model="sonnet",
                          cursor_bridge=inbox, error_dump_path=None,
                          require_bridge_armed=True)
    dec = cli.structured("system", '{"candidates":[]}', DecisionOutput)
    assert dec.market_view == "from-bridge"
    assert cli.last_source == "bridge"


def test_cli_limit_unarmed_raises_quota(tmp_path):
    """heartbeat 없으면 한도 시 bridge 를 안 부르고 BrainQuotaError."""
    from src.agents.llm import BrainQuotaError
    inbox = FileInboxLLM(tmp_path / "inbox", timeout_sec=5, poll_sec=0.05,
                         notify_fn=lambda _s: None)
    fake = Path(__file__).parent / "fake_claude_limit.py"
    cli = ClaudeCLIClient(command=sys.executable, base_args=[str(fake)],
                          model="opus", fallback_model="sonnet",
                          cursor_bridge=inbox, error_dump_path=None,
                          require_bridge_armed=True)
    with pytest.raises(BrainQuotaError):
        cli.structured("system", '{"candidates":[]}', DecisionOutput)


def test_cli_non_limit_skips_bridge():
    """타임아웃 등 비한도 오류는 bridge 를 호출하지 않는다."""
    called = []

    class Bridge:
        def structured(self, system, user, schema):
            called.append(schema.__name__)
            raise AssertionError("bridge must not run")

    cli = ClaudeCLIClient(command="claude", model="opus", error_dump_path=None,
                          cursor_bridge=Bridge())

    def boom(_prompt):
        raise ClaudeCLIError("claude CLI 응답 시간 초과(120s)", rc="timeout")

    cli._run = boom  # type: ignore[method-assign]
    with pytest.raises(ClaudeCLIError, match="시간 초과"):
        cli.structured("sys", "{}", DecisionOutput)
    assert called == []


def test_cli_other_error_skips_bridge():
    called = []

    class Bridge:
        def structured(self, system, user, schema):
            called.append(1)
            return DecisionOutput(market_view="x", proposals=[])

    cli = ClaudeCLIClient(command="claude", model="opus", error_dump_path=None,
                          cursor_bridge=Bridge())

    def boom(_prompt):
        raise ClaudeCLIError("ENOENT or auth weirdness", rc=1)

    cli._run = boom  # type: ignore[method-assign]
    with pytest.raises(ClaudeCLIError):
        cli.structured("sys", "{}", DecisionOutput)
    assert called == []


def test_build_cursor_bridge_disabled(tmp_path):
    cfg = load_config()
    cfg.raw.setdefault("agents", {})["cursor_bridge"] = {"enabled": False}
    assert build_cursor_bridge(cfg) is None
    llm = build_live_llm(cfg, use_cli=True, subscription=False, api_key=None)
    assert getattr(llm, "cursor_bridge", None) is None


def test_build_cursor_bridge_enabled(tmp_path, monkeypatch):
    cfg = load_config()
    cfg.raw.setdefault("agents", {})["cursor_bridge"] = {
        "enabled": True,
        "inbox_dir": str(tmp_path / "llm_inbox"),
        "timeout_sec": 30,
        "poll_sec": 0.5,
        "require_armed": True,
        "armed_max_age_sec": 60,
    }
    bridge = build_cursor_bridge(cfg)
    assert isinstance(bridge, FileInboxLLM)
    assert bridge.timeout_sec == 30
    llm = build_live_llm(cfg, use_cli=True, subscription=False, api_key=None)
    assert llm.cursor_bridge is not None
    assert llm.cursor_bridge.inbox_dir == tmp_path / "llm_inbox"
    assert llm.require_bridge_armed is True
    assert llm.bridge_armed_max_age_sec == 60.0


def test_bridge_docs_use_repo_root_paths():
    root = Path(__file__).resolve().parent.parent
    text = (root / "docs" / "cursor_brain_fallback.md").read_text(encoding="utf-8")
    assert "`data/llm_inbox/bridge.heartbeat`" in text
    assert "`data/llm_inbox/request.json`" in text
    assert "`argus/data/llm_inbox/bridge.heartbeat`" not in text
    assert "경로 앞에 `argus/`" in text
    ps1 = (root / "scripts" / "cursor_brain_fallback_loop.ps1").read_text(encoding="utf-8")
    assert "data/llm_inbox/request.json" in ps1
    assert "argus/data/llm_inbox/request.json" in ps1
