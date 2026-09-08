"""밸류 트랙 — value_watchlist(저평가 지도)를 읽는 중장기 진입 판단 루프.

value_scan(src/value_scan.py)이 쌓은 data/value_watchlist.json(저평가 지도)을 읽어
①셀렉터(저평가·신선·미보유) ②결정적 사전 가드(안전마진 실재) ③타이밍 게이트(안정화)로
후보를 좁히고, 밸류 결정 에이전트(LLM)→기존 검증·하드게이트·페이퍼 집행까지 잇는다.

판단은 기존 데이트레/스윙 뇌와 분리(전용 에이전트·프롬프트)하되, 인프라(검증/게이트/
페이퍼 계좌/감시 루프)는 공유한다. 진입한 포지션은 store 에 strategy="value",
meta.source="value" 로 미러링되어 감시 루프의 손절/성과귀속이 읽는다(중장기 슬리브).

V2 로 두 가지가 붙었다: ①분할 매수(tranches — 남은 회차가 있는 보유분을 후보로 남기고
회차 비중으로 target_weight 를 코드가 클램프. 기본 [1.0]=한 번에 전량이라 opt-in)
②시간 손절(time_stop_days — 코드는 청산하지 않고 pipeline._portfolio 가 뇌 컨텍스트에
플래그만 싣는다).
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

import pandas as pd

from .conviction import freeze_value_snap, score_value_buy
from .cycle import run_cycle
from .schemas import DecisionOutput
from .validation_agent import ValidationAgent
from ..config import AppConfig, ROOT
from ..focus import build_focus, attach_krx_fields
from ..lessons import build_symbol_lessons
from ..logging_setup import get_logger
from ..session_policy import market_value_due, value_sessions_from_raw
from ..value_scan import load_watchlist, WATCHLIST

log = get_logger("agents.value_trade")

DATA = ROOT / "data"
_KST = ZoneInfo("Asia/Seoul")


def _load_market_state(path: Path) -> dict:
    """market_state.json 로드. 없거나 깨지면 {} (밸류 사이클은 후보만으로도 진행)."""
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as e:
        log.warning("[value_trade] market_state 로드 실패(시황 생략): %s", e)
    return {}


def _market_block(ms: dict) -> dict:
    """메인 뇌 build_context 와 같은 시황 슬롯 — 지시(국면)와 데이터 불일치 해소."""
    return {
        "regime": ms.get("regime"),
        "sentiment": ms.get("sentiment"),
        "macro": ms.get("macro"),
        "macro_kr": ms.get("macro_kr"),
        "markets": ms.get("markets"),
        "sectors": ms.get("sectors"),
        "fx": ms.get("fx"),
        "flows_market": ms.get("flows_market"),
    }


# ── config ────────────────────────────────────────────────────────
def _parse_tranches(raw_val) -> list[float]:
    """분할 매수 회차별 비중 배분. 기본 [1.0] = 한 번에 전량(V1 동작, 하위호환).

    합이 1.0 이 아니면 경고 후 정규화한다(설정 실수로 슬리브를 넘기거나 남기지 않게).
    파싱 불가·빈 값이면 기본값으로 폴백(=분할 매수 비활성).
    """
    if raw_val is None:
        return [1.0]
    try:
        vals = [float(x) for x in raw_val if float(x) > 0]
    except (TypeError, ValueError):
        log.warning("[value_trade] tranches 파싱 실패 — 기본 [1.0] 사용: %r", raw_val)
        return [1.0]
    if not vals:
        log.warning("[value_trade] tranches 가 비었거나 전부 0 이하 — 기본 [1.0] 사용")
        return [1.0]
    total = sum(vals)
    if abs(total - 1.0) > 1e-9:
        log.warning("[value_trade] tranches 합이 %.4f (≠1.0) — 정규화합니다: %s", total, vals)
        vals = [v / total for v in vals]
    return vals


def value_trade_cfg(cfg: AppConfig) -> dict:
    """config value_trade 블록 + 기본값."""
    raw = (cfg.raw.get("value_trade") or {})
    # review/entry 분리. review_per_run 미지정이면 max_per_run(레거시) 또는 10.
    if "review_per_run" in raw:
        review = int(raw["review_per_run"])
    elif "max_per_run" in raw:
        review = int(raw["max_per_run"])
    else:
        review = 10
    target_new = int(raw.get("new_entries_per_run", 1))
    ceiling = int(raw.get("new_entries_ceiling", max(2, target_new)))
    # max_positions: 숫자=고정(레거시/테스트), 없거나 null/'dynamic'=동적 뇌예약
    if "max_positions" not in raw or raw.get("max_positions") in (None, "dynamic"):
        max_pos = None
    else:
        max_pos = int(raw["max_positions"])
    return {
        "enabled": bool(raw.get("enabled", False)),
        "sleeve_pct": float(raw.get("sleeve_pct", 0.60)),
        "brain_reserve_pct": float(raw.get("brain_reserve_pct", 0.30)),
        "max_per_run": review,  # 게이트 루프 호환 별칭 = review_per_run
        "review_per_run": review,
        "new_entries_per_run": target_new,
        "new_entries_ceiling": max(target_new, ceiling),
        "max_positions": max_pos,
        # 1주 시범매수 — 밸류 기본 off(비중 상한 면제 경로라 쏠림이 크다).
        "allow_min_lot": bool(raw.get("allow_min_lot", False)),
        # 소수점 매수 시장 — 미장 정규장은 소수점이 되므로 고단가주도 정상 비중으로
        # '되는 만큼' 산다. 밸류는 정규장 창(KR 10:00 / US 23:00)에만 돌아 안전하다.
        "fractional_markets": [str(m).upper() for m in
                               (raw.get("fractional_markets") or ["US"])],
        "sort_key": str(raw.get("sort_key", "composite_value")),
        "min_dossier_conviction": float(raw.get("min_dossier_conviction", 0.4)),
        "dossier_ttl_hours": float(raw.get("dossier_ttl_hours", 400)),
        "hard_stop_pct": float(raw.get("hard_stop_pct", 0.20)),
        "time_stop_days": int(raw.get("time_stop_days", 120)),
        "tranches": _parse_tranches(raw.get("tranches")),
        "tranche_min_days": float(raw.get("tranche_min_days", 7)),
        "markets": list(raw.get("markets", ["KR", "US"])),
        "model": raw.get("model", "fable"),
        "timeout": int(raw.get("timeout", 600)),
        "windows": dict(raw.get("windows", {"KR": "10:00", "US": "23:00"})),
        "cooldown_hold_n": int(raw.get("cooldown_hold_n", 3)),
        # "오늘 나보다 나은 후보가 있었다"는 그 종목에 대한 반증이 아니다 — 퇴짜(3회)와
        # 같은 임계를 쓰면 준우승 종목이 순서대로 유배된다.
        "cooldown_cap_bump_n": int(raw.get("cooldown_cap_bump_n", 8)),
        "cooldown_days": float(raw.get("cooldown_days", 5)),
        "cooldown_streak_ttl_days": float(raw.get("cooldown_streak_ttl_days", 5)),
        # 코드 산출 확신도의 결정적 하한. LLM 자가채점 임계(agents.min_conviction)를
        # 대체한다 — 채점자에게 임계값을 알려주면 그 바로 위에 붙는다(관측: BUY 11건
        # 전부 0.61~0.64). score_value_buy 는 base 0.45 라 스케일이 다르다.
        "code_conviction_floor": float(raw.get("code_conviction_floor", 0.35)),
    }


# ── 슬리브 예산(순수 함수 — 러너·대시보드 공유) ─────────────────────
def compute_sleeve(*, sleeve_pct: float, brain_reserve_pct: float,
                   max_gross_exposure: float | None, base: float,
                   value_invested: float, brain_invested: float) -> dict:
    """밸류 슬리브의 동적 예산/투자/잔여(전부 원가 기준 qty×avg_price).

        gross   = max_gross_exposure (None 이면 1.0)
        dynamic = base*gross - max(brain_invested, base*brain_reserve_pct)
        budget  = max(0, min(base*sleeve_pct, dynamic))
        room    = budget - value_invested

    sleeve_pct 는 **절대 상한**(고정 배분이 아니다), brain_reserve_pct 는 뇌(단타/스윙)
    트랙에 항상 남겨둘 활주로다 — 밸류는 자본을 수개월 잠그므로, 뇌가 아직 안 쓴 여유는
    빌려주되 예비금 아래로는 절대 내려가지 않게 한다. 뇌가 예비금보다 많이 쓰고 있으면
    그 실사용액이 그대로 차감돼 밸류 예산이 줄어든다.

    base<=0(예: US capital 0)이면 budget 은 0 이 되어 진입이 차단된다.
    """
    gross = 1.0 if max_gross_exposure is None else float(max_gross_exposure)
    base = float(base)
    brain_reserve = base * float(brain_reserve_pct)
    gross_limit = base * gross
    dynamic = gross_limit - max(float(brain_invested), brain_reserve)
    budget = max(0.0, min(base * float(sleeve_pct), dynamic))
    return {"budget": round(budget, 2),
            "invested": round(float(value_invested), 2),
            "room": round(budget - float(value_invested), 2),
            "base": round(base, 2),
            "brain_invested": round(float(brain_invested), 2),
            "brain_reserve": round(brain_reserve, 2),
            "gross_limit": round(gross_limit, 2)}


# ── (a) 셀렉터 ─────────────────────────────────────────────────────
def _row_val(row, key: str, default=None):
    """store 행(sqlite3.Row/dict)에서 안전하게 값 읽기 — 키 없거나 NULL 이면 default."""
    try:
        v = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if v is None else v


def _row_meta(row) -> dict:
    """store 행의 meta(JSON 문자열) → dict. 결손·파싱 실패는 빈 dict."""
    try:
        raw = _row_val(row, "meta")
        return json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        return {}


def tranche_ready(rows, cfg_v: dict, now: float) -> dict[str, dict]:
    """열린 밸류 포지션 중 '지금 추가 트랜치를 살 수 있는' 종목 → 회차 정보.

    조건 전부 충족: strategy=='value' & meta 에 트랜치 정보 존재 & 남은 회차 있음 &
    직전 트랜치 체결 후 tranche_min_days 경과. meta 에 트랜치 정보가 없는 구 포지션
    (V1 시절 진입분)은 대상이 아니다 — 정보가 없으면 사지 않는 쪽이 안전하다.
    """
    out: dict[str, dict] = {}
    gap = float(cfg_v.get("tranche_min_days", 7)) * 86400
    for r in rows:
        if _row_val(r, "strategy") != "value":
            continue
        meta = _row_meta(r)
        tr, idx, last = meta.get("tranches"), meta.get("tranche_idx"), meta.get("last_tranche_at")
        if not isinstance(tr, list) or not tr or idx is None or last is None:
            continue
        try:
            idx = int(idx)
            last = float(last)
        except (TypeError, ValueError):
            continue
        if idx >= len(tr):                     # 회차 소진
            continue
        if now - last < gap:                   # 최소 간격 미경과
            continue
        out[_row_val(r, "symbol")] = {"idx": idx + 1, "total": len(tr),
                                      "weight": float(tr[idx])}
    return out


def select_candidates(watchlist: dict, store, cfg_v: dict,
                      now: float, *, cooldown_path=None) -> list[dict]:
    """value_watchlist 에서 매매 후보를 고른다(composite_value 내림차순, 없으면 conviction).

    통과 조건: stance=='undervalued' & conviction>=min_dossier_conviction &
    신선(now-ts < dossier_ttl_hours*3600, 기본 400h=지도 수명) & market in markets
    & 미보유. 저평가 판정 자체는 가격이 하단 아래면 며칠 지나도 유효하다.
    스캐너가 보유·undervalued 를 주 1회 재감정하는 것과 별개로, 매매는 이 TTL 까지
    지도를 쓴다. 이미 보유한 추가 트랜치는 신선도 만료로 막지 않는다.
    미보유는 store 의 열린 포지션 + 진입대기(armed)까지 전부 제외(중복 진입 방지).
    예외는 **분할 매수**다 — 남은 트랜치가 있고 최소 간격이 지난 밸류 보유분은 후보로
    남기고 회차 정보를 _tranche 로 실어준다(armed 는 여전히 전량 제외).
    분할이 켜져 있으면(len(tranches)>1) 신규 진입에도 1회차 비중을 실어 첫 매수부터
    쪼갠다 — 1회차를 안 막으면 전량을 먹고 2회차가 비중 게이트에 막혀 분할이 무의미해진다.
    각 후보에 watchlist 항목 전체를 담아 반환(symbol 키 부착).
    """
    held: set[str] = set()
    armed: set[str] = set()
    tranche: dict[str, dict] = {}
    if store is not None:
        rows = store.get_open_positions()
        held = {_row_val(r, "symbol") for r in rows}
        tranche = tranche_ready(rows, cfg_v, now)
        # armed(진입대기)는 재선별하지 않는다 — 추가 트랜치 대상도 아니다.
        try:
            armed = {r["symbol"] for r in store.get_armed()}
        except AttributeError:
            pass
    markets = set(cfg_v["markets"])
    ttl_sec = cfg_v["dossier_ttl_hours"] * 3600
    min_conv = cfg_v["min_dossier_conviction"]
    tranches = cfg_v.get("tranches") or [1.0]
    from ..value_ops import in_cooldown, load_cooldown
    cooldown = load_cooldown(cooldown_path)
    out: list[dict] = []
    for sym, entry in (watchlist or {}).items():
        if not isinstance(entry, dict):
            continue
        if entry.get("stance") != "undervalued":
            continue
        conv = entry.get("conviction")
        if conv is None or float(conv) < min_conv:
            continue
        if entry.get("market") not in markets:
            continue
        if now - float(entry.get("ts", 0)) >= ttl_sec and sym not in tranche:
            continue
        if sym in armed:
            continue
        if sym in held and sym not in tranche:
            continue
        # S3 쿨다운
        if in_cooldown(cooldown, sym, now) and sym not in tranche:
            continue
        cand = {"symbol": sym, **entry}
        if sym in tranche:
            cand["_tranche"] = tranche[sym]
        elif len(tranches) > 1:
            cand["_tranche"] = {"idx": 1, "total": len(tranches),
                                "weight": float(tranches[0])}
        out.append(cand)
    sort_key = cfg_v.get("sort_key") or "composite_value"
    # 폴백은 **리스트 단위**다. composite(0~1)와 conviction(0.4~0.9)은 스케일이 달라
    # 종목별로 섞으면 'Score 없는 종목'이 항상 위로 올라온다(스캔 annotate 실패 직후).
    if sort_key != "conviction" and any(
            c.get("composite_value") is None for c in out):
        missing = [c["symbol"] for c in out if c.get("composite_value") is None]
        log.warning("[value_trade] composite_value 결손 %d종(%s…) — 이번 정렬은 "
                    "conviction 폴백", len(missing), ",".join(missing[:3]))
        sort_key = "conviction"

    def _sk(c):
        if sort_key == "conviction":
            return float(c.get("conviction") or 0)
        return float(c.get("composite_value") or 0)

    out.sort(key=_sk, reverse=True)
    return out


# ── (b) 타이밍 게이트 ──────────────────────────────────────────────
def timing_gate(df: pd.DataFrame, current_price: float) -> dict | None:
    """일봉 df(1y)로 안정화 판정 — '떨어지는 칼' 회피.

    통과 = current_price > SMA20 그리고 ret_20d > 0(20일 수익률 양전환).
    캔들 부족(<60봉)이면 None(탈락). 통과 시 LLM 컨텍스트용 지표 dict, 탈락 시 None.
    """
    if df is None or len(df) < 60 or not current_price or current_price <= 0:
        return None
    close = df["close"].astype(float)
    sma20 = float(close.tail(20).mean())
    prev20 = float(close.iloc[-20])
    prev5 = float(close.iloc[-5])
    if prev20 <= 0 or prev5 <= 0 or sma20 <= 0:
        return None
    ret_20d = current_price / prev20 - 1
    ret_5d = current_price / prev5 - 1
    if not (current_price > sma20 and ret_20d > 0):
        return None
    return {"sma20": round(sma20, 2),
            "ret_20d_pct": round(ret_20d * 100, 1),
            "ret_5d_pct": round(ret_5d * 100, 1)}


# ── (c) 결정적 사전 가드 ───────────────────────────────────────────
def fair_price_low(candidate: dict) -> float | None:
    """스캔 시점가 기준 절대 적정가 하단으로 환산. fair_low_pct 없으면 None(후보 제외)."""
    pct = candidate.get("fair_low_pct")
    metrics = candidate.get("metrics") or {}
    scan_price = metrics.get("price")
    if pct is None or not scan_price:
        return None
    return round(float(scan_price) * (1 + float(pct) / 100.0), 2)


def fair_price_high(candidate: dict) -> float | None:
    pct = candidate.get("fair_high_pct")
    metrics = candidate.get("metrics") or {}
    scan_price = metrics.get("price")
    if pct is None or not scan_price:
        return None
    return round(float(scan_price) * (1 + float(pct) / 100.0), 2)


def passes_margin_guard(candidate: dict, current_price: float) -> bool:
    """안전마진 사전 가드: current_price < 적정가 하단이어야 통과.

    fair_low_pct 결손(→ fair_price_low None)이면 탈락(제외). 현재가가 이미 적정가
    하단에 도달했으면(>=) 안전마진 소멸로 제외.
    """
    fpl = fair_price_low(candidate)
    if fpl is None or not current_price or current_price <= 0:
        return False
    return current_price < fpl


# ── (d) 밸류 결정 에이전트 ─────────────────────────────────────────
VALUE_TRADE_SYSTEM = """\
당신은 자율 투자 시스템의 가치투자 진입 판단자다. 입력의 후보(candidates)들은 이미 밸류
스캔이 '저평가(undervalued)'로 분류하고, 타이밍 게이트(20일선 위·모멘텀 양전환=바닥
안정화)를 통과한 종목들이다. 너의 일은 이 중 실제로 지금 살 만한 것을 골라 BUY 로,
아닌 것을 HOLD 로 판단하는 것이다.

판단 순서:
1. 안전마진 — current_price 대비 적정가 밴드(fair_price_low~fair_price_high)의 상승여력이
   충분한가. 스캔 이후 이미 급등해 현재가가 적정가 하단에 근접·도달했으면 안전마진이
   소멸한 것이니 HOLD 하라.
2. 촉매 — dossier 의 thesis/recent_news 에 저평가 해소 시나리오(업황 반등·구조 개선·
   일회성 악재 소멸 등)가 여전히 살아있는가. 촉매가 사라졌거나 악화됐으면 HOLD.
3. 밸류트랩 재확인 — risks/fundamentals 가 만성 저PBR 지주사·적자·재무 악화 등 구조적
   저평가(밸류트랩) 신호를 가리키면 HOLD. '싸진 것'이 아니라 '싼 이유가 있는 것'을 걸러라.
4. 국면 — context.market_state(regime·sentiment·macro/_kr·markets)와 focus 를 읽어라.
   밸류 매수는 약세 국면 역행이 전제다. 따라서 regime 이 약세라는 이유만으로 HOLD 하지
   마라. 다만 sentiment.fear_greed / fear_kr 이 fear·extreme_fear 이고 급락이 진행 중이면
   진입을 보수적으로 하라. fear_kr.incomplete 이면 그 등급을 확정 국면처럼 쓰지 마라.
   inputs.vkospi·put_call_ratio 는 전일 부가입력이지 score 가중치가 아니다. focus.lenses 가 있으면 그 순서·hint 를 시황 배경으로 깔고
   thesis 에 해당 id·수치를 인용하라(없으면 평소처럼 regime·수급으로).
5. 약세 스틸맨 — 사기로 마음이 기울었으면 반대편을 먼저 세워라. bear_case 에 "이 종목이
   저평가가 아니라 **정당하게 싼 것**이라는 가장 강한 논리"를, bear_rebuttal 에 그 반박을
   써라. 밸류 트랙에서 이건 곧 밸류트랩 반증이다 — fundamentals/risks/recent_news 의 실제
   수치를 인용하고, 일반론("업황이 나쁠 수 있다")은 쓰지 마라. **반박하지 못하면 HOLD 다.**

후보에 past_trades(과거 이 종목 거래 회고)가 있으면 참고하라 — 직전 청산과 같은 셋업의
재진입이면 그때와 무엇이 다른지 thesis 에 명시하라. 과거 손실 자체가 금지 사유는 아니다.

분할 매수(candidates[].tranche — 있으면 그 종목은 신규 진입이 아니다):
- tranche 가 붙은 후보는 **이미 보유 중인 종목의 추가 매수**다(idx/total 이 이번이 몇 회차인지,
  total 이 전체 회차 수). 첫 진입 때 세운 저평가 논리가 지금도 유효한지 다시 검증하고,
  **첫 진입 대비 무엇이 나아졌는지**(촉매 진전·실적 확인) 또는 **안전마진이 더 커졌는지**
  (가격 하락으로 적정가 밴드까지 여력 확대)를 thesis 에 구체적으로 써라. 둘 다 대지 못하면
  그냥 물타기다 — 그럴 땐 HOLD 하라.
- 비중은 신경 쓰지 마라. **매수 수량은 코드가 총자산×기본비중(config)으로 정한다.**
  target_weight 는 스키마 호환용이며 사이징에 쓰이지 않는다. 분할 회차 상한도 코드가
  강제한다. 너는 '지금 더 살 만한가'만 판단하면 된다.

출력 규칙:
- 각 후보에 대해 side 는 BUY 또는 HOLD 만 낸다(SELL 금지 — 청산은 코드/후속이 담당).
- BUY 에는 bear_case·bear_rebuttal 을 반드시 채워라(위 5번). HOLD 는 비워도 된다.
- horizon 은 반드시 "position"(중장기).
- strategy/params 는 지정하지 마라(전략 매매가 아닌 가치투자 진입이다).
- target_weight 는 사이징에 미반영이니 형식만 맞추면 된다(예: 0 또는 {max_position_pct}).
- conviction 은 이 판단에 대한 네 확신을 **솔직하게** 매겨라. 사이징에는 쓰이지 않는다 —
  매수 크기는 코드 루브릭(저평가도·안전마진·공시)이 정한다. 특정 임계값을 겨냥해 점수를
  맞추지 마라. 확신이 부족하면 억지 BUY 대신 HOLD 가 정답이다.
- market_view 에 이번 판단의 밸류 슬리브 상태(예산/잔여)와 시장 국면 요약을 한두 문장으로.

오직 주어진 데이터에 근거해 판단하라. 데이터에 없는 사실을 지어내지 마라."""


class ValueDecisionAgent:
    """밸류 트랙 결정 에이전트 — DecisionAgent 와 같은 구조(structured 호출)."""

    def __init__(self, llm, max_position_pct: float = 0.2,
                 tranche_by_sym: dict[str, dict] | None = None):
        self.llm = llm
        self.max_position_pct = float(max_position_pct)
        # 추가 트랜치 후보의 회차 정보(symbol -> {idx,total,weight}). decide() 후처리에서
        # 비중 상한을 코드가 강제한다 — 분할 매수의 목적은 LLM 재량이 아니라 규칙이다.
        self.tranche_by_sym = tranche_by_sym or {}
        self.system = VALUE_TRADE_SYSTEM.format(
            max_position_pct=round(float(max_position_pct), 3))

    def _clamp_tranche_weights(self, out: DecisionOutput) -> None:
        """추가 트랜치 BUY 의 target_weight 를 회차 비중(max_position_pct×weight)으로 클램프.

        _tranche 가 실린 심볼만 대상 — 신규 진입 제안은 건드리지 않는다(하위호환).
        """
        for p in out.proposals:
            tr = self.tranche_by_sym.get(p.symbol)
            if not tr or p.side != "BUY":
                continue
            cap = self.max_position_pct * float(tr["weight"])
            if p.target_weight > cap:
                log.info("[value_trade][%s] 트랜치 %d/%d 비중 클램프: %.4f → %.4f",
                         p.symbol, tr["idx"], tr["total"], p.target_weight, cap)
                p.target_weight = cap

    def decide(self, context_json: str) -> DecisionOutput:
        out = self.llm.structured(self.system, context_json, DecisionOutput)
        self._clamp_tranche_weights(out)
        log.info("밸류 결정: %s | 제안 %d건", out.market_view[:60], len(out.proposals))
        return out


# ── 기본 주입 함수(데몬 배선에서 gateway 기반으로 교체 가능) ──────────
def _default_fetch_history(sym: str, market: str) -> pd.DataFrame:
    from ..datasources.history import fetch_history
    return fetch_history(sym, interval="1d", range_="1y", market=market,
                         max_age_hours=20)


def _last_close(df: pd.DataFrame) -> float | None:
    if df is None or len(df) == 0:
        return None
    try:
        px = float(df["close"].astype(float).iloc[-1])
    except (KeyError, ValueError, IndexError):
        return None
    return px if px > 0 else None


# ── 상태 파일(시장별 마지막 실행 KST 날짜) ──────────────────────────
def _kst_date(now: float) -> str:
    return datetime.fromtimestamp(now, tz=timezone.utc).astimezone(_KST).strftime("%Y%m%d")


def _load_state(path: Path) -> dict:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError) as e:
        log.warning("[value_trade] state 로드 실패(빈 상태로 진행): %s", e)
    return {}


def _save_state(path: Path, data: dict) -> None:
    """원자적 쓰기(tmp + replace) — 실행 도중 크래시에도 상태가 깨지지 않게."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


# ── (e) 러너 ───────────────────────────────────────────────────────
class ValueRunner:
    """밸류 트랙 1사이클: due 시장별로 셀렉터→가드→게이트→결정→검증→체결→store 미러링.

    전부 주입 가능(테스트 격리). llm_factory/val_llm_factory 는 후보 리스트를 받아 llm 을
    반환한다(pipeline 관례). fetch_history_fn(sym,market)->df, price_fn(symbols,market)->dict
    (없으면 fetch_history 마지막 종가). state_path 미지정 시 data/, journal 은 state 와 같은
    디렉터리(tmp 격리 시 자동 동반). 예외는 시장 단위로 삼켜 다른 시장이 계속 돌게 한다.
    """

    def __init__(self, cfg: AppConfig, store, broker, risk, llm_factory,
                 val_llm_factory, *, fetch_history_fn: Callable | None = None,
                 price_fn: Callable | None = None,
                 watchlist_path: str | Path | None = None,
                 state_path: str | Path | None = None,
                 market_state_path: str | Path | None = None,
                 now_fn: Callable[[], float] = time.time) -> None:
        self.cfg = cfg
        self.store = store
        self.broker = broker
        self.risk = risk
        self.llm_factory = llm_factory
        self.val_llm_factory = val_llm_factory
        self.fetch_history_fn = fetch_history_fn or _default_fetch_history
        self.price_fn = price_fn
        self.watchlist_path = Path(watchlist_path) if watchlist_path else WATCHLIST
        self.state_path = Path(state_path) if state_path else (DATA / "value_trade_state.json")
        # 결정 저널은 state 파일과 같은 디렉터리에 둔다(tmp 격리 시 자동 동반).
        self.journal_path = self.state_path.parent / "value_decisions.jsonl"
        # 쿨다운·깔때기도 state 와 같은 디렉터리 — 모듈 기본 경로를 쓰면 테스트가
        # 라이브 data/ 를 덮어쓴다(측정 데이터 오염).
        self.cooldown_path = self.state_path.parent / "value_trade_cooldown.json"
        self.funnel_path = self.state_path.parent / "value_funnel.jsonl"
        self.market_state_path = (Path(market_state_path) if market_state_path
                                  else DATA / "market_state.json")
        self.now_fn = now_fn
        agents_cfg = cfg.raw.get("agents", {})
        self.min_conviction = float(agents_cfg.get("min_conviction", 0.6))
        self.max_position_pct = float(cfg.risk.get("max_position_pct", 0.25))
        self.base_position_pct = float(cfg.risk.get("base_position_pct", 0.20))
        # 사이징 하한은 RiskGate 속성이 아니라 config 값이다(cycle.py 와 같은 소스).
        self.conviction_size_floor = float(
            cfg.risk.get("conviction_size_floor", 0.75) or 0.75)

    # ── 슬리브(중장기 자본 잠김 분리) ────────────────────────────
    def _value_positions(self) -> list:
        """store 의 열린 포지션 중 meta.source=='value' 인 것들."""
        rows = self.store.get_open_positions() if self.store else []
        out = []
        for r in rows:
            try:
                meta = json.loads(r["meta"]) if r["meta"] else {}
            except (ValueError, TypeError):
                meta = {}
            if meta.get("source") == "value":
                out.append(r)
        return out

    def _market_open_count(self, market: str) -> int:
        """이 시장의 열린 종목 수 — 뇌+밸류 합산(계좌 하드게이트와 같은 기준)."""
        rows = self.store.get_open_positions() if self.store else []
        return len({_row_val(r, "symbol") for r in rows
                    if (_row_val(r, "market") or _row_meta(r).get("market")) == market
                    and _row_val(r, "symbol")})

    def _exposure_base_amount(self, market: str) -> float:
        """슬리브 예산의 기준 금액 — 하드게이트와 **같은** 기준(실자산/고정자본)을 쓴다.

        게이트(broker.gate)가 없거나 산출이 실패하면 config 의 capital 로 폴백한다.
        """
        gate = getattr(self.broker, "gate", None)
        acct = getattr(self.broker, "account", None)
        if gate is not None and acct is not None and hasattr(gate, "exposure_base_amount"):
            try:
                return float(gate.exposure_base_amount(acct, market))
            except Exception as e:
                log.warning("[value_trade][%s] 노출 기준 산출 실패 — capital 폴백: %s",
                            market, e)
        return float(self.cfg.risk.get("capital", {}).get(market, 0) or 0)

    def _sleeve(self, market: str, cfg_v: dict, value_rows: list) -> dict:
        """시장별 밸류 슬리브 — 입력(기준금액·투자액)만 모아 compute_sleeve 에 위임.

        예산은 정적 배분이 아니라 **뇌 트랙의 실사용량에 반응하는 동적 값**이다. 뇌 투자액은
        이 시장의 열린 포지션 원가 합에서 밸류분(value_rows)을 뺀 나머지다.
        """
        invested = sum(float(r["qty"]) * float(r["avg_price"]) for r in value_rows
                       if r["market"] == market)
        rows = self.store.get_open_positions() if self.store else []
        total = sum(float(r["qty"]) * float(r["avg_price"]) for r in rows
                    if r["market"] == market)
        brain_invested = max(0.0, total - invested)
        return compute_sleeve(sleeve_pct=cfg_v["sleeve_pct"],
                              brain_reserve_pct=cfg_v["brain_reserve_pct"],
                              max_gross_exposure=self.cfg.risk.get("max_gross_exposure"),
                              base=self._exposure_base_amount(market),
                              value_invested=invested,
                              brain_invested=brain_invested)

    # ── due 시장 판정 ────────────────────────────────────────────
    def _due_markets(self, cfg_v: dict, state: dict, now: float) -> list[str]:
        """오늘(KST) 아직 안 돈 & value_trade.sessions(기본 정규장) 허용 시장."""
        today = _kst_date(now)
        value_sessions = value_sessions_from_raw(self.cfg.raw)
        due = []
        for m in cfg_v["markets"]:
            if state.get(m) == today:
                continue
            if not market_value_due(m, value_sessions, now):
                continue
            due.append(m)
        return due

    def run(self) -> dict:
        cfg_v = value_trade_cfg(self.cfg)
        if not cfg_v["enabled"]:
            return {"enabled": False}
        now = self.now_fn()
        state = _load_state(self.state_path)
        due = self._due_markets(cfg_v, state, now)
        if not due:
            return {"due": [], "markets": {}}

        watchlist = load_watchlist(self.watchlist_path)
        summary: dict = {"due": due, "markets": {}}
        today = _kst_date(now)
        for market in due:
            try:
                mres = self._run_market(market, cfg_v, watchlist, now)
            except Exception as e:                # 시장 단위로 삼켜 다른 시장 계속
                log.exception("[value_trade][%s] 시장 사이클 실패(스킵): %s", market, e)
                if self.store:
                    try:
                        self.store.log_event("value_error", None,
                                             {"market": market, "err": str(e)})
                    except Exception:
                        pass
                continue
            summary["markets"][market] = mres
            # due 시장 state 갱신(오늘 날짜 기록, 원자적). 예외로 스킵한 시장은 미기록.
            state[market] = today
            _save_state(self.state_path, state)
        return summary

    def _run_market(self, market: str, cfg_v: dict, watchlist: dict,
                    now: float) -> dict:
        from ..value_ops import (
            append_funnel, apply_post_cycle_cooldown, effective_new_entries_cap,
            load_cooldown, record_cooldown_event, save_cooldown,
            truncate_buys_by_score,
        )
        # 1) 셀렉터 — 이 시장 후보만.
        cands = [c for c in select_candidates(
                     watchlist, self.store, cfg_v, now,
                     cooldown_path=self.cooldown_path)
                 if c.get("market") == market]
        funnel = {"market": market, "ts": now, "selected": len(cands),
                  "too_expensive": 0, "margin_fail": 0, "timing_fail": 0, "gated": 0,
                  "dropped": [],
                  "cap_drops": [], "proposed": 0, "filled": 0, "vetoed": 0}
        res = {"candidates": len(cands), "gated": 0, "proposed": 0,
               "filled": 0, "vetoed": 0, "funnel": funnel}
        if not cands:
            append_funnel(funnel, self.funnel_path)
            return res

        # 2) 슬리브·슬롯
        value_rows = self._value_positions()
        # 시장별 보유만 슬롯에 반영(타시장 보유가 이 시장 remaining을 깎지 않음)
        value_mkt = [
            r for r in value_rows
            if (_row_val(r, "market") or _row_meta(r).get("market")) == market
        ]
        if not value_mkt and value_rows:
            # market 라벨이 전원 없으면 보수적으로 전체 카운트
            if not any(_row_val(r, "market") or _row_meta(r).get("market")
                       for r in value_rows):
                value_mkt = list(value_rows)
        sleeve = self._sleeve(market, cfg_v, value_rows)
        if sleeve["room"] <= 0:
            res["skip"] = "sleeve_full"
            funnel["skip"] = "sleeve_full"
            append_funnel(funnel, self.funnel_path)
            return res

        # 종목 수 상한은 두지 않는다 — 개수는 **자본 정책**이 정한다(슬리브 60% ·
        # 종목당 20~25% · 뇌 몫 30% · 총노출 90%). 칸으로 한 번 더 막으면 뇌가 예약된
        # 30% 를 갖고도 자리가 없어 못 쓰는 상태가 생겨 예약의 의미가 깨진다.
        # 계좌 상한이 명시된 경우에만(레거시/테스트) 미리 반영해 어차피 거부될 BUY 를
        # LLM 에 태우지 않는다.
        acct_cap = self.risk.max_positions_for(market) if self.risk else None
        if cfg_v.get("max_positions") is not None:      # 명시 설정(레거시/테스트)
            slot_cap = int(cfg_v["max_positions"])
            n_held = len({_row_val(r, "symbol") for r in value_mkt})
        else:
            slot_cap = acct_cap                          # None = 무제한
            n_held = self._market_open_count(market)     # 뇌 포함 전체
        remaining_slots = (None if slot_cap is None
                           else max(0, int(slot_cap) - n_held))
        n_value = len({_row_val(r, "symbol") for r in value_mkt})
        funnel["account_slot_cap"] = acct_cap            # None = 무제한
        funnel["value_held"] = n_value
        funnel["market_open"] = self._market_open_count(market)
        if remaining_slots is not None and remaining_slots <= 0:
            cands = [c for c in cands if c.get("_tranche")]
            if not cands:
                res["skip"] = "max_positions"
                funnel["skip"] = "max_positions"
                append_funnel(funnel, self.funnel_path)
                return res

        min_ticket = (float(sleeve.get("base") or 0)
                      * self.base_position_pct
                      * self.conviction_size_floor)
        # 하드캡 = ceiling×슬롯×room (플랜 §6.2). new_entries_per_run 은 LLM 힌트만.
        eff_cap = effective_new_entries_cap(
            ceiling=cfg_v["new_entries_ceiling"],
            remaining_slots=remaining_slots,
            sleeve_room=float(sleeve.get("room") or 0),
            min_ticket=min_ticket,
        )
        funnel["effective_new_cap"] = eff_cap
        funnel["new_entries_target"] = cfg_v["new_entries_per_run"]
        funnel["slot_cap"] = slot_cap
        funnel["remaining_slots"] = remaining_slots
        funnel["min_ticket"] = round(min_ticket, 2)
        log.info("[value_trade][%s] 밸류 %d종 / 시장 %d종(칸 상한 %s) · 슬리브 예산 %.0f "
                 "중 잔여 %.0f · 신규캡 %d", market, n_value,
                 funnel["market_open"], acct_cap if acct_cap is not None else "무제한",
                 sleeve.get("budget") or 0, sleeve.get("room") or 0, eff_cap)

        # 3) 마진·타이밍 → review_per_run
        symbols = [c["symbol"] for c in cands]
        prices: dict = {}
        if self.price_fn is not None:
            try:
                prices = self.price_fn(symbols, market) or {}
            except Exception as e:
                log.warning("[value_trade][%s] price_fn 실패 — 종가 폴백: %s", market, e)
                prices = {}

        fractional_ok = market.upper() in (cfg_v.get("fractional_markets") or [])
        review_n = int(cfg_v.get("review_per_run") or cfg_v.get("max_per_run") or 10)

        def _drop(sym: str, stage: str, extra: dict | None = None) -> None:
            """탈락 종목·단계 기록 — 숫자만 남으면 '왜 A 를 안 샀나'에 답할 수 없다."""
            if len(funnel["dropped"]) < 30:
                row = {"symbol": sym, "stage": stage}
                if extra:
                    row.update(extra)
                funnel["dropped"].append(row)

        gated: list[dict] = []
        for c in cands:
            sym = c["symbol"]
            try:
                df = self.fetch_history_fn(sym, market)
            except Exception as e:
                log.debug("[value_trade][%s] 히스토리 실패(스킵): %s", sym, e)
                continue
            price = prices.get(sym) or _last_close(df)
            if not price or price <= 0:
                continue
            if (not fractional_ok and not cfg_v.get("allow_min_lot")
                    and min_ticket > 0 and price > min_ticket):
                # 소수점이 안 되는 시장에서 1주 가격이 티켓보다 비싸면 정상 사이징으로
                # 0주다. min_lot 을 끈 이상 제안돼도 체결될 수 없으니 자리를 안 쓴다.
                # 소수점 시장(미장 정규장)은 '되는 만큼' 사면 되므로 제외하지 않는다.
                funnel["too_expensive"] += 1
                _drop(sym, "too_expensive", {"price": round(float(price), 2)})
                continue
            if not passes_margin_guard(c, price):
                funnel["margin_fail"] += 1
                _drop(sym, "margin", {"price": round(float(price), 2),
                                      "fair_low": fair_price_low(c)})
                continue
            timing = timing_gate(df, price)
            if timing is None:
                funnel["timing_fail"] += 1
                _drop(sym, "timing", {"price": round(float(price), 2)})
                continue
            c["_current_price"] = float(price)
            c["_timing"] = timing
            # 확신도 채점의 안전마진 축 — 게이트가 이미 계산한 값을 재활용한다.
            c["_fair_low"] = fair_price_low(c)
            gated.append(c)
            if len(gated) >= review_n:
                break
        res["gated"] = len(gated)
        funnel["gated"] = len(gated)
        if not gated:
            append_funnel(funnel, self.funnel_path)
            return res

        # 4) 컨텍스트
        context = self._build_context(market, cfg_v, sleeve, gated, value_rows, now)
        price_lookup = {c["symbol"]: c["_current_price"] for c in gated}

        # 5) LLM → BUY 절단 → run_cycle
        llm = self.llm_factory(gated)
        val_llm = self.val_llm_factory(gated) if self.val_llm_factory else llm
        tranche_by_sym = {c["symbol"]: c["_tranche"] for c in gated if c.get("_tranche")}
        # 1주 시범매수(min_lot)는 밸류에서 쓰지 않는다. 이 경로는 종목당 비중 상한을
        # 면제하는데, 미국 예산이 작아 고단가주 1주가 계좌의 절반을 넘는다($414 1주 =
        # 계좌 57%). 넉 달 들고 갈 진입에 쓰기엔 쏠림이 크고, 1주로는 밸류 논지를
        # 표현할 수도(나중에 비중 상한 때문에 추가매수도) 없다.
        agents_cfg = self.cfg.raw.get("agents", {}) or {}
        if cfg_v.get("allow_min_lot"):                  # 명시적으로 켰을 때만
            mlc = agents_cfg.get("min_lot_conviction")
            if mlc is None and self.cfg.risk.get("allow_min_lot"):
                mlc = self.min_conviction
        else:
            mlc = None

        decision_agent = ValueDecisionAgent(
            llm, max_position_pct=self.max_position_pct, tranche_by_sym=tranche_by_sym)
        cooldown_path = self.cooldown_path

        # 결정만 먼저 받아 절단(run_cycle 전체를 두 번 돌리지 않기 위해 agent 래핑)
        class _CapAgent:
            def __init__(self, inner, gated_rows, cap, funnel_ref):
                self.inner = inner
                self.gated_rows = gated_rows
                self.cap = cap
                self.funnel_ref = funnel_ref

            def decide(self, context_json: str):
                out = self.inner.decide(context_json)
                tranche_syms = {c["symbol"] for c in self.gated_rows if c.get("_tranche")}
                # 1) cap 적용 먼저
                forced_hold: set[str] = set()
                if self.cap <= 0:
                    for p in out.proposals:
                        if p.side == "BUY" and p.symbol not in tranche_syms:
                            p.side = "HOLD"
                            forced_hold.add(p.symbol)
                    self.funnel_ref["cap_drops"] = [
                        {"symbol": s, "reason": "cap_zero"} for s in forced_hold]
                else:
                    non_tr = [p for p in out.proposals
                              if p.side == "BUY" and p.symbol not in tranche_syms]
                    _, drops = truncate_buys_by_score(
                        non_tr, self.gated_rows, cap=self.cap)
                    drop_syms = {d["symbol"] for d in drops}
                    for p in out.proposals:
                        if p.side == "BUY" and p.symbol in drop_syms:
                            p.side = "HOLD"
                            forced_hold.add(p.symbol)
                    self.funnel_ref["cap_drops"] = drops

                # 2) 쿨다운 (a)/(c)만 — BUY 리셋·(b) reject는 사이클 후 화이트리스트
                cd = load_cooldown(cooldown_path)
                buy_syms = {p.symbol for p in out.proposals if p.side == "BUY"}
                for c in self.gated_rows:
                    sym = c["symbol"]
                    if sym in buy_syms:
                        continue  # 체결/veto 후처리
                    ttl = cfg_v.get("cooldown_streak_ttl_days", 5)
                    if sym in forced_hold and self.cap > 0:
                        record_cooldown_event(
                            cd, sym, kind="cap_bump", now=now,
                            hold_n=cfg_v.get("cooldown_cap_bump_n", 8),
                            cool_days=cfg_v.get("cooldown_days", 5),
                            streak_ttl_days=ttl)
                    elif sym not in forced_hold:
                        record_cooldown_event(
                            cd, sym, kind="llm_hold", now=now,
                            hold_n=cfg_v.get("cooldown_hold_n", 3),
                            cool_days=cfg_v.get("cooldown_days", 5),
                            streak_ttl_days=ttl)
                    # cap==0 강제 HOLD → 미가산
                save_cooldown(cd, cooldown_path)
                return out

            def __getattr__(self, name):
                return getattr(self.inner, name)

        capped_agent = _CapAgent(decision_agent, gated, eff_cap, funnel)

        # 확신도는 코드 루브릭(score_value_buy)이 산출한다 — LLM 자가채점은 저널에
        # conviction_code[sym].llm 으로만 남는다. brief 는 도시에 대신 후보 항목 자체이고
        # (composite_value·_fair_low), features 는 공유 감점항(_event_parts)의 입력이다.
        brief_by_sym = {c["symbol"]: c for c in gated}
        value_features = {c["symbol"]: {"news": c.get("recent_news") or []}
                          for c in gated}
        cyc = run_cycle(
            context_json=context,
            decision_agent=capped_agent,
            validation_agent=ValidationAgent(
                val_llm, min_conviction=self.min_conviction,
                code_floor=float(cfg_v["code_conviction_floor"])),
            broker=self.broker, risk=self.risk, price_lookup=price_lookup,
            apply_code_conviction=True,
            conviction_score_fn=score_value_buy,
            conviction_snap_fn=freeze_value_snap,
            dossier_brief_fn=brief_by_sym.get,
            features_by_sym=value_features,
            journal_path=self.journal_path,
            arm_fn=None, dossier_fn=None, zone_fn=None, conviction_sizing=True,
            min_lot_conviction=float(mlc) if mlc is not None else None,
            market_fn=lambda s: market,
            store=self.store,
            allow_add=True,
            tranche_weights={
                c["symbol"]: float(c["_tranche"]["weight"])
                for c in gated if c.get("_tranche")
            },
            budget_caps={c["symbol"]: float(sleeve.get("room") or 0) for c in gated},
            fractional_markets=set(cfg_v.get("fractional_markets") or []),
        )

        if self.store:
            from ..shadow_ledger import book_blocked, book_soft_pending
            book_blocked(self.store, cyc, price_lookup, sleeve="value",
                         cfg=self.cfg.raw)
            book_soft_pending(self.store, cyc, price_lookup, sleeve="value",
                              cfg=self.cfg.raw)

        res["proposed"] = sum(1 for p in cyc.decision.proposals if p.side == "BUY")
        res["vetoed"] = sum(1 for e in cyc.executed if e.get("status") == "vetoed")
        res["filled"] = sum(
            1 for e in cyc.executed if e.get("status") in ("filled", "partial"))
        funnel["proposed"] = res["proposed"]
        funnel["filled"] = res["filled"]
        funnel["vetoed"] = res["vetoed"]

        # (b) reject 화이트리스트 + 체결 시 streak 리셋 (하드게이트 거부는 제외)
        apply_post_cycle_cooldown(
            cyc.executed, now=now,
            hold_n=cfg_v.get("cooldown_hold_n", 3),
            cool_days=cfg_v.get("cooldown_days", 5),
            streak_ttl_days=cfg_v.get("cooldown_streak_ttl_days", 5),
            path=self.cooldown_path)
        append_funnel(funnel, self.funnel_path)

        self._mirror_fills(market, cfg_v, cyc, gated)
        return res

    def _build_context(self, market: str, cfg_v: dict, sleeve: dict,
                       gated: list[dict], value_rows: list, now: float) -> str:
        # 종목별 과거 거래 회고(lessons) — 이력 있는 후보에만 부착(LLM 0콜).
        lessons = (build_symbol_lessons(self.store, [c["symbol"] for c in gated])
                   if self.store and self.cfg.raw.get("agents", {}).get("lessons", True)
                   else {})
        cand_ctx = []
        for c in gated:
            metrics = c.get("metrics") or {}
            entry = {
                "symbol": c["symbol"],
                "name": c.get("name"),
                "current_price": c["_current_price"],
                "fair_price_low": fair_price_low(c),
                "fair_price_high": fair_price_high(c),
                "dossier": {
                    "stance": c.get("stance"),
                    "conviction": c.get("conviction"),
                    "thesis": c.get("thesis"),
                    "risks": c.get("risks", []),
                    "evidence": c.get("evidence", []),
                },
                "fundamentals": c.get("fundamentals"),
                "recent_news": c.get("recent_news"),
                "metrics": metrics,
                "timing": c["_timing"],
                "scan_age_hours": round((now - float(c.get("ts", now))) / 3600, 1),
            }
            pt = lessons.get(c["symbol"])
            if pt:
                entry["past_trades"] = pt
            if c.get("_tranche"):            # 추가 트랜치(이미 보유 중인 종목의 추가 매수)
                entry["tranche"] = c["_tranche"]
            cand_ctx.append(entry)
        portfolio = [{"symbol": r["symbol"], "market": r["market"], "qty": r["qty"],
                      "avg_price": r["avg_price"], "entry_thesis": r["thesis"]}
                     for r in value_rows]
        # 시황·주의층 — 프롬프트 4번(국면)과 데이터가 맞도록 메인 뇌와 동일 슬롯 주입.
        # top-level "market" 은 시장코드(KR/US) 유지(하위호환). 시황 블록은 market_state.
        ms = _load_market_state(self.market_state_path)
        attach_krx_fields(gated, ms)
        focus = build_focus(ms, candidates=gated, positions=portfolio)
        ctx = {"track": "value", "market": market,
               "asof": ms.get("asof"),
               "market_state": _market_block(ms),
               "sleeve": sleeve, "candidates": cand_ctx,
               "portfolio_value_positions": portfolio,
               "constraints": {
                   "max_position_pct": self.max_position_pct,
                   # min_conviction 은 싣지 않는다 — 확신도 임계를 채점자에게 알려주면
                   # 그 바로 위에 붙는다(관측: BUY 11건 전부 0.61~0.64). 사이징·거부는
                   # 코드 루브릭(score_value_buy)이 맡는다.
                   "new_entries_target": cfg_v.get("new_entries_per_run", 1),
                   "new_entries_ceiling": cfg_v.get("new_entries_ceiling", 2),
               }}
        if focus:
            ctx["focus"] = focus
        return json.dumps(ctx, ensure_ascii=False)

    def _mirror_fills(self, market: str, cfg_v: dict, cyc, gated: list[dict]) -> None:
        """체결(filled) BUY 를 store 에 미러링(멱등).

        신규 진입은 open_position(strategy="value", stop=entry*(1-hard_stop_pct),
        target=적정가 하단(fair_price_low), meta 에 source/entry_thesis/fair_low/fair_high/
        scan_ts/트랜치 상태). 이미 열린 행이 있으면 **추가 트랜치일 때만** 그 행을 갱신하고
        (_mirror_tranche), 아니면 건너뛴다(기존 멱등 동작).
        """
        if not self.store:
            return
        by_sym = {c["symbol"]: c for c in gated}
        prop_by_sym = {p.symbol: p for p in cyc.decision.proposals}
        open_rows = {r["symbol"]: r for r in self.store.get_open_positions()}
        done: set[str] = set()
        for e in cyc.executed:
            if e.get("status") not in ("filled", "partial") or e.get("action") != "BUY":
                continue
            sym = e["symbol"]
            if sym in done:                      # 멱등 — 이번 사이클에서 이미 반영
                continue
            pos = self.broker.position(sym)
            if not pos.is_open:
                continue
            done.add(sym)
            entry = pos.avg_price
            cand = by_sym.get(sym, {})
            prop = prop_by_sym.get(sym)
            stop = round(entry * (1 - cfg_v["hard_stop_pct"]), 2) if entry else None
            row = open_rows.get(sym) or next(
                (r for r in self.store.get_open_positions() if r["symbol"] == sym), None)
            if row is not None:
                meta = _row_meta(row)
                if (cand.get("_tranche") and meta.get("source") == "value"
                        and isinstance(meta.get("tranches"), list)):
                    self._mirror_tranche(row, sym, cand, pos, stop)
                    continue
                if meta.get("source") == "value":
                    continue
                # run_cycle(store=) mirror 가 fill_mirror 행을 만들었으면 value 메타로 승격.
                log.info("[value] promote fill_mirror → value meta sym=%s id=%s", sym, row["id"])
                fpl = fair_price_low(cand)
                fph = fair_price_high(cand)
                promote_meta = {"source": "value", "horizon": "position",
                        "entry_thesis": (prop.thesis if prop else cand.get("thesis")),
                        "fair_low": fpl, "fair_high": fph,
                        "scan_ts": cand.get("ts"),
                        "tranches": list(cfg_v["tranches"]), "tranche_idx": 1,
                        "last_tranche_at": self.now_fn()}
                self.store.update_position(
                    int(row["id"]), qty=pos.qty, avg_price=entry, strategy="value",
                    thesis=(prop.thesis if prop else cand.get("thesis")),
                    target_price=fpl, stop_price=stop, meta=promote_meta)
                self.store.disarm_symbol(sym)
                from ..shadow_ledger import cancel_shadow_on_fill
                cancel_shadow_on_fill(self.store, sym)
                self.store.log_event("value_entry", sym,
                                     {"entry": entry, "stop": stop, "target": fpl,
                                      "qty": pos.qty})
                continue
            fpl = fair_price_low(cand)
            fph = fair_price_high(cand)
            meta = {"source": "value", "horizon": "position",
                    "entry_thesis": (prop.thesis if prop else cand.get("thesis")),
                    "fair_low": fpl, "fair_high": fph,
                    "scan_ts": cand.get("ts"),
                    # 분할 매수 상태(기본 [1.0] 이면 1회차로 즉시 소진 = 기존 전량 진입).
                    "tranches": list(cfg_v["tranches"]), "tranche_idx": 1,
                    "last_tranche_at": self.now_fn()}
            self.store.open_position(
                sym, market, pos.qty, entry, strategy="value",
                thesis=(prop.thesis if prop else cand.get("thesis")),
                target_price=fpl, stop_price=stop, meta=meta)
            self.store.disarm_symbol(sym)
            from ..shadow_ledger import cancel_shadow_on_fill
            cancel_shadow_on_fill(self.store, sym)
            self.store.log_event("value_entry", sym,
                                 {"entry": entry, "stop": stop, "target": fpl,
                                  "qty": pos.qty})

    def _mirror_tranche(self, row, sym: str, cand: dict, pos, stop) -> None:
        """추가 트랜치 체결을 기존 store 행에 반영(수량·평단·손절가·회차 갱신).

        손절가는 **새 평단 기준**으로 다시 계산한다 — 옛 평단에 남아 있으면 추가 매수 후
        손절 지점이 어긋난다. 후보에 _tranche 가 없으면(추가 트랜치가 아니면) 무접촉.
        """
        if not cand.get("_tranche"):
            return
        meta = _row_meta(row)
        tr = meta.get("tranches")
        if not isinstance(tr, list) or meta.get("tranche_idx") is None:
            return                               # 트랜치 정보 없는 구 포지션 — 갱신 안 함
        idx = int(meta["tranche_idx"]) + 1
        meta["tranche_idx"] = idx
        meta["last_tranche_at"] = self.now_fn()
        self.store.update_position(row["id"], qty=pos.qty, avg_price=pos.avg_price,
                                   stop_price=stop, meta=meta)
        self.store.log_event("value_tranche", sym,
                             {"tranche": f"{idx}/{len(tr)}", "qty": pos.qty,
                              "avg_price": pos.avg_price, "stop": stop})
        log.info("[value_trade][%s] 추가 트랜치 %d/%d — 수량 %s, 새 평단 %s, 새 손절 %s",
                 sym, idx, len(tr), pos.qty, pos.avg_price, stop)
