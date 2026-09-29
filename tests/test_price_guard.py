"""PriceGuard — lastPrice 이상 틱 필터 + gateway 배선 + 청산 재시도 백오프.

재현 사고: 2026-09-23 06:08 KST US 애프터에 /prices lastPrice 가 HPE 61.1→21.58,
ROIV 38.25→29.12 로 찍혀 가짜 stop_hit 청산.
"""
from src.engine.execution import ExitExecutor
from src.engine.gateway import TossGateway
from src.engine.price_guard import PriceGuard
from src.engine.store import Store


class _Clock:
    def __init__(self, t=1_000.0):
        self.t = t

    def __call__(self):
        return self.t


class _Quotes:
    def __init__(self, quote):
        self.quote = quote
        self.calls = 0

    def __call__(self, symbol):
        self.calls += 1
        return self.quote


def _guard(quote, clock=None, events=None, **kw):
    q = _Quotes(quote)
    g = PriceGuard(q, clock=clock or _Clock(),
                   on_event=(lambda k, p: events.append(p)) if events is not None else None,
                   **kw)
    return g, q


def test_normal_ticks_pass_without_quote_calls():
    g, q = _guard((99.0, 101.0))
    assert g.filter("A", 100.0) == (100.0, None)       # ref 없음 → 채택
    assert g.filter("A", 104.0) == (104.0, None)       # 4% — 밴드 안
    assert q.calls == 0


def test_bad_print_rejected_by_quote_hpe_case():
    events = []
    g, q = _guard((61.05, 61.15), events=events)
    g.filter("HPE", 61.1)
    px, verdict = g.filter("HPE", 21.58)
    assert verdict == "rejected"
    assert abs(px - 61.1) < 1e-9                       # 호가 중간값
    # 같은 오류가가 매 틱 반복돼도 recheck 창 안에선 호가 재조회·이벤트 없음
    for _ in range(5):
        assert g.filter("HPE", 21.58)[1] == "rejected"
    assert q.calls == 1
    assert len(events) == 1 and events[0]["raw"] == 21.58
    # 정상가 복귀는 그대로 채택
    assert g.filter("HPE", 61.1) == (61.1, None)


def test_persistent_bad_print_requeried_after_recheck_roiv_case():
    clock = _Clock()
    g, q = _guard((37.98, 38.97), clock=clock, recheck_sec=30)
    g.filter("ROIV", 38.25)
    for _ in range(40):                                 # 40분간 29.12 고정
        clock.t += 60
        px, verdict = g.filter("ROIV", 29.12)
        assert verdict == "rejected" and px > 37
    assert q.calls == 40                                # 재확인은 recheck 주기마다 1회


def test_real_move_confirmed_by_quote():
    g, q = _guard((84.9, 85.1))
    g.filter("A", 100.0)
    assert g.filter("A", 85.0) == (85.0, "confirmed")
    assert g.filter("A", 84.5) == (84.5, None)          # 새 기준가 대비 밴드 안


def test_wide_spread_real_print_confirmed():
    """시간외 넓은 스프레드(bid 90 / ask 100)에서 bid 근처 체결은 진짜 — 중간값과 5% 차이."""
    g, q = _guard((90.0, 100.0))
    g.filter("A", 105.0)
    assert g.filter("A", 90.5) == (90.5, "confirmed")


def test_no_quote_holds_then_accepts_after_confirm_sec():
    clock = _Clock()
    g, q = _guard(None, clock=clock, confirm_sec=180, recheck_sec=30)
    g.filter("A", 100.0)
    assert g.filter("A", 70.0) == (100.0, "held")       # 판단 근거 없음 → 직전가 유지
    clock.t += 100
    assert g.filter("A", 70.0) == (100.0, "held")
    clock.t += 100
    assert g.filter("A", 70.0) == (70.0, "timeout")     # 진짜 급락을 영영 막지 않는다
    assert g.filter("A", 69.0) == (69.0, None)


def test_stale_reference_accepts_gap_when_no_quote():
    """밤새 안 본 종목의 개장 갭 — 호가를 못 구하면 낡은 기준으로 붙잡지 않는다."""
    clock = _Clock()
    g, q = _guard(None, clock=clock, ref_max_age_sec=1800)
    g.filter("A", 100.0)
    clock.t += 3600
    assert g.filter("A", 80.0) == (80.0, "timeout")
    assert q.calls == 1
    assert g.filter("A", 79.0) == (79.0, None)


def test_stale_reference_still_checks_quote_after_poll_gap():
    """30분 폴링 공백 뒤 첫 틱도 오류가면 기각 — 예전엔 기준 없이 그대로 채택(가짜 손절)."""
    clock = _Clock()
    g, q = _guard((37.98, 38.97), clock=clock, ref_max_age_sec=1800)
    g.filter("ROIV", 38.25)
    clock.t += 3600
    px, verdict = g.filter("ROIV", 29.12)
    assert verdict == "rejected" and px > 38
    real, _ = _guard((79.9, 80.1), clock=clock, ref_max_age_sec=1800)
    real.filter("A", 100.0)
    clock.t += 3600
    assert real.filter("A", 80.0) == (80.0, "confirmed")


def test_quote_error_treated_as_unavailable():
    def boom(symbol):
        raise RuntimeError("down")
    g = PriceGuard(boom, clock=_Clock())
    g.filter("A", 100.0)
    assert g.filter("A", 50.0) == (100.0, "held")


def test_quote_budget_caps_burst_then_catches_up():
    """오류가가 여러 종목에 동시에 뜨면 초당 상한만큼만 호가 조회, 나머지는 직전가 유지."""
    clock = _Clock()
    g, q = _guard((49.9, 50.1), clock=clock, max_quotes_per_sec=3)
    syms = [f"S{i}" for i in range(8)]
    for s in syms:
        g.filter(s, 50.0)
    first = [g.filter(s, 20.0) for s in syms]
    assert q.calls == 3
    assert [v for _, v in first].count("rejected") == 3
    assert all(px == 50.0 for px, _ in first)          # 미확인분도 오류가를 쓰지 않는다
    clock.t += 1
    second = [g.filter(s, 20.0) for s in syms]
    assert q.calls == 6
    assert [v for _, v in second].count("rejected") == 6


def test_from_config_disabled_returns_none():
    assert PriceGuard.from_config({"enabled": False}, lambda s: None) is None
    g = PriceGuard.from_config({"max_jump_pct": 0.2}, lambda s: None)
    assert g is not None and g.max_jump_pct == 0.2


# ── gateway 배선 ─────────────────────────────────────────

class _SeqClient:
    def __init__(self, prices, book):
        self.prices = list(prices)
        self.book = book

    def get_prices(self, symbols):
        p = self.prices.pop(0)
        return [{"symbol": s, "lastPrice": str(p)} for s in symbols]

    def _request(self, name, params=None, **kw):
        assert name == "orderbook"
        self.request_kw = kw
        return self.book


def test_gateway_sanitizes_price_keeps_raw_payload(tmp_path):
    store = Store(tmp_path / "t.db")
    client = _SeqClient(["38.25", "29.12"],
                        {"bids": [{"price": "37.98", "volume": "10"}],
                         "asks": [{"price": "38.97", "volume": "10"}]})
    gw = TossGateway(client, store=store)
    gw.price_guard = PriceGuard(gw.best_quote,
                                on_event=lambda k, p: store.log_event(k, p["symbol"], p))
    gw.poll_prices(["ROIV"])
    row = gw.poll_prices(["ROIV"])[0]
    assert row["price_guard"] == "rejected"
    assert row["raw_price"] == 29.12
    assert 38.4 < row["price"] < 38.5
    assert row["payload"]["lastPrice"] == "29.12"          # 원시값 감사용 보존
    snap = store.conn.execute(
        "SELECT price FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()
    assert 38.4 < snap["price"] < 38.5
    kinds = [r["kind"] for r in store.conn.execute("SELECT kind FROM events")]
    assert "price_guard" in kinds


def test_best_quote_handles_empty_book():
    gw = TossGateway(_SeqClient([], {"bids": [], "asks": []}))
    assert gw.best_quote("A") is None


# ── 청산 재시도 백오프 ─────────────────────────────────────

class _Pos:
    qty = 3


class _Res:
    def __init__(self, ok, why=None):
        self.ok, self.reject_reason = ok, why
        self.avg_price, self.filled_qty, self.order_qty = 38.0, 3, 3
        self.status, self.partial, self.order_id, self.side = "FILLED", False, "X", "SELL"

    def __bool__(self):
        return self.ok


class _RejectingBroker:
    def __init__(self):
        self.calls = 0
        self.ok = False

    def position(self, symbol):
        return _Pos()

    def execute_with_mirror(self, order, **kw):
        self.calls += 1
        return _Res(self.ok, None if self.ok else "스프레드 초과")


class _Trig:
    kind = "stop_hit"


def test_exit_rejections_back_off(tmp_path):
    store = Store(tmp_path / "t.db")
    broker = _RejectingBroker()
    clock = _Clock()
    ex = ExitExecutor(broker, store, retry_base_sec=10, retry_max_sec=40,
                      urgent_retry_max_sec=40, clock=clock)
    attempts = []
    for _ in range(120):                                # 1초 틱 2분
        before = broker.calls
        ex("ROIV", "US", 38.0, _Trig())
        if broker.calls > before:
            attempts.append(clock.t - 1_000.0)
        clock.t += 1
    # 0, +10, +20(누적30), +40(70), +40(110) — 매초 120회 대신 5회
    assert attempts == [0, 10, 30, 70, 110]
    errs = store.conn.execute("SELECT COUNT(*) AS n FROM events WHERE kind='error'").fetchone()
    assert errs["n"] == 5


def test_exit_backoff_clears_on_success(tmp_path):
    store = Store(tmp_path / "t.db")
    broker = _RejectingBroker()
    clock = _Clock()
    ex = ExitExecutor(broker, store, retry_base_sec=10, clock=clock)
    assert ex("A", "US", 38.0, _Trig()) is False
    clock.t += 10
    broker.ok = True
    broker.position = lambda s: _Pos()
    assert ex("A", "US", 38.0, _Trig()) is True
    assert not [k for k in ex._retry if k[0] == "A"]


# ── 09-27 점검: 멈춘 호가·재기동·캐시·호가 장애 ─────────────────

def test_stale_book_does_not_block_real_crash_forever():
    """호가가 멈춰(시각이 의심 전) 진짜 급락을 계속 부정해도, 체결가가 움직이면
    confirm_sec 뒤 채택 — 예전엔 기각이 의심 시계를 매번 리셋해 손절이 영영 안 났다."""
    clock = _Clock()
    frozen_at = clock.t - 600
    g, q = _guard((99.0, 101.0, frozen_at), clock=clock, confirm_sec=180,
                  recheck_sec=30)
    g.filter("A", 100.0)
    verdicts = []
    for i in range(10):
        clock.t += 30
        verdicts.append(g.filter("A", 80.0 - i * 0.5))
    assert verdicts[0][1] == "rejected"
    assert ("stale_quote" in [v for _, v in verdicts])
    px, v = next((p, v) for p, v in verdicts if v == "stale_quote")
    assert px < 80.0
    # 채택 이후엔 새 기준가 — 평상 통과
    assert g.filter("A", 75.0)[1] is None


def test_constant_bad_print_rejected_even_with_frozen_book():
    """한 값에 고정된 오류가(ROIV 29.12 30분)는 호가가 멈춰 보여도 끝까지 기각."""
    clock = _Clock()
    g, q = _guard((37.98, 38.97, clock.t - 3600), clock=clock, recheck_sec=30)
    g.filter("ROIV", 38.25)
    for _ in range(40):
        clock.t += 60
        px, verdict = g.filter("ROIV", 29.12)
        assert verdict == "rejected" and px > 37


def test_live_book_keeps_rejecting_moving_prints():
    """호가 시각이 의심 뒤로 갱신되면 살아 있는 호가 — 체결가가 움직여도 기각."""
    clock = _Clock()

    def quote(sym):
        return (37.98, 38.97, clock.t - 1)

    g = PriceGuard(quote, clock=clock, recheck_sec=30)
    g.filter("ROIV", 38.25)
    for i in range(20):
        clock.t += 30
        assert g.filter("ROIV", 29.0 + i * 0.01)[1] == "rejected"


def test_restart_seeds_reference_from_snapshot():
    """재기동 직후 첫 틱이 오류가여도 직전 스냅샷 기준으로 호가 확인 → 기각."""
    clock = _Clock()
    g, q = _guard((37.98, 38.97), clock=clock)
    assert g.unseeded(["ROIV", "HPE"]) == ["ROIV", "HPE"]
    g.seed_refs({"ROIV": (38.25, clock.t - 120)}, tried=["ROIV", "HPE"])
    assert g.unseeded(["ROIV", "HPE"]) == []
    px, verdict = g.filter("ROIV", 29.12)
    assert verdict == "rejected" and px > 38
    assert g.filter("HPE", 21.58) == (21.58, None)       # 시드 없음 → 기존대로 채택


def test_gateway_seeds_guard_from_store_snapshots(tmp_path):
    store = Store(tmp_path / "t.db")
    store.record_snapshots([{"symbol": "ROIV", "price": 38.25}])
    client = _SeqClient(["29.12"],
                        {"bids": [{"price": "37.98", "volume": "10"}],
                         "asks": [{"price": "38.97", "volume": "10"}]})
    gw = TossGateway(client, store=store)
    gw.price_guard = PriceGuard(gw.best_quote)
    row = gw.poll_prices(["ROIV"])[0]                    # 재기동 후 첫 폴링
    assert row["price_guard"] == "rejected" and row["price"] > 38


def test_verdict_cache_invalidated_when_reference_moves():
    """(종목, 오류가) 판정 캐시가 기준가 이동 뒤에도 옛 중간값을 돌려주던 문제."""
    clock = _Clock()
    q = _Quotes((61.05, 61.15))
    g = PriceGuard(q, clock=clock, recheck_sec=30)
    g.filter("HPE", 61.1)
    assert g.filter("HPE", 21.58)[1] == "rejected"      # 캐시: 21.58 → 61.1
    clock.t += 1
    q.quote = (49.9, 50.1)
    assert g.filter("HPE", 50.0) == (50.0, "confirmed")  # 진짜 급락 확인 → 새 기준
    clock.t += 1
    px, verdict = g.filter("HPE", 21.58)                 # 같은 오류가 재등장
    assert verdict == "rejected" and abs(px - 50.0) < 1e-9
    assert q.calls == 3


def test_quote_failure_cooldown_and_breaker():
    """호가 장애 종목은 쿨다운, 연속 실패는 전 종목 조회 차단 — 매 틱 붙잡지 않는다."""
    clock = _Clock()
    calls = []

    def boom(sym):
        calls.append(sym)
        return None

    g = PriceGuard(boom, clock=clock, quote_fail_cooldown_sec=30, max_quotes_per_sec=10)
    for s in "ABCDE":
        g.filter(s, 100.0)
    for i in range(5):                                   # 매 틱 가격이 바뀌어 캐시 미스
        for s in "ABCDE":
            g.filter(s, 70.0 - i)
        clock.t += 1
    assert len(calls) == 3                               # 3회 연속 실패 → 차단
    clock.t += 30
    g.filter("A", 60.0)
    assert len(calls) == 4


def test_budget_starved_symbol_keeps_rejection():
    """호가 예산을 못 받는 틱에도 이번 의심 중 호가가 부정했으면 오류가를 채택하지 않는다."""
    clock = _Clock()
    g, q = _guard((49.9, 50.1), clock=clock, max_quotes_per_sec=1, confirm_sec=60,
                  recheck_sec=30)
    g.filter("A", 50.0)
    g.filter("B", 50.0)
    assert g.filter("A", 20.0)[1] == "rejected"
    clock.t += 61
    g.filter("B", 20.0)                                  # B 가 이번 초 예산 소진
    px, verdict = g.filter("A", 20.5)
    assert verdict == "rejected" and px == 50.0


def test_best_quote_single_attempt_short_timeout():
    client = _SeqClient([], {"bids": [{"price": "10", "volume": "1"}],
                             "asks": [{"price": "11", "volume": "1"}],
                             "timestamp": "2026-09-23T06:08:00+09:00"})
    gw = TossGateway(client, quote_timeout_sec=1.5)
    bid, ask, at = gw.best_quote("A")
    assert (bid, ask) == (10.0, 11.0) and at is not None
    assert client.request_kw == {"max_attempts": 1, "timeout": 1.5}


def test_exit_backoff_urgent_kind_capped_shorter(tmp_path):
    """익절 거부가 쌓은 streak 이 손절을 최대 120초 막지 않는다 — 손절은 30초 상한.

    백오프는 종목 단위(브로커는 kind 무관 같은 전량 매도)라 거부 직후엔 kind 를 바꿔도
    바로 다시 내지 않는다. 대신 긴급 kind 의 상한이 짧다.
    """
    store = Store(tmp_path / "t.db")
    broker = _RejectingBroker()
    clock = _Clock()
    ex = ExitExecutor(broker, store, retry_base_sec=10, retry_max_sec=120,
                      urgent_retry_max_sec=30, clock=clock)

    class _Target:
        kind = "target_hit"

    for _ in range(400):                                 # 익절만 계속 거부 → 상한 120초
        ex("A", "US", 38.0, _Target())
        clock.t += 1
    streak, last = ex._retry["A"]
    assert streak >= 4 and ex._delay("target_hit", streak) == 120
    clock.t = last + 31
    assert not ex.ready("A", "target_hit")
    assert ex.ready("A", "stop_hit")
    calls = broker.calls
    ex("A", "US", 38.0, _Trig())
    assert broker.calls == calls + 1
