"""성과 탭 거래별 실현손익 — store 청산 기준(저널 FIFO 고아 SELL 배제)."""
from scripts.dashboard import _store_trade_stats, _perf_html


def test_store_trade_stats_uses_closed_pnl_not_orphan_zero():
    closed = [
        {"symbol": "005930", "market": "KR", "qty": 1, "avg_price": 267500,
         "exit_price": 252000, "pnl": -15500, "closed_at": 100.0, "strategy": "rsi"},
        {"symbol": "257720", "market": "KR", "qty": 3, "avg_price": 39000,
         "exit_price": 44300, "pnl": 15900, "closed_at": 50.0, "strategy": "value"},
    ]
    t = _store_trade_stats(closed, paper={"start_cash": {"KR": 1_000_000}}, fx=1400)
    assert t["n"] == 2
    assert t["wins"] == 1 and t["losses"] == 1
    assert t["realized_krw"] == 400.0  # -15500+15900
    assert abs(t["ret_total"] - 0.04) < 1e-9
    # chronological: oldest→newest
    assert t["closed"][0]["symbol"] == "257720"
    assert t["closed"][1]["net"] == -15500
    assert abs(t["closed"][1]["ret_pct"] - (-15500 / 267500 * 100)) < 1e-9
    assert t["closed"][1]["avg_price"] == 267500
    assert t["closed"][1]["exit_price"] == 252000


def test_store_trade_stats_entries_include_open():
    t = _store_trade_stats(
        [{"symbol": "A", "market": "KR", "qty": 1, "avg_price": 100,
          "pnl": 10, "closed_at": 1}],
        open_positions=[{"state": "open", "qty": 2}, {"state": "armed", "qty": 0}],
        paper={"start_cash": {"KR": 1000}},
        fx=1,
    )
    assert t["entries"] == 2  # 1 closed + 1 open


def test_perf_html_shows_samsung_loss_not_zero():
    d = {
        "trades": _store_trade_stats(
            [{"symbol": "005930", "market": "KR", "qty": 1, "avg_price": 267500,
              "exit_price": 252000, "pnl": -15500, "closed_at": 1787615918.0}],
            paper={"start_cash": {"KR": 1_000_000}}, fx=1400),
        "names": {"005930": "삼성전자"},
        "closed_pos": [],
        "alpha": [],
        "manager_epochs": None,
        "calib": None,
        "shadow": None,
    }
    html = _perf_html(d)
    assert "-15,500" in html
    assert "-5.79%" in html
    assert "실현손익 (거래별)" in html
    assert "<th>매수가</th>" in html and "<th>매도가</th>" in html
    assert "267,500" in html and "252,000" in html
    i_buy = html.index("<th>매수가</th>")
    i_sell = html.index("<th>매도가</th>")
    i_pnl = html.index("<th>실현손익</th>")
    assert i_buy < i_sell < i_pnl
