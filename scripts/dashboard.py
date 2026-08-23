"""Argus 야간 대시보드 — bot.db 를 읽어 예쁜 HTML 로 보여주고 30초마다 자동 갱신.

브라우저로 http://127.0.0.1:8787 접속. 5탭(오늘·리서치·성과·밸류·시스템)으로 구성되며,
meta-refresh 로 30초마다 새로고침되고 매 요청마다 bot.db(읽기전용)·heartbeat·ALERT.json·
paper_account.json·base_rates.json·value_watchlist.json·value_decisions.jsonl 을 실시간으로
읽는다. 자동 갱신 후에도 보던 탭이 유지된다(localStorage).

watch 프로세스 안 데몬 스레드로도(start_background), 단독 프로세스로도(main) 뜬다. 상태 무관
읽기전용(WAL + mode=ro)이라 데몬의 SQLite 쓰기를 방해하지 않는다.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time
from collections import defaultdict, deque
from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.market_hours import is_open, current_session  # noqa: E402
from src.engine.singleton import _pid_alive  # noqa: E402
from src.agents.value_trade import compute_sleeve  # noqa: E402

DB = ROOT / "data" / "bot.db"
HEARTBEAT = ROOT / "data" / "watch.heartbeat"
ALERT = ROOT / "data" / "ALERT.json"
PAPER = ROOT / "data" / "paper_account.json"
WATCHPID = ROOT / "data" / "watch.pid"
MARKET_STATE = ROOT / "data" / "market_state.json"
FEAR_HISTORY = ROOT / "data" / "fear_history.json"
BASE_RATES = ROOT / "data" / "base_rates.json"
SNAPSHOT = ROOT / "data" / "account_snapshot.json"
VALUE_WATCHLIST = ROOT / "data" / "value_watchlist.json"
VALUE_DECISIONS = ROOT / "data" / "value_decisions.jsonl"
CONFIG = ROOT / "config.yaml"

_KST = ZoneInfo("Asia/Seoul")

PORT = 8787
REFRESH_SEC = 30
WINDOW_SEC = 12 * 3600  # "최근 12시간" 집계 창
DISCLOSURE_SEC = 24 * 3600  # 오늘 공시 표시 창
SNAP_STALE_SEC = 900  # 자산 스냅샷이 이보다 오래되면 경고색(기본 갱신 300s의 3배)

# 알파·차트 공통 벤치마크: 시장별 지수 B&H. "같은 돈을 그냥 지수에 넣었다면?"
BENCH = {"KR": ("^KS11", "코스피"), "US": ("SPY", "S&P500")}
_BENCH_TTL = 1800.0          # 지수 히스토리 갱신 주기(초) — 30분마다만 네트워크
_bench_cache: dict = {}      # market -> (fetch_ts, df)
_chart_cache: dict = {}      # "KR" -> (fetch_ts, payload)
_CHART_TTL = 300.0           # 메인 수익률 차트 캐시(초) — 가격·스냅샷이 자주 바뀜

KIND_COLORS = {
    "brain_done": "#3ddc84", "cycle": "#5aa9ff", "entry": "#3ddc84",
    "exit": "#ffb454", "trigger": "#c9a3ff", "wake": "#7fd1ff",
    "error": "#ff5c63", "brain_start": "#9aa4b2", "brain_skip": "#6b7280",
    "loop_start": "#6b7280", "loop_stop": "#6b7280", "arm": "#c9a3ff",
    "disarm": "#9aa4b2", "strategy_exit": "#ffb454",
    "dossier": "#5aa9ff", "disclosure": "#c9a3ff", "athena_done": "#7fd1ff",
}


def _ro_conn() -> sqlite3.Connection | None:
    if not DB.exists():
        return None
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=2.0)
    con.row_factory = sqlite3.Row
    return con


def _hms(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def _read_heartbeat() -> dict | None:
    try:
        return json.loads(HEARTBEAT.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _read_alert() -> dict | None:
    try:
        d = json.loads(ALERT.read_text(encoding="utf-8"))
        return d if d.get("active") else None
    except (OSError, ValueError):
        return None


def _read_pidfile() -> int | None:
    try:
        return int(WATCHPID.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _read_base_rates() -> dict | None:
    try:
        return json.loads(BASE_RATES.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _read_fear_history() -> dict:
    """공포지수 시계열(src/datasources/fear_greed.record_history 가 적재). 스파크라인용.

    없거나 깨졌으면 {} — 패널은 시계열 없이도 그려진다.
    """
    try:
        d = json.loads(FEAR_HISTORY.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _read_snapshot() -> dict | None:
    """실계좌 자산 스냅샷(데몬이 캐시). 없거나 깨졌으면 None('스냅샷 대기중')."""
    try:
        return json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _conv(v) -> float:
    """확신도 등 정렬용 수치 캐스팅 — 값이 없거나 깨졌으면 0."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


# 도셔 표시 정렬: 강세 → 중립 → 약세 → 기타. 같은 stance 안에서는 최신·고확신 우선.
_STANCE_SORT_RANK = {"bullish": 0, "neutral": 1, "bearish": 2}


def _dossier_stance(row: dict) -> str:
    """도셔 행에서 stance 추출 — 공개판(이미 펼침) / 대시보드(evidence JSON) 둘 다."""
    st = row.get("stance")
    if st:
        return str(st).lower()
    ev = row.get("evidence")
    if isinstance(ev, str):
        try:
            ev = json.loads(ev) if ev else {}
        except (ValueError, TypeError):
            ev = {}
    if isinstance(ev, dict):
        return str(ev.get("stance") or "").lower()
    return ""


def sort_dossiers(rows: list) -> list:
    """도셔 목록을 강세→중립→약세, 같은 stance 안에서는 최신순·확신도 높은 순으로 정렬.

    원본 리스트는 건드리지 않고 새 리스트를 반환한다. row 가 dict 가 아니면 맨 뒤로.
    """
    def key(r):
        if not isinstance(r, dict):
            return (9, 0.0, 0.0)
        rank = _STANCE_SORT_RANK.get(_dossier_stance(r), 3)
        try:
            created = -float(r.get("created_at") or 0)
        except (TypeError, ValueError):
            created = 0.0
        return (rank, created, -_conv(r.get("conviction")))
    return sorted(rows or [], key=key)


# 리서치 탭: 손익비(RR)·진입존·집행 흐름. cycle.executed.status → 사람말.
_RR_HELP = ("손익비(RR) = (목표가 − 진입존 중앙) ÷ (진입존 중앙 − 무효화가). "
            "예: 2.0이면 계획상 이익 여지가 위험(손절폭)의 약 2배.")

_ZONE_LABEL = {
    "in": ("zx-in", "진입존 안"),
    "above": ("zx-above", "진입존 위"),
    "below": ("zx-below", "진입존 아래"),
    "invalidated": ("zx-inval", "무효화 하회"),
}

_EXEC_FLOW = {
    "filled": ("fl-ok", "매수 체결"),
    "armed": ("fl-wait", "진입대기 등록"),
    "gap_armed": ("fl-wait", "존 재진입 대기"),
    "vetoed": ("fl-cut", "매수 제안 → 검증 거부"),
    "gap_rejected": ("fl-cut", "무효화로 진입 거부"),
    "gate_rejected": ("fl-cut", "리스크게이트 거부"),
    "no_dossier": ("fl-cut", "도씨에 없어 차단"),
    "arm_skipped": ("fl-cut", "대기 등록 실패"),
    "no_price": ("fl-cut", "가격 없어 스킵"),
}


def zone_status(price, entry_low, entry_high, invalidation=None,
                tol: float = 0.005) -> str | None:
    """현재가 대비 도씨에 진입존 위치. in/above/below/invalidated, 판단 불가면 None."""
    try:
        px, lo, hi = float(price), float(entry_low), float(entry_high)
    except (TypeError, ValueError):
        return None
    hi_tol = hi * (1 + tol)
    try:
        inval = float(invalidation) if invalidation is not None else None
    except (TypeError, ValueError):
        inval = None
    if inval is not None and px < inval:
        return "invalidated"
    if lo <= px <= hi_tol:
        return "in"
    if px > hi_tol:
        return "above"
    if px < lo:
        return "below"
    return None


def _after_dossier(ts, dossier: dict) -> bool:
    """이벤트/판단이 이 도씨에 생성 이후인지. created_at 없으면 True(보수적으로 표시)."""
    try:
        created = float(dossier.get("created_at"))
    except (TypeError, ValueError):
        return True
    try:
        return float(ts) >= created
    except (TypeError, ValueError):
        return False


def build_dossier_flow(dossier: dict, *, price=None, position: dict | None = None,
                       last_exec: dict | None = None,
                       last_buy: dict | None = None) -> dict:
    """도씨에 이후 매수 플로우 요약. {key, cls, label, detail, zone}.

    우선순위: 보유 → 진입대기 → (도씨에 이후) cycle 집행결과 → BUY 판단 → 존 위치 → 스탠스.
    """
    d = dossier if isinstance(dossier, dict) else {}
    stance = _dossier_stance(d)
    zone = zone_status(price, d.get("entry_low"), d.get("entry_high"),
                       d.get("invalidation"))
    pos = position if isinstance(position, dict) else None
    if pos and pos.get("state") == "open":
        return {"key": "holding", "cls": "fl-ok", "label": "보유 중",
                "detail": "도씨에 계획으로 진입한 포지션(또는 동기화 보유).", "zone": zone}
    if pos and pos.get("state") == "armed":
        return {"key": "armed", "cls": "fl-wait", "label": "진입대기",
                "detail": "존 재진입·데이트레 대기 등록됨. 감시 루프가 체결을 잡는다.",
                "zone": zone}

    ex = last_exec if isinstance(last_exec, dict) else None
    if ex and _after_dossier(ex.get("ts"), d):
        st = str(ex.get("status") or "")
        cls, lab = _EXEC_FLOW.get(st, ("fl-mute", st or "집행 기록"))
        detail = str(ex.get("reason") or "").strip() or lab
        return {"key": st or "exec", "cls": cls, "label": lab, "detail": detail, "zone": zone}

    buy = last_buy if isinstance(last_buy, dict) else None
    if buy and _after_dossier(buy.get("ts"), d):
        vd = str(buy.get("verdict") or "").lower()
        if vd == "vetoed":
            detail = str(buy.get("thesis") or "").strip() or "검증기가 매수 제안을 거부."
            return {"key": "vetoed", "cls": "fl-cut", "label": "매수 제안 → 검증 거부",
                    "detail": detail, "zone": zone}
        if vd == "approved":
            return {"key": "buy_approved", "cls": "fl-wait", "label": "매수 승인 · 집행 대기/진행",
                    "detail": str(buy.get("thesis") or "").strip() or "검증 통과.",
                    "zone": zone}
        return {"key": "buy_proposed", "cls": "fl-wait", "label": "매수 제안 있음",
                "detail": str(buy.get("thesis") or "").strip() or vd or "BUY 기록.",
                "zone": zone}

    if stance == "bullish" and zone == "above":
        return {"key": "zone_above", "cls": "fl-skip", "label": "진입존 위 · 추격 매수 안 함",
                "detail": "현재가가 진입존 상단을 넘김. 존 재진입 전엔 시장가 추격을 하지 않는다.",
                "zone": zone}
    if stance == "bullish" and zone == "below":
        return {"key": "zone_below", "cls": "fl-wait", "label": "진입존 아래 · 회복 대기",
                "detail": "현재가가 진입존 하단 아래. 존 회복 시에만 진입 후보.",
                "zone": zone}
    if stance == "bullish" and zone == "invalidated":
        return {"key": "zone_inval", "cls": "fl-cut", "label": "무효화가 하회 · 계획 깨짐",
                "detail": "현재가가 무효화가 아래 — 이 도씨에로는 신규 매수하지 않는다.",
                "zone": zone}
    if stance == "bullish" and zone == "in":
        return {"key": "zone_in", "cls": "fl-ready", "label": "진입존 안 · 아직 매수 플로우 없음",
                "detail": "가격은 계획 구간 안이지만, 이 도씨에 이후 BUY 집행 기록이 없다.",
                "zone": zone}
    if stance == "bearish":
        return {"key": "bear_plan", "cls": "fl-mute", "label": "약세 계획 · 신규매수 비대상",
                "detail": "약세 스탠스 — 스윙/장투 신규매수 게이트 대상이 아님.", "zone": zone}
    if stance == "neutral":
        return {"key": "neutral_plan", "cls": "fl-mute", "label": "관망 계획",
                "detail": "중립 스탠스 — 매수 근거로 쓰지 않음.", "zone": zone}
    return {"key": "idle", "cls": "fl-mute", "label": "아직 매수 플로우 없음",
            "detail": "도씨에만 있고 BUY 제안·집행 기록이 없다.", "zone": zone}


def index_latest_exec(cycle_events: list) -> dict:
    """cycle 이벤트(최신순)의 executed[] 를 symbol → {status,reason,ts} 로 접는다."""
    out: dict = {}
    for ev in cycle_events or []:
        if not isinstance(ev, dict):
            continue
        try:
            ts = float(ev.get("ts") or 0)
        except (TypeError, ValueError):
            ts = 0.0
        pl = ev.get("payload")
        if isinstance(pl, str):
            try:
                pl = json.loads(pl) if pl else {}
            except (ValueError, TypeError):
                pl = {}
        if not isinstance(pl, dict):
            pl = {}
        for e in pl.get("executed") or []:
            if not isinstance(e, dict):
                continue
            sym = e.get("symbol")
            if not sym or sym in out:
                continue
            out[str(sym)] = {"status": e.get("status"), "reason": e.get("reason"),
                             "action": e.get("action"), "ts": ts}
    return out


def index_latest_by_symbol(rows: list, *, ts_key: str = "ts") -> dict:
    """이미 최신순인 행 리스트를 symbol → 첫 행으로."""
    out: dict = {}
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        sym = r.get("symbol")
        if sym and sym not in out:
            out[str(sym)] = r
    return out


def _fair_band(e: dict) -> tuple:
    """워치리스트 항목의 절대 적정가 밴드(스캔 시점가 × (1+pct/100)).

    src.agents.value_trade.fair_price_low/high 와 같은 식(대시보드는 외부 호출 없이 계산).
    """
    try:
        base = float((e.get("metrics") or {}).get("price") or 0)
    except (TypeError, ValueError, AttributeError):
        return None, None
    if base <= 0:
        return None, None
    out = []
    for k in ("fair_low_pct", "fair_high_pct"):
        pct = e.get(k)
        try:
            out.append(round(base * (1 + float(pct) / 100.0), 2) if pct is not None else None)
        except (TypeError, ValueError):
            out.append(None)
    return out[0], out[1]


def _read_value_cfg() -> dict:
    """config 의 밸류 트랙 파라미터(슬리브·시간손절·시장) + 슬리브 계산에 필요한 risk 값.

    슬리브 예산은 뇌 사용량에 반응하는 동적 값이라(src.agents.value_trade.compute_sleeve)
    capital 뿐 아니라 max_gross_exposure·exposure_base·brain_reserve_pct 까지 필요하다.
    """
    out = {"sleeve_pct": 0.60, "brain_reserve_pct": 0.30, "time_stop_days": 120,
           "markets": ["KR", "US"], "capital": {}, "tranches": [1.0],
           "max_gross_exposure": None, "exposure_base": "capital"}
    try:
        import yaml
        raw = yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {}
        v = raw.get("value_trade", {}) or {}
        out["sleeve_pct"] = float(v.get("sleeve_pct", 0.60))
        out["brain_reserve_pct"] = float(v.get("brain_reserve_pct", 0.30))
        out["time_stop_days"] = int(v.get("time_stop_days", 120))
        out["markets"] = list(v.get("markets", ["KR", "US"]))
        out["tranches"] = list(v.get("tranches", [1.0]))
        rk = raw.get("risk", {}) or {}
        out["capital"] = dict(rk.get("capital", {}) or {})
        gross = rk.get("max_gross_exposure")
        out["max_gross_exposure"] = float(gross) if gross is not None else None
        out["exposure_base"] = str(rk.get("exposure_base", "capital")).lower()
    except Exception:
        pass
    return out


def _read_value_watchlist(limit: int = 15) -> list[dict]:
    """저평가 워치리스트 상위 N(확신도 내림차순). 파일 없거나 깨졌으면 빈 목록."""
    try:
        wl = json.loads(VALUE_WATCHLIST.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return []
    if not isinstance(wl, dict):
        return []
    out = [{"symbol": s, **e} for s, e in wl.items()
           if isinstance(e, dict) and e.get("stance") == "undervalued"]
    out.sort(key=lambda c: _conv(c.get("conviction")), reverse=True)
    return out[:limit]


def _read_value_decisions(limit: int = 15) -> list[dict]:
    """밸류 결정 저널(jsonl) 최근 N행 — 제안 1건 = 1행(시각·종목·액션·승인여부).

    깨진 줄은 그 줄만 건너뛴다(대시보드는 read-only, 절대 예외를 올리지 않는다).
    """
    try:
        lines = VALUE_DECISIONS.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict] = []
    for line in lines[-200:]:
        rec = _safe_json(line)
        if not isinstance(rec, dict):
            continue
        verd = {v.get("symbol"): v.get("approved") for v in (rec.get("verdicts") or [])
                if isinstance(v, dict)}
        for pr in (rec.get("proposals") or []):
            if not isinstance(pr, dict):
                continue
            out.append({"ts": rec.get("ts"), "symbol": pr.get("symbol"),
                        "action": pr.get("side"), "approved": verd.get(pr.get("symbol"))})
    return out[-limit:][::-1]


def _read_live_mode() -> bool:
    """라이브 여부: config broker.mode==live AND run.dry_run==false AND .env DRY_RUN!=true.
    브로커 라이브 게이트(3중 조건)와 동일. 어떤 값이든 못 읽으면 안전하게 페이퍼로 본다."""
    try:
        import yaml
        raw = yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {}
        mode = str((raw.get("broker", {}) or {}).get("mode", "paper")).lower()
        yaml_dry = bool((raw.get("run", {}) or {}).get("dry_run", True))
        env_dry = os.getenv("DRY_RUN", "true").lower() != "false"
        return mode == "live" and not yaml_dry and not env_dry
    except Exception:
        return False


def _trade_stats(paper: dict | None, fx: float | None = None) -> dict | None:
    """페이퍼 저널을 FIFO 로 매칭해 청산 라운드트립 승률/손익/수익률을 낸다.

    거래별 net 은 계좌 realized_pnl 규약과 맞춘다: qty*(매도가-매수가) - 매도수수료
    (매수수수료는 매수 시점 현금유출로 이미 반영됨). net>0 이면 승. 거래 수익률은
    net/원가(qty*매수가). 총수익률(원금대비)은 fx 로 KRW 환산해 합산한다.
    """
    if not paper:
        return None
    market_of = paper.get("symbol_market", {})
    lots: dict[str, deque] = defaultdict(deque)
    closed: list[dict] = []
    entries = 0
    for j in paper.get("journal", []):
        s = j["symbol"]; q = float(j["qty"]); p = float(j["price"]); f = float(j.get("fee", 0))
        if j["side"] == "BUY":
            entries += 1
            lots[s].append([q, p])
        else:  # SELL
            remain = q; realized = 0.0; cost = 0.0
            while remain > 1e-9 and lots[s]:
                lq, lp = lots[s][0]
                take = min(remain, lq)
                realized += take * (p - lp); cost += take * lp
                lq -= take; remain -= take
                if lq <= 1e-9:
                    lots[s].popleft()
                else:
                    lots[s][0][0] = lq
            matched = q - remain
            sell_fee = f * (matched / q) if q else 0.0
            net = realized - sell_fee
            closed.append({"symbol": s, "market": j.get("market") or market_of.get(s),
                           "qty": matched, "net": net, "cost": cost, "ts": j.get("ts"),
                           "ret_pct": (net / cost * 100) if cost else None})
    n = len(closed)
    wins = sum(1 for t in closed if t["net"] > 0)
    rbm = paper.get("realized_pnl", {}); sc = paper.get("start_cash", {})
    realized_krw = seed_krw = ret_total = None
    if fx:
        realized_krw = sum(v * (fx if m == "US" else 1) for m, v in rbm.items())
        seed_krw = sum(v * (fx if m == "US" else 1) for m, v in sc.items())
        ret_total = (realized_krw / seed_krw * 100) if seed_krw else None
    return {
        "closed": closed, "n": n, "wins": wins, "losses": n - wins, "entries": entries,
        "win_rate": (wins / n) if n else None,
        "realized_by_market": rbm, "start_cash": sc,
        "realized_krw": realized_krw, "seed_krw": seed_krw, "ret_total": ret_total, "fx": fx,
    }


def _bench_history(market: str):
    """벤치마크 지수 일봉(30분 TTL). 네트워크 실패 시 디스크 캐시→기존 값→None 순 폴백."""
    now = time.time()
    hit = _bench_cache.get(market)
    if hit and now - hit[0] < _BENCH_TTL:
        return hit[1]
    from src.datasources.history import fetch_history
    sym = BENCH[market][0]
    df = None
    try:
        df = fetch_history(sym, interval="1d", range_="1y", market=market, refresh=True)
    except Exception:
        try:
            df = fetch_history(sym, interval="1d", range_="1y", market=market)
        except Exception:
            df = hit[1] if hit else None
    if df is not None and len(df):
        _bench_cache[market] = (now, df)
        return df
    return hit[1] if hit else None


def _bench_ret_pct(df, since_dt: datetime) -> float | None:
    """since_dt 시점(직전 종가) 대비 지수 최근 종가 수익률(%). 데이터 없으면 None."""
    if df is None or not len(df):
        return None
    try:
        import pandas as pd
        t = pd.to_datetime(df["time"])
        if t.dt.tz is not None:
            t = t.dt.tz_localize(None)
        base = df.loc[t <= pd.Timestamp(since_dt.replace(tzinfo=None)), "close"]
        start = float(base.iloc[-1]) if len(base) else float(df["close"].iloc[0])
        last = float(df["close"].iloc[-1])
        return (last / start - 1) * 100 if start else None
    except Exception:
        return None


def _alpha_rows(paper: dict | None, latest_px: dict,
                bench_fn=_bench_history) -> list[dict]:
    """시장별 [포트폴리오 평가수익률 vs 지수 B&H → 알파]. 거래 없던 시장은 제외.

    포트 수익률 = (현금+보유평가) / 시작현금 - 1 (평가 기준, 미실현 포함).
    지수 수익률 = 첫 체결일 이후 벤치마크 B&H. 알파 = 포트 - 지수.
    """
    if not paper or not paper.get("journal"):
        return []
    first_ts: dict[str, datetime] = {}
    for j in paper["journal"]:
        m = j.get("market")
        try:
            dt = datetime.fromisoformat(str(j["ts"]))
            dt = dt.astimezone().replace(tzinfo=None) if dt.tzinfo else dt
        except (ValueError, TypeError, KeyError):
            continue
        if m and (m not in first_ts or dt < first_ts[m]):
            first_ts[m] = dt
    market_of = paper.get("symbol_market", {})
    rows = []
    for m, since in sorted(first_ts.items()):
        start_cash = float((paper.get("start_cash") or {}).get(m, 0) or 0)
        if start_cash <= 0:
            continue
        equity = float((paper.get("cash") or {}).get(m, 0) or 0)
        for sym, p in (paper.get("positions") or {}).items():
            if market_of.get(sym) != m:
                continue
            px = latest_px.get(sym) or p.get("avg_price") or 0
            equity += float(p.get("qty", 0)) * float(px)
        port_ret = (equity / start_cash - 1) * 100
        bench_ret = _bench_ret_pct(bench_fn(m), since) if m in BENCH else None
        rows.append({
            "market": m, "since": since.strftime("%m-%d"),
            "port_ret": port_ret, "bench": BENCH.get(m, ("?", "?"))[1],
            "bench_ret": bench_ret,
            "alpha": (port_ret - bench_ret) if bench_ret is not None else None,
        })
    return rows


def _parse_jts(ts) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(ts))
        return dt.astimezone().replace(tzinfo=None) if dt.tzinfo else dt
    except (ValueError, TypeError):
        return None


def _hist_closes(symbol: str, market: str = "KR") -> dict[str, float]:
    """일봉 종가 {YYYY-MM-DD: close}. 실패 시 빈 dict."""
    try:
        from src.datasources.history import fetch_history
        df = fetch_history(symbol, interval="1d", range_="1y", market=market)
    except Exception:
        return {}
    if df is None or not len(df):
        return {}
    out: dict[str, float] = {}
    for _, row in df.iterrows():
        try:
            t = row["time"]
            d = t.strftime("%Y-%m-%d") if hasattr(t, "strftime") else str(t)[:10]
            out[d] = float(row["close"])
        except (TypeError, ValueError, KeyError):
            continue
    return out


def _kr_journal_fills(paper: dict, store_rows: list | None = None) -> list[dict]:
    """KR 체결 타임라인. 저널 누락 보유는 store opened_at·평단으로 가상 매수 보강."""
    fills = []
    for j in paper.get("journal") or []:
        if (j.get("market") or "KR") != "KR":
            continue
        dt = _parse_jts(j.get("ts"))
        if not dt:
            continue
        fills.append({
            "dt": dt, "symbol": str(j["symbol"]), "side": j["side"],
            "qty": float(j["qty"]), "price": float(j["price"]),
            "fee": float(j.get("fee") or 0),
        })
    seen = {f["symbol"] for f in fills if f["side"] == "BUY"}
    # 저널에 매수가 없는 현재 보유 → store 평단·진입시각으로 보강
    paper_pos = paper.get("positions") or {}
    for row in store_rows or []:
        if (row.get("market") or "KR") != "KR":
            continue
        if row.get("state") != "open":
            continue
        sym = str(row.get("symbol") or "")
        if not sym or sym in seen or sym not in paper_pos:
            continue
        qty = float((paper_pos.get(sym) or {}).get("qty") or row.get("qty") or 0)
        px = float((paper_pos.get(sym) or {}).get("avg_price") or row.get("avg_price") or 0)
        if qty <= 0 or px <= 0:
            continue
        dt = None
        oa = row.get("opened_at")
        if isinstance(oa, (int, float)) and oa > 0:
            dt = datetime.fromtimestamp(float(oa))
        if dt is None and paper.get("journal"):
            dt = _parse_jts(paper["journal"][0].get("ts"))
        if dt is None:
            continue
        fills.append({"dt": dt, "symbol": sym, "side": "BUY", "qty": qty,
                      "price": px, "fee": 0.0})
        seen.add(sym)
    fills.sort(key=lambda f: f["dt"])
    return fills


def _equity_vs_kospi(paper: dict | None, snap: dict | None,
                     store_rows: list | None = None,
                     latest_px: dict | None = None) -> dict | None:
    """KR 포트 평가수익률 vs 코스피 누적 수익률 시계열.

    반환: {dates, port, bench, port_now, bench_now, alpha_now, since, bench_name}
    포인트는 일별(%). 데이터 부족 시 None.
    """
    now = time.time()
    hit = _chart_cache.get("KR")
    # 스냅샷 ts 가 바뀌면 캐시 무효 — 당일 평가 반영
    snap_ts = float((snap or {}).get("ts") or 0)
    if hit and now - hit[0] < _CHART_TTL and hit[1].get("snap_ts") == snap_ts:
        return hit[1]

    if not paper:
        return None
    start_cash = float((paper.get("start_cash") or {}).get("KR") or 0)
    if start_cash <= 0:
        return None
    fills = _kr_journal_fills(paper, store_rows)
    if not fills and not (paper.get("positions") or {}):
        return None

    # 심볼별 일봉
    syms = {f["symbol"] for f in fills} | set((paper.get("positions") or {}).keys())
    closes = {s: _hist_closes(s, "KR") for s in syms}
    bsym, bname = BENCH["KR"]
    bcloses = _hist_closes(bsym, "KR")
    if not bcloses:
        return None

    first_dt = fills[0]["dt"] if fills else datetime.now()
    since = first_dt.strftime("%Y-%m-%d")
    # 벤치 캘린더 중 since 이후
    days = sorted(d for d in bcloses if d >= since)
    if not days:
        return None

    def _px(sym: str, day: str, holdings_px: dict) -> float | None:
        c = closes.get(sym) or {}
        if day in c:
            return c[day]
        # ffill: day 이전 마지막 종가
        prev = [c[d] for d in sorted(c) if d <= day]
        if prev:
            return prev[-1]
        return holdings_px.get(sym)

    cash = start_cash
    hold: dict[str, float] = {}
    hold_px: dict[str, float] = {}
    fi = 0
    dates: list[str] = []
    port: list[float] = []
    bench: list[float] = []
    b0 = None

    for day in days:
        # 당일 체결 반영(그날 종가 평가 전에)
        while fi < len(fills) and fills[fi]["dt"].strftime("%Y-%m-%d") <= day:
            f = fills[fi]; fi += 1
            q, px, fee = f["qty"], f["price"], f["fee"]
            if f["side"] == "BUY":
                cash -= q * px + fee
                hold[f["symbol"]] = hold.get(f["symbol"], 0.0) + q
                hold_px[f["symbol"]] = px
            else:
                take = min(q, hold.get(f["symbol"], 0.0))
                cash += take * px - fee
                hold[f["symbol"]] = hold.get(f["symbol"], 0.0) - take
                if hold.get(f["symbol"], 0) <= 1e-9:
                    hold.pop(f["symbol"], None)
        eq = cash
        for s, q in hold.items():
            px = _px(s, day, hold_px)
            if px is None:
                px = hold_px.get(s, 0)
            eq += q * float(px)
        pret = (eq / start_cash - 1.0) * 100.0
        bc = bcloses.get(day)
        if bc is None:
            continue
        if b0 is None:
            b0 = bc
        Bret = (bc / b0 - 1.0) * 100.0
        dates.append(day)
        port.append(pret)
        bench.append(Bret)

    # 오늘 실계좌 스냅샷으로 종점 보정(저널 누락·미실현 반영)
    if snap and dates:
        try:
            live_eq = (float((snap.get("cash") or {}).get("KR") or 0)
                       + float((snap.get("market_value") or {}).get("KR") or 0))
            if live_eq > 0:
                today = datetime.now().strftime("%Y-%m-%d")
                live_ret = (live_eq / start_cash - 1.0) * 100.0
                if dates[-1] == today:
                    port[-1] = live_ret
                else:
                    # 장중: 벤치는 전일 종가 유지, 포트만 오늘 점 추가
                    dates.append(today)
                    port.append(live_ret)
                    bench.append(bench[-1])
        except (TypeError, ValueError):
            pass
    # latest_px 로도 종점 보강(스냅 없을 때)
    elif latest_px and dates:
        eq = float((paper.get("cash") or {}).get("KR") or 0)
        for s, p in (paper.get("positions") or {}).items():
            px = latest_px.get(s) or p.get("avg_price") or 0
            eq += float(p.get("qty") or 0) * float(px)
        port[-1] = (eq / start_cash - 1.0) * 100.0

    if len(dates) < 2:
        return None
    payload = {
        "dates": dates, "port": port, "bench": bench,
        "port_now": port[-1], "bench_now": bench[-1],
        "alpha_now": port[-1] - bench[-1],
        "since": since, "bench_name": bname, "snap_ts": snap_ts,
    }
    _chart_cache["KR"] = (now, payload)
    return payload


def _ret_chart_svg(series: dict, w: int = 640, h: int = 220) -> str:
    """수익률(%) 이중선 SVG — Argus vs 벤치마크."""
    dates = series.get("dates") or []
    port = series.get("port") or []
    bench = series.get("bench") or []
    if len(dates) < 2 or len(port) != len(dates) or len(bench) != len(dates):
        return ""
    pad_l, pad_r, pad_t, pad_b = 44.0, 12.0, 16.0, 28.0
    plot_w = w - pad_l - pad_r
    plot_h = h - pad_t - pad_b
    vals = list(port) + list(bench) + [0.0]
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-6:
        hi, lo = hi + 1.0, lo - 1.0
    margin = (hi - lo) * 0.08
    lo -= margin; hi += margin

    def _x(i: int) -> float:
        return pad_l + i * plot_w / (len(dates) - 1)

    def _y(v: float) -> float:
        return pad_t + (hi - v) / (hi - lo) * plot_h

    def _poly(arr, color: str) -> str:
        pts = " ".join(f"{_x(i):.1f},{_y(v):.1f}" for i, v in enumerate(arr))
        return (f"<polyline fill=none stroke='{color}' stroke-width='2' "
                f"stroke-linejoin=round points='{pts}'/>")

    # 0% 기준선
    y0 = _y(0.0)
    grid = []
    for t in (lo, 0.0, hi):
        if t == 0.0 or abs(t - lo) < 1e-9 or abs(t - hi) < 1e-9:
            yy = _y(t)
            grid.append(f"<line x1='{pad_l}' y1='{yy:.1f}' x2='{w-pad_r}' y2='{yy:.1f}' "
                        f"stroke='#2a3344' stroke-dasharray='{'3 3' if abs(t)>1e-9 else '0'}'/>")
            grid.append(f"<text x='{pad_l-6}' y='{yy+3:.1f}' text-anchor='end' "
                        f"fill='#8b94a3' font-size='10'>{t:+.1f}%</text>")

    # x 라벨: 첫·중간·끝
    xlab = [0, len(dates) // 2, len(dates) - 1]
    labs = []
    for i in xlab:
        labs.append(f"<text x='{_x(i):.1f}' y='{h-8}' text-anchor='middle' "
                    f"fill='#8b94a3' font-size='10'>{escape(dates[i][5:])}</text>")

    pcol, bcol = "#5aa9ff", "#ffb454"
    hover = (
        f"<g class=bc-hover-layer>"
        f"<line class=bc-vline x1='0' y1='{pad_t:.1f}' x2='0' y2='{pad_t + plot_h:.1f}' "
        f"stroke='#5a6373' stroke-width='1' stroke-dasharray='4 3' visibility='hidden'/>"
        f"<circle class='bc-dot bc-dot-port' r='4' fill='{pcol}' stroke='#0b0e14' "
        f"stroke-width='1.5' visibility='hidden'/>"
        f"<circle class='bc-dot bc-dot-bench' r='4' fill='{bcol}' stroke='#0b0e14' "
        f"stroke-width='1.5' visibility='hidden'/>"
        f"<rect class=bc-overlay x='{pad_l:.1f}' y='{pad_t:.1f}' width='{plot_w:.1f}' "
        f"height='{plot_h:.1f}' fill='transparent' cursor='crosshair'/>"
        f"</g>"
    )
    return (
        f"<svg viewBox='0 0 {w} {h}' width='100%' height='{h}' "
        f"style='display:block;max-width:{w}px'>"
        f"<rect x='{pad_l}' y='{pad_t}' width='{plot_w}' height='{plot_h}' "
        f"fill='#0d1320' stroke='#1c2433'/>"
        f"{''.join(grid)}"
        f"<line x1='{pad_l}' y1='{y0:.1f}' x2='{w-pad_r}' y2='{y0:.1f}' "
        f"stroke='#3a4558' stroke-width='1'/>"
        f"{_poly(bench, bcol)}{_poly(port, pcol)}"
        f"<circle cx='{_x(len(dates)-1):.1f}' cy='{_y(port[-1]):.1f}' r='3' fill='{pcol}'/>"
        f"<circle cx='{_x(len(dates)-1):.1f}' cy='{_y(bench[-1]):.1f}' r='3' fill='{bcol}'/>"
        f"{''.join(labs)}"
        f"{hover}"
        f"</svg>"
    )


def _bench_chart_points(series: dict, w: int = 640, h: int = 220) -> list[dict]:
    """툴팁용 날짜별 좌표·수익률."""
    dates = series.get("dates") or []
    port = series.get("port") or []
    bench = series.get("bench") or []
    if len(dates) < 2:
        return []
    pad_l, pad_r, pad_t, pad_b = 44.0, 12.0, 16.0, 28.0
    plot_w = w - pad_l - pad_r
    plot_h = h - pad_t - pad_b
    vals = list(port) + list(bench) + [0.0]
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-6:
        hi, lo = hi + 1.0, lo - 1.0
    margin = (hi - lo) * 0.08
    lo -= margin
    hi += margin

    def _x(i: int) -> float:
        return pad_l + i * plot_w / (len(dates) - 1)

    def _y(v: float) -> float:
        return pad_t + (hi - v) / (hi - lo) * plot_h

    out = []
    for i, d in enumerate(dates):
        pv, bv = float(port[i]), float(bench[i])
        out.append({
            "date": d, "port": pv, "bench": bv, "alpha": pv - bv,
            "x": round(_x(i), 1), "yp": round(_y(pv), 1), "yb": round(_y(bv), 1),
        })
    return out


def _latest_prices(cur, symbols: list[str]) -> dict:
    """종목의 최신 스냅샷 가격(감시 루프가 매 틱 기록 → 장중엔 사실상 실시간)."""
    out = {}
    for s in symbols:
        r = cur.execute("select price from snapshots where symbol=? and price is not null"
                        " order by ts desc limit 1", (s,)).fetchone()
        if r:
            out[s] = r["price"]
    return out


def _gather() -> dict:
    now = time.time()
    cut = now - WINDOW_SEC
    data: dict = {"now": now, "kr_session": current_session("KR"),
                  "us_session": current_session("US")}
    hb = _read_heartbeat()
    data["hb"] = hb
    data["hb_age"] = (now - hb["ts"]) if hb else None
    data["alert"] = _read_alert()
    # 프로세스/데몬 정보(시스템 탭)
    pid = _read_pidfile()
    data["pid"] = pid
    data["pid_alive"] = _pid_alive(pid) if pid else False

    con = _ro_conn()
    if con is None:
        data["db"] = False
        return data
    data["db"] = True
    cur = con.cursor()
    tally: dict[str, int] = {}
    for r in cur.execute("select kind, count(*) n from events where ts>? group by kind", (cut,)):
        tally[r["kind"]] = r["n"]
    data["tally"] = tally
    data["events"] = [dict(r) for r in cur.execute(
        "select ts,kind,symbol,payload from events order by ts desc limit 30")]
    row = cur.execute("select ts,payload from events where kind='cycle' order by ts desc limit 1").fetchone()
    data["last_cycle"] = (row["ts"], _safe_json(row["payload"])) if row else None
    bd = cur.execute("select ts,payload from events where kind='brain_done' order by ts desc limit 1").fetchone()
    data["last_brain_done"] = bd["ts"] if bd else None
    data["brain_summary"] = _safe_json(bd["payload"]).get("summary") if bd else None
    # 뇌 가용성(연속 실패) — 세션 한도 등으로 루프(하트비트)는 멀쩡한데 판단만 멈추는
    # '무음 실패'를 보이게 한다. 워치독은 하트비트만 보므로 이 상태를 못 잡는다.
    health: dict = {}
    for pref in ("brain", "value"):
        hr = cur.execute("select ts,payload from events where kind=? "
                         "order by ts desc limit 1", (f"{pref}_health",)).fetchone()
        if hr:
            health[pref] = dict(_safe_json(hr["payload"]) or {}, ts=hr["ts"])
    data["brain_health"] = health
    # 영속 뇌 모드(회로차단/브릿지) — health 이벤트보다 SSOT.
    try:
        from src.engine.brain_mode import load_mode
        bm = load_mode(ROOT / "data" / "brain_mode.json")
        data["brain_mode"] = bm.get("mode", "ok")
        data["brain_mode_state"] = bm
        base = health.get("brain") or {"consecutive_failures": 0, "last_ok_ts": 0}
        health["brain"] = {**base, "mode": bm.get("mode"),
                           "reset_at": bm.get("reset_at"),
                           "bridge_armed": bm.get("bridge_armed")}
        data["brain_health"] = health
    except Exception:
        data["brain_mode"] = "ok"
    data["positions"] = [dict(r) for r in cur.execute(
        "select symbol,market,strategy,state,qty,avg_price,thesis,stop_price,target_price,"
        "opened_at,meta from positions where state not in ('closed') order by opened_at desc")]
    # 최근 판단(decisions) 5건
    data["decisions"] = [dict(r) for r in cur.execute(
        "select ts,symbol,action,conviction,verdict from decisions order by ts desc limit 5")]
    # 도시에(신선한 것만, 심볼별 최신 1건) — 표시는 강세→중립→약세, 동순위 최신순
    data["dossiers"] = sort_dossiers([dict(r) for r in cur.execute(
        "select * from dossiers d where expires_at > :now and created_at = "
        "(select max(created_at) from dossiers where symbol = d.symbol)",
        {"now": now})])
    # 리서치 탭 흐름: 최근 cycle 집행·BUY 판단(심볼별 최신 1건)
    data["research_cycles"] = [dict(r) for r in cur.execute(
        "select ts,payload from events where kind='cycle' order by ts desc limit 120")]
    data["research_buys"] = [dict(r) for r in cur.execute(
        "select ts,symbol,action,conviction,thesis,verdict,payload from decisions "
        "where action='BUY' order by ts desc limit 120")]
    # 오늘 공시(최근 24h)
    data["disclosures"] = [dict(r) for r in cur.execute(
        "select ts,symbol,payload from events where kind='disclosure' and ts>? "
        "order by ts desc", (now - DISCLOSURE_SEC,))]
    # 오늘(KST) 봇 라이브 거래: 체결(live_order)·전송실패(live_order_error)·매수차단(buy_blocked)
    kst_midnight = datetime.now(_KST).replace(hour=0, minute=0, second=0,
                                              microsecond=0).timestamp()
    data["live_trades"] = [dict(r) for r in cur.execute(
        "select ts,kind,symbol,payload from events where kind in "
        "('live_order','live_order_error','buy_blocked') and ts>=? "
        "order by ts desc limit 10", (kst_midnight,))]
    # Athena 실행 로그(최근 5건)
    data["athena_runs"] = [dict(r) for r in cur.execute(
        "select ts,payload from events where kind='athena_done' order by ts desc limit 5")]
    # 청산 포지션(성과귀속: 전략별 / 도시에 A·B)
    data["closed_pos"] = [dict(r) for r in cur.execute(
        "select symbol,market,strategy,qty,avg_price,exit_price,pnl,exit_reason,meta,closed_at "
        "from positions where state='closed' and pnl is not null order by closed_at desc")]
    sr = cur.execute("select count(*) n, max(ts) m from snapshots").fetchone()
    data["snap_count"], data["snap_last"] = sr["n"], sr["m"]
    try:
        data["paper"] = json.loads(PAPER.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data["paper"] = None
    # 보유·진입대기·도씨에 종목 최신가(오늘 평가손익 + 리서치 존 위치)
    try:
        held_syms = [x["symbol"] for x in data["positions"]]
        dos_syms = [x["symbol"] for x in data["dossiers"] if x.get("symbol")]
        data["pos_px"] = _latest_prices(cur, list(dict.fromkeys(held_syms + dos_syms)))
    except Exception:
        data["pos_px"] = {}
    try:                                     # 알파: 포트폴리오 평가수익률 vs 지수 B&H
        held = list(((data["paper"] or {}).get("positions") or {}).keys())
        data["alpha"] = _alpha_rows(data["paper"], _latest_prices(cur, held))
    except Exception:                        # 알파 실패가 대시보드를 막지 않게
        data["alpha"] = []
    # 차트용 open 행은 close 전에 복사(스냅샷은 아래에서 읽음)
    data["_open_for_chart"] = [x for x in (data.get("positions") or [])
                               if x.get("state") == "open"]
    con.close()
    # market_state 는 한 번만 읽어 fx·sentiment 를 함께 뽑는다.
    try:
        ms = json.loads(MARKET_STATE.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        ms = {}
    try:
        fx = float(ms["fx"]["USDKRW"])
    except (KeyError, ValueError, TypeError):
        fx = None
    data["fx"] = fx
    sent = ms.get("sentiment")
    data["sentiment"] = sent if isinstance(sent, dict) else {}
    data["fear_history"] = _read_fear_history()
    data["trades"] = _trade_stats(data.get("paper"), fx)
    data["names"] = _load_names()
    data["base_rates"] = _read_base_rates()
    data["snapshot"] = _read_snapshot()
    data["live_mode"] = _read_live_mode()
    try:
        data["bench_chart"] = _equity_vs_kospi(
            data.get("paper"), data.get("snapshot"),
            store_rows=data.pop("_open_for_chart", []),
            latest_px=data.get("pos_px") or {})
    except Exception:
        data["bench_chart"] = None
        data.pop("_open_for_chart", None)
    # 밸류 탭 재료(전부 read-only 파일 — 없으면 빈 값)
    data["value_cfg"] = _read_value_cfg()
    data["value_watchlist"] = _read_value_watchlist()
    data["value_decisions"] = _read_value_decisions()
    return data


def _safe_json(s) -> dict:
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        return {}


# ───────────────────────── HTML 렌더 ─────────────────────────

CSS = """
:root{color-scheme:dark;}
*{box-sizing:border-box;}
body{margin:0;background:#0b0e14;color:#e6e9ef;font:14px/1.5 -apple-system,Segoe UI,Roboto,'Malgun Gothic',sans-serif;}
.wrap{max-width:1040px;margin:0 auto;padding:22px 18px 60px;}
h1{font-size:20px;margin:0;letter-spacing:.5px;}
h1 .eye{color:#5aa9ff;}
.sub{color:#8b94a3;font-size:12px;margin-top:3px;}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:18px 0;}
.card{background:#141925;border:1px solid #222a3a;border-radius:12px;padding:14px 16px;}
.card .k{color:#8b94a3;font-size:11px;text-transform:uppercase;letter-spacing:.6px;}
.card .v{font-size:22px;font-weight:600;margin-top:4px;font-variant-numeric:tabular-nums;}
.card .v small{font-size:12px;color:#8b94a3;font-weight:400;}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px;vertical-align:middle;}
.ok{background:#3ddc84;} .warn{background:#ffb454;} .bad{background:#ff5c63;}
.sec{margin:22px 0 8px;font-size:13px;color:#8b94a3;text-transform:uppercase;letter-spacing:.6px;}
.panel{background:#141925;border:1px solid #222a3a;border-radius:12px;padding:16px 18px;}
.alert{background:linear-gradient(90deg,#3a0d10,#2a0a0c);border:1px solid #ff5c63;border-radius:12px;padding:14px 18px;margin-bottom:6px;}
.alert b{color:#ff8c91;}
.alert ul{margin:8px 0 0;padding-left:20px;}
.mv{color:#cdd4e0;line-height:1.65;}
table{width:100%;border-collapse:collapse;font-size:13px;}
th,td{text-align:left;padding:7px 8px;border-bottom:1px solid #1c2433;}
th{color:#8b94a3;font-weight:500;font-size:11px;text-transform:uppercase;letter-spacing:.4px;}
td.mono,.mono{font-variant-numeric:tabular-nums;font-family:ui-monospace,Consolas,monospace;}
.chip{padding:1px 8px;border-radius:20px;font-size:11px;font-weight:600;background:#1c2433;}
.veto{color:#ff8c91;} .fill{color:#3ddc84;}
.pos{color:#3ddc84;} .neg{color:#ff5c63;}
.muted{color:#6b7280;}
.foot{margin-top:26px;color:#5a6373;font-size:11px;text-align:center;}
/* stance 뱃지 / route 칩 */
.b-bull{padding:1px 8px;border-radius:20px;font-size:11px;font-weight:600;background:#12341f;color:#3ddc84;}
.b-neut{padding:1px 8px;border-radius:20px;font-size:11px;font-weight:600;background:#232a36;color:#9aa4b2;}
.b-bear{padding:1px 8px;border-radius:20px;font-size:11px;font-weight:600;background:#3a1013;color:#ff5c63;}
/* 자산 관제 패널 */
.asset{background:linear-gradient(120deg,#101a2a,#0d1320);border:1px solid #243349;border-radius:14px;padding:18px 20px 16px;margin:14px 0 6px;}
.asset .hd{display:flex;align-items:center;gap:10px;margin-bottom:14px;flex-wrap:wrap;}
.asset .hd .ttl{font-size:14px;font-weight:600;letter-spacing:.4px;}
.asset .hd .fresh{margin-left:auto;font-size:11px;color:#8b94a3;}
.asset-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:14px;}
.asset-grid .k{color:#8b94a3;font-size:11px;text-transform:uppercase;letter-spacing:.6px;}
.asset-grid .big{font-size:28px;font-weight:700;margin-top:5px;font-variant-numeric:tabular-nums;line-height:1.15;}
.asset-grid .big small{font-size:13px;font-weight:500;}
.asset-grid .sub2{font-size:12px;margin-top:3px;font-variant-numeric:tabular-nums;}
.b-live{padding:2px 10px;border-radius:20px;font-size:12px;font-weight:700;background:#3a1013;color:#ff8c91;border:1px solid #ff5c63;}
.b-paper{padding:2px 10px;border-radius:20px;font-size:12px;font-weight:700;background:#12341f;color:#3ddc84;}
.freshwarn{color:#ffb454;}
.r-wake{padding:1px 8px;border-radius:20px;font-size:11px;font-weight:600;background:#3a1013;color:#ff8c91;}
.r-queue{padding:1px 8px;border-radius:20px;font-size:11px;font-weight:600;background:#332a10;color:#ffcf6b;}
.vd-approved{padding:1px 8px;border-radius:20px;font-size:11px;font-weight:600;background:#12341f;color:#3ddc84;}
.vd-vetoed{padding:1px 8px;border-radius:20px;font-size:11px;font-weight:600;background:#3a1013;color:#ff8c91;}
.vd-other{padding:1px 8px;border-radius:20px;font-size:11px;font-weight:600;background:#232a36;color:#9aa4b2;}
/* 시장 심리(공포·탐욕) 패널 */
.fg-ef{padding:1px 8px;border-radius:20px;font-size:11px;font-weight:600;background:#3a1013;color:#ff5c63;}
.fg-f{padding:1px 8px;border-radius:20px;font-size:11px;font-weight:600;background:#3a2410;color:#ff8c4b;}
.fg-n{padding:1px 8px;border-radius:20px;font-size:11px;font-weight:600;background:#232a36;color:#9aa4b2;}
.fg-g{padding:1px 8px;border-radius:20px;font-size:11px;font-weight:600;background:#12341f;color:#7bd88f;}
.fg-eg{padding:1px 8px;border-radius:20px;font-size:11px;font-weight:600;background:#0d3b1e;color:#3ddc84;}
.fg-wrap+.fg-wrap{border-top:1px solid #1c2433;margin-top:16px;padding-top:16px;}
.fg-row{display:flex;gap:18px;align-items:flex-start;flex-wrap:wrap;}
.fg-main{flex:1;min-width:280px;}
.fg-spark{flex:0 0 auto;}
.fg-head{display:flex;align-items:center;gap:10px;flex-wrap:wrap;}
.fg-lab{font-size:11px;color:#8b94a3;text-transform:uppercase;letter-spacing:.6px;}
.fg-score{font-size:30px;font-weight:700;font-variant-numeric:tabular-nums;line-height:1.1;}
.fg-gauge{position:relative;height:10px;border-radius:6px;margin-top:10px;background:linear-gradient(90deg,#ff5c63 0%,#ff8c4b 25%,#9aa4b2 50%,#7bd88f 75%,#3ddc84 100%);}
.fg-mark{position:absolute;top:-4px;width:3px;height:18px;border-radius:2px;background:#fff;box-shadow:0 0 6px rgba(0,0,0,.8);transform:translateX(-50%);}
.fg-ticks{display:flex;justify-content:space-between;font-size:10px;color:#5a6373;margin-top:6px;}
.fg-foot{display:flex;flex-wrap:wrap;gap:8px 18px;align-items:baseline;margin-top:12px;}
.fg-comps{display:flex;flex-wrap:wrap;gap:10px 16px;}
.fg-c{display:flex;align-items:center;gap:6px;font-size:11px;color:#8b94a3;}
.fg-clab{font-size:10px;color:#5a6373;text-transform:uppercase;letter-spacing:.5px;align-self:center;}
.fg-cb{display:inline-block;width:60px;height:5px;border-radius:3px;background:#1c2433;overflow:hidden;}
.fg-cb i{display:block;height:100%;border-radius:3px;}
.fg-cv{font-variant-numeric:tabular-nums;color:#cdd4e0;}
.fg-note{font-size:12px;color:#8b94a3;margin-left:auto;}
.fg-cap{font-size:10px;color:#5a6373;margin-top:3px;text-align:right;}
.fg-src{font-size:11px;color:#5a6373;margin-top:8px;}
/* Argus vs 코스피 수익률 차트 */
.bench-chart{margin-top:16px;padding-top:14px;border-top:1px solid #1c2433;}
.bench-chart .bc-hd{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;margin-bottom:10px;}
.bench-chart .bc-ttl{font-size:12px;color:#8b94a3;text-transform:uppercase;letter-spacing:.6px;}
.bench-chart .bc-leg{display:flex;gap:14px;flex-wrap:wrap;font-size:12px;margin-left:auto;}
.bench-chart .bc-leg i{display:inline-block;width:10px;height:3px;border-radius:2px;margin-right:5px;vertical-align:middle;}
.bench-chart .bc-stats{display:flex;gap:16px;flex-wrap:wrap;margin-top:8px;font-size:13px;font-variant-numeric:tabular-nums;}
.bench-chart .bc-stats .k{color:#8b94a3;font-size:11px;margin-right:4px;}
.bench-chart .bc-wrap{position:relative;}
.bench-chart .bc-tip{position:absolute;z-index:5;min-width:168px;padding:10px 12px;border-radius:10px;
  background:#141925;border:1px solid #2a3344;box-shadow:0 8px 24px rgba(0,0,0,.45);
  font-size:12px;line-height:1.55;pointer-events:none;}
.bench-chart .bc-tip[hidden]{display:none;}
.bench-chart .bc-tip .bc-tip-dt{font-weight:600;color:#e6e9ef;margin-bottom:6px;font-size:13px;}
.bench-chart .bc-tip .bc-tip-row{display:flex;justify-content:space-between;gap:12px;}
.bench-chart .bc-tip .bc-tip-lab{color:#8b94a3;}
.bench-chart .bc-tip .bc-tip-val{font-variant-numeric:tabular-nums;font-family:ui-monospace,Consolas,monospace;}
/* 탭 */
.tabs>input[type=radio]{position:absolute;opacity:0;pointer-events:none;}
.tabbar{display:flex;gap:6px;margin:18px 0 4px;border-bottom:1px solid #222a3a;flex-wrap:wrap;}
.tabbar label{padding:9px 16px;font-size:13px;color:#8b94a3;cursor:pointer;border:1px solid transparent;border-bottom:none;border-radius:9px 9px 0 0;user-select:none;}
.tabbar label:hover{color:#cdd4e0;}
.tabpage{display:none;}
#t-today:checked~.tabbar label[for=t-today],
#t-research:checked~.tabbar label[for=t-research],
#t-perf:checked~.tabbar label[for=t-perf],
#t-value:checked~.tabbar label[for=t-value],
#t-system:checked~.tabbar label[for=t-system]{color:#e6e9ef;background:#141925;border-color:#222a3a;}
#t-today:checked~.page-today,
#t-research:checked~.page-research,
#t-perf:checked~.page-perf,
#t-value:checked~.page-value,
#t-system:checked~.page-system{display:block;}
/* 리서치 도씨에 카드 */
.dos-help{font-size:12px;color:#8b94a3;line-height:1.55;margin:0 0 12px;}
.dos-list{display:flex;flex-direction:column;gap:8px;}
.dos-card{background:#101622;border:1px solid #222a3a;border-radius:10px;overflow:hidden;}
.dos-card>summary{list-style:none;cursor:pointer;display:flex;flex-wrap:wrap;gap:8px 12px;
  align-items:center;padding:10px 12px;user-select:none;}
.dos-card>summary::-webkit-details-marker{display:none;}
.dos-card>summary:hover{background:#141925;}
.dos-card[open]>summary{border-bottom:1px solid #1c2433;}
.dos-card .dos-name{font-weight:600;min-width:4.5em;}
.dos-card .dos-meta{margin-left:auto;display:flex;flex-wrap:wrap;gap:8px;align-items:center;
  font-size:12px;color:#8b94a3;}
.dos-body{padding:12px 14px 14px;}
.dos-levels{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;
  margin-bottom:12px;}
.dos-levels .k{font-size:10px;color:#5a6373;text-transform:uppercase;letter-spacing:.5px;}
.dos-levels .v{font-size:13px;margin-top:2px;font-variant-numeric:tabular-nums;
  font-family:ui-monospace,Consolas,monospace;}
.dos-thesis{font-size:13px;line-height:1.65;color:#cdd4e0;white-space:pre-wrap;word-break:break-word;}
.dos-flow{margin-top:12px;padding-top:12px;border-top:1px solid #1c2433;font-size:12px;line-height:1.55;}
.dos-flow .fl-detail{color:#8b94a3;margin-top:4px;white-space:pre-wrap;word-break:break-word;}
.zx-in{padding:1px 8px;border-radius:20px;font-size:11px;font-weight:600;background:#12341f;color:#3ddc84;}
.zx-above{padding:1px 8px;border-radius:20px;font-size:11px;font-weight:600;background:#332a10;color:#ffcf6b;}
.zx-below{padding:1px 8px;border-radius:20px;font-size:11px;font-weight:600;background:#232a36;color:#9aa4b2;}
.zx-inval{padding:1px 8px;border-radius:20px;font-size:11px;font-weight:600;background:#3a1013;color:#ff8c91;}
.fl-ok{padding:1px 8px;border-radius:20px;font-size:11px;font-weight:600;background:#12341f;color:#3ddc84;}
.fl-wait{padding:1px 8px;border-radius:20px;font-size:11px;font-weight:600;background:#1a2740;color:#5aa9ff;}
.fl-ready{padding:1px 8px;border-radius:20px;font-size:11px;font-weight:600;background:#12341f;color:#7bd88f;}
.fl-cut{padding:1px 8px;border-radius:20px;font-size:11px;font-weight:600;background:#3a1013;color:#ff8c91;}
.fl-skip{padding:1px 8px;border-radius:20px;font-size:11px;font-weight:600;background:#332a10;color:#ffcf6b;}
.fl-mute{padding:1px 8px;border-radius:20px;font-size:11px;font-weight:600;background:#232a36;color:#9aa4b2;}
"""


def _status_dot(age, market_open) -> str:
    if age is None or age > 300:
        return '<span class="dot bad"></span>'
    if age > 90 and market_open:
        return '<span class="dot warn"></span>'
    return '<span class="dot ok"></span>'


def _fmt(n, dp=0) -> str:
    try:
        return f"{float(n):,.{dp}f}"
    except (TypeError, ValueError):
        return "–"


def _fmt_ts(v) -> str:
    """ISO 문자열(저널) 또는 epoch(스토어) 둘 다 'MM-DD HH:MM' 로."""
    try:
        if v is None:
            return "–"
        if isinstance(v, (int, float)):
            dt = datetime.fromtimestamp(float(v))
        else:
            dt = datetime.fromisoformat(str(v))
            if dt.tzinfo:
                dt = dt.astimezone().replace(tzinfo=None)
        return dt.strftime("%m-%d %H:%M")
    except (ValueError, TypeError, OSError):
        return "–"


def _load_names() -> dict:
    """종목코드→이름 매핑. 여러 로컬 소스를 병합(뒤에 올수록 우선).

    소스: base_universe → ranking_cache → value_watchlist → stock_info_cache → universe.yaml
    롤링 유니버스 밖 도셔/보유 종목도 코드로만 뜨지 않게 한다.
    """
    m: dict[str, str] = {}

    def _put(sym, name, *, overwrite: bool = False) -> None:
        s = str(sym or "").strip()
        n = str(name or "").strip()
        if not s or not n or n == s:
            return
        if overwrite or s not in m:
            m[s] = n

    for fn in ("base_universe_KR.txt", "base_universe_US.txt"):
        try:
            for line in (ROOT / "data" / fn).read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split(",", 1)
                if len(parts) == 2:
                    _put(parts[0], parts[1])
        except OSError:
            pass

    # 장중 랭킹 캐시(시총/거래대금 상위 — 이름 포함)
    try:
        rc = json.loads((ROOT / "data" / "ranking_cache.json").read_text(encoding="utf-8")) or {}
        for blk in rc.values() if isinstance(rc, dict) else []:
            rows = (blk or {}).get("rows") if isinstance(blk, dict) else None
            for r in rows or []:
                if isinstance(r, dict):
                    _put(r.get("symbol"), r.get("name"))
    except (OSError, ValueError, TypeError):
        pass

    # 밸류 워치리스트(저평가 지도 — 유니버스 밖 종목명)
    try:
        wl = json.loads((ROOT / "data" / "value_watchlist.json").read_text(encoding="utf-8")) or {}
        for sym, v in wl.items() if isinstance(wl, dict) else []:
            if isinstance(v, dict):
                _put(sym, v.get("name"))
    except (OSError, ValueError, TypeError):
        pass

    # 토스 종목정보 캐시
    try:
        info = json.loads((ROOT / "data" / "stock_info_cache.json").read_text(encoding="utf-8")) or {}
        for sym, v in info.items() if isinstance(info, dict) else []:
            row = (v or {}).get("info") if isinstance(v, dict) else None
            if isinstance(row, dict):
                _put(sym, row.get("name"), overwrite=True)
    except (OSError, ValueError, TypeError):
        pass

    # 활성 유니버스(스크리너 최신 — 최우선)
    try:
        import yaml
        u = yaml.safe_load((ROOT / "data" / "universe.yaml").read_text(encoding="utf-8")) or {}
        for lst in u.values():
            for it in (lst or []):
                if isinstance(it, dict) and it.get("symbol"):
                    _put(it["symbol"], it.get("name"), overwrite=True)
    except Exception:  # yaml 없거나 파싱 실패 → 위 소스만으로 진행
        pass
    return m


def _name(sym: str, names: dict) -> str:
    """심볼→종목명만. 이름을 못 찾으면 코드(폴백)."""
    sym = str(sym or "")
    nm = names.get(sym) or sym
    return escape(str(nm))


def _pos_name(x: dict, names: dict) -> str:
    return _name(x.get("symbol", ""), names)


_STANCE_BADGE = {"bullish": ("b-bull", "강세"), "bearish": ("b-bear", "약세"),
                 "neutral": ("b-neut", "중립")}


def _stance_badge(stance: str) -> str:
    cls, lab = _STANCE_BADGE.get(str(stance or "").lower(), ("b-neut", stance or "?"))
    return f"<span class={cls}>{escape(str(lab))}</span>"


_SESSION_LABEL = {"premarket": "프리마켓", "regular": "정규장", "aftermarket": "애프터마켓",
                  "daymarket": "데이마켓", "closed": "휴장"}


def _session_badge(sess: str) -> str:
    """세션 배지: 운영중이면 강조(초록 b-bull), 휴장이면 흐리게(회색 b-neut)."""
    s = str(sess or "closed").lower()
    lab = _SESSION_LABEL.get(s, s or "?")
    cls = "b-neut" if s == "closed" else "b-bull"
    return f"<span class={cls}>{escape(lab)}</span>"


def _trail_badge(meta: dict) -> str:
    """트레일링 활성 포지션이면 '▲트레일' 배지(초록 b-bull), 아니면 빈 문자열.

    meta.trail_active 는 감시 루프가 목표가 돌파 시 세운다 — 이 포지션은 목표가 청산 대신
    이익을 잠근 트레일링 스톱(stop_price)으로 관리 중이라는 표식. peak 은 tooltip 으로.
    """
    if not (isinstance(meta, dict) and meta.get("trail_active")):
        return ""
    peak = meta.get("trail_peak")
    tip = f" title='트레일 최고가 {peak}'" if peak is not None else ""
    return f" <span class=b-bull{tip}>&#9650;트레일</span>"


def _money(v, market: str = "KR") -> str:
    """금액 표시: KR 은 ₩정수, US 는 $소수2. None/파싱실패면 –."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "–"
    return f"${v:,.2f}" if market == "US" else f"₩{v:,.0f}"


def _open_ledger(d: dict) -> dict:
    """봇 원장(store positions)의 보유분을 {symbol: row} 로. 실계좌 표에 전략·손절·목표를
    얹기 위한 조인 테이블이다(원장에만 있는 종목 검출에도 쓴다)."""
    out = {}
    for x in (d.get("positions") or []):
        if x.get("state") == "open" and x.get("symbol"):
            out[str(x["symbol"])] = x
    return out


def _plan_cells(row: dict | None, dp: int) -> str:
    """손절/목표 = '코드가 이 포지션을 어떻게 지키는가'.

    손절이 비어 있으면 **가격 기반 자동 청산이 안 걸린 상태**다(계좌 동기화로 채택된 고아
    보유 등). 조용한 '–' 로 두면 안 보이므로 경고색으로 드러낸다.
    """
    if not row:
        return "<td class=freshwarn>원장 없음</td><td class=muted>–</td>"
    trail = _trail_badge(_safe_json(row.get("meta")))
    stop = row.get("stop_price")
    stop_s = (f"<span class=mono>{_fmt(stop, dp)}</span>{trail}" if stop
              else "<span class=freshwarn title='가격 기반 자동 손절 미설정'>미설정</span>")
    return f"<td>{stop_s}</td><td class=mono>{_fmt(row.get('target_price'), dp)}</td>"


def _strategy_label(row: dict | None) -> str:
    """종목명 아래 작은 줄 — 봇이 이걸 어떤 전략으로 들고 있는지."""
    if not row:
        return "<div class=freshwarn style='font-size:11px'>봇 원장에 없음</div>"
    strat = row.get("strategy")
    if strat:
        return f"<div class=muted style='font-size:11px'>{escape(str(strat))}</div>"
    # 원장엔 있지만 전략 미배정 = 계좌 동기화로 채택된 보유. 뇌는 재평가하지만 코드 청산은 없다.
    return "<div class=muted style='font-size:11px'>전략 미배정 (동기화 채택)</div>"


def _snap_name(it: dict, names: dict) -> str:
    """스냅샷 종목명: 응답에 name 이 있으면 그걸(코드 tail 병기), 없으면 universe 매핑."""
    if it.get("name"):
        sym = str(it.get("symbol") or "")
        tail = (f" <span class='muted mono' style='font-size:11px'>{escape(sym)}</span>"
                if sym else "")
        return f"{escape(str(it['name']))}{tail}"
    return _name(it.get("symbol"), names)


# ───────────────────────── 자산 관제 패널(탭 위) ─────────────────────────

def _bench_chart_html(d: dict) -> str:
    """메인 상단: Argus vs 코스피 누적 수익률 차트."""
    s = d.get("bench_chart")
    if not s:
        return ("<div class=bench-chart><div class=bc-hd>"
                "<span class=bc-ttl>수익률 · Argus vs 코스피</span></div>"
                "<span class=muted>차트 데이터 준비중(체결·지수 히스토리 부족).</span></div>")
    svg = _ret_chart_svg(s)
    if not svg:
        return ""
    pn, bn, an = s["port_now"], s["bench_now"], s["alpha_now"]
    pc = "pos" if pn >= 0 else "neg"
    bc = "pos" if bn >= 0 else "neg"
    ac = "pos" if an >= 0 else "neg"
    bname_raw = str(s.get("bench_name") or "코스피")
    bname = escape(bname_raw)
    pts = _bench_chart_points(s)
    tip_data = json.dumps({"bench": bname_raw, "points": pts}, ensure_ascii=False)
    return (
        "<div class=bench-chart>"
        "<div class=bc-hd>"
        f"<span class=bc-ttl>수익률 · Argus vs {bname}</span>"
        f"<span class=muted>첫 체결 {escape(s.get('since') or '')}~ · 평가 기준</span>"
        "<span class=bc-leg>"
        "<span><i style='background:#5aa9ff'></i>Argus</span>"
        f"<span><i style='background:#ffb454'></i>{bname}</span>"
        "</span></div>"
        "<div class=bc-wrap>"
        f"<script type='application/json' class=bc-data>{tip_data}</script>"
        "<div class=bc-tip hidden></div>"
        f"{svg}"
        "</div>"
        "<div class=bc-stats>"
        f"<span><span class=k>Argus</span><span class='mono {pc}'>{pn:+.2f}%</span></span>"
        f"<span><span class=k>{bname}</span><span class='mono {bc}'>{bn:+.2f}%</span></span>"
        f"<span><span class=k>알파</span><span class='mono {ac}'><b>{an:+.2f}%p</b></span></span>"
        "</div></div>"
    )


def _asset_html(d: dict) -> str:
    """실계좌 자산 종합/보유/오늘거래 — 탭 위 상단 관제 패널. 스냅샷 없으면 대기 안내."""
    snap = d.get("snapshot")
    names = d.get("names", {})
    now = d["now"]
    badge = ("<span class=b-live>LIVE 실계좌</span>" if d.get("live_mode")
             else "<span class=b-paper>PAPER</span>")
    p = ['<div class=asset><div class=hd><span class=ttl>&#128176; 실계좌 자산 관제</span>', badge]
    if not snap:
        p.append("<span class=fresh>스냅샷 대기중…</span></div>"
                 "<div class=muted style='margin-top:4px'>아직 실계좌 스냅샷이 없습니다. "
                 "데몬이 조회하면 표시됩니다.</div></div>")
        # 스냅샷이 없어도 보유는 보여야 한다 — 봇 원장으로 폴백(평가금액·손익은 스냅샷 전용).
        ledger = _open_ledger(d)
        p.append("<div class=sec>보유 종목 (봇 원장 — 실계좌 스냅샷 대기중)</div><div class=panel>")
        if ledger:
            p.append("<table><tr><th>종목명</th><th>시장</th><th>수량</th><th>평단</th>"
                     "<th>손절</th><th>목표</th></tr>")
            for x in ledger.values():
                mk = str(x.get("market") or "KR")
                dp = 0 if mk == "KR" else 2
                p.append(f"<tr><td>{_pos_name(x, names)}{_strategy_label(x)}</td>"
                         f"<td>{escape(mk)}</td>"
                         f"<td class=mono>{_fmt(x.get('qty'),0)}</td>"
                         f"<td class=mono>{_fmt(x.get('avg_price'),dp)}</td>"
                         f"{_plan_cells(x, dp)}</tr>")
            p.append("</table>")
        else:
            p.append("<span class=muted>보유 없음.</span>")
        p.append("</div>")
        p.append(_bench_chart_html(d))
        return "".join(p)
    # 신선도
    ts = snap.get("ts")
    age = (now - float(ts)) if ts else None
    if age is None:
        fresh, fcls = "갱신시각 미상", "freshwarn"
    else:
        mins = age / 60
        fresh = f"{mins:.0f}분 전 갱신" if mins >= 1 else "방금 갱신"
        fcls = "freshwarn" if age > SNAP_STALE_SEC else ""
    p.append(f"<span class='fresh {fcls}'>{escape(fresh)}</span></div>")

    # KR 종합: 총자산=현금+평가, 투입원금(원가)=현금+매입원가, 평가손익=총자산-투입원금
    cash = snap.get("cash") or {}
    mv = snap.get("market_value") or {}
    tp = snap.get("total_purchase") or {}
    cash_kr = float(cash.get("KR", 0) or 0)
    mv_kr = float(mv.get("KR", 0) or 0)
    tp_kr = float(tp.get("KR", 0) or 0)
    total = cash_kr + mv_kr
    principal = cash_kr + tp_kr
    pnl = (snap.get("profit") or {}).get("KR")
    pr = (snap.get("profit_rate") or {}).get("KR")
    dpnl = (snap.get("daily_profit") or {}).get("KR")
    dpr = (snap.get("daily_profit_rate") or {}).get("KR")
    pcls = "pos" if (pnl or 0) >= 0 else "neg"
    dcls = "pos" if (dpnl or 0) >= 0 else "neg"
    pnl_disp = f"₩{pnl:+,.0f}" if pnl is not None else "–"
    dpnl_disp = f"₩{dpnl:+,.0f}" if dpnl is not None else "–"
    pr_s = f" <small>({pr*100:+.2f}%)</small>" if pr is not None else ""
    dpr_s = f" <small>({dpr*100:+.2f}%)</small>" if dpr is not None else ""
    p.append("<div class=asset-grid>")
    p.append(f"<div><div class=k>총자산</div><div class=big>₩{total:,.0f}</div>"
             f"<div class='sub2 muted'>현금 ₩{cash_kr:,.0f} + 평가 ₩{mv_kr:,.0f}</div></div>")
    p.append(f"<div><div class=k>투입원금 <small>원가</small></div><div class=big>₩{principal:,.0f}</div>"
             f"<div class='sub2 muted'>현금 ₩{cash_kr:,.0f} + 매입 ₩{tp_kr:,.0f}</div></div>")
    p.append(f"<div><div class=k>평가손익</div><div class='big {pcls}'>{pnl_disp}{pr_s}</div></div>")
    p.append(f"<div><div class=k>일손익</div><div class='big {dcls}'>{dpnl_disp}{dpr_s}</div></div>")
    p.append("</div>")  # asset-grid
    # US 잔고(있으면, 환산 미포함 참고선)
    us_cash = cash.get("US")
    us_mv = mv.get("US")
    if us_cash or us_mv:
        p.append(f"<div class='sub2 muted' style='margin-top:8px'>US: 현금 "
                 f"${float(us_cash or 0):,.2f} · 평가 ${float(us_mv or 0):,.2f} (₩환산 미포함)</div>")

    # Argus vs 코스피 누적 수익률
    p.append(_bench_chart_html(d))
    p.append("</div>")  # .asset

    # 보유 종목 — 실계좌 스냅샷(수량·평단·평가)에 봇 원장의 운용계획(전략·손절·목표)을 조인.
    #   예전엔 '오늘' 탭에 원장 기반 표가 따로 있었는데, 같은 보유를 두 번 보여주면서 정작
    #   운용계획은 아래에만 있었다 → 한 표로 합친다. 두 출처가 어긋나는 경우만 아래에 남긴다.
    items = snap.get("items") or []
    ledger = _open_ledger(d)
    p.append("<div class=sec>보유 종목 (실계좌 + 봇 운용계획)</div><div class=panel>")
    if items:
        p.append("<table><tr><th>종목명</th><th>시장</th><th>수량</th><th>평단</th>"
                 "<th>현재가</th><th>평가금액</th><th>평가손익</th><th>손절</th><th>목표</th></tr>")
        for it in items:
            mk = str(it.get("market") or "KR")
            dp = 0 if mk == "KR" else 2
            rate = it.get("pnl_rate")
            rcls = "pos" if (rate or 0) >= 0 else "neg"
            rate_s = f"{rate*100:+.2f}%" if rate is not None else "–"
            pnl_v = it.get("pnl")
            pnl_v_s = _money(pnl_v, mk) if pnl_v is not None else "–"
            row = ledger.pop(str(it.get("symbol") or ""), None)
            p.append(f"<tr><td>{_snap_name(it, names)}{_strategy_label(row)}</td>"
                     f"<td>{escape(mk)}</td>"
                     f"<td class=mono>{_fmt(it.get('qty'),0)}</td>"
                     f"<td class=mono>{_fmt(it.get('avg'),dp)}</td>"
                     f"<td class=mono>{_fmt(it.get('last'),dp)}</td>"
                     f"<td class=mono>{_money(it.get('value'), mk)}</td>"
                     f"<td class='mono {rcls}'>{pnl_v_s} ({rate_s})</td>"
                     f"{_plan_cells(row, dp)}</tr>")
        p.append("</table>")
    else:
        p.append("<span class=muted>보유 없음.</span>")
    # 원장엔 열려 있는데 실계좌 스냅샷엔 없는 종목 — 조용히 숨기면 안 된다(스냅샷 지연이거나
    # 유령 포지션이고, 후자면 주기 재대사가 정리한다). 어긋남 자체가 신호이므로 드러낸다.
    if ledger:
        p.append("<div class=freshwarn style='margin-top:10px;font-size:12px'>"
                 "&#9888; 봇 원장에만 있는 보유(실계좌 스냅샷 미반영): "
                 + ", ".join(f"{_pos_name(x, names)}"
                             f"<span class=muted> x{_fmt(x.get('qty'),0)}</span>"
                             for x in ledger.values())
                 + " <span class=muted>— 스냅샷 지연이거나 유령 포지션(재대사가 정리).</span></div>")
    p.append("</div>")

    # 오늘 봇 거래(라이브)
    trades = d.get("live_trades") or []
    p.append("<div class=sec>오늘 봇 거래 (라이브)</div><div class=panel>")
    if trades:
        p.append("<table><tr><th>시각</th><th>구분</th><th>종목명</th><th>내용</th></tr>")
        for e in trades:
            pl = _safe_json(e.get("payload"))
            kind = e.get("kind")
            if kind == "live_order":
                chip = "<span class=fill>체결</span>"
                detail = (f"{escape(str(pl.get('side') or ''))} x{_fmt(pl.get('qty'),0)} "
                          f"@ {_fmt(pl.get('price'),0)} · "
                          f"id={escape(str(pl.get('order_id') or '–'))}")
            elif kind == "live_order_error":
                chip = "<span class=veto>전송실패</span>"
                detail = escape(str(pl.get("error") or "")[:80])
            else:  # buy_blocked
                chip = "<span class=r-queue>매수차단</span>"
                detail = escape(str(pl.get("reason") or "")[:80])
            nm = _name(e.get("symbol"), names) if e.get("symbol") else "–"
            p.append(f"<tr><td class=mono>{_hms(e['ts'])}</td><td>{chip}</td>"
                     f"<td>{nm}</td>"
                     f"<td class='muted mono' style='font-size:12px'>{detail}</td></tr>")
        p.append("</table>")
    else:
        p.append("<span class=muted>오늘 라이브 거래 없음.</span>")
    p.append("</div>")
    return "".join(p)


def _brain_health_card(d: dict, prefix: str = "brain", label: str = "뇌 연속실패") -> str:
    """뇌 사이클 연속 실패 + 가용성 모드 카드.

    하트비트(루프 생존)만으로는 '루프는 도는데 판단은 100% 실패'(세션 한도 등)를 못 본다.
    mode=ok|bridge|circuit_open|auth_needed 를 함께 표시한다.
    """
    bh = (d.get("brain_health") or {}).get(prefix) or {}
    cf = int(bh.get("consecutive_failures") or 0)
    mode = str(bh.get("mode") or d.get("brain_mode") or "ok")
    mode_col = {
        "ok": "#3ddc84", "bridge": "#ffb454",
        "circuit_open": "#ff5c63", "auth_needed": "#ff5c63",
    }.get(mode, "#9aa4b2")
    col = "#ff5c63" if cf >= 3 else ("#ffb454" if cf >= 1 else "#3ddc84")
    lok = bh.get("last_ok_ts") or 0
    sub = f"{mode} · 성공 {_hms(lok)}" if lok else f"{mode} · 성공 기록 없음"
    err = escape(str(bh.get("last_error") or ""))[:80]
    tip = f' title="{err}"' if err else ""
    return (f"<div class=card{tip}><div class=k>{label}</div>"
            f"<div class='v mono' style='color:{col}'>{cf}</div>"
            f"<small class=muted style='color:{mode_col}'>{escape(sub)}</small></div>")


# ───────────────────────── 시장 심리(공포·탐욕) ─────────────────────────

# rating → (뱃지 클래스, 한글, 색). 색은 게이지 그라디언트 구간색과 맞춘다.
_FG_RATING = {
    "extreme_fear": ("fg-ef", "극단적 공포", "#ff5c63"),
    "fear": ("fg-f", "공포", "#ff8c4b"),
    "neutral": ("fg-n", "중립", "#9aa4b2"),
    "greed": ("fg-g", "탐욕", "#7bd88f"),
    "extreme_greed": ("fg-eg", "극단적 탐욕", "#3ddc84"),
}

# 성분 코드 → 한글. 앞 7개는 CNN(US), 뒤 3개는 KR 대리지표.
_FG_COMP_KO = {
    "market_momentum_sp500": "모멘텀", "stock_price_strength": "주가강도",
    "stock_price_breadth": "브레드스", "put_call_options": "풋콜비율",
    "market_volatility_vix": "변동성", "junk_bond_demand": "정크본드",
    "safe_haven_demand": "안전자산선호",
    "breadth": "브레드스", "drawdown": "지수낙폭", "ret_5d": "5일수익률",
}


def _fnum(v):
    """수치 캐스팅 — 못 하면 None(0 으로 뭉개지 않는다: 게이지 위치가 거짓말한다)."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _fg_rating(score) -> str:
    """점수 → 등급 키. src.datasources.fear_greed.rating_of 와 같은 경계.

    (대시보드는 무거운 임포트 없이 도는 읽기전용 프로세스라 경계표만 로컬에 둔다.)
    """
    s = _fnum(score)
    if s is None:
        return "neutral"
    if s < 25:
        return "extreme_fear"
    if s < 45:
        return "fear"
    if s < 55:
        return "neutral"
    if s < 75:
        return "greed"
    return "extreme_greed"


def _sparkline(points, color: str, w: int = 240, h: int = 44) -> str:
    """0~100 고정 스케일 스파크라인(인라인 SVG, 라이브러리 없음).

    공포지수는 절대 스케일이라 자동 y스케일은 오해를 부른다 — 늘 0~100 으로 그리고
    중립선 50 과 극단 구간(0~25 / 75~100)을 옅게 깔아 위치를 읽히게 한다.
    포인트가 2개 미만이면 빈 문자열.
    """
    try:
        vals = []
        for pt in (points or []):
            if not isinstance(pt, (list, tuple)) or len(pt) < 2:
                continue
            v = _fnum(pt[1])
            if v is not None:
                vals.append(max(0.0, min(100.0, v)))
        if len(vals) < 2:
            return ""
        pad = 2.0
        span = h - pad * 2
        n = len(vals)

        def _y(v):
            return pad + (100.0 - v) / 100.0 * span

        xs = [i * (w - 1) / (n - 1) for i in range(n)]
        poly = " ".join(f"{x:.1f},{_y(v):.1f}" for x, v in zip(xs, vals))
        q = span * 0.25
        return (f"<svg viewBox='0 0 {w} {h}' width={w} height={h} style='display:block'>"
                f"<rect x=0 y='{pad:.1f}' width={w} height='{q:.1f}' fill='#3ddc84' opacity='.05'/>"
                f"<rect x=0 y='{_y(25.0):.1f}' width={w} height='{q:.1f}' fill='#ff5c63' opacity='.05'/>"
                f"<line x1=0 y1='{_y(50.0):.1f}' x2={w} y2='{_y(50.0):.1f}' "
                f"stroke='#2a3344' stroke-dasharray='3 3'/>"
                f"<polyline fill=none stroke='{color}' stroke-width='1.8' "
                f"stroke-linejoin=round points='{poly}'/>"
                f"<circle cx='{xs[-1]:.1f}' cy='{_y(vals[-1]):.1f}' r='2.6' fill='{color}'/>"
                f"</svg>")
    except Exception:
        return ""


def _fg_comps_html(comps) -> str:
    """성분 미니바들 — 성분마다 '자기 점수의 등급색'으로 칠한다.

    ("정크본드는 탐욕인데 브레드스는 극공포"가 한눈에 보이게 — 이게 성분을 보는 이유다.)
    """
    if not isinstance(comps, dict) or not comps:
        return ""
    out = []
    for k, v in comps.items():
        s = _fnum(v)
        if s is None:
            continue
        col = _FG_RATING[_fg_rating(s)][2]
        nm = _FG_COMP_KO.get(str(k), str(k))
        pct = max(0.0, min(100.0, s))
        out.append(f"<span class=fg-c><span>{escape(nm)}</span>"
                   f"<span class=fg-cb><i style='width:{pct:.0f}%;background:{col}'></i></span>"
                   f"<span class=fg-cv>{_fmt(s,1)}</span></span>")
    # "지수낙폭 0.0" 을 '낙폭이 없다'로 오독하지 않게 — 이 숫자들은 전부 0~100 성분 점수다
    # (0=최악의 공포). 원재료 수치는 옆의 fg-note 에 따로 적힌다.
    return (f"<div class=fg-comps><span class=fg-clab>성분점수</span>{''.join(out)}</div>"
            if out else "")


def _fg_us_note(info: dict) -> str:
    """US 부가정보: 과거 비교값(있는 것만) + 갱신 실패 표시."""
    bits = []
    for key, lab in (("prev_1y", "1년"), ("prev_1m", "1개월"),
                     ("prev_1w", "1주"), ("prev_close", "전일")):
        v = _fnum(info.get(key))
        if v is not None:
            bits.append(f"{lab} {_fmt(v,1)}")
    txt = " · ".join(bits)
    if info.get("stale"):
        txt += " <span class=freshwarn>(갱신 실패 — 마지막 값)</span>"
    return txt.strip()


def _fg_kr_note(info: dict) -> str:
    """KR 부가정보: 등급 기준·원재료·성분 결측."""
    inp = info.get("inputs")
    inp = inp if isinstance(inp, dict) else {}
    bits = []
    if info.get("incomplete"):
        bits.append("<span class=freshwarn>불완전</span>")
    basis = str(info.get("rating_basis") or "")
    pct = _fnum(info.get("score_pct"))
    if basis == "percentile" and pct is not None:
        bits.append(f"이력대비 {pct:.0f}백분위 (50=평년)")
    elif basis == "absolute":
        bits.append("등급=합성 원점수 (이력 부족)")
    dd = _fnum(inp.get("index_drawdown_pct"))
    if dd is not None:
        bits.append(f"KOSPI 20일 고점 대비 {dd:+.1f}%")
    r5 = _fnum(inp.get("index_ret_5d_pct"))
    if r5 is not None:
        bits.append(f"5일 {r5:+.1f}%")
    br = _fnum(inp.get("breadth_above_ma20"))
    missing = [str(m) for m in info.get("missing") or []
               if isinstance(info.get("missing"), list)]
    if br is not None:
        n = inp.get("n")
        n_s = f" (n={escape(str(n))})" if n is not None else ""
        bits.append(f"20일선 상회 {br*100:.0f}%{n_s}")
    if br is None:
        bits.append("<span class=muted>브레드스 성분 없음(장전 배치 경로)</span>")
    elif missing:
        ko = {"breadth": "브레드스", "drawdown": "낙폭", "ret_5d": "5일"}
        bits.append("결측 " + "·".join(ko.get(m, m) for m in missing))
    vk = _fnum(inp.get("vkospi"))
    if vk is not None:
        bits.append(f"VKOSPI {vk:.2f} (전일)")
    pcr = _fnum(inp.get("put_call_ratio"))
    if pcr is not None:
        bits.append(f"풋콜 {pcr:.2f}")
    return " · ".join(bits)


def _fg_kr_tail(info: dict) -> str:
    """국내 공포 행 하단 출처 문구."""
    inp = info.get("inputs") if isinstance(info.get("inputs"), dict) else {}
    if _fnum(inp.get("vkospi")) is not None or _fnum(inp.get("put_call_ratio")) is not None:
        return "브레드스·낙폭·5일 합성 + KRX 전일(VKOSPI·풋콜은 점수 외 부가)"
    return "VKOSPI 미제공 → 브레드스·낙폭·5일수익률 합성 대리지표"


def _fear_row(info: dict, series, label: str, code: str, caption: str,
              note: str, tail: str = "") -> str:
    """공포지수 한 행(라벨·점수·등급·게이지·스파크라인·성분·부가정보)."""
    score = _fnum(info.get("score"))
    if score is None:
        return ""
    rk = str(info.get("rating") or "") or _fg_rating(score)
    if rk not in _FG_RATING:
        rk = _fg_rating(score)
    cls, ko, col = _FG_RATING[rk]
    pos = max(0.0, min(100.0, score))
    spark = _sparkline(series, col)
    if spark:
        side = f"<div class=fg-spark>{spark}<div class=fg-cap>{escape(caption)}</div></div>"
    else:
        side = ("<div class=fg-spark><span class=muted style='font-size:11px'>"
                "추이 누적 중</span></div>")
    p = ["<div class=fg-wrap><div class=fg-row><div class=fg-main>"]
    p.append(f"<div class=fg-head><span class=fg-lab>{escape(label)} · {escape(code)}</span>"
             f"<span class=fg-score style='color:{col}'>{_fmt(score,1)}</span>"
             f"<span class={cls}>{escape(ko)}</span></div>")
    p.append(f"<div class=fg-gauge><div class=fg-mark style='left:{pos:.1f}%'></div></div>")
    p.append("<div class=fg-ticks><span>0</span><span>25</span><span>50</span>"
             "<span>75</span><span>100</span></div>")
    comps = _fg_comps_html(info.get("components"))
    if comps or note:
        p.append("<div class=fg-foot>")
        p.append(comps)
        if note:
            p.append(f"<div class=fg-note>{note}</div>")
        p.append("</div>")
    if tail:
        p.append(f"<div class=fg-src>{escape(tail)}</div>")
    p.append(f"</div>{side}</div></div>")
    return "".join(p)


def _fear_html(d: dict) -> str:
    """시장 심리 패널(국내 위 / 미국 아래). 데이터가 하나도 없으면 빈 문자열.

    대시보드는 라이브 운영자의 유일한 관제창이라 이 패널의 버그로 페이지 전체가
    죽으면 안 된다 — 전체를 try 로 감싸 실패 시 조용히 빠진다.
    """
    try:
        sent = d.get("sentiment") or {}
        hist = d.get("fear_history") or {}
        if not isinstance(sent, dict):
            return ""
        if not isinstance(hist, dict):
            hist = {}
        rows = []
        kr = sent.get("fear_kr")
        if isinstance(kr, dict):
            kr_series = hist.get("kr") or []
            n_kr = len(kr_series) if isinstance(kr_series, list) else 0
            rows.append(_fear_row(
                kr, kr_series, "국내", "KR", f"누적 {n_kr}포인트", _fg_kr_note(kr),
                tail=_fg_kr_tail(kr)))
        us = sent.get("fear_greed")
        if isinstance(us, dict):
            rows.append(_fear_row(us, hist.get("us") or [], "미국", "US",
                                  "최근 1년", _fg_us_note(us)))
        rows = [r for r in rows if r]
        if not rows:
            return ""
        return ("<div class=sec>시장 심리 (공포 · 탐욕)</div>"
                f"<div class=panel>{''.join(rows)}</div>")
    except Exception:
        return ""


# ───────────────────────── 탭1: 오늘 ─────────────────────────

def _today_html(d: dict) -> str:
    now = d["now"]
    ksess = d.get("kr_session", "closed")
    ussess = d.get("us_session", "closed")
    market_open = (ksess != "closed") or (ussess != "closed")
    age = d["hb_age"]; hb = d["hb"] or {}
    names = d.get("names", {})
    daemon_txt = "정상" if (age is not None and age <= 300) else "응답없음"
    lbd = d.get("last_brain_done")
    p = ['<div class="tabpage page-today">', "<div class=grid>"]
    p.append(f"<div class=card><div class=k>데몬</div><div class=v>{_status_dot(age, market_open)}{daemon_txt} "
             f"<small>{'%.0fs'%age if age is not None else '–'}</small></div></div>")
    p.append("<div class=card><div class=k>장</div>"
             f"<div class=v style='font-size:14px;line-height:1.9'>"
             f"국내 {_session_badge(ksess)}<br>해외 {_session_badge(ussess)}</div></div>")
    p.append(f"<div class=card><div class=k>틱</div><div class='v mono'>{hb.get('ticks','–')}</div></div>")
    p.append(f"<div class=card><div class=k>마지막 완주</div><div class='v mono'>{_hms(lbd) if lbd else '–'}</div></div>")
    p.append(_brain_health_card(d))
    p.append("</div>")

    # 시장 심리(공포·탐욕) — 데이터 없으면 통째로 생략
    p.append(_fear_html(d))

    # 마지막 브레인 판단
    lc = d.get("last_cycle")
    s = d.get("brain_summary") or {}
    p.append("<div class=sec>마지막 브레인 판단</div><div class=panel>")
    if lc:
        cts, cp = lc
        p.append(f"<div class=sub style='margin-bottom:8px'>{_hms(cts)} · "
                 f"제안 {s.get('executed','?')} / 체결 <span class=fill>{s.get('filled','?')}</span> / "
                 f"거부 <span class=veto>{s.get('vetoed','?')}</span></div>")
        p.append(f"<div class=mv>{escape(str(cp.get('market_view','')))}</div>")
    else:
        p.append("<span class=muted>아직 완주한 사이클 없음.</span>")
    # 최근 판단 5건
    decisions = d.get("decisions") or []
    if decisions:
        p.append("<table style='margin-top:12px'><tr><th>시각</th><th>종목명</th><th>액션</th>"
                 "<th>확신도</th><th>판정</th></tr>")
        for dc in decisions:
            vd = str(dc.get("verdict") or "")
            vcls = {"approved": "vd-approved", "vetoed": "vd-vetoed"}.get(vd, "vd-other")
            conv = dc.get("conviction")
            conv_s = f"{float(conv):.2f}" if conv is not None else "–"
            p.append(f"<tr><td class=mono>{_hms(dc['ts'])}</td><td>{_name(dc.get('symbol'), names)}</td>"
                     f"<td class=mono>{escape(str(dc.get('action') or '–'))}</td>"
                     f"<td class=mono>{conv_s}</td>"
                     f"<td><span class={vcls}>{escape(vd or '–')}</span></td></tr>")
        p.append("</table>")
    p.append("</div>")

    # ※ 보유 포지션 표는 여기 있었으나 상단 '보유 종목(실계좌 + 봇 운용계획)' 으로 합쳤다 —
    #   같은 보유를 두 번 보여주면서 전략·손절·목표만 아래에 있던 구조였다. 진입대기는
    #   보유가 아니라 '아직 안 산 것'이라 성격이 달라 여기 남긴다.
    pos = d.get("positions", [])
    px = d.get("pos_px", {})
    armed = [x for x in pos if x.get("state") == "armed"]

    # 진입대기(armed)
    p.append("<div class=sec>진입대기</div><div class=panel>")
    if armed:
        p.append("<table><tr><th>종목명</th><th>시장</th><th>전략</th><th>등록시각</th><th>도시에</th></tr>")
        for x in armed:
            meta = _safe_json(x.get("meta"))
            has_dos = "✓" if meta.get("dossier_id") else "–"
            dcls = "pos" if meta.get("dossier_id") else "muted"
            p.append(f"<tr><td>{_pos_name(x, names)}</td><td>{escape(str(x.get('market') or '–'))}</td>"
                     f"<td>{escape(str(x.get('strategy') or '–'))}</td>"
                     f"<td class=mono>{_fmt_ts(x.get('opened_at'))}</td>"
                     f"<td class='{dcls}'>{has_dos}</td></tr>")
        p.append("</table>")
    else:
        p.append("<span class=muted>대기 없음.</span>")
    p.append("</div>")

    # 오늘 공시
    disc = d.get("disclosures") or []
    p.append("<div class=sec>오늘 공시</div><div class=panel>")
    if disc:
        p.append("<table><tr><th>시각</th><th>종목명</th><th>공시명</th><th>경로</th></tr>")
        for e in disc:
            pl = _safe_json(e.get("payload"))
            corp = pl.get("corp_name") or ""
            nm = _name(e.get("symbol"), names) if e.get("symbol") else escape(str(corp))
            route = pl.get("route")
            if route == "wake":
                rchip = "<span class=r-wake>각성</span>"
            elif route == "queue":
                rchip = "<span class=r-queue>큐</span>"
            else:
                rchip = "<span class=chip>–</span>"
            p.append(f"<tr><td class=mono>{_hms(e['ts'])}</td><td>{nm}</td>"
                     f"<td>{escape(str(pl.get('report_nm') or '–'))}</td><td>{rchip}</td></tr>")
        p.append("</table>")
    else:
        p.append("<span class=muted>오늘 잡힌 중대 공시 없음.</span>")
    p.append("</div></div>")
    return "".join(p)


# ───────────────────────── 탭2: 리서치 ─────────────────────────

def _flow_chip(flow: dict) -> str:
    cls = escape(str(flow.get("cls") or "fl-mute"))
    lab = escape(str(flow.get("label") or "–"))
    return f"<span class={cls}>{lab}</span>"


def _zone_chip(zone: str | None) -> str:
    if not zone:
        return "<span class=muted>–</span>"
    cls, lab = _ZONE_LABEL.get(zone, ("fl-mute", zone))
    return f"<span class={cls}>{escape(lab)}</span>"


def _rr_label(rr) -> str:
    try:
        v = float(rr)
    except (TypeError, ValueError):
        return "–"
    tip = escape(_RR_HELP)
    return f"<span class=mono title=\"{tip}\">손익비 {v:.2f}×</span>"


def _research_html(d: dict) -> str:
    now = d["now"]
    names = d.get("names", {})
    dossiers = d.get("dossiers") or []
    br = d.get("base_rates") or {}
    px = d.get("pos_px") or {}
    pos_by = {str(x["symbol"]): x for x in (d.get("positions") or [])
              if x.get("symbol") and x.get("state") in ("open", "armed")}
    exec_by = index_latest_exec(d.get("research_cycles") or [])
    buy_by = index_latest_by_symbol(d.get("research_buys") or [])
    # 유니버스 종목수: universe.yaml 심볼 기준 → 없으면 base_rates 종목수
    univ_syms = set()
    try:
        import yaml
        u = yaml.safe_load((ROOT / "data" / "universe.yaml").read_text(encoding="utf-8")) or {}
        for lst in u.values():
            for it in (lst or []):
                if it.get("symbol"):
                    univ_syms.add(it["symbol"])
    except Exception:
        pass
    if not univ_syms:
        univ_syms = set((br.get("symbols") or {}).keys())
    univ_n = len(univ_syms)
    fresh_syms = {x["symbol"] for x in dossiers}
    stance_ct = {"bullish": 0, "neutral": 0, "bearish": 0}
    for x in dossiers:
        st = _dossier_stance(x)
        if st in stance_ct:
            stance_ct[st] += 1
    uncovered = max(0, univ_n - len(fresh_syms & univ_syms)) if univ_n else 0

    p = ['<div class="tabpage page-research">', "<div class=grid>"]
    p.append(f"<div class=card><div class=k>유니버스</div><div class='v mono'>{univ_n}</div></div>")
    p.append(f"<div class=card><div class=k>신선 도시에</div><div class='v mono'>{len(dossiers)}</div></div>")
    p.append(f"<div class=card><div class=k>stance 분포</div><div class=v style='font-size:15px'>"
             f"<span class=pos>{stance_ct['bullish']}</span> / "
             f"<span class=muted>{stance_ct['neutral']}</span> / "
             f"<span class=neg>{stance_ct['bearish']}</span></div></div>")
    p.append(f"<div class=card><div class=k>미커버</div><div class='v mono'>{uncovered}</div></div>")
    p.append("</div>")

    p.append("<div class=sec>도시에 (강세→중립→약세 · 클릭해 가설·흐름)</div><div class=panel>")
    p.append(f"<div class=dos-help>{escape(_RR_HELP)} "
             "행을 열면 가설 전문과, 이 도시에 이후 매수 제안이 어디서 끊겼는지가 보인다.</div>")
    if dossiers:
        p.append("<div class=dos-list>")
        for x in dossiers:
            sym = str(x.get("symbol") or "")
            stance = _dossier_stance(x)
            price = px.get(sym)
            flow = build_dossier_flow(
                x, price=price, position=pos_by.get(sym),
                last_exec=exec_by.get(sym), last_buy=buy_by.get(sym))
            zone = flow.get("zone")
            conv = x.get("conviction")
            conv_s = f"{float(conv):.2f}" if conv is not None else "–"
            age_h = (now - float(x["created_at"])) / 3600 if x.get("created_at") else None
            age_s = f"{age_h:.0f}h" if age_h is not None else "–"
            thesis = (x.get("thesis") or "").strip()
            zone_s = "–"
            if x.get("entry_low") is not None and x.get("entry_high") is not None:
                zone_s = f"{_fmt(x['entry_low'],2)}~{_fmt(x['entry_high'],2)}"
            inval_s = _fmt(x.get("invalidation"), 2) if x.get("invalidation") is not None else "–"
            tgt_s = _fmt(x.get("target"), 2) if x.get("target") is not None else "–"
            px_s = _fmt(price, 2) if price is not None else "–"
            # 펼침 여부는 클라 localStorage 가 복원(서버 open 강제 금지 — 리프레시마다 다시 열림 방지).
            p.append(f'<details class=dos-card data-sym="{escape(sym)}">')
            p.append("<summary>")
            p.append(f"<span class=dos-name>{_name(sym, names)}</span>")
            p.append(_stance_badge(stance))
            p.append(_flow_chip(flow))
            p.append(_zone_chip(zone))
            p.append("<span class=dos-meta>")
            p.append(_rr_label(x.get("rr")))
            p.append(f"<span class=mono>확신 {conv_s}</span>")
            p.append(f"<span class=mono>{age_s}</span>")
            p.append("</span></summary>")
            p.append("<div class=dos-body>")
            p.append("<div class=dos-levels>")
            p.append(f"<div><div class=k>현재가</div><div class=v>{px_s}</div></div>")
            p.append(f"<div><div class=k>진입존</div><div class=v>{zone_s}</div></div>")
            p.append(f"<div><div class=k>무효화(손절)</div><div class=v>{inval_s}</div></div>")
            p.append(f"<div><div class=k>목표</div><div class=v>{tgt_s}</div></div>")
            p.append(f"<div><div class=k title=\"{escape(_RR_HELP)}\">손익비</div>"
                     f"<div class=v>{_rr_label(x.get('rr'))}</div></div>")
            p.append(f"<div><div class=k>시장</div><div class=v>"
                     f"{escape(str(x.get('market') or '–'))}</div></div>")
            p.append("</div>")
            if thesis:
                p.append(f"<div class=dos-thesis>{escape(thesis)}</div>")
            else:
                p.append("<div class='dos-thesis muted'>가설 없음.</div>")
            detail = str(flow.get("detail") or "").strip()
            p.append("<div class=dos-flow>")
            p.append(f"<div>흐름 {_flow_chip(flow)} · 존 {_zone_chip(zone)}</div>")
            if detail:
                # 검증 거부 사유는 길 수 있어 800자까지
                p.append(f"<div class=fl-detail>{escape(detail[:800])}</div>")
            p.append("</div></div></details>")
        p.append("</div>")
    else:
        p.append("<span class=muted>신선한 도시에 없음.</span>")
    p.append("</div>")

    # 활성 베이스레이트
    p.append("<div class=sec>활성 베이스레이트</div><div class=panel>")
    active_rows = []
    for sym, v in (br.get("symbols") or {}).items():
        for name in (v.get("active_now") or []):
            setup = (v.get("setups") or {}).get(name) or {}
            active_rows.append((sym, name, setup))
    if active_rows:
        p.append("<table><tr><th>종목명</th><th>셋업</th><th>10봉 승률</th><th>평균수익</th><th>표본</th></tr>")
        for sym, name, setup in active_rows:
            st10 = (setup.get("stats") or {}).get("10") or {}
            wr = st10.get("win_rate"); ar = st10.get("avg_ret_pct"); nn = st10.get("n")
            wr_s = f"{wr*100:.0f}%" if wr is not None else "–"
            ar_s = f"{ar:+.2f}%" if ar is not None else "–"
            acls = "pos" if (ar or 0) >= 0 else "neg"
            n_s = str(nn) if nn is not None else "–"
            if setup.get("small_sample"):
                n_s += " <span class=warn>&#9888; 표본부족</span>"
            p.append(f"<tr><td>{_name(sym, names)}</td><td>{escape(str(name))}</td>"
                     f"<td class=mono>{wr_s}</td><td class='mono {acls}'>{ar_s}</td>"
                     f"<td class=mono>{n_s}</td></tr>")
        p.append("</table>")
    else:
        p.append("<span class=muted>지금 활성 셋업 없음.</span>")
    p.append("</div>")

    # Athena 실행 로그
    p.append("<div class=sec>Athena 실행 로그</div><div class=panel>")
    runs = d.get("athena_runs") or []
    if runs:
        p.append("<table><tr><th>시각</th><th>시장</th><th>완료</th><th>실패</th><th>데드라인 중단</th></tr>")
        for e in runs:
            pl = _safe_json(e.get("payload"))
            dl = "예" if pl.get("stopped_by_deadline") else "–"
            p.append(f"<tr><td class=mono>{_hms(e['ts'])}</td><td>{escape(str(pl.get('market') or '–'))}</td>"
                     f"<td class=mono>{pl.get('done','–')}</td>"
                     f"<td class=mono>{pl.get('failed','–')}</td><td class=mono>{dl}</td></tr>")
        p.append("</table>")
    else:
        p.append("<span class=muted>Athena 실행 기록 없음.</span>")
    p.append("</div></div>")
    return "".join(p)


# ───────────────────────── 탭3: 성과 ─────────────────────────

def _closed_ret_pct(c: dict) -> float | None:
    """청산 포지션 행별 수익률(%): pnl / (avg_price*qty). 원가<=0 이면 None."""
    try:
        cost = float(c.get("avg_price") or 0) * float(c.get("qty") or 0)
        if cost > 0 and c.get("pnl") is not None:
            return float(c["pnl"]) / cost * 100
    except (TypeError, ValueError):
        pass
    return None


def _perf_html(d: dict) -> str:
    t = d.get("trades")
    names = d.get("names", {})
    p = ['<div class="tabpage page-perf">']
    if not t:
        p.append("<div class=panel><span class=muted>페이퍼 계좌 데이터 없음.</span></div>")
    else:
        n = t["n"]; wr = t["win_rate"]
        wr_s = f"{wr*100:.0f}%" if wr is not None else "–"
        rk = t.get("realized_krw"); rt = t.get("ret_total")
        rkcls = "pos" if (rk or 0) >= 0 else "neg"
        rtcls = "pos" if (rt or 0) >= 0 else "neg"
        # 요약 카드 5개
        p.append("<div class=grid>")
        p.append(f"<div class=card><div class=k>승률</div><div class='v'>{wr_s} "
                 f"<small>{t['wins']}승 {t['losses']}패</small></div></div>")
        p.append(f"<div class=card><div class=k>진입거래</div><div class='v mono'>{t.get('entries',0)}</div></div>")
        p.append(f"<div class=card><div class=k>청산거래</div><div class='v mono'>{n}</div></div>")
        p.append(f"<div class=card><div class=k>실현손익 <small>₩환산</small></div>"
                 f"<div class='v mono {rkcls}'>{_fmt(rk) if rk is not None else '–'}</div></div>")
        p.append(f"<div class=card><div class=k>수익률 <small>원금대비</small></div>"
                 f"<div class='v mono {rtcls}'>{('%+.2f%%'%rt) if rt is not None else '–'}</div></div>")
        p.append("</div>")
        if n < 5:
            p.append(f"<div class=sub style='margin:-6px 0 10px'>&#9888; 표본 {n}건 — 승률·수익률은 아직 표본이 작습니다.</div>")

    # 알파
    alpha = d.get("alpha") or []
    if alpha:
        p.append("<div class=sec>알파 (지수 B&H 대비, 첫 체결일 기준·평가수익률)</div><div class=panel>")
        p.append("<table><tr><th>시장</th><th>시작</th><th>포트폴리오</th><th>벤치마크</th>"
                 "<th>지수 수익률</th><th>알파</th></tr>")
        for a in alpha:
            pr = a["port_ret"]; br = a.get("bench_ret"); al = a.get("alpha")
            prc = "pos" if pr >= 0 else "neg"
            brs = ("%+.2f%%" % br) if br is not None else "–"
            als = ("%+.2f%%p" % al) if al is not None else "–"
            alc = "pos" if (al or 0) >= 0 else "neg"
            p.append(f"<tr><td>{escape(a['market'])}</td><td class=mono>{a['since']}~</td>"
                     f"<td class='mono {prc}'>{pr:+.2f}%</td><td>{escape(a['bench'])}</td>"
                     f"<td class=mono>{brs}</td><td class='mono {alc}'><b>{als}</b></td></tr>")
        p.append("</table></div>")

    # 전략별 성과
    closed = d.get("closed_pos") or []
    p.append("<div class=sec>전략별 성과 (store 청산)</div><div class=panel>")
    if closed:
        agg: dict[str, dict] = {}
        for c in closed:
            strat = c.get("strategy") or "–"
            a = agg.setdefault(strat, {"n": 0, "wins": 0, "total_pnl": 0.0, "rets": []})
            a["n"] += 1
            if (c.get("pnl") or 0) > 0:
                a["wins"] += 1
            a["total_pnl"] += float(c.get("pnl") or 0)
            rp = _closed_ret_pct(c)
            if rp is not None:
                a["rets"].append(rp)
        p.append("<table><tr><th>전략</th><th>거래수</th><th>승률</th><th>실현손익</th><th>평균수익률</th></tr>")
        for strat, a in sorted(agg.items(), key=lambda kv: kv[1]["n"], reverse=True):
            wr = a["wins"] / a["n"] if a["n"] else None
            wr_s = f"{wr*100:.0f}%" if wr is not None else "–"
            tp = a["total_pnl"]; tpcls = "pos" if tp >= 0 else "neg"
            ar = sum(a["rets"]) / len(a["rets"]) if a["rets"] else None
            ar_s = f"{ar:+.2f}%" if ar is not None else "–"
            arcls = "pos" if (ar or 0) >= 0 else "neg"
            p.append(f"<tr><td>{escape(str(strat))}</td><td class=mono>{a['n']}</td>"
                     f"<td class=mono>{wr_s}</td><td class='mono {tpcls}'>{_fmt(tp,0)}</td>"
                     f"<td class='mono {arcls}'>{ar_s}</td></tr>")
        p.append("</table>")
    else:
        p.append("<span class=muted>청산 표본 없음.</span>")
    p.append("</div>")

    # 도시에 A/B
    p.append("<div class=sec>도시에 기반 vs 비도시에</div>")
    if closed:
        buckets = {True: {"n": 0, "wins": 0, "rets": []}, False: {"n": 0, "wins": 0, "rets": []}}
        for c in closed:
            meta = _safe_json(c.get("meta"))
            key = bool(meta.get("dossier_id"))
            b = buckets[key]
            b["n"] += 1
            if (c.get("pnl") or 0) > 0:
                b["wins"] += 1
            rp = _closed_ret_pct(c)
            if rp is not None:
                b["rets"].append(rp)
        p.append("<div class=grid>")
        for key, label in ((True, "도시에 기반"), (False, "비도시에")):
            b = buckets[key]
            wr = b["wins"] / b["n"] if b["n"] else None
            wr_s = f"{wr*100:.0f}%" if wr is not None else "–"
            ar = sum(b["rets"]) / len(b["rets"]) if b["rets"] else None
            ar_s = f"{ar:+.2f}%" if ar is not None else "–"
            arcls = "pos" if (ar or 0) >= 0 else "neg"
            p.append(f"<div class=card><div class=k>{label}</div>"
                     f"<div class='v mono' style='font-size:16px'>{b['n']}건 · 승률 {wr_s}</div>"
                     f"<div class='mono {arcls}' style='margin-top:4px'>평균수익률 {ar_s}</div></div>")
        p.append("</div>")
    else:
        p.append("<div class=panel><span class=muted>청산 표본 없음.</span></div>")

    # 거래별 실현손익
    p.append("<div class=sec>실현손익 (거래별)</div><div class=panel>")
    if t and t["closed"]:
        p.append("<table><tr><th>거래일자</th><th>시장</th><th>종목명</th><th>실현손익</th><th>수익률</th></tr>")
        for c in reversed(t["closed"]):
            cls = "pos" if c["net"] > 0 else "neg"
            mk = c.get("market") or ""
            val = f"${c['net']:,.2f}" if mk == "US" else f"{c['net']:,.0f}"
            rp = ("%+.2f%%" % c["ret_pct"]) if c.get("ret_pct") is not None else "–"
            p.append(f"<tr><td class=mono>{_fmt_ts(c.get('ts'))}</td><td>{escape(mk)}</td>"
                     f"<td>{_pos_name(c, names)}</td>"
                     f"<td class='mono {cls}'>{val}</td><td class='mono {cls}'>{rp}</td></tr>")
        p.append("</table>")
    else:
        p.append("<span class=muted>아직 청산된 거래 없음.</span>")
    p.append("</div></div>")
    return "".join(p)


# ───────────────────────── 탭4: 밸류 ─────────────────────────

def _is_value_pos(x: dict) -> bool:
    """밸류 트랙 포지션인가 — strategy='value' 또는 meta.source='value'."""
    return (x.get("strategy") == "value"
            or _safe_json(x.get("meta")).get("source") == "value")


def _sleeve_base(vcfg: dict, snap: dict | None, m: str) -> float:
    """슬리브 예산의 기준 금액 — RiskGate._exposure_base 와 같은 기준.

    exposure_base='equity' 면 실계좌 스냅샷의 현금+평가(시장별), 산출 실패·0 이하면
    capital 폴백(대시보드는 read-only — 절대 예외를 올리지 않는다).
    """
    cap = float((vcfg.get("capital") or {}).get(m) or 0)
    if str(vcfg.get("exposure_base") or "capital").lower() != "equity":
        return cap
    try:
        s = snap or {}
        eq = (float((s.get("cash") or {}).get(m) or 0)
              + float((s.get("market_value") or {}).get(m) or 0))
    except (TypeError, ValueError, AttributeError):
        return cap
    return eq if eq > 0 else cap


def _value_html(d: dict) -> str:
    now = d["now"]
    names = d.get("names", {})
    vcfg = d.get("value_cfg") or {}
    px = d.get("pos_px", {})
    tsd = int(vcfg.get("time_stop_days") or 0)
    rows = [x for x in (d.get("positions") or [])
            if x.get("state") == "open" and _is_value_pos(x)]
    p = ['<div class="tabpage page-value">']

    # 1) 슬리브 현황(시장별 예산/투자/잔여) — ValueRunner._sleeve 와 **같은 순수 함수**로
    #    계산한다(compute_sleeve). 예산은 뇌 사용량에 반응하는 동적 값이다.
    p.append("<div class=sec>밸류 슬리브 (시장별)</div><div class=grid>")
    sleeve_pct = float(vcfg.get("sleeve_pct") or 0)
    snap = d.get("snapshot")
    open_rows = [x for x in (d.get("positions") or []) if x.get("state") == "open"]
    for m in (vcfg.get("markets") or ["KR", "US"]):
        invested = sum(float(x.get("qty") or 0) * float(x.get("avg_price") or 0)
                       for x in rows if x.get("market") == m)
        brain = sum(float(x.get("qty") or 0) * float(x.get("avg_price") or 0)
                    for x in open_rows
                    if x.get("market") == m and not _is_value_pos(x))
        s = compute_sleeve(sleeve_pct=sleeve_pct,
                           brain_reserve_pct=float(vcfg.get("brain_reserve_pct") or 0),
                           max_gross_exposure=vcfg.get("max_gross_exposure"),
                           base=_sleeve_base(vcfg, snap, m),
                           value_invested=invested, brain_invested=brain)
        budget, room = s["budget"], s["room"]
        used_s = f"{invested / budget * 100:.0f}%" if budget else "–"
        rcls = "neg" if room <= 0 else "pos"
        p.append(f"<div class=card><div class=k>{escape(m)} 슬리브 <small>"
                 f"동적 · 상한 {sleeve_pct*100:.0f}%</small></div>"
                 f"<div class='v mono' style='font-size:17px'>{_money(budget, m)}</div>"
                 f"<div class='mono muted' style='margin-top:4px;font-size:12px'>"
                 f"투자 {_money(invested, m)} ({used_s})</div>"
                 f"<div class='mono muted' style='font-size:12px'>"
                 f"뇌 사용 {_money(s['brain_invested'], m)} "
                 f"<span class=muted>/ 활주로 {_money(s['brain_reserve'], m)}</span></div>"
                 f"<div class='mono {rcls}' style='font-size:12px'>잔여 {_money(room, m)}</div>"
                 f"</div>")
    p.append(f"<div class=card><div class=k>밸류 보유</div>"
             f"<div class='v mono'>{len(rows)}</div>"
             f"<small class=muted>시간손절 {tsd}일" + ("" if tsd else " (비활성)") + "</small></div>")
    p.append("</div>")

    # 2) 밸류 포지션
    p.append("<div class=sec>밸류 포지션</div><div class=panel>")
    if rows:
        p.append("<table><tr><th>종목명</th><th>시장</th><th>수량</th><th>평단</th><th>현재가</th>"
                 "<th>수익률</th><th>적정가 밴드</th><th>목표까지</th><th>보유일</th>"
                 "<th>시간손절</th><th>트랜치</th></tr>")
        for x in rows:
            mk = str(x.get("market") or "KR")
            dp = 0 if mk == "KR" else 2
            meta = _safe_json(x.get("meta"))
            avg = x.get("avg_price")
            cur_px = px.get(x.get("symbol"))
            ret_s, rcls = "–", ""
            try:
                if cur_px is not None and avg:
                    r = (float(cur_px) - float(avg)) / float(avg) * 100
                    ret_s = f"{r:+.2f}%"
                    rcls = "pos" if r >= 0 else "neg"
            except (TypeError, ValueError, ZeroDivisionError):
                pass
            lo, hi = meta.get("fair_low"), meta.get("fair_high")
            band = (f"{_fmt(lo, dp)}~{_fmt(hi, dp)}" if lo is not None or hi is not None
                    else "–")
            # 목표(적정가 하단)까지 남은 여력 — 현재가가 없으면 계산하지 않는다.
            room_s = "–"
            try:
                if lo is not None and cur_px:
                    room_s = f"{(float(lo) / float(cur_px) - 1) * 100:+.1f}%"
            except (TypeError, ValueError, ZeroDivisionError):
                pass
            held_d = None
            if x.get("opened_at"):
                held_d = (now - float(x["opened_at"])) / 86400
            held_s = f"{held_d:.0f}일" if held_d is not None else "–"
            # 시간손절 D-day: 남은 일수. 초과면 강조(neg).
            ts_s, tcls = "–", "muted"
            if tsd > 0 and held_d is not None:
                left = tsd - held_d
                ts_s = f"D-{left:.0f}" if left >= 0 else f"초과 {abs(left):.0f}일"
                tcls = "neg" if left < 0 else "muted"
            tr = meta.get("tranches")
            tr_s = (f"{meta.get('tranche_idx')}/{len(tr)}"
                    if isinstance(tr, list) and tr and meta.get("tranche_idx") else "–")
            trail = _trail_badge(meta)                       # 트레일링 활성 표식(목표까지 칸에)
            p.append(f"<tr><td>{_pos_name(x, names)}</td><td>{escape(mk)}</td>"
                     f"<td class=mono>{_fmt(x.get('qty'),0)}</td>"
                     f"<td class=mono>{_fmt(avg,dp)}</td>"
                     f"<td class=mono>{_fmt(cur_px,dp) if cur_px is not None else '–'}</td>"
                     f"<td class='mono {rcls}'>{ret_s}</td>"
                     f"<td class=mono>{band}</td><td class=mono>{room_s}{trail}</td>"
                     f"<td class=mono>{held_s}</td>"
                     f"<td class='mono {tcls}'>{ts_s}</td>"
                     f"<td class=mono>{escape(tr_s)}</td></tr>")
        p.append("</table>")
    else:
        p.append("<span class=muted>밸류 포지션 없음.</span>")
    p.append("</div>")

    # 3) 저평가 워치리스트 상위
    wl = d.get("value_watchlist") or []
    p.append("<div class=sec>저평가 워치리스트 (확신도 상위)</div><div class=panel>")
    if wl:
        p.append("<table><tr><th>종목명</th><th>시장</th><th>확신도</th>"
                 "<th>적정가 밴드</th><th>스캔</th></tr>")
        for e in wl:
            mk = str(e.get("market") or "KR")
            dp = 0 if mk == "KR" else 2
            lo, hi = _fair_band(e)
            band = (f"{_fmt(lo, dp)}~{_fmt(hi, dp)}" if lo is not None or hi is not None
                    else "–")
            conv = e.get("conviction")
            conv_s = f"{_conv(conv):.2f}" if conv is not None else "–"
            p.append(f"<tr><td>{_name(e.get('symbol'), names)}</td><td>{escape(mk)}</td>"
                     f"<td class=mono>{conv_s}</td><td class=mono>{band}</td>"
                     f"<td class=mono>{_fmt_ts(e.get('ts'))}</td></tr>")
        p.append("</table>")
    else:
        p.append("<span class=muted>저평가 종목 없음(또는 워치리스트 미생성).</span>")
    p.append("</div>")

    # 4) 최근 밸류 판단 이력
    vds = d.get("value_decisions") or []
    p.append("<div class=sec>최근 밸류 판단</div><div class=panel>")
    if vds:
        p.append("<table><tr><th>시각</th><th>종목명</th><th>액션</th><th>판정</th></tr>")
        for r in vds:
            ap = r.get("approved")
            if ap is True:
                vcls, vlab = "vd-approved", "승인"
            elif ap is False:
                vcls, vlab = "vd-vetoed", "거부"
            else:
                vcls, vlab = "vd-other", "–"
            p.append(f"<tr><td class=mono>{_fmt_ts(r.get('ts'))}</td>"
                     f"<td>{_name(r.get('symbol'), names)}</td>"
                     f"<td class=mono>{escape(str(r.get('action') or '–'))}</td>"
                     f"<td><span class={vcls}>{vlab}</span></td></tr>")
        p.append("</table>")
    else:
        p.append("<span class=muted>밸류 판단 기록 없음.</span>")
    p.append("</div></div>")
    return "".join(p)


# ───────────────────────── 탭5: 시스템 ─────────────────────────

def _system_html(d: dict) -> str:
    age = d["hb_age"]; hb = d["hb"] or {}
    pid = d.get("pid")
    alive = d.get("pid_alive")
    dot = '<span class="dot ok"></span>' if alive else '<span class="dot bad"></span>'
    t = d.get("tally", {})
    p = ['<div class="tabpage page-system">', "<div class=grid>"]
    p.append(f"<div class=card><div class=k>워커 프로세스</div><div class=v>{dot}{'살아있음' if alive else '없음'} "
             f"<small>pid {pid if pid else '–'}</small></div></div>")
    p.append(f"<div class=card><div class=k>하트비트</div><div class='v mono'>{'%.0fs'%age if age is not None else '–'}</div></div>")
    p.append(f"<div class=card><div class=k>틱 / 폴</div><div class='v mono'>{hb.get('ticks','–')}<small> / {hb.get('polled','–')}</small></div></div>")
    p.append(f"<div class=card><div class=k>대시보드</div><div class=v>인프로세스 <small>:{PORT}</small></div></div>")
    p.append(_brain_health_card(d))
    p.append(_brain_health_card(d, prefix="value", label="밸류 연속실패"))
    p.append("</div>")
    # 12시간 이벤트 집계
    def g(k): return t.get(k, 0)
    p.append("<div class=sec>최근 12시간 활동</div><div class=grid>")
    for k, lab, col in [("wake", "각성", "#7fd1ff"), ("cycle", "완주", "#5aa9ff"),
                        ("entry", "진입", "#3ddc84"), ("exit", "청산", "#ffb454"),
                        ("arm", "대기등록", "#c9a3ff"), ("disarm", "대기해제", "#9aa4b2"),
                        ("strategy_exit", "전략청산", "#ffb454"),
                        ("dossier", "도시에", "#5aa9ff"), ("disclosure", "공시", "#c9a3ff"),
                        ("error", "에러", "#ff5c63" if g("error") else "#3ddc84")]:
        p.append(f"<div class=card><div class=k>{lab}</div><div class='v mono' style='color:{col}'>{g(k)}</div></div>")
    p.append("</div>")
    # 최근 이벤트 30건
    p.append("<div class=sec>최근 이벤트</div><div class=panel><table>"
             "<tr><th>시각</th><th>종류</th><th>종목</th><th>내용</th></tr>")
    for e in d.get("events", []):
        col = KIND_COLORS.get(e["kind"], "#9aa4b2")
        detail = escape((e.get("payload") or "")[:90])
        p.append(f"<tr><td class=mono>{_hms(e['ts'])}</td>"
                 f"<td style='color:{col};font-weight:600'>{escape(e['kind'])}</td>"
                 f"<td class=mono>{escape(str(e.get('symbol') or ''))}</td>"
                 f"<td class='muted mono' style='font-size:11px'>{detail}</td></tr>")
    p.append("</table></div></div>")
    return "".join(p)


BENCH_CHART_JS = """
<script>
(function(){
  function fmtPct(v){return (v>=0?'+':'')+v.toFixed(2)+'%';}
  function fmtAlpha(v){return (v>=0?'+':'')+v.toFixed(2)+'%p';}
  function cls(v){return v>=0?'pos':'neg';}
  document.querySelectorAll('.bc-wrap').forEach(function(wrap){
    var dataEl=wrap.querySelector('.bc-data');
    if(!dataEl)return;
    var meta,pts,bname;
    try{meta=JSON.parse(dataEl.textContent);}catch(e){return;}
    pts=meta.points||[]; bname=meta.bench||'코스피';
    if(!pts.length)return;
    var svg=wrap.querySelector('svg');
    var overlay=svg.querySelector('.bc-overlay');
    var vline=svg.querySelector('.bc-vline');
    var dotP=svg.querySelector('.bc-dot-port');
    var dotB=svg.querySelector('.bc-dot-bench');
    var tip=wrap.querySelector('.bc-tip');
    if(!overlay||!tip)return;
    function show(i,clientX,clientY){
      var p=pts[i];
      vline.setAttribute('x1',p.x); vline.setAttribute('x2',p.x);
      vline.setAttribute('visibility','visible');
      dotP.setAttribute('cx',p.x); dotP.setAttribute('cy',p.yp);
      dotP.setAttribute('visibility','visible');
      dotB.setAttribute('cx',p.x); dotB.setAttribute('cy',p.yb);
      dotB.setAttribute('visibility','visible');
      tip.innerHTML=
        '<div class=bc-tip-dt>'+p.date+'</div>'+
        '<div class=bc-tip-row><span class=bc-tip-lab>Argus</span>'+
        '<span class="bc-tip-val '+cls(p.port)+'">'+fmtPct(p.port)+'</span></div>'+
        '<div class=bc-tip-row><span class=bc-tip-lab>'+bname+'</span>'+
        '<span class="bc-tip-val '+cls(p.bench)+'">'+fmtPct(p.bench)+'</span></div>'+
        '<div class=bc-tip-row><span class=bc-tip-lab>알파</span>'+
        '<span class="bc-tip-val '+cls(p.alpha)+'"><b>'+fmtAlpha(p.alpha)+'</b></span></div>';
      tip.hidden=false;
      var wr=wrap.getBoundingClientRect();
      var tx=clientX-wr.left+14, ty=clientY-wr.top-72;
      tip.style.left=Math.max(8,Math.min(tx,wrap.clientWidth-tip.offsetWidth-8))+'px';
      tip.style.top=Math.max(8,ty)+'px';
    }
    function hide(){
      vline.setAttribute('visibility','hidden');
      dotP.setAttribute('visibility','hidden');
      dotB.setAttribute('visibility','hidden');
      tip.hidden=true;
    }
    overlay.addEventListener('mousemove',function(ev){
      var sr=svg.getBoundingClientRect();
      var vb=svg.viewBox.baseVal;
      var scale=vb.width/sr.width;
      var x=(ev.clientX-sr.left)*scale;
      var padL=pts[0].x, padR=pts[pts.length-1].x;
      var rel=(x-padL)/(padR-padL||1);
      var i=Math.round(rel*(pts.length-1));
      i=Math.max(0,Math.min(pts.length-1,i));
      show(i,ev.clientX,ev.clientY);
    });
    overlay.addEventListener('mouseleave',hide);
  });
})();
</script>
"""

TAB_JS = """
<script>
(function(){
  var ids=['today','research','perf','value','system'];
  try{
    var saved=localStorage.getItem('argusTab');
    if(saved){var el=document.getElementById('t-'+saved); if(el) el.checked=true;}
  }catch(e){}
  ids.forEach(function(t){
    var el=document.getElementById('t-'+t);
    if(el) el.addEventListener('change',function(){try{localStorage.setItem('argusTab',t);}catch(e){}});
  });
  // 도씨에 카드 펼침 유지(심볼 집합). 서버는 open을 강제하지 않는다.
  var DOS_KEY='argusDosOpen';
  function loadOpen(){
    try{return JSON.parse(localStorage.getItem(DOS_KEY)||'[]');}catch(e){return [];}
  }
  function saveOpen(arr){
    try{localStorage.setItem(DOS_KEY, JSON.stringify(arr));}catch(e){}
  }
  var openSet={};
  loadOpen().forEach(function(s){ if(s) openSet[s]=1; });
  document.querySelectorAll('details.dos-card[data-sym]').forEach(function(d){
    var sym=d.getAttribute('data-sym');
    if(sym && openSet[sym]) d.open=true;
    d.addEventListener('toggle', function(){
      if(!sym) return;
      if(d.open) openSet[sym]=1; else delete openSet[sym];
      saveOpen(Object.keys(openSet));
    });
  });
  // 스크롤 위치 복원(meta-refresh 후)
  try{
    var y=sessionStorage.getItem('argusScroll');
    if(y!=null){ var n=parseInt(y,10); if(!isNaN(n)) requestAnimationFrame(function(){ window.scrollTo(0,n); }); }
  }catch(e){}
  function rememberScroll(){
    try{sessionStorage.setItem('argusScroll', String(window.scrollY||0));}catch(e){}
  }
  window.addEventListener('scroll', rememberScroll, {passive:true});
  window.addEventListener('pagehide', rememberScroll);
})();
</script>
""" + BENCH_CHART_JS


def render(d: dict) -> str:
    if not d.get("db"):
        return "<html><body style='background:#0b0e14;color:#e6e9ef;font-family:sans-serif;padding:40px'>" \
               "bot.db 를 찾을 수 없습니다. 데몬이 한 번 이상 돌아야 생성됩니다.</body></html>"
    now = d["now"]
    parts = ["<!doctype html><html lang=ko><head><meta charset=utf-8>",
             f"<meta http-equiv=refresh content={REFRESH_SEC}>",
             "<meta name=viewport content='width=device-width,initial-scale=1'>",
             "<title>Argus Night Watch</title><style>", CSS, "</style>",
             # 탭 라디오보다 먼저 저장된 탭을 읽어 첫 페인트 깜빡임 줄임
             "<script>(function(){try{var s=localStorage.getItem('argusTab');"
             "if(s)document.documentElement.setAttribute('data-tab',s);}catch(e){}})();</script>",
             "</head><body><div class=wrap>"]
    parts.append(
        f"<h1><span class=eye>&#128065;</span> Argus <span style='color:#8b94a3'>Night Watch</span></h1>"
        f"<div class=sub>{datetime.fromtimestamp(now).strftime('%Y-%m-%d %H:%M:%S')} 기준 · "
        f"{REFRESH_SEC}초마다 자동 갱신 · 페이퍼 모드</div>")
    # 경보 배너(탭 위, 항상 노출)
    a = d.get("alert")
    if a:
        since = a.get("since")
        since_s = _hms(since) if isinstance(since, (int, float)) else "?"
        reasons = "".join(f"<li>{escape(str(x))}</li>" for x in a.get("reasons", []))
        parts.append(f"<div class=alert style='margin-top:14px'>&#9888; <b>경보 발생</b> (since {since_s})<ul>{reasons}</ul></div>")
    # 자산 관제 패널(탭 위, 항상 노출 — 라이브 운영자가 실계좌를 한눈에)
    try:
        parts.append(_asset_html(d))
    except Exception as e:  # 자산 패널 오류가 대시보드를 막지 않게
        parts.append(f"<div class=asset><span class=muted>자산 패널 오류: {escape(str(e))}</span></div>")
    # 탭 — 기본 checked 는 오늘. head 의 data-tab + 직후 스크립트가 저장된 탭으로 덮어쓴다.
    parts.append('<div class=tabs>')
    parts.append('<input type=radio name=tab id=t-today checked>')
    parts.append('<input type=radio name=tab id=t-research>')
    parts.append('<input type=radio name=tab id=t-perf>')
    parts.append('<input type=radio name=tab id=t-value>')
    parts.append('<input type=radio name=tab id=t-system>')
    parts.append("<script>(function(){try{var s=localStorage.getItem('argusTab')||"
                 "document.documentElement.getAttribute('data-tab');"
                 "if(!s)return;var el=document.getElementById('t-'+s);if(el)el.checked=true;}catch(e){}})();</script>")
    parts.append('<nav class=tabbar>'
                 '<label for=t-today>오늘</label>'
                 '<label for=t-research>리서치</label>'
                 '<label for=t-perf>성과</label>'
                 '<label for=t-value>밸류</label>'
                 '<label for=t-system>시스템</label></nav>')
    parts.append(_today_html(d))
    parts.append(_research_html(d))
    parts.append(_perf_html(d))
    parts.append(_value_html(d))
    parts.append(_system_html(d))
    parts.append('</div>')  # .tabs
    parts.append(f"<div class=foot>Argus · 읽기전용 대시보드 · {datetime.fromtimestamp(now).strftime('%H:%M:%S')} 생성</div>")
    parts.append("</div>")
    parts.append(TAB_JS)
    parts.append("</body></html>")
    return "".join(parts)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path in ("/favicon.ico",):
            self.send_response(204); self.end_headers(); return
        try:
            html = render(_gather())
        except Exception as e:  # 대시보드는 절대 죽지 않게
            html = f"<html><body style='background:#0b0e14;color:#ff5c63;font-family:monospace;padding:40px'>" \
                   f"render error: {escape(str(e))}</body></html>"
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # 콘솔/로그 소음 억제
        pass


def start_background(port: int = PORT) -> ThreadingHTTPServer:
    """대시보드 HTTP 서버를 데몬 스레드로 띄우고 서버 객체를 반환한다.

    watch 프로세스에 인프로세스로 병합하기 위한 진입점(별도 pythonw 불필요). 읽기전용
    (bot.db mode=ro)이라 감시 루프의 SQLite 쓰기를 방해하지 않는다. 데몬 스레드라
    호출 프로세스가 끝나면 함께 종료된다.
    """
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, name="dashboard", daemon=True).start()
    return srv


def main() -> int:
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
