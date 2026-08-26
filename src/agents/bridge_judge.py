"""Cursor bridge judge — inbox request.json → response.json (real decisions).

Used when claude CLI hits quota and FileInboxLLM is armed. Tries cursor_sdk
(CURSOR_API_KEY) headlessly; if unavailable returns None and an external Cursor
agent (/loop) must write response.json before timeout.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Callable

from .schemas import DecisionOutput, ValidationOutput

log = logging.getLogger(__name__)

JudgeFn = Callable[[str, str, str], dict[str, Any] | None]

_SCHEMA_MODEL = {
    "DecisionOutput": DecisionOutput,
    "ValidationOutput": ValidationOutput,
}


def _schema_hint(schema_name: str) -> str:
    if schema_name == "DecisionOutput":
        return (
            'result must match DecisionOutput: {"market_view": str, "proposals": [...]}. '
            "Each proposal: symbol, market (KR|US), side (BUY|SELL|HOLD), conviction 0-1, "
            "horizon (day|swing|position), target_weight 0-1, thesis, key_risks[]. "
            "BUY thesis and market_view must start with [CURSOR_FALLBACK]."
        )
    if schema_name == "ValidationOutput":
        return (
            'result must match ValidationOutput: {"verdicts": [{"symbol", "approved", "reason"}]}. '
            "One verdict per proposal in user JSON. reason starts with [CURSOR_FALLBACK]."
        )
    return f"result must match schema {schema_name}"


def _extract_json_object(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if not raw:
        raise ValueError("empty judge output")
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw, re.IGNORECASE)
    if fence:
        raw = fence.group(1).strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in judge output")
    return json.loads(raw[start : end + 1])


def _normalize_result(data: dict[str, Any], schema_name: str) -> dict[str, Any]:
    if "result" in data and isinstance(data["result"], dict):
        result = data["result"]
    else:
        result = {k: v for k, v in data.items() if k != "id"}
    model = _SCHEMA_MODEL.get(schema_name)
    if model is None:
        raise ValueError(f"unknown schema {schema_name}")
    validated = model.model_validate(result)
    return validated.model_dump()


def _judge_via_cursor_sdk(system: str, user: str, schema_name: str) -> dict[str, Any] | None:
    api_key = (os.getenv("CURSOR_API_KEY") or "").strip()
    if not api_key:
        return None
    try:
        from cursor_sdk import Agent, AgentOptions, LocalAgentOptions
    except ImportError:
        log.debug("bridge_judge: cursor_sdk not installed")
        return None

    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    model = (os.getenv("CURSOR_BRIDGE_MODEL") or "composer-2.5").strip()
    prompt = (
        "Argus trading brain fallback. Output ONLY one JSON object for the `result` field.\n"
        f"Schema: {schema_name}. {_schema_hint(schema_name)}\n\n"
        f"SYSTEM:\n{system}\n\nUSER (JSON context):\n{user}\n"
    )
    try:
        out = Agent.prompt(
            prompt,
            AgentOptions(
                api_key=api_key,
                model=model,
                local=LocalAgentOptions(cwd=str(root)),
            ),
        )
    except Exception as e:
        log.warning("bridge_judge: cursor_sdk failed: %s", e)
        return None
    text = getattr(out, "result", None) or ""
    try:
        parsed = _extract_json_object(str(text))
        return _normalize_result(parsed, schema_name)
    except (ValueError, json.JSONDecodeError) as e:
        log.warning("bridge_judge: cursor_sdk output parse failed: %s", e)
        return None


def judge_inbox_request(
    req: dict[str, Any],
    *,
    judge_fn: JudgeFn | None = None,
) -> dict[str, Any] | None:
    """Return response payload {id, result} or None if judge cannot run yet."""
    req_id = str(req.get("id") or "")
    schema_name = str(req.get("schema") or "")
    system = str(req.get("system") or "")
    user = str(req.get("user") or "")
    if not req_id or schema_name not in _SCHEMA_MODEL:
        return None

    fn = judge_fn if judge_fn is not None else _judge_via_cursor_sdk
    result = fn(system, user, schema_name)
    if result is None:
        return None
    return {"id": req_id, "result": result}
