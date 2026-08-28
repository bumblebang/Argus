"""J3-게이트 — realized_pnl 을 우회한 체결 때문에 일손실 한도가 눈이 멀던 결함.

재현(수정 전): 미체결 매도가 폴링 밖에서 체결되면 주기 재대사가 live holdings
로 흡수한다(apply_reconcile_from_live 는 cash/positions 만 덮는다). 그 매도의
손익은 realized_pnl 에 안 잡히므로 일손실 게이트는 계속 0 을 본다 — 실제로는
자산이 한도 넘게 줄었는데 신규 매수가 계속 승인된다.
"""
from src.market_hours import market_day
from src.paper_account import PaperAccount
from src.risk_gate import Order, RiskGate
from src.strategies.base import Position


def _gate(tmp_path, **over):
    limits = {"capital": {"KR": 1_000_000}, "max_position_pct": 1.0,
              "max_positions": 5, "daily_loss_limit_pct": 0.05,
              "kill_switch_file": str(tmp_path / "HALT")}
    limits.update(over)
    return RiskGate(limits)


def _acct(tmp_path, cash=1_000_000):
    return PaperAccount(cash={"KR": cash}, fee_rate={"KR": 0.0},
                        slippage_bps={"KR": 0.0}, state_path=tmp_path / "a.json")


def _order(qty=1, price=1_000):
    return Order("000660", "KR", "BUY", qty, price)


def _set_realized_today(acct, market, pnl):
    acct.realized_pnl_today[market] = pnl
    acct._pnl_day[market] = market_day(market)


# ── SoD 델타 산출 ───────────────────────────────────────────────
def test_sod_delta_none_before_snapshot(tmp_path):
    acct = PaperAccount(cash={"KR": 0}, state_path=tmp_path / "a.json")
    assert acct.sod_equity_delta("KR") is None      # equity 0 -> 스냅 불가


def test_sod_delta_zero_at_open(tmp_path):
    acct = _acct(tmp_path)
    acct.ensure_sod_equity("KR")
    assert acct.sod_equity_delta("KR") == 0.0


def test_sod_delta_tracks_cash_drop(tmp_path):
    """재대사가 cash 를 실계좌 값으로 덮은 상황 모사."""
    acct = _acct(tmp_path)
    acct.ensure_sod_equity("KR")
    acct.cash["KR"] = 900_000
    assert acct.sod_equity_delta("KR") == -100_000.0


def test_sod_delta_includes_unrealized(tmp_path):
    acct = _acct(tmp_path)
    acct.apply_fill("005930", "KR", "BUY", 10, 10_000, 0.0, "seed")
    acct.set_marks({"005930": 10_000})              # SoD 스냅
    acct.set_marks({"005930": 8_000})
    assert acct.sod_equity_delta("KR") == -20_000.0


# ── 게이트: 재현 → 차단 ─────────────────────────────────────────
def test_daily_loss_trips_on_equity_drop_without_realized(tmp_path):
    """핵심 재현: realized_pnl 은 0 인데 자산이 6% 줄었다."""
    gate, acct = _gate(tmp_path), _acct(tmp_path)
    acct.ensure_sod_equity("KR")
    assert gate.check(_order(), acct).approved

    acct.cash["KR"] = 940_000                       # 재대사가 덮은 결과
    assert acct.daily_realized_pnl("KR") == 0.0     # realized 는 눈이 멀어 있다
    d = gate.check(_order(), acct)
    assert not d.approved
    assert "일 손실 한도" in d.reason and "자산변화" in d.reason


def test_small_equity_drop_still_allowed(tmp_path):
    gate, acct = _gate(tmp_path), _acct(tmp_path)
    acct.ensure_sod_equity("KR")
    acct.cash["KR"] = 980_000                       # -2% < 5%
    assert gate.check(_order(), acct).approved


def test_realized_loss_still_trips(tmp_path):
    """기존 경로(실현손익)도 그대로 동작해야 한다."""
    gate, acct = _gate(tmp_path), _acct(tmp_path)
    acct.ensure_sod_equity("KR")
    _set_realized_today(acct, "KR", -60_000)
    d = gate.check(_order(), acct)
    assert not d.approved and "실현손익" in d.reason


def test_worse_of_the_two_is_used(tmp_path):
    """실현 -10,000 / 자산 -60,000 → 나쁜 쪽(자산)으로 차단."""
    gate, acct = _gate(tmp_path), _acct(tmp_path)
    acct.ensure_sod_equity("KR")
    _set_realized_today(acct, "KR", -10_000)
    acct.cash["KR"] = 940_000
    d = gate.check(_order(), acct)
    assert not d.approved and "자산변화" in d.reason


def test_equity_gain_does_not_mask_realized_loss(tmp_path):
    """자산이 늘어도 실현손실이 한도를 넘었으면 막아야 한다."""
    gate, acct = _gate(tmp_path), _acct(tmp_path)
    acct.ensure_sod_equity("KR")
    _set_realized_today(acct, "KR", -60_000)
    acct.cash["KR"] = 1_200_000                     # 델타 +200,000
    d = gate.check(_order(), acct)
    assert not d.approved and "실현손익" in d.reason


def test_midday_first_sod_snap_refused(tmp_path, monkeypatch):
    """장중 + 당일 체결 있음: SoD 스냅 거부(일손실 리셋 방지)."""
    monkeypatch.setattr("src.paper_account.current_session",
                        lambda m, now=None: "regular")
    gate, acct = _gate(tmp_path), _acct(tmp_path, cash=940_000)
    acct.apply_fill("005930", "KR", "BUY", 1, 70000, 0.0, "seed")
    assert acct.ensure_sod_equity("KR") == 0.0
    assert acct.sod_equity_delta("KR") is None
    # capital 폴백(1M×5%=50k). 실현 -60k 면 차단.
    _set_realized_today(acct, "KR", -60_000)
    d = gate.check(_order(), acct)
    assert not d.approved and "일 손실" in d.reason


def test_us_first_sod_allowed_when_no_fill_yet(tmp_path, monkeypatch):
    """US 프리/정규 첫 틱·당일 체결 없음 → SoD 허용(KR-only 창만으로는 못 찍던 케이스)."""
    monkeypatch.setattr("src.paper_account.current_session",
                        lambda m, now=None: "premarket" if m == "US" else "closed")
    acct = PaperAccount(cash={"KR": 0, "US": 500_000}, fee_rate={"KR": 0.0, "US": 0.0},
                        slippage_bps={"KR": 0.0, "US": 0.0}, state_path=tmp_path / "us.json")
    assert acct.ensure_sod_equity("US") == 500_000.0


def test_deposit_does_not_mask_bypass_loss(tmp_path):
    """입금이 SoD 를 같이 올려 우회 손실 델타를 가리지 못한다."""
    gate, acct = _gate(tmp_path), _acct(tmp_path)
    acct.ensure_sod_equity("KR")                     # SoD=1,000,000
    acct.cash["KR"] = 940_000                        # 우회 손실 -60k
    assert acct.sod_equity_delta("KR") == -60_000.0
    acct.adjust_sod_for_external_cash("KR", 200_000)  # 입금
    acct.cash["KR"] = 1_140_000                      # 940k+200k
    # SoD 도 1.2M 으로 이동 → 델타는 여전히 -60k
    assert acct._sod_equity["KR"] == 1_200_000.0
    assert acct.sod_equity_delta("KR") == -60_000.0
    d = gate.check(_order(), acct)
    assert not d.approved and "자산변화" in d.reason


def test_reconcile_deposit_adjusts_sod(tmp_path):
    from src.broker_sync import apply_reconcile_from_live
    acct = _acct(tmp_path)
    acct.ensure_sod_equity("KR")
    data = {"cash": {"KR": 1_200_000}, "holdings_ok": True, "items": []}
    out = apply_reconcile_from_live(acct, None, data)
    assert out.get("external_cash", {}).get("KR") == 200_000
    assert acct._sod_equity["KR"] == 1_200_000.0
    assert acct.sod_equity_delta("KR") == 0.0


def test_flag_off_restores_legacy_behaviour(tmp_path):
    gate = _gate(tmp_path, daily_loss_use_sod_delta=False)
    acct = _acct(tmp_path)
    acct.ensure_sod_equity("KR")
    acct.cash["KR"] = 940_000
    assert gate.check(_order(), acct).approved      # 실현손익만 보면 통과


def test_sell_not_blocked_by_daily_loss(tmp_path):
    """일손실은 신규 매수만 막는다 — 청산은 계속 가능해야 한다."""
    gate, acct = _gate(tmp_path), _acct(tmp_path)
    acct.apply_fill("005930", "KR", "BUY", 10, 10_000, 0.0, "seed")
    acct.ensure_sod_equity("KR")
    acct.cash["KR"] = 0
    d = gate.check(Order("005930", "KR", "SELL", 10, 5_000), acct)
    assert d.approved


def test_drawdown_stays_cumulative_axis(tmp_path):
    """DD 는 누적 축 — 당일 델타를 섞지 않는다(두 브레이커가 붕괴하지 않게)."""
    gate = _gate(tmp_path, max_drawdown_pct=0.10, daily_loss_limit_pct=0.99)
    acct = _acct(tmp_path)
    acct.ensure_sod_equity("KR")
    acct.cash["KR"] = 800_000                       # 당일 -20%
    d = gate.check(_order(), acct)
    assert d.approved, "DD 는 realized 누적+미실현만 본다"


def test_account_without_sod_delta_falls_back(tmp_path):
    """sod_equity_delta 가 없는 계좌 객체(테스트 스텁 등)도 동작."""
    class _Legacy:
        realized_pnl = {"KR": 0.0}
        positions: dict = {}
        symbol_market: dict = {}
        open_count = 0

        def buying_power(self, m):
            return 1_000_000

        def position(self, s):
            return Position(symbol=s)

    assert _gate(tmp_path).check(_order(), _Legacy()).approved
