"""토스증권 Open API 클라이언트 (실제 OpenAPI 3.1 스펙 v1.1.5 기준).

Base URL: https://openapi.tossinvest.com
인증: OAuth2 Client Credentials -> POST /oauth2/token (form-urlencoded) -> Bearer JWT.
모든 응답은 {result: ...} 로 감싸짐(ApiResponse) -> _request 가 result 를 벗겨서 반환.
계좌 관련 엔드포인트는 헤더 X-Tossinvest-Account = accountSeq(정수) 필요.
시세 값(가격/수량)은 문자열로 내려옴.
"""
from __future__ import annotations

import json
import random
import threading
import time
from pathlib import Path
from typing import Any

import requests

from .config import TossCredentials
from .logging_setup import get_logger

log = get_logger("toss.client")

# 토큰을 디스크에 캐시 -> 스크립트/실행마다 재발급하지 않고 1개를 재사용한다.
# (재발급 폭주가 BASIC tier rate limit/쿨다운을 유발하므로 반드시 캐시할 것)
_TOKEN_CACHE = Path("data/.token.json")

# 실제 스펙 기준 엔드포인트 (data/openapi.json 에서 확정)
EP = {
    "token":        ("POST",   "/oauth2/token"),
    "accounts":     ("GET",    "/api/v1/accounts"),
    "candles":      ("GET",    "/api/v1/candles"),
    "prices":       ("GET",    "/api/v1/prices"),
    "orderbook":    ("GET",    "/api/v1/orderbook"),
    "holdings":     ("GET",    "/api/v1/holdings"),
    "buying_power": ("GET",    "/api/v1/buying-power"),
    "sellable":     ("GET",    "/api/v1/sellable-quantity"),
    "order_create": ("POST",   "/api/v1/orders"),
    "order_cancel": ("POST",   "/api/v1/orders/{order_id}/cancel"),
    "order_modify": ("POST",   "/api/v1/orders/{order_id}/modify"),
    "order_get":    ("GET",    "/api/v1/orders/{order_id}"),
    "exchange_rate":("GET",    "/api/v1/exchange-rate"),
    "calendar":     ("GET",    "/api/v1/market-calendar/{country}"),
    "stocks":       ("GET",    "/api/v1/stocks"),
    "warnings":     ("GET",    "/api/v1/stocks/{symbol}/warnings"),
    "rankings":     ("GET",    "/api/v1/rankings"),
}

# EP key -> rate limit 그룹 (BASIC tier 는 그룹별 TPS 가 다름). engine.ratelimit 참조.
EP_GROUP = {
    "token":        "AUTH",
    "accounts":     "ACCOUNT",
    "holdings":     "ASSET",
    "buying_power": "ACCOUNT",
    "sellable":     "ACCOUNT",
    "candles":      "MARKET_DATA_CHART",
    "prices":       "MARKET_DATA",
    "orderbook":    "MARKET_DATA",
    "order_create": "ORDER",
    "order_cancel": "ORDER",
    "order_modify": "ORDER",
    "order_get":    "ORDER_HISTORY",
    "exchange_rate":"MARKET_INFO",
    "calendar":     "MARKET_INFO",
    "stocks":       "STOCK",
    "warnings":     "STOCK",
    "rankings":     "RANKING",
}

_CCY = {"KR": "KRW", "US": "USD"}  # 시장 -> 통화


class TossAPIError(RuntimeError):
    def __init__(self, status: int, body: Any):
        super().__init__(f"Toss API error {status}: {body}")
        self.status = status
        self.body = body


class TossClient:
    def __init__(self, creds: TossCredentials, timeout: int = 10, rate_limiter=None):
        self.creds = creds
        self.timeout = timeout
        self.session = requests.Session()
        self._token: str | None = None
        self._token_exp: float = 0.0
        self._token_issued_at: float = 0.0     # 마지막 발급 시각(쓰로틀/경합 판별용)
        self._token_lock = threading.Lock()    # 동시 재발급 직렬화(스레드 간 토큰 경합 방지)
        # 그룹별 토큰버킷(engine.ratelimit.GroupRateLimiter). None 이면 페이싱 없음(기존 동작).
        self.rate_limiter = rate_limiter

    # ── 인증 ──────────────────────────────────────────────
    def _valid(self, tok: str | None, exp: float) -> bool:
        return bool(tok) and time.time() < exp - 120  # 만료 2분 전까지 유효로 간주

    def _load_cached(self) -> bool:
        try:
            d = json.loads(_TOKEN_CACHE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if d.get("client_id") == self.creds.client_id and self._valid(d.get("access_token"), d.get("exp", 0)):
            self._token, self._token_exp = d["access_token"], d["exp"]
            return True
        return False

    def _ensure_token(self) -> str:
        if self._valid(self._token, self._token_exp):
            return self._token
        # 락으로 동시 재발급 직렬화: 락 대기 중 다른 스레드가 발급했으면 그걸 쓴다
        # (client당 토큰 1개 정책 → 스레드들이 앞다퉈 재발급하면 서로 무효화하는 thrash).
        with self._token_lock:
            if self._valid(self._token, self._token_exp):
                return self._token
            if self._load_cached():  # 디스크 캐시 재사용 (재발급 폭주 방지)
                return self._token
            _, path = EP["token"]
            resp = self.session.post(
                self.creds.base_url + path,
                data={"grant_type": "client_credentials",
                      "client_id": self.creds.client_id,
                      "client_secret": self.creds.client_secret},
                timeout=self.timeout,
            )
            if resp.status_code != 200:
                raise TossAPIError(resp.status_code, resp.text)
            data = resp.json()
            self._token = data["access_token"]
            self._token_exp = time.time() + int(data.get("expires_in", 3600))
            self._token_issued_at = time.time()
        try:
            _TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
            _TOKEN_CACHE.write_text(json.dumps(
                {"client_id": self.creds.client_id, "access_token": self._token,
                 "exp": self._token_exp}), encoding="utf-8")
        except OSError as e:
            log.warning("토큰 캐시 저장 실패: %s", e)
        log.info("토큰 신규 발급 (만료 %ss)", data.get("expires_in"))
        return self._token

    def _invalidate_token(self) -> None:
        """stale 토큰 폐기: 메모리·디스크 캐시 비워 다음 호출이 강제 재발급하게 한다.

        토스는 client당 토큰 1개만 유효 → 다른 프로세스가 재발급하면 이 프로세스의
        캐시 토큰이 서버에서 무효화돼 401 invalid-token 이 난다. 이때 캐시까지 지워야
        _load_cached 가 stale 토큰을 다시 읽지 않는다.
        """
        self._token = None
        self._token_exp = 0.0
        try:
            _TOKEN_CACHE.unlink(missing_ok=True)
        except OSError as e:
            log.warning("토큰 캐시 삭제 실패: %s", e)

    def _headers(self, account_seq: int | str | None = None) -> dict[str, str]:
        h = {"Authorization": f"Bearer {self._ensure_token()}", "Accept": "application/json"}
        if account_seq is not None:
            h["X-Tossinvest-Account"] = str(account_seq)
        return h

    # ── 공용 요청 (ApiResponse.result 언랩) ───────────────
    def _request(self, key: str, *, params: dict | None = None, json: dict | None = None,
                 account_seq: int | str | None = None, path_params: dict | None = None) -> Any:
        method, path = EP[key]
        if self.rate_limiter is not None:
            self.rate_limiter.acquire(EP_GROUP.get(key, ""))
        if path_params:
            path = path.format(**path_params)
        url = self.creds.base_url + path
        token_retried = False
        for attempt in range(4):
            resp = self.session.request(method, url, params=params, json=json,
                                        headers=self._headers(account_seq), timeout=self.timeout)
            if resp.status_code == 401 and "invalid-token" in resp.text:
                # 401 invalid-token 은 (a)stale 토큰 또는 (b)BASIC tier 쓰로틀 신호다.
                # 방금 발급한 토큰이 401 이면 쓰로틀/경합이므로 '재발급하지 말고' 백오프 후 같은
                # 토큰으로 재시도한다(잦은 재발급 자체가 쓰로틀 대상 → thrash 원인). 오래된 토큰만 1회 재발급.
                age = time.time() - self._token_issued_at
                if age > 60 and not token_retried:
                    log.warning("401 invalid-token (토큰 %.0fs 경과, stale 추정) -> 재발급 1회 후 재시도", age)
                    self._invalidate_token()
                    token_retried = True
                    continue
                wait = min(2 ** attempt, 8) + random.uniform(0, 0.5)
                log.warning("401 invalid-token (쓰로틀 추정, 토큰 %.0fs 전 발급) -> 재발급 없이 %.1fs 백오프", age, wait)
                time.sleep(wait)
                continue
            if resp.status_code == 429:
                # 문서 권장: Retry-After 존중 + 지수 백오프 + 지터
                retry_after = resp.headers.get("Retry-After")
                base = float(retry_after) if retry_after else 2 ** attempt
                wait = base + random.uniform(0, 0.5)
                log.warning("429 rate limit (group=%s, Retry-After=%s) -> %.1fs 후 재시도",
                            resp.headers.get("X-RateLimit-Group", "?"), retry_after, wait)
                time.sleep(wait)
                continue
            if resp.status_code >= 400:
                raise TossAPIError(resp.status_code, resp.text)
            body = resp.json() if resp.content else {}
            return body.get("result", body) if isinstance(body, dict) else body
        raise TossAPIError(429, "rate limit 재시도 초과")

    # ── 계좌 ──────────────────────────────────────────────
    def get_accounts(self) -> list[dict]:
        """[{accountNo, accountSeq, accountType}]. accountSeq 가 헤더용 정수 식별자."""
        return self._request("accounts") or []

    def get_holdings(self, account_seq: int | str, symbol: str | None = None) -> dict:
        params = {"symbol": symbol} if symbol else None
        return self._request("holdings", params=params, account_seq=account_seq)

    def get_buying_power(self, account_seq: int | str, market: str) -> dict:
        return self._request("buying_power", params={"currency": _CCY.get(market, "KRW")},
                             account_seq=account_seq)

    def get_sellable(self, account_seq: int | str, symbol: str) -> dict:
        return self._request("sellable", params={"symbol": symbol}, account_seq=account_seq)

    # ── 시세 ──────────────────────────────────────────────
    def get_candle_page(self, symbol: str, interval: str = "1d", count: int = 60,
                        adjusted: bool = True, before: str | None = None) -> dict:
        """캔들 1페이지. interval='1m'|'1d', count≤200.

        반환: {candles: [...], nextBefore: ISO8601|None}.
        다음 페이지는 nextBefore 를 before 로 그대로 넘긴다(스펙).
        """
        params: dict[str, Any] = {
            "symbol": symbol, "interval": interval, "count": count, "adjusted": adjusted}
        if before:
            params["before"] = before
        res = self._request("candles", params=params)
        if isinstance(res, dict):
            return {"candles": res.get("candles") or [],
                    "nextBefore": res.get("nextBefore")}
        return {"candles": res or [], "nextBefore": None}

    def get_candles(self, symbol: str, interval: str = "1d", count: int = 60,
                    adjusted: bool = True, before: str | None = None) -> list[dict]:
        """interval 은 '1m' 또는 '1d' 만 지원. 반환: [Candle{timestamp, openPrice, ...}] (값은 문자열)."""
        return self.get_candle_page(symbol, interval=interval, count=count,
                                    adjusted=adjusted, before=before)["candles"]

    def get_prices(self, symbols: list[str] | str) -> list[dict]:
        s = symbols if isinstance(symbols, str) else ",".join(symbols)
        return self._request("prices", params={"symbols": s}) or []

    def get_price(self, symbol: str) -> dict | None:
        rows = self.get_prices(symbol)
        return rows[0] if rows else None

    # ── 주문 ──────────────────────────────────────────────
    def place_order(self, *, account_seq: int | str, symbol: str, side: str, qty,
                    order_type: str = "MARKET", price: float | None = None,
                    time_in_force: str = "DAY", client_order_id: str | None = None) -> dict:
        """side: BUY/SELL, order_type: MARKET/LIMIT. quantity 는 문자열로 전송."""
        body: dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "orderType": order_type,
            "quantity": str(qty),
            "timeInForce": time_in_force,
        }
        if order_type == "LIMIT" and price is not None:
            body["price"] = str(price)
        if client_order_id:
            body["clientOrderId"] = client_order_id
        return self._request("order_create", json=body, account_seq=account_seq)

    def cancel_order(self, account_seq: int | str, order_id: str) -> dict:
        return self._request("order_cancel", path_params={"order_id": order_id},
                             account_seq=account_seq)

    def get_order(self, account_seq: int | str, order_id: str) -> dict:
        """GET /api/v1/orders/{order_id} — 단건 주문 조회(체결 대사용).

        반환 Order{status, price, quantity, execution{filledQuantity,
        averageFilledPrice, filledAmount, commission, tax, ...}, ...}. 값은 문자열.
        status: PENDING/PARTIAL_FILLED/FILLED/CANCELED/REJECTED 등.
        """
        return self._request("order_get", path_params={"order_id": order_id},
                             account_seq=account_seq)

    # ── 시황(Market Info) ─────────────────────────────────
    def get_exchange_rate(self, base: str = "USD", quote: str = "KRW") -> dict:
        return self._request("exchange_rate",
                             params={"baseCurrency": base, "quoteCurrency": quote})

    def get_market_calendar(self, country: str, date: str | None = None) -> dict:
        params = {"date": date} if date else None
        return self._request("calendar", params=params, path_params={"country": country})

    def get_stock_info(self, symbols: list[str] | str) -> list[dict]:
        """GET /api/v1/stocks?symbols=... (배치 최대 200). 반환: [StockInfo{symbol,
        securityType, status, isCommonShare, market, name, ...}] (값은 문자열/enum)."""
        s = symbols if isinstance(symbols, str) else ",".join(symbols)
        res = self._request("stocks", params={"symbols": s})
        if isinstance(res, dict):
            return res.get("stocks", [])
        return res or []

    def get_stock_warnings(self, symbol: str) -> list[dict]:
        return self._request("warnings", path_params={"symbol": symbol}) or []

    def get_rankings(self, *, rank_type: str, market_country: str, duration: str,
                     count: int = 100, exclude_investment_caution: bool = True) -> dict:
        """GET /api/v1/rankings — 시장 거래대금/거래량·급등락·토스 체결 랭킹.

        데이트레 풀은 type=MARKET_TRADING_AMOUNT (주 수가 아닌 대금). 반환:
        {rankedAt, rankings:[{rank,symbol,tradingAmount,tradingVolume,price,...}]}.
        """
        return self._request("rankings", params={
            "type": rank_type,
            "marketCountry": market_country,
            "duration": duration,
            "count": int(count),
            "excludeInvestmentCaution": bool(exclude_investment_caution),
        }) or {}
