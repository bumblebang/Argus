"""매크로 이벤트 캘린더 — FOMC·금통위 등. data/macro_calendar.json 산출.

실적 캘린더와 대칭: 배치가 파일을 쓰고, focus.build_focus 가 사이클마다 dday 를
재계산해 렌즈를 켠다. 하드게이트 없음.

소스 우선순위:
  1) data/macro_events.yaml (큐레이티드 SSOT — Finnhub 경제캘린더는 무료 403)
  2) Finnhub /calendar/economic (키가 있고 허용되면 CPI 등 보강; 실패는 조용히 스킵)
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import requests
import yaml

from ..logging_setup import get_logger

log = get_logger("src.macro_cal")

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
EVENTS_YAML = DATA_DIR / "macro_events.yaml"
FINNHUB_URL = "https://finnhub.io/api/v1/calendar/economic"

# Finnhub event 이름 → 우리 id (부분일치, 소문자)
_FINNHUB_MAP: list[tuple[str, str, str, str]] = [
    # (needle, id, label, market)
    ("fomc", "fomc", "FOMC", "US"),
    ("federal funds rate", "fomc", "FOMC", "US"),
    ("cpi", "cpi_us", "US CPI", "US"),
    ("consumer price index", "cpi_us", "US CPI", "US"),
    ("nonfarm", "nfp", "NFP", "US"),
    ("non-farm", "nfp", "NFP", "US"),
]


def _dday(d: str, today: date | None = None) -> int | None:
    today = today or date.today()
    try:
        return (datetime.strptime(str(d)[:10], "%Y-%m-%d").date() - today).days
    except (ValueError, TypeError):
        return None


def load_curated(path: Path | None = None) -> list[dict]:
    """YAML events[] → 표준형 리스트."""
    p = path or EVENTS_YAML
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        log.warning("macro_events.yaml 로드 실패: %s", e)
        return []
    out: list[dict] = []
    for ev in raw.get("events") or []:
        if not isinstance(ev, dict) or not ev.get("date") or not ev.get("id"):
            continue
        out.append({
            "id": str(ev["id"]),
            "label": ev.get("label") or str(ev["id"]).upper(),
            "date": str(ev["date"])[:10],
            "market": ev.get("market") or "US",
            "source": "curated",
        })
    return out


def _match_finnhub(name: str) -> tuple[str, str, str] | None:
    n = (name or "").lower()
    for needle, eid, label, market in _FINNHUB_MAP:
        if needle in n:
            return eid, label, market
    return None


def fetch_finnhub(api_key: str, *, days_ahead: int = 45,
                  days_back: int = 5) -> list[dict]:
    """Finnhub 경제캘린더 → allowlist 이벤트. 403/실패 시 []."""
    if not api_key:
        return []
    today = date.today()
    try:
        r = requests.get(FINNHUB_URL, params={
            "from": (today - timedelta(days=days_back)).isoformat(),
            "to": (today + timedelta(days=days_ahead)).isoformat(),
            "token": api_key,
        }, timeout=15)
        if r.status_code == 403:
            log.info("Finnhub economic calendar 403 — 큐레이티드 YAML만 사용")
            return []
        r.raise_for_status()
        rows = (r.json() or {}).get("economicCalendar") or []
    except Exception as e:
        log.warning("Finnhub economic calendar 실패: %s", e)
        return []
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        m = _match_finnhub(row.get("event") or "")
        if not m:
            continue
        eid, label, market = m
        d = (row.get("date") or "")[:10]
        if not d or (eid, d) in seen:
            continue
        seen.add((eid, d))
        out.append({"id": eid, "label": label, "date": d, "market": market,
                    "source": "finnhub"})
    return out


def merge_events(curated: Iterable[dict], extra: Iterable[dict]) -> list[dict]:
    """id+date 키로 합친다. curated 우선(이미 있으면 extra 스킵)."""
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for src in (curated, extra):
        for ev in src:
            key = (ev["id"], ev["date"])
            if key in seen:
                continue
            seen.add(key)
            out.append(dict(ev))
    out.sort(key=lambda e: e.get("date") or "")
    return out


def with_ddays(events: list[dict], today: date | None = None) -> list[dict]:
    today = today or date.today()
    out = []
    for ev in events:
        dday = _dday(ev.get("date"), today)
        row = dict(ev)
        if dday is not None:
            row["dday"] = dday
        out.append(row)
    return out


def build_calendar(*, api_key: str | None = None,
                   curated_path: Path | None = None,
                   today: date | None = None) -> dict:
    """→ {asof, events:[...]}."""
    curated = load_curated(curated_path)
    extra = fetch_finnhub(api_key or os.getenv("FINNHUB_API_KEY") or "")
    events = with_ddays(merge_events(curated, extra), today=today)
    return {
        "asof": datetime.now(timezone.utc).isoformat(),
        "events": events,
        "n_curated": len(curated),
        "n_finnhub": len(extra),
    }
