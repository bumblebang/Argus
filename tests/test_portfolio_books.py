"""portfolio_books — KR/US 원장 + ₩ 환산 총자산."""
from src.portfolio_books import apply_books, book_rates, build_books, read_fx_usdkrw


def _snap(**kw):
    base = {
        "cash": {}, "total_purchase": {}, "market_value": {},
        "profit": {}, "daily_profit": {}, "items": [],
    }
    base.update(kw)
    return base


def test_book_rates_from_purchase_and_equity():
    snap = _snap(
        cash={"KR": 100.0},
        market_value={"KR": 900.0},
        total_purchase={"KR": 800.0},
        profit={"KR": 100.0},
        daily_profit={"KR": 20.0},
    )
    pnl_r, daily_r = book_rates(snap)
    assert abs(pnl_r["KR"] - 100 / 800) < 1e-9
    assert abs(daily_r["KR"] - 20 / 1000) < 1e-9


def test_build_books_fx_total():
    snap = _snap(
        cash={"KR": 1_000_000.0, "US": 100.0},
        market_value={"KR": 500_000.0, "US": 50.0},
        total_purchase={"KR": 480_000.0, "US": 40.0},
        profit={"KR": 20_000.0, "US": 10.0},
        daily_profit={"KR": 1_000.0, "US": 2.0},
    )
    out = build_books(snap, fx_usdkrw=1300.0, fx_ts=1.0)
    assert out["fx"]["USDKRW"] == 1300.0
    assert out["books"]["KR"]["equity"] == 1_500_000.0
    assert out["books"]["US"]["equity"] == 150.0
    assert out["books"]["US"]["equity_krw"] == 150.0 * 1300
    # 총자산 = KR equity + US equity * FX
    assert out["totals"]["equity_krw"] == 1_500_000.0 + 150.0 * 1300
    assert out["totals"]["fx_note"] == "USDKRW estimate"
    assert abs(out["books"]["KR"]["pnl_rate"] - 20_000 / 480_000) < 1e-9
    assert abs(out["books"]["US"]["pnl_rate"] - 10 / 40) < 1e-9


def test_build_books_no_fx_nulls_total_when_us_present():
    snap = _snap(
        cash={"KR": 100.0, "US": 10.0},
        market_value={"KR": 0.0, "US": 5.0},
    )
    out = build_books(snap, fx_usdkrw=None)
    assert out["fx"]["USDKRW"] is None
    assert out["totals"]["equity_krw"] is None
    assert out["totals"]["fx_note"] == "FX unavailable"
    assert "equity_krw" not in out["books"]["US"] or out["books"]["US"].get("equity_krw") is None


def test_build_books_kr_only_total_without_fx():
    snap = _snap(cash={"KR": 700.0}, market_value={"KR": 300.0}, profit={"KR": 10.0})
    out = build_books(snap)
    assert out["totals"]["equity_krw"] == 1000.0
    assert out["books"]["KR"]["equity_krw"] == 1000.0


def test_apply_books_overwrites_scalar_rates():
    snap = _snap(
        cash={"KR": 0.0, "US": 0.0},
        total_purchase={"KR": 100.0, "US": 50.0},
        market_value={"KR": 110.0, "US": 55.0},
        profit={"KR": 10.0, "US": 5.0},
        profit_rate={"KR": 0.99, "US": 0.99},  # stale scalar paste
    )
    apply_books(snap, fx_usdkrw=1000.0, ensure_markets=("KR", "US"))
    assert abs(snap["profit_rate"]["KR"] - 0.1) < 1e-9
    assert abs(snap["profit_rate"]["US"] - 0.1) < 1e-9
    assert "books" in snap and "totals" in snap
    assert snap["totals"]["equity_krw"] == 110.0 + 55.0 * 1000


def test_read_fx_usdkrw():
    assert read_fx_usdkrw(None) == (None, None)
    assert read_fx_usdkrw({}) == (None, None)
    rate, ts = read_fx_usdkrw({"fx": {"USDKRW": 1350.5, "ts": 12.0}})
    assert rate == 1350.5 and ts == 12.0
    assert read_fx_usdkrw({"fx": {"USDKRW": 0}})[0] is None
