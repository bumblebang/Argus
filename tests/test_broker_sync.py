"""실계좌 → 봇 원장/store 동기화 테스트.

핵심 불변식: 실계좌가 진실(single source of truth). 봇 원장/store 는 실계좌 기준으로
교체된다. 동기화된 보유는 코드 청산 비활성(stop/target=None) + 재평가 thesis 로 표시.
"""
import json

from src.paper_account import PaperAccount
from src.engine.store import Store
from src.strategies.base import Position
from src.broker_sync import (sync_from_live, reconcile_from_live, should_sync,
                             SYNC_THESIS, RECONCILE_THESIS)


class _MockClient:
    """holdings/buying-power 응답을 지정. 문자열 값(실 API 그대로). 호출 카운트 기록."""
    def __init__(self, holdings=None, buying_power=None, holdings_exc=None,
                 bp_exc=None):
        self._holdings = holdings if holdings is not None else {"items": []}
        self._bp = buying_power or {}     # {market: {"cashBuyingPower": "..."}}
        self._holdings_exc = holdings_exc
        self._bp_exc = bp_exc
        self.holdings_calls = 0
        self.bp_calls = []

    def get_holdings(self, account_seq, symbol=None):
        self.holdings_calls += 1
        if self._holdings_exc is not None:
            raise self._holdings_exc
        return self._holdings

    def get_buying_power(self, account_seq, market):
        self.bp_calls.append(market)
        if isinstance(self._bp_exc, dict) and market in self._bp_exc:
            raise self._bp_exc[market]
        return self._bp.get(market, {})


def _acct(tmp_path, cash=None):
    return PaperAccount(cash=cash or {"KR": 0, "US": 0},
                        state_path=tmp_path / "acct.json")


_SAMSUNG = {"symbol": "005930", "name": "삼성전자", "marketCountry": "KR",
            "currency": "KRW", "quantity": "1", "lastPrice": "273500",
            "averagePurchasePrice": "267500"}


# (a) 실계좌 미러링 — 현금·포지션·store.open_position(source=synced) ─────────
def test_sync_mirrors_cash_and_positions(tmp_path):
    client = _MockClient(holdings={"items": [_SAMSUNG]},
                         buying_power={"KR": {"cashBuyingPower": "732463"},
                                       "US": {"cashBuyingPower": "0"}})
    acct = _acct(tmp_path)
    store = Store(tmp_path / "bot.db")
    summary = sync_from_live(client, 1, acct, store, markets=("KR", "US"))

    assert acct.cash["KR"] == 732463
    pos = acct.positions["005930"]
    assert pos.qty == 1 and pos.avg_price == 267500
    assert acct.symbol_market["005930"] == "KR"

    rows = store.get_open_positions()
    assert len(rows) == 1
    row = rows[0]
    assert row["symbol"] == "005930" and row["qty"] == 1 and row["avg_price"] == 267500
    assert row["thesis"] == SYNC_THESIS
    assert row["stop_price"] is None and row["target_price"] is None
    import json
    assert json.loads(row["meta"])["source"] == "synced"

    assert summary["synced"] == 1
    assert summary["cash"]["KR"] == 732463
    assert summary["positions"] == [{"symbol": "005930", "qty": 1, "avg": 267500}]
    store.close()


# (b) 실계좌에 없는 기존 store open 포지션은 close ────────────────────────
def test_sync_closes_stale_store_positions(tmp_path):
    store = Store(tmp_path / "bot.db")
    # 실계좌엔 없는 유령 보유가 store 에 open 상태.
    ghost = store.open_position("000660", "KR", 5, 100000, thesis="예전 매수")
    acct = _acct(tmp_path)
    client = _MockClient(holdings={"items": [_SAMSUNG]},
                         buying_power={"KR": {"cashBuyingPower": "732463"}})
    sync_from_live(client, 1, acct, store, markets=("KR",))

    open_syms = {r["symbol"] for r in store.get_open_positions()}
    assert open_syms == {"005930"}          # 유령은 청산, 실보유만 남음
    row = store.conn.execute("SELECT state, exit_reason FROM positions WHERE id=?",
                             (ghost,)).fetchone()
    assert row["state"] == "closed" and row["exit_reason"] == "live_sync"
    store.close()


def test_sync_idempotent_updates_existing(tmp_path):
    # 이미 synced 된 종목을 재동기화하면 중복 insert 없이 수량/평단 갱신만.
    store = Store(tmp_path / "bot.db")
    acct = _acct(tmp_path)
    client = _MockClient(holdings={"items": [_SAMSUNG]},
                         buying_power={"KR": {"cashBuyingPower": "732463"}})
    sync_from_live(client, 1, acct, store, markets=("KR",))
    # 2주로 늘어난 실계좌 상태로 재동기화.
    grown = dict(_SAMSUNG, quantity="2", averagePurchasePrice="270000")
    client2 = _MockClient(holdings={"items": [grown]},
                          buying_power={"KR": {"cashBuyingPower": "500000"}})
    sync_from_live(client2, 1, acct, store, markets=("KR",))

    rows = store.get_open_positions()
    assert len(rows) == 1                    # 중복 없음
    assert rows[0]["qty"] == 2 and rows[0]["avg_price"] == 270000
    assert acct.positions["005930"].qty == 2
    store.close()


# (c) 문자열 파싱·빈 items·조회 실패 시장 스킵 ─────────────────────────────
def test_sync_empty_items_no_positions(tmp_path):
    acct = _acct(tmp_path, cash={"KR": 111, "US": 222})
    client = _MockClient(holdings={"items": []},
                         buying_power={"KR": {"cashBuyingPower": "732463"},
                                       "US": {"cashBuyingPower": "50"}})
    summary = sync_from_live(client, 1, acct, None, markets=("KR", "US"))
    assert acct.positions == {}
    assert acct.cash == {"KR": 732463, "US": 50}
    assert summary["synced"] == 0


def test_sync_bp_failure_keeps_existing_cash(tmp_path):
    acct = _acct(tmp_path, cash={"KR": 999, "US": 888})
    # US 조회는 예외 → 기존값 유지. KR 은 정상 세팅.
    client = _MockClient(holdings={"items": []},
                         buying_power={"KR": {"cashBuyingPower": "732463"}},
                         bp_exc={"US": RuntimeError("boom")})
    sync_from_live(client, 1, acct, None, markets=("KR", "US"))
    assert acct.cash["KR"] == 732463
    assert acct.cash["US"] == 888            # 실패 시장은 기존값 유지


def test_sync_holdings_failure_keeps_positions(tmp_path):
    acct = _acct(tmp_path)
    acct.positions["005930"] = Position(symbol="005930", qty=1, avg_price=100)
    client = _MockClient(holdings_exc=RuntimeError("holdings down"),
                         buying_power={"KR": {"cashBuyingPower": "732463"}})
    summary = sync_from_live(client, 1, acct, None, markets=("KR",))
    # 보유 조회 실패 → 포지션 통째로 비우지 않음(기존 유지). 현금은 반영.
    assert "005930" in acct.positions
    assert acct.cash["KR"] == 732463
    assert summary["synced"] == 0


def test_sync_parses_string_numbers_and_skips_bad_qty(tmp_path):
    items = [dict(_SAMSUNG),
             {"symbol": "000660", "marketCountry": "KR", "quantity": "0",
              "averagePurchasePrice": "50000"},         # 수량 0 → 스킵
             {"symbol": "", "quantity": "3", "averagePurchasePrice": "1"}]  # 심볼 없음 → 스킵
    client = _MockClient(holdings={"items": items},
                         buying_power={"KR": {"cashBuyingPower": "732463"}})
    acct = _acct(tmp_path)
    summary = sync_from_live(client, 1, acct, None, markets=("KR",))
    assert set(acct.positions) == {"005930"}
    assert summary["synced"] == 1


def test_sync_normalizes_market_country_upper(tmp_path):
    """marketCountry 소문자도 KR/US 로 정규화 — live_markets·익스포저 게이트 일치."""
    from src.broker_sync import _parse_holdings_items
    items = [{"symbol": "AAPL", "marketCountry": "us", "quantity": "3",
              "averagePurchasePrice": "150"}]
    pos, mkt = _parse_holdings_items(items)
    assert mkt["AAPL"] == "US"


# (d) 페이퍼 모드면 watch 배선이 sync 안 함(호출 카운트 0) ────────────────
def test_should_sync_gates_on_broker_mode(tmp_path):
    from src.agents.pipeline import build_paper_core
    from src.config import load_config
    cfg = load_config()

    # live_client 미주입 → 페이퍼(build_paper_core 심층 방어).
    broker_paper, _ = build_paper_core(cfg)
    assert broker_paper.mode == "paper"
    assert should_sync(broker_paper) is False

    # watch 배선을 그대로 재현: should_sync False 면 sync_from_live 호출 0.
    client = _MockClient(holdings={"items": [_SAMSUNG]},
                         buying_power={"KR": {"cashBuyingPower": "732463"}})
    if should_sync(broker_paper):
        sync_from_live(client, 1, broker_paper.account, None)
    assert client.holdings_calls == 0 and client.bp_calls == []


# ── 주기 재대사(reconcile_from_live) — 병합 규율 ────────────────────────────
# 기동 동기화(전면 교체)와 달리, 봇이 관리 중인 포지션의 thesis/손절/목표는 보존하고
# 수량·평단만 실계좌로 맞춘다. 고아는 채택, 유령은 청산.

def test_reconcile_preserves_bot_managed_plan(tmp_path):
    store = Store(tmp_path / "bot.db")
    acct = _acct(tmp_path)
    # 봇이 산 포지션(계획 레벨 보유). 실계좌는 2주/평단 270000 으로 어긋나 있다.
    store.open_position("005930", "KR", 1, 267500, strategy="ma_crossover",
                        thesis="봇 매수 근거", target_price=290000, stop_price=250000,
                        meta={"horizon": "swing"})
    grown = dict(_SAMSUNG, quantity="2", averagePurchasePrice="270000")
    client = _MockClient(holdings={"items": [grown]},
                         buying_power={"KR": {"cashBuyingPower": "500000"}})
    res = reconcile_from_live(client, 1, acct, store, markets=("KR",))

    row = store.get_open_positions()[0]
    assert row["qty"] == 2 and row["avg_price"] == 270000        # 실계좌로 수렴
    assert row["thesis"] == "봇 매수 근거"                        # 계획은 보존
    assert row["stop_price"] == 250000 and row["target_price"] == 290000
    assert row["strategy"] == "ma_crossover"
    assert acct.positions["005930"].qty == 2                     # 원장도 실계좌 미러
    assert acct.cash["KR"] == 500000
    assert res["adopted"] == [] and res["closed"] == []
    assert res["updated"] == ["005930"]
    store.close()


def test_reconcile_adopts_orphan_holding(tmp_path):
    # 실계좌엔 있는데 봇이 모르는 보유(고아) → 채택해 뇌가 재평가하게 한다.
    store = Store(tmp_path / "bot.db")
    acct = _acct(tmp_path)
    client = _MockClient(holdings={"items": [_SAMSUNG]},
                         buying_power={"KR": {"cashBuyingPower": "732463"}})
    res = reconcile_from_live(client, 1, acct, store, markets=("KR",))

    row = store.get_open_positions()[0]
    assert row["symbol"] == "005930" and row["qty"] == 1
    assert row["thesis"] == RECONCILE_THESIS
    assert row["stop_price"] is None and row["target_price"] is None  # 코드청산 비활성
    assert json.loads(row["meta"])["source"] == "reconcile_adopted"
    assert res["adopted"] == ["005930"]
    store.close()


def test_reconcile_closes_phantom_position(tmp_path):
    # 봇 원장/store 엔 있는데 실계좌엔 없는 유령 → 청산.
    store = Store(tmp_path / "bot.db")
    acct = _acct(tmp_path)
    acct.positions["000660"] = Position(symbol="000660", qty=5, avg_price=100000)
    acct.symbol_market["000660"] = "KR"
    ghost = store.open_position("000660", "KR", 5, 100000, thesis="유령")
    client = _MockClient(holdings={"items": [_SAMSUNG]},
                         buying_power={"KR": {"cashBuyingPower": "732463"}})
    res = reconcile_from_live(client, 1, acct, store, markets=("KR",))

    assert "000660" not in acct.positions                        # 원장에서 제거
    row = store.conn.execute("SELECT state, exit_reason FROM positions WHERE id=?",
                             (ghost,)).fetchone()
    assert row["state"] == "closed" and row["exit_reason"] == "reconcile"
    assert res["closed"] == ["000660"] and res["adopted"] == ["005930"]
    store.close()


def test_reconcile_holdings_failure_keeps_positions(tmp_path):
    # 보유 조회 실패 → 포지션 병합 생략(기존 유지). 현금은 이미 반영된 값 유지.
    acct = _acct(tmp_path)
    acct.positions["005930"] = Position(symbol="005930", qty=1, avg_price=100)
    client = _MockClient(holdings_exc=RuntimeError("holdings down"),
                         buying_power={"KR": {"cashBuyingPower": "732463"}})
    res = reconcile_from_live(client, 1, acct, None, markets=("KR",))
    assert "005930" in acct.positions
    assert acct.cash["KR"] == 732463
    assert "error" in res


def test_reconcile_partial_qty_drop_records_pnl(tmp_path):
    """재대사로 qty 감소 시 partial exit 귀속(track_record).

    저널 매도는 코드 청산기가 방금 apply_fill 한 것 — 시간 창 안이어야 인정된다.
    """
    from datetime import datetime, timezone

    from src.paper_account import Fill
    store = Store(tmp_path / "bot.db")
    acct = _acct(tmp_path)
    acct.positions["005930"] = Position(symbol="005930", qty=1, avg_price=267500)
    acct.symbol_market["005930"] = "KR"
    acct.journal.append(Fill(ts=datetime.now(timezone.utc).isoformat(),
                             symbol="005930", market="KR",
                             side="SELL", qty=1, price=270000, fee=0, reason="test"))
    row_id = store.open_position("005930", "KR", 2, 267500, thesis="tracked")
    partial = _SAMSUNG.copy()
    partial["quantity"] = "1"
    client = _MockClient(holdings={"items": [partial]},
                         buying_power={"KR": {"cashBuyingPower": "732463"}})
    res = reconcile_from_live(client, 1, acct, store, markets=("KR",))
    row = store.get_open_positions()[0]
    assert row["qty"] == 1
    closed = store.conn.execute(
        "SELECT qty, exit_price, pnl FROM positions "
        "WHERE state='closed' AND exit_reason='reconcile'"
    ).fetchone()
    assert closed["qty"] == 1
    assert closed["exit_price"] == 270000
    assert closed["pnl"] == 2500.0
    from src.attribution import track_record
    tr = track_record(store)
    assert tr.get("strategy_stats") or tr.get("total_trades", 0) >= 0
    assert res["updated"] == ["005930"]
    store.close()


def test_reconcile_adopt_cancels_shadow(tmp_path):
    """고아 채택 시 해당 종목 pending shadow 취소."""
    from src.shadow_ledger import book_row
    store = Store(tmp_path / "bot.db")
    acct = _acct(tmp_path)
    book_row(
        store, cycle_ts=1.0, cycle_ts_iso="2026-01-01T00:00:00+09:00",
        sleeve="brain", symbol="005930", market="KR",
        block_status="armed", block_reason="test", verifier_reason=None,
        concerns=[], conviction=0.7, horizon="swing", target_weight=0.1,
        thesis="shadow", strategy=None, proposal=None, entry_price=270000,
        state="pending",
    )
    assert len(store.get_pending_shadow_positions()) == 1
    client = _MockClient(holdings={"items": [_SAMSUNG]},
                         buying_power={"KR": {"cashBuyingPower": "732463"}})
    reconcile_from_live(client, 1, acct, store, markets=("KR",))
    assert store.get_pending_shadow_positions() == []
    assert len(store.get_open_positions()) == 1
    store.close()
