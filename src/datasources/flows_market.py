"""시장 전체 수급 (코스피/코스닥 외국인·기관·개인). market_state.flows_market 에 저장.

종목별 flows(네이버 stock/{code}/trend)와 분리 — 장 방향 시그널.
엔드포인트: m.stock.naver.com/api/index/{KOSPI|KOSDAQ}/trend (당일 1행).
3일 합·p90 은 data/flows_market_history.json 롤링 캐시로 계산(엔드포인트에 이력 없음).
유니버스 합산 폴백은 하지 않는다(왜곡) — 실패 시 빈 dict + warning.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from .base import DataSource, SourceContext
from ..logging_setup import get_logger

log = get_logger("src.flows_market")

_UA = {"User-Agent": "Mozilla/5.0 argus", "Referer": "https://m.stock.naver.com/"}
TREND_URL = "https://m.stock.naver.com/api/index/{code}/trend"
MARKETS = ("KOSPI", "KOSDAQ")
_HISTORY = Path(__file__).resolve().parents[2] / "data" / "flows_market_history.json"
_HISTORY_MAX = 60  # 일수 상한


def _num(s) -> float | None:
    """'+72,410' / '-82,840' → float. 단위는 네이버 지수 수급(백만원 표기 관례)."""
    if s is None:
        return None
    try:
        return float(str(s).replace(",", "").replace("+", "").strip())
    except (ValueError, TypeError, AttributeError):
        return None


def parse_trend_row(row: dict) -> dict | None:
    """네이버 index trend JSON → {date, foreign_net, inst_net, indiv_net}."""
    if not isinstance(row, dict):
        return None
    date = (row.get("bizdate") or "").strip()
    foreign = _num(row.get("foreignValue"))
    inst = _num(row.get("institutionalValue"))
    indiv = _num(row.get("personalValue"))
    if not date or foreign is None:
        return None
    return {"date": date, "foreign_net": foreign, "inst_net": inst, "indiv_net": indiv}


def _load_history(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _save_history(path: Path, hist: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(hist, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def update_history(hist: dict, market: str, row: dict) -> dict:
    """시장별 일자 리스트에 row 를 upsert. 최신 _HISTORY_MAX 일만 유지."""
    days = list(hist.get(market) or [])
    days = [d for d in days if isinstance(d, dict) and d.get("date") != row["date"]]
    days.append({"date": row["date"], "foreign_net": row["foreign_net"],
                 "inst_net": row.get("inst_net"), "indiv_net": row.get("indiv_net")})
    days.sort(key=lambda d: d.get("date") or "")
    hist[market] = days[-_HISTORY_MAX:]
    return hist


def enrich_from_history(row: dict, days: list[dict]) -> dict:
    """당일 row 에 foreign_net_3d / foreign_net_p90 을 붙인다.

    p90: 최근(당일 포함) 최대 20일의 |foreign_net| 90분위.
    표본 < 5 이면 p90 생략(렌즈 오발 방지).
    """
    out = dict(row)
    if not days:
        return out
    # 당일 포함 최근 3일의 foreign_net 합
    last3 = days[-3:]
    vals3 = [d["foreign_net"] for d in last3
             if isinstance(d.get("foreign_net"), (int, float))]
    if vals3:
        out["foreign_net_3d"] = sum(vals3)
    # |foreign_net| p90
    window = days[-20:]
    abs_vals = sorted(abs(float(d["foreign_net"])) for d in window
                      if isinstance(d.get("foreign_net"), (int, float)))
    if len(abs_vals) >= 5:
        # nearest-rank 90%
        idx = min(len(abs_vals) - 1, max(0, int(round(0.9 * (len(abs_vals) - 1)))))
        out["foreign_net_p90"] = abs_vals[idx]
    return out


class FlowsMarketSource(DataSource):
    name = "flows_market"

    def __init__(self, history_path: Path | None = None, spacing_sec: float = 0.15):
        self.history_path = Path(history_path) if history_path else _HISTORY
        self.spacing = spacing_sec

    def fetch(self, ctx: SourceContext) -> dict:
        if ctx.dry:
            return {"flows_market": {
                "KOSPI": {"date": "20260101", "foreign_net": 5000.0, "inst_net": -2000.0,
                          "indiv_net": -3000.0, "foreign_net_3d": 12000.0,
                          "foreign_net_p90": 4000.0},
                "KOSDAQ": {"date": "20260101", "foreign_net": 800.0, "inst_net": 200.0,
                           "indiv_net": -1000.0},
                "source": "naver_index", "asof": "dry",
            }}
        hist = _load_history(self.history_path)
        out: dict = {}
        for i, code in enumerate(MARKETS):
            try:
                r = requests.get(TREND_URL.format(code=code), headers=_UA, timeout=12)
                r.raise_for_status()
                row = parse_trend_row(r.json())
            except Exception as e:
                log.warning("[%s] 시장 수급 실패: %s", code, e)
                row = None
            if not row:
                continue
            hist = update_history(hist, code, row)
            out[code] = enrich_from_history(row, hist.get(code) or [])
            if self.spacing and i < len(MARKETS) - 1:
                time.sleep(self.spacing)
        if out:
            try:
                _save_history(self.history_path, hist)
            except OSError as e:
                log.warning("flows_market history 저장 실패: %s", e)
            out["source"] = "naver_index"
            out["asof"] = datetime.now(timezone.utc).isoformat()
            log.info("flows_market: KOSPI 외국인=%s KOSDAQ 외국인=%s",
                     (out.get("KOSPI") or {}).get("foreign_net"),
                     (out.get("KOSDAQ") or {}).get("foreign_net"))
        else:
            log.warning("flows_market: 전 시장 실패 — 빈 슬롯")
        return {"flows_market": out}
