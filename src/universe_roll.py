"""롤링 유니버스 — 코어(개장 전 리프레시) + 무버(장중 add-only union)의 순수 로직.

data/universe.yaml(매매 유니버스)의 생명주기를 스케줄 배치가 아니라 데몬이 소유한다.
두 진입점:
  - core_refresh(cfg, market, store): 해당 시장을 개장 전에 통째 재스크린(발굴→지표→필터
    →전략픽). picks 는 시장별 배수(picks_scale) 적용. store 가 있으면 보유·armed·강세
    도시에 보유분은 새 리스트에 없어도 유지(retention). 그 시장 부분만 원자적 교체.
  - mover_scan(cfg, market): 장중 거래대금 랭킹 상위에서 아직 유니버스에 없는 심볼을
    스크리너 하드필터로 거른 뒤, 신규만 지표·전략 배정해 add-only union. 스캔·총 상한.

레이어 태그: 각 item 에 layer("core"|"mover")·added_at(epoch) 를 붙인다. 무버는 도시에가
없으므로 뇌의 no_dossier 게이트(require_dossier)가 자동으로 무버를 데이트레(armed) 전용으로
강제한다 — 별도 게이트 코드 불필요.

scripts/screen.py 의 발굴 헬퍼(_discover_kr/_discover_us)를 여기로 이동해 CLI 와 데몬이
같은 발굴·쓰기 경로를 공유한다(scripts/screen.py 는 이 모듈을 재사용하는 얇은 CLI).

역할 분리: 발굴=Toss TV(KR core)/Naver(legacy)/Yahoo(US), 백테스트 히스토리=Yahoo, 라이브 매매=토스.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd
import yaml

from .config import ROOT, default_config_path
from .logging_setup import get_logger
from .screener import screen, liquidity_core_pick, liquidity_core_pick_light, compute_metrics, passes_filters
from .gem_screen import gem_candidates
from .datasources import discovery
from .datasources.history import fetch_history

log = get_logger("src.universe_roll")

DATA_DIR = ROOT / "data"
OUT = DATA_DIR / "universe.yaml"

# 무버 스캔 시 거래대금 랭킹을 얼마나 넓게 받아 신규 후보 풀로 볼지(상한과 별개).
_MOVER_POOL = 40

# 합성(dry) 모드 — 발굴/히스토리를 네트워크 없이 결정적 합성으로 대체한다. 기본 False(실네트워크).
# scripts/screen.py --dry 가 True 로 설정한다. 데몬은 항상 False(실발굴). 테스트는 발굴·히스토리
# 함수를 직접 monkeypatch 하므로 이 플래그를 건드리지 않아도 된다.
_DRY = False

# KR core 발굴 — TossGateway.get_rankings (watch 기동 시 set_rankings_fn 으로 주입).
_RANKINGS_FN: Callable[[str, str, str, int], dict] | None = None


def set_rankings_fn(fn: Callable[[str, str, str, int], dict] | None) -> None:
    """데몬/CLI: Toss rankings fetch 콜백 주입. (rank_type, market, duration, count)."""
    global _RANKINGS_FN
    _RANKINGS_FN = fn


# ── 발굴(scripts/screen.py 에서 이동, CLI·데몬 공유) ──────────────────────
def _liquidity_core_cfg(cfg) -> dict:
    """screener.liquidity_core — 거래대금 풀 → 하드필터 → core_size."""
    raw = cfg.raw if hasattr(cfg, "raw") else (cfg or {})
    sc = (raw.get("screener") or {}) if isinstance(raw, dict) else {}
    lc = sc.get("liquidity_core") or {}
    dp = (raw.get("day_pool") or {}) if isinstance(raw, dict) else {}
    return {
        "enabled": bool(lc.get("enabled", False)),
        "light": bool(lc.get("light", False)),
        "core_size": int(lc.get("core_size", 100)),
        "discover_pool": int(lc.get("discover_pool", 300)),
        "default_strategy": str(lc.get("default_strategy", "ma_crossover")),
        "rank_by": str(lc.get("rank_by", "avg_turnover")),
        "day_tag_top": int(lc.get("day_tag_top", 0)),
        "day_strategy": str(lc.get("day_strategy", "volatility_breakout")),
        "kr_rank_type": str(lc.get("kr_rank_type") or dp.get("type")
                             or "MARKET_TRADING_AMOUNT"),
        "kr_duration_live": str(lc.get("kr_duration_live") or dp.get("duration_live")
                                 or "realtime"),
        "kr_duration_fallback": str(
            lc.get("kr_duration_fallback") or dp.get("duration_fallback") or "1d"),
    }


def discover_trading_value_pool(cfg, market: str, pool: int, dry: bool
                                ) -> tuple[list[tuple[str, str, str]],
                                           dict[str, float], dict[str, float]]:
    """거래대금 상위 pool 발굴. 반환: (candidates, symbol→trading_value, symbol→price)."""
    criteria = cfg.raw.get("screener", {})
    min_price = (criteria.get("min_price", {}) or {}).get(market, 0)
    mkt = str(market).upper()
    if dry:
        if mkt == "KR":
            rows = [{"symbol": f"00{i:04d}", "name": f"종목{i}",
                     "trading_value": float(1_000_000_000 * (pool - i)),
                     "price": 50_000.0}
                    for i in range(pool)]
        else:
            rows = [{"symbol": f"SYM{i}", "name": f"SYM{i}",
                     "trading_value": float(1_000_000_000 * (pool - i)),
                     "price": 100.0}
                    for i in range(pool)]
    elif mkt == "KR":
        lc = _liquidity_core_cfg(cfg)
        if _RANKINGS_FN is not None:
            rows = discovery.top_kr_by_toss_trading_value(
                count=pool, pool=pool, min_price=min_price,
                fetch_rankings=_RANKINGS_FN,
                rank_type=lc["kr_rank_type"],
                duration_live=lc["kr_duration_live"],
                duration_fallback=lc["kr_duration_fallback"],
            )
        else:
            log.warning("[%s] Toss rankings_fn 미설정 — Naver TV 폴백(legacy)", mkt)
            rows = discovery.top_by_trading_value(
                "KR", count=pool, pool=pool, min_price=min_price)
    elif mkt == "US":
        rows = discovery.top_us_by_trading_value(
            count=pool, pool=pool, min_price=min_price)
    else:
        return [], {}, {}
    cands = [(mkt, d["symbol"], d["name"]) for d in rows]
    tv = {d["symbol"]: float(d.get("trading_value") or 0) for d in rows}
    prices = {d["symbol"]: float(d.get("price") or 0) for d in rows}
    return cands, tv, prices


def discover_kr(cfg, count: int, dry: bool) -> list[tuple[str, str, str]]:
    """KR 후보 발굴(Naver 거래대금 상위). 반환: [(market, symbol, name)]."""
    lc = _liquidity_core_cfg(cfg)
    pool = lc["discover_pool"] if lc["enabled"] else count
    cands, _, _ = discover_trading_value_pool(cfg, "KR", pool, dry)
    return cands


def discover_us(cfg, count: int, dry: bool) -> list[tuple[str, str, str]]:
    """US 후보 발굴(Yahoo most_actives → 거래대금 정렬)."""
    lc = _liquidity_core_cfg(cfg)
    pool = lc["discover_pool"] if lc["enabled"] else count
    cands, _, _ = discover_trading_value_pool(cfg, "US", pool, dry)
    return cands


_DISCOVER = {"KR": discover_kr, "US": discover_us}


def _synthetic_fetch(symbol: str, market: str) -> pd.DataFrame:
    """--dry/테스트용 합성 캔들(결정적). 실네트워크 없이 파이프라인을 돌린다."""
    seed = abs(hash((symbol, market))) % (2**32)
    rng = np.random.default_rng(seed)
    n = 80
    base = 70000 if market == "KR" else 100
    rets = rng.normal(0.0008, rng.uniform(0.01, 0.04), n)
    close = base * np.cumprod(1 + rets)
    return pd.DataFrame({
        "open": np.concatenate([[close[0]], close[:-1]]),
        "high": close * 1.01, "low": close * 0.99, "close": close,
        "volume": rng.integers(1_000_000, 50_000_000, n),
    })


def _fetch_fn(cfg, dry: bool):
    """히스토리 fetch 콜백. dry 면 합성, 아니면 Yahoo(screener criteria 기간 사용)."""
    if dry:
        return _synthetic_fetch
    criteria = cfg.raw.get("screener", {})
    rng = criteria.get("history_range", "6mo")

    max_age = float(criteria.get("history_max_age_hours", 20))

    def fetch(symbol: str, market: str) -> pd.DataFrame:
        return fetch_history(symbol, interval=criteria.get("candle_interval", "1d"),
                             range_=rng, market=market, max_age_hours=max_age)
    return fetch


def _strategy_scores_fetch(cfg, dry: bool):
    """core_refresh 배치용 — 당일 스코어는 stale CSV 재사용하지 않는다."""
    if dry:
        return _synthetic_fetch
    criteria = cfg.raw.get("screener", {})
    rng = criteria.get("history_range", "6mo")
    interval = criteria.get("candle_interval", "1d")

    def fetch(symbol: str, market: str) -> pd.DataFrame:
        return fetch_history(symbol, interval=interval, range_=rng, market=market,
                             max_age_hours=0, refresh=True)
    return fetch


# ── 정적(config) sector 전파 — screen 결과엔 sector 가 없으므로 아는 것만 싣는다 ──
def _static_universe(cfg) -> dict:
    """설정 파일의 정적 universe 블록(동적 유니버스로 대체되기 전 원문).

    load_config 는 동적 universe.yaml 으로 raw['universe'] 를 덮어쓸 수 있어서
    파일을 다시 읽는다. 경로는 ARGUS_CONFIG / pytest example / config.yaml.
    """
    path = default_config_path()
    if not path.exists():
        path = ROOT / "config.example.yaml"
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError:
        return {}
    return raw.get("universe") or {}


def _static_us(cfg) -> list[dict]:
    """US 정적 유니버스(설정 파일 원본). US 발굴/스크린 실패 시 폴백용."""
    return _static_universe(cfg).get("US", [])


_SECTOR_MAP_PATH = ROOT / "config" / "sector_map.yaml"
_UNIVERSE_PATH = ROOT / "data" / "universe.yaml"
_ETF_NAME_MARKERS = ("TIGER", "KODEX", "ETF", "SPDR", "ISHARES", "INVESCO")


def _infer_sector(item: dict) -> str | None:
    name = str(item.get("name") or "").upper()
    if any(m in name for m in _ETF_NAME_MARKERS):
        return "ETF"
    return None


def _sector_lookup(cfg) -> dict[str, str]:
    """symbol→sector: config 정적 + sector_map.yaml + 기존 universe.yaml."""
    out: dict[str, str] = {}
    for lst in _static_universe(cfg).values():
        for it in lst or []:
            sym, sec = it.get("symbol"), it.get("sector")
            if sym and sec:
                out[str(sym)] = str(sec)
    if _SECTOR_MAP_PATH.is_file():
        try:
            extra = yaml.safe_load(_SECTOR_MAP_PATH.read_text(encoding="utf-8")) or {}
            for lst in extra.values():
                if not isinstance(lst, dict):
                    continue
                for sym, sec in lst.items():
                    if sym and sec:
                        out[str(sym)] = str(sec)
        except (OSError, ValueError, yaml.YAMLError) as e:
            log.warning("sector_map.yaml 로드 실패(무시): %s", e)
    if _UNIVERSE_PATH.is_file():
        try:
            live = yaml.safe_load(_UNIVERSE_PATH.read_text(encoding="utf-8")) or {}
            for lst in live.values():
                for it in lst or []:
                    sym, sec = it.get("symbol"), it.get("sector")
                    if sym and sec:
                        out[str(sym)] = str(sec)
        except (OSError, ValueError, yaml.YAMLError) as e:
            log.warning("universe.yaml sector 로드 실패(무시): %s", e)
    return out


def _annotate_sectors(cfg, items: list[dict]) -> None:
    """동적 선정 종목에 sector 전파(in-place)."""
    lookup = _sector_lookup(cfg)
    for it in items:
        sym = str(it.get("symbol") or "")
        sec = it.get("sector") or lookup.get(sym) or _infer_sector(it)
        if sec:
            it["sector"] = sec


# ── yaml 쓰기(코어·무버 공유, 원자적) ─────────────────────────────────────
def _load_yaml(path: Path) -> dict:
    """기존 universe.yaml 로드(없거나 깨졌으면 빈 dict)."""
    try:
        if path.exists():
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError, yaml.YAMLError) as e:
        log.warning("universe.yaml 로드 실패(빈 dict 취급): %s", e)
    return {}


def _atomic_write(mapping: dict, path: Path) -> None:
    """tmp+os.replace 로 원자적 저장. 기존 포맷(allow_unicode, 순서 유지) 그대로."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(mapping, allow_unicode=True, sort_keys=False),
                   encoding="utf-8")
    os.replace(tmp, path)


def _tag(item: dict, layer: str, now: float) -> dict:
    """item 에 layer/added_at 태그를 붙인 새 dict 반환(기존 태그는 보존)."""
    out = dict(item)
    out["layer"] = layer
    out.setdefault("added_at", now)
    return out


def sort_core_by_turnover(market: str = "KR", path: Path | None = None) -> dict | None:
    """스윙 유니버스 멤버십은 유지하고, 전일 거래대금 캐시 순으로만 재정렬.

    Athena 미커버 순회가 yaml 삽입 순서를 따르므로, 도씨에 창 앞에서 한 번 돌린다.
    캐시가 없으면 파일을 건드리지 않는다.
    """
    from .datasources.discovery import _load_ranking_cache
    p = path or OUT
    existing = _load_yaml(p)
    items = list(existing.get(market) or [])
    if not items:
        return None
    ranked = _load_ranking_cache("KR" if market == "KR" else market)
    order = {str(r.get("symbol")): i for i, r in enumerate(ranked) if r.get("symbol")}
    if not order:
        log.info("[%s] 거래대금 캐시 없음 — 스윙 유니버스 순서 유지", market)
        return None
    n = len(order)
    items.sort(key=lambda it: order.get(it.get("symbol"), n))
    existing[market] = items
    _atomic_write(existing, p)
    log.info("[%s] 스윙 유니버스 거래대금 순 재정렬 %d종목", market, len(items))
    return existing


# ── 코어 리프레시 ─────────────────────────────────────────────────────────
def _retained_symbols(store, market: str, old_symbols: set[str] | None = None) -> set[str]:
    """코어 리프레시 시 새 리스트에 없어도 유지할 심볼: 보유 ∪ armed ∪ 신선한 강세 도시에.

    store=None(수동 CLI)이면 빈 집합(보존 생략). 강세 도시에는 **보유 여부와 무관하게** 유지
    — Athena 가 예산 써 만든 유효한 계획(진입존·무효화가)이 랭킹에서 밀렸다고 사라지면
    리서치 자본 낭비이고, 갭 가드의 존 대기 후보가 뇌 후보에서 소실된다. 도시에 TTL(60h)이
    지나면 자연히 보존 대상에서 빠진다(무한 잔류 없음).
    """
    if store is None:
        return set()
    keep: set[str] = set()
    for r in store.get_open_positions():
        if r["market"] == market:
            keep.add(r["symbol"])
    for r in store.get_armed():
        if r["market"] == market:
            keep.add(r["symbol"])
    # 신선한 강세 도시에(비보유 포함): 기존 유니버스 항목 중 살아있는 bullish 계획이 있는 것.
    for sym in (old_symbols or set()) - keep:
        row = store.get_fresh_dossier(sym)
        if not row:
            continue
        # ETF 등 매수 불가 유형의 bullish 도시에는 유니버스에 붙잡지 않음
        # (리서치 예산·뇌 후보 낭비). 보유/armed 는 위에서 이미 keep.
        name = ""
        try:
            from .security_filter import is_buy_ineligible
            bad, _ = is_buy_ineligible(sym, market, name)
            if bad:
                continue
        except Exception:
            pass
        try:
            ev = _json_loads(row["evidence"])
        except Exception:
            ev = {}
        if ev.get("stance") == "bullish":
            keep.add(sym)
    return keep


def _json_loads(s):
    import json
    return json.loads(s) if s else {}


def core_refresh(cfg, market: str, store=None, *, now_fn=time.time) -> dict | None:
    """해당 시장만 풀 스크린해 universe.yaml 의 그 시장 부분을 원자적으로 교체.

    발굴→지표/필터/전략픽(screen). picks 는 config screener.rolling.picks_scale 를 그 시장
    picks count 에 곱해 확대(US 저녁 코어를 ~18종목으로). store 가 있으면 보유·armed·강세
    도시에 보유분을 유지(retention). 그 시장의 기존 무버는 새 리스트에 안 실려 자동 소멸.
    선정 0종목이거나 실패면 기존 파일을 건드리지 않고 None 반환.

    반환: 갱신 후 전체 유니버스 dict(성공) 또는 None(무변경).
    dry(합성) 여부는 모듈 _DRY(기본 False=실네트워크). CLI --dry 만 True 로 설정한다.
    """
    dry = _DRY
    disc = _DISCOVER.get(market)
    if disc is None:
        log.warning("core_refresh: 미지원 시장 %s", market)
        return None
    rolling = (cfg.raw.get("screener", {}) or {}).get("rolling", {}) or {}
    scale = float((rolling.get("picks_scale", {}) or {}).get(market, 1.0))
    screener_raw = cfg.raw.get("screener", {}) or {}
    count = int(screener_raw.get("count", 40))
    lc = _liquidity_core_cfg(cfg)
    fetch = _fetch_fn(cfg, dry)

    if lc["enabled"]:
        pool = lc["discover_pool"]
        cands, discovery_tv, discovery_prices = discover_trading_value_pool(
            cfg, market, pool, dry)
        log.info("[%s] core_refresh: liquidity_core pool=%d → core_size=%d%s",
                 market, pool, lc["core_size"],
                 " (light)" if lc.get("light") else "")
    else:
        cands = disc(cfg, count, dry)
        discovery_tv = {}
        discovery_prices = {}

    if not cands:
        log.warning("[%s] core_refresh: 후보 발굴 0 — 기존 유니버스 유지.", market)
        return None
    from .security_filter import filter_candidates
    cands = filter_candidates(cands)
    if not cands:
        log.warning("[%s] core_refresh: ETF/부적격 제외 후 후보 0 — 기존 유지.", market)
        return None

    criteria = screener_raw if lc["enabled"] else _scaled_criteria(screener_raw, scale)
    if lc["enabled"]:
        if lc.get("light"):
            picks = liquidity_core_pick_light(
                cands, criteria,
                limit=lc["core_size"],
                default_strategy=lc["default_strategy"],
                discovery_tv=discovery_tv,
                discovery_prices=discovery_prices,
                day_tag_top=lc.get("day_tag_top", 0),
                day_strategy=lc.get("day_strategy", "volatility_breakout"),
            )
        else:
            picks = liquidity_core_pick(
                cands, fetch, criteria,
                limit=lc["core_size"],
                default_strategy=lc["default_strategy"],
                rank_by=lc["rank_by"],
                discovery_tv=discovery_tv if lc["rank_by"] == "discovery_tv" else None,
            )
    else:
        picks = screen(cands, fetch, criteria, exclude_fn=None)
    items = list(picks.get(market, []))
    # US 발굴/스크린 실패 → 정적(config) 폴백(수동/데몬 공통).
    if market == "US" and not items:
        items = [{k: v for k, v in {"symbol": it["symbol"],
                                    "name": it.get("name", it["symbol"]),
                                    "strategy": it.get("strategy", "ma_crossover"),
                                    "sector": it.get("sector")}.items() if v is not None}
                 for it in _static_us(cfg)]
    if not items:
        log.error("[%s] core_refresh: 선정 0종목(필터 과도?) — 기존 유니버스 유지.", market)
        return None

    now = now_fn()
    _annotate_sectors(cfg, items)
    new_items = [_tag(it, "core", now) for it in items]

    # 기존 파일에서 그 시장 부분과 타 시장을 분리. 타 시장은 그대로 보존.
    existing = _load_yaml(OUT)
    old_market = existing.get(market, []) or []
    new_syms = {it["symbol"] for it in new_items}

    # gem 버킷(역추세·저평가 발굴 축): 메인 선정 뒤 코어에 append. gem 스크린은 별도
    # 깔때기(박스권·낙폭과대 → edge 백테스트)로 회귀 전략이 쓸 종목을 올린다. 실패 시
    # gem 없이 진행(경고). exclude=메인 선정 ∪ 기존 유니버스 전 시장(현재 유니버스 재발굴 방지).
    gems_cfg = ((cfg.raw.get("screener", {}) or {}).get("gems", {}) or {})
    if gems_cfg.get("enabled", True):
        exclude = set(new_syms)
        for lst in existing.values():
            for it in (lst or []):
                if it.get("symbol"):
                    exclude.add(it["symbol"])
        try:
            gems = gem_candidates(cfg, market, fetch, exclude=exclude, dry=dry)
        except Exception as e:
            log.warning("[%s] core_refresh: gem 스크린 실패(gem 없이 진행): %s", market, e)
            gems = []
        gems = [g for g in gems if g["symbol"] not in new_syms]   # 방어적 중복 제거
        if gems:
            _annotate_sectors(cfg, gems)
            for g in gems:
                new_items.append(_tag(g, "core", now))   # source:"gem" 은 _tag 가 보존
                new_syms.add(g["symbol"])
            log.info("[%s] core_refresh: gem 버킷 %d종목 추가 %s",
                     market, len(gems), [g["symbol"] for g in gems])

    # 보존(retention): 보유·armed·강세도시에 보유분이 새 리스트에 없으면 기존 항목(레이어=core,
    # 기존 태그 보존)으로 유지. store=None 이면 보존 생략.
    keep = _retained_symbols(store, market,
                             {it.get("symbol") for it in old_market if it.get("symbol")})
    retained = 0
    for it in old_market:
        sym = it.get("symbol")
        if sym and sym in keep and sym not in new_syms:
            new_items.append(_tag(it, "core", now))   # added_at 은 기존값 보존(setdefault)
            new_syms.add(sym)
            retained += 1

    out = dict(existing)
    out[market] = new_items
    _atomic_write(out, OUT)
    if lc["enabled"] and lc.get("light"):
        try:
            from .strategy_scores import refresh_strategy_scores
            refresh_strategy_scores(
                {market: out[market]}, _strategy_scores_fetch(cfg, dry),
                dry=dry, prune_universe=out)
        except Exception as e:
            log.warning("[%s] strategy_scores 배치 실패(유니버스는 저장됨): %s", market, e)
    mode = (f"liquidity_core_light/{lc['core_size']}" if lc.get("light")
            else f"liquidity_core/{lc['core_size']}" if lc["enabled"]
            else f"picks/scale={scale:.2f}")
    log.info("[%s] core_refresh: 선정 %d종목(%s, 보존 %d) -> %s",
             market, len(new_items), mode, retained, OUT)
    return out


def _scaled_criteria(criteria: dict, scale: float) -> dict:
    """screener criteria 의 picks count 에 scale 을 곱해(반올림) 확대한 사본 반환.

    scale==1.0 이면 원본을 그대로 쓴다(불필요한 복제 방지). count 는 최소 1 보장.
    """
    if scale == 1.0:
        return criteria
    out = dict(criteria)
    picks = {}
    for strat, cfg in (criteria.get("picks", {}) or {}).items():
        c = dict(cfg)
        c["count"] = max(1, round(int(cfg.get("count", 5)) * scale))
        picks[strat] = c
    out["picks"] = picks
    return out


# ── 무버 스캔(장중 add-only) ──────────────────────────────────────────────
def mover_scan(cfg, market: str, *, now_fn=time.time) -> list[str]:
    """장중 거래대금 랭킹 상위 중 유니버스 미포함 신규 심볼을 하드필터·지표 후 add-only union.

    현재 랭킹 상위 풀에서 (1) 이미 universe.yaml 에 있는 심볼 제외 → (2) 신규만 히스토리
    받아 지표 계산 + 스크리너 하드필터(min_price 등, passes_filters) 통과 → (3) screen 파이프
    라인으로 전략 배정 → (4) 스캔당 최대 movers_per_scan, 시장당 무버 총 상한
    mover_max_per_market 안에서 layer="mover" 태그해 union. 기존 항목은 절대 제거하지 않는다.

    반환: 이번 스캔에서 편입된 심볼 목록(없으면 []).
    """
    dry = _DRY
    disc = _DISCOVER.get(market)
    if disc is None:
        return []
    rolling = (cfg.raw.get("screener", {}) or {}).get("rolling", {}) or {}
    per_scan = int(rolling.get("movers_per_scan", 3))
    max_total = int(rolling.get("mover_max_per_market", 6))
    criteria = cfg.raw.get("screener", {}) or {}

    existing = _load_yaml(OUT)
    market_items = existing.get(market, []) or []
    present = {it.get("symbol") for it in market_items if it.get("symbol")}
    # 이미 편입된 무버 수 — 시장당 총 상한 남은 슬롯 계산.
    mover_count = sum(1 for it in market_items if it.get("layer") == "mover")
    room = max_total - mover_count
    if room <= 0:
        return []
    room = min(room, per_scan)

    # 거래대금 랭킹 상위 풀에서 유니버스 미포함 신규만(ETF 등 매수불가 제외).
    from .security_filter import filter_candidates
    cands = filter_candidates(disc(cfg, _MOVER_POOL, dry))
    new_cands = [(m, s, n) for (m, s, n) in cands if s not in present]
    if not new_cands:
        return []

    fetch = _fetch_fn(cfg, dry)
    # 하드필터 통과분만 남긴다(히스토리 실패는 스킵=보수적). 필터는 screen 내부에서도 걸리지만,
    # 상한(room) 산정 전에 걸러 예산을 절약한다.
    filtered: list[tuple[str, str, str]] = []
    for m, s, n in new_cands:
        try:
            df = fetch(s, m)
        except Exception as e:
            log.warning("[%s] 무버 히스토리 실패(스킵): %s", s, e)
            continue
        metric = compute_metrics(df, s, m, n)
        if metric and passes_filters(metric, criteria):
            filtered.append((m, s, n))
        if len(filtered) >= room:
            break
    if not filtered:
        return []

    # screen 으로 전략 배정(하드필터·랭킹 재사용). 히스토리는 위에서 통과 확인된 것만.
    picks = screen(filtered, fetch, criteria, exclude_fn=None)
    picked = picks.get(market, [])
    if not picked:
        return []
    _annotate_sectors(cfg, picked)

    now = now_fn()
    added: list[str] = []
    tagged = list(market_items)   # add-only: 기존 항목 그대로 두고 뒤에 추가.
    for it in picked:
        sym = it.get("symbol")
        if not sym or sym in present:
            continue
        tagged.append(_tag(it, "mover", now))
        present.add(sym)
        added.append(sym)
        if len(added) >= room:
            break
    if not added:
        return []

    out = dict(existing)
    out[market] = tagged
    _atomic_write(out, OUT)
    log.info("[%s] mover_scan: 신규 편입 %d종목 %s (무버 총 %d/%d)",
             market, len(added), added, mover_count + len(added), max_total)
    return added
