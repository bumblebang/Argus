"""코드 진입/청산 실행기 — 감시 루프(빠른손)가 뇌를 거치지 않고 즉시 집행.

청산(ExitExecutor): 손절/익절 트리거 시 보유 전량 즉시 매도.
진입(EntryExecutor): 뇌가 배정한 진입대기(armed) 종목의 캔들에 전략 decide() 를 돌려
  BUY 신호가 뜨면 그때 매수한다 — "뇌가 BUY→즉시 시장가"가 아니라 코드가 진입 타이밍을
  잡는다(데이트레: 종목/전략/파라미터는 LLM, 진입/청산은 코드). ExitExecutor 의 대칭.

손절·익절·진입 모두 하드 게이트(broker.execute)를 그대로 통과한다.
페이퍼/라이브는 broker.mode 와 DRY_RUN 이 정한다.
"""
from __future__ import annotations

import json
import time

from ..logging_setup import get_logger
from ..risk_gate import Order
from ..runner import candles_to_df, patch_live_price
from ..strategies import build_strategy, REGISTRY
from ..strategies.base import Action, Position
from ..agents.conviction import size_weight, min_lot_adjust

log = get_logger("engine.execution")

_LOOKBACK_BUFFER = 10        # min_candles 위에 여유분(지표 안정화)


def _meta_of(pos: dict) -> dict:
    meta = pos.get("meta")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except (ValueError, TypeError):
            meta = {}
    return meta if isinstance(meta, dict) else {}


def _entry_weight(meta: dict, sig_weight=None) -> float | None:
    """armed meta 의 목표비중.

    스윙/장투 갭대기는 즉시 체결과 같은 conviction_sizing 식을 쓴다.
    데이트레(day)는 사이클이 사이징 전에 arm 하므로 비중을 그대로 둔다.
    옛 armed 행(conviction 키 없음)도 비중 그대로.
    """
    raw = sig_weight if sig_weight is not None else meta.get("target_weight")
    if raw is None:
        return None
    hz = str(meta.get("horizon") or "day").lower()
    enabled = bool(meta.get("conviction_sizing")) and hz != "day"
    return size_weight(float(raw), meta.get("conviction"), enabled=enabled)


def _entry_lot(meta: dict, market: str, price: float, risk, sig_weight=None
               ) -> tuple[float | None, float]:
    """갭대기 스윙은 즉시 체결과 같은 min_lot. 데이트레·옛 행은 수량 하한 없음."""
    weight = _entry_weight(meta, sig_weight)
    if weight is None:
        return None, 0.0
    hz = str(meta.get("horizon") or "day").lower()
    cap = float(getattr(risk, "capital", {}).get(market, 0) or 0)
    return min_lot_adjust(
        weight, price=price, capital=cap,
        conviction=meta.get("conviction"),
        min_lot_conviction=meta.get("min_lot_conviction"),
        enabled=(hz != "day"))


class ExitExecutor:
    def __init__(self, broker, store) -> None:
        self.broker = broker
        self.store = store

    def __call__(self, symbol: str, market: str, price: float | None, trigger) -> bool:
        """보유 전량 시장 청산. 체결되면 True. (감시 루프가 트리거 시 호출)"""
        pos = self.broker.position(symbol)
        if pos.qty <= 0 or not price or price <= 0:
            return False
        kind = getattr(trigger, "kind", "exit")
        ok = self.broker.execute(Order(symbol, market, "SELL", pos.qty, price),
                                 reason=f"[exit] {kind}")
        if not ok:
            why = (getattr(self.broker, "last_reject_reason", None)
                   or "gate_rejected")
            self.store.log_event("error", symbol,
                                 {"where": "exit", "kind": kind, "reason": why})
            return False
        for row in self.store.get_open_positions():
            if row["symbol"] == symbol:
                self.store.close_position(row["id"], exit_price=price, reason=kind)
        self.store.log_event("exit", symbol,
                             {"kind": kind, "price": price, "qty": pos.qty})
        log.info("코드 청산 %s x%s @ %.2f (%s)", symbol, pos.qty, price, kind)
        return True


class EntryExecutor:
    """진입대기(armed) 종목에 배정 전략을 적용해 BUY 신호 시 코드가 진입한다.

    뇌가 (종목·전략·파라미터)를 store.arm_candidate 로 배정 → 감시 루프가 매 틱 이
    실행기를 호출 → 캔들에 strategy.decide() → BUY 면 사이징·하드게이트 통과 후 매수하고
    armed→open 으로 승격(진입가 기준 손절/목표 확정). StrategyRunner(청산)의 진입 대칭.

    plan_fn(entry_price, horizon, params) -> (stop, target) 주입 시 진입가·전략 파라미터
    기준으로 손절/목표를 확정한다(보통 pipeline.entry_stop_target). 전략은 armed 행에 이미
    배정돼 있으므로(뇌 선택), 여기선 손절/목표만 계산한다.
    """

    def __init__(self, gateway, broker, risk, store, plan_fn=None) -> None:
        self.gw = gateway
        self.broker = broker
        self.risk = risk
        self.store = store
        self.plan_fn = plan_fn

    def evaluate(self, armed: dict, market: str, price: float | None = None) -> dict:
        """진입대기 1건에 전략 적용. BUY 신호면 진입 집행. 반환: {action, executed, reason}.

        price(실시간가) 주어지면 캐시 캔들의 마지막봉 종가를 패치해 1초 반응성 확보.
        meta 에 entry_zone(갭 진입 가드) 이 있으면 전략 신호 대신 도시에 진입존/무효화가
        기준으로 진입/해제를 판정한다(_evaluate_zone).
        """
        meta = _meta_of(armed)
        zone = meta.get("entry_zone")
        if zone:
            return self._evaluate_zone(armed, market, price, meta, zone)

        sym = armed.get("symbol")
        name = armed.get("strategy")
        if not name or name not in REGISTRY:
            return {"action": "skip", "executed": False, "reason": "전략 미배정"}
        strat = build_strategy(name, meta.get("params", {}))
        raw = self.gw.candles(sym, interval=strat.candle_interval,
                              count=strat.min_candles + _LOOKBACK_BUFFER)
        df = patch_live_price(candles_to_df(raw), price)
        if len(df) < strat.min_candles:
            return {"action": "hold", "executed": False, "reason": "캔들 부족"}

        sig = strat.decide(df, Position(symbol=sym, qty=0.0, avg_price=0.0))
        if sig.action != Action.BUY:
            return {"action": sig.action.value, "executed": False, "reason": sig.reason}

        price = float(df["close"].iloc[-1])
        weight, min_qty = _entry_lot(meta, market, price, self.risk, sig.target_weight)
        qty = self.risk.size_buy(market, price, weight, min_qty=min_qty)
        if qty <= 0:
            return {"action": "buy", "executed": False, "reason": "사이징 0"}

        ok = self.broker.execute(Order(sym, market, "BUY", qty, price),
                                 reason=f"[entry:{name}] {sig.reason}")
        if not ok:
            why = (getattr(self.broker, "last_reject_reason", None)
                   or "gate_rejected")
            self.store.log_event("error", sym, {"where": "entry", "reason": why})
            return {"action": "buy", "executed": False, "reason": why}

        horizon = meta.get("horizon", "day")
        if self.plan_fn:
            stop, target = self.plan_fn(price, horizon, meta.get("params"))
        else:
            stop = target = None
        self.store.promote_armed(armed["id"], qty, price,
                                 target_price=target, stop_price=stop)
        from ..shadow_ledger import cancel_shadow_on_fill
        cancel_shadow_on_fill(self.store, sym)
        self.store.log_event("entry", sym,
                             {"strategy": name, "price": price, "qty": qty,
                              "reason": sig.reason})
        log.info("코드 진입 %s x%s @ %.2f (%s: %s)", sym, qty, price, name, sig.reason)
        return {"action": "buy", "executed": True, "reason": sig.reason}

    def _evaluate_zone(self, armed: dict, market: str, price: float | None,
                       meta: dict, zone: dict) -> dict:
        """갭 진입 가드 모드: 도시에 진입존/무효화가/만료 기준 진입·해제 판정.

        캔들 대신 감시층이 배치 폴링한 라이브가(price 인자)만 쓴다 — 존 판정에 캔들이
        불필요하고, gateway.candles 호출은 CHART 예산을 쓰므로 여기선 호출하지 않는다.
        """
        sym = armed.get("symbol")
        if not price or price <= 0:
            return {"action": "hold", "executed": False, "reason": "가격 미확보(존 모드)"}

        if price < zone["invalidation"]:
            self.store.close_position(armed["id"], reason="disarm:invalidated")
            self.store.log_event("disarm", sym, {"reason": "invalidated", "price": price})
            return {"action": "disarm", "executed": False, "reason": "무효화가 하회"}

        expires_at = zone.get("expires_at")
        if expires_at and time.time() > expires_at:
            self.store.close_position(armed["id"], reason="disarm:dossier_expired")
            self.store.log_event("disarm", sym, {"reason": "dossier_expired", "price": price})
            return {"action": "disarm", "executed": False, "reason": "도시에 만료"}

        if zone["low"] <= price <= zone["high"]:
            weight, min_qty = _entry_lot(meta, market, price, self.risk)
            qty = self.risk.size_buy(market, price, weight, min_qty=min_qty)
            if qty <= 0:
                return {"action": "buy", "executed": False, "reason": "사이징 0"}
            ok = self.broker.execute(Order(sym, market, "BUY", qty, price),
                                     reason="[entry:zone] 존 진입")
            if not ok:
                why = (getattr(self.broker, "last_reject_reason", None)
                       or "gate_rejected")
                self.store.log_event("error", sym, {"where": "entry", "reason": why})
                return {"action": "buy", "executed": False, "reason": why}
            self.store.promote_armed(armed["id"], qty, price,
                                     target_price=zone.get("target"),
                                     stop_price=zone["invalidation"])
            from ..shadow_ledger import cancel_shadow_on_fill
            cancel_shadow_on_fill(self.store, sym)
            self.store.log_event("entry", sym,
                                 {"strategy": armed.get("strategy"), "price": price, "qty": qty,
                                  "reason": "존 진입"})
            log.info("코드 진입(존) %s x%s @ %.2f", sym, qty, price)
            return {"action": "buy", "executed": True, "reason": "존 진입"}

        return {"action": "hold", "executed": False, "reason": "존 밖 대기"}
