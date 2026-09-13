"""events 보존 prune + 종목 스코프 이벤트 조회.

`athena_queue` 처럼 틱마다 쌓이는 관측 이벤트가 bot.db 를 비대화시키고
(2026-09-13 실측: events 68만행 중 athena_queue 39만행), 행 기준 limit 조회는
대상 종목을 창에서 밀어내 쿨다운을 무력화했다.
"""
from src.engine.store import Store


def _ev(store, kind, symbol, ts):
    store.conn.execute(
        "INSERT INTO events(ts, kind, symbol, payload) VALUES(?,?,?,?)",
        (ts, kind, symbol, "{}"))
    store.conn.commit()


def test_prune_events_only_targets_observational_kinds(tmp_path):
    """만료돼도 원장·귀속이 읽는 kind 는 남긴다."""
    s = Store(tmp_path / "t.db")
    now = 1_700_000_000.0
    old = now - 30 * 86400
    _ev(s, "athena_queue", "A", old)
    _ev(s, "athena_scan", "A", old)
    _ev(s, "live_order", "A", old)          # 원장 — 대상 아님
    _ev(s, "decision", "A", old)            # 귀속 — 대상 아님
    _ev(s, "athena_queue", "B", now - 60)   # 보존기간 내

    info = s.prune_events(older_than_sec=7 * 86400, now=now, batch_limit=100)
    assert info["done"] is True
    assert info["deleted"] == 2
    left = sorted(r[0] for r in s.conn.execute("SELECT kind FROM events"))
    assert left == ["athena_queue", "decision", "live_order"]


def test_prune_events_batches_until_done(tmp_path):
    s = Store(tmp_path / "t.db")
    now = 1_700_000_000.0
    for i in range(5):
        _ev(s, "athena_queue", f"S{i}", now - 30 * 86400)
    _ev(s, "athena_queue", "KEEP", now - 10)

    first = s.prune_events(older_than_sec=7 * 86400, now=now,
                           batch_limit=2, max_batches=1)
    assert first["deleted"] == 2 and first["done"] is False
    while not s.prune_events(older_than_sec=7 * 86400, now=now,
                             batch_limit=2, max_batches=1)["done"]:
        pass
    left = [r[0] for r in s.conn.execute("SELECT symbol FROM events")]
    assert left == ["KEEP"]


def test_prune_events_custom_kinds(tmp_path):
    s = Store(tmp_path / "t.db")
    now = 1_700_000_000.0
    _ev(s, "precision", "A", now - 30 * 86400)
    _ev(s, "athena_queue", "A", now - 30 * 86400)

    info = s.prune_events(kinds=["precision"], older_than_sec=7 * 86400, now=now)
    assert info["deleted"] == 1 and info["kinds"] == ["precision"]
    assert [r[0] for r in s.conn.execute("SELECT kind FROM events")] == ["athena_queue"]


def test_prune_events_readonly_and_empty_kinds_noop(tmp_path):
    path = tmp_path / "t.db"
    s = Store(path)
    _ev(s, "athena_queue", "A", 1.0)
    assert s.prune_events(kinds=[], older_than_sec=1, now=10_000)["deleted"] == 0
    ro = Store(path, readonly=True)
    info = ro.prune_events(older_than_sec=1, now=10_000)
    assert info["deleted"] == 0 and info["done"] is True


def test_recent_events_symbol_scope_survives_busy_kind(tmp_path):
    """행 limit 을 kind 전체에 걸면 대상 종목이 창에서 밀려난다(쿨다운 무력화)."""
    s = Store(tmp_path / "t.db")
    now = 1_700_000_000.0
    _ev(s, "athena_queue", "TARGET", now - 100)
    for i in range(200):
        _ev(s, "athena_queue", f"NOISE{i}", now - 50)

    assert s.recent_events("athena_queue", now - 3600, limit=30, symbol="TARGET")
    assert not any(r["symbol"] == "TARGET"
                   for r in s.recent_events("athena_queue", now - 3600, limit=30))


def test_recent_events_by_symbol_returns_latest_per_symbol(tmp_path):
    s = Store(tmp_path / "t.db")
    now = 1_700_000_000.0
    _ev(s, "athena_queue", "A", now - 300)
    for i in range(50):
        _ev(s, "athena_queue", "SPAM", now - 200 + i)
    _ev(s, "athena_queue", "A", now - 100)      # A 최신
    _ev(s, "athena_queue", None, now - 90)      # symbol 없음 — 제외

    rows = s.recent_events_by_symbol("athena_queue", now - 3600, limit=100)
    got = {r["symbol"]: r["ts"] for r in rows}
    assert set(got) == {"A", "SPAM"}
    assert got["A"] == now - 100
    assert got["SPAM"] == now - 151
    assert [r["symbol"] for r in rows] == ["A", "SPAM"]      # 최신순
    assert len(s.recent_events_by_symbol("athena_queue", now - 3600, limit=1)) == 1
