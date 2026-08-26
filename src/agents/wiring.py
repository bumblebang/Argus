"""LLM/브로커 배선 헬퍼 — CycleRunner 와 분리 (Phase 1).

build_paper_core · select_backend · bridge/LLM 팩토리 · 전략·손절 헬퍼.
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Callable

import numpy as np

from ..config import AppConfig, ROOT
from ..logging_setup import get_logger
from ..strategies import REGISTRY, validate_params
from ..paper_account import PaperAccount
from ..risk import RiskManager, risk_manager_from_cfg
from ..risk_gate import RiskGate
from ..broker import Broker
from ..datasources.earnings import dday_of
from . import (LLMClient, ClaudeCLIClient, MockLLM,
               FileInboxLLM,
               DecisionOutput, ValidationOutput, Proposal, ValidationVerdict)

log = get_logger("agents.wiring")

DATA = ROOT / "data"

LLMFactory = Callable[[list], object]
FetchCandles = Callable[[str, str], list]


def resolve_execution_mode(*, broker_mode: str, dry_run: bool,
                           live_client) -> str:
    """실주문 가능 여부 단일 판정. 반환: "live" | "paper".

    live = (broker.mode==live) AND (live_client 주입) AND (not dry).
    G0 / build_paper_core / 문서가 이 함수만 본다.
    """
    mode = (broker_mode or "paper").lower()
    if mode == "live" and live_client is not None and not dry_run:
        return "live"
    return "paper"

def sector_map_from_universe(cfg: AppConfig) -> dict:
    """config.universe 의 symbol→sector 매핑(포트폴리오 감독관의 섹터 집중도용).

    item 에 sector 가 없으면 제외(그 종목은 섹터 검사에서 빠짐 — 비활성, 안전).
    """
    out: dict[str, str] = {}
    for _market, lst in (cfg.universe or {}).items():
        for it in (lst or []):
            sym, sec = it.get("symbol"), it.get("sector")
            if sym and sec:
                out[sym] = sec
    return out


def build_paper_core(cfg: AppConfig, *, live_client=None, account_seq=None,
                     store=None) -> tuple[Broker, RiskManager]:
    """공유 코어(계좌+하드게이트+브로커, 리스크매니저) 구성.

    진입(뇌 CycleRunner)과 청산(감시 루프 ExitExecutor)이 **같은 계좌**를 봐야 하므로
    한 번 만들어 둘 다에 주입한다. PaperAccount 는 data/paper_account.json 으로 영속.
    하드 게이트엔 포트폴리오 수준 감독관(총 익스포저·섹터 집중도)도 함께 싣는다.

    라이브 배선(심층 방어): live_client 를 **명시 주입한 프로세스(watch 데몬)만** 실주문이
    가능하다. 배치/스크립트는 live_client 를 넘기지 않으므로 config 가 live 여도 자동 페이퍼.
    live 판정 = (broker.mode=='live') AND (live_client 주입됨) AND (dry 아님). dry 는 기존
    관례대로 config run.dry_run 과 .env DRY_RUN 중 하나라도 true 면 참(cfg.dry_run 헬퍼 재사용).
    store 는 라이브 주문 이벤트 기록용(선택).
    """
    risk_cfg = cfg.risk
    paper_cfg = cfg.raw.get("paper", {})
    sector_map = sector_map_from_universe(cfg)
    if risk_cfg.get("max_sector_pct") is not None:
        n_syms = sum(len(lst or []) for lst in (cfg.universe or {}).values())
        if len(sector_map) < n_syms:      # 조용한 비활성 방지 — 커버리지를 크게 알린다
            log.warning("섹터 집중도 감독: universe %d종목 중 %d종목만 sector 지정 — "
                        "미지정 종목은 검사에서 제외됩니다(동적 유니버스면 screen.py 의 "
                        "sector 미기록이 원인).", n_syms, len(sector_map))
    account = PaperAccount(
        cash=paper_cfg.get("cash", {"KR": 10_000_000, "US": 10_000}),
        fee_rate=paper_cfg.get("fee_rate", {}),
        slippage_bps=paper_cfg.get("slippage_bps", {}),
        sell_tax_rate=paper_cfg.get("sell_tax_rate", {}))
    gate = RiskGate({"capital": risk_cfg.get("capital", {}),
                     "max_position_pct": risk_cfg.get("max_position_pct", 0.2),
                     "max_positions": risk_cfg.get("max_positions", 5),
                     "daily_loss_limit_pct": risk_cfg.get("daily_loss_limit_pct", 0.05),
                     "max_order_notional": risk_cfg.get("max_order_notional", {}),
                     "kill_switch_file": risk_cfg.get("kill_switch_file", "data/state/HALT"),
                     "blocked_symbols_file": risk_cfg.get(
                         "blocked_symbols_file", "data/krx_blocked_symbols.json"),
                     # 포트폴리오 감독관(선택; config 미설정 시 None=비활성)
                     "max_gross_exposure": risk_cfg.get("max_gross_exposure"),
                     "max_sector_pct": risk_cfg.get("max_sector_pct"),
                     "max_drawdown_pct": risk_cfg.get("max_drawdown_pct"),
                     # 노출 한도 기준: capital(고정) | equity(실자산 추종)
                     "exposure_base": risk_cfg.get("exposure_base", "capital"),
                     "daily_loss_use_sod_delta": risk_cfg.get(
                         "daily_loss_use_sod_delta", True),
                     "allow_min_lot": risk_cfg.get("allow_min_lot", False),
                     "min_lot_qty": risk_cfg.get("min_lot_qty", 1.0),
                     "sector_map": sector_map})
    broker_cfg = cfg.raw.get("broker", {}) or {}
    mode = broker_cfg.get("mode", "paper")
    live_markets = broker_cfg.get("live_markets", ["KR"])
    # 라이브 집행 파라미터(마켓터블 리밋 슬리피지 상한·체결 대사 폴링). config 미지정 시 기본.
    live_kw = dict(
        limit_slippage_pct=float(broker_cfg.get("limit_slippage_pct", 0.01)),
        max_spread_pct_extended=float(broker_cfg.get("max_spread_pct_extended", 0.02)),
        reconcile_poll_attempts=int(broker_cfg.get("reconcile_poll_attempts", 5)),
        reconcile_poll_sec=float(broker_cfg.get("reconcile_poll_sec", 0.4)),
        reservation_ttl_sec=float(broker_cfg.get("reservation_ttl_sec", 300.0)),
        working_order_ttl_sec=float(broker_cfg.get("working_order_ttl_sec", 60.0)),
        block_on_working_order=bool(broker_cfg.get("block_on_working_order", True)))
    dry = bool(getattr(cfg, "dry_run", True))
    is_live = resolve_execution_mode(
        broker_mode=str(mode), dry_run=dry, live_client=live_client) == "live"
    if is_live:
        seq = account_seq if account_seq is not None else broker_cfg.get("account_seq")
        broker = Broker(account=account, gate=gate, client=live_client, mode="live",
                        account_seq=seq, live_markets=live_markets, store=store, **live_kw)
        log.info("broker=LIVE — 실주문 집행(게이트웨이 경유). live_markets=%s, account_seq=%s",
                 live_markets, seq)
    else:
        broker = Broker(account=account, gate=gate, client=None, mode="paper", store=store)
        # 왜 라이브가 아닌지 한 줄로 명확히(운영자 확인용).
        if mode != "live":
            why = "broker.mode != live"
        elif live_client is None:
            why = "live_client 미주입(배치/스크립트 또는 --dry)"
        else:
            why = "dry 활성(.env DRY_RUN 또는 config run.dry_run == true)"
        log.info("broker=PAPER — %s (mode=%s, dry=%s, live_client=%s)",
                 why, mode, dry, "주입됨" if live_client is not None else "없음")
    risk = risk_manager_from_cfg(risk_cfg)
    return broker, risk


def select_backend(*, dry: bool = False, cli: bool = False, live: bool = False,
                   api_key: str | None = None) -> tuple[bool, bool, bool]:
    """(dry, use_cli, subscription) 결정. 우선순위: dry > cli > live/키 > 기본.

    기본(아무 플래그 없음): 키 있으면 API, 없으면 dry. subscription=키 없는 API OAuth.
    """
    if dry:
        return True, False, False
    if cli:
        return False, True, False
    if live or api_key:
        return False, False, (not api_key)   # 키 없는 live = 구독 OAuth
    return True, False, False                 # 인증 없음 -> 자동 dry


# ── dry(인증 불필요) 헬퍼: 합성 캔들 + MockLLM. 배선/데모 검증용. ──────────
def synth_candles(symbol: str, market: str) -> list[dict]:
    """결정적 난수로 30봉 합성 OHLCV. 토스 호출 없이 흐름을 돌려본다."""
    rng = np.random.default_rng(abs(hash((symbol, market, time.time() // 3))) % (2**32))
    base = 70000 if market == "KR" else 100
    close = base * np.cumprod(1 + rng.normal(0.001, 0.02, 30))
    return [{"open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": 1000}
            for c in close]


def dry_llm_factory(candidates: list[dict]) -> MockLLM:
    """첫 유효 후보를 BUY, 나머지 HOLD. 검증은 전부 승인. (데모/배선 검증)"""
    def responder(schema, system, user):
        if schema is DecisionOutput:
            props, picked = [], False
            for c in candidates:
                if not picked and c.get("price"):
                    props.append(Proposal(symbol=c["symbol"], market=c["market"], side="BUY",
                                          conviction=0.7, horizon="swing", target_weight=0.2,
                                          thesis="[DRY] 데모 매수 제안", key_risks=["dry-run"]))
                    picked = True
                else:
                    props.append(Proposal(symbol=c["symbol"], market=c["market"], side="HOLD",
                                          conviction=0.5, target_weight=0.0,
                                          thesis="[DRY] 관망", key_risks=[]))
            return DecisionOutput(market_view="[DRY] 데모 시장관", proposals=props)
        if schema is ValidationOutput:
            syms = [p["symbol"] for p in json.loads(user)["proposals"]]
            return ValidationOutput(verdicts=[ValidationVerdict(symbol=s, approved=True,
                                    reason="[DRY] 승인") for s in syms])
        raise AssertionError(schema)
    return MockLLM(responder)


def earnings_near(e: dict | None) -> bool:
    """뇌에 실을 만큼 발표가 가까운가 — 발표 3일 후 ~ 3주 전 창(그 밖은 소음).

    dday 가 없는(일정 미공지) 종목도 제외한다. 게이트가 아니라 컨텍스트 필터다 —
    붙이지 않는다고 매매가 막히는 건 없다. 캘린더의 dday 는 배치 시점 스냅샷이라
    오늘 기준으로 다시 계산해서 본다(배치가 밀려도 창이 어긋나지 않게).
    """
    d = dday_of(e)
    return d is not None and -3 <= d <= 21


# 보유기간별 기본 손절/익절(%) — 전략 config 에 값이 없을 때의 폴백.
_HORIZON_DEFAULTS = {"day": (0.02, 0.03), "swing": (0.05, 0.10), "position": (0.08, 0.20)}


def _config_strategy(cfg: AppConfig, symbol: str) -> str | None:
    for _m, lst in (cfg.universe or {}).items():
        for it in (lst or []):
            if it.get("symbol") == symbol:
                return it.get("strategy")
    return None


def resolve_strategy(cfg: AppConfig, symbol: str, proposal=None) -> tuple[str | None, dict]:
    """이 종목에 쓸 (전략명, 파라미터)를 정한다.

    우선순위: **뇌가 고른 전략(proposal.strategy)** > config.universe 매핑(폴백).
    파라미터는 config 기본 위에 뇌가 제시한 값(proposal.params)을 덮고, 하드 가드
    (ParamSpec 범위)로 **클램프**한다 — LLM 은 범위 안에서만 고를 수 있다(미친값 차단).
    뇌가 고른 전략이 무효(레지스트리에 없음)면 그 파라미터는 무시하고 폴백 전략을 쓴다.
    """
    chosen = getattr(proposal, "strategy", None) if proposal else None
    brain_params = getattr(proposal, "params", None) if proposal else None
    if chosen in REGISTRY:
        name = chosen
    else:
        name = _config_strategy(cfg, symbol)
        brain_params = None                      # 엉뚱한 전략에 뇌 파라미터 적용 방지
    base = dict(cfg.strategies.get(name, {})) if name else {}
    if name and brain_params:
        clamped, violations = validate_params(name, {**base, **brain_params})
        if violations:
            log.info("[%s] 뇌 파라미터 클램프: %s", name, "; ".join(violations))
        base = clamped
    return name, base


# entry_stop_target 이 읽는 비율의 최종 하드 바운드. strategies.base.COMMON_PARAMS 와
# 같은 범위 — store meta 등 validate 를 안 거친 params 가 들어와도 여기서 잘린다.
_PCT_BOUNDS = {"stop_loss_pct": (0.005, 0.30), "target_profit_pct": (0.005, 0.50)}


def _plan_pct(params: dict | None, key: str, default: float) -> float:
    """손절/익절 비율을 유한·범위 안으로 강제. 위반이면 보유기간 기본값."""
    raw = (params or {}).get(key)
    if raw is None:
        return default
    try:
        v = float(raw)
    except (TypeError, ValueError):
        log.warning("%s 비정상값 %r → 기본 %s", key, raw, default)
        return default
    lo, hi = _PCT_BOUNDS[key]
    if not math.isfinite(v) or not (lo <= v <= hi):
        log.warning("%s 범위 밖 %r (허용 %s~%s) → 기본 %s", key, v, lo, hi, default)
        return default
    return v


def entry_stop_target(entry_price: float, horizon: str,
                      params: dict | None) -> tuple[float | None, float | None]:
    """진입가·보유기간·전략 파라미터로 (손절가, 목표가) 산출.

    손절/익절%는 전략 params(stop_loss_pct/target_profit_pct), 없으면 보유기간 기본값.
    비유한값·범위 밖은 기본값으로 되돌린다(NaN 손절가 = 손절 무발화).
    """
    d_stop, d_target = _HORIZON_DEFAULTS.get(horizon, _HORIZON_DEFAULTS["swing"])
    stop_pct = _plan_pct(params, "stop_loss_pct", d_stop)
    target_pct = _plan_pct(params, "target_profit_pct", d_target)
    stop = round(entry_price * (1 - stop_pct), 2) if entry_price else None
    target = round(entry_price * (1 + target_pct), 2) if entry_price else None
    return stop, target


def position_plan(cfg: AppConfig, symbol: str, entry_price: float,
                  horizon: str = "swing",
                  proposal=None) -> tuple[str, float | None, float | None]:
    """진입 종목의 (전략명, 손절가, 목표가). 전략·파라미터는 resolve_strategy(뇌 선택 우선)."""
    name, params = resolve_strategy(cfg, symbol, proposal)
    stop, target = entry_stop_target(entry_price, horizon, params)
    return (name or horizon), stop, target


def build_cursor_bridge(cfg: AppConfig) -> FileInboxLLM | None:
    """agents.cursor_bridge.enabled 이면 FileInboxLLM, 아니면 None (기본 off)."""
    cb = (cfg.raw.get("agents") or {}).get("cursor_bridge") or {}
    if not cb.get("enabled"):
        return None
    inbox = cb.get("inbox_dir") or "data/inbox"
    return FileInboxLLM(inbox_dir=inbox,
                        timeout_sec=float(cb.get("timeout_sec", 600)),
                        poll_sec=float(cb.get("poll_sec", 1.0)))


def _cursor_bridge_opts(cfg: AppConfig) -> dict:
    cb = (cfg.raw.get("agents") or {}).get("cursor_bridge") or {}
    return {
        "require_bridge_armed": bool(cb.get("require_armed", True)),
        "bridge_armed_max_age_sec": float(cb.get("armed_max_age_sec", 90)),
    }


def build_live_llm(cfg: AppConfig, *, use_cli: bool, subscription: bool,
                   api_key: str | None, model_override: str | None = None):
    """cli(구독) 또는 api(종량/구독OAuth) LLM 클라이언트 생성.

    model_override 로 역할별 모델 분리(결정=상위 티어, 검증=독립 티어)를 지원한다.
    지정 없으면 config 기본(cli=claude_model, api=agents.model)을 쓴다.
    cli 경로에서 cursor_bridge.enabled 이면 한도 소진 시 FileInboxLLM 3단 폴백
    (require_armed 이면 bridge.heartbeat 신선할 때만).
    """
    a = cfg.raw.get("agents", {})
    if use_cli:
        bridge = build_cursor_bridge(cfg)
        opts = _cursor_bridge_opts(cfg)
        if bridge is not None:
            log.info("cursor_bridge ON — inbox=%s timeout=%ss require_armed=%s",
                     bridge.inbox_dir, int(bridge.timeout_sec),
                     opts["require_bridge_armed"])
        return ClaudeCLIClient(command=a.get("claude_command", "claude"),
                               model=(model_override or a.get("claude_model") or None),
                               timeout=int(a.get("claude_timeout", 120)),
                               fallback_model=(a.get("claude_fallback_model") or None),
                               cursor_bridge=bridge, **opts)
    return LLMClient(model=(model_override or a.get("model", "claude-opus-4-8")),
                     api_key=api_key, subscription=subscription,
                     thinking=bool(a.get("thinking", True)),
                     max_tokens=int(a.get("max_tokens", 8000)))


