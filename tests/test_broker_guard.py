"""Broker 매수 안전가드(tradable_fn) 테스트.

핵심 불변식:
- tradable_fn 이 False 반환 → BUY 미체결(원장 무변화) + buy_blocked 이벤트.
- SELL 은 가드를 타지 않는다(청산=위험축소 허용).
- tradable_fn None(기본) → 기존대로 동작(하위호환).
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.broker import Broker
from src.paper_account import PaperAccount
from src.risk_gate import RiskGate, Order
from src.engine.store import Store


def _gate(tmp_path):
    return RiskGate({"capital": {"KR": 1_000_000}, "max_position_pct": 0.5,
                     "max_positions": 5, "daily_loss_limit_pct": 0.05,
                     "max_order_notional": {"KR": 500_000},
                     "kill_switch_file": str(tmp_path / "HALT")})


def _paper_broker(tmp_path, store=None, tradable_fn=None):
    acct = PaperAccount(cash={"KR": 1_000_000, "US": 100_000},
                        state_path=tmp_path / "pa.json")
    return Broker(account=acct, gate=_gate(tmp_path), mode="paper",
                  store=store, tradable_fn=tradable_fn)


def test_guard_blocks_buy_no_fill_and_event(tmp_path):
    store = Store(tmp_path / "t.db")
    b = _paper_broker(tmp_path, store=store,
                      tradable_fn=lambda s, m: (False, "부적격유형: ETF"))
    ok = b.execute(Order("069500", "KR", "BUY", 1, 30000.0), "test")
    assert ok is False
    assert b.account.position("069500").qty == 0        # 미체결
    assert b.account.journal == []                       # 원장 무변화
    assert b.last_reject_reason == "부적격유형: ETF"
    evs = store.recent_events("buy_blocked", 0)
    assert len(evs) == 1
    p = json.loads(evs[0]["payload"])
    assert p["symbol"] == "069500" and "ETF" in p["reason"]


def test_guard_allows_buy_when_ok(tmp_path):
    b = _paper_broker(tmp_path, tradable_fn=lambda s, m: (True, ""))
    ok = b.execute(Order("005930", "KR", "BUY", 1, 70000.0), "test")
    assert ok is True
    assert b.account.position("005930").qty == 1        # 체결됨


def test_guard_does_not_block_sell(tmp_path):
    # 먼저 보유를 만든 뒤(가드 통과 BUY), 부적격 판정으로 바꿔도 SELL 은 통과해야 한다.
    calls = []

    def guard(s, m):
        calls.append((s, m))
        return (False, "부적격유형: ETF")

    b = _paper_broker(tmp_path)
    b.tradable_fn = None
    b.execute(Order("005930", "KR", "BUY", 2, 70000.0), "seed")   # 보유 2주
    b.tradable_fn = guard                                          # 이제 부적격으로
    ok = b.execute(Order("005930", "KR", "SELL", 1, 71000.0), "exit")
    assert ok is True
    assert b.account.position("005930").qty == 1                  # 1주 청산됨
    assert calls == []                                            # SELL 은 가드 미호출


def test_guard_none_backward_compat(tmp_path):
    b = _paper_broker(tmp_path, tradable_fn=None)
    ok = b.execute(Order("005930", "KR", "BUY", 1, 70000.0), "test")
    assert ok is True
    assert b.account.position("005930").qty == 1


def test_guard_exception_fail_open(tmp_path):
    def boom(s, m):
        raise RuntimeError("guard exploded")

    b = _paper_broker(tmp_path, tradable_fn=boom)
    ok = b.execute(Order("005930", "KR", "BUY", 1, 70000.0), "test")
    assert ok is True                                             # fail-open → 체결
    assert b.account.position("005930").qty == 1
