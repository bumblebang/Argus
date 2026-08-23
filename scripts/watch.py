"""Argus-Watch 상시 감시 루프 진입점 (M1 감시 + M2 뇌 연결).

  python scripts/watch.py                 # 상시 가동(장중 폴링→트리거→뇌 각성)
  python scripts/watch.py --ticks 3       # 3틱만 실행하고 종료(점검용)
  python scripts/watch.py --once          # 1틱 실행 후 결과 출력(각성 시 동기 1사이클)
  python scripts/watch.py --dry           # 뇌 백엔드를 MockLLM 로(배선 검증)
  python scripts/watch.py --no-brain      # 감시만(뇌 비활성)

단일 프로세스·단일 Gateway 로 토스를 만진다(토큰 1개 정책).
broker.mode 와 DRY_RUN 에 따라 페이퍼 또는 라이브. act 트리거가 뜨면 BrainWorker
(별도 스레드)가 결정→검증→하드게이트→사이클을 돈다. 느린 뇌가 감시 루프를 막지 않게
비동기·단일실행.
"""
from __future__ import annotations

import argparse
import atexit
import json
import os
import sys
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS.parent))
sys.path.insert(0, str(_SCRIPTS))   # 같은 폴더의 dashboard.py 임포트용(scripts 는 패키지 아님)

from src.config import load_config
from src.day_pool import (day_pool_cfg, load_day_pool, merge_swing_and_day,
                          refresh_all_day_pools)
from src.logging_setup import setup_logging, get_logger
from src.engine.singleton import SingleInstance, AlreadyRunning
from src.engine.store import Store
from src.engine.disclosure import DisclosureWatcher, start_watcher_thread
from src.engine.earnings_watch import (EarningsResultWatcher,
                                       start_earnings_watcher_thread)
from src.engine.slice_refresher import SliceRefresher, start_slice_refresher_thread
from src.engine.universe_refresher import (UniverseRefresher,
                                          start_universe_refresher_thread)
from src.universe_roll import core_refresh, mover_scan
from src.market_hours import is_open, is_tradable, calendar_covers
from src.live_slice import build_fast_slice, apply_fast_slice
from src.datasources.dart import fetch_recent_disclosures, fetch_earnings_actuals
from src.datasources.earnings import dday_of, fetch_us_results
from src.datasources.market_calendar import refresh_sessions
from src.datasources.account_snapshot import fetch_account_snapshot, save_snapshot
from src.datasources.stock_info import (check_tradable, is_sell_tax_exempt,
                                        load_info_cache, load_warn_cache)
from src.engine.gateway import TossGateway
from src.engine.loop import WatchLoop, WatchConfig, build_watchlist
from src.engine.universe_provider import UniverseProvider
from src.engine.brain import BrainWorker
from src.engine.execution import ExitExecutor, EntryExecutor
from src.engine.strategy_runner import StrategyRunner
from src.agents.pipeline import (CycleRunner, select_backend, build_live_llm,
                                 synth_candles, dry_llm_factory, build_paper_core,
                                 entry_stop_target)
from src.agents.llm import ClaudeCLIClient
from src.agents.value_trade import ValueRunner, value_trade_cfg
from src.broker_sync import (apply_reconcile_from_live,
                             fetch_live_account_data, should_sync)

log = get_logger("watch")

_KST = ZoneInfo("Asia/Seoul")

# Windows 절전 차단: 상주 프로세스가 도는 동안 시스템 슬립 금지(디스플레이는 꺼져도 됨).
_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001


def _keep_awake(enable: bool) -> None:
    """SetThreadExecutionState 로 시스템 슬립 방지/해제 (Windows 한정, best-effort)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        flags = (_ES_CONTINUOUS | _ES_SYSTEM_REQUIRED) if enable else _ES_CONTINUOUS
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
        log.info("절전 차단 %s", "ON" if enable else "OFF")
    except Exception as e:
        log.warning("절전 차단 설정 실패(무시): %s", e)


def _refresh_day_pool(gateway, cfg) -> None:
    """뇌 사이클 직전 토스 거래대금 랭킹으로 day_pool 을 맞춘다. 실패는 기존 파일."""
    if not day_pool_cfg(cfg).get("enabled", True):
        return
    if getattr(gateway, "get_rankings", None) is None:
        return

    def fetch(rank_type, market_country, duration, count):
        return gateway.get_rankings(rank_type=rank_type, market_country=market_country,
                                    duration=duration, count=count)

    refresh_all_day_pools(fetch, cfg)


def _build_brain(cfg, gateway, store, args, broker, risk, universe_fn=None,
                 open_markets_fn=None, illiquid_fn=None) -> BrainWorker | None:
    """act 트리거 시 LLM 사이클을 비동기로 돌릴 BrainWorker 구성. --no-brain 이면 None.

    백엔드: --dry/--cli/--live 우선, 없으면 config watch.brain_backend(기본 cli=구독무료).
    캔들은 Gateway 로(단일 토스 통로). broker/risk 는 코드 청산과 공유.
    """
    if args.no_brain:
        log.info("brain 비활성(--no-brain) — 감시+코드청산만 수행.")
        return None
    api_key = os.getenv("ANTHROPIC_API_KEY")
    wcfg = cfg.raw.get("watch", {})
    bk = ("dry" if args.dry else "live" if args.live else "cli" if args.cli
          else wcfg.get("brain_backend", "cli"))
    dry, use_cli, subscription = select_backend(dry=(bk == "dry"), cli=(bk == "cli"),
                                                live=(bk == "live"), api_key=api_key)
    val_llm_factory = None
    if dry:
        log.warning("brain backend=DRY (MockLLM, 합성가격) — 검증용. 실거래판단 아님.")
        llm_factory, fetch = dry_llm_factory, synth_candles
    else:
        acfg = cfg.raw.get("agents", {})
        try:
            llm = build_live_llm(cfg, use_cli=use_cli, subscription=subscription,
                                 api_key=api_key)
            # 검증 전용 모델(선택): 결정과 다른 티어로 독립성 확보. 미설정이면 결정과 공유.
            val_model = (acfg.get("claude_val_model") if use_cli
                         else acfg.get("val_model"))
            val_llm = llm
            if val_model:
                val_llm = build_live_llm(cfg, use_cli=use_cli, subscription=subscription,
                                         api_key=api_key, model_override=val_model)
        except Exception as e:
            log.error("brain LLM 초기화 실패 → 감시만 진행: %s", e)
            return None
        llm_factory = lambda cands: llm
        val_llm_factory = (lambda cands: val_llm) if val_llm is not llm else None
        fetch = lambda s, m: gateway.candles(s, "1d", 30)
        dec_m = (acfg.get("claude_model") if use_cli else acfg.get("model")) or "default"
        log.info("brain backend=%s (결정=%s, 검증=%s)",
                 "cli(구독)" if use_cli else ("api-key" if api_key else "api-oauth"),
                 dec_m, (val_model or dec_m))
    runner = CycleRunner(cfg, llm_factory=llm_factory, fetch_candles=fetch, store=store,
                         broker=broker, risk=risk, val_llm_factory=val_llm_factory,
                         universe_fn=universe_fn,    # 뇌도 런타임 유니버스를 매 사이클 재독
                         open_markets_fn=open_markets_fn,   # 닫힌 시장 후보 제외(opt-in)
                         illiquid_fn=illiquid_fn)    # 시간외 체결정지 종목 후보 제외(opt-in)

    def cycle(wake=None):
        try:
            _refresh_day_pool(gateway, cfg)
        except Exception as e:
            log.warning("day_pool 리프레시 실패(기존 유지): %s", e)
        return runner.run(wake=wake)

    cooldown = float(wcfg.get("brain_cooldown_sec", 300))
    # 뇌 모드/회로차단: data/brain_mode.json + Cursor bridge heartbeat 게이트.
    from src.agents.pipeline import DATA, build_cursor_bridge
    acfg = cfg.raw.get("agents", {})
    cb = (acfg.get("cursor_bridge") or {})
    mode_path = DATA / "brain_mode.json"
    bridge = build_cursor_bridge(cfg) if not dry else None
    inbox_dir = (bridge.inbox_dir if bridge is not None
                 else (cb.get("inbox_dir") or str(DATA / "llm_inbox")))
    source_fn = None
    if not dry:
        # llm 은 위 else 분기에서 생성된 ClaudeCLIClient(또는 API 클라이언트).
        _llm_ref = llm  # type: ignore[name-defined]

        def source_fn():
            return getattr(_llm_ref, "last_source", None)

    return BrainWorker(
        cycle, store=store, cooldown_sec=cooldown,
        mode_path=mode_path,
        bridge_inbox_dir=inbox_dir,
        bridge_armed_max_age_sec=float(cb.get("armed_max_age_sec", 90)),
        circuit_fail_threshold=int(wcfg.get("circuit_fail_threshold", 2)),
        source_fn=source_fn,
    )


def _build_value_worker(cfg, gateway, store, broker, risk, args) -> BrainWorker | None:
    """밸류 트랙 워커(하루 1회 저평가주 진입 판단). config value_trade.enabled 이고 dry/
    no-brain 아닐 때만. 결정=ClaudeCLIClient(value_trade.model), 검증=brain 과 동일 구성
    (claude_val_model). price_fn 은 gateway 배치 시세. broker/risk 는 데몬 공유 인스턴스.

    BrainWorker(kind_prefix="value", cooldown 3600)로 감싸 반환 — 타이머가 하루 1회 wake.
    """
    vt = value_trade_cfg(cfg)
    if not vt["enabled"]:
        return None
    if args.dry or args.no_brain:
        log.info("밸류 워커 비활성(--dry/--no-brain).")
        return None
    acfg = cfg.raw.get("agents", {})
    try:
        dec_llm = ClaudeCLIClient(command=acfg.get("claude_command", "claude"),
                                  model=vt["model"], timeout=vt["timeout"],
                                  fallback_model=(acfg.get("claude_fallback_model") or None),
                                  error_dump_path="data/value_trade_cli_error.json")
        # 검증 LLM: 브레인의 val 구성 방식(claude_val_model, 독립 티어). 미설정이면 결정과 공유.
        val_model = acfg.get("claude_val_model")
        val_llm = dec_llm
        if val_model:
            val_llm = build_live_llm(cfg, use_cli=True, subscription=False,
                                     api_key=None, model_override=val_model)
    except Exception as e:
        log.error("밸류 워커 LLM 초기화 실패 → 밸류 트랙 비활성: %s", e)
        return None

    def price_fn(symbols, market):
        rows = gateway.poll_prices(symbols, record=False)
        return {r["symbol"]: r["price"] for r in rows if r.get("price")}

    llm_factory = lambda cands: dec_llm
    val_llm_factory = (lambda cands: val_llm) if val_llm is not dec_llm else None
    runner = ValueRunner(cfg, store, broker, risk, llm_factory, val_llm_factory,
                         price_fn=price_fn)
    log.info("밸류 워커 배선 — 결정=%s, 검증=%s, windows=%s",
             vt["model"], (val_model or vt["model"]), vt["windows"])
    return BrainWorker(cycle_fn=runner.run, store=store, cooldown_sec=3600,
                       kind_prefix="value")


def _start_value_timer(worker, cfg, markets) -> threading.Event:
    """하루 1회 wake 를 거는 가벼운 타이머 스레드(60초 간격). 실제 due/중복 판정은
    ValueRunner.run() 이 state 파일로 최종 처리하므로 타이머는 러프해도 된다.

    조건: 시장이 장중 & KST 시각 ≥ value_trade.windows[market] & 오늘 아직 미wake.
    """
    vt = value_trade_cfg(cfg)
    windows = vt["windows"]
    stop = threading.Event()
    woken: dict[str, str] = {}          # market -> 마지막 wake KST 날짜(중복 wake 방지)

    def loop():
        while not stop.wait(60):
            try:
                now = datetime.now(_KST)
                today, hhmm = now.strftime("%Y%m%d"), now.strftime("%H:%M")
                for m in markets:
                    if m not in windows or not is_open(m):
                        continue
                    if hhmm >= windows[m] and woken.get(m) != today:
                        worker.wake("value_daily")
                        woken[m] = today
            except Exception as e:
                log.warning("밸류 타이머 오류(무시): %s", e)

    threading.Thread(target=loop, name="value_timer", daemon=True).start()
    return stop


def _check_calendar_coverage(markets, store=None) -> list:
    """휴장일 캘린더가 올해를 담고 있는지 점검. 미커버 시장은 ERROR 로그 + 이벤트.

    정적 캘린더(market_hours.HOLIDAYS)는 연 1회 손갱신이라, 해가 바뀌면 is_open 이 요일만
    보게 되어 휴장일에도 폴링·뇌 각성·주문 시도가 일어난다. 조용히 지나치지 않게 기동 시와
    주기(세션 갱신 스레드)마다 확인한다. 반환: 미커버 시장 목록."""
    missing = [m for m in markets if not calendar_covers(m)]
    if missing:
        log.error("휴장일 캘린더 미갱신 — %s 의 올해 휴장일이 없습니다. "
                  "src/market_hours.py HOLIDAYS 를 갱신하세요(미갱신 시 휴장일에도 "
                  "폴링·뇌 각성·주문 시도가 발생).", ", ".join(missing))
        if store is not None:
            try:
                store.log_event("calendar_stale", None, {"markets": missing})
            except Exception as e:
                log.warning("캘린더 경고 이벤트 기록 실패(무시): %s", e)
    return missing


def _start_session_refresher(gateway, markets, interval_sec: float = 1800,
                             store=None) -> threading.Event:
    """시장 세션표(프리/정규/애프터)를 주기적으로 재갱신하는 가벼운 데몬 스레드."""
    stop = threading.Event()

    def loop():
        while not stop.wait(interval_sec):
            try:
                gateway.refresh_market_sessions(markets)
            except Exception as e:
                log.warning("세션표 주기 갱신 오류(무시): %s", e)
            _check_calendar_coverage(markets, store)

    threading.Thread(target=loop, name="session_refresher", daemon=True).start()
    return stop


def _start_account_refresher(gateway, cfg) -> threading.Event | None:
    """실계좌 스냅샷을 주기 조회(기본 300s)해 data/account_snapshot.json 에 캐시하는
    가벼운 데몬 스레드. gateway.client(단일 토큰)로만 조회 — 대시보드는 이 캐시만 읽는다
    (토큰 충돌 방지). 라이브/페이퍼 무관하게 client 가 있으면 띄운다(관제용 조회).

    account_seq 는 config broker.account_seq. markets 는 run.trade_markets. 조회 예외는
    삼켜 로깅(대시보드는 마지막 캐시 유지). 데몬 시작 시 1회 즉시 조회 후 주기 반복.
    client 없으면(키 없음) None 을 반환하고 스레드를 띄우지 않는다. Event.set() 으로 정지."""
    client = getattr(gateway, "client", None)
    if client is None:
        return None
    wcfg = cfg.raw.get("watch", {})
    interval_sec = float(wcfg.get("account_snapshot_sec", 300))
    account_seq = (cfg.raw.get("broker", {}) or {}).get("account_seq")
    markets = tuple(cfg.run.get("trade_markets", ["KR"]))
    stop = threading.Event()

    def _once():
        try:
            from src.datasources.account_snapshot import save_snapshot
            save_snapshot(gateway.fetch_account_snapshot(account_seq, markets=markets))
        except Exception as e:
            log.warning("자산 스냅샷 조회 오류(무시, 마지막 캐시 유지): %s", e)

    _once()  # 시작 시 1회 즉시(대시보드가 곧바로 실계좌를 보게)

    def loop():
        while not stop.wait(interval_sec):
            _once()

    threading.Thread(target=loop, name="account_refresher", daemon=True).start()
    return stop


def _start_reconcile_timer(broker, gateway, store, cfg, markets) -> threading.Event | None:
    """라이브 원장 주기 재대사 타이머(라이브만). broker.reconcile(락) 경유로 실계좌
    (holdings+buying-power)를 봇 원장에 병합해 드리프트를 없앤다 — 체결가 근사 오차·수수료·
    수동 계좌변경·기업행위·미체결 지연 등 모든 잔여 드리프트를 주기적으로 자가치유한다.

    주기=broker.reconcile_sec(기본 300s). 봇 관리 포지션의 thesis/손절/목표는 보존, 고아는
    채택, 유령은 청산(reconcile_from_live). 페이퍼면 None(스킵). Event.set() 으로 정지."""
    if getattr(broker, "mode", "paper") != "live":
        return None
    sec = float((cfg.raw.get("broker", {}) or {}).get("reconcile_sec", 300))
    seq = broker.account_seq
    stop = threading.Event()

    def _once():
        try:
            data = fetch_live_account_data(gateway, seq, markets=tuple(markets))
            res = broker.reconcile(
                lambda acct: apply_reconcile_from_live(
                    acct, store, data, markets=tuple(markets)))
            if res.get("adopted") or res.get("closed") or res.get("error"):
                store.log_event("reconcile", None, res)
        except Exception as e:
            log.warning("주기 재대사 오류(무시): %s", e)

    _once()

    def loop():
        while not stop.wait(sec):
            _once()

    threading.Thread(target=loop, name="reconcile_timer", daemon=True).start()
    return stop


def _earnings_calendar_reader(path: Path):
    """data/earnings_calendar.json 을 mtime 캐시로 읽는 얇은 리더 반환.

    워처가 10초마다 도는데 매번 디스크를 읽으면 낭비 — 파일이 바뀐 때만 다시 파싱한다.
    실패(없음·깨짐)는 빈 dict 로 (워처는 예전 그대로 동작).
    """
    cache: dict = {"mtime": None, "symbols": {}}

    def read() -> dict:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return {}
        if cache["mtime"] != mtime:
            try:
                cache["symbols"] = json.loads(
                    path.read_text(encoding="utf-8")).get("symbols", {}) or {}
            except (OSError, ValueError) as e:
                log.warning("실적 캘린더 로드 실패(생략): %s", e)
                cache["symbols"] = {}
            cache["mtime"] = mtime
        return cache["symbols"]

    return read


def _build_disclosure_watcher(cfg, store, brain,
                              universe_fn=None) -> DisclosureWatcher | None:
    """DART 공시 워처 구성. 키 없거나 config 로 끄면 None(감시·매매는 무영향).

    universe_fn(=UniverseProvider.markets) 이 있으면 KR 감시 종목도 핫리로드된다
    (screen 재생성 시 새 종목의 공시가 큐/각성 라우팅에 잡히게).
    실적 캘린더가 있으면 실적 공시에 컨센서스를 붙이고, 발표 임박(D-1)엔 폴링 창을
    넓힌다(watcher.earnings_consensus 로 끌 수 있음).
    """
    wcfg = cfg.raw.get("watcher", {})
    if not wcfg.get("enabled", True):
        return None
    dart_key = os.getenv("DART_API_KEY")
    if not dart_key:
        log.warning("DART_API_KEY 없음 → 공시 워처 비활성(시세 감시는 정상).")
        return None

    def universe_kr() -> set:
        uni = universe_fn() if universe_fn else (cfg.universe or {})
        return {it.get("symbol") for it in (uni or {}).get("KR", [])
                if it.get("symbol")}

    earnings_fn = imminent_fn = None
    if wcfg.get("earnings_consensus", True):
        read_cal = _earnings_calendar_reader(_SCRIPTS.parent / "data" / "earnings_calendar.json")

        def earnings_fn(symbol: str) -> dict | None:      # noqa: F811 (config 로 끄면 None)
            return read_cal().get(symbol)

        def imminent_fn() -> bool:                        # noqa: F811
            """KR 종목 중 오늘/내일 발표가 하나라도 있으면 True(폴링 창 확장).

            캘린더의 dday 는 장전 배치 시점 값이라 배치가 밀리면 낡는다 — 데몬은
            며칠씩 붙어 도니 반드시 오늘 기준으로 다시 계산한다(안 그러면 정작 D-1 에
            안 켜지거나, 지나간 D-1 로 계속 켜진 채 DART 쿼터를 태운다).
            """
            return any(e.get("market") == "KR" and dday_of(e) in (0, 1)
                       for e in read_cal().values() if isinstance(e, dict))

    return DisclosureWatcher(
        store, lambda: fetch_recent_disclosures(dart_key), universe_kr,
        on_wake=(brain.wake if brain else None),
        poll_active_sec=float(wcfg.get("dart_poll_sec_active", 10)),
        poll_idle_sec=float(wcfg.get("dart_poll_sec_idle", 60)),
        after_close_hours=float(wcfg.get("active_after_close_hours", 2)),
        earnings_fn=earnings_fn,
        actuals_fn=((lambda rcept_no: fetch_earnings_actuals(dart_key, rcept_no))
                    if wcfg.get("earnings_actuals", True) else None),
        imminent_fn=imminent_fn,
        imminent_after_close_hours=float(wcfg.get("imminent_after_close_hours", 4)))


def _build_earnings_watcher(cfg, store, brain,
                            universe_fn=None) -> EarningsResultWatcher | None:
    """실적 결과(서프라이즈) 워처 구성. 키 없거나 config 로 끄면 None(감시·매매는 무영향).

    발표 예정일·컨센서스는 이미 캘린더에 있다 — 이 워처가 채우는 건 '발표된 실제 숫자'다.
    대상은 US 유니버스 ∪ 보유 US 종목(둘 다 폴링 시점에 재계산 → 유니버스 핫리로드·신규
    매수가 다음 폴부터 자동 반영). 폴링 주기는 캘린더의 발표 임박 여부가 정한다.
    """
    wcfg = cfg.raw.get("watcher", {})
    # watcher.enabled 는 이 블록 전체의 킬스위치다 — 장애 때 하나만 꺼지면 함정이 된다.
    if not wcfg.get("enabled", True) or not wcfg.get("earnings_result", True):
        return None
    fh_key = os.getenv("FINNHUB_API_KEY")
    if not fh_key:
        log.warning("FINNHUB_API_KEY 없음 → 실적 결과 워처 비활성(감시·매매는 정상).")
        return None

    def universe_us() -> set:
        uni = universe_fn() if universe_fn else (cfg.universe or {})
        return {it.get("symbol") for it in (uni or {}).get("US", [])
                if it.get("symbol")}

    def held_us() -> set:
        """보유 US 종목 — 유니버스에서 빠진 뒤에도 내 포지션 실적은 놓치면 안 된다."""
        try:
            return {r["symbol"] for r in store.get_open_positions()
                    if r["market"] == "US"}
        except Exception as e:
            log.warning("보유 US 종목 조회 실패(무시): %s", e)
            return set()

    def fetch() -> dict:
        symbols = sorted(universe_us() | held_us())
        return fetch_us_results(fh_key, symbols) if symbols else {}

    read_cal = _earnings_calendar_reader(_SCRIPTS.parent / "data" / "earnings_calendar.json")
    return EarningsResultWatcher(
        store, fetch, universe_us, on_wake=(brain.wake if brain else None),
        poll_sec=float(wcfg.get("earnings_poll_sec", 600)),
        idle_poll_sec=float(wcfg.get("earnings_idle_poll_sec", 3600)),
        calendar_fn=read_cal)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="1틱만 실행하고 결과 출력")
    ap.add_argument("--ticks", type=int, default=None, help="N틱 실행 후 종료(점검용)")
    ap.add_argument("--dry", action="store_true", help="뇌 백엔드를 MockLLM 로(배선 검증)")
    ap.add_argument("--cli", action="store_true", help="뇌 백엔드 claude CLI(구독)")
    ap.add_argument("--live", action="store_true", help="뇌 백엔드 API 키")
    ap.add_argument("--no-brain", action="store_true", help="감시만(뇌 비활성)")
    args = ap.parse_args()
    # 데몬은 무콘솔(pythonw)로 돌므로 전용 파일에 로깅(logs/watch.log).
    setup_logging("INFO", log_file="watch.log")

    cfg = load_config()
    # 감시 루프(M1)는 시세·캔들만 폴링 -> 인증(client_id/secret)만 있으면 된다.
    # account_no 는 잔고/주문(M2+)에서만 필요하므로 여기선 요구하지 않는다.
    need = [k for k, v in {"TOSS_CLIENT_ID": cfg.creds.client_id,
                           "TOSS_CLIENT_SECRET": cfg.creds.client_secret}.items() if not v]
    if need:
        log.error("토스 인증 누락: %s (.env 확인)", ", ".join(need))
        return 1

    # 단일 인스턴스 락 — 오펀/중복 기동 시 토스 토큰 경합(→401)·claude 동시 스폰 경합 방지.
    lock = SingleInstance(_SCRIPTS.parent / "data" / "watch.pid")
    try:
        lock.acquire()
    except AlreadyRunning as e:
        log.warning("다른 watch 인스턴스가 이미 실행 중(pid=%s) — 이 인스턴스는 종료합니다.", e.pid)
        return 0
    atexit.register(lock.release)

    run_cfg = cfg.raw.get("run", {})
    wcfg = cfg.raw.get("watch", {})
    store = Store(run_cfg.get("db_path", "data/bot.db"))
    gateway = TossGateway.from_config(cfg, store=store)
    markets = cfg.run.get("trade_markets", ["KR", "US"])
    heartbeat = run_cfg.get("heartbeat_path", "data/watch.heartbeat")
    # 런타임 유니버스 재독기: screen 이 data/universe.yaml 을 재생성하면(2회/일) 재기동 없이
    # 다음 markets() 호출부터 반영. 감시 루프(build_watchlist)와 뇌(CycleRunner)가 공유.
    provider = UniverseProvider(cfg)
    _uni0 = provider.markets()
    log.info("유니버스 핫리로드 활성 — 현재 시장=%s, 종목수=%d",
             list(_uni0.keys()), sum(len(v or []) for v in _uni0.values()))
    # 진입(뇌)과 청산(코드)이 같은 계좌를 공유. 라이브 배선(심층 방어): --dry 가 아니고
    # config broker.mode==live 이며 dry 아닐 때만 게이트웨이가 live_client 로 주입돼 실주문
    # 경로가 열린다. gateway 를 넘겨 gateway.place_order(레이트리미터+단일토큰)를 타게 한다
    # (브로커가 TossClient 를 새로 만들면 데몬 토큰 무효화 → 반드시 게이트웨이 재사용).
    broker_cfg = cfg.raw.get("broker", {}) or {}
    live_client = None if args.dry else gateway
    broker, risk = build_paper_core(cfg, live_client=live_client,
                                    account_seq=broker_cfg.get("account_seq"), store=store)
    # 라이브 전환 시 실계좌 → 봇 원장/store 미러링(감시 루프가 broker 를 쓰기 전에 1회).
    # 페이퍼면 스킵(config 초기값 유지). 라이브에서 동기화 실패는 심각 → ERROR + 이벤트,
    # 단 데몬은 계속(다음 사이클에 뇌가 어긋난 상태로 판단하지 않도록 로그로 크게 알린다).
    if should_sync(broker):
        try:
            _sync = broker.sync_from_live(gateway, store, markets=tuple(markets))
            log.info("실계좌 동기화 완료 — 현금=%s, 보유=%d종목", _sync["cash"], _sync["synced"])
            store.log_event("live_sync", None, _sync)
        except Exception as e:
            log.error("실계좌 동기화 실패(라이브에서 심각) — 데몬은 계속: %s", e)
            store.log_event("error", None, {"where": "live_sync", "error": str(e)})
    # 매수 안전가드 배선: 부적격 종목(관리/거래정지/상폐예정/ETF·ETN 등) 매수를 차단한다.
    # gateway.client(단일 토큰) 로만 조회. 키 없음/dry 면 주입 안 함(가드 비활성, 페이퍼 순수).
    # 캐시 dict 는 프로세스 내 공유(로드/세이브). 조회 실패는 fail-open(check_tradable 내부).
    _guard_gw = None if args.dry else gateway
    if _guard_gw is not None and getattr(_guard_gw, "client", None) is not None:
        _info_cache = load_info_cache()
        _warn_cache = load_warn_cache()
        broker.tradable_fn = lambda sym, mkt: _guard_gw.check_tradable(
            sym, mkt, info_cache=_info_cache, warn_cache=_warn_cache)
        log.info("매수가드=on(warnings/stockinfo) — 부적격 종목 매수 차단 활성")
        # 합성 매도세 면제(국내 ETF/ETN) — 같은 StockInfo 캐시를 재사용(네트워크 추가 없음).
        # 라이브는 체결 대사에서 실세금을 받으므로 이 경로를 타지 않는다(페이퍼 비용모델용).
        broker.account.sell_tax_exempt_fn = lambda sym, mkt: is_sell_tax_exempt(
            sym, mkt, _info_cache)
    # 거래 가능 세션인 시장만 뇌 후보로(국장 마감 후 KR 후보 제외). 청산은 portfolio 경유라 무관.
    # Athena 직후(athena_done)는 CycleRunner 가 프리 전이라도 live_markets 후보를 남긴다.
    # 감시 루프의 거래 게이트(WatchLoop.run_once)와 같은 판정을 써야 뇌가 못 사는 시장을 안 고른다.
    watch_config = WatchConfig.from_config(cfg.raw)
    open_markets_fn = lambda: [m for m in markets
                               if is_tradable(m, watch_config.trading_sessions.get(m))]
    # 유동성 illiquid 집합: 감시 루프(WatchLoop)가 매 틱 갱신하고 뇌(CycleRunner)가 읽는다.
    # 뇌는 별도 스레드라 루프의 락 안에서 스냅샷을 떠 간다(illiquid_snapshot).
    # loop 는 brain 을 on_wake 로 받아야 해서 아래에서 만들어진다 → 지연 참조 홀더로 연결.
    loop_ref: list = []
    def combined_universe():
        return merge_swing_and_day(provider.markets(), load_day_pool())

    brain = _build_brain(cfg, gateway, store, args, broker, risk,
                         universe_fn=combined_universe,
                         open_markets_fn=open_markets_fn,
                         illiquid_fn=lambda: loop_ref[0].illiquid_snapshot() if loop_ref else set())
    # 밸류 트랙 워커(하루 1회 저평가주 진입) — 데몬 공유 broker/risk 로 페이퍼 단일 원칙 유지.
    value_worker = _build_value_worker(cfg, gateway, store, broker, risk, args)
    executor = ExitExecutor(broker, store)
    strategy_runner = StrategyRunner(gateway, broker, store)
    # 코드 자율 진입: armed 종목에 전략 BUY 신호 시 매수(진입가 기준 손절/목표 확정).
    entry_executor = EntryExecutor(
        gateway, broker, risk, store,
        plan_fn=lambda price, hz, params: entry_stop_target(price, hz, params))
    loop = WatchLoop(gateway, store,
                     lambda: build_watchlist(cfg, store, universe=combined_universe()),
                     markets=markets, config=watch_config,
                     on_wake=(brain.wake if brain else None), executor=executor,
                     strategy_runner=strategy_runner, entry_executor=entry_executor,
                     on_prices=broker.set_marks,   # 미실현 손익을 게이트가 보게(락 안)
                     heartbeat_path=heartbeat)
    loop_ref.append(loop)                    # 뇌의 illiquid_fn 이 이제 루프를 볼 수 있다

    if args.once:
        res = loop.run_once()
        print(f"markets_open={res.markets_open} polled={res.polled} "
              f"triggers={len(res.triggers)} precision={res.precision} woke={res.woke}")
        for t in res.triggers:
            print(f"  [{t.urgency}] {t.kind:14} {t.symbol:8} {t.reason}")
        if brain and res.woke:
            print("  brain woke -> 1사이클 동기 실행(once 모드)...")
            brain.run_pending()
        return 0

    broker_tag = (f"LIVE({','.join(broker.live_markets)})" if broker.mode == "live"
                  else "PAPER")
    log.info("Argus-Watch 시작 — markets=%s, broker=%s, 장중 %ss / 휴장 %ss, brain=%s, heartbeat=%s",
             markets, broker_tag, loop.cfg.watch_interval_sec, loop.cfg.idle_interval_sec,
             "on" if brain else "off", heartbeat)
    if brain:
        brain.start()
    # 밸류 워커 + 하루 1회 wake 타이머(장중·윈도 지난 시각·오늘 미실행 시 wake).
    value_timer_stop = None
    if value_worker:
        value_worker.start()
        value_timer_stop = _start_value_timer(value_worker, cfg, markets)
        log.info("밸류 트랙 활성 — 감시 루프와 분리된 하루 1회 진입 판단(cooldown 3600s).")
    # 대시보드를 같은 프로세스 안 데몬 스레드로 병합(별도 pythonw 불필요, 읽기전용).
    if wcfg.get("dashboard", True):
        port = int(wcfg.get("dashboard_port", 8787))
        try:
            import dashboard as _dashboard
            _dashboard.start_background(port)
            log.info("대시보드 인프로세스 기동 — http://127.0.0.1:%d", port)
        except OSError as e:
            log.warning("대시보드 포트 사용 중(%d)일 수 있음 — 대시보드 생략: %s", port, e)
        except Exception as e:  # 대시보드 실패가 감시 루프를 막지 않게
            log.warning("대시보드 기동 실패(무시): %s", e)
    # 공시 워처: 별도 데몬 스레드(장중 10초 폴링 → 3단 라우팅 → 보유/armed 중대공시 각성).
    watcher_stop = None
    watcher = _build_disclosure_watcher(cfg, store, brain,
                                        universe_fn=provider.markets)
    if watcher:
        _t, watcher_stop = start_watcher_thread(watcher)
        log.info("공시 워처 기동 — 장중 %.0fs / 장외 %.0fs",
                 watcher.poll_active_sec, watcher.poll_idle_sec)
    # 실적 결과 워처: 별도 데몬 스레드(발표 임박 10분 폴링 → 서프라이즈% 라우팅).
    earn_watcher_stop = None
    earn_watcher = _build_earnings_watcher(cfg, store, brain,
                                           universe_fn=provider.markets)
    if earn_watcher:
        _et, earn_watcher_stop = start_earnings_watcher_thread(earn_watcher)
        log.info("실적 결과 워처 기동 — 임박 %.0fs / 평시 %.0fs",
                 earn_watcher.poll_sec, earn_watcher.idle_poll_sec)
    # 빠른 슬라이스 갱신기: 별도 데몬 스레드로 장중 5분마다 regime/sentiment/markets 갱신
    # (Yahoo 만, 토스 무접촉). regime_flip 트리거가 장중에 먹게 파일을 신선하게 유지.
    slice_stop = None
    if wcfg.get("market_state_refresh", True):
        refresh_sec = float(wcfg.get("market_state_refresh_sec", 300))
        # regime 은 감시 루프가 매 틱 계산해 둔 실시간 유니버스 브레드스를 우선 채택한다
        # (Yahoo 지수 조회 생략). 루프 참조는 illiquid_fn 과 같은 지연 홀더로 — 기동 직후
        # (아직 1틱도 안 돌았을 때)는 빈 스냅샷이라 지수 프록시로 자동 폴백.
        def _breadth_fn() -> dict:
            return loop_ref[0].breadth_snapshot() if loop_ref else {}

        refresher = SliceRefresher(lambda: build_fast_slice(cfg, breadth_fn=_breadth_fn),
                                   apply_fast_slice,
                                   markets=markets, refresh_sec=refresh_sec)
        _st, slice_stop = start_slice_refresher_thread(refresher)
        _ma20_path = _SCRIPTS.parent / "data" / "ma20.json"
        log.info("빠른 슬라이스 갱신기 기동 — 장중 %.0fs, markets=%s, 실시간 브레드스=%s",
                 refresh_sec, markets,
                 ("on(ma20.json, 최소 %d종목)"
                  % int(cfg.raw.get("market_state", {}).get("breadth_min_n", 10))
                  if _ma20_path.exists() else "off(ma20.json 없음 → 지수 프록시)"))
    # 롤링 유니버스 갱신기: 별도 데몬 스레드로 코어(개장 전 리프레시)+무버(장중 add-only)를
    # 소유한다. data/universe.yaml 을 갱신하면 핫리로드(UniverseProvider)로 뇌·감시·공시에
    # 자동 전파. 무버 편입 시 brain.wake("movers") 로 다음 사이클에 반영(쿨다운은 BrainWorker).
    uni_stop = None
    rolling_cfg = cfg.raw.get("screener", {}).get("rolling", {})
    if rolling_cfg.get("enabled", True):
        movers_on = bool(rolling_cfg.get("movers_enabled", False))
        weekdays = rolling_cfg.get("core_weekdays")
        def _on_added(mkt, syms):
            if brain:
                brain.wake("movers", [])
        uni_refresher = UniverseRefresher(
            core_fn=lambda m: core_refresh(cfg, m, store=store),
            mover_fn=(lambda m: mover_scan(cfg, m)) if movers_on else (lambda m: []),
            markets=markets,
            core_preopen_min=rolling_cfg.get("core_preopen_min"),
            interval_min=float(rolling_cfg.get("interval_min", 60)),
            on_added=_on_added if movers_on else None,
            core_weekdays=weekdays)
        _ut, uni_stop = start_universe_refresher_thread(uni_refresher)
        log.info("롤링 유니버스 갱신기 기동 — preopen=%s, 코어요일=%s, 무버=%s, markets=%s",
                 uni_refresher.core_preopen_min, weekdays,
                 "on" if movers_on else "off", markets)
    # 시장 세션표(프리/정규/애프터) 하루 1회 갱신 — 대시보드 current_session 이 이 캐시로
    # 세션 배지를 그린다. gateway.client(단일 토큰)로만 호출. 실패해도 데몬은 계속(is_open 폴백).
    session_stop = None
    if getattr(gateway, "client", None) is not None:
        try:
            gateway.refresh_market_sessions(tuple(markets))
        except Exception as e:
            log.warning("세션표 초기 갱신 실패(무시, is_open 폴백): %s", e)
        session_stop = _start_session_refresher(gateway, tuple(markets), store=store)
        log.info("세션표 갱신기 기동 — 하루 1회(주기 폴링, 오늘 받았으면 스킵), markets=%s", markets)
    # 휴장일 캘린더 커버리지 점검(기동 1회) — 연 1회 손갱신을 놓치면 휴장에도 매매를 시도한다.
    _check_calendar_coverage(markets, store)
    # 실계좌 자산 스냅샷 갱신기 — gateway.client(단일 토큰)로 5분마다 매수여력+보유를 떠
    # data/account_snapshot.json 에 캐시한다. 대시보드가 이 캐시만 읽어 자산 관제 패널을
    # 그린다(대시보드는 TossClient 직접호출 금지). 조회·표시 전용(주문 로직 무영향).
    account_stop = _start_account_refresher(gateway, cfg)
    if account_stop:
        _snap_sec = float(wcfg.get("account_snapshot_sec", 300))
        log.info("자산 스냅샷=on(%.0f분) — 실계좌 매수여력+보유 캐시", _snap_sec / 60)
    # 라이브 원장 주기 재대사 — 실계좌를 봇 원장에 병합해 드리프트 자가치유(라이브만).
    reconcile_stop = _start_reconcile_timer(broker, gateway, store, cfg, markets)
    if reconcile_stop:
        _rec_sec = float(broker_cfg.get("reconcile_sec", 300))
        log.info("원장 재대사=on(%.0f분) — 실계좌 병합으로 드리프트 차단", _rec_sec / 60)
    _keep_awake(True)
    try:
        loop.run_forever(max_ticks=args.ticks)
    finally:
        _keep_awake(False)
        if watcher_stop:
            watcher_stop.set()
        if earn_watcher_stop:
            earn_watcher_stop.set()
        if slice_stop:
            slice_stop.set()
        if uni_stop:
            uni_stop.set()
        if session_stop:
            session_stop.set()
        if account_stop:
            account_stop.set()
        if reconcile_stop:
            reconcile_stop.set()
        if value_timer_stop:
            value_timer_stop.set()
        if value_worker:
            value_worker.stop()
        if brain:
            brain.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
