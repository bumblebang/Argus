"""engine.entry_basis — 청산 룰셋을 진입 근거에 묶는다.

2026-09-09 사고 회귀: 도시에 진입존 논거로 산 066570 을 배정 라벨(macd) 만 보고
코드가 2초 만에 데드크로스로 팔았다. 진입 논거에 골든크로스는 없었다.
"""
import pandas as pd
import pytest

from src.engine.entry_basis import (BASIS_ORPHAN, BASIS_SIGNAL, BASIS_THESIS,
                                    BASIS_VALUE, BASIS_ZONE, SignalExitConfig,
                                    basis_of, parse_signal_exit,
                                    signal_exit_allowed)
from src.engine.store import Store
from src.engine.strategy_runner import StrategyRunner
from src.paper_account import PaperAccount
from src.risk_gate import RiskGate
from src.broker import Broker
from src.runner import drop_unclosed_bar
from src.strategies import build_strategy


class FakeGW:
    def __init__(self, candles):
        self._c = candles

    def candles(self, sym, interval="1m", count=20):
        return self._c


def _broker(tmp_path):
    acct = PaperAccount(cash={"KR": 10_000_000}, state_path=tmp_path / "pa.json")
    gate = RiskGate({"capital": {"KR": 1_000_000}, "max_order_notional": {},
                     "kill_switch_file": str(tmp_path / "HALT")})
    return Broker(account=acct, gate=gate, mode="paper")


def _rising(n=40):
    return [{"open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": 1000}
            for c in (float(x) for x in range(20, 20 + n))]


# ── basis 판정 ────────────────────────────────────────────────
def test_explicit_stamp_wins():
    assert basis_of({"meta": {"entry_basis": "signal"}}) == BASIS_SIGNAL
    assert basis_of({"meta": '{"entry_basis": "zone"}'}) == BASIS_ZONE


def test_infers_legacy_rows_without_stamp():
    """스탬프 이전 포지션도 meta 모양으로 추론된다(라이브 보유분 호환)."""
    assert basis_of({"meta": {"entry_zone": {"low": 1, "high": 2}}}) == BASIS_ZONE
    assert basis_of({"meta": {"source": "value"}}) == BASIS_VALUE
    assert basis_of({"strategy": "value", "meta": {}}) == BASIS_VALUE
    assert basis_of({"meta": {"source": "fill_mirror"}}) == BASIS_ORPHAN
    # 뇌 즉시 체결(도시에 논거) — entry_zone 없음
    assert basis_of({"meta": {"dossier_id": 1633, "horizon": "swing"}}) == BASIS_THESIS


def test_unknown_defaults_to_thesis_not_signal():
    """판정 불가는 보수적으로 — 논거 미상 포지션을 라벨만 보고 팔지 않는다."""
    assert basis_of({}) == BASIS_THESIS
    assert basis_of({"meta": {"entry_basis": "무언가"}}) == BASIS_THESIS
    assert signal_exit_allowed({})[0] is False


@pytest.mark.parametrize("meta,allowed", [
    ({"entry_basis": "signal"}, True),
    ({"entry_basis": "zone"}, False),
    ({"entry_basis": "thesis"}, False),
    ({"entry_basis": "value"}, False),
    ({"entry_basis": "orphan"}, False),
    # 코드 소유 트랙은 basis 와 무관하게 신호 청산 ON(세션종료 청산과 짝)
    ({"entry_basis": "thesis", "horizon": "day"}, True),
    ({"entry_basis": "thesis", "horizon": "close_scan"}, True),
])
def test_signal_exit_matrix(meta, allowed):
    assert signal_exit_allowed({"meta": meta})[0] is allowed


def test_day_horizon_falls_back_to_strategy_class():
    """meta.horizon 이 없어도 전략 클래스가 day 면 코드 소유 트랙으로 본다."""
    pos = {"strategy": "volatility_breakout", "meta": {"entry_basis": "thesis"}}
    assert signal_exit_allowed(pos)[0] is True
    # 스윙 전략은 그대로 OFF
    assert signal_exit_allowed(
        {"strategy": "macd", "meta": {"entry_basis": "thesis"}})[0] is False


# ── StrategyRunner 게이트 ─────────────────────────────────────
def test_zone_entry_demotes_instead_of_selling(tmp_path):
    """도시에 진입존으로 산 자리는 반대신호가 떠도 팔지 않고 강등한다."""
    store = Store(tmp_path / "t.db")
    broker = _broker(tmp_path)
    broker.account.fill("005930", "KR", "BUY", 3, 40)
    store.open_position("005930", "KR", 3, 40, strategy="rsi_reversion",
                        meta={"entry_basis": "zone",
                              "params": {"period": 14, "overbought": 70}})
    sr = StrategyRunner(FakeGW(_rising()), broker, store,
                        cfg=SignalExitConfig(min_hold_sec=0))

    r = sr.evaluate(dict(store.get_open_positions()[0]), "KR")
    assert r["executed"] is False and r["demoted"] is True
    assert r["basis"] == BASIS_ZONE and r["strategy"] == "rsi_reversion"
    assert broker.position("005930").qty == 3        # 보유 유지
    kinds = {e["kind"] for e in store.conn.execute("SELECT kind FROM events")}
    assert "strategy_exit" not in kinds


def test_rollback_switch_restores_old_behavior(tmp_path):
    store = Store(tmp_path / "t.db")
    broker = _broker(tmp_path)
    broker.account.fill("005930", "KR", "BUY", 3, 40)
    store.open_position("005930", "KR", 3, 40, strategy="rsi_reversion",
                        meta={"entry_basis": "zone",
                              "params": {"period": 14, "overbought": 70}})
    sr = StrategyRunner(FakeGW(_rising()), broker, store,
                        cfg=SignalExitConfig(bind_to_entry_basis=False,
                                             min_hold_sec=0))
    r = sr.evaluate(dict(store.get_open_positions()[0]), "KR")
    assert r["executed"] is True


def test_min_hold_blocks_instant_flip(tmp_path):
    """진입 직후 반대신호는 최소 보유시간 안에서 집행하지 않는다(2초 청산 방어)."""
    store = Store(tmp_path / "t.db")
    broker = _broker(tmp_path)
    broker.account.fill("005930", "KR", "BUY", 3, 40)
    store.open_position("005930", "KR", 3, 40, strategy="rsi_reversion",
                        meta={"entry_basis": "signal",
                              "params": {"period": 14, "overbought": 70}})
    pos = dict(store.get_open_positions()[0])
    gw = FakeGW(_rising())

    blocked = StrategyRunner(gw, broker, store,
                             cfg=SignalExitConfig(min_hold_sec=600))
    r = blocked.evaluate(pos, "KR")
    assert r["executed"] is False and "최소 보유" in r["reason"]
    assert broker.position("005930").qty == 3

    # 시간이 지나면 같은 신호로 청산된다.
    later = StrategyRunner(gw, broker, store,
                           cfg=SignalExitConfig(min_hold_sec=600),
                           now_fn=lambda: pos["opened_at"] + 601)
    assert later.evaluate(pos, "KR")["executed"] is True


# ── 확정봉 판정 ───────────────────────────────────────────────
def test_cross_strategies_are_closed_bar_only():
    assert build_strategy("macd", {}).closed_bar_signal is True
    assert build_strategy("ma_crossover", {}).closed_bar_signal is True
    # 레벨형은 실시간가 반응이 설계 의도 — 특히 당일 익절/손절을 쓰는 데이트레
    assert build_strategy("volatility_breakout", {}).closed_bar_signal is False
    assert build_strategy("rsi_reversion", {}).closed_bar_signal is False


def test_drop_unclosed_bar_removes_todays_partial():
    from src.market_hours import trading_date
    today = pd.Timestamp(trading_date("KR"))
    df = pd.DataFrame({"time": [today - pd.Timedelta(days=2),
                                today - pd.Timedelta(days=1), today],
                       "close": [1.0, 2.0, 3.0]})
    out = drop_unclosed_bar(df, "KR")
    assert len(out) == 2 and out["close"].iloc[-1] == 2.0
    # 마지막 봉이 오늘이 아니면(장 마감 후 어제 봉 등) 그대로 둔다
    assert len(drop_unclosed_bar(df.iloc[:-1], "KR")) == 2
    # time 열이 없으면 판정 불가 — 원본 유지
    assert len(drop_unclosed_bar(df[["close"]], "KR")) == 3


# ── config ────────────────────────────────────────────────────
def test_parse_signal_exit_defaults_on():
    d = parse_signal_exit({})
    assert d.bind_to_entry_basis is True and d.closed_bar_only is True
    c = parse_signal_exit({"strategy_exit": {"bind_to_entry_basis": False,
                                             "min_hold_sec": 5,
                                             "wake_cooldown_sec": -1}})
    assert c.bind_to_entry_basis is False and c.min_hold_sec == 5
    assert c.wake_cooldown_sec == 1800.0        # 음수는 기본값으로
