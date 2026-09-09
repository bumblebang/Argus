"""전략 실행기 — 보유 포지션에 배정된 전략을 코드가 직접 돌려 청산을 집행한다.

뇌(LLM)가 종목에 전략+파라미터를 배정하면(store.positions.strategy/meta.params), 이후
청산은 코드가 그 전략의 decide() 로 판단해 즉시 집행한다(빠른손). 가격터치 손절/익절
(ExitExecutor)에 더해 **신호기반 청산**(데드크로스·RSI 과매수·변동성돌파 익절 등)을 맡는다.

코드 자율 '진입'은 다음 증분(현재는 보유분의 전략기반 '청산'까지).

**신호 청산 권한은 진입 근거에 묶인다**(engine.entry_basis) — 전략 신호로 진입한
포지션(basis=signal)과 코드 소유 트랙(day/close_scan)만 신호로 청산한다. 도시에
진입존·뇌 논거·밸류로 산 포지션은 신호가 떠도 팔지 않고 `demoted` 를 돌려주고,
감시 루프가 뇌를 깨워 논거를 재평가하게 한다(triggers.strategy_signal_trigger).
"""
from __future__ import annotations

import json
import time

from ..logging_setup import get_logger
from ..store_fill import fill_event_payload
from ..runner import (candles_to_df, drop_unclosed_bar, order_price,
                      patch_live_price)
from ..risk_gate import Order
from ..strategies import build_strategy, REGISTRY
from ..strategies.base import Action, Position
from .entry_basis import SignalExitConfig, signal_exit_allowed

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
    def __init__(self, gateway, broker, store,
                 cfg: SignalExitConfig | None = None, now_fn=time.time) -> None:
        self.gw = gateway
        self.broker = broker
        self.store = store
        self.cfg = cfg or SignalExitConfig()
        self._now = now_fn

    def _held_sec(self, pos: dict) -> float | None:
        opened = pos.get("opened_at")
        if opened is None:
            return None
        try:
            return float(self._now()) - float(opened)
        except (TypeError, ValueError):
            return None

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
        allowed, basis = signal_exit_allowed(pos)
        if not self.cfg.bind_to_entry_basis:
            allowed = True                       # 롤백 스위치: 구 동작(라벨만 보고 청산)
        raw = self.gw.candles(sym, interval=strat.candle_interval,
                              count=strat.min_candles + _LOOKBACK_BUFFER)
        df = candles_to_df(raw)
        # 크로스형 신호는 확정봉으로만 — 미완성 당봉에 실시간가를 덮지 않는다.
        if self.cfg.closed_bar_only and strat.closed_bar_signal:
            df = drop_unclosed_bar(df, market)
        else:
            df = patch_live_price(df, price, market=market)
        if len(df) < strat.min_candles:
            return {"action": "hold", "executed": False, "reason": "캔들 부족"}

        position = Position(symbol=sym, qty=float(pos.get("qty", 0)),
                            avg_price=float(pos.get("avg_price", 0)))
        sig = strat.decide(df, position)
        if sig.action != Action.SELL or position.qty <= 0:
            return {"action": sig.action.value, "executed": False, "reason": sig.reason}

        # 진입 근거가 전략 신호가 아니면 청산하지 않고 뇌 각성으로 강등한다.
        if not allowed:
            log.info("신호 청산 강등 %s (%s, basis=%s): %s", sym, name, basis, sig.reason)
            return {"action": "sell", "executed": False, "demoted": True,
                    "strategy": name, "basis": basis, "reason": sig.reason}

        # 진입 직후 반대신호 방어 — 손절/목표(ExitExecutor)는 이 가드와 무관하다.
        held = self._held_sec(pos)
        if self.cfg.min_hold_sec > 0 and held is not None and held < self.cfg.min_hold_sec:
            return {"action": "sell", "executed": False, "held_sec": held,
                    "reason": f"최소 보유 미달({held:.0f}s < {self.cfg.min_hold_sec:.0f}s)"}

        exec_px = order_price(price, df)
        exit_reason = f"strategy:{name}"
        res = self.broker.execute_with_mirror(
            Order(sym, market, "SELL", position.qty, exec_px),
            reason=f"[strategy:{name}] {sig.reason}",
            store=self.store, exit_reason=exit_reason)
        if not res:
            why = res.reject_reason or getattr(self.broker, "last_reject_reason", None) or "gate_rejected"
            self.store.log_event("error", sym, {"where": "strategy_exit",
                                                "reason": why})
            return {"action": "sell", "executed": False, "reason": why}
        self.store.log_event("strategy_exit", sym, fill_event_payload(
            res, strategy=name, price=res.avg_price or exec_px, reason=sig.reason))
        log.info("전략청산 %s filled=%s @ %.2f (%s: %s)",
                 sym, res.filled_qty, res.avg_price or exec_px, name, sig.reason)
        return {"action": "sell", "executed": True, "reason": sig.reason}
