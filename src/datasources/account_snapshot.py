"""실계좌 스냅샷 조회·캐시 — 매수여력(현금) + 보유(종합·종목별)를 표준 dict 로 정규화.

라이브 관제용: 데몬이 gateway.client(단일 토큰)로 주기 조회해 data/account_snapshot.json
에 캐시하고, 대시보드가 이 파일만 읽어 자산 종합/보유/손익을 그린다(토큰 충돌 방지 —
대시보드는 TossClient 를 직접 만지지 않는다).

토스 응답의 모든 숫자는 문자열이고 krw/usd 로 통화가 분리된다(US 종목은 usd). 여기서
문자열→float 로 파싱하고 통화별로 시장(krw→KR, usd→US) 키를 붙인다. 파싱 실패/누락은
전부 안전하게(None/스킵) 처리해 데몬을 막지 않는다(대시보드는 마지막 캐시 유지).

응답 스키마(실호출 확인):
- get_buying_power(seq, "KR") -> {"currency":"KRW","cashBuyingPower":"732463"}
- get_holdings(seq) -> {"totalPurchaseAmount":{"krw","usd"},
    "marketValue":{"amount":{"krw","usd"},...}, "profitLoss":{"amount":{"krw"},"rate"},
    "dailyProfitLoss":{"amount":{"krw"},"rate"},
    "items":[{"symbol","name","marketCountry","quantity","lastPrice",
              "averagePurchasePrice","marketValue":{"amount",...},
              "profitLoss":{"amount","rate"}, ...}]}
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from ..logging_setup import get_logger

log = get_logger("src.account_snapshot")

CACHE_PATH = Path("data/account_snapshot.json")

# 토스 통화키 -> 표준 시장코드(종합 필드가 krw/usd 로 분리돼 온다).
_CCY_MARKET = [("krw", "KR"), ("usd", "US")]


def _to_float(v, default=None):
    """문자열/숫자 -> float. None/파싱실패면 default(기본 None)."""
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _split_by_ccy(obj) -> dict:
    """{"krw":"267500","usd":null} -> {"KR":267500.0}. null/파싱실패 통화는 제외."""
    out: dict = {}
    if not isinstance(obj, dict):
        return out
    for ccy, market in _CCY_MARKET:
        v = _to_float(obj.get(ccy))
        if v is not None:
            out[market] = v
    return out


def fetch_account_snapshot(client, account_seq, markets=("KR",)) -> dict:
    """실계좌 스냅샷을 표준 dict 로. buying-power(시장별) + holdings(단일 호출) 정규화.

    - cash: 시장별 매수여력(cashBuyingPower). 조회 실패 시장은 스킵+로깅.
    - total_purchase/market_value/profit/daily_profit: holdings 종합(통화→시장 분리).
    - profit_rate/daily_profit_rate: 종합 rate 는 스칼라(계좌 가중) — 해당 손익이 있는
      시장 키에 그대로 매단다(현재 KR 단독이면 {"KR": rate}).
    - items: 종목별(marketCountry→market, 문자열→float). US 종목이 있으면 그대로 담는다.

    모든 파싱은 안전(실패→None/0/스킵). holdings 조회 자체가 실패하면 빈 보유로 진행한다.
    """
    snap: dict = {"ts": time.time(), "cash": {}, "total_purchase": {}, "market_value": {},
                  "profit": {}, "profit_rate": {}, "daily_profit": {},
                  "daily_profit_rate": {}, "items": []}
    # 현금(매수여력) — 시장별 개별 호출
    for m in markets:
        mk = str(m).upper()
        try:
            bp = client.get_buying_power(account_seq, mk) or {}
            snap["cash"][mk] = _to_float(bp.get("cashBuyingPower"), 0.0)
        except Exception as e:
            log.warning("%s 매수여력 조회 실패(스킵): %s", mk, e)
    # 보유(종합 + 종목별) — 단일 호출
    try:
        h = client.get_holdings(account_seq) or {}
    except Exception as e:
        log.warning("보유 조회 실패(빈 보유로 진행): %s", e)
        h = {}
    snap["total_purchase"] = _split_by_ccy(h.get("totalPurchaseAmount"))
    snap["market_value"] = _split_by_ccy((h.get("marketValue") or {}).get("amount"))
    pl = h.get("profitLoss") or {}
    snap["profit"] = _split_by_ccy(pl.get("amount"))
    rate = _to_float(pl.get("rate"))
    if rate is not None:
        for mk in (snap["profit"] or snap["market_value"]):
            snap["profit_rate"][mk] = rate
    dpl = h.get("dailyProfitLoss") or {}
    snap["daily_profit"] = _split_by_ccy(dpl.get("amount"))
    drate = _to_float(dpl.get("rate"))
    if drate is not None:
        for mk in (snap["daily_profit"] or snap["market_value"]):
            snap["daily_profit_rate"][mk] = drate
    # 종목별
    for it in (h.get("items") or []):
        if not isinstance(it, dict):
            continue
        try:
            mk = str(it.get("marketCountry") or "KR").upper()
            mv = it.get("marketValue") or {}
            ipl = it.get("profitLoss") or {}
            snap["items"].append({
                "symbol": it.get("symbol"), "name": it.get("name"), "market": mk,
                "qty": _to_float(it.get("quantity"), 0.0),
                "avg": _to_float(it.get("averagePurchasePrice")),
                "last": _to_float(it.get("lastPrice")),
                "value": _to_float(mv.get("amount")),
                "pnl": _to_float(ipl.get("amount")),
                "pnl_rate": _to_float(ipl.get("rate")),
            })
        except Exception as e:
            log.warning("보유 종목 파싱 실패(스킵) %s: %s", it.get("symbol"), e)
    return snap


def save_snapshot(data: dict) -> None:
    """원자적 쓰기(tmp+os.replace). 실패는 무시(대시보드가 마지막 캐시를 유지)."""
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_PATH.with_name(CACHE_PATH.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, CACHE_PATH)
    except OSError as e:
        log.warning("자산 스냅샷 저장 실패(무시): %s", e)


def load_snapshot() -> dict | None:
    """캐시 로드. 없거나 깨졌으면 None(대시보드는 '스냅샷 대기중' 표시)."""
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
