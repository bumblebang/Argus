#!/usr/bin/env python3
"""Argus cursor-bridge tick - heartbeat + judge (default) or frugal auto.

Modes (token-aware):
  (default)     refresh heartbeat; print request status (no response write)
  --judge       heartbeat + if request.json, real judge (cursor_sdk or pending)
  --auto        heartbeat + frugal HOLD/reject (legacy frugal mode)
  --heartbeat-only
  --serve [sec]            one terminal: heartbeat + judge every N sec (default 60)
  --heartbeat-loop [sec]   heartbeat only every N sec (default 60)

Claude CLI remains primary; this inbox path activates on quota only (see docs).

See .cursor/skills/argus-bridge.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.agents.bridge_judge import judge_inbox_request  # noqa: E402
from src.agents.llm import (  # noqa: E402
    _atomic_write_json,
    is_bridge_armed,
    write_bridge_heartbeat,
)
from src import paths as _paths  # noqa: E402

INBOX = _paths.resolve("inbox", configured="data/llm_inbox")
STATE = INBOX / "bridge.tick_state.json"
MODE_FILE = INBOX / "bridge.mode"


def _load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}


def _save_state(state: dict) -> None:
    _atomic_write_json(STATE, state)


def _write_mode(mode: str) -> None:
    INBOX.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(MODE_FILE, {"mode": mode, "ts": time.time()})


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
            "market_view": "[CURSOR_FALLBACK] frugal auto - hold (quota bridge)",
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


def _already_answered(req_id: str) -> bool:
    state = _load_state()
    if state.get("last_answered_id") != req_id:
        return False
    return (INBOX / "response.json").is_file()


def _mark_answered(req_id: str, schema: str) -> None:
    state = _load_state()
    state["last_answered_id"] = req_id
    state["last_schema"] = schema
    state["ts"] = time.time()
    _save_state(state)


def _write_auto_response(req: dict) -> str:
    req_id = str(req.get("id") or "")
    schema = str(req.get("schema") or "")
    if _already_answered(req_id):
        return f"request={schema} id={req_id} skipped=already_answered"

    if schema == "DecisionOutput":
        payload = _auto_decision(req_id)
    elif schema == "ValidationOutput":
        payload = _auto_validation(req)
    else:
        return f"request={schema} id={req_id} error=unknown_schema"

    _atomic_write_json(INBOX / "response.json", payload)
    _mark_answered(req_id, schema)
    return f"request={schema} id={req_id} auto=wrote_response"


def _write_judge_response(req: dict) -> str:
    req_id = str(req.get("id") or "")
    schema = str(req.get("schema") or "")
    if _already_answered(req_id):
        return f"request={schema} id={req_id} skipped=already_answered"

    payload = judge_inbox_request(req)
    if payload is None:
        return (
            f"request={schema} id={req_id} judge=pending "
            "(set CURSOR_API_KEY+pip install cursor-sdk, or Cursor /loop judge)"
        )

    _atomic_write_json(INBOX / "response.json", payload)
    _mark_answered(req_id, schema)
    return f"request={schema} id={req_id} judge=wrote_response"


def run_once(*, mode: str = "judge", heartbeat_only: bool = False) -> int:
    ok = _heartbeat()
    print(f"heartbeat={'ok' if ok else 'fail'} mode={mode} inbox={INBOX}")
    if heartbeat_only:
        return 0 if ok else 1

    req = _read_request()
    if req is None:
        print("request=none")
        return 0

    schema = req.get("schema", "?")
    rid = req.get("id", "?")
    if mode == "auto":
        print(_write_auto_response(req))
    elif mode == "judge":
        print(_write_judge_response(req))
    else:
        print(f"request={schema} id={rid}")
        print(f"request_path={INBOX / 'request.json'}")
    return 0


def _serve_loop(sec: int, *, mode: str) -> None:
    sec = max(15, int(sec))
    _write_mode(mode)
    print(f"serve every {sec}s (heartbeat+{mode}) → {INBOX}", flush=True)
    while True:
        run_once(mode=mode, heartbeat_only=False)
        time.sleep(sec)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--auto",
        action="store_true",
        help="Frugal HOLD/reject (overrides default judge on --serve)",
    )
    ap.add_argument(
        "--judge",
        action="store_true",
        help="One-shot: heartbeat + judge pending request (default for --serve)",
    )
    ap.add_argument("--heartbeat-only", action="store_true")
    ap.add_argument(
        "--serve",
        nargs="?",
        const=60,
        type=int,
        metavar="SEC",
        help="Loop heartbeat+judge every SEC seconds (default 60; use --auto for frugal)",
    )
    ap.add_argument(
        "--heartbeat-loop",
        nargs="?",
        const=60,
        type=int,
        metavar="SEC",
        help="Loop heartbeat only every SEC seconds (default 60)",
    )
    args = ap.parse_args()

    mode = "auto" if args.auto else "judge"

    if args.serve is not None:
        _serve_loop(args.serve, mode=mode)
        return 0

    if args.heartbeat_loop is not None:
        sec = max(15, int(args.heartbeat_loop))
        _write_mode("heartbeat")
        print(f"heartbeat-loop every {sec}s → {INBOX}", flush=True)
        while True:
            ok = _heartbeat()
            print(f"{time.strftime('%H:%M:%S')} heartbeat={'ok' if ok else 'fail'}",
                  flush=True)
            time.sleep(sec)

    if args.judge and not args.auto:
        mode = "judge"
    return run_once(mode=mode, heartbeat_only=args.heartbeat_only)


if __name__ == "__main__":
    from src.cli.legacy import warn_legacy_script
    warn_legacy_script("argus bridge")
    raise SystemExit(main())
