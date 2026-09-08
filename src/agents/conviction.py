"""BUY 확신도 — 사이징용. 매수 봉인이 아니다.

트랙별 루브릭 2종. **"왜 사는가"는 트랙마다 다르고, "사면 안 되는 이유"는 공통이다** —
가산항만 갈라지고 감점항(_event_parts: 법적·희석 공시, 실적 미스)은 공유한다.
  · 스윙/단타 `score_buy`  — 손익비·안정화·수급·셋업·무효화 (진입 정밀도가 수익을 가른다)
  · 밸류     `score_value_buy` — 저평가도(composite_value)·안전마진 (넉 달을 들고 간다)

사이징: base_position_pct × (floor + span × conviction), 기본 floor=0.75·span=0.25
→ 확신 0~1 에서 목표비중의 75~100%(소폭 ±). 배율·기본비중은 config risk.* 로 조정.

설계:
- 증거 *문장 개수*·LLM 자가채점·존 안 보너스는 쓰지 않는다.
  얇은 표본의 다음 세션 수익으로 가중치를 맞추지 않는다.
- 게이트(도시에·존 체결·검증 LLM)와 사이징을 분리한다. 이미 통과한 BUY 의
  기본은 0.48. 가산은 '이 매수의 기대 손익을 키우는 신선한 부호 정보'만,
  감점은 '이 시스템의 매수 논리와 충돌하는 사실'.
- 결측은 0 (중립). 연간 흑자·공급계약·뉴스 호재 제목은 가산하지 않는다.
  텍스트 재료는 뇌·검증이 BUY/HOLD 를 가르고, 코드는 부호가 있는 충돌만 깎는다
  (희석·법적 공시, 컨센서스 대비 실적 미스, 연간 적자).
- 연속량(수급·안정화·셋업·RSI·실적편차)은 부호만이 아니라 강도를 W_* 한도 안에
  접는다. 충돌항(소송·희석·무효화·계획없음·적자)은 계단함수로 둔다.
- BUY 저널 `conviction_code.*.snap` 에 채점 입력을 동결한다. 가중치 자동 갱신은 없다.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .athena import compute_rr
from .wiring import entry_stop_target

FLOOR = 0.22
CAP = 0.82
BASE = 0.48

# 신뢰도 순. 캔들/수급은 당일, 손익비는 도시에는 그리지만 실행이 쓰는 숫자,
# 셋업은 n 가드, 재무는 연간이라 적자만. 연속항의 상한 — 강도는 이 안에 접는다.
W_RR_HI = 0.08       # rr >= 2
W_RR_OK = 0.04       # 1.5 <= rr < 2
W_RR_LO = -0.10      # rr < 1.5 (계획 자체가 얇음)
W_STAB = 0.12        # 20일선 위 AND 20일+  — 이 시스템의 칼 피하기 가설
W_STAB_BAD = -0.12
W_FLOW = 0.08        # 외국인 순매수
W_FLOW_BAD = -0.08
W_SETUP = 0.08       # 셋업 승률. 0.50 에서 0, ±0.10 에서 포화
W_SETUP_BAD = -0.08  # 하위호환 별칭(W_SETUP 과 대칭)
W_FUND_RED = -0.06   # 순이익률 < -5% 만. 흑자는 너무 흔해서 가산 안 함
W_NO_PLAN = -0.10    # 스윙인데 bullish 도시에 없음 / day 의 계획 부재
W_DAY_OS = 0.08      # day + RSI<40 (프롬프트의 과매도=day 와 일치)
W_INVAL = -0.20      # 무효화 하회 — 체결 거부 대상이라 사이징도 깎음
W_DISC_LEGAL = -0.12 # 소송·횡령·상장폐지 등 — 매수 논리와 충돌
W_DISC_DILUTE = -0.10  # 유상증자·CB·감자 — 희석
W_EARN_MISS = -0.10  # 컨센서스 대비 -10%. 하회 가중(이미 가격에 반영)
W_GAP_SHAPE = 0.06       # close_scan: 종가가 당일 레인지 하단
W_GAP_SHAPE_BAD = -0.08  # close_scan: 장중 반등 흔적(close_loc 높음)
W_GAP_DOWN = 0.05        # close_scan: 갭다운 >=2%

# 강도 스케일. unit_intensity(x, scale) 가 scale 에서 ≈0.76, 2×scale 에서 ≈0.96.
STAB_RET_SCALE = 3.0     # 20일 수익률 +3% 가 분명한 안정화
STAB_DD_SCALE = 6.0      # |20일| 또는 |낙폭| 6% 가 분명한 칼
FLOW_PART_SCALE = 0.05   # |순매수|/거래량 = 5% 가 분명한 수급
SETUP_WR_MID = 0.50
SETUP_WR_SPAN = 0.10     # 승률 40%→−1, 60%→+1
EARN_MISS_SCALE = 20.0   # |하회| 20% 가 분명한 미스
RSI_OS = 40.0
RSI_OS_SPAN = 8.0        # RSI 32 가 분명한 과매도

# ── 밸류 트랙 루브릭 ──────────────────────────────────────────────
# 스윙과 축이 다르다. **가산은 트랙 고유(저평가도·안전마진), 감점은 공유(_event_parts).**
# 손익비·무효화·bullish 도시에·안정화·수급·셋업은 밸류에 넣지 않는다 — ValueDossier 에
# 레벨(entry/invalidation/target)이 없고, 안정화는 timing_gate 가 하드 요구라 후보 전원이
# 통과해 변별력이 0이며, 일간 수급·셋업 승률은 time_stop_days=120 보유엔 노이즈다.
# 재무(ROE·부채·성장)는 composite_value 안의 quality_tilt 가 이미 0.15 가중으로 보고
# 있으니 여기서 또 깎지 않는다(이중계상 금지) — 감점은 **이벤트항만**.
VALUE_BASE = 0.45
VALUE_FLOOR = 0.25
VALUE_CAP = 0.85
W_VALUE_CHEAP = 0.14   # composite_value. 이 루브릭 최대 연속항(스윙 W_STAB 에 대응)
W_VALUE_MARGIN = 0.12  # 안전마진. 편도(+) — 음수 구간은 passes_margin_guard 가 이미 자름
# 중심·포화폭은 후보 풀(undervalued·conviction>=0.4) 관측 분포로 잡는다.
# 2026-09-08 n=178: mean 0.616 · p50 0.633 · sd 0.153 · p10~p90 = 0.418~0.815.
CV_MID = 0.60
CV_SPAN = 0.20         # ±0.20 에서 포화(≈p10/p90)
MARGIN_SCALE = 0.15    # 안전마진 15% 가 분명한 마진(tanh)

# 사이징에 넣는 공시만. 공급계약·수주·자기주식·잠정실적 제목은 여기 없다
# (호재 스탬프가 되거나, 실적은 surprise 숫자로만 본다).
_LEGAL_KW = ("소송", "횡령", "배임", "파산", "회생절차", "감사의견",
             "관리종목", "상장폐지", "거래정지", "영업정지")
_DILUTE_KW = ("유상증자", "감자", "전환사채", "신주인수권", "교환사채")


def unit_intensity(x: float, scale: float) -> float:
    """비음수 x → 0~1. scale 에서 tanh≈0.76, 2×scale 에서 ≈0.96."""
    if x <= 0 or scale <= 0:
        return 0.0
    return math.tanh(x / scale)


def size_weight(base: float, conviction: float | None, *,
                enabled: bool = True,
                floor: float = 0.75, span: float = 0.25,
                cap: float | None = None) -> float:
    """사이징 비중. base×(floor+span×c). enabled 끄거나 c 없으면 base 그대로.

    floor/span 은 config risk.conviction_size_* (시점·운용자별 조정).
    cap(보통 max_position_pct)이 있으면 그 이하로 클램프.
    """
    w = float(base)
    if enabled and conviction is not None:
        c = max(0.0, min(1.0, float(conviction)))
        w = w * (float(floor) + float(span) * c)
    if cap is not None:
        try:
            w = min(w, float(cap))
        except (TypeError, ValueError):
            pass
    return w


def min_lot_adjust(weight: float, *, price: float, capital: float,
                   conviction: float | None,
                   min_lot_conviction: float | None,
                   enabled: bool = True) -> tuple[float, float]:
    """확신도 OK 인데 1주도 안 나오면 (1주분 비중, min_qty=1). 아니면 (weight, 0).

    데이트레·옛 armed(컷 없음)·문턱 미달은 손대지 않는다.
    """
    w = float(weight)
    if (not enabled or min_lot_conviction is None or conviction is None
            or price <= 0 or capital <= 0):
        return w, 0.0
    if float(conviction) < float(min_lot_conviction):
        return w, 0.0
    if w * capital < price:
        return max(w, price / capital), 1.0
    return w, 0.0


def skip_position_headroom(min_qty: float) -> bool:
    """시범 1주(min_qty>0)는 종목비중 headroom 을 사이징 캡으로 쓰지 않는다.

    게이트도 같은 1주만 비중 면제. 이미 보유 추가는 호출측이 min_qty=0 이거나
    BUY 스킵이라 여기로 안 온다.
    """
    try:
        return float(min_qty) > 0
    except (TypeError, ValueError):
        return False


def _part(delta: float, label: str) -> tuple[float, str]:
    return (delta, f"{label} {delta:+.2f}")


@dataclass
class ConvictionScore:
    value: float
    llm: float | None
    parts: list[str] = field(default_factory=list)


def _rr_of(dossier: dict) -> float | None:
    """도시에에 적힌 RR (저널·표시용). 사이징 가산에는 쓰지 않는다 — LLM 레벨 종속."""
    rr = dossier.get("rr")
    if rr is not None:
        try:
            return float(rr)
        except (TypeError, ValueError):
            pass
    return compute_rr(dossier.get("entry_low"), dossier.get("entry_high"),
                      dossier.get("invalidation"), dossier.get("target"))


def _plan_entry(price: float | None, dossier: dict) -> float | None:
    """사이징 RR 의 기준가 — 진입존 중앙, 없으면 현재가.

    현재가로 나누면 존 위/아래에서 RR 이 바뀌어 사이징이 존 위치 보너스가 된다.
    존 위치는 체결 경로가 처리하므로 계획은 존 중앙으로 평가한다.
    """
    lo, hi = dossier.get("entry_low"), dossier.get("entry_high")
    try:
        lo, hi = float(lo), float(hi)
        if lo > 0 and hi >= lo:
            return (lo + hi) / 2
    except (TypeError, ValueError):
        pass
    return price if price and price > 0 else None


def _sizing_rr(price: float | None, horizon: str, dossier: dict) -> float | None:
    """사이징용 손익비 = (코드 목표 − 진입) / (진입 − 코드 손절).

    확신도 RR 은 LLM/Athena 레벨·proposal.params 를 쓰지 않는다.
    책에 심는 손절은 combine_stop_target 이 별도 경로 — 채점 되먹임 차단.
    """
    entry = _plan_entry(price, dossier)
    if not entry or entry <= 0:
        return None
    # params=None → 보유기간 기본%만 (stop_loss_pct/target_profit_pct 무시)
    code_stop, code_target = entry_stop_target(entry, horizon, None)
    if code_stop is None or code_target is None:
        return None
    risk = entry - code_stop
    if risk <= 0:
        return None
    return round((code_target - entry) / risk, 2)


def _f(v):
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return None


def _hit(blob: str, kws: tuple[str, ...]) -> str | None:
    for kw in kws:
        if kw in blob:
            return kw
    return None


def _event_blob(features: dict) -> str:
    parts: list[str] = []
    for n in features.get("news") or []:
        if isinstance(n, dict) and n.get("title"):
            parts.append(str(n["title"]))
    for d in features.get("disclosures") or []:
        if isinstance(d, dict):
            parts.append(f"{d.get('keyword') or ''} {d.get('report_nm') or ''}")
    return " ".join(parts)


def _surprise_pct(er: dict) -> float | None:
    """컨센서스 대비 %. 영업이익 → 순이익 → EPS → 매출 순. 파싱 실패는 결측."""
    if er.get("parse_ok") is False:
        return None
    for k in ("op_profit_surprise_pct", "net_income_surprise_pct",
              "eps_surprise_pct", "revenue_surprise_pct"):
        v = _f(er.get(k))
        if v is not None:
            return v
    sp = er.get("surprise_pct")
    if isinstance(sp, dict):
        return _surprise_pct(sp)
    return _f(sp)


def _event_parts(features: dict) -> list[tuple[float, str]]:
    out: list[tuple[float, str]] = []
    blob = _event_blob(features)
    if blob:
        kw = _hit(blob, _LEGAL_KW)
        if kw:
            out.append((W_DISC_LEGAL, f"공시 {kw} −"))
        else:
            kw = _hit(blob, _DILUTE_KW)
            if kw:
                out.append((W_DISC_DILUTE, f"공시 {kw} −"))

    rows = [er for er in (features.get("earnings_results") or [])
            if isinstance(er, dict)]
    one = features.get("earnings_result")
    if isinstance(one, dict):
        rows.append(one)
    worst = None
    for er in rows:
        s = _surprise_pct(er)
        if s is not None and (worst is None or s < worst):
            worst = s
    if worst is not None and worst <= -10:
        mag = unit_intensity(-worst, EARN_MISS_SCALE)
        out.append(_part(W_EARN_MISS * mag, f"실적 하회 {worst:g}%"))
    return out


def attach_event_features(features_by_sym: dict | None,
                          disclosures: list | None = None,
                          earnings_results: list | None = None) -> dict:
    """워처 공시·실적 결과를 종목 피처에 붙인다. 후보에 없는 종목은 건너뛴다."""
    feats = features_by_sym or {}
    for d in disclosures or []:
        if not isinstance(d, dict):
            continue
        row = feats.get(d.get("symbol"))
        if isinstance(row, dict):
            row.setdefault("disclosures", []).append(d)
    for er in earnings_results or []:
        if not isinstance(er, dict):
            continue
        row = feats.get(er.get("symbol"))
        if isinstance(row, dict):
            row.setdefault("earnings_results", []).append(er)
    return feats


def _stab_parts(features: dict) -> list[tuple[float, str]]:
    st = features.get("stabilizing") or {}
    ret = _f(st.get("ret_20d_pct"))
    if st.get("ok") is True:
        mag = unit_intensity(ret, STAB_RET_SCALE) if ret is not None else 1.0
        if mag <= 0:
            return []
        label = (f"안정화 ret20 {ret:+g}%" if ret is not None
                 else "안정화(20일선·20일+)")
        return [_part(W_STAB * mag, label)]
    knife = st.get("ok") is False or (
        st.get("above_ma20") is False and ret is not None and ret < 0)
    if not knife:
        return []
    x = abs(ret) if ret is not None and ret < 0 else None
    dd = _f(features.get("drawdown_pct"))
    if x is None and dd is not None and dd < 0:
        x = abs(dd)
    mag = unit_intensity(x, STAB_DD_SCALE) if x is not None else 1.0
    if mag <= 0:
        return []
    if ret is not None:
        label = f"안정화 실패 ret20 {ret:+g}%"
    elif dd is not None:
        label = f"안정화 실패 낙폭 {dd:g}%"
    else:
        label = "안정화 실패(칼)"
    return [_part(W_STAB_BAD * mag, label)]


def _flow_parts(features: dict) -> list[tuple[float, str]]:
    fn = _f((features.get("flows") or {}).get("foreign_net"))
    if fn is None or fn == 0:
        return []
    vol = _f(features.get("volume"))
    w = W_FLOW if fn > 0 else W_FLOW_BAD
    side = "순매수" if fn > 0 else "순매도"
    if vol is None or vol <= 0:
        return [_part(w, f"외국인 {side}")]
    part = abs(fn) / vol
    mag = unit_intensity(part, FLOW_PART_SCALE)
    if mag <= 0:
        return []
    return [_part(w * mag, f"외국인 {side} 참여 {part:.1%}")]


def _setup_parts(features: dict) -> list[tuple[float, str]]:
    best = None
    for name, info in (features.get("base_rates") or {}).items():
        if not isinstance(info, dict) or info.get("small_sample"):
            continue
        wr = _f(info.get("win_rate"))
        if wr is None:
            continue
        n = info.get("n")
        if isinstance(n, (int, float)) and n < 10:
            continue
        ar = _f(info.get("avg_ret_pct"))
        if best is None or wr > best[0]:
            best = (wr, ar, name)
    if not best:
        return []
    wr, ar, name = best
    t = max(-1.0, min(1.0, (wr - SETUP_WR_MID) / SETUP_WR_SPAN))
    if t > 0 and ar is not None and ar <= 0:
        t = 0.0
    if abs(t) < 1e-9:
        return []
    return [_part(W_SETUP * t, f"셋업 {name} 승률 {wr:.0%}")]


def _gap_shape_parts(features: dict, horizon: str) -> list[tuple[float, str]]:
    if (horizon or "").lower() != "close_scan":
        return []
    gs = features.get("gap_shape")
    if not isinstance(gs, dict):
        return []
    out: list[tuple[float, str]] = []
    if gs.get("close_near_day_low"):
        out.append(_part(W_GAP_SHAPE, "종가 당일 레인지 하단"))
    else:
        loc = _f(gs.get("close_loc"))
        if loc is not None and loc > 0.25:
            out.append(_part(W_GAP_SHAPE_BAD, f"장중 반등 흔적 close_loc {loc:.2f}"))
    if gs.get("gap_down_deep"):
        out.append(_part(W_GAP_DOWN, "갭다운 2%+"))
    return out


def _signed(features: dict | None) -> list[tuple[float, str]]:
    if not isinstance(features, dict):
        return []
    out: list[tuple[float, str]] = []
    out.extend(_stab_parts(features))
    out.extend(_flow_parts(features))
    nm = _f((features.get("fundamentals") or {}).get("net_margin"))
    if nm is not None and nm < -0.05:
        out.append((W_FUND_RED, "순이익률 적자 −"))
    out.extend(_setup_parts(features))
    out.extend(_event_parts(features))
    return out


def score_buy(proposal, *, price: float | None, dossier: dict | None,
              zone_tol: float = 0.005, features: dict | None = None) -> ConvictionScore:
    parts = [f"base {BASE:.2f}"]
    v = BASE
    d = dossier if isinstance(dossier, dict) else {}
    llm = float(getattr(proposal, "conviction", 0) or 0)
    hz = (getattr(proposal, "horizon", "") or "swing").lower()
    bullish = d.get("stance") == "bullish"

    if not bullish:
        v += W_NO_PLAN
        parts.append("계획 없음(도시에는 bullish 아님) −")
        if hz == "day":
            rsi = _f((features or {}).get("rsi"))
            if rsi is not None and rsi < RSI_OS:
                mag = unit_intensity(RSI_OS - rsi, RSI_OS_SPAN)
                if mag > 0:
                    dlt = W_DAY_OS * mag
                    v += dlt
                    parts.append(f"day 과매도 RSI {rsi:g} {dlt:+.2f}")
    else:
        rr = _sizing_rr(price, hz, d)
        if rr is not None:
            if rr >= 2.0:
                v += W_RR_HI
                parts.append(f"손익비 {rr:g}≥2 +")
            elif rr >= 1.5:
                v += W_RR_OK
                parts.append(f"손익비 {rr:g}≥1.5 +")
            else:
                v += W_RR_LO
                parts.append(f"손익비 {rr:g}<1.5 −")
        inval = d.get("invalidation")
        if price and inval is not None:
            try:
                if price < float(inval):
                    v += W_INVAL
                    parts.append("무효화가 하회 −")
            except (TypeError, ValueError):
                pass

    for delta, label in _signed(features):
        v += delta
        parts.append(label)
    for delta, label in _gap_shape_parts(features or {}, hz):
        v += delta
        parts.append(label)

    value = round(min(CAP, max(FLOOR, v)), 2)
    parts.append(f"→ {value:.2f}")
    return ConvictionScore(value=value, llm=llm, parts=parts)


def value_margin(price: float | None, fair_low: float | None) -> float | None:
    """안전마진 = (적정가 하단 − 현재가) / 현재가. 결손·비양수면 None."""
    p, f = _f(price), _f(fair_low)
    if p is None or f is None or p <= 0 or f <= 0:
        return None
    return (f - p) / p


def score_value_buy(proposal, *, price: float | None, dossier: dict | None,
                    zone_tol: float = 0.005,
                    features: dict | None = None) -> ConvictionScore:
    """밸류 BUY 확신도 — 사이징용. 스윙 score_buy 와 축이 다르다.

    dossier 는 value_watchlist 항목(+ 셀렉터가 붙인 `_fair_low`). zone_tol 은
    score_buy 와 시그니처를 맞추기 위한 자리이며 쓰지 않는다(밸류엔 진입존이 없다).
    결측은 0 기여 — 스윙과 같은 규약.
    """
    parts = [f"base {VALUE_BASE:.2f}"]
    v = VALUE_BASE
    d = dossier if isinstance(dossier, dict) else {}

    cv = _f(d.get("composite_value"))
    if cv is not None:
        t = max(-1.0, min(1.0, (cv - CV_MID) / CV_SPAN))
        if abs(t) > 1e-9:
            delta = W_VALUE_CHEAP * t
            v += delta
            parts.append(f"저평가도 {cv:.3f} {delta:+.2f}")

    margin = value_margin(price, d.get("_fair_low"))
    if margin is not None and margin > 0:
        mag = unit_intensity(margin, MARGIN_SCALE)
        if mag > 0:
            delta = W_VALUE_MARGIN * mag
            v += delta
            parts.append(f"안전마진 {margin:.1%} {delta:+.2f}")

    for delta, label in _event_parts(features if isinstance(features, dict) else {}):
        v += delta
        parts.append(label)

    value = round(min(VALUE_CAP, max(VALUE_FLOOR, v)), 2)
    parts.append(f"→ {value:.2f}")
    return ConvictionScore(
        value=value, llm=float(getattr(proposal, "conviction", 0) or 0), parts=parts)


def _zone_loc(price: float | None, dossier: dict | None) -> str | None:
    if not price or not isinstance(dossier, dict):
        return None
    lo, hi = dossier.get("entry_low"), dossier.get("entry_high")
    try:
        lo, hi = float(lo), float(hi)
    except (TypeError, ValueError):
        return None
    if price < lo:
        return "below"
    if price > hi:
        return "above"
    return "in"


def _setup_snap(features: dict) -> dict | None:
    best = None
    for name, info in (features.get("base_rates") or {}).items():
        if not isinstance(info, dict):
            continue
        wr = _f(info.get("win_rate"))
        if wr is None:
            continue
        n = info.get("n")
        n = int(n) if isinstance(n, (int, float)) else None
        ar = _f(info.get("avg_ret_pct"))
        row = {"name": str(name), "n": n, "win_rate": wr, "avg_ret_pct": ar,
               "small_sample": bool(info.get("small_sample"))}
        if best is None or wr > best["win_rate"]:
            best = row
    return best


def freeze_snap(proposal, *, price: float | None, dossier: dict | None,
                features: dict | None = None) -> dict:
    """채점 입력을 JSON 안전 스칼라로 동결. 후보 dict 전체는 남기지 않는다."""
    feat = features if isinstance(features, dict) else {}
    d = dossier if isinstance(dossier, dict) else {}
    st = feat.get("stabilizing") if isinstance(feat.get("stabilizing"), dict) else {}
    flows = feat.get("flows") if isinstance(feat.get("flows"), dict) else {}
    funds = feat.get("fundamentals") if isinstance(feat.get("fundamentals"), dict) else {}
    discs = []
    for x in (feat.get("disclosures") or [])[:3]:
        if isinstance(x, dict) and (x.get("keyword") or x.get("report_nm")):
            discs.append({"keyword": x.get("keyword"), "report_nm": x.get("report_nm")})
    worst = None
    for er in list(feat.get("earnings_results") or []) + (
            [feat["earnings_result"]] if isinstance(feat.get("earnings_result"), dict) else []):
        if isinstance(er, dict):
            s = _surprise_pct(er)
            if s is not None and (worst is None or s < worst):
                worst = s
    px = _f(price)
    rr = _rr_of(d) if d else None
    return {
        "price": px,
        "horizon": (getattr(proposal, "horizon", None) or "swing"),
        "stance": d.get("stance"),
        "rr": round(rr, 4) if rr is not None else None,
        "entry_low": _f(d.get("entry_low")),
        "entry_high": _f(d.get("entry_high")),
        "invalidation": _f(d.get("invalidation")),
        "target": _f(d.get("target")),
        "zone": _zone_loc(px, d),
        "stab_ok": None if not st else bool(st.get("ok")) if st.get("ok") is not None else None,
        "above_ma20": None if not st else bool(st["above_ma20"]) if st.get("above_ma20") is not None else None,
        "ret_20d_pct": _f(st.get("ret_20d_pct")) if st else None,
        "drawdown_pct": _f(feat.get("drawdown_pct")),
        "rsi": _f(feat.get("rsi")),
        "volume": _f(feat.get("volume")),
        "foreign_net": _f(flows.get("foreign_net")),
        "net_margin": _f(funds.get("net_margin")),
        "setup": _setup_snap(feat),
        "disclosures": discs,
        "earn_surprise_pct": worst,
    }


def freeze_value_snap(proposal, *, price: float | None, dossier: dict | None,
                      features: dict | None = None) -> dict:
    """밸류 채점 입력 + 사후 대조용 밸류 축 동결(스윙 freeze_snap 과 필드가 다르다).

    pb/roe/debt_ratio 는 지금 채점에 **쓰이지 않는다**(quality_tilt 가 composite_value
    안에서 이미 본다). 그래도 남기는 이유는, 나중에 코드 점수와 실현손익을 대조할 때
    "이 축을 따로 넣었어야 했나"를 되짚을 재료가 필요하기 때문이다.
    가중치 자동 갱신은 없다 — 채점 시점 입력이 그대로 남아 있어야 사후 분석이 된다.
    """
    feat = features if isinstance(features, dict) else {}
    d = dossier if isinstance(dossier, dict) else {}
    funds = d.get("fundamentals") if isinstance(d.get("fundamentals"), dict) else {}
    metrics = d.get("metrics") if isinstance(d.get("metrics"), dict) else {}
    news = [str(n["title"]) for n in (feat.get("news") or [])[:3]
            if isinstance(n, dict) and n.get("title")]
    px = _f(price)
    fair_low = _f(d.get("_fair_low"))
    margin = value_margin(px, fair_low)
    return {
        "price": px,
        "horizon": (getattr(proposal, "horizon", None) or "position"),
        "stance": d.get("stance"),
        "conviction_scan": _f(d.get("conviction")),
        "composite_value": _f(d.get("composite_value")),
        "value_factor": _f(d.get("value_factor")),
        "dd_component": _f(d.get("dd_component")),
        "quality_tilt": _f(d.get("quality_tilt")),
        "age_decay": _f(d.get("age_decay")),
        "fair_low": fair_low,
        "fair_low_pct": _f(d.get("fair_low_pct")),
        "fair_high_pct": _f(d.get("fair_high_pct")),
        "margin": round(margin, 4) if margin is not None else None,
        "scan_price": _f(metrics.get("price")),
        "drawdown_1y_pct": _f(metrics.get("drawdown_1y_pct")),
        "pb": _f(funds.get("pb")),
        "pe_trailing": _f(funds.get("pe_trailing")),
        "net_margin": _f(funds.get("net_margin")),
        "debt_ratio": _f(funds.get("debt_ratio")),
        "roe": _f(funds.get("roe")),
        "news": news,
    }


def apply_buy_conviction(decision, price_lookup: dict, brief_fn=None,
                         zone_tol: float = 0.005, features_by_sym: dict | None = None,
                         score_fn=None, snap_fn=None) -> dict:
    """BUY 확신도를 코드 점수로 덮어쓰고 채점 감사를 반환한다.

    score_fn/snap_fn 으로 트랙별 루브릭을 주입한다. 기본은 스윙(score_buy·freeze_snap),
    밸류는 score_value_buy·freeze_value_snap — 둘은 가산 축이 다르고 감점만 공유한다.
    """
    audit: dict = {}
    lookup = price_lookup or {}
    feats = features_by_sym or {}
    score = score_fn or score_buy
    snap = snap_fn or freeze_snap
    for p in decision.proposals:
        if getattr(p, "side", None) != "BUY":
            continue
        brief = brief_fn(p.symbol) if brief_fn else None
        feat = feats.get(p.symbol)
        px = lookup.get(p.symbol)
        sc = score(p, price=px, dossier=brief, zone_tol=zone_tol, features=feat)
        audit[p.symbol] = {"llm": sc.llm, "code": sc.value, "parts": sc.parts,
                           "snap": snap(p, price=px, dossier=brief, features=feat)}
        p.conviction = sc.value
    return audit
