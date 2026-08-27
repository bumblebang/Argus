"""경보 판정 — 뇌 모드 전이 기반(슬라이딩 창 플리핑 제거)."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import alert_check as ac  # noqa: E402

NOW = 1_000_000.0


def test_healthy_ok_mode():
    assert ac.evaluate(NOW, hb_age=2.0, market_open=True,
                       brain_mode="ok") == []


def test_heartbeat_missing():
    r = ac.evaluate(NOW, hb_age=None, market_open=True, brain_mode="ok")
    assert any("하트비트 없음" in x for x in r)


def test_heartbeat_stale():
    r = ac.evaluate(NOW, hb_age=400, market_open=False, brain_mode="ok")
    assert any("하트비트 끊김" in x for x in r)


def test_heartbeat_path_prefers_state_layout(tmp_path, monkeypatch):
    """레거시가 stale 여도 state/ 실파일을 본다(오탐 하트비트 끊김 방지)."""
    import time as _t
    root = tmp_path
    legacy = root / "data" / "watch.heartbeat"
    state = root / "data" / "state" / "watch.heartbeat"
    legacy.parent.mkdir(parents=True)
    state.parent.mkdir(parents=True)
    now = _t.time()
    legacy.write_text(json.dumps({"ts": now - 9999}), encoding="utf-8")
    state.write_text(json.dumps({"ts": now - 5}), encoding="utf-8")
    monkeypatch.setattr(ac._paths, "ROOT", root)
    age = ac._read_heartbeat_age(now)
    assert age is not None and age < 60


def test_sliding_window_ignored():
    """옛 20분 에러/40분 무완주는 더 이상 경보를 만들지 않는다."""
    r = ac.evaluate(NOW, hb_age=2.0, market_open=True,
                    brain_errors_recent=9, last_brain_done_age=99999,
                    brain_mode="ok")
    assert r == []
    assert not any("브레인 에러" in x for x in r)
    assert not any("무완주" in x for x in r)


def test_circuit_open_alerts_even_when_market_closed():
    """휴장이라도 회로차단은 유지 — '정상 복구' 거짓 신호 방지."""
    r = ac.evaluate(NOW, hb_age=2.0, market_open=False,
                    brain_mode="circuit_open", reset_at=NOW + 3600,
                    mode_reason="quota_no_bridge")
    assert any("회로차단" in x for x in r)


def test_bridge_mode_alert():
    r = ac.evaluate(NOW, hb_age=2.0, market_open=True,
                    brain_mode="bridge", reset_at=NOW + 3600,
                    quota_kind="weekly")
    assert any("브릿지 운용" in x for x in r)
    assert any("주간" in x for x in r)


def test_auth_mode_alert():
    r = ac.evaluate(NOW, hb_age=2.0, market_open=False,
                    brain_mode="auth_needed")
    assert any("인증 만료" in x for x in r)
    assert any("claude_login" in x for x in r)


def test_auth_expired_flag_when_mode_ok():
    r = ac.evaluate(NOW, hb_age=2.0, market_open=False,
                    brain_mode="ok", auth_expired=True)
    assert any("인증 만료" in x for x in r)


def test_mode_reasons_helper():
    assert ac.mode_reasons("ok") == []
    assert "브릿지" in ac.mode_reasons("bridge", reset_at=NOW)[0]
    assert "회로차단" in ac.mode_reasons("circuit_open", reset_at=NOW)[0]
    assert "세션" in ac.mode_reasons(
        "bridge", reset_at=NOW + 600, quota_kind="session")[0]


def test_인증에러_문구_판별(tmp_path, monkeypatch):
    import sqlite3, json as _json
    db = tmp_path / "bot.db"
    con = sqlite3.connect(db)
    con.execute("create table events(ts real, kind text, symbol text, payload text)")
    con.execute("insert into events values(?,?,?,?)",
                (NOW - 60, "error", None,
                 _json.dumps({"where": "brain",
                              "err": "API Error: Access token ... has expired"})))
    con.commit(); con.close()
    monkeypatch.setattr(ac, "DB", db)
    assert ac._auth_expired_recent(NOW) is True

    db2 = tmp_path / "bot2.db"
    con = sqlite3.connect(db2)
    con.execute("create table events(ts real, kind text, symbol text, payload text)")
    con.execute("insert into events values(?,?,?,?)",
                (NOW - 60, "error", None,
                 _json.dumps({"where": "brain", "err": "You've hit your session limit"})))
    con.commit(); con.close()
    monkeypatch.setattr(ac, "DB", db2)
    assert ac._auth_expired_recent(NOW) is False


def test_bridge_armed_true_no_disarm_reason():
    r = ac.evaluate(NOW, hb_age=2.0, market_open=True,
                    brain_mode="bridge", reset_at=NOW + 3600,
                    bridge_armed=True)
    assert any("브릿지 운용" in x for x in r)
    assert not any("미무장" in x for x in r)


def test_bridge_armed_false_adds_disarm_reason():
    r = ac.evaluate(NOW, hb_age=2.0, market_open=True,
                    brain_mode="bridge", reset_at=NOW + 3600,
                    bridge_armed=False)
    assert any("브릿지 운용" in x for x in r)
    assert any("미무장" in x for x in r)
    assert any("circuit" in x for x in r)


def test_bridge_armed_none_skips_disarm_check():
    """하위호환: bridge_armed 미전달 시 미무장 사유 없음."""
    r = ac.evaluate(NOW, hb_age=2.0, market_open=True,
                    brain_mode="bridge", reset_at=NOW + 3600)
    assert any("브릿지 운용" in x for x in r)
    assert not any("미무장" in x for x in r)


def test_actions_for_heartbeat_auth_circuit():
    from src.ops_playbook import actions_for, format_push_body
    hb = actions_for(["데몬 하트비트 끊김 400s (>300s) — 죽음/행 의심"],
                     brain_mode="ok")
    assert hb and any("데몬" in a or "heartbeat" in a.lower() for a in hb)
    auth = actions_for(["claude 인증 만료 — 뇌 전면 정지"], brain_mode="auth_needed")
    assert auth and any("claude_login" in a for a in auth)
    circuit = actions_for(
        ["뇌 회로차단 — 브릿지 미준비, 리셋 ?까지 정지"],
        brain_mode="circuit_open")
    assert circuit and any("serve" in a or "브릿지" in a for a in circuit)
    body = format_push_body(
        ["브릿지 모드인데 미무장 — bridge.heartbeat 만료/없음 → circuit 위험"],
        actions_for(
            ["브릿지 모드인데 미무장 — bridge.heartbeat 만료/없음 → circuit 위험"],
            brain_mode="bridge"))
    assert "다음:" in body
    assert "미무장" in body


def test_ops_card_html_renders():
    import dashboard as dash
    html = dash._ops_card_html({
        "brain_mode": "bridge",
        "bridge_armed": False,
        "ops_reasons": ["브릿지 모드인데 미무장 — bridge.heartbeat 만료/없음 → circuit 위험"],
        "ops_actions": ["브릿지 재무장: argus bridge --serve 60"],
        "budget": {
            "line": "세션 예산 · 추정 50% (22.0K / 44.0K, 최근 5h)",
            "quota_label": "주간 한도",
            "countdown": "2시간",
            "session_est": {"pct": 50.0, "used": 22000, "cap": 44000, "window_sec": 18000},
        },
        "alert": {},
    })
    assert "Ops" in html
    assert "미무장" in html
    assert "다음" in html
    assert "예산" in html
    assert "주간" in html
