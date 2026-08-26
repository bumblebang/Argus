"""측정층 공통 청산 정의 (trade_defs)."""
from src.eval.trade_defs import (group_scored_rows, roundtrip_cost_pct,
                                 scored_trades, trade_group_id)


def test_roundtrip_cost_pct_defaults():
    kr = roundtrip_cost_pct("KR")
    # 0.00015*2 + 0.0015 + 5bps*2 = 0.0003 + 0.0015 + 0.001 = 0.0028
    assert abs(kr - 0.0028) < 1e-12
    us = roundtrip_cost_pct("US")
    # 0.001*2 + 0 + 5bps*2 = 0.003
    assert abs(us - 0.003) < 1e-12


def test_roundtrip_cost_pct_reads_cfg():
    cfg = {"paper": {"fee_rate": {"KR": 0.001}, "slippage_bps": {"KR": 0},
                     "sell_tax_rate": {"KR": 0.0}}}
    assert roundtrip_cost_pct("KR", cfg) == 0.002


def test_qty_zero_and_null_pnl_dropped():
    rows = [
        {"id": 1, "symbol": "A", "pnl": 10, "qty": 0, "avg_price": 100},
        {"id": 2, "symbol": "B", "pnl": None, "qty": 1, "avg_price": 100},
        {"id": 3, "symbol": "C", "pnl": 5, "qty": 1, "avg_price": 100},
    ]
    out = group_scored_rows(rows)
    assert [t["symbol"] for t in out] == ["C"]
    assert out[0]["pnl"] == 5


def test_parent_id_groups_partial_exits():
    rows = [
        {"id": 10, "parent_id": 10, "symbol": "A", "pnl": 3, "qty": 1,
         "avg_price": 100, "meta": '{"conviction": 0.6}'},
        {"id": 11, "parent_id": 10, "symbol": "A", "pnl": 7, "qty": 2,
         "avg_price": 100},
        {"id": 12, "symbol": "B", "pnl": -1, "qty": 1, "avg_price": 50},
    ]
    out = group_scored_rows(rows)
    assert len(out) == 2
    a = next(t for t in out if t["symbol"] == "A")
    assert a["id"] == 10 and a["pnl"] == 10 and a["qty"] == 3
    assert a["cost"] == 300
    assert a["meta"]


def test_scored_trades_uses_store(tmp_path):
    from src.engine.store import Store
    store = Store(tmp_path / "t.db")
    a = store.open_position("AAA", "KR", 2, 100)
    store.close_position(a, exit_price=110, reason="target")
    b = store.open_position("BBB", "KR", 1, 50)
    store.close_position(b, exit_price=40, reason="stop")
    # armed-only (qty=0) 는 거래가 아니다
    store.arm_candidate("CCC", "KR")
    trades = scored_trades(store)
    syms = {t["symbol"] for t in trades}
    assert syms == {"AAA", "BBB"}
    assert trade_group_id(trades[0]) in {t["id"] for t in trades}
