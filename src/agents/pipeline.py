"""LLM 사이클 빌더 — 결정→검증→하드게이트→페이퍼 한 사이클의 공용 조립.

배치 진입점(scripts/agent_cycle.py)과 상시 루프의 '뇌'(engine.brain)가 같은 배선을
공유하도록 추출했다. 영속 상태(페이퍼 계좌·리스크게이트·리스크매니저·후보목록)는
CycleRunner 가 들고, run() 한 번이 1사이클이다.

LLM·캔들소스는 주입(inject)한다 — 호출측이 백엔드(dry/cli/live)와 캔들 출처
(합성/TossClient/Gateway)를 정해 넘긴다. 그래서 이 모듈은 백엔드에 중립적이다.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable

import numpy as np

from ..attribution import track_record
from ..config import AppConfig, ROOT
from ..logging_setup import get_logger
from ..strategies import REGISTRY, validate_params
from ..paper_account import PaperAccount
from ..risk import RiskManager
from ..risk_gate import RiskGate
from ..broker import Broker
from .features import assemble
from .context import build_context
from .conviction import attach_event_features
from ..lessons import build_symbol_lessons
from ..datasources.earnings import dday_of, with_fresh_dday
from ..focus import attach_macro_tags, build_focus
from .cycle import run_cycle, CycleResult
from .value_trade import value_trade_cfg
from . import (DecisionAgent, ValidationAgent, LLMClient, ClaudeCLIClient, MockLLM,
               FileInboxLLM,
               DecisionOutput, ValidationOutput, Proposal, ValidationVerdict)

log = get_logger("agents.pipeline")

DATA = ROOT / "data"

# 주입 타입: 후보목록으로 LLM 만들기 / (symbol,market)->캔들 원본
LLMFactory = Callable[[list], object]
FetchCandles = Callable[[str, str], list]


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
                     "kill_switch_file": risk_cfg.get("kill_switch_file", "data/HALT"),
                     "blocked_symbols_file": risk_cfg.get(
                         "blocked_symbols_file", "data/krx_blocked_symbols.json"),
                     # 포트폴리오 감독관(선택; config 미설정 시 None=비활성)
                     "max_gross_exposure": risk_cfg.get("max_gross_exposure"),
                     "max_sector_pct": risk_cfg.get("max_sector_pct"),
                     "max_drawdown_pct": risk_cfg.get("max_drawdown_pct"),
                     # 노출 한도 기준: capital(고정) | equity(실자산 추종)
                     "exposure_base": risk_cfg.get("exposure_base", "capital"),
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
        reconcile_poll_sec=float(broker_cfg.get("reconcile_poll_sec", 0.4)))
    dry = bool(getattr(cfg, "dry_run", True))
    is_live = (mode == "live") and (live_client is not None) and (not dry)
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
    risk = RiskManager(capital=risk_cfg.get("capital", {}),
                       max_position_pct=risk_cfg.get("max_position_pct", 0.2),
                       allow_fractional=risk_cfg.get("allow_fractional", False))
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


def entry_stop_target(entry_price: float, horizon: str,
                      params: dict | None) -> tuple[float | None, float | None]:
    """진입가·보유기간·전략 파라미터로 (손절가, 목표가) 산출.

    손절/익절%는 전략 params(stop_loss_pct/target_profit_pct), 없으면 보유기간 기본값.
    """
    d_stop, d_target = _HORIZON_DEFAULTS.get(horizon, _HORIZON_DEFAULTS["swing"])
    stop_pct = float((params or {}).get("stop_loss_pct", d_stop))
    target_pct = float((params or {}).get("target_profit_pct", d_target))
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
    inbox = cb.get("inbox_dir") or str(DATA / "llm_inbox")
    return FileInboxLLM(inbox_dir=inbox,
                        timeout_sec=float(cb.get("timeout_sec", 240)),
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


class CycleRunner:
    """영속 페이퍼 상태를 들고 run() 으로 1사이클씩 돈다.

    llm_factory(candidates)->llm : dry 는 후보 의존 MockLLM, cli/live 는 고정 클라이언트.
    fetch_candles(symbol,market)->raw : 합성/TossClient/Gateway 중 호출측이 결정.
    store(선택) : 사이클 요약/결정을 SQLite 에 남긴다(상시 루프 감사추적용).
    """

    def __init__(self, cfg: AppConfig, *, llm_factory: LLMFactory,
                 fetch_candles: FetchCandles, store=None, broker: Broker | None = None,
                 risk: RiskManager | None = None,
                 val_llm_factory: LLMFactory | None = None,
                 universe_fn: Callable[[], dict] | None = None,
                 open_markets_fn: Callable[[], list] | None = None,
                 illiquid_fn: Callable[[], set] | None = None,
                 journal_path: str | Path = DATA / "decisions.jsonl",
                 market_state_path: str | Path = DATA / "market_state.json",
                 candle_interval: str = "1d", candle_count: int = 30) -> None:
        self.cfg = cfg
        self.llm_factory = llm_factory
        # 검증 전용 LLM 팩토리(선택). 없으면 결정과 같은 llm 공유(하위호환). 분리 시
        # 결정=상위 티어·검증=독립 티어로 "같은 편향 두 번 통과"를 막는다.
        self.val_llm_factory = val_llm_factory
        self.fetch_candles = fetch_candles
        self.store = store
        self.journal_path = Path(journal_path)
        self.market_state_path = Path(market_state_path)
        self.candle_interval = candle_interval
        self.candle_count = candle_count

        agents_cfg = cfg.raw.get("agents", {})
        # broker 주입 시 그 계좌를 공유(감시 루프의 코드 청산과 같은 계좌). 없으면 자체 구성(배치).
        if broker is None:
            broker, default_risk = build_paper_core(cfg)
            if risk is None:
                risk = default_risk
        elif risk is None:
            risk = RiskManager(capital=cfg.risk.get("capital", {}),
                               max_position_pct=cfg.risk.get("max_position_pct", 0.2),
                               allow_fractional=cfg.risk.get("allow_fractional", False))
        self.broker = broker
        self.account = broker.account
        self.risk = risk
        self.min_conv = float(agents_cfg.get("min_conviction", 0.6))
        self.brain_min_conv = float(agents_cfg.get("brain_min_conviction", 0.0))
        # 런타임 유니버스 재독기(선택). 있으면 run() 마다 그 시점 유니버스로 후보를 다시 만든다
        # (screen 재생성이 재기동 없이 반영). 없으면 기동 시 cfg.universe 로 고정(하위호환·배치).
        self.universe_fn = universe_fn
        # 열린 시장 필터(선택; opt-in). 있으면 run() 후보를 그 시점 개장 시장으로 제한한다
        # (국장 마감 후 뇌 컨텍스트에서 KR 후보 제외 → 닫힌 시장 후보에 스냅샷이 진입하는
        # 이상 경로 차단). 기본 None=무필터(기존 테스트·배치 전부 무변경 통과). 보유분 청산
        # 판단은 portfolio 경유(_portfolio)라 이 필터와 무관하게 계속 뇌에 실린다.
        self.open_markets_fn = open_markets_fn
        # 유동성 필터(선택; opt-in). 있으면 run() 후보에서 그 시점 illiquid(시간외 체결정지)
        # 심볼을 제외한다 — 프리/애프터장에 거래가 없던 종목이 신규진입 후보로 뽑히는 것을
        # 막는다. 보유분 청산 판단은 portfolio 경유(_portfolio)라 이 필터와 무관.
        self.illiquid_fn = illiquid_fn
        # 밸류 시간 손절 임계일(0=비활성). 매 사이클 config 재파싱을 피해 기동 시 1회만 읽는다.
        self.value_time_stop_days = int(value_trade_cfg(cfg)["time_stop_days"])
        self.items = self._items_from(cfg.universe or {})
        self._regime_now: dict = {}    # 이번 사이클 시장별 국면(진입 시 meta 에 기록 → regime_flip)

    @staticmethod
    def _items_from(universe: dict) -> list:
        """{market: [item,...]} → 후보 flat 목록([{symbol,name,market}...])."""
        return [{"symbol": it["symbol"], "name": it.get("name", it["symbol"]),
                 "market": market,
                 "pool": it.get("pool") or ("day" if it.get("layer") == "day" else "swing"),
                 "sector": it.get("sector"),
                 "rank": it.get("rank"), "trading_amount": it.get("trading_amount")}
                for market, lst in (universe or {}).items() for it in (lst or [])
                if isinstance(it, dict) and it.get("symbol")]

    def _universe_item(self, symbol: str) -> dict | None:
        """유니버스 원천 item(source 등 태그 포함) 조회 — 성과귀속용 source 태그 배관.

        universe_fn 이 있으면 그 시점 유니버스를, 없으면 cfg.universe(기동 시 고정)를 본다.
        self.items 는 source 를 안 실으므로 원천 dict 를 직접 찾는다.
        """
        universe = self.universe_fn() if self.universe_fn else (self.cfg.universe or {})
        for _market, lst in (universe or {}).items():
            for it in (lst or []):
                if it.get("symbol") == symbol:
                    return it
        return None

    def _portfolio(self, earnings: dict | None = None) -> dict:
        """보유 종목에 store 의 진입 thesis/전략/손절목표/보유기간을 붙여 뇌에게 전달.

        뇌가 "왜 샀는지"(entry_thesis)를 현재 데이터와 대조해 thesis 가 깨졌으면 SELL 을
        제안할 수 있게 한다(thesis 기억 → 깨짐 청산). store 가 없으면 기본 정보만.
        발표가 가까운 보유 종목엔 실적 캘린더(earnings)도 붙인다 — 후보보다 보유가 더 중요.
        밸류 포지션엔 시간 손절 플래그(time_stop)도 붙인다 — 코드는 강제 청산하지 않고
        '예정 기간 안에 저평가가 해소되지 않았다'는 사실만 뇌에게 알린다.
        """
        earnings = self._earnings() if earnings is None else earnings
        rows = {r["symbol"]: r for r in self.store.get_open_positions()} if self.store else {}
        positions = []
        # list() 스냅샷: 재대사/체결 스레드가 broker 락 안에서 account.positions 를 갈아끼울
        # 수 있어 순회 중 크기 변경(RuntimeError) 방지(뇌 사이클은 락 밖에서 읽는다).
        for s, p in list(self.account.positions.items()):
            if not p.is_open:
                continue
            item = {"symbol": s, "qty": p.qty, "avg_price": p.avg_price,
                    "market": self.account.symbol_market.get(s)}
            row = rows.get(s)
            if row is not None:
                item["entry_thesis"] = row["thesis"]
                item["strategy"] = row["strategy"]
                item["stop_price"] = row["stop_price"]
                item["target_price"] = row["target_price"]
                if row["opened_at"]:
                    item["days_held"] = round((time.time() - row["opened_at"]) / 86400, 1)
                    # 시간 손절(밸류 포지션 한정). threshold 0=비활성이면 아예 안 붙인다.
                    if row["strategy"] == "value" and self.value_time_stop_days > 0:
                        item["time_stop"] = {
                            "days_held": item["days_held"],
                            "threshold_days": self.value_time_stop_days,
                            "exceeded": item["days_held"] >= self.value_time_stop_days}
                # 트레일링 활성 포지션: stop_price 는 이익을 잠근 트레일링 스톱, target_price 는
                # 더는 상한이 아니다. 뇌가 "목표 도달=청산"으로 오판하지 않게 상태를 싣는다.
                try:
                    m = json.loads(row["meta"]) if row["meta"] else {}
                except (ValueError, TypeError):
                    m = {}
                if isinstance(m, dict) and m.get("trail_active"):
                    item["trail_active"] = True
            e = earnings.get(s)
            if earnings_near(e):
                item["earnings"] = with_fresh_dday(e)
            positions.append(item)
        return {"cash": self.account.cash, "positions": positions}

    def _recent_disclosures(self, hours: float = 6.0, limit: int = 10) -> list[dict]:
        """워처가 events 에 남긴 최근 중대 공시(각성/큐 라우팅분) — 뇌 입력용."""
        if not self.store:
            return []
        try:
            rows = self.store.recent_events("disclosure", time.time() - hours * 3600,
                                            limit=limit)
            out = []
            for r in rows:
                p = json.loads(r["payload"]) if r["payload"] else {}
                item = {"symbol": r["symbol"], "report_nm": p.get("report_nm"),
                        "keyword": p.get("keyword"), "route": p.get("route"),
                        "rcept_dt": p.get("rcept_dt")}
                for k in ("actuals", "consensus", "surprise_pct", "rcept_no"):
                    if p.get(k) is not None:
                        item[k] = p[k]
                out.append(item)
            return out
        except Exception as e:
            log.warning("최근 공시 로드 실패(생략): %s", e)
            return []

    def _recent_earnings_results(self, hours: float = 36.0,
                                 limit: int = 8) -> list[dict]:
        """워처가 events 에 남긴 최근 실적 결과(컨센서스 대비 실제 편차) — 뇌 입력용.

        창이 36시간인 건 amc(장마감 후) 발표가 다음 거래일 판단까지 살아 있어야 해서다.
        """
        if not self.store:
            return []
        try:
            rows = self.store.recent_events("earnings_result", time.time() - hours * 3600,
                                            limit=limit)
            out = []
            for r in rows:
                p = json.loads(r["payload"]) if r["payload"] else {}
                item = {"symbol": r["symbol"], "date": p.get("date"),
                        "eps_estimate": p.get("eps_estimate"),
                        "eps_actual": p.get("eps_actual"),
                        "eps_surprise_pct": p.get("eps_surprise_pct"),
                        "revenue_surprise_pct": p.get("revenue_surprise_pct"),
                        "route": p.get("route")}
                for k in ("market", "rcept_no", "parse_ok", "unit", "scope",
                          "revenue_actual", "op_profit_actual", "net_income_actual",
                          "revenue_estimate", "op_profit_estimate", "net_income_estimate",
                          "op_profit_surprise_pct", "net_income_surprise_pct"):
                    if p.get(k) is not None:
                        item[k] = p[k]
                out.append(item)
            return out
        except Exception as e:
            log.warning("최근 실적 결과 로드 실패(생략): %s", e)
            return []

    # ── 도시에(Athena 딥리서치) 연동 ─────────────────────────
    def _fresh_dossier(self, symbol: str):
        """유효기간 내 최신 도시에 행(없으면 None)."""
        return self.store.get_fresh_dossier(symbol) if self.store else None

    def _dossier_brief(self, symbol: str) -> dict | None:
        """후보 피처용 도시에 요약(레벨·손익비·확신도·stance·나이)."""
        row = self._fresh_dossier(symbol)
        if not row:
            return None
        try:
            ev = json.loads(row["evidence"]) if row["evidence"] else {}
        except (ValueError, TypeError):
            ev = {}
        if not isinstance(ev, dict):
            ev = {}
        return {"id": row["id"], "stance": ev.get("stance"),
                "thesis": (row["thesis"] or "")[:300],
                "entry_low": row["entry_low"], "entry_high": row["entry_high"],
                "invalidation": row["invalidation"], "target": row["target"],
                "expires_at": row["expires_at"],
                "rr": row["rr"], "conviction": row["conviction"],
                "age_hours": round((time.time() - row["created_at"]) / 3600, 1)}

    def _has_bullish_dossier(self, symbol: str) -> bool:
        b = self._dossier_brief(symbol)
        return bool(b and b["stance"] == "bullish")

    def _entry_zone(self, symbol: str) -> dict | None:
        """갭 진입 가드용 진입존. bullish + 레벨(진입존·무효화가) 전부 있어야 dict 반환.

        레벨이 결손되거나 stance 가 bullish 가 아니면 None(가드 비활성 = 기존 즉시체결).
        """
        b = self._dossier_brief(symbol)
        if not b or b.get("stance") != "bullish":
            return None
        if b.get("entry_low") is None or b.get("entry_high") is None or b.get("invalidation") is None:
            return None
        return {"entry_low": b["entry_low"], "entry_high": b["entry_high"],
                "invalidation": b["invalidation"], "target": b.get("target"),
                "expires_at": b.get("expires_at")}

    def _base_rates(self) -> dict:
        """data/base_rates.json(장전 배치 산출)의 종목별 셋업 통계. 없으면 빈 dict."""
        p = DATA / "base_rates.json"
        try:
            if p.exists():
                return json.loads(p.read_text(encoding="utf-8")).get("symbols", {})
        except (OSError, ValueError) as e:
            log.warning("base_rates 로드 실패(생략): %s", e)
        return {}

    def _earnings(self) -> dict:
        """data/earnings_calendar.json(장전 배치 산출)의 종목별 실적 일정·컨센서스.

        없거나 깨졌으면 빈 dict — 실적 데이터가 없어도 사이클은 예전 그대로 돈다.
        """
        p = DATA / "earnings_calendar.json"
        try:
            if p.exists():
                return json.loads(p.read_text(encoding="utf-8")).get("symbols", {})
        except (OSError, ValueError) as e:
            log.warning("earnings_calendar 로드 실패(생략): %s", e)
        return {}

    def run(self, wake: dict | None = None) -> CycleResult:
        ms = (json.loads(self.market_state_path.read_text(encoding="utf-8"))
              if self.market_state_path.exists() else {})
        self._regime_now = {m: (v or {}).get("label")
                            for m, v in (ms.get("regime") or {}).items()}
        # 후보는 매 실행마다 계산 — universe_fn 이 있으면 그 시점 유니버스로(런타임 재독),
        # 없으면 기동 시 고정된 self.items(하위호환).
        items = self._items_from(self.universe_fn()) if self.universe_fn else self.items
        # 열린 시장 필터(opt-in): 주어졌을 때만 닫힌 시장 후보를 제외한다.
        # Athena 종료 훅(07:30)은 프리 전이므로 후보를 비우면 안 된다 — 오늘 살 시장만 남긴다.
        if self.open_markets_fn is not None:
            reason = str((wake or {}).get("reason") or "")
            if reason == "athena_done":
                broker_live = (self.cfg.raw.get("broker") or {}).get("live_markets")
                trade = (self.cfg.raw.get("run") or {}).get("trade_markets")
                keep = [str(m).upper() for m in (broker_live or trade or ["KR"])]
                items = [i for i in items if i["market"] in keep]
            else:
                open_mkts = set(self.open_markets_fn())
                items = [i for i in items if i["market"] in open_mkts]
        # 유동성 필터(opt-in): 시간외 세션에서 체결이 멈춘 종목은 신규진입 후보에서 제외.
        if self.illiquid_fn is not None:
            stale = self.illiquid_fn()
            if stale:
                items = [i for i in items if i["symbol"] not in stale]
        candidates, price_lookup = assemble(items, ms, self.fetch_candles,
                                            enrich_strategy=True,
                                            base_rates=self._base_rates())
        earnings = self._earnings()              # 장전 배치 산출(없으면 {})
        for c in candidates:                     # Athena 도시에를 후보 피처로(있는 것만)
            d = self._dossier_brief(c["symbol"])
            if d:
                c["dossier"] = d
            e = earnings.get(c["symbol"])        # 발표 임박(-3~21일) 종목만 — 소음 차단
            if earnings_near(e):
                c["earnings"] = with_fresh_dday(e)
        # 종목별 과거 거래 회고(lessons) — 이력 있는 후보에만 past_trades 부착(LLM 0콜).
        if self.store and self.cfg.raw.get("agents", {}).get("lessons", True):
            lessons = build_symbol_lessons(self.store, [c["symbol"] for c in candidates])
            for c in candidates:
                pt = lessons.get(c["symbol"])
                if pt:
                    c["past_trades"] = pt
        # 매크로 민감 태그(정적 맵) + 주의층 렌즈 — dday 신선도를 위해 사이클마다 계산.
        attach_macro_tags(candidates, sector_map_from_universe(self.cfg))
        portfolio = self._portfolio(earnings)
        focus = build_focus(ms, candidates=candidates,
                            positions=portfolio.get("positions") or [])
        llm = self.llm_factory(candidates)
        val_llm = self.val_llm_factory(candidates) if self.val_llm_factory else llm
        constraints = {"capital": self.cfg.risk.get("capital", {}),
                       "max_position_pct": self.cfg.risk.get("max_position_pct", 0.2),
                       "max_positions": self.cfg.risk.get("max_positions", 5),
                       "open_positions": self.account.open_count}
        # 트랙레코드(라이브 성과 귀속) + 최근 중대 공시(워처가 잡은 것)를 함께 실어
        # 뇌가 자기 과거 성과와 방금 뜬 재료를 보고 판단하게 한다.
        # wake: BrainWorker 가 넘긴 각성 사유(없으면 배치/수동 호출).
        discs = self._recent_disclosures()
        ers = self._recent_earnings_results()
        context = build_context(ms, candidates, portfolio, constraints,
                                track_record=(track_record(self.store)
                                              if self.store else None),
                                recent_disclosures=discs,
                                earnings_results=ers,
                                focus=focus,
                                wake=(wake if wake and (wake.get("reason")
                                                        or wake.get("triggers"))
                                      else None))
        feat_map = attach_event_features(
            {c["symbol"]: c for c in candidates}, discs, ers)
        agents_cfg = self.cfg.raw.get("agents", {})
        mlc = agents_cfg.get("min_lot_conviction")
        if mlc is None and self.cfg.risk.get("allow_min_lot"):
            mlc = self.min_conv
        res = run_cycle(context_json=context, decision_agent=DecisionAgent(llm),
                        validation_agent=ValidationAgent(val_llm, min_conviction=self.brain_min_conv),
                        broker=self.broker, risk=self.risk, price_lookup=price_lookup,
                        journal_path=self.journal_path,
                        arm_fn=(self._arm if self.store else None),
                        # 도시에 우선 원칙(스윙/장투): store 있고 config 로 켜져 있을 때만
                        dossier_fn=(self._has_bullish_dossier
                                    if self.store and agents_cfg.get("require_dossier", True)
                                    else None),
                        # 갭 진입 가드(스윙/장투): store 있고 config 로 켜져 있을 때만
                        zone_fn=(self._entry_zone
                                 if self.store and agents_cfg.get("entry_zone_guard", True)
                                 else None),
                        entry_zone_tolerance_pct=float(
                            agents_cfg.get("entry_zone_tolerance_pct", 0.005)),
                        conviction_sizing=bool(agents_cfg.get("conviction_sizing", True)),
                        min_lot_conviction=float(mlc) if mlc is not None else None,
                        apply_code_conviction=bool(agents_cfg.get("conviction_code", True)),
                        dossier_brief_fn=(self._dossier_brief if self.store else None),
                        features_by_sym=feat_map)
        self._record(res)
        self.sync_store_positions(res)
        return res

    def _arm(self, proposal, price: float, zone: dict | None = None) -> bool:
        """BUY 제안을 진입대기(armed)로 등록 → 진입 타이밍은 감시 루프가 잡는다.

        데이트레(day)는 기존 경로(zone=None). zone 이 주어지면 갭 진입 가드 경로 —
        스윙/장투 BUY 가 진입존 밖(갭상승/존이탈)이라 도시에 레벨 기준 존 재진입을
        기다리는 armed 등록이다. horizon 은 proposal 원래 값(day 가 아니므로 종가
        강제청산 대상에서 자동 제외됨). 전략명은 청산과 같은 출처(config.universe
        symbol→strategy)에서 가져온다. 이미 보유 중이거나 진입대기인 종목은 중복
        arm 하지 않는다(멱등).
        """
        if not self.store:
            return False
        sym = proposal.symbol
        held = {r["symbol"] for r in self.store.get_open_positions()}
        pending = {r["symbol"] for r in self.store.get_armed()}
        if sym in held or sym in pending:
            return False
        horizon = proposal.horizon or "day"
        strat, params = resolve_strategy(self.cfg, sym, proposal)   # 뇌 선택 우선(클램프)
        d = self._dossier_brief(sym)             # day 는 도시에 없어도 arm 가능(예외 경로)
        agents_cfg = self.cfg.raw.get("agents", {})
        mlc = agents_cfg.get("min_lot_conviction")
        if mlc is None and self.cfg.risk.get("allow_min_lot"):
            mlc = self.min_conv
        meta = {"horizon": horizon, "params": params,
                "target_weight": proposal.target_weight,
                "conviction": getattr(proposal, "conviction", None),
                "conviction_sizing": bool(agents_cfg.get("conviction_sizing", True)),
                "entry_regime": self._regime_now.get(proposal.market),
                "dossier_id": (d["id"] if d else None)}
        if mlc is not None:
            meta["min_lot_conviction"] = float(mlc)
        item = self._universe_item(sym)      # 성과귀속용 source 태그(gem 등, 있을 때만)
        if item and item.get("source"):
            meta["source"] = item["source"]
        if zone:
            meta["entry_zone"] = {"low": zone["entry_low"], "high": zone["entry_high"],
                                  "invalidation": zone["invalidation"],
                                  "target": zone.get("target"),
                                  "expires_at": zone.get("expires_at")}
        self.store.arm_candidate(
            sym, proposal.market, strategy=strat, thesis=proposal.thesis, meta=meta)
        self.store.log_event("arm", sym,
                             {"strategy": strat, "horizon": horizon, "price": price,
                              "thesis": (proposal.thesis or "")[:80]})
        log.info("진입대기 등록 %s (%s, horizon=%s) @ %.2f", sym, strat, horizon, price)
        return True

    def sync_store_positions(self, res: CycleResult) -> None:
        """페이퍼 계좌 ↔ store.positions 정합화(멱등).

        새로 생긴 보유 → open_position(전략/thesis/손절/목표 포함, 감시 루프가 읽어 트리거).
        평탄해진 보유 → close_position. 이게 있어야 데몬의 손절/익절 트리거가 실보유에 걸린다.
        """
        if not self.store:
            return
        prop_by_sym = {p.symbol: p for p in res.decision.proposals}
        open_rows = {r["symbol"]: r for r in self.store.get_open_positions()}
        acct = self.broker.account
        for sym, pos in list(acct.positions.items()):   # 스냅샷(재대사 스레드와 경합 방지)
            if not pos.is_open:
                continue
            if sym in open_rows:                      # 기존 보유: 수량/평단 갱신만
                row = open_rows[sym]
                if (abs(row["qty"] - pos.qty) > 1e-9
                        or abs(row["avg_price"] - pos.avg_price) > 1e-9):
                    self.store.update_position(row["id"], qty=pos.qty, avg_price=pos.avg_price)
                continue
            market = acct.symbol_market.get(sym, "KR")
            prop = prop_by_sym.get(sym)
            horizon = getattr(prop, "horizon", "swing") or "swing"
            # 전략·파라미터는 뇌 선택 우선(없으면 config 폴백, 하드가드 클램프). 코드 전략
            # 실행기(진입/청산)와 손절/목표가 모두 이 전략 기준으로 동작한다.
            strat, params = resolve_strategy(self.cfg, sym, prop)
            stop, target = entry_stop_target(pos.avg_price, horizon, params)
            # 도시에가 있으면 무효화가/목표가를 손절/목표로(리서치 근거 레벨 > %기본값).
            d = self._dossier_brief(sym)
            if d and d.get("invalidation"):
                stop = d["invalidation"]
            if d and d.get("target"):
                target = d["target"]
            meta = {"horizon": horizon, "params": params,
                    "entry_regime": self._regime_now.get(market),
                    "dossier_id": (d["id"] if d else None)}   # A/B 귀속 태그
            item = self._universe_item(sym)      # 성과귀속용 source 태그(gem 등, 있을 때만)
            if item and item.get("source"):
                meta["source"] = item["source"]
            self.store.open_position(
                sym, market, pos.qty, pos.avg_price, strategy=strat,
                thesis=(prop.thesis if prop else None),
                target_price=target, stop_price=stop, meta=meta)
        for sym, row in open_rows.items():            # 계좌에서 사라진 보유 → 청산처리
            p = acct.positions.get(sym)
            if p is None or not p.is_open:
                # 청산 체결가는 계좌 저널의 마지막 SELL 에서(성과귀속). 없으면 NULL.
                exit_px = next((f.price for f in reversed(acct.journal)
                                if f.symbol == sym and f.side == "SELL"), None)
                self.store.close_position(row["id"], exit_price=exit_px, reason="brain")

    def _record(self, res: CycleResult) -> None:
        if not self.store:
            return
        verdicts = {v.symbol: v for v in res.validation.verdicts}
        for p in res.decision.proposals:
            v = verdicts.get(p.symbol)
            d = self._dossier_brief(p.symbol)
            self.store.record_decision(
                symbol=p.symbol, action=p.side, conviction=p.conviction,
                thesis=p.thesis, verdict=("approved" if (v and v.approved) else "vetoed"),
                payload={"target_weight": p.target_weight, "horizon": p.horizon,
                         "dossier_id": (d["id"] if d else None)})   # 도시에 A/B 귀속
        self.store.log_event("cycle", None, {"market_view": res.decision.market_view,
                                             "executed": res.executed})
