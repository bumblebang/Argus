"""LLM 정지 경보 — 모드 ok 인 채 조용히 실패(2026-10 CLI 경로 사고). 성공 기록으로만 풀린다."""
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import alert_check as ac  # noqa: E402
from src.ops_playbook import actions_for  # noqa: E402

NOW = 1_800_000_000.0


def _db(tmp_path, monkeypatch, rows):
    db = tmp_path / "bot.db"
    con = sqlite3.connect(db)
    con.execute("drop table if exists events")
    con.execute("create table events(ts real, kind text, symbol text, payload text)")
    con.executemany("insert into events values(?,?,?,?)",
                    [(ts, kind, None, json.dumps(p) if p is not None else None)
                     for ts, kind, p in rows])
    con.commit()
    con.close()
    monkeypatch.setattr(ac, "DB", db)


def _err(ts):
    return (ts, "error", {"where": "brain", "err": "실행 파일을 찾을 수 없습니다"})


def _reasons(**kw):
    return ac.evaluate(NOW, hb_age=2.0, llm_health=ac._llm_health(), **kw)


def _athena(ts, market, done, failed):
    return (ts, "athena_done", {"market": market, "targets": done + failed,
                                "done": done, "failed": failed})


def test_brain_stall_fires_with_mode_ok(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch, [(NOW - 900, "brain_done", {}),
                                _err(NOW - 300), _err(NOW - 200), _err(NOW - 100)])
    r = _reasons(brain_mode="ok")
    assert len(r) == 1 and ac.LLM_STALL_TAG in r[0] and "뇌" in r[0]


def test_brain_stall_below_threshold_silent(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch, [(NOW - 900, "brain_done", {}),
                                _err(NOW - 200), _err(NOW - 100)])
    assert _reasons(brain_mode="ok") == []


def test_brain_success_clears_old_errors(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch, [_err(NOW - 900), _err(NOW - 800), _err(NOW - 700),
                                (NOW - 100, "brain_done", {})])
    assert _reasons(brain_mode="ok") == []


def test_brain_stall_persists_with_old_errors(tmp_path, monkeypatch):
    """슬라이딩 창이 아니다 — 시간이 지나도 성공 전엔 풀리지 않는다."""
    old = NOW - 30 * 86400
    _db(tmp_path, monkeypatch, [_err(old), _err(old + 1), _err(old + 2)])
    assert any(ac.LLM_STALL_TAG in r for r in _reasons(brain_mode="ok"))


def test_reason_text_stable_as_failures_grow(tmp_path, monkeypatch):
    """문구가 건수로 바뀌면 사이클마다 재푸시된다."""
    _db(tmp_path, monkeypatch, [_err(NOW - 300), _err(NOW - 200), _err(NOW - 100)])
    first = _reasons(brain_mode="ok")
    _db(tmp_path, monkeypatch, [_err(NOW - 300), _err(NOW - 200), _err(NOW - 100),
                                _err(NOW - 50), _err(NOW - 10)])
    assert _reasons(brain_mode="ok") == first


def test_mode_alert_not_doubled_by_brain_stall(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch, [_err(NOW - 300), _err(NOW - 200), _err(NOW - 100)])
    r = _reasons(brain_mode="auth_needed")
    assert not any(ac.LLM_STALL_TAG in x for x in r)
    assert any("인증" in x for x in r)


def test_athena_all_failed_per_market(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch, [_athena(NOW - 500, "US", 30, 0),
                                _athena(NOW - 100, "KR", 0, 30)])
    r = _reasons(brain_mode="ok")
    assert len(r) == 1 and "Athena KR" in r[0] and "30건" in r[0]


def test_athena_next_success_clears_only_that_market(tmp_path, monkeypatch):
    """서로 다른 때(창 밖) 실패 — 공통 장애가 아니므로 KR 성공이 US 를 지우지 않는다."""
    _db(tmp_path, monkeypatch, [_athena(NOW - 7 * 3600 - 500, "US", 0, 30),
                                _athena(NOW - 400, "KR", 0, 30),
                                _athena(NOW - 100, "KR", 12, 18)])
    r = _reasons(brain_mode="ok")
    assert len(r) == 1 and "Athena US" in r[0]


def test_empty_run_is_not_a_failure(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch, [_athena(NOW - 100, "KR", 0, 0),
                                (NOW - 50, "value_scan_done", {"done": 0, "failed": 0,
                                                               "aborted": "limit"})])
    assert _reasons(brain_mode="ok") == []


def test_value_scan_all_failed_then_cleared(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch, [(NOW - 100, "value_scan_done", {"done": 0, "failed": 40})])
    r = _reasons(brain_mode="ok")
    assert len(r) == 1 and "밸류 스캔" in r[0]
    _db(tmp_path, monkeypatch, [(NOW - 100, "value_scan_done", {"done": 0, "failed": 40}),
                                (NOW - 50, "value_scan_done", {"done": 5, "failed": 0})])
    assert _reasons(brain_mode="ok") == []


def test_missing_db_and_query_errors_are_silent(tmp_path, monkeypatch):
    monkeypatch.setattr(ac, "DB", tmp_path / "없음.db")
    assert ac._llm_health() == {}
    db = tmp_path / "빈.db"
    sqlite3.connect(db).close()
    monkeypatch.setattr(ac, "DB", db)
    assert ac._llm_health() == {}


def test_stall_reason_not_on_5min_unresolved_track():
    """5분 재푸시(미해소) 트랙에 끼면 스팸 — 12시간 트랙만 탄다."""
    r = [f"{ac.LLM_STALL_TAG}: Athena KR — 10/30 05:35 KST 실행 30건 전부 실패"]
    assert ac.unresolved_renotify_due(r, NOW) == (False, False, False)


def test_push_title_and_playbook():
    r = [f"{ac.LLM_STALL_TAG}: 뇌 — 10/22 20:30 KST 부터 성공 없음"]
    assert ac._push_title_for(r, "ok") == "Argus LLM"
    assert any("which_claude" in a for a in actions_for(r, brain_mode="ok"))


def _run_main(tmp_path, monkeypatch, now, rows):
    """main() 한 주기 — 파일 경로를 tmp 로 돌리고 푸시 횟수를 잰다."""
    _db(tmp_path, monkeypatch, rows)
    pushes = []
    monkeypatch.setattr(ac, "ALERT", tmp_path / "ALERT.json")
    monkeypatch.setattr(ac, "ALERTS_LOG", tmp_path / "alerts.jsonl")
    monkeypatch.setattr(ac, "_PUSH_STATE", tmp_path / "push.json")
    monkeypatch.setattr(ac, "_read_heartbeat", lambda n: (2.0, {}))
    monkeypatch.setattr(ac, "_load_brain_mode", lambda: {"mode": "ok"})
    monkeypatch.setattr(ac, "_auth_expired_recent", lambda n: False)
    monkeypatch.setattr(ac, "crosscheck_reasons", lambda n: [])
    monkeypatch.setattr(ac, "is_bridge_armed", lambda *a, **k: True)
    monkeypatch.setattr(ac, "_ntfy_topic", lambda: "t")
    monkeypatch.setattr(ac, "_push_live_orders", lambda n: None)
    monkeypatch.setattr(ac, "_push", lambda t, m: pushes.append((t, m)) or True)
    monkeypatch.setattr(ac.time, "time", lambda: now)
    ac.main()
    return pushes


def test_main_pushes_once_then_renotifies_after_12h(tmp_path, monkeypatch):
    rows = [_err(NOW - 300), _err(NOW - 200), _err(NOW - 100)]
    assert len(_run_main(tmp_path, monkeypatch, NOW, rows)) == 1
    assert _run_main(tmp_path, monkeypatch, NOW + 300, rows) == []
    assert _run_main(tmp_path, monkeypatch, NOW + 11 * 3600, rows) == []
    p = _run_main(tmp_path, monkeypatch, NOW + 12 * 3600 + 1, rows)
    assert len(p) == 1 and p[0][0] == "Argus LLM"


# ── 공통 원인 복구 — 휴장 시장 경보가 원인 복구 뒤에도 며칠 남던 문제(2026-11-06) ──

H = 3600


def _value(ts, done, failed):
    return (ts, "value_scan_done", {"done": done, "failed": failed})


def test_closed_market_cleared_when_shared_outage_recovered(tmp_path, monkeypatch):
    """11-06 재현: KR 05:35 전부 실패 + 밸류 07:49 전부 실패 → 밸류 15:49 성공. KR 은 주말 내내 창 없음."""
    _db(tmp_path, monkeypatch, [_athena(NOW - 40 * H, "KR", 0, 30),
                                _value(NOW - 38 * H, 0, 15),
                                _value(NOW - 30 * H, 15, 0)])
    assert _reasons(brain_mode="ok") == []
    assert ac._llm_health()["recovered"] == ["Athena KR"]


def test_brain_outage_recovery_clears_batch(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch, [_err(NOW - 41 * H), _err(NOW - 40 * H), _err(NOW - 39 * H),
                                _athena(NOW - 40 * H, "KR", 0, 30),
                                (NOW - 20 * H, "brain_done", {})])
    assert _reasons(brain_mode="ok") == []


def test_isolated_market_failure_stays(tmp_path, monkeypatch):
    """다른 소비자는 멀쩡 — 시장 단독 장애(데이터 원천 등)는 해제하지 않는다."""
    _db(tmp_path, monkeypatch, [_value(NOW - 50 * H, 10, 0),
                                _athena(NOW - 40 * H, "KR", 0, 30),
                                _athena(NOW - 30 * H, "US", 30, 0),
                                _value(NOW - 20 * H, 10, 0),
                                (NOW - 10 * H, "brain_done", {})])
    r = _reasons(brain_mode="ok")
    assert len(r) == 1 and "Athena KR" in r[0]


def test_sporadic_brain_errors_do_not_clear(tmp_path, monkeypatch):
    """뇌 에러 1~2건은 흔하다 — 공통 장애로 치지 않는다."""
    _db(tmp_path, monkeypatch, [_err(NOW - 40 * H), _err(NOW - 39 * H),
                                _athena(NOW - 40 * H, "KR", 0, 30),
                                (NOW - 20 * H, "brain_done", {})])
    assert any("Athena KR" in x for x in _reasons(brain_mode="ok"))


def test_concurrent_failure_outside_window_stays(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch, [_value(NOW - 50 * H, 0, 15),
                                _athena(NOW - 40 * H, "KR", 0, 30),
                                _value(NOW - 20 * H, 15, 0)])
    assert any("Athena KR" in x for x in _reasons(brain_mode="ok"))


def test_shared_outage_not_yet_recovered_keeps_both(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch, [_athena(NOW - 4 * H, "KR", 0, 30),
                                _value(NOW - 2 * H, 0, 15)])
    r = _reasons(brain_mode="ok")
    assert len(r) == 2


def test_other_market_recovery_clears_sibling(tmp_path, monkeypatch):
    """US·KR 이 같은 장애로 함께 실패 → US 가 다음 창에서 성공하면 KR 도 해제."""
    _db(tmp_path, monkeypatch, [_athena(NOW - 50 * H, "US", 0, 30),
                                _athena(NOW - 46 * H, "KR", 0, 30),
                                _athena(NOW - 26 * H, "US", 30, 0)])
    assert _reasons(brain_mode="ok") == []
