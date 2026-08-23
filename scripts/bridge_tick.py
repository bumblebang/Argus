#!/usr/bin/env python3
"""Argus cursor-bridge tick — heartbeat + optional zero-LLM auto reply.

Modes (token-cheap by default):
  (default)     refresh heartbeat; print request status (no response write)
  --auto        heartbeat + if request.json, write frugal HOLD/reject response
  --heartbeat-only
  --heartbeat-loop [sec]   daemon: heartbeat forever every N sec (default 60)
                           no agent / no LLM — keeps bridge armed

Agent should NOT wake every minute. Prefer --heartbeat-loop in background
and --auto on demand (or a rare watcher). See .cursor/skills/argus-bridge.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.agents.llm import (  # noqa: E402
    _atomic_write_json,
    is_bridge_armed,
    write_bridge_heartbeat,
)

INBOX = ROOT / "data" / "llm_inbox"
STATE = INBOX / "bridge.tick_state.json"


def _load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}


def _save_state(state: dict) -> None:
    _atomic_write_json(STATE, state)


def _heartbeat() -> bool:
    INBOX.mkdir(parents=True, exist_ok=True)
    write_bridge_heartbeat(INBOX)
    return is_bridge_armed(INBOX, max_age_sec=90)


def _read_request() -> dict | None:
    path = INBOX / "request.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _auto_decision(req_id: str) -> dict:
    return {
        "id": req_id,
        "result": {
            "market_view": "[CURSOR_FALLBACK] frugal auto — hold (quota bridge)",
            "proposals": [],
        },
    }


def _auto_validation(req: dict) -> dict:
    """Reject all proposals in user JSON; empty if none. No deep read of system."""
    req_id = str(req.get("id") or "")
    verdicts: list[dict] = []
    raw_user = req.get("user") or "{}"
    try:
        user = json.loads(raw_user) if isinstance(raw_user, str) else (raw_user or {})
    except (ValueError, TypeError):
        user = {}
    for p in user.get("proposals") or []:
        if not isinstance(p, dict):
            continue
        sym = p.get("symbol")
        if not sym:
            continue
        verdicts.append({
            "symbol": str(sym),
            "approved": False,
            "reason": "[CURSOR_FALLBACK] frugal auto-reject",
        })
    return {"id": req_id, "result": {"verdicts": verdicts}}


def _write_auto_response(req: dict) -> str:
    req_id = str(req.get("id") or "")
    schema = str(req.get("schema") or "")
    state = _load_state()
    if state.get("last_answered_id") == req_id and (INBOX / "response.json").is_file():
        return f"request={schema} id={req_id} skipped=already_answered"

    if schema == "DecisionOutput":
        payload = _auto_decision(req_id)
    elif schema == "ValidationOutput":
        payload = _auto_validation(req)
    else:
        return f"request={schema} id={req_id} error=unknown_schema"

    _atomic_write_json(INBOX / "response.json", payload)
    state["last_answered_id"] = req_id
    state["last_schema"] = schema
    state["ts"] = time.time()
    _save_state(state)
    return f"request={schema} id={req_id} auto=wrote_response"


def run_once(*, auto: bool, heartbeat_only: bool) -> int:
    ok = _heartbeat()
    print(f"heartbeat={'ok' if ok else 'fail'} inbox={INBOX}")
    if heartbeat_only:
        return 0 if ok else 1

    req = _read_request()
    if req is None:
        print("request=none")
        return 0

    schema = req.get("schema", "?")
    rid = req.get("id", "?")
    if auto:
        print(_write_auto_response(req))
    else:
        print(f"request={schema} id={rid}")
        print(f"request_path={INBOX / 'request.json'}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--auto", action="store_true",
                    help="Write frugal HOLD/reject response without LLM")
    ap.add_argument("--heartbeat-only", action="store_true")
    ap.add_argument("--heartbeat-loop", nargs="?", const=60, type=int, metavar="SEC",
                    help="Loop heartbeat forever every SEC seconds (default 60)")
    args = ap.parse_args()

    if args.heartbeat_loop is not None:
        sec = max(15, int(args.heartbeat_loop))
        print(f"heartbeat-loop every {sec}s → {INBOX}", flush=True)
        while True:
            ok = _heartbeat()
            print(f"{time.strftime('%H:%M:%S')} heartbeat={'ok' if ok else 'fail'}",
                  flush=True)
            time.sleep(sec)

    return run_once(auto=args.auto, heartbeat_only=args.heartbeat_only)


if __name__ == "__main__":
    raise SystemExit(main())
