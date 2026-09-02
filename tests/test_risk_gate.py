from src.paper_account import PaperAccount
from src.risk_gate import RiskGate, Order


def _acct(tmp_path, cash=None):
    return PaperAccount(cash=cash or {"KR": 1_000_000},
                        state_path=tmp_path / "acct.json")


def _gate(tmp_path, **over):
    limits = {
        "capital": {"KR": 1_000_000},
        "max_position_pct": 0.20,
        "max_positions": 5,
        "daily_loss_limit_pct": 0.05,
        "max_order_notional": {"KR": 500_000},
        "kill_switch_file": str(tmp_path / "HALT"),
    }
    limits.update(over)
    return RiskGate(limits)


def test_normal_buy_approved(tmp_path):
    gate, acct = _gate(tmp_path), _acct(tmp_path)
    assert gate.check(Order("005930", "KR", "BUY", 100, 100), acct).approved


# ── 노출 한도 기준: capital(고정) vs equity(실자산 추종) ────────────────────
def test_exposure_base_equity_tightens_when_assets_shrink(tmp_path):
    """자산이 줄면 실자산 기준 한도는 같이 조여진다(고정 capital 은 그대로 헐겁다)."""
    # 실자산 50만(현금)인데 config capital 은 100만 — 20% 비중이면 equity 기준 10만 상한.
    acct = _acct(tmp_path, cash={"KR": 500_000})
    order = Order("005930", "KR", "BUY", 1500, 100)          # 15만

    cap_gate = _gate(tmp_path)                                # 기본=capital 기준(20만 상한)
    assert cap_gate.check(order, acct).approved

    eq_gate = _gate(tmp_path, exposure_base="equity")         # 실자산 기준(10만 상한)
    d = eq_gate.check(order, acct)
    assert not d.approved and "비중" in d.reason


def test_exposure_base_equity_grows_with_assets(tmp_path):
    """자산이 불어나면 한도도 함께 커진다(고정 capital 이면 막혔을 주문이 통과)."""
    acct = _acct(tmp_path, cash={"KR": 2_000_000})            # 실자산 200만
    order = Order("005930", "KR", "BUY", 3000, 100)           # 30만

    cap_gate = _gate(tmp_path)                                # capital 100만 → 20% = 20만
    assert not cap_gate.check(order, acct).approved

    eq_gate = _gate(tmp_path, exposure_base="equity")         # equity 200만 → 20% = 40만
    assert eq_gate.check(order, acct).approved


def test_exposure_base_equity_falls_back_to_capital_when_zero(tmp_path):
    """실자산 0(산출 불가 포함)이면 capital 로 폴백 — 한도가 조용히 사라지지 않는다."""
    acct = _acct(tmp_path, cash={"KR": 0})
    gate = _gate(tmp_path, exposure_base="equity")
    d = gate.check(Order("005930", "KR", "BUY", 2500, 100), acct)   # 25만 > capital 20%
    # 매수여력(현금0)이 먼저 걸리므로 '한도가 사라지지 않았음'만 확인한다.
    assert not d.approved


def test_reject_over_order_notional(tmp_path):
    gate, acct = _gate(tmp_path), _acct(tmp_path)
    d = gate.check(Order("005930", "KR", "BUY", 6000, 100), acct)  # 600k > 500k
    assert not d.approved and "주문금액" in d.reason


def test_reject_insufficient_buying_power(tmp_path):
    gate = _gate(tmp_path)
    acct = _acct(tmp_path, cash={"KR": 5_000})
    d = gate.check(Order("005930", "KR", "BUY", 100, 100), acct)  # 10k > 5k cash
    assert not d.approved and "매수여력" in d.reason


def test_reject_over_position_weight(tmp_path):
    gate, acct = _gate(tmp_path), _acct(tmp_path)
    d = gate.check(Order("005930", "KR", "BUY", 2500, 100), acct)  # 250k > 200k(20%)
    assert not d.approved and "비중" in d.reason


def test_reject_sell_more_than_held(tmp_path):
    gate, acct = _gate(tmp_path), _acct(tmp_path)
    d = gate.check(Order("005930", "KR", "SELL", 10, 100), acct)  # 보유 0
    assert not d.approved and "매도수량" in d.reason


def test_kill_switch_blocks(tmp_path):
    (tmp_path / "HALT").write_text("halt")
    gate, acct = _gate(tmp_path), _acct(tmp_path)
    d = gate.check(Order("005930", "KR", "BUY", 1, 100), acct)
    assert not d.approved and "킬스위치" in d.reason


# ── 포트폴리오 수준 감독관 (총 익스포저 · 섹터 집중도) ────────────────
def test_gross_exposure_blocks_overinvestment(tmp_path):
    gate = _gate(tmp_path, max_gross_exposure=0.9, max_position_pct=1.0,
                 max_order_notional={"KR": 2_000_000})
    acct = _acct(tmp_path, cash={"KR": 1_000_000})
    acct.fill("005930", "KR", "BUY", 8000, 100)               # 800k 투자
    d = gate.check(Order("035720", "KR", "BUY", 1500, 100), acct)  # +150k -> 950k > 900k
    assert not d.approved and "총 익스포저" in d.reason


def test_gross_exposure_allows_within_limit(tmp_path):
    gate = _gate(tmp_path, max_gross_exposure=0.9, max_position_pct=1.0,
                 max_order_notional={"KR": 2_000_000})
    acct = _acct(tmp_path, cash={"KR": 1_000_000})
    acct.fill("005930", "KR", "BUY", 7000, 100)               # 700k
    d = gate.check(Order("035720", "KR", "BUY", 1500, 100), acct)  # +150k -> 850k < 900k
    assert d.approved


def test_sector_concentration_blocks(tmp_path):
    smap = {"005930": "반도체", "000660": "반도체", "035720": "인터넷"}
    gate = _gate(tmp_path, max_sector_pct=0.4, max_position_pct=0.5, sector_map=smap)
    acct = _acct(tmp_path, cash={"KR": 1_000_000})
    acct.fill("005930", "KR", "BUY", 2500, 100)               # 250k 반도체
    d = gate.check(Order("000660", "KR", "BUY", 2000, 100), acct)  # +200k 반도체 -> 450k > 400k
    assert not d.approved and "섹터 집중" in d.reason


def test_sector_concentration_other_sector_ok(tmp_path):
    smap = {"005930": "반도체", "000660": "반도체", "035720": "인터넷"}
    gate = _gate(tmp_path, max_sector_pct=0.4, max_position_pct=0.5, sector_map=smap)
    acct = _acct(tmp_path, cash={"KR": 1_000_000})
    acct.fill("005930", "KR", "BUY", 2500, 100)               # 250k 반도체
    d = gate.check(Order("035720", "KR", "BUY", 2000, 100), acct)  # 200k 인터넷 -> 다른 섹터 OK
    assert d.approved


def test_sector_check_skips_unmapped_symbol(tmp_path):
    gate = _gate(tmp_path, max_sector_pct=0.4, max_position_pct=0.5, sector_map={})
    acct = _acct(tmp_path, cash={"KR": 1_000_000})
    acct.fill("005930", "KR", "BUY", 2500, 100)
    d = gate.check(Order("000660", "KR", "BUY", 2000, 100), acct)  # 섹터 미상 -> 섹터검사 스킵
    assert d.approved


def test_portfolio_checks_off_by_default(tmp_path):
    # 감독관 한도 미설정 -> 종목단위만, 쏠려도 통과(하위호환)
    gate = _gate(tmp_path, max_position_pct=1.0, max_order_notional={"KR": 2_000_000})
    acct = _acct(tmp_path, cash={"KR": 1_000_000})
    acct.fill("005930", "KR", "BUY", 8000, 100)               # 800k
    d = gate.check(Order("035720", "KR", "BUY", 1500, 100), acct)  # 950k 이지만 감독관 off
    assert d.approved


def test_sell_exempt_from_portfolio_limits(tmp_path):
    smap = {"005930": "반도체"}
    gate = _gate(tmp_path, max_gross_exposure=0.5, max_sector_pct=0.1, sector_map=smap,
                 max_position_pct=1.0, max_order_notional={"KR": 2_000_000})
    acct = _acct(tmp_path, cash={"KR": 1_000_000})
    acct.fill("005930", "KR", "BUY", 5000, 100)               # 500k
    d = gate.check(Order("005930", "KR", "SELL", 2000, 100), acct)  # 청산은 감독관 무관
    assert d.approved


# ── 일 손실 한도: 누적이 아니라 '오늘' 실현손익 기준 ────────────────
def test_daily_loss_uses_today_not_cumulative(tmp_path):
    from src.market_hours import market_day
    gate, acct = _gate(tmp_path), _acct(tmp_path)
    acct.realized_pnl["KR"] = -500_000                     # 과거 누적 손실(자본의 50%)
    acct._pnl_day["KR"] = "2020-01-01"                     # 오늘 실현손실은 없음
    d = gate.check(Order("005930", "KR", "BUY", 100, 100), acct)
    assert d.approved                                      # 누적으로 영구 차단되면 안 됨


def test_daily_loss_blocks_when_today_over_limit(tmp_path):
    from src.market_hours import market_day
    gate, acct = _gate(tmp_path), _acct(tmp_path)
    acct.realized_pnl_today["KR"] = -60_000                # 오늘 -6% (한도 5%)
    acct._pnl_day["KR"] = market_day("KR")
    d = gate.check(Order("005930", "KR", "BUY", 100, 100), acct)
    assert not d.approved and "일 손실" in d.reason


def test_daily_loss_scales_with_sod_equity(tmp_path):
    """SoD 가 크면 같은 원 손실도 비율 한도 안에 들어 매수 허용."""
    from src.market_hours import market_day
    gate = _gate(tmp_path, max_order_notional={"KR": 10_000_000})
    acct = _acct(tmp_path, cash={"KR": 100_000_000})
    acct.realized_pnl_today["KR"] = -60_000                 # 고정 capital 5%면 차단이던 금액
    acct._pnl_day["KR"] = market_day("KR")
    assert gate.check(Order("005930", "KR", "BUY", 100, 100), acct).approved
    acct.realized_pnl_today["KR"] = -5_500_000              # SoD 1억의 5.5%
    d = gate.check(Order("005930", "KR", "BUY", 100, 100), acct)
    assert not d.approved and "일 손실" in d.reason


def test_sod_equity_persists_across_reload(tmp_path):
    from src.market_hours import market_day
    path = tmp_path / "acct.json"
    acct = PaperAccount(cash={"KR": 1_000_000}, state_path=path)
    day = market_day("KR")
    acct._sod_day["KR"] = day
    acct._sod_equity["KR"] = 100_000_000.0
    acct._save()
    reloaded = PaperAccount(cash={"KR": 1_000_000}, state_path=path)
    assert reloaded.ensure_sod_equity("KR") == 100_000_000.0
    # 장중 equity 가 줄어도 당일 SoD 유지
    reloaded.cash["KR"] = 500_000
    assert reloaded.ensure_sod_equity("KR") == 100_000_000.0


def test_sod_day_rollover_resnaps(tmp_path):
    acct = _acct(tmp_path, cash={"KR": 2_000_000})
    acct._sod_day["KR"] = "2000-01-01"
    acct._sod_equity["KR"] = 50_000.0
    assert acct.ensure_sod_equity("KR") == 2_000_000.0


def test_loss_budget_falls_back_to_capital_when_sod_zero(tmp_path):
    """당일 SoD 가 0으로 고정되면 capital 폴백(기존 100만×5%)."""
    from src.market_hours import market_day
    gate, acct = _gate(tmp_path), _acct(tmp_path)
    acct._sod_day["KR"] = market_day("KR")
    acct._sod_equity["KR"] = 0.0
    acct.realized_pnl_today["KR"] = -60_000
    acct._pnl_day["KR"] = market_day("KR")
    d = gate.check(Order("005930", "KR", "BUY", 100, 100), acct)
    assert not d.approved and "일 손실" in d.reason


# ── 드로다운 브레이커: 미실현 손실 포함 ────────────────────────────
def test_drawdown_breaker_blocks_with_unrealized(tmp_path):
    gate = _gate(tmp_path, max_drawdown_pct=0.10, max_position_pct=1.0,
                 max_order_notional={"KR": 2_000_000})
    acct = _acct(tmp_path)
    acct.fill("005930", "KR", "BUY", 5000, 100)            # 500k 투자
    acct.set_marks({"005930": 80})                          # 미실현 -100k = 자본의 -10%
    d = gate.check(Order("035720", "KR", "BUY", 100, 100), acct)
    assert not d.approved and "드로다운" in d.reason


def test_drawdown_breaker_allows_within_limit(tmp_path):
    gate = _gate(tmp_path, max_drawdown_pct=0.10, max_position_pct=1.0,
                 max_order_notional={"KR": 2_000_000})
    acct = _acct(tmp_path)
    acct.fill("005930", "KR", "BUY", 5000, 100)
    acct.set_marks({"005930": 95})                          # 미실현 -25k = -2.5%
    d = gate.check(Order("035720", "KR", "BUY", 100, 100), acct)
    assert d.approved


def test_drawdown_breaker_off_when_unset(tmp_path):
    gate, acct = _gate(tmp_path), _acct(tmp_path)           # max_drawdown_pct 미설정
    acct.fill("005930", "KR", "BUY", 5000, 100)
    acct.set_marks({"005930": 1})                           # 미실현 대폭락이어도
    d = gate.check(Order("035720", "KR", "BUY", 100, 100), acct)
    assert d.approved                                       # 비활성(하위호환)


def test_drawdown_breaker_sell_exempt(tmp_path):
    gate = _gate(tmp_path, max_drawdown_pct=0.10, max_position_pct=1.0,
                 max_order_notional={"KR": 2_000_000})
    acct = _acct(tmp_path)
    acct.fill("005930", "KR", "BUY", 5000, 100)
    acct.set_marks({"005930": 50})                          # 깊은 손실
    d = gate.check(Order("005930", "KR", "SELL", 5000, 50), acct)
    assert d.approved                                       # 위험 축소는 항상 허용


def test_sell_exempt_from_max_order_notional(tmp_path):
    """고단가 1주 청산이 BUY용 주문상한에 막히면 안 된다(삼전 25만 > 캡 20만 사례)."""
    gate = _gate(tmp_path, max_order_notional={"KR": 200_000})
    acct = _acct(tmp_path)
    acct.fill("005930", "KR", "BUY", 1, 267_500)
    d = gate.check(Order("005930", "KR", "SELL", 1, 253_000), acct)
    assert d.approved
    # 같은 금액의 BUY 는 여전히 거부
    d_buy = gate.check(Order("000660", "KR", "BUY", 1, 253_000), acct)
    assert not d_buy.approved and "주문금액 초과" in d_buy.reason


def test_max_order_notional_disabled_when_empty_or_zero(tmp_path):
    """{} / 0 이면 절대캡 비활성 — 비중·현금 게이트만."""
    acct = _acct(tmp_path)
    for caps in ({}, {"KR": 0}, {"KR": None}):
        gate = _gate(tmp_path, max_order_notional=caps, max_position_pct=1.0)
        d = gate.check(Order("005930", "KR", "BUY", 1, 253_000), acct)
        assert d.approved, caps


# ── 최소 1주 시범매수 (allow_min_lot) ────────────────────────────────
def test_min_lot_exempts_order_notional_only(tmp_path):
    """qty=1 은 주문상한 면제. 종목비중 안이면 통과(비중 면제와 별개 경로)."""
    gate = _gate(tmp_path, allow_min_lot=True, max_position_pct=0.50,
                 max_order_notional={"KR": 200_000})
    acct = _acct(tmp_path)
    # 농심급 단가 35.7만 — 주문상한 20만 초과(면제), 비중 50%(50만) 안(통과)
    assert gate.check(Order("004370", "KR", "BUY", 1, 357_000), acct).approved


def test_min_lot_exempts_position_pct_for_first_share(tmp_path):
    """시범 1주는 종목비중을 넘어도 통과. 2주째는 면제 없음."""
    gate = _gate(tmp_path, allow_min_lot=True, max_position_pct=0.20,
                 max_order_notional={"KR": 200_000})
    acct = _acct(tmp_path)
    assert gate.check(Order("004370", "KR", "BUY", 1, 357_000), acct).approved
    acct.apply_fill("004370", "KR", "BUY", 1, 357_000, 0.0, "probe")
    d = gate.check(Order("004370", "KR", "BUY", 1, 357_000), acct)
    assert not d.approved


def test_min_lot_not_exempt_when_already_held(tmp_path):
    """보유 중이면 시범이 아니다 — 1주 피라미딩 차단."""
    gate = _gate(tmp_path, allow_min_lot=True, max_position_pct=0.50,
                 max_order_notional={"KR": 200_000})
    acct = _acct(tmp_path)
    order = Order("004370", "KR", "BUY", 1, 357_000)
    assert gate.check(order, acct).approved            # 첫 시범은 통과
    acct.apply_fill("004370", "KR", "BUY", 1, 357_000, 0.0, "probe")
    d = gate.check(order, acct)
    assert not d.approved and "주문금액" in d.reason


def test_min_lot_absolute_cap(tmp_path):
    """면제를 받는 주문일수록 절대 크기를 제한한다."""
    gate = _gate(tmp_path, allow_min_lot=True, max_position_pct=1.0,
                 max_order_notional={"KR": 200_000},
                 min_lot_max_notional=300_000)
    acct = _acct(tmp_path)
    d = gate.check(Order("004370", "KR", "BUY", 1, 357_000), acct)
    assert not d.approved and "시범매수 한도" in d.reason
    assert gate.check(Order("005930", "KR", "BUY", 1, 250_000), acct).approved


def test_min_lot_absolute_cap_per_market(tmp_path):
    gate = _gate(tmp_path, allow_min_lot=True, max_position_pct=1.0,
                 max_order_notional={"KR": 200_000},
                 min_lot_max_notional={"KR": 300_000})
    acct = _acct(tmp_path)
    assert not gate.check(Order("004370", "KR", "BUY", 1, 357_000), acct).approved


def test_min_lot_off_still_rejects_over_limits(tmp_path):
    gate = _gate(tmp_path, allow_min_lot=False, max_position_pct=0.20,
                 max_order_notional={"KR": 200_000})
    acct = _acct(tmp_path)
    d = gate.check(Order("004370", "KR", "BUY", 1, 357_000), acct)
    assert not d.approved


def test_min_lot_only_for_exact_min_qty(tmp_path):
    """2주면 시범매수 면제 대상이 아니다."""
    gate = _gate(tmp_path, allow_min_lot=True, min_lot_qty=1.0,
                 max_position_pct=1.0, max_order_notional={"KR": 200_000})
    acct = _acct(tmp_path)
    d = gate.check(Order("004370", "KR", "BUY", 2, 150_000), acct)  # 30만
    assert not d.approved


def test_min_lot_still_requires_cash(tmp_path):
    gate = _gate(tmp_path, allow_min_lot=True, max_order_notional={"KR": 200_000})
    acct = _acct(tmp_path, cash={"KR": 100_000})
    d = gate.check(Order("004370", "KR", "BUY", 1, 357_000), acct)
    assert not d.approved and "매수여력" in d.reason


# ── KR/US 슬롯·마켓 pause ──────────────────────────────────────────────
def test_max_positions_int_normalizes_to_both_markets(tmp_path):
    gate = _gate(tmp_path, max_positions=3)
    assert gate.max_positions == {"KR": 3, "US": 3}
    assert gate.max_positions_for("KR") == 3


def test_max_positions_per_market_kr_does_not_block_us(tmp_path):
    gate = _gate(
        tmp_path,
        capital={"KR": 1_000_000, "US": 10_000},
        max_positions={"KR": 1, "US": 2},
        max_position_pct=1.0,
        max_order_notional={},
        daily_loss_limit_pct=0.5,
    )
    acct = PaperAccount(
        cash={"KR": 1_000_000, "US": 10_000},
        fee_rate={"KR": 0.0, "US": 0.0},
        slippage_bps={"KR": 0.0, "US": 0.0},
        state_path=tmp_path / "dual.json",
    )
    acct.apply_fill("005930", "KR", "BUY", 1, 1_000, 0.0, "seed")
    # KR 슬롯 가득 → KR 신규 거부
    d_kr = gate.check(Order("000660", "KR", "BUY", 1, 1_000), acct)
    assert not d_kr.approved and "KR" in d_kr.reason
    # US 슬롯은 비어 있음 → 통과
    d_us = gate.check(Order("AAPL", "US", "BUY", 1, 100), acct)
    assert d_us.approved


def test_market_pause_blocks_buy_allows_sell(tmp_path):
    gate = _gate(tmp_path, max_position_pct=1.0, max_order_notional={})
    acct = _acct(tmp_path)
    acct.apply_fill("005930", "KR", "BUY", 2, 100, 0.0, "seed")
    pause = gate._market_pause_path("KR")
    pause.write_text("pause", encoding="utf-8")
    assert gate.pause_status() == "KR"
    d_buy = gate.check(Order("000660", "KR", "BUY", 1, 100), acct)
    assert not d_buy.approved and "pause" in d_buy.reason
    d_sell = gate.check(Order("005930", "KR", "SELL", 1, 100), acct)
    assert d_sell.approved


def test_market_pause_finds_legacy_halt_us(tmp_path, monkeypatch):
    """data/HALT.US 레거시 경로도 인식 — resolve(halt)=data/state/HALT 여도."""
    monkeypatch.setattr("src.paths.ROOT", tmp_path)
    legacy = tmp_path / "data" / "HALT.US"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("pause", encoding="utf-8")
    gate = _gate(tmp_path, max_position_pct=1.0, max_order_notional={},
                 kill_switch_file=str(tmp_path / "data" / "state" / "HALT"))
    acct = _acct(tmp_path)
    assert gate.is_market_paused("US")
    d = gate.check(Order("AAPL", "US", "BUY", 1, 100), acct)
    assert not d.approved and "pause" in d.reason


def test_global_halt_blocks_sell_too(tmp_path):
    (tmp_path / "HALT").write_text("halt", encoding="utf-8")
    gate, acct = _gate(tmp_path), _acct(tmp_path)
    acct.apply_fill("005930", "KR", "BUY", 1, 100, 0.0, "seed")
    d = gate.check(Order("005930", "KR", "SELL", 1, 100), acct)
    assert not d.approved and "킬스위치" in d.reason
    assert gate.pause_status() == "ALL"


def test_capital_keys_normalized_upper(tmp_path):
    gate = _gate(tmp_path, capital={"kr": 1_000_000})
    assert gate.capital == {"KR": 1_000_000}
    assert gate._cap("kr") == 1_000_000


def test_missing_capital_market_skips_five_limits(tmp_path, caplog):
    """capital 에 없는 시장은 base=0 — 일손실·DD·비중·총노출·섹터 전부 스킵."""
    import logging
    from src.risk_gate import capital_coverage_gaps, warn_capital_coverage

    assert capital_coverage_gaps({"KR": 1_000_000}, ["KR", "US"]) == ["US"]
    with caplog.at_level(logging.WARNING, logger="risk.gate"):
        warn_capital_coverage({"KR": 1_000_000}, ["US"])
    assert any("capital[US]" in r.message for r in caplog.records)

    smap = {"AAPL": "테크"}
    gate = _gate(
        tmp_path,
        capital={"KR": 1_000_000},
        max_drawdown_pct=0.01,
        max_gross_exposure=0.1,
        max_sector_pct=0.1,
        sector_map=smap,
        max_position_pct=0.01,
        max_order_notional={},
        daily_loss_limit_pct=0.01,
    )
    acct = PaperAccount(
        cash={"US": 1_000_000},
        fee_rate={"US": 0.0},
        slippage_bps={"US": 0.0},
        state_path=tmp_path / "us_only.json",
    )
    acct.realized_pnl_today["US"] = -500_000
    acct.realized_pnl["US"] = -500_000
    # 비중·총노출·섹터·일손실·DD 모두 걸릴 조건이지만 capital.US 없음 → 통과
    d = gate.check(Order("AAPL", "US", "BUY", 9000, 100), acct)  # 90만 > 1% 비중
    assert d.approved


def test_zero_capital_warns_and_skips_limits(tmp_path, caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="risk.gate"):
        gate = _gate(tmp_path, capital={"KR": 0}, max_position_pct=0.01,
                     max_order_notional={})
    assert any("capital[KR]=0" in r.message for r in caplog.records)
    acct = _acct(tmp_path)
    d = gate.check(Order("005930", "KR", "BUY", 9000, 100), acct)  # 90만 > 1% 비중
    assert d.approved
