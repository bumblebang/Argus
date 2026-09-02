"""CycleRunner — 1사이클 prep/run/post (Phase 1, pipeline 에서 분리)."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable

from ..attribution import track_record
from ..config import AppConfig
from ..logging_setup import get_logger
from ..risk import RiskManager, risk_manager_from_cfg
from ..broker import Broker
from .features import assemble, filter_gap_rebound_candidates, wake_has_gap_scan
from .context import build_context
from .conviction import attach_event_features
from ..lessons import build_symbol_lessons
from ..datasources.earnings import with_fresh_dday
from ..datasources.nxt_universe import filter_items_for_gap_scan, nxt_supported_map
from ..focus import attach_macro_tags, build_focus
from .cycle import run_cycle, CycleResult
from .value_trade import value_trade_cfg
from . import serve_policy as serve
from . import DecisionAgent, ValidationAgent
from .brain_model_policy import decision_tier
from .wiring import (
    DATA, LLMFactory, FetchCandles,
    build_paper_core, sector_map_from_universe, earnings_near,
    resolve_strategy, combine_stop_target,
)
from .. import paths as _paths

log = get_logger("agents.cycle_runner")

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
                 decision_llm_fn: Callable[[dict | None], object] | None = None,
                 universe_fn: Callable[[], dict] | None = None,
                 open_markets_fn: Callable[[], list] | None = None,
                 illiquid_fn: Callable[[], set] | None = None,
                 price_fn: Callable[[list[str], str], dict[str, float]] | None = None,
                 journal_path: str | Path = "data/decisions.jsonl",
                 market_state_path: str | Path = DATA / "market_state.json",
                 candle_interval: str = "1d", candle_count: int = 250) -> None:
        self.cfg = cfg
        self.llm_factory = llm_factory
        # 검증 전용 LLM 팩토리(선택). 없으면 결정과 같은 llm 공유(하위호환). 분리 시
        # 결정=상위 티어·검증=독립 티어로 "같은 편향 두 번 통과"를 막는다.
        self.val_llm_factory = val_llm_factory
        self.decision_llm_fn = decision_llm_fn
        self.fetch_candles = fetch_candles
        self.store = store
        self.journal_path = _paths.resolve("decisions", configured=journal_path)
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
            risk = risk_manager_from_cfg(cfg.risk)
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
        self.price_fn = price_fn
        # 밸류 시간 손절 임계일(0=비활성). 매 사이클 config 재파싱을 피해 기동 시 1회만 읽는다.
        self.value_time_stop_days = int(value_trade_cfg(cfg)["time_stop_days"])
        self.items = self._items_from(cfg.universe or {})
        self._regime_now: dict = {}    # 이번 사이클 시장별 국면(진입 시 meta 에 기록 → regime_flip)

    @staticmethod
    def _items_from(universe: dict) -> list:
        """{market: [item,...]} → 후보 flat 목록([{symbol,name,market}...])."""
        return [{"symbol": it["symbol"], "name": it.get("name", it["symbol"]),
                 "market": market,
                 "pool": it.get("pool") or ("day" if it.get("layer") == "day"
                                            else "gap_decline" if it.get("layer") == "gap_decline"
                                            else "swing"),
                 "sector": it.get("sector"),
                 "source": it.get("source"),
                 "layer": it.get("layer"),
                 "rank": it.get("rank"),
                 "trading_amount": it.get("trading_amount"),
                 "fluctuation": it.get("fluctuation"),
                 "decline_pct": it.get("decline_pct"),
                 "nxt_supported": it.get("nxt_supported"),
                 "added_at": it.get("added_at"),
                 "pool_date": it.get("pool_date")}
                for market, lst in (universe or {}).items() for it in (lst or [])
                if isinstance(it, dict) and it.get("symbol")]

    def _universe_item(self, symbol: str) -> dict | None:
        """유니버스 원천 item(source 등 태그 포함) 조회 — 성과귀속용 source 태그 배관.

        universe_fn 이 있으면 그 시점 유니버스를, 없으면 cfg.universe(기동 시 고정)를 본다.
        self.items 는 source 를 안 실으므로 원천 dict 를 직접 찾는다.
        item 에는 유니버스 키였던 market 을 실어 준다(원천 dict 에는 없음).
        """
        universe = self.universe_fn() if self.universe_fn else (self.cfg.universe or {})
        for market, lst in (universe or {}).items():
            for it in (lst or []):
                if it.get("symbol") == symbol:
                    return {**it, "market": it.get("market") or market}
        return None

    def market_of(self, symbol: str) -> str | None:
        """코드 권위 market. 유니버스 → 원장(symbol_market) 순. 없으면 None.

        LLM `proposal.market` 은 스키마(KR|US) 검증만 받고 실제 시장과 대조되지 않는다.
        그 값이 자본 풀·한도 분모·live 집행 판정까지 흐르므로, 라벨이 아니라 코드가
        아는 사실을 권위로 쓴다. 심볼 형태 휴리스틱(6자리=KR)은 쓰지 않는다 —
        예외 티커에서 조용히 틀리느니 거부가 낫다.
        """
        item = self._universe_item(symbol)
        if item and item.get("market"):
            return str(item["market"])
        acct = getattr(self.broker, "account", None)
        held = (getattr(acct, "symbol_market", None) or {}).get(symbol)
        return str(held) if held else None

    def _portfolio(self, earnings: dict | None = None,
                   live_prices: dict[str, float] | None = None) -> dict:
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
            px = None
            if live_prices:
                try:
                    v = live_prices.get(s)
                    if v is not None:
                        px = float(v)
                except (TypeError, ValueError):
                    px = None
            if px is not None and px > 0:
                item["current_price"] = round(px, 2)
                avg = float(p.avg_price or 0)
                if avg > 0:
                    item["unrealized_pnl_pct"] = round((px / avg - 1) * 100, 2)
                    item["unrealized_pnl"] = round((px - avg) * p.qty, 0)
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
                for k in ("actuals", "consensus", "surprise_pct", "rcept_no",
                          "market", "form", "items", "accession", "filing_date"):
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

    def _batch_live_prices(self, items: list[dict]) -> dict[str, float]:
        """토스 배치 현재가 → {symbol: price}. price_fn 없으면 {}."""
        if not self.price_fn or not items:
            return {}
        from collections import defaultdict
        by_mkt: dict[str, list[str]] = defaultdict(list)
        for it in items:
            sym = it.get("symbol")
            if sym:
                by_mkt[it.get("market", "KR")].append(sym)
        out: dict[str, float] = {}
        for mkt, syms in by_mkt.items():
            try:
                rows = self.price_fn(syms, mkt) or {}
                for sym, px in rows.items():
                    try:
                        v = float(px)
                    except (TypeError, ValueError):
                        continue
                    if v > 0:
                        out[str(sym)] = v
            except Exception as e:
                log.warning("live price 배치 실패(%s, %d종): %s", mkt, len(syms), e)
        if out:
            log.info("live price %d/%d종 (gateway 배치)", len(out), len(items))
        return out

    def _resolve_price(self, symbol: str, market: str) -> float | None:
        """shortlist 밖 제안·집행용 — live quote 우선, 없으면 캔들 종가."""
        mkt = market or self.market_of(symbol) or "KR"
        if self.price_fn:
            try:
                rows = self.price_fn([symbol], mkt) or {}
                v = rows.get(symbol)
                if v is not None:
                    px = float(v)
                    if px > 0:
                        return px
            except Exception as e:
                log.debug("[%s] live quote 실패: %s", symbol, e)
        try:
            raw = self.fetch_candles(symbol, mkt)
            from ..runner import candles_to_df
            df = candles_to_df(raw) if raw else None
            if df is not None and len(df) >= 1:
                return float(df["close"].astype(float).iloc[-1])
        except Exception as e:
            log.debug("[%s] 캔들 가격 실패: %s", symbol, e)
        return None

    def run(self, wake: dict | None = None) -> CycleResult:
        ms = (json.loads(self.market_state_path.read_text(encoding="utf-8"))
              if self.market_state_path.exists() else {})
        self._regime_now = {m: (v or {}).get("label")
                            for m, v in (ms.get("regime") or {}).items()}
        # 후보는 매 실행마다 계산 — universe_fn 이 있으면 그 시점 유니버스로(런타임 재독),
        # 없으면 기동 시 고정된 self.items(하위호환).
        items = self._items_from(self.universe_fn()) if self.universe_fn else self.items
        # 열린 시장 필터(opt-in): 주어졌을 때만 닫힌 시장 후보를 제외한다.
        # Athena 종료 훅 — wake.market 시장만(프리 전이라 open_markets 우회). market 없으면 live_markets.
        wake_reason = str((wake or {}).get("reason") or "")
        full_universe = items
        held = serve.held_symbols(self.account.positions)
        if wake_has_gap_scan(wake_reason):
            from ..gap_decline_pool import items_for_gap_scan
            items = items_for_gap_scan(wake_reason, universe_items=full_universe,
                                       held=held)
        else:
            items = [i for i in items
                     if str(i.get("pool") or "") != "gap_decline"]
        if self.open_markets_fn is not None:
            reason = str((wake or {}).get("reason") or "")
            if reason == "athena_done":
                # Athena 창 종료 wake — 해당 시장 배치만 끝났다(wake.market).
                # KR 직후에 US 스윙 20종을 넣지 않음(간밤 맥락은 market_state·헤드라인).
                # market 태그 없으면 live_markets 폴백(구 wake 호환).
                wake_mkt = str((wake or {}).get("market") or "").strip().upper()
                if wake_mkt in ("KR", "US"):
                    keep = {wake_mkt}
                else:
                    broker_live = (self.cfg.raw.get("broker") or {}).get("live_markets")
                    trade = (self.cfg.raw.get("run") or {}).get("trade_markets")
                    keep = {str(m).upper() for m in (broker_live or trade or ["KR"])}
                items = [i for i in items if str(i.get("market") or "").upper() in keep]
            else:
                open_mkts = set(self.open_markets_fn())
                items = [i for i in items if i["market"] in open_mkts]
        # 유동성 필터(opt-in): 시간외 세션에서 체결이 멈춘 종목은 신규진입 후보에서 제외.
        if self.illiquid_fn is not None:
            stale = self.illiquid_fn()
            if stale:
                items = [i for i in items if i["symbol"] not in stale]
        if wake_has_gap_scan(wake_reason):
            nxt = nxt_supported_map(i["symbol"] for i in items if i.get("market") == "KR")
            before = len(items)
            items = filter_items_for_gap_scan(items, wake_reason, nxt)
            if len(items) < before:
                log.info("갭반등 NXT split %s %d→%d", wake_reason, before, len(items))
        agents_cfg = self.cfg.raw.get("agents", {})
        scfg = serve.serve_cfg(agents_cfg)
        armed = ([str(r["symbol"]) for r in self.store.get_armed()]
                 if self.store else [])
        bullish = (self.store.list_fresh_bullish_symbols()
                   if self.store else [])
        from ..strategy_scores import (load_strategy_scores, strategy_fit_brief,
                                       strategy_scores_asof, strategy_scores_stale)
        scores_stale = strategy_scores_stale()
        strat_scores = load_strategy_scores() if not scores_stale else {}
        scores_asof = strategy_scores_asof()
        n_items_before = len(items)
        items, tier = serve.select_candidates(
            items, wake, held=held, armed=armed, bullish=bullish,
            scores=strat_scores, cfg=scfg)
        scan_shortlist = (
            tier == "scan" and scfg.get("scan_enabled", True)
            and not serve.scan_shortlist_exempt(wake))
        enrich_strategy = not scan_shortlist
        gap_scan = wake_has_gap_scan(wake_reason)
        fetch_fn = self.fetch_candles
        if gap_scan:
            from .wiring import history_candles_1y
            fetch_fn = lambda s, m: history_candles_1y(s, m, fresh=True)
        live_prices = self._batch_live_prices(items)
        held_only = [
            {"symbol": s, "market": self.market_of(s) or "KR"}
            for s in held
            if s and (not live_prices or s not in live_prices)
        ]
        if held_only:
            held_px = self._batch_live_prices(held_only)
            if held_px:
                live_prices = {**(live_prices or {}), **held_px}
        candidates, price_lookup = assemble(items, ms, fetch_fn,
                                            enrich_strategy=enrich_strategy,
                                            base_rates=self._base_rates(),
                                            live_prices=live_prices or None,
                                            daily_fetch_fresh=gap_scan)
        if scan_shortlist:
            for c in candidates:
                rec = strat_scores.get(str(c.get("symbol") or ""))
                brief = strategy_fit_brief(rec)
                if brief:
                    c["strategy_fit"] = brief
        if wake_has_gap_scan(wake_reason):
            before = len(candidates)
            candidates = filter_gap_rebound_candidates(candidates, held=held)
            if len(candidates) < before:
                log.info("갭반등 pre-filter %d→%d (floor<=-5%%)", before, len(candidates))
        from ..gap_decline_pool import fresh_gap_symbols, load_gap_decline_pool
        _gap_raw = load_gap_decline_pool()
        _gap_overlay = fresh_gap_symbols(_gap_raw, "KR")
        if _gap_overlay:
            for c in candidates:
                sym = str(c.get("symbol") or "")
                if c.get("market") == "KR" and sym in _gap_overlay:
                    c["pool"] = "gap_decline"
                    c["source"] = "gap_rebound"
                    c["pool_date"] = _gap_overlay[sym]
        scfg_enrich = serve.serve_cfg(agents_cfg)
        from ..candidate_enrich import enrich_candidates
        enrich_stats = enrich_candidates(
            candidates, ms,
            gap_scan=(wake_has_gap_scan(wake_reason)),
            enrich_fundamentals=bool(scfg_enrich.get("enrich_fundamentals", True)),
            enrich_flows=bool(scfg_enrich.get("enrich_flows", True)),
            gap_enrich_max=int(scfg_enrich.get("gap_enrich_max", 25)),
            patch_missing_max=int(scfg_enrich.get("patch_missing_fundamentals_max", 5)),
        )
        if any(enrich_stats.values()):
            log.info("후보 enrich fundamentals=%d flows=%d",
                     enrich_stats.get("fundamentals", 0), enrich_stats.get("flows", 0))
        ondemand_n = 0
        if tier == "focus" and scfg.get("ondemand_flows"):
            try:
                flows = serve.fetch_ondemand_flows(
                    [c["symbol"] for c in candidates])
                ondemand_n += serve.patch_candidate_flows(candidates, flows)
            except Exception as e:
                log.warning("focus 온디맨드 flows 실패(배치값 유지): %s", e)
        if tier == "focus" and scfg.get("ondemand_news"):
            try:
                news = serve.fetch_ondemand_news(
                    [c["symbol"] for c in candidates])
                ondemand_n += serve.patch_candidate_news(candidates, news)
            except Exception as e:
                log.warning("focus 온디맨드 news 실패(배치값 유지): %s", e)
        earnings = self._earnings()              # 장전 배치 산출(없으면 {})
        for c in candidates:                     # Athena 도시를 후보 피처로(있는 것만)
            d = self._dossier_brief(c["symbol"])
            if d:
                c["dossier"] = d
            e = earnings.get(c["symbol"])        # 발표 임박(-3~21일) 종목만 — 소음 차단
            if earnings_near(e):
                c["earnings"] = with_fresh_dday(e)
        # 종목별 과거 거래 회고(lessons) — 이력 있는 후보에만 past_trades 부착(LLM 0콜).
        if self.store and agents_cfg.get("lessons", True):
            lessons = build_symbol_lessons(self.store, [c["symbol"] for c in candidates])
            for c in candidates:
                pt = lessons.get(c["symbol"])
                if pt:
                    c["past_trades"] = pt
        # 매크로 민감 태그(정적 맵) + 주의층 렌즈 — dday 신선도를 위해 사이클마다 계산.
        attach_macro_tags(candidates, sector_map_from_universe(self.cfg))
        portfolio = self._portfolio(earnings, live_prices=live_prices or None)
        focus = build_focus(ms, candidates=candidates,
                            positions=portfolio.get("positions") or [],
                            wake=wake)
        llm = (self.decision_llm_fn(wake) if self.decision_llm_fn
               else self.llm_factory(candidates))
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
        hl_lim = scfg.get("focus_headline_limit") if tier == "focus" else None
        compact = bool(scfg.get("compact_json")) and tier == "focus"
        wake_ctx = (wake if wake and (wake.get("reason") or wake.get("triggers"))
                    else None)
        notify_hl = bool(scfg.get("focus_trim_notify", False)) if tier == "focus" else True
        context = build_context(ms, candidates, portfolio, constraints,
                                track_record=(track_record(self.store)
                                              if self.store else None),
                                recent_disclosures=discs,
                                earnings_results=ers,
                                focus=focus,
                                wake=wake_ctx,
                                tier=tier,
                                headline_limit=hl_lim,
                                headline_ttl_hours=float(
                                    scfg.get("headline_ttl_hours", 24)),
                                focus_macro_pad=int(scfg.get("focus_macro_pad", 8)),
                                notify_headline_trim=notify_hl,
                                compact=compact,
                                strategy_scores_asof=scores_asof,
                                strategy_scores_stale=scores_stale)
        if self.store:
            try:
                from datetime import datetime, timezone

                def _age_sec(raw) -> float | None:
                    if not raw:
                        return None
                    try:
                        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        return round((datetime.now(timezone.utc) - dt).total_seconds(), 1)
                    except (TypeError, ValueError):
                        return None

                fast_asof = ms.get("fast_asof") or ms.get("asof")
                batch_asof = ms.get("batch_asof")
                self.store.log_event("brain_serve", None, {
                    "tier": tier,
                    "n_candidates": len(candidates),
                    "n_items": len(items),
                    "n_items_before": n_items_before,
                    "scan_shortlist": scan_shortlist,
                    "enrich_strategy": enrich_strategy,
                    "context_bytes": len(context.encode("utf-8")),
                    "ondemand_n": ondemand_n,
                    "asof": ms.get("asof"),
                    "fast_asof": fast_asof,
                    "batch_asof": batch_asof,
                    "asof_age_sec": _age_sec(fast_asof),
                    "fast_asof_age_sec": _age_sec(fast_asof),
                    "batch_asof_age_sec": _age_sec(batch_asof),
                    "strategy_scores_stale": scores_stale,
                    "reason": (wake or {}).get("reason") if wake else None,
                    "decision_tier": (
                        decision_tier(wake, agents_cfg=self.cfg.raw.get("agents"))
                        if self.decision_llm_fn else None),
                })
            except Exception as e:
                log.warning("brain_serve 관측 로깅 실패: %s", e)
        feat_map = attach_event_features(
            {c["symbol"]: c for c in candidates}, discs, ers)
        mlc = agents_cfg.get("min_lot_conviction")
        if mlc is None and self.cfg.risk.get("allow_min_lot"):
            mlc = self.min_conv
        conv_sz = bool(agents_cfg.get("conviction_sizing", True))
        if conv_sz and self.store:
            from ..calibration import sizing_enabled
            conv_sz = sizing_enabled(self.store, configured=True)
        res = run_cycle(context_json=context, decision_agent=DecisionAgent(llm),
                        validation_agent=ValidationAgent(val_llm, min_conviction=self.brain_min_conv),
                        broker=self.broker, risk=self.risk, price_lookup=price_lookup,
                        journal_path=self.journal_path,
                        arm_fn=(self._arm if self.store else None),
                        # 도시 우선 원칙(스윙/장투): store 있고 config 로 켜져 있을 때만
                        dossier_fn=(self._has_bullish_dossier
                                    if self.store and agents_cfg.get("require_dossier", True)
                                    else None),
                        # 갭 진입 가드(스윙/장투): store 있고 config 로 켜져 있을 때만
                        zone_fn=(self._entry_zone
                                 if self.store and agents_cfg.get("entry_zone_guard", True)
                                 else None),
                        entry_zone_tolerance_pct=float(
                            agents_cfg.get("entry_zone_tolerance_pct", 0.005)),
                        conviction_sizing=conv_sz,
                        min_lot_conviction=float(mlc) if mlc is not None else None,
                        apply_code_conviction=bool(agents_cfg.get("conviction_code", True)),
                        dossier_brief_fn=(self._dossier_brief if self.store else None),
                        features_by_sym=feat_map,
                        market_fn=self.market_of,
                        resolve_price_fn=self._resolve_price,
                        store=self.store,
                        wake_reason=wake_reason)
        self._record(res)
        if self.store:
            from ..shadow_ledger import book_blocked, book_soft_pending
            book_blocked(self.store, res, price_lookup, sleeve="brain",
                         cfg=self.cfg.raw)
            book_soft_pending(self.store, res, price_lookup, sleeve="brain",
                              cfg=self.cfg.raw)
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
        if sym in held or sym in pending or self.broker.position(sym).qty > 0:
            return False
        horizon = proposal.horizon or "day"
        strat, params = resolve_strategy(self.cfg, sym, proposal)   # 뇌 선택 우선(클램프)
        d = self._dossier_brief(sym)             # day 는 도시에 없어도 arm 가능(예외 경로)
        agents_cfg = self.cfg.raw.get("agents", {})
        mlc = agents_cfg.get("min_lot_conviction")
        if mlc is None and self.cfg.risk.get("allow_min_lot"):
            mlc = self.min_conv
        meta = {"horizon": horizon, "params": params,
                # 사이징은 코드 base(config) — LLM target_weight 는 저널/하위호환용
                "base_position_pct": float(getattr(self.risk, "base_position_pct", 0.20)),
                "max_position_pct": float(getattr(self.risk, "max_position_pct", 0.25)),
                "conviction_size_floor": float(
                    getattr(self.risk, "conviction_size_floor", 0.75)),
                "conviction_size_span": float(
                    getattr(self.risk, "conviction_size_span", 0.25)),
                "target_weight": float(getattr(self.risk, "base_position_pct", 0.20)),
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
        """페이퍼 계좌 ↔ store.positions 정합화(멱등). broker 락 안에서 실행."""
        if not self.store:
            return
        self.broker.run_locked(lambda acct: self._sync_store_positions_locked(res, acct))

    def _sync_store_positions_locked(self, res: CycleResult, acct) -> None:
        """execute+mirror 후 orphan 승격·안전망 청산·메타 보강."""
        from ..store_sync import is_orphan_store_row, sync_open_qty, _row_get

        prop_by_sym = {p.symbol: p for p in res.decision.proposals}
        fill_by_sym = {
            e["symbol"]: e for e in res.executed
            if e.get("status") in ("filled", "partial")
            and e.get("action") in ("BUY", "SELL")
        }
        open_rows = {r["symbol"]: r for r in self.store.get_open_positions()}
        for sym, pos in list(acct.positions.items()):
            if not pos.is_open:
                continue
            if sym in open_rows:
                row = open_rows[sym]
                if is_orphan_store_row(row):
                    market = acct.symbol_market.get(sym, "KR")
                    prop = prop_by_sym.get(sym)
                    horizon = getattr(prop, "horizon", "swing") or "swing"
                    strat, params = resolve_strategy(self.cfg, sym, prop)
                    d = self._dossier_brief(sym)
                    stop, target, stop_note = combine_stop_target(
                        pos.avg_price, horizon, params,
                        (d or {}).get("invalidation"), (d or {}).get("target"))
                    meta = {"horizon": horizon, "params": params,
                            "entry_regime": self._regime_now.get(market),
                            "dossier_id": (d["id"] if d else None),
                            "conviction": getattr(prop, "conviction", None) if prop else None,
                            "manager_epoch": (res.manager or {}).get("epoch") if res else None}
                    from ..thesis_watch import default_spec_from_dossier
                    meta["thesis_invalidation"] = default_spec_from_dossier(d, horizon)
                    if stop_note:
                        meta["stop_note"] = stop_note
                    item = self._universe_item(sym)
                    if item and item.get("source"):
                        meta["source"] = item["source"]
                    self.store.update_position(
                        row["id"], qty=pos.qty, avg_price=pos.avg_price,
                        strategy=strat, thesis=(prop.thesis if prop else _row_get(row, "thesis")),
                        target_price=target, stop_price=stop, meta=meta)
                elif (abs(float(row["qty"] or 0) - pos.qty) > 1e-9
                      or abs(float(row["avg_price"] or 0) - pos.avg_price) > 1e-9):
                    fe = fill_by_sym.get(sym) or {}
                    sync_open_qty(
                        self.store, row, sym, pos.qty, pos.avg_price, acct,
                        exit_price=fe.get("avg_price") if fe.get("action") == "SELL" else None,
                        reason="brain")
                else:
                    self.store.disarm_symbol(sym)
                continue
            market = acct.symbol_market.get(sym, "KR")
            prop = prop_by_sym.get(sym)
            horizon = getattr(prop, "horizon", "swing") or "swing"
            strat, params = resolve_strategy(self.cfg, sym, prop)
            d = self._dossier_brief(sym)
            stop, target, stop_note = combine_stop_target(
                pos.avg_price, horizon, params,
                (d or {}).get("invalidation"), (d or {}).get("target"))
            meta = {"horizon": horizon, "params": params,
                    "entry_regime": self._regime_now.get(market),
                    "dossier_id": (d["id"] if d else None),
                    "conviction": getattr(prop, "conviction", None) if prop else None,
                    "manager_epoch": (res.manager or {}).get("epoch") if res else None}
            from ..thesis_watch import default_spec_from_dossier
            meta["thesis_invalidation"] = default_spec_from_dossier(d, horizon)
            if stop_note:
                meta["stop_note"] = stop_note
            item = self._universe_item(sym)
            if item and item.get("source"):
                meta["source"] = item["source"]
            self.store.open_position(
                sym, market, pos.qty, pos.avg_price, strategy=strat,
                thesis=(prop.thesis if prop else None),
                target_price=target, stop_price=stop, meta=meta)
            self.store.disarm_symbol(sym)
            from ..shadow_ledger import cancel_shadow_on_fill
            cancel_shadow_on_fill(self.store, sym)
        for sym, row in open_rows.items():
            p = acct.positions.get(sym)
            if p is None or not p.is_open:
                fe = fill_by_sym.get(sym) or {}
                exit_px = (fe.get("avg_price") if fe.get("action") == "SELL" else None
                           or next((f.price for f in reversed(acct.journal)
                                    if f.symbol == sym and f.side == "SELL"), None))
                fee = next((f.fee for f in reversed(acct.journal)
                            if f.symbol == sym and f.side == "SELL"), 0.0)
                self.store.close_position(row["id"], exit_price=exit_px, reason="brain",
                                          fee=fee)

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
                         "dossier_id": (d["id"] if d else None),
                         "manager": res.manager})   # 매니저 에포크 귀속
        self.store.log_event("cycle", None, {"market_view": res.decision.market_view,
                                             "executed": res.executed,
                                             "manager": res.manager})
