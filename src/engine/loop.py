"""Argus-Watch — 상시 감시 루프 (M1 골격).

장중에 보유+후보 종목을 주기 폴링하고(감시층, /prices 배치=싸다) 트리거를
평가해 events 로 남긴다. 트리거가 임박(watch/act)이면 정밀층(캔들)을 선별 폴링하고,
act 가 뜨면 각성 콜백(on_wake)으로 '뇌(LLM)'를 깨울 수 있다(M2 연결 지점).

설계 원칙
  - 단일 프로세스·단일 Gateway(토스 토큰 1개 정책). 빠른손=코드/느린뇌=LLM 분리.
  - 폴링예산 2층: 감시층=/prices 배치(MARKET_DATA 1콜, 전종목), 정밀층=캔들(CHART, 선별 소수).
  - 장시간 인지: 열린 시장만 폴링, 둘 다 닫히면 길게 대기.
  - 테스트 용이: now_fn/sleep_fn 주입, run_once() 로 1틱만 실행 가능.

뇌 각성(on_wake)·실제 매매 연결은 M2. M1 은 "감시→트리거→로깅"까지.
"""
from __future__ import annotations

import json
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

from ..config import ROOT
from ..datasources.breadth import label_of
from ..logging_setup import get_logger
from ..market_hours import current_session, is_tradable, near_session_end, market_day
from .gateway import TossGateway
from .store import Store
from . import triggers as T
from .exit_policy import ExitPolicyConfig, time_stop_trigger, thesis_inval_trigger
from .wake_request import consume_brain_wake

log = get_logger("engine.loop")

# 거래 허용 세션 기본값 — 설정(config trading_sessions)이 없으면 정규장 전용(기존 동작).
_DEFAULT_TRADING_SESSIONS: dict[str, tuple[str, ...]] = {"KR": ("regular",), "US": ("regular",)}
# 정기각성(brain_interval_sec)이 도는 세션 기본값 — 설정 없으면 정규장 전용(기존 동작).
_DEFAULT_BRAIN_SESSIONS: dict[str, tuple[str, ...]] = {"KR": ("regular",), "US": ("regular",)}

_KST = ZoneInfo("Asia/Seoul")   # extra_wakes(지정시각 각성) HH:MM 판정용

# MA20 캐시(data/ma20.json, 장전 배치 부산물) 신선도 한도. 넘으면 캐시를 안 쓴다 —
# 낡은 MA20 으로 국면을 오판하느니 live_slice 의 지수 프록시로 폴백하는 게 낫다.
_MA20_MAX_AGE_SEC = 5 * 86400
_MA20_PATH = ROOT / "data" / "ma20.json"


def _iso_epoch(s: Any) -> float | None:
    """ISO8601 문자열 -> epoch. 파싱 불가면 None. tz 없으면 UTC 로 본다(배치가 UTC 로 쓴다)."""
    if not isinstance(s, str):
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def ma20_reader(path: str | Path | None = None, max_age_sec: float = _MA20_MAX_AGE_SEC,
                now_fn: Callable[[], float] = time.time) -> Callable[[], dict]:
    """data/ma20.json 을 mtime 캐시로 읽는 얇은 리더 반환. {symbol: {ma20, close, n, market}}.

    감시 루프는 1초마다 도는데 매 틱 디스크를 읽으면 낭비 — 파일이 바뀐 때만 다시 파싱한다
    (scripts/watch.py 의 _earnings_calendar_reader 와 같은 패턴).

    빈 dict 를 돌려주는 경우 = 실시간 브레드스 비활성(= live_slice 가 지수 프록시로 폴백):
      - 파일 없음/깨짐(장전 배치가 아직 안 돌았거나 실패)
      - asof 가 max_age_sec 보다 오래됨(배치가 멎어 MA20 이 낡음)
    신선도는 매 호출마다 재판정한다 — 배치가 멎으면 mtime 은 그대로인데 데이터만 늙는다.
    경고는 파일이 바뀔 때까지 1회만(1초 틱 로그 스팸 방지).
    """
    p = Path(path) if path else _MA20_PATH
    cache: dict = {"mtime": None, "symbols": {}, "asof": None, "raw": None, "warned": False}

    def read() -> dict:
        try:
            mtime = p.stat().st_mtime
        except OSError:
            return {}
        if cache["mtime"] != mtime:
            cache["mtime"] = mtime
            cache["warned"] = False
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                cache["symbols"] = data.get("symbols") or {}
                cache["raw"] = data.get("asof")
                cache["asof"] = _iso_epoch(cache["raw"])
            except (OSError, ValueError) as e:
                log.warning("MA20 캐시 로드 실패(실시간 브레드스 비활성): %s", e)
                cache["symbols"], cache["asof"], cache["raw"] = {}, None, None
        if not cache["symbols"]:
            return {}
        asof = cache["asof"]
        # asof 가 없으면(필드 누락·형식 깨짐) 신선도를 증명할 수 없다 -> 안 쓴다(보수적).
        if asof is None or (now_fn() - asof) > max_age_sec:
            if not cache["warned"]:
                cache["warned"] = True
                log.warning("MA20 캐시가 낡음/무효(asof=%s) — 실시간 브레드스 비활성, "
                            "지수 프록시로 폴백.", cache["raw"])
            return {}
        return cache["symbols"]

    return read


@dataclass
class WatchConfig:
    watch_interval_sec: float = 5.0    # 감시층 폴링 주기(장중)
    idle_interval_sec: float = 60.0    # 장 닫힘 시 대기 주기
    precision_per_tick: int = 3        # 틱당 정밀(캔들) 폴링 최대 종목수(CHART 예산 보호)
    strategy_per_tick: int = 3         # 틱당 전략 실행(캔들+청산판단) 최대 보유종목수
    entry_per_tick: int = 3            # 틱당 진입 판단(캔들+진입신호) 최대 armed 종목수
    vol_window: int = 12               # 변동성 트리거용 최근 스냅샷 수(주기×window ≈ 관측창)
    vol_spike_pct: float = 0.03        # 단기 급변 임계
    stop_buffer_pct: float = 0.005     # 손절 임박 버퍼
    brain_interval_sec: float = 0.0    # 주기 각성: 0보다 크면 N초마다 뇌 재평가(0=트리거만)
    session_end_exit_min: float = 0.0  # 장 마감 N분 전 데이트레(day) 강제청산(0=비활성)
    stale_quote_sec: float = 0.0       # 시간외 세션 체결정지 판정 임계(초). 0=비활성(기존 동작)
    # 시장별 거래 허용 세션(market_hours.current_session 기준). 기본=정규장 전용(기존 동작).
    trading_sessions: dict[str, tuple[str, ...]] = field(
        default_factory=lambda: dict(_DEFAULT_TRADING_SESSIONS))
    # 정기각성이 도는 시장별 세션. 기본=정규장 전용(기존 동작). 프리장을 열면 그 세션에서도
    # brain_interval_sec 주기로 뇌가 재평가한다(NXT 프리마켓처럼 실가격이 형성되는 구간용).
    brain_sessions: dict[str, tuple[str, ...]] = field(
        default_factory=lambda: dict(_DEFAULT_BRAIN_SESSIONS))
    # 지정 시각(KST HH:MM) 1회 각성. {market: (HH:MM,...)}. 기본={}=비활성(기존 동작).
    extra_wakes: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # Athena 등 외부 배치가 남기는 각성 요청 파일. 빈 문자열이면 비활성.
    wake_request_path: str = "data/brain_wake_request.json"
    # 트레일링 스톱(목표가 도달 시 전량 청산 대신 이익 태우기). 최상위 trailing 블록에서 읽는다.
    # 기본={}=비활성(기존 동작: 목표가 전량 청산). 정규화형: {enabled, base_pct, regime_mult, horizons}.
    trailing: dict = field(default_factory=dict)
    # 공통 청산 바닥(v1: time_stop). 최상위 exit_policy 블록.
    exit_policy: "ExitPolicyConfig | None" = None

    @staticmethod
    def _parse_exit_policy(raw: dict) -> "ExitPolicyConfig":
        from .exit_policy import parse_exit_policy
        return parse_exit_policy(raw)

    @staticmethod
    def _parse_session_map(block: Any,
                           default: dict[str, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
        """{market: [값,...]} 블록 파싱 공통 로직(trading_sessions/brain_sessions/extra_wakes 공유).

        block 이 dict 가 아니면 default 그대로. 문자열 1개도 허용(리스트로 승격), 리스트가
        아니면 빈 튜플(그 시장은 비활성 — 오타로 뭔가 켜지는 쪽보다 안전), 시장키는 대문자.
        """
        out = dict(default)
        if isinstance(block, dict):
            for m, names in block.items():
                if isinstance(names, str):
                    names = [names]
                if not isinstance(names, (list, tuple)):
                    names = []
                out[str(m).upper()] = tuple(str(s).strip() for s in names if str(s).strip())
        return out

    @staticmethod
    def _parse_sessions(raw: dict) -> dict[str, tuple[str, ...]]:
        """config 최상위 trading_sessions 블록 → {market: (세션명,...)}.

        블록이 없거나 그 시장이 없으면 정규장 전용(기존 동작).
        """
        block = (raw or {}).get("trading_sessions")
        return WatchConfig._parse_session_map(block, _DEFAULT_TRADING_SESSIONS)

    @staticmethod
    def _parse_trailing(raw: dict) -> dict:
        """config 최상위 trailing 블록 → 정규화 dict(또는 비활성 {}).

        블록이 없거나 enabled 가 아니면 빈 dict 를 돌려 트레일링 전체 비활성 = 기존 동작
        (목표가 전량 청산). 하위호환 최우선 — 오타로 뭔가 켜지는 것보다 꺼지는 쪽이 안전하다.
        """
        block = (raw or {}).get("trailing") or {}
        if not isinstance(block, dict) or not block.get("enabled"):
            return {}
        horizons = block.get("horizons", ["swing", "position"])
        if isinstance(horizons, str):
            horizons = [horizons]
        if not isinstance(horizons, (list, tuple)):
            horizons = ["swing", "position"]
        regime_mult: dict[str, float] = {}
        rm = block.get("regime_mult")
        if isinstance(rm, dict):
            for k, v in rm.items():
                try:
                    regime_mult[str(k)] = float(v)
                except (TypeError, ValueError):
                    pass
        return {
            "enabled": True,
            "base_pct": float(block.get("base_pct", 0.05)),
            "regime_mult": regime_mult,
            "horizons": tuple(str(h) for h in horizons),
        }

    @classmethod
    def from_config(cls, raw: dict) -> "WatchConfig":
        w = (raw or {}).get("watch", {})
        d = cls()
        return cls(
            watch_interval_sec=float(w.get("watch_interval_sec", d.watch_interval_sec)),
            idle_interval_sec=float(w.get("idle_interval_sec", d.idle_interval_sec)),
            precision_per_tick=int(w.get("precision_per_tick", d.precision_per_tick)),
            strategy_per_tick=int(w.get("strategy_per_tick", d.strategy_per_tick)),
            entry_per_tick=int(w.get("entry_per_tick", d.entry_per_tick)),
            vol_window=int(w.get("vol_window", d.vol_window)),
            vol_spike_pct=float(w.get("vol_spike_pct", d.vol_spike_pct)),
            stop_buffer_pct=float(w.get("stop_buffer_pct", d.stop_buffer_pct)),
            brain_interval_sec=float(w.get("brain_interval_sec", d.brain_interval_sec)),
            session_end_exit_min=float(w.get("session_end_exit_min", d.session_end_exit_min)),
            stale_quote_sec=float(w.get("stale_quote_sec", d.stale_quote_sec)),
            # 거래 세션은 watch 블록이 아니라 최상위 trading_sessions 블록에서 읽는다.
            trading_sessions=cls._parse_sessions(raw),
            # 정기각성 세션·지정시각 각성은 watch 블록 안에서 읽는다(trading_sessions 와 달리
            # 최상위가 아니라 watch 하위 — brain_interval_sec 등 다른 뇌 설정과 같이 둔다).
            brain_sessions=cls._parse_session_map(w.get("brain_sessions"), _DEFAULT_BRAIN_SESSIONS),
            extra_wakes=cls._parse_session_map(w.get("extra_wakes"), {}),
            wake_request_path=str(w.get("wake_request_path", d.wake_request_path) or ""),
            # 트레일링은 watch 블록이 아니라 최상위 trailing 블록에서 읽는다(trading_sessions 와 동일).
            trailing=cls._parse_trailing(raw),
            exit_policy=cls._parse_exit_policy(raw),
        )


# watchlist_fn 반환형: {market: {"positions": [pos dict...], "candidates": [symbol...]}}
WatchlistFn = Callable[[], dict]


def _blank_market() -> dict:
    return {"positions": [], "candidates": [], "armed": []}


def _read_regime(market_state_path: str | Path | None) -> dict:
    """market_state.json 에서 시장별 현재 국면 라벨만 가볍게 추출. {market: label}."""
    if not market_state_path:
        return {}
    try:
        p = Path(market_state_path)
        if not p.exists():
            return {}
        ms = json.loads(p.read_text(encoding="utf-8"))
        return {m: (v or {}).get("label") for m, v in (ms.get("regime") or {}).items()}
    except (OSError, ValueError):
        return {}


def _read_market_state(path: str | Path | None = None) -> dict:
    """market_state.json 전체(수급 streak 등). 틱당 1회 읽기."""
    p = Path(path) if path else ROOT / "data" / "market_state.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except (OSError, ValueError):
        return {}


def build_watchlist(cfg, store: Store,
                    market_state_path: str | Path | None = "data/market_state.json",
                    universe: dict | None = None) -> dict:
    """기본 watchlist: 보유 포지션 + 진입대기(armed) + 후보(유니버스)를 시장별로 묶는다.

    보유(open)는 손절/익절·전략청산 대상, armed(진입대기)는 진입신호 대상, 후보는 단순 감시.
    셋 다 감시층 /prices 배치에 들어가 가격이 추적된다(스냅샷). 현재 국면(regime)은 "_regime"
    키로 함께 실어 보유분 regime_flip(국면 반전 → thesis 재평가) 트리거에 쓴다.

    universe 를 주면(UniverseProvider.markets() 런타임 재독분) cfg.universe 대신 그걸 쓴다 —
    유니버스에서 빠진 심볼이라도 store 의 보유/armed 는 아래에서 그대로 포함(감시 지속).
    """
    universe = universe if universe is not None else (cfg.universe or {})
    by_market: dict[str, dict] = {}
    for market, lst in universe.items():
        cands = [it.get("symbol") for it in (lst or []) if it.get("symbol")]
        by_market[market] = {**_blank_market(), "candidates": cands}
    for row in store.get_open_positions():
        pos = dict(row)
        market = pos.get("market") or "KR"
        by_market.setdefault(market, _blank_market())["positions"].append(pos)
    for row in store.get_armed():
        a = dict(row)
        market = a.get("market") or "KR"
        by_market.setdefault(market, _blank_market())["armed"].append(a)
    by_market["_regime"] = _read_regime(market_state_path)   # 시장 아님(예약 키)
    return by_market


def _meta_field(pos: dict, key: str) -> Any:
    """포지션/armed 행 meta(JSON 문자열 or dict)에서 key 추출. 없으면 None."""
    meta = pos.get("meta")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except (ValueError, TypeError):
            return None
    return meta.get(key) if isinstance(meta, dict) else None


def _meta_dict(pos: dict) -> dict:
    """포지션 meta(JSON 문자열 or dict) → dict 복사본. 없거나 깨졌으면 빈 dict.

    _meta_field 는 단일 키만 읽지만, 트레일 상태 갱신은 meta 전체를 수정해 되써야 하므로
    복사본을 돌려준다(원본 문자열/dict 을 직접 건드리지 않게).
    """
    meta = pos.get("meta")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except (ValueError, TypeError):
            return {}
    return dict(meta) if isinstance(meta, dict) else {}


def _entry_regime_of(pos: dict) -> str | None:
    """보유 포지션 meta 의 진입 시점 국면(entry_regime). 없으면 None(트리거 비활성)."""
    return _meta_field(pos, "entry_regime")


def _horizon_of(pos: dict) -> str | None:
    """포지션/armed 행의 보유기간(meta.horizon). day 면 종가 강제청산 대상."""
    return _meta_field(pos, "horizon")


def _rotated(lst: list, offset: int) -> list:
    """per_tick 상한 슬라이스의 기아(starvation) 방지 — 틱마다 시작점을 돌린다."""
    if len(lst) <= 1:
        return lst
    off = offset % len(lst)
    return lst[off:] + lst[:off]


@dataclass
class TickResult:
    polled: int = 0                    # 가격 폴링된 종목 수
    triggers: list = field(default_factory=list)
    precision: list = field(default_factory=list)  # 정밀 폴링한 심볼
    exits: list = field(default_factory=list)       # 코드 청산한 심볼
    entries: list = field(default_factory=list)     # 코드 진입한 심볼(armed→open)
    woke: bool = False
    markets_open: list = field(default_factory=list)


# 코드(빠른손)가 즉시 처리하는 청산 트리거 — 뇌를 거치지 않는다.
_EXIT_KINDS = {"stop_hit", "target_hit", "time_stop"}


class WatchLoop:
    def __init__(self, gateway: TossGateway, store: Store, watchlist_fn: WatchlistFn, *,
                 markets: Iterable[str] = ("KR", "US"),
                 config: WatchConfig | None = None,
                 on_wake: Callable[[str, list], None] | None = None,
                 executor: Callable[[str, str, float, object], bool] | None = None,
                 strategy_runner=None,
                 entry_executor=None,
                 on_prices: Callable[[dict], None] | None = None,
                 heartbeat_path: str | Path | None = None,
                 ma20_fn: Callable[[], dict] | None = None,
                 now_fn: Callable[[], float] = time.time,
                 sleep_fn: Callable[[float], None] = time.sleep) -> None:
        self.gw = gateway
        self.store = store
        self.watchlist_fn = watchlist_fn
        self.markets = tuple(markets)
        self.cfg = config or WatchConfig()
        self.on_wake = on_wake
        self.executor = executor          # 손절/익절 코드 청산기(symbol,market,price,trigger)->bool
        self.strategy_runner = strategy_runner   # 보유분 전략기반 청산 실행기(.evaluate(pos,market))
        self.entry_executor = entry_executor     # armed 종목 전략기반 진입 실행기(.evaluate(armed,market))
        self.on_prices = on_prices               # 이번 틱 실시간가 콜백(계좌 마크 갱신 등)
        self.heartbeat_path = Path(heartbeat_path) if heartbeat_path else None
        self._now = now_fn
        self._sleep = sleep_fn
        self._ticks = 0
        # 변동성 트리거용 심볼별 가격 링버퍼(인메모리; 재시작 시 수 틱 만에 재축적).
        self._hist: dict[str, deque] = defaultdict(lambda: deque(maxlen=self.cfg.vol_window))
        # regime_flip 중복 각성 방지: 심볼→마지막으로 각성시킨 국면(인메모리, 재시작 시 1회 재평가).
        self._regime_acked: dict[str, str] = {}
        self._last_brain_wake = 0.0    # 주기 각성 타이머(개장 첫 틱에 바로 1회 발화)
        self._rr = 0                   # per_tick 상한 라운드로빈 오프셋(기아 방지)
        self._last_session: dict[str, str] = {}     # 세션 경계 리셋용: 시장→직전 틱 세션명
        self._extra_fired: dict[tuple[str, str], str] = {}  # 지정시각 각성 dedup: (market,HH:MM)->발화한 거래일
        # 유동성 illiquid 집합(시간외 체결정지 종목). 이 루프 스레드가 매 틱 갱신하고
        # 뇌 워커(별도 스레드)가 illiquid_snapshot() 으로 읽는다 → 순회 중 크기 변경
        # (RuntimeError) 을 막으려면 양쪽 다 락을 거쳐야 한다.
        self._illiquid_lock = threading.Lock()
        self.illiquid_symbols: set[str] = set()
        # 실시간 유니버스 브레드스: 이 루프가 매 틱 폴링하는 전 종목 실시간가 + 장전 배치가
        # 만든 MA20 캐시로 계산한다(추가 조회 0). 빠른 슬라이스 갱신기(별도 스레드)가
        # breadth_snapshot() 으로 읽어 regime 으로 채택 → 지수 2개 프록시를 대체.
        self._ma20_fn = ma20_fn or ma20_reader(now_fn=now_fn)
        self._breadth_lock = threading.Lock()
        self._breadth: dict[str, dict] = {}

    def breadth_snapshot(self) -> dict:
        """열린 시장의 실시간 브레드스 스냅샷(스레드 안전).

        {market: {"breadth_above_ma20":.., "n":.., "label":.., "source":"universe_live",
                  "ts":epoch}}
        감시 루프 스레드가 매 틱 갱신하고 슬라이스 갱신기(다른 스레드)가 읽으므로
        illiquid_snapshot 과 같은 락 패턴으로 복사본을 뜬다. 닫힌 시장·데이터 부족(n=0)
        시장은 아예 빠진다 — 낡은 값을 남기면 소비자의 지수 프록시 폴백이 안 걸린다.
        """
        with self._breadth_lock:
            return {m: dict(v) for m, v in self._breadth.items()}

    def _set_breadth(self, snap: dict) -> None:
        """이번 틱 브레드스로 통째 교체(락 1회). 이번 틱에 없는 시장은 자동으로 사라진다."""
        with self._breadth_lock:
            self._breadth = snap

    @staticmethod
    def _market_breadth(symbols: Iterable[str], price_of: dict, ma20: dict,
                        ts: float) -> dict | None:
        """이 시장 감시종목의 20일선 상회 비율. 계산 불가(n=0)면 None.

        분모는 ①이번 틱 실시간가가 있고 ②MA20 캐시에 있는 종목뿐 — 폴링 실패·신규 상장 등
        한쪽이라도 없으면 뺀다(없는 걸 0 으로 세면 브레드스가 아래로 왜곡된다).
        순수 인메모리 계산: 디스크·네트워크 접촉 없음(MA20 은 이미 캐시된 dict).
        """
        above = total = 0
        for sym in symbols:
            price = price_of.get(sym)
            row = ma20.get(sym)
            if price is None or price <= 0 or not isinstance(row, dict):
                continue
            m = row.get("ma20")
            if not isinstance(m, (int, float)) or m <= 0:
                continue
            total += 1
            if price > m:
                above += 1
        if not total:
            return None
        pct = above / total
        return {"breadth_above_ma20": round(pct, 3), "n": total,
                "label": label_of(pct), "source": "universe_live", "ts": ts}

    def illiquid_snapshot(self) -> set[str]:
        """시간외 체결정지 종목 스냅샷(스레드 안전).

        감시 루프 스레드가 매 틱 갱신하는 집합을 뇌 워커(다른 스레드)가 읽으므로
        락 안에서 복사본을 뜬다. 뇌는 이 집합의 종목을 신규진입 후보에서 제외한다
        (청산 경로는 이 필터를 타지 않는다 — 위험 축소를 막으면 안 된다).
        """
        with self._illiquid_lock:
            return set(self.illiquid_symbols)

    # ── 하트비트: stall 감지용. 매 틱 epoch 기록 → 외부 watchdog 이 신선도 검사. ──
    def _beat(self, res: "TickResult", *, tick_error: bool = False) -> None:
        if not self.heartbeat_path:
            return
        try:
            self.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
            # 장중인데 폴링 0 또는 틱 예외면 ok=False — age 만 보면 가짜 초록이 된다.
            markets = list(res.markets_open or [])
            polled = int(res.polled or 0)
            ok = (not tick_error) and (not markets or polled > 0)
            self.heartbeat_path.write_text(json.dumps({
                "ts": self._now(), "ticks": self._ticks,
                "markets_open": markets, "polled": polled,
                "ok": ok, "tick_error": bool(tick_error),
            }), encoding="utf-8")
        except OSError as e:
            log.warning("하트비트 기록 실패: %s", e)

    # ── 종가 강제청산: day 보유 전량 청산 + day 진입대기 해제 ──────────
    def _session_end_flatten(self, market: str, positions: dict, armed: dict,
                             price_of: dict, res: "TickResult") -> None:
        """장 마감 임박 시 데이트레(day) 포지션을 코드가 즉시 정리한다(오버나잇 금지).

        처리한 심볼은 positions/armed dict 에서 제거 — 같은 틱의 후속 평가에서 빠진다.
        가격 미확보 종목은 이번 틱은 건너뛰고 다음 틱(1초 뒤)에 재시도된다.
        """
        for sym in list(positions):
            pos = positions[sym]
            if _horizon_of(pos) != "day" or not self.executor:
                continue
            price = price_of.get(sym)
            if not price or price <= 0:
                continue
            t = T.session_end_trigger(sym, market)
            try:
                if self.executor(sym, pos.get("market", market), price, t):
                    # 성공 시에만 trigger 로깅(마감창 매 틱 재시도의 이벤트 스팸 방지 —
                    # 실패는 ExitExecutor 가 error 이벤트로 이미 남긴다).
                    self.store.log_event("trigger", sym, t.as_event())
                    res.exits.append(sym)
                    positions.pop(sym)
            except Exception as e:
                log.error("종가 청산 실패 %s: %s", sym, e)
                self.store.log_event("error", sym, {"where": "session_end", "err": str(e)})
        for sym in list(armed):
            a = armed[sym]
            if _horizon_of(a) != "day":
                continue
            try:
                self.store.close_position(a["id"], reason="disarm:session_end")
                self.store.log_event("disarm", sym, {"reason": "session_end"})
                armed.pop(sym)
                log.info("진입대기 해제(장 마감) %s", sym)
            except Exception as e:
                log.error("진입대기 해제 실패 %s: %s", sym, e)
                self.store.log_event("error", sym, {"where": "disarm", "err": str(e)})

    # ── 트레일링 스톱(목표가 도달 시 전량 청산 대신 이익 태우기) ──────────
    def _is_trail_target(self, pos: dict) -> bool:
        """이 보유 포지션이 트레일링 대상인가.

        적용: trailing.enabled + horizon(meta.horizon)이 대상목록(기본 swing/position) +
        target_price 존재. 제외: 비활성/day/목표가 없음/meta.horizon 없는 동기화 포지션
        (정보 없으면 개입 안 함 — 안전). 이 판정이 곧 position_triggers 의 suppress_target.
        """
        tr = self.cfg.trailing
        if not tr.get("enabled"):
            return False
        hz = _horizon_of(pos)                    # meta.horizon (없으면 None)
        if hz is None or hz not in tr.get("horizons", ()):
            return False
        return bool(pos.get("target_price"))

    def _trail_pct(self, market: str, cur_regime: dict) -> float:
        """실제 트레일 폭 = base_pct × regime_mult[현재국면]. 라벨 없거나 매핑에 없으면 배수 1.0."""
        tr = self.cfg.trailing
        base = tr.get("base_pct", 0.05)
        label = (cur_regime or {}).get(market)
        mult = tr.get("regime_mult", {}).get(label, 1.0)
        return base * mult

    def _persist_trail(self, pos: dict, new_stop: float, meta: dict) -> None:
        """트레일 상태(meta) + 상향된 손절가를 store 에 영속. id 없으면(테스트 등) 스킵.

        재시작 후에도 trail_active/trail_peak 이 살아 있어야 이익을 놓치지 않는다(활성/신고가
        갱신 시에만 호출돼 매 틱 디스크 쓰기를 피한다).
        """
        pid = pos.get("id")
        if self.store is None or not pid:
            return
        self.store.update_position(pid, stop_price=new_stop, meta=meta)

    def _update_trailing(self, pos: dict, price: float | None, market: str,
                         cur_regime: dict) -> None:
        """트레일 대상 포지션의 상태기계 갱신(활성화 / 최고가 래칫). 손절가는 올리기만.

        빠른손 청산 트리거 평가 '전에' 호출된다 — 여기서 stop_price 를 끌어올려 두면 같은 틱의
        position_triggers 가 그 트레일링 스톱으로 stop_hit 을 낸다(별도 청산 경로 불필요).
          1) 미활성 + price>=target → 활성화: trail_active, trail_peak=price, 손절가 상향.
          2) 활성 + price>trail_peak → trail_peak 갱신, 손절가 래칫업(올라갔을 때만 영속).
        손절가는 항상 max(현재 stop, peak×(1-trail_pct)) — 절대 내리지 않는다(래칫 불변식).
        """
        if price is None or price <= 0:
            return
        target = pos.get("target_price")
        if not target:
            return
        meta = _meta_dict(pos)
        pct = self._trail_pct(market, cur_regime)
        cur_stop = pos.get("stop_price") or 0.0
        sym = pos.get("symbol")

        if not meta.get("trail_active"):
            if price < target:
                return                            # 아직 목표가 미도달 — 트레일 미개입
            new_stop = max(cur_stop, price * (1 - pct))
            meta["trail_active"] = True
            meta["trail_peak"] = price
            pos["meta"] = meta
            pos["stop_price"] = new_stop
            self._persist_trail(pos, new_stop, meta)
            self.store.log_event("trail_activated", sym,
                                 {"price": price, "target": target,
                                  "stop": new_stop, "trail_pct": pct})
            log.info("트레일링 활성 %s — 목표가 %s 돌파, 손절가 %.2f 로 상향(트레일 %.1f%%)",
                     sym, target, new_stop, pct * 100)
            return

        peak = meta.get("trail_peak") or 0.0
        if price <= peak:
            return                                # 신고가 아님 — 영속·로그 스팸 방지(손절가 유지)
        new_stop = max(cur_stop, price * (1 - pct))
        meta["trail_peak"] = price
        pos["meta"] = meta
        pos["stop_price"] = new_stop
        self._persist_trail(pos, new_stop, meta)
        self.store.log_event("trail_raised", sym,
                             {"price": price, "peak": price, "stop": new_stop})

    def _mark_brain_wake(self) -> None:
        """정기 각성 시계를 지금으로. Athena 훅·08:00 extra 가 시간 그리드를 맞춘다."""
        self._last_brain_wake = self._now()

    # ── 한 틱: 폴링 → 트리거 평가 → 정밀 → 로깅 ──────────────
    def run_once(self) -> TickResult:
        res = TickResult()
        # 외부 각성 요청(Athena 종료 등) — 시장 개장 여부와 무관하게 idle 틱에서도 소비.
        if self.on_wake and self.cfg.wake_request_path:
            req = consume_brain_wake(self.cfg.wake_request_path)
            if req:
                reason = str(req.get("reason") or "external")
                res.woke = True
                self.store.log_event("wake", None, {
                    "reason": reason, "source": "wake_request",
                    "market": req.get("market"),
                })
                try:
                    self._mark_brain_wake()
                    self.on_wake(reason, [])
                except Exception as e:
                    log.error("외부 각성 콜백 실패: %s", e)
                    self.store.log_event("error", None,
                                         {"where": "on_wake_request", "err": str(e)})

        # 거래 게이트: 정규장뿐 아니라 설정된 세션(프리/애프터)까지 '이번 틱에 행동할 시장'.
        # 설정이 없으면 trading_sessions 기본값이 정규장 전용이라 기존 동작 그대로다.
        now_ts = self._now()
        open_markets = [m for m in self.markets
                        if is_tradable(m, self.cfg.trading_sessions.get(m), now_ts)]
        res.markets_open = open_markets
        if not open_markets:
            self._set_breadth({})      # 전 시장 휴장 — 낡은 브레드스 제거(프록시 폴백)
            return res
        # 세션명은 아래 게이트들(데이트레 진입·주기 각성)이 다시 쓴다 — 틱당 1회만 조회.
        sessions = {m: current_session(m, now_ts) for m in open_markets}

        wl = self.watchlist_fn()
        ms_full = _read_market_state()
        cur_regime = wl.get("_regime", {})          # {market: label} — regime_flip 비교용
        imminent: list[tuple[str, str, list]] = []  # (market, symbol, triggers)
        managed: list[tuple[str, str, dict]] = []   # 보유분(전략청산 대상)
        armed_list: list[tuple[str, str, dict]] = []  # 진입대기(전략진입 대상)
        live_price: dict[str, float] = {}           # 이번 틱 실시간가(전략 decide 패치용)
        # 이번 틱 브레드스는 새 dict 에 모았다가 끝에서 통째 교체 — 이번 틱에 못 구한
        # 시장(휴장·감시종목 없음·MA20 캐시 없음)은 자동으로 스냅샷에서 빠진다.
        breadth_new: dict[str, dict] = {}
        ma20 = self._ma20_fn() or {}                # mtime 캐시(파일 안 바뀌면 디스크 미접촉)

        for market in open_markets:
            mdata = wl.get(market) or {}
            positions = {p["symbol"]: p for p in mdata.get("positions", []) if p.get("symbol")}
            armed = {p["symbol"]: p for p in mdata.get("armed", []) if p.get("symbol")}
            candidates = [s for s in mdata.get("candidates", []) if s]
            symbols = list(dict.fromkeys([*positions.keys(), *armed.keys(), *candidates]))

            # 세션 경계 리셋: 세션이 바뀌면 이 시장 심볼들의 가격이력을 비운다 — 프리장
            # 갭업을 '방금 급변'으로 오인해 헛 vol_spike 각성이 뜨는 것 방지. 첫 틱(직전
            # 세션 기록 없음)에는 리셋하지 않는다.
            cur_sess = sessions.get(market)
            if self._last_session.get(market) not in (None, cur_sess):
                for sym in symbols:
                    self._hist.pop(sym, None)
            self._last_session[market] = cur_sess

            if not symbols:
                continue

            # 감시층: /prices 배치 1콜(스냅샷 자동 기록).
            rows = self.gw.poll_prices(symbols, record=True)
            res.polled += len(rows)
            price_of = {r["symbol"]: r["price"] for r in rows if r.get("symbol")}
            payload_of = {r["symbol"]: r.get("payload") for r in rows if r.get("symbol")}

            # 실시간 브레드스: 방금 폴링한 이 시장 전 종목 가격 + MA20 캐시로 20일선 상회
            # 비율을 계산한다(추가 조회 0 — MA20 은 일봉 기반이라 장중에 안 변한다).
            if ma20:
                b = self._market_breadth(symbols, price_of, ma20, now_ts)
                if b:
                    breadth_new[market] = b

            # 종가 강제청산: 장 마감 임박 시 데이트레(day) 보유는 전량 청산, day 진입대기는
            # 해제(오버나잇 금지). 청산/해제된 심볼은 이후 전략·진입 평가에서 빠진다.
            if self.cfg.session_end_exit_min > 0 and near_session_end(
                    market, minutes=int(self.cfg.session_end_exit_min),
                    now=datetime.fromtimestamp(self._now(), tz=timezone.utc),
                    allowed=self.cfg.trading_sessions.get(market)):
                self._session_end_flatten(market, positions, armed, price_of, res)

            # 유동성 판정 결과를 이 시장 단위로 모았다가 마지막에 락 한 번으로 반영한다
            # (1초틱 × 종목수만큼 락을 잡지 않기 위해).
            stale_syms: set[str] = set()
            fresh_syms: set[str] = set()

            for sym in symbols:
                price = price_of.get(sym)
                if price is not None:
                    self._hist[sym].append(price)
                    live_price[sym] = price

                # 유동성 판정: 시간외 세션에서 체결이 멈춘 종목은 뇌 신규진입 후보에서
                # 제외(illiquid). 정규장이거나 비활성(stale_quote_sec<=0)이면 이 시장
                # 심볼은 매 틱 여기서 자동 해제된다(세션이 바뀌면 즉시 풀림). 파싱 실패/
                # 키 없음은 fail-open(illiquid 아님) — 시세 포맷이 바뀌었다고 전 종목
                # 거래를 막지 않는다.
                if self.cfg.stale_quote_sec > 0 and sessions.get(market) != "regular":
                    payload = payload_of.get(sym)
                    ts_raw = payload.get("timestamp") if isinstance(payload, dict) else None
                    stale = False
                    if ts_raw:
                        try:
                            stale = (now_ts - datetime.fromisoformat(ts_raw).timestamp()
                                     > self.cfg.stale_quote_sec)
                        except (ValueError, TypeError):
                            stale = False
                    (stale_syms if stale else fresh_syms).add(sym)
                else:
                    fresh_syms.add(sym)

                trigs: list[T.Trigger] = []
                if sym in positions:
                    ep = self.cfg.exit_policy
                    if ep and ep.enabled:
                        ts = time_stop_trigger(positions[sym], cfg=ep, now=now_ts)
                        if ts:
                            trigs.append(ts)
                    # thesis 무효화(price/flow/time) — flow_streak 은 market_state.flows
                    from ..thesis_watch import flow_streak_from_market_state
                    fstr = flow_streak_from_market_state(ms_full, sym)
                    ti = thesis_inval_trigger(positions[sym], price=price, now=now_ts,
                                              flow_streak=fstr)
                    if ti:
                        trigs.append(ti)
                    # 트레일링: 목표가 도달 시 전량 청산 대신 손절가를 끌어올려(래칫) 이익을
                    # 태운다. 청산 트리거 평가 '전에' stop_price 를 갱신해야 같은 틱의
                    # position_triggers 가 그 트레일링 스톱으로 stop_hit 을 낸다. 트레일 대상은
                    # target_hit 을 억제(목표가는 청산가가 아니라 활성화 지점).
                    suppress_target = self._is_trail_target(positions[sym])
                    if suppress_target:
                        self._update_trailing(positions[sym], price, market, cur_regime)
                    trigs += T.position_triggers(positions[sym], price,
                                                 stop_buffer_pct=self.cfg.stop_buffer_pct,
                                                 suppress_target=suppress_target)
                    # 국면 반전 → 뇌 각성(thesis 재평가). 연속 반전 동안 1회만(인메모리 dedup).
                    # 반전이 해소되면(같은 국면 복귀/neutral) ack 을 비워 다음 반전이 재발화.
                    rt = T.regime_flip_trigger(sym, _entry_regime_of(positions[sym]),
                                               cur_regime.get(market))
                    if rt:
                        if self._regime_acked.get(sym) != rt.payload["current_regime"]:
                            trigs.append(rt)
                            self._regime_acked[sym] = rt.payload["current_regime"]
                    else:
                        self._regime_acked.pop(sym, None)
                vt = T.volatility_trigger(sym, list(self._hist[sym]),
                                          spike_pct=self.cfg.vol_spike_pct)
                if vt:
                    trigs.append(vt)

                for t in trigs:
                    self.store.log_event("trigger", sym, t.as_event())
                    res.triggers.append(t)

                # 빠른손: 손절/익절은 코드가 즉시 청산(뇌 안 거침). 청산하면 이 심볼은
                # 정밀/각성 대상에서 제외(포지션 사라짐).
                exited = False
                if self.executor and sym in positions:
                    pos_market = positions[sym].get("market", market)
                    for t in trigs:
                        if t.kind in _EXIT_KINDS:
                            try:
                                if self.executor(sym, pos_market, price, t):
                                    res.exits.append(sym)
                                    exited = True
                            except Exception as e:
                                log.error("코드 청산 실패 %s: %s", sym, e)
                                self.store.log_event("error", sym,
                                                     {"where": "exit", "err": str(e)})
                            break
                if exited:
                    continue
                if sym in positions:                 # 보유분 → 전략기반 청산 대상
                    managed.append((market, sym, positions[sym]))
                if sym in armed and sym not in positions:  # 진입대기(미보유만)
                    # 프리·애프터에서도 day/swing/position 진입. 데이트레 오버나잇은
                    # 오늘 마지막 허용 세션 종료 N분 전(_session_end_flatten)이 막는다.
                    armed_list.append((market, sym, armed[sym]))
                if T.URGENCY.get(T.max_urgency(trigs) or "info", 0) >= T.URGENCY["watch"]:
                    imminent.append((market, sym, trigs))

            # 이 시장 유동성 판정 반영(락 1회). 다른 시장 심볼은 건드리지 않는다.
            if stale_syms or fresh_syms:
                with self._illiquid_lock:
                    self.illiquid_symbols |= stale_syms
                    self.illiquid_symbols -= fresh_syms

        self._set_breadth(breadth_new)   # 이번 틱 브레드스 공개(락 1회, 통째 교체)

        # 정밀층: 임박 종목만 캔들 폴링(CHART 예산 보호 위해 틱당 상한).
        # 각성 후보 = 코드청산이 아닌 트리거(기회 신호: vol_spike/관심가/손절근접 등).
        # 손절/익절은 이미 코드가 처리했으니 뇌를 깨우지 않는다.
        wake_triggers: list[T.Trigger] = []
        for market, sym, trigs in imminent[: self.cfg.precision_per_tick]:
            try:
                candles = self.gw.candles(sym, interval="1m", count=20)
            except Exception as e:  # 정밀 폴링 실패는 틱 전체를 죽이지 않는다.
                log.warning("정밀 폴링 실패 %s: %s", sym, e)
                self.store.log_event("error", sym, {"where": "precision", "err": str(e)})
                continue
            res.precision.append(sym)
            last = candles[-1] if candles else None
            self.store.log_event("precision", sym, {"n": len(candles), "last": last})
            wake_triggers += [t for t in trigs if t.kind not in _EXIT_KINDS]

        # 각성: 기회 트리거가 있으면 뇌를 깨운다(비동기·쿨다운은 BrainWorker 가).
        if wake_triggers:
            res.woke = True
            payload = [t.as_event() | {"symbol": t.symbol} for t in wake_triggers]
            self.store.log_event("wake", None, {"triggers": payload})
            if self.on_wake:
                try:
                    self.on_wake("wake_triggers", wake_triggers)
                except Exception as e:
                    log.error("각성 콜백 실패: %s", e)
                    self.store.log_event("error", None, {"where": "on_wake", "err": str(e)})

        # 이번 틱 실시간가 통지(계좌 마크 갱신 → 미실현 손익이 리스크 게이트에 보이게).
        if self.on_prices and live_price:
            try:
                self.on_prices(live_price)
            except Exception as e:
                log.warning("실시간가 콜백 실패(무시): %s", e)

        # 전략 실행(빠른손): 보유분에 배정된 전략을 코드가 돌려 신호기반 청산(데드크로스 등).
        # 캔들 호출이라 틱당 상한으로 CHART 예산 보호. 라운드로빈으로 기아 방지
        # (상한보다 보유가 많아도 모든 종목이 차례로 평가된다).
        self._rr += 1
        if self.strategy_runner:
            for market, sym, pos in _rotated(managed, self._rr)[: self.cfg.strategy_per_tick]:
                try:
                    r = self.strategy_runner.evaluate(pos, market, live_price.get(sym))
                    if r.get("executed"):
                        res.exits.append(sym)
                except Exception as e:
                    log.error("전략 실행 실패 %s: %s", sym, e)
                    self.store.log_event("error", sym, {"where": "strategy", "err": str(e)})

        # 진입 실행(빠른손): armed 종목에 배정전략 BUY 신호 시 코드가 진입(armed→open).
        # 뇌가 "BUY→즉시 시장가"가 아니라 코드가 진입 타이밍을 잡는다. 실시간가 패치로 1초 반응.
        if self.entry_executor:
            for market, sym, apos in _rotated(armed_list, self._rr)[: self.cfg.entry_per_tick]:
                try:
                    r = self.entry_executor.evaluate(apos, market, live_price.get(sym))
                    if r.get("executed"):
                        res.entries.append(sym)
                except Exception as e:
                    log.error("진입 실행 실패 %s: %s", sym, e)
                    self.store.log_event("error", sym, {"where": "entry", "err": str(e)})

        # 주기 각성: 트리거가 없어도 brain_interval 마다 뇌가 유니버스를 재평가한다.
        # 데이트레 실행(진입/청산)은 위 빠른손이 매 틱 하지만, '무엇을 armed 할지'는 뇌가 정한다 →
        # 잔잔한 날에도 뇌가 주기적으로 돌아야 종목·전략이 배정된다(vol_spike 3%에만 의존 X).
        # 정기 각성은 brain_sessions 에 설정된 세션에서만(기본=정규장 전용). 이벤트 각성은
        # 위에서 전 세션 발화한다. 이미 이번 틱에 깼으면(외부 요청·트리거) 건너뛴다.
        brain_open = any(sessions.get(m) in self.cfg.brain_sessions.get(m, ())
                         for m in sessions)
        if (self.on_wake and not res.woke and self.cfg.brain_interval_sec > 0 and brain_open
                and (self._now() - self._last_brain_wake) >= self.cfg.brain_interval_sec):
            self._last_brain_wake = self._now()
            res.woke = True
            self.store.log_event("wake", None, {"reason": "periodic"})
            try:
                self.on_wake("periodic", [])
            except Exception as e:
                log.error("주기 각성 콜백 실패: %s", e)
                self.store.log_event("error", None, {"where": "on_wake_periodic", "err": str(e)})

        # 지정 시각 1회 각성: 세션 주기와 별개로 그 시각(KST)이 지나면 그 거래일 1회만.
        # 같은 틱에서 이미 깼으면(외부 요청·periodic·트리거) 건너뛴다(LLM 이중 호출 방지).
        # 열린 시장에서만 평가 -> 휴장일엔 open_markets 가 비어 이 블록 자체가 안 돈다.
        if self.on_wake and not res.woke:
            now_kst = datetime.fromtimestamp(now_ts, tz=_KST)
            for m in open_markets:
                for hhmm in self.cfg.extra_wakes.get(m, ()):
                    try:
                        target = datetime.strptime(hhmm, "%H:%M").time()
                    except ValueError:
                        log.warning("extra_wakes 시각 형식 오류(무시) %s: %s", m, hhmm)
                        continue
                    if now_kst.time() < target:
                        continue
                    day = market_day(m, now_kst)
                    key = (m, hhmm)
                    if self._extra_fired.get(key) == day:
                        continue
                    self._extra_fired[key] = day
                    res.woke = True
                    self._mark_brain_wake()
                    self.store.log_event("wake", None,
                                         {"reason": "extra", "market": m, "at": hhmm})
                    try:
                        self.on_wake("extra", [])
                    except Exception as e:
                        log.error("지정시각 각성 콜백 실패: %s", e)
                        self.store.log_event("error", None,
                                             {"where": "on_wake_extra", "err": str(e)})
        return res

    # ── 상시 루프 ─────────────────────────────────────────────
    def run_forever(self, stop_event=None, max_ticks: int | None = None) -> int:
        """장중엔 watch_interval, 휴장엔 idle_interval 로 쉬며 무한 폴링.

        stop_event(threading.Event 호환) 가 set 되면 종료. max_ticks 로 횟수 제한(테스트).
        """
        ticks = 0
        self.store.log_event("loop_start", None,
                             {"markets": list(self.markets), "cfg": vars(self.cfg)})
        try:
            while True:
                if stop_event is not None and stop_event.is_set():
                    break
                if max_ticks is not None and ticks >= max_ticks:
                    break
                tick_error = False
                try:
                    res = self.run_once()
                except Exception as e:  # 한 틱 실패가 루프를 죽이지 않게.
                    log.exception("틱 실패: %s", e)
                    self.store.log_event("error", None, {"where": "tick", "err": str(e)})
                    res = TickResult()
                    tick_error = True
                ticks += 1
                self._ticks = ticks
                self._beat(res, tick_error=tick_error)  # ok 포함 — polled=0 가짜 초록 방지
                interval = (self.cfg.watch_interval_sec if res.markets_open
                            else self.cfg.idle_interval_sec)
                self._sleep(interval)
        except KeyboardInterrupt:
            log.info("중단 요청(Ctrl-C) — 루프 종료.")
        finally:
            self.store.log_event("loop_stop", None, {"ticks": ticks})
        return ticks
