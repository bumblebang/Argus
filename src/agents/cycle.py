"""에이전트 1사이클: 결정 -> 검증 -> 하드 게이트 -> 페이퍼 집행 + 결정 저널.

흐름:
  결정 에이전트가 제안 -> 검증 에이전트가 독립 검토(거부권) -> 승인된 것만
  주문으로 변환 -> broker.execute()가 하드 리스크 게이트로 최종 검증 후 페이퍼 집행.
모든 단계의 근거(thesis/verdict)를 data/decisions.jsonl에 남긴다.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .schemas import DecisionOutput, ValidationOutput
from .conviction import (
    apply_buy_conviction, size_weight, min_lot_adjust, skip_position_headroom,
)
from .features import GAP_SCAN_REASONS, wake_has_gap_scan
from ..logging_setup import get_logger
from ..risk_gate import Order
from .. import paths as _paths

log = get_logger("agents.cycle")


def _resolve_market(market_fn, symbol: str) -> str | None:
    """코드 권위 market 조회. 예외는 '모름'으로 본다(거부 쪽이 안전)."""
    try:
        m = market_fn(symbol)
    except Exception as e:
        log.warning("market 조회 실패 %s: %s", symbol, e)
        return None
    return str(m) if m else None


def _already_held(broker, store, symbol: str) -> bool:
    """broker 원장 또는 store open 행 — 피라미딩 차단(allow_add=False 일 때)."""
    if broker.position(symbol).qty > 0:
        return True
    if store is not None:
        return any(r["symbol"] == symbol for r in store.get_open_positions())
    return False


@dataclass
class CycleResult:
    decision: DecisionOutput
    validation: ValidationOutput
    executed: list[dict]    # 집행/시도 결과 [{symbol, action, status, reason}]
    cycle_ts: float = 0.0           # 저널·그림자장부 dedup 키 (epoch)
    cycle_ts_iso: str = ""          # decisions.jsonl ts 필드
    manager: dict | None = None     # model/prompt_hash 매니저 정체성


def _fresh_gap_pool_member(f: dict, *, market: str = "KR") -> bool:
    """당일 pool_date 가 있는 gap_decline/gap_rebound 만 active 갭 트랙."""
    if f.get("pool") != "gap_decline" and f.get("source") != "gap_rebound":
        return False
    pd = f.get("pool_date")
    if not pd:
        return False
    try:
        from ..market_hours import trading_date
        mkt = str(f.get("market") or market or "KR").upper()
        return str(pd) == trading_date(mkt)
    except Exception:
        return False


def _close_scan_candidate(symbol: str, features_by_sym: dict | None) -> bool:
    """오늘 pool_date gap_decline 풀 — close_scan 트랙 후보."""
    f = (features_by_sym or {}).get(symbol) or {}
    return _fresh_gap_pool_member(f)


def _apply_close_scan_horizon(p, *, wake_reason: str,
                              features_by_sym: dict | None) -> str | None:
    """갭반등 close_scan 트랙 BUY 의 horizon 정규화. 거부 시 status 문자열."""
    if p.side != "BUY":
        return None
    gap_wake = wake_has_gap_scan(wake_reason)
    gap_pool = _close_scan_candidate(p.symbol, features_by_sym)
    hz = (p.horizon or "swing").lower()
    if not gap_wake:
        if gap_pool or hz == "close_scan":
            return "wrong_horizon"
        return None
    if not gap_pool:
        if hz == "close_scan":
            return "wrong_horizon"
        return None
    if hz == "day":
        return "wrong_horizon"
    if hz == "close_scan":
        return None
    log.warning("[close_scan 교정] %s: horizon %s → close_scan", p.symbol, hz)
    p.horizon = "close_scan"
    return None


def run_cycle(*, context_json: str, decision_agent, validation_agent, broker, risk,
              price_lookup: dict[str, float],
              journal_path: str | Path = "data/decisions.jsonl",
              arm_fn=None, dossier_fn=None, zone_fn=None,
              entry_zone_tolerance_pct: float = 0.005,
              conviction_sizing: bool = False,
              min_lot_conviction: float | None = None,
              apply_code_conviction: bool = False,
              dossier_brief_fn=None,
              features_by_sym: dict | None = None,
              market_fn=None,
              store=None,
              allow_add: bool = False,
              tranche_weights: dict[str, float] | None = None,
              budget_caps: dict[str, float] | None = None,
              wake_reason: str = "") -> CycleResult:
    """결정→검증→집행. 데이트레(horizon='day') BUY 는 즉시 체결 대신 arm_fn 으로 라우팅.

    arm_fn(proposal, price)->bool 이 주어지면 day BUY 는 진입대기(armed)로 등록하고
    진입 타이밍은 감시 루프(코드)가 잡는다. swing/position 은 기존대로 즉시 진입.

    dossier_fn(symbol)->bool 이 주어지면 **도시에 우선 원칙**: 스윙/장투 BUY 는 신선한
    bullish 도시에가 있어야 집행(없으면 status='no_dossier' 차단). 데이트레(armed)는
    예외 — 빠른 기회는 전략 신호 경로가 담당한다. conviction_sizing=True 면 확신도가
    사이징에 소폭 반영된다(base×(floor+span×c), config risk.*).

    min_lot_conviction 이 주어지면(예: 0.6) 그 확신도 이상 BUY 는 목표비중이 1주도
    못 살 때 최소 1주로 올린다(고단가 floor=0 구멍). RiskGate.allow_min_lot 과 짝.

    tranche_weights: 심볼→회차 비중(밸류 분할). budget_caps: 심볼→명목 상한(슬리브 room).
    LLM target_weight 는 사이징에 쓰지 않는다(저널용으로만 남을 수 있음).

    zone_fn(symbol)->dict|None 이 주어지면 **갭 진입 가드**(스윙/장투 BUY 한정): 현재가가
    도시에 진입존(entry_low~entry_high) 안이면 기존대로 즉시 시장가, 존 위(갭상승)면 시장가
    취소 후 arm_fn 으로 존 재진입 대기 등록(status='gap_armed'), 존 아래·무효화가 위면
    존 회복 대기(gap_armed), 무효화가 하회면 진입 거부(gap_rejected). zone 정보가 없으면
    (도시에 없음/레벨 결손/가드 꺼짐) 기존 동작 그대로(하위호환). arm_fn 은 zone 을 함께
    넘겨(arm_fn(p, price, zone=levels)) 감시 루프가 존 기준으로 진입/해제하게 한다.

    market_fn(symbol)->str|None 이 주어지면 **market 은 코드 권위**: proposal.market 을
    코드가 아는 시장(유니버스·원장)으로 덮어쓰고, 코드가 모르는 심볼은 거부한다
    (status='market_unknown'). LLM 라벨은 스키마 검증만 받을 뿐 실제 시장과 대조되지
    않는데, 그 값이 자본 풀·한도 분모·live 집행 판정을 바꾼다.

    apply_code_conviction=True 면 BUY 확신도를 코드 루브릭으로 덮어쓴 뒤 검증에 넘긴다
    (LLM 자가채점은 사이징에 쓰지 않음). dossier_brief_fn(symbol)->dict 와
    features_by_sym 이 가감 입력. 연속량은 부호×강도로 W_* 한도 안에 접고,
    희석·법적 공시·실적 미스·적자만 계단 감점한다.
    """
    journal_path = _paths.resolve("decisions", configured=journal_path)
    decision = decision_agent.decide(context_json)
    conv_audit: dict = {}
    if apply_code_conviction:
        conv_audit = apply_buy_conviction(
            decision, price_lookup, dossier_brief_fn,
            zone_tol=entry_zone_tolerance_pct,
            features_by_sym=features_by_sym)
        log.info("확신도 코드 %s", conv_audit)
    validation = validation_agent.review(context_json, decision)
    verdict_by_sym = {v.symbol: v for v in validation.verdicts}

    executed: list[dict] = []
    try:
        for p in decision.proposals:
            if p.side == "HOLD":
                continue
            # market 은 코드 권위 — LLM 라벨을 믿지 않는다. 자본 풀·한도 분모·live
            # 집행 판정이 전부 이 값에 달려 있어, 틀린 라벨 하나가 과대 사이징이나
            # 청산 불능(live_markets 밖 스킵)을 만든다.
            if market_fn is not None:
                true_market = _resolve_market(market_fn, p.symbol)
                if true_market is None:
                    executed.append({"symbol": p.symbol, "action": p.side,
                                     "status": "market_unknown",
                                     "reason": "코드가 시장을 모름(유니버스·원장에 없음)"})
                    continue
                if true_market != p.market:
                    log.warning("[market 교정] %s: 제안 %s → 코드 %s",
                                p.symbol, p.market, true_market)
                    if store is not None:
                        try:
                            store.log_event("market_mismatch", p.symbol,
                                            {"proposed": p.market, "resolved": true_market,
                                             "side": p.side})
                        except Exception:
                            pass
                    p.market = true_market

            v = verdict_by_sym.get(p.symbol)
            if v is None or not v.approved:
                executed.append({"symbol": p.symbol, "action": p.side, "status": "vetoed",
                                 "reason": v.reason if v else "검증 결과 없음"})
                continue
            price = price_lookup.get(p.symbol)
            if not price or price <= 0:
                executed.append({"symbol": p.symbol, "action": p.side, "status": "no_price",
                                 "reason": "가격 미확보"})
                continue

            hz_err = _apply_close_scan_horizon(
                p, wake_reason=str(wake_reason or ""),
                features_by_sym=features_by_sym)
            if hz_err:
                executed.append({"symbol": p.symbol, "action": p.side, "status": hz_err,
                                 "reason": "갭반등은 horizon=close_scan 전용(day/swing 금지)"})
                continue

            # 데이트레 BUY: 코드 자율 진입(armed). 뇌는 종목/전략/파라미터만 배정.
            if p.side == "BUY" and arm_fn and (p.horizon or "").lower() == "day":
                armed = bool(arm_fn(p, price))
                executed.append({"symbol": p.symbol, "action": "BUY",
                                 "status": "armed" if armed else "arm_skipped",
                                 "reason": p.thesis[:80]})
                continue

            # 도시에 우선 원칙: 스윙/장투 신규매수는 신선 bullish 도시에 필수(코드 하드가드).
            # close_scan(갭반등)·day 는 별도 트랙 — dossier 면제.
            _hz = (p.horizon or "swing").lower()
            if (p.side == "BUY" and dossier_fn
                    and _hz not in ("day", "close_scan") and not dossier_fn(p.symbol)):
                executed.append({"symbol": p.symbol, "action": "BUY", "status": "no_dossier",
                                 "reason": "신선한 bullish 도시에 없음 — 스윙/장투 신규매수 차단"})
                continue

            # 갭 진입 가드(스윙/장투 BUY): close_scan·day 제외
            levels = (zone_fn(p.symbol) if (zone_fn and p.side == "BUY"
                                            and _hz not in ("day", "close_scan")) else None)
            if levels:
                lo, hi = levels["entry_low"], levels["entry_high"]
                inval = levels["invalidation"]
                hi_tol = hi * (1 + entry_zone_tolerance_pct)
                if lo <= price <= hi_tol:
                    pass                              # 존 안 → 기존대로 즉시 시장가 진입
                elif price > hi_tol:                  # 존 위(갭상승) → 존 재진입 대기
                    armed = bool(arm_fn(p, price, zone=levels)) if arm_fn else False
                    executed.append({"symbol": p.symbol, "action": "BUY",
                                     "status": "gap_armed" if armed else "arm_skipped",
                                     "reason": f"갭 위 — 존 재진입 대기 (p {price:g} > high {hi:g})"})
                    continue
                elif inval <= price < lo:             # 존 아래·무효화가 위 → 존 회복 대기
                    armed = bool(arm_fn(p, price, zone=levels)) if arm_fn else False
                    executed.append({"symbol": p.symbol, "action": "BUY",
                                     "status": "gap_armed" if armed else "arm_skipped",
                                     "reason": f"존 아래 — 회복 대기 (p {price:g} < low {lo:g})"})
                    continue
                else:                                 # 무효화가 하회 → 이미 thesis 깨짐, 진입 거부
                    executed.append({"symbol": p.symbol, "action": "BUY", "status": "gap_rejected",
                                     "reason": f"무효화가 하회 — 진입 거부 "
                                               f"(p {price:g} < inval {inval:g})"})
                    continue

            if p.side == "BUY" and not allow_add and _already_held(broker, store, p.symbol):
                executed.append({"symbol": p.symbol, "action": "BUY", "status": "already_held",
                                 "reason": "already holds position"})
                continue

            if p.side == "BUY":
                # 코드 기본비중(config) — LLM target_weight 무시. 확신도는 소폭 ±만.
                def _rf(name: str, default: float) -> float:
                    try:
                        v = getattr(risk, name, None)
                        return float(v) if v is not None else default
                    except (TypeError, ValueError):
                        return default

                base_pct = _rf("base_position_pct", _rf("max_position_pct", 0.20))
                floor = _rf("conviction_size_floor", 0.75)
                span = _rf("conviction_size_span", 0.25)
                hard_cap = _rf("max_position_pct", 0.25)
                weight = size_weight(
                    base_pct, p.conviction, enabled=conviction_sizing,
                    floor=floor, span=span, cap=hard_cap)
                tr_w = (tranche_weights or {}).get(p.symbol)
                if tr_w is not None:
                    try:
                        weight = min(weight * float(tr_w), hard_cap)
                    except (TypeError, ValueError):
                        pass
                try:
                    equity = (risk.sizing_base_amount(broker, p.market)
                              if hasattr(risk, "sizing_base_amount")
                              else float(getattr(risk, "capital", {}).get(p.market, 0) or 0))
                    equity = float(equity)
                except (TypeError, ValueError):
                    equity = 0.0
                # 종목 잔여 한도 + 선택적 슬리브/예산 캡
                pos = broker.position(p.symbol)
                try:
                    cur_notional = float(pos.qty) * float(price)
                except (TypeError, ValueError):
                    cur_notional = 0.0
                headroom = max(0.0, equity * hard_cap - cur_notional)
                extra = (budget_caps or {}).get(p.symbol)
                weight, min_qty = min_lot_adjust(
                    weight, price=price, capital=equity, conviction=p.conviction,
                    min_lot_conviction=min_lot_conviction)
                caps: list[float] = []
                if not skip_position_headroom(min_qty):
                    caps.append(headroom)
                if extra is not None:
                    try:
                        caps.append(float(extra))
                    except (TypeError, ValueError):
                        pass
                notional_cap = min(caps) if caps else None
                # 저널용: 실제 쓴 비중을 target_weight 에 기록(LLM 값 덮어씀)
                p.target_weight = float(weight)
                if min_qty > 0 and equity > 0:
                    p.target_weight = min(
                        1.0, max(float(p.target_weight), price / equity))
                qty = risk.size_buy(
                    p.market, price, weight, min_qty=min_qty,
                    base_equity=equity, notional_cap=notional_cap)
            else:  # SELL: 보유 수량 전량
                qty = broker.position(p.symbol).qty
            exit_reason = "brain" if p.side == "SELL" else None
            res = broker.execute(
                Order(p.symbol, p.market, p.side, qty, price),
                reason=f"[agent] {p.thesis[:60]}",
                store=store, exit_reason=exit_reason)
            if res.partial:
                st = "partial"
            elif res.ok:
                st = "filled"
            else:
                st = "gate_rejected"
            if st == "filled" or st == "partial":
                exec_reason = p.thesis[:80]
            else:
                exec_reason = (res.reject_reason
                               or getattr(broker, "last_reject_reason", None)
                               or "리스크게이트 거부")
            executed.append({"symbol": p.symbol, "action": p.side,
                             "status": st, "reason": exec_reason,
                             "avg_price": res.avg_price,
                             "filled_qty": res.filled_qty if res.ok else 0.0})

    except Exception:
        # 부분 체결이 이미 paper 에 남았을 수 있음 — 저널만이라도 남기고 재전파.
        log.exception("사이클 집행 중 예외 — 지금까지 %d건 저널 후 재전파", len(executed))
        cycle_ts = time.time()
        cycle_ts_iso = datetime.fromtimestamp(cycle_ts, tz=timezone.utc).isoformat()
        try:
            _journal(journal_path, decision, validation, executed, conv_audit=conv_audit,
                     cycle_ts_iso=cycle_ts_iso, manager=None, archive_meta=None)
        except Exception:
            log.exception("예외 경로 저널 실패")
        raise

    cycle_ts = time.time()
    cycle_ts_iso = datetime.fromtimestamp(cycle_ts, tz=timezone.utc).isoformat()
    from .manager_id import manager_snapshot
    dec_prompt = getattr(decision_agent, "SYSTEM", "") or ""
    val_prompt = getattr(validation_agent, "SYSTEM", "") or ""
    # 모듈에 모듈 상수로 있을 수 있음
    if not dec_prompt:
        from . import decision_agent as _da
        dec_prompt = getattr(_da, "SYSTEM", "") or ""
    if not val_prompt:
        from . import validation_agent as _va
        val_prompt = getattr(_va, "SYSTEM", "") or ""
    manager = manager_snapshot(
        decision_llm=getattr(decision_agent, "llm", None),
        validation_llm=getattr(validation_agent, "llm", None),
        decision_prompt=dec_prompt,
        validation_prompt=val_prompt,
    )
    archive_meta = _archive_context(context_json, cycle_ts, journal_path, manager)
    _journal(journal_path, decision, validation, executed, conv_audit=conv_audit,
             cycle_ts_iso=cycle_ts_iso, manager=manager, archive_meta=archive_meta)
    log.info("사이클 완료: 집행시도 %d건 epoch=%s", len(executed), manager.get("epoch"))
    return CycleResult(decision, validation, executed,
                       cycle_ts=cycle_ts, cycle_ts_iso=cycle_ts_iso, manager=manager)


def _archive_context(context_json: str, cycle_ts: float, journal_path: str | Path,
                     manager: dict | None) -> dict | None:
    """입력 컨텍스트를 gzip 아카이브. 실패해도 사이클은 계속."""
    try:
        from ..eval.archive import persist_context
        return persist_context(
            context_json, cycle_ts=cycle_ts, journal_path=journal_path,
            manager=manager)
    except Exception as e:
        log.warning("컨텍스트 아카이브 실패(사이클 계속): %s", e)
        return None


def _journal(path: str | Path, decision: DecisionOutput, validation: ValidationOutput,
             executed: list[dict], conv_audit: dict | None = None,
             cycle_ts_iso: str | None = None, manager: dict | None = None,
             archive_meta: dict | None = None) -> None:
    rec = {
        "ts": cycle_ts_iso or datetime.now(timezone.utc).isoformat(),
        "market_view": decision.market_view,
        "proposals": [p.model_dump() for p in decision.proposals],
        "verdicts": [v.model_dump() for v in validation.verdicts],
        "executed": executed,
    }
    if conv_audit:
        rec["conviction_code"] = conv_audit
    if manager:
        rec["manager"] = manager
    if archive_meta:
        for k in ("context_ref", "context_sha256", "context_bytes"):
            if k in archive_meta:
                rec[k] = archive_meta[k]
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
