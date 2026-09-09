"""2026-09-09 회귀: 매수 2초 뒤 데드크로스 청산.

실제 사고 재현 — 066570 을 도시에 진입존 논거로 205,500 에 샀는데, 그 가격에서
이미 MACD 데드크로스가 켜져 있었고(임계 ~205,850) 다음 틱에 205,000 에 팔렸다.
GILD 도 같은 패턴($146.75 매수, 임계 ~$148.0).

세 겹으로 막는다:
  ① 진입 근거 바인딩 — 도시에 논거로 산 자리는 신호로 팔지 않는다(핵심).
  ② 확정봉 판정 — 미완성 당봉에 실시간가를 덮어 크로스를 켜지 않는다.
  ③ 최소 보유시간 — 진입 직후 반대신호 집행 금지.
"""
import pandas as pd

from src.engine.entry_basis import SignalExitConfig
from src.engine.store import Store
from src.engine.strategy_runner import StrategyRunner
from src.indicators import crossed_down, macd as macd_ind
from src.market_hours import trading_date
from src.paper_account import PaperAccount
from src.risk_gate import RiskGate
from src.broker import Broker

# 066570 일봉 종가(2026-07 ~ 09-08) 꼬리 — 09-08 확정봉까지.
# 066570 실제 일봉 종가 100개(마지막 = 사고 당일 09-09 진행 중 봉).
# 앞 99개(=09-08 확정봉까지)는 데드크로스가 아니고, 마지막 봉을 205,500/205,000
# 으로 덮는 순간 크로스가 켜진다 — 사고의 정확한 조건.
CLOSES = [
    122200, 126000, 124200, 124500, 126600, 132800, 129900, 127500,
    130000, 140000, 135800, 140900, 143200, 154900, 148700, 154100,
    156700, 184900, 191400, 217000, 240500, 217000, 191700, 181000,
    235000, 237000, 239500, 235000, 225500, 293000, 380500, 392500,
    328000, 303000, 268000, 248000, 224000, 226000, 225500, 243500,
    234000, 234500, 228500, 211500, 227500, 202000, 204500, 203000,
    195900, 196700, 203000, 193300, 192300, 191000, 185700, 189100,
    195800, 178000, 182400, 185600, 185400, 194000, 179000, 167200,
    173200, 182700, 187500, 169200, 172300, 157500, 150500, 148000,
    162100, 158800, 165500, 179500, 175200, 185000, 190000, 181700,
    205000, 206500, 215000, 208000, 201500, 202500, 194500, 199800,
    200500, 200500, 198300, 201500, 216500, 206500, 200500, 199700,
    201500, 214000, 205000, 204500,
]


class FakeGW:
    def __init__(self, rows):
        self._rows = rows

    def candles(self, sym, interval="1m", count=20):
        return self._rows


def _rows(closes, days):
    return [{"time": str(d.date()), "open": c, "high": c, "low": c,
             "close": c, "volume": 1000} for c, d in zip(closes, days)]


def _days(n):
    today = pd.Timestamp(trading_date("KR"))
    return [today - pd.Timedelta(days=n - 1 - i) for i in range(n)]


def _broker(tmp_path):
    acct = PaperAccount(cash={"KR": 10_000_000}, state_path=tmp_path / "pa.json")
    gate = RiskGate({"capital": {"KR": 5_000_000}, "max_order_notional": {},
                     "kill_switch_file": str(tmp_path / "HALT")})
    return Broker(account=acct, gate=gate, mode="paper")


def test_buy_price_already_satisfied_dead_cross():
    """사고의 전제 고정 — 체결가 205,500 에서 데드크로스가 이미 참이었다."""
    base = pd.Series([float(c) for c in CLOSES[:-1]])
    hit = {}
    for px in (206_500, 205_500, 205_000):
        s = pd.concat([base, pd.Series([float(px)])], ignore_index=True)
        m, sig, _ = macd_ind(s, 12, 26, 9)
        hit[px] = bool(crossed_down(m, sig))
    assert hit[205_500] is True and hit[205_000] is True   # 매수가·매도가 모두 참
    assert hit[206_500] is False                           # 조금 위였다면 아니었다


def test_dossier_entry_survives_dead_cross(tmp_path):
    """① 도시에 논거(zone)로 산 자리 — 데드크로스가 떠도 팔지 않고 강등한다."""
    store = Store(tmp_path / "t.db")
    broker = _broker(tmp_path)
    broker.account.fill("066570", "KR", "BUY", 1, 205_500)
    store.open_position(
        "066570", "KR", 1, 205_500, strategy="macd",
        stop_price=193_000, target_price=232_000,
        meta={"entry_basis": "zone", "horizon": "swing", "dossier_id": 1633,
              "params": {"fast": 12, "slow": 26, "signal": 9}})
    gw = FakeGW(_rows(CLOSES, _days(len(CLOSES))))
    sr = StrategyRunner(gw, broker, store,
                        cfg=SignalExitConfig(min_hold_sec=0, closed_bar_only=False))

    r = sr.evaluate(dict(store.get_open_positions()[0]), "KR", price=205_000)
    assert r["executed"] is False and r["demoted"] is True
    assert "데드크로스" in r["reason"]           # 신호는 났지만 집행되지 않았다
    assert broker.position("066570").qty == 1
    assert store.get_open_positions()[0]["stop_price"] == 193_000   # 무효화선은 살아있다


def test_closed_bar_ignores_intraday_wiggle(tmp_path):
    """② 확정봉 판정 — 미완성 당봉의 장중 흔들림으로는 크로스가 켜지지 않는다."""
    store = Store(tmp_path / "t.db")
    broker = _broker(tmp_path)
    broker.account.fill("066570", "KR", "BUY", 1, 205_500)
    store.open_position(
        "066570", "KR", 1, 205_500, strategy="macd",
        meta={"entry_basis": "signal", "horizon": "swing",
              "params": {"fast": 12, "slow": 26, "signal": 9}})
    # 마지막 봉 = 오늘(진행 중). 확정봉만 쓰면 이 봉은 판정에서 빠진다.
    gw = FakeGW(_rows(CLOSES, _days(len(CLOSES))))
    sr = StrategyRunner(gw, broker, store,
                        cfg=SignalExitConfig(min_hold_sec=0, closed_bar_only=True))

    r = sr.evaluate(dict(store.get_open_positions()[0]), "KR", price=205_000)
    assert r["executed"] is False
    assert broker.position("066570").qty == 1


def test_min_hold_blocks_same_tick_exit(tmp_path):
    """③ 전략 신호로 산 자리라도 진입 직후 반대신호는 집행하지 않는다."""
    store = Store(tmp_path / "t.db")
    broker = _broker(tmp_path)
    broker.account.fill("066570", "KR", "BUY", 1, 205_500)
    store.open_position(
        "066570", "KR", 1, 205_500, strategy="macd",
        meta={"entry_basis": "signal", "horizon": "swing",
              "params": {"fast": 12, "slow": 26, "signal": 9}})
    gw = FakeGW(_rows(CLOSES, _days(len(CLOSES))))
    sr = StrategyRunner(gw, broker, store,
                        cfg=SignalExitConfig(min_hold_sec=60, closed_bar_only=False))

    r = sr.evaluate(dict(store.get_open_positions()[0]), "KR", price=205_000)
    assert r["executed"] is False and "최소 보유" in r["reason"]
    assert broker.position("066570").qty == 1
