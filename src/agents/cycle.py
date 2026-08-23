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
from .conviction import apply_buy_conviction, size_weight, min_lot_adjust
from ..logging_setup import get_logger
from ..risk_gate import Order

log = get_logger("agents.cycle")


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
              store=None,
              allow_add: bool = False) -> CycleResult:
    """결정→검증→집행. 데이트레(horizon='day') BUY 는 즉시 체결 대신 arm_fn 으로 라우팅.

    arm_fn(proposal, price)->bool 이 주어지면 day BUY 는 진입대기(armed)로 등록하고
    진입 타이밍은 감시 루프(코드)가 잡는다. swing/position 은 기존대로 즉시 진입.

    dossier_fn(symbol)->bool 이 주어지면 **도시에 우선 원칙**: 스윙/장투 BUY 는 신선한
    bullish 도시에가 있어야 집행(없으면 status='no_dossier' 차단). 데이트레(armed)는
    예외 — 빠른 기회는 전략 신호 경로가 담당한다. conviction_sizing=True 면 확신도가
    사이징에 반영된다(weight × (0.5+0.5×conviction) — 확신 낮으면 절반).

    min_lot_conviction 이 주어지면(예: 0.6) 그 확신도 이상 BUY 는 목표비중이 1주도
    못 살 때 최소 1주로 올린다(고단가 floor=0 구멍). RiskGate.allow_min_lot 과 짝.

    zone_fn(symbol)->dict|None 이 주어지면 **갭 진입 가드**(스윙/장투 BUY 한정): 현재가가
    도시에 진입존(entry_low~entry_high) 안이면 기존대로 즉시 시장가, 존 위(갭상승)면 시장가
    취소 후 arm_fn 으로 존 재진입 대기 등록(status='gap_armed'), 존 아래·무효화가 위면
    존 회복 대기(gap_armed), 무효화가 하회면 진입 거부(gap_rejected). zone 정보가 없으면
    (도시에 없음/레벨 결손/가드 꺼짐) 기존 동작 그대로(하위호환). arm_fn 은 zone 을 함께
    넘겨(arm_fn(p, price, zone=levels)) 감시 루프가 존 기준으로 진입/해제하게 한다.

    apply_code_conviction=True 면 BUY 확신도를 코드 루브릭으로 덮어쓴 뒤 검증에 넘긴다
    (LLM 자가채점은 사이징에 쓰지 않음). dossier_brief_fn(symbol)->dict 와
    features_by_sym 이 가감 입력. 연속량은 부호×강도로 W_* 한도 안에 접고,
    희석·법적 공시·실적 미스·적자만 계단 감점한다.
    """
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
    for p in decision.proposals:
        if p.side == "HOLD":
            continue
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

        # 데이트레 BUY: 코드 자율 진입(armed). 뇌는 종목/전략/파라미터만 배정.
        if p.side == "BUY" and arm_fn and (p.horizon or "").lower() == "day":
            armed = bool(arm_fn(p, price))
            executed.append({"symbol": p.symbol, "action": "BUY",
                             "status": "armed" if armed else "arm_skipped",
                             "reason": p.thesis[:80]})
            continue

        # 도시에 우선 원칙: 스윙/장투 신규매수는 신선 bullish 도시에 필수(코드 하드가드).
        if (p.side == "BUY" and dossier_fn
                and (p.horizon or "swing").lower() != "day" and not dossier_fn(p.symbol)):
            executed.append({"symbol": p.symbol, "action": "BUY", "status": "no_dossier",
                             "reason": "신선한 bullish 도시에 없음 — 스윙/장투 신규매수 차단"})
            continue

        # 갭 진입 가드(스윙/장투 BUY): 진입은 오직 진입존 안에서만. 갭상승/존이탈은 시장가
        # 추격 대신 존 재진입 대기(arm)로 라우팅하고, thesis 깨진 자리(무효화가 하회)는 거부.
        levels = (zone_fn(p.symbol) if (zone_fn and p.side == "BUY"
                                        and (p.horizon or "swing").lower() != "day") else None)
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
            weight = size_weight(p.target_weight, p.conviction,
                                 enabled=conviction_sizing)
            cap = float(getattr(risk, "capital", {}).get(p.market, 0) or 0)
            weight, min_qty = min_lot_adjust(
                weight, price=price, capital=cap, conviction=p.conviction,
                min_lot_conviction=min_lot_conviction)
            if min_qty > 0 and cap > 0:
                p.target_weight = min(
                    1.0, max(float(p.target_weight), price / cap))
            qty = risk.size_buy(p.market, price, weight, min_qty=min_qty)
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
