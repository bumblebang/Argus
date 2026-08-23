"""전략 실행기 — 보유 포지션에 배정된 전략을 코드가 직접 돌려 청산을 집행한다.

뇌(LLM)가 종목에 전략+파라미터를 배정하면(store.positions.strategy/meta.params), 이후
청산은 코드가 그 전략의 decide() 로 판단해 즉시 집행한다(빠른손). 가격터치 손절/익절
(ExitExecutor)에 더해 **신호기반 청산**(데드크로스·RSI 과매수·변동성돌파 익절 등)을 맡는다.

코드 자율 '진입'은 다음 증분(현재는 보유분의 전략기반 '청산'까지).
"""
from __future__ import annotations

import json

from ..logging_setup import get_logger
from ..runner import candles_to_df, patch_live_price
from ..risk_gate import Order
from ..strategies import build_strategy, REGISTRY
from ..strategies.base import Action, Position

log = get_logger("engine.strategy_runner")

_LOOKBACK_BUFFER = 10        # min_candles 위에 여유분(지표 안정화)


def _params_of(pos: dict) -> dict:
    meta = pos.get("meta")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except (ValueError, TypeError):
            meta = {}
    return (meta or {}).get("params", {}) if isinstance(meta, dict) else {}


class StrategyRunner:
    def __init__(self, gateway, broker, store) -> None:
        self.gw = gateway
        self.broker = broker
        self.store = store

    def evaluate(self, pos: dict, market: str, price: float | None = None) -> dict:
        """보유 포지션 1건에 배정 전략을 적용. SELL 신호면 청산 집행.

        price(실시간가) 주어지면 캐시 캔들 마지막봉을 패치해 1초 반응성 확보.
        반환: {action, executed, reason}. (감시 루프가 호출, 결과는 로깅/테스트용)
        """
        sym = pos.get("symbol")
        name = pos.get("strategy")
        if not name or name not in REGISTRY:
            return {"action": "skip", "executed": False, "reason": "전략 미배정"}
        strat = build_strategy(name, _params_of(pos))
        raw = self.gw.candles(sym, interval=strat.candle_interval,
                              count=strat.min_candles + _LOOKBACK_BUFFER)
        df = patch_live_price(candles_to_df(raw), price)
        if len(df) < strat.min_candles:
            return {"action": "hold", "executed": False, "reason": "캔들 부족"}

        position = Position(symbol=sym, qty=float(pos.get("qty", 0)),
                            avg_price=float(pos.get("avg_price", 0)))
        sig = strat.decide(df, position)
        if sig.action != Action.SELL or position.qty <= 0:
            return {"action": sig.action.value, "executed": False, "reason": sig.reason}

        price = float(df["close"].iloc[-1])
        ok = self.broker.execute(Order(sym, market, "SELL", position.qty, price),
                                 reason=f"[strategy:{name}] {sig.reason}")
        if not ok:
            why = (getattr(self.broker, "last_reject_reason", None)
                   or "gate_rejected")
            self.store.log_event("error", sym, {"where": "strategy_exit",
                                                "reason": why})
            return {"action": "sell", "executed": False, "reason": why}
        for row in self.store.get_open_positions():
            if row["symbol"] == sym:
                self.store.close_position(row["id"], exit_price=price,
                                          reason=f"strategy:{name}")
        self.store.log_event("strategy_exit", sym,
                             {"strategy": name, "price": price, "reason": sig.reason})
        log.info("전략청산 %s x%s @ %.2f (%s: %s)", sym, position.qty, price, name, sig.reason)
        return {"action": "sell", "executed": True, "reason": sig.reason}
