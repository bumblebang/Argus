"""TossGateway — 모든 토스 호출의 단일 통로.

상주 프로세스에서 토스 API 를 만지는 유일한 객체. 여기 한 곳에서:
  - 그룹별 rate limit(TokenBucket) 적용 (client 에 limiter 주입)
  - /prices 배치 폴링(최대 200종목/콜, 초과 시 청크 분할) + snapshot 기록
  - 토큰관리·401복구·429재시도는 TossClient 가 이미 처리

멀티프로세스 금지(토스 토큰 1개 정책) — 프로세스당 Gateway 1개를 공유한다.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from ..config import AppConfig
from ..logging_setup import get_logger
from ..toss_client import TossClient
from .price_guard import PriceGuard
from .ratelimit import GroupRateLimiter
from .store import Store

log = get_logger("engine.gateway")

_PRICES_BATCH = 200  # /api/v1/prices 1콜 최대 종목수
_KST = timezone(timedelta(hours=9))
# 가격 가드 시드 — 재기동 직후 이보다 오래된 스냅샷은 기준가로 쓰지 않는다.
_GUARD_SEED_MAX_AGE_SEC = 86400.0


def _to_float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_ts(v: Any) -> float | None:
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_KST)
    return dt.timestamp()


def _best_level(levels: Any) -> float | None:
    """호가 레벨 [{price, volume}, ...] 의 첫 유효 가격."""
    if not isinstance(levels, (list, tuple)):
        return None
    for lv in levels:
        px = _to_float(lv.get("price")) if isinstance(lv, dict) else None
        if px and px > 0:
            return px
    return None


class TossGateway:
    def __init__(self, client: TossClient, store: Store | None = None,
                 limiter: GroupRateLimiter | None = None,
                 candle_ttl_sec: float = 0.0,
                 price_guard: PriceGuard | None = None,
                 quote_timeout_sec: float = 2.0) -> None:
        self.client = client
        self.store = store
        self.limiter = limiter
        # lastPrice 이상 틱 필터(None=끔). 감시 루프·뇌·밸류가 모두 poll_prices 를 거치므로
        # 여기 한 곳에서 걸러야 손절·사이징·vol_spike 가 같은 가격을 본다.
        self.price_guard = price_guard
        # 가드 교차확인 호가는 1회·짧은 타임아웃 — 늦은 호가보다 '호가 없음' 판정이 낫다
        # (조회 중엔 게이트웨이 락을 쥐어 전 종목 시세·주문이 멈춘다).
        self.quote_timeout_sec = float(quote_timeout_sec)
        # 캔들 TTL 캐시: 1초틱에서 같은 캔들을 매초 재호출하면 CHART 예산(5TPS) 초과 →
        # ttl 동안은 캐시 반환(라이브 반응성은 호출측이 마지막봉 종가를 실시간가로 패치).
        self.candle_ttl_sec = float(candle_ttl_sec)
        self._candle_cache: dict[tuple, tuple[float, list[dict]]] = {}
        # 모든 토스 호출을 한 줄로 직렬화(감시 루프 스레드 + 뇌 워커 스레드 동시 접근 방지).
        # requests.Session 의 스레드 동시사용 회피 + "단일 게이트웨이" 원칙 보장.
        self._lock = threading.Lock()

    @classmethod
    def from_config(cls, cfg: AppConfig, store: Store | None = None,
                    limits: dict[str, float] | None = None, timeout: int = 10) -> "TossGateway":
        limiter = GroupRateLimiter(limits)
        client = TossClient(cfg.creds, timeout=timeout, rate_limiter=limiter)
        watch = cfg.raw.get("watch", {}) or {}
        ttl = float(watch.get("candle_ttl_sec", 0.0))
        pg_raw = watch.get("price_guard") or {}
        gw = cls(client, store=store, limiter=limiter, candle_ttl_sec=ttl,
                 quote_timeout_sec=float(pg_raw.get("quote_timeout_sec", 2.0)))
        on_event = ((lambda kind, p: store.log_event(kind, p.get("symbol"), p))
                    if store is not None else None)
        gw.price_guard = PriceGuard.from_config(
            pg_raw, gw.best_quote, on_event=on_event)
        return gw

    # ── 감시층: 전종목 현재가 배치 폴링 (싸다) ─────────────
    def poll_prices(self, symbols: Iterable[str], record: bool = True) -> list[dict]:
        """보유+후보 전체 현재가를 /prices 배치로. 반환: [{symbol, price, payload}].

        record=True 면 snapshots 테이블에 한 번에 기록(시계열 축적).
        """
        syms = [s for s in symbols if s]
        out: list[dict] = []
        with self._lock:
            for i in range(0, len(syms), _PRICES_BATCH):
                chunk = syms[i:i + _PRICES_BATCH]
                for r in self.client.get_prices(chunk):
                    out.append({
                        "symbol": r.get("symbol"),
                        "price": _to_float(r.get("lastPrice")),
                        "payload": r,
                    })
        # 가드는 락 밖에서 — 의심 틱이면 호가를 부르는데 orderbook() 도 같은 락을 잡는다.
        # 원시 lastPrice 는 payload 에 그대로 남고 price 만 걸러진 값으로 바뀐다.
        if self.price_guard is not None:
            self._seed_guard([row["symbol"] for row in out])
            for row in out:
                px, verdict = self.price_guard.filter(row["symbol"], row["price"])
                if verdict is not None:
                    row["raw_price"] = row["price"]
                    row["price"] = px
                    row["price_guard"] = verdict
        if record and self.store and out:
            self.store.record_snapshots(out)
        return out

    def _seed_guard(self, symbols: list[str]) -> None:
        """재기동 직후 첫 틱 — 직전 스냅샷(가드 통과가)을 기준가로 심는다.

        기준가가 없으면 첫 틱을 무조건 채택하므로, 오류가가 떠 있는 동안 재기동하면
        가짜 손절이 그대로 재현된다.
        """
        todo = self.price_guard.unseeded(symbols)
        if not todo:
            return
        refs: dict[str, tuple[float, float]] = {}
        if self.store is not None and hasattr(self.store, "latest_snapshot_refs"):
            try:
                refs = self.store.latest_snapshot_refs(
                    todo, max_age_sec=_GUARD_SEED_MAX_AGE_SEC)
            except Exception as e:
                log.warning("가격 가드 시드 조회 실패(첫 틱 채택): %s", e)
        self.price_guard.seed_refs(refs, tried=todo)

    # ── 정밀층: 종목별 캔들/호가 (비싸다, 선별 호출 + TTL 캐시) ───────
    def candles(self, symbol: str, interval: str = "1m", count: int = 200) -> list[dict]:
        key = (symbol, interval, count)
        with self._lock:
            if self.candle_ttl_sec > 0:
                hit = self._candle_cache.get(key)
                if hit and (time.time() - hit[0]) < self.candle_ttl_sec:
                    return hit[1]
            data = self.client.get_candles(symbol, interval=interval, count=count)
            if self.candle_ttl_sec > 0:
                self._candle_cache[key] = (time.time(), data)
            return data

    def get_rankings(self, **kw: Any) -> dict:
        """주식 랭킹(MARKET_TRADING_AMOUNT 등). 단일 게이트웨이 락 경유."""
        with self._lock:
            return self.client.get_rankings(**kw)

    def orderbook(self, symbol: str) -> Any:
        with self._lock:
            return self.client._request("orderbook", params={"symbol": symbol})

    def best_quote(self, symbol: str) -> tuple[float | None, float | None, float | None] | None:
        """최우선 (bid, ask, 호가시각). 조회 실패·빈 호가면 None — PriceGuard 교차확인용.

        재시도 없이 1회·짧은 타임아웃. 클라이언트 기본(4회·백오프)이면 한 종목 호가
        장애가 게이트웨이 락을 수십 초 쥐어 전 종목 손절 평가가 밀린다.
        """
        try:
            with self._lock:
                ob = self.client._request("orderbook", params={"symbol": symbol},
                                          max_attempts=1,
                                          timeout=self.quote_timeout_sec) or {}
        except Exception as e:
            log.warning("호가 조회 실패(가격 가드) %s: %s", symbol, e)
            return None
        if not isinstance(ob, dict):
            return None
        bid, ask = _best_level(ob.get("bids")), _best_level(ob.get("asks"))
        return (bid, ask, _parse_ts(ob.get("timestamp"))) if (bid or ask) else None

    # ── 계좌/주문 위임 ────────────────────────────────────
    def holdings(self, account_seq: int | str, symbol: str | None = None) -> dict:
        with self._lock:
            return self.client.get_holdings(account_seq, symbol)

    def place_order(self, **kw: Any) -> dict:
        with self._lock:
            return self.client.place_order(**kw)

    # 라이브 체결 대사·재동기화용 위임(단일 토큰·락 경유 — 브로커/재대사 타이머가
    # gateway.client 를 직접 만지면 requests.Session 동시사용·토큰 경합이 난다).
    def get_order(self, account_seq: int | str, order_id: str) -> dict:
        with self._lock:
            return self.client.get_order(account_seq, order_id)

    def list_orders(self, account_seq: int | str, **kw: Any) -> dict:
        with self._lock:
            return self.client.list_orders(account_seq, **kw)

    def cancel_order(self, account_seq: int | str, order_id: str) -> dict:
        with self._lock:
            return self.client.cancel_order(account_seq, order_id)

    def get_sellable(self, account_seq: int | str, symbol: str) -> dict:
        with self._lock:
            return self.client.get_sellable(account_seq, symbol)

    def get_holdings(self, account_seq: int | str, symbol: str | None = None) -> dict:
        """holdings() 와 동일(락 경유). 재동기화 로직이 raw TossClient 와 같은
        메서드명(get_holdings)으로 gateway 를 쓸 수 있게 하는 별칭."""
        with self._lock:
            return self.client.get_holdings(account_seq, symbol)

    def get_buying_power(self, account_seq: int | str, market: str) -> dict:
        with self._lock:
            return self.client.get_buying_power(account_seq, market)

    def fetch_account_snapshot(self, account_seq, markets=("KR",), *,
                               fx_usdkrw=None, fx_ts=None) -> dict:
        """account_refresher 전용 — gateway 락 경유."""
        from ..datasources.account_snapshot import fetch_account_snapshot
        with self._lock:
            return fetch_account_snapshot(
                self.client, account_seq, markets=markets,
                fx_usdkrw=fx_usdkrw, fx_ts=fx_ts,
            )

    def refresh_market_sessions(self, markets=("KR", "US")) -> dict:
        from ..datasources.market_calendar import refresh_sessions
        with self._lock:
            return refresh_sessions(self.client, markets)

    def check_tradable(self, sym: str, mkt: str, *, info_cache, warn_cache,
                       fail_closed: bool = False) -> tuple[bool, str]:
        from ..datasources.stock_info import check_tradable
        with self._lock:
            return check_tradable(sym, mkt, client=self.client,
                                  info_cache=info_cache, warn_cache=warn_cache,
                                  fail_closed=fail_closed)

    def get_accounts(self) -> list:
        """doctor 등 일회성 스크립트용 — rate limiter·락 경유."""
        with self._lock:
            return self.client.get_accounts() or []

    def get_prices(self, symbols: list[str]) -> list[dict]:
        """report 등 일회성 스크립트용 — rate limiter·락 경유."""
        syms = [s for s in symbols if s]
        if not syms:
            return []
        with self._lock:
            return self.client.get_prices(syms)
