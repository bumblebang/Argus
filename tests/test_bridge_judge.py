"""bridge_judge + bridge_tick judge mode."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts import bridge_tick  # noqa: E402
from src.agents.bridge_judge import judge_inbox_request, _normalize_result
from src.agents.llm import _atomic_write_json


def test_normalize_decision_result():
    data = {
        "market_view": "[CURSOR_FALLBACK] test",
        "proposals": [],
    }
    out = _normalize_result(data, "DecisionOutput")
    assert out["market_view"].startswith("[CURSOR_FALLBACK]")


def test_judge_inbox_mock_fn():
    req = {
        "id": "abc",
        "schema": "DecisionOutput",
        "system": "sys",
        "user": '{"candidates":[]}',
    }

    def fake(_s, _u, schema):
        assert schema == "DecisionOutput"
        return {"market_view": "[CURSOR_FALLBACK] ok", "proposals": []}

    payload = judge_inbox_request(req, judge_fn=fake)
    assert payload is not None
    assert payload["id"] == "abc"
    assert payload["result"]["market_view"] == "[CURSOR_FALLBACK] ok"


def test_judge_inbox_no_fn_returns_none():
    req = {"id": "x", "schema": "DecisionOutput", "system": "s", "user": "{}"}
    assert judge_inbox_request(req, judge_fn=lambda *_: None) is None


def test_bridge_tick_judge_writes_response(tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    monkeypatch.setattr(bridge_tick, "INBOX", inbox)
    monkeypatch.setattr(bridge_tick, "STATE", inbox / "bridge.tick_state.json")

    req = {
        "id": "rid1",
        "schema": "DecisionOutput",
        "system": "s",
        "user": "{}",
    }
    _atomic_write_json(inbox / "request.json", req)

    def fake_judge(req):
        return {
            "id": req["id"],
            "result": {
                "market_view": "[CURSOR_FALLBACK] from test",
                "proposals": [],
            },
        }

    monkeypatch.setattr(bridge_tick, "judge_inbox_request", fake_judge)

    msg = bridge_tick._write_judge_response(req)
    assert "judge=wrote_response" in msg
    resp = json.loads((inbox / "response.json").read_text(encoding="utf-8"))
    assert resp["id"] == "rid1"
    assert resp["result"]["market_view"] == "[CURSOR_FALLBACK] from test"


def test_bridge_tick_auto_still_hold(tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    monkeypatch.setattr(bridge_tick, "INBOX", inbox)
    monkeypatch.setattr(bridge_tick, "STATE", inbox / "bridge.tick_state.json")
    req = {"id": "r2", "schema": "DecisionOutput", "system": "", "user": "{}"}
    msg = bridge_tick._write_auto_response(req)
    assert "auto=wrote_response" in msg
    resp = json.loads((inbox / "response.json").read_text(encoding="utf-8"))
    assert resp["result"]["proposals"] == []
