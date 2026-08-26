"""클코 예산 계기판 (세션/주간 · 카운트다운 · 추정 %)."""
from src.claude_local_usage import parse_claude_line, tokens_since
from src.ops_budget import (
    budget_gauge, classify_quota_kind, estimate_session_pct,
    format_budget_push_line, format_countdown,
)


def test_classify_session_weekly():
    assert classify_quota_kind("session limit resets 5:20am") == "session"
    assert classify_quota_kind("weekly limit resets 6pm") == "weekly"
    assert classify_quota_kind("Usage limit reached") == "unknown"
    assert classify_quota_kind("network timeout") is None


def test_format_countdown():
    now = 1_000_000.0
    assert format_countdown(None, now=now) == "—"
    assert format_countdown(now - 10, now=now) == "리셋 시각 지남"
    assert "분" in format_countdown(now + 45 * 60, now=now)
    assert "시간" in format_countdown(now + 3 * 3600 + 10 * 60, now=now)


def test_budget_gauge_ok():
    g = budget_gauge({"mode": "ok"}, now=1_000_000.0,
                     usage_fn=lambda **kw: {"used": 0, "n_events": 0})
    assert g["status"] == "ok"
    assert g["quota_kind"] is None
    assert g["session_est"]["pct"] == 0.0


def test_budget_gauge_bridge_weekly():
    now = 1_000_000.0
    g = budget_gauge({
        "mode": "bridge",
        "quota_kind": "weekly",
        "reset_at": now + 7200,
        "last_error": "weekly limit resets 6pm",
    }, now=now, usage_fn=lambda **kw: {"used": 22_000, "n_events": 3})
    assert g["quota_kind"] == "weekly"
    assert g["quota_label"] == "주간 한도"
    assert "브릿지 운용" in g["line"]
    assert "시간" in g["countdown"]
    assert g["session_est"]["pct"] == 50.0
    assert "추정 50%" in g["line"]
    assert "예산:" in format_budget_push_line(g)


def test_budget_gauge_circuit_from_error_text():
    now = 1_000_000.0
    g = budget_gauge({
        "mode": "circuit_open",
        "reason": "quota_no_bridge",
        "last_error": "session limit resets 5:20am",
        "reset_at": now + 1800,
    }, now=now, usage_fn=lambda **kw: {"used": 40_000, "n_events": 10})
    assert g["quota_kind"] == "session"
    assert "회로차단" in g["line"]
    assert g["session_est"]["pct"] == 90.9


def test_estimate_session_pct_cap_override():
    est = estimate_session_pct(
        now=1.0,
        cfg={"agents": {"claude_budget": {"session_token_cap": 10_000}}},
        usage_fn=lambda **kw: {"used": 2500, "n_events": 1},
    )
    assert est["pct"] == 25.0
    assert est["cap"] == 10_000
    assert "추정 25%" in est["label"]


def test_parse_claude_line_and_tokens_since(tmp_path):
    import json
    from datetime import datetime, timezone
    proj = tmp_path / "proj"
    proj.mkdir()
    ts = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
    line = json.dumps({
        "type": "assistant",
        "timestamp": ts.isoformat(),
        "requestId": "r1",
        "message": {
            "id": "m1",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_creation_input_tokens": 10,
                "cache_read_input_tokens": 5,
            },
        },
    })
    (proj / "s.jsonl").write_text(line + "\n", encoding="utf-8")
    ent = parse_claude_line(line)
    assert ent is not None and ent.total == 165
    out = tokens_since(since_ts=ts.timestamp() - 10, roots=[proj])
    assert out["used"] == 165
    assert out["n_events"] == 1
