"""클코 예산 계기판 (세션/주간 · 카운트다운 · 최근 창 사용량)."""
from src.claude_local_usage import parse_claude_line, tokens_since
from src.ops_budget import (
    budget_gauge, classify_quota_kind, format_budget_push_line,
    format_countdown, session_usage,
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
    assert g["session_usage"]["used"] == 0
    assert "한도 정상" in g["line"]


def test_budget_gauge_bridge_weekly():
    now = 1_000_000.0
    g = budget_gauge({
        "mode": "bridge",
        "quota_kind": "weekly",
        "reset_at": now + 7200,
        "last_error": "weekly limit resets 6pm",
    }, now=now, usage_fn=lambda **kw: {
        "used": 22_000, "output": 1_500, "n_events": 3})
    assert g["quota_kind"] == "weekly"
    assert g["quota_label"] == "주간 한도"
    assert "브릿지 운용" in g["line"]
    assert "시간" in g["countdown"]
    assert g["session_usage"]["output"] == 1_500
    assert "출력 1.5K" in g["line"] and "3회" in g["line"]
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
    assert "%" not in g["line"]


def test_session_usage_reports_tokens_without_pct():
    """고정 cap 으로 나눈 추정 % 는 늘 >100% 였다 — 사용량만 보고한다(구 cap 설정은 무시)."""
    est = session_usage(
        now=1.0,
        cfg={"agents": {"claude_budget": {"session_token_cap": 1_000}}},
        usage_fn=lambda **kw: {"used": 2_030_000, "output": 66_864,
                               "cache_read": 1_290_557, "n_events": 32},
    )
    assert est["ok"] is True
    assert est["output"] == 66_864 and est["cache_read"] == 1_290_557
    assert "pct" not in est and "over_cap" not in est
    assert est["label"] == "최근 5h 출력 66.9K · 캐시 포함 2.03M · 32회"


def test_session_usage_disabled_and_error():
    off = {"agents": {"claude_budget": {"enabled": False}}}
    assert session_usage(now=1.0, cfg=off) is None

    def boom(**kw):
        raise OSError("no home")
    est = session_usage(now=1.0, usage_fn=boom)
    assert est["ok"] is False and "label" not in est


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
    assert out["output"] == 50 and out["cache_read"] == 5
    assert out["n_events"] == 1
