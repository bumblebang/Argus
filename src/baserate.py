"""베이스레이트 마이닝 — "이 종목, 이 셋업의 과거 승률/수익폭"을 숫자로.

"반등지점을 찾는다"를 조건부 확률로 번역한다: 과거 캔들에서 셋업(조건 조합)이
발동했던 모든 시점을 찾아, 이후 N봉 수익률 분포(승률·평균·중앙값)를 계산한다.
뇌(결정·검증)와 Athena(딥리서치)가 이 통계를 근거로 쓴다 — 느낌이 아니라 숫자.

전부 순수 계산(pandas) — 네트워크·LLM 없음. 데이터는 호출측이 주입한다.

셋업 함수 규약: (df: OHLCV DataFrame) -> pd.Series[bool] (각 봉에서 셋업 발동 여부,
인덱스는 df 와 동일). 마지막 봉 True = "지금 이 셋업 상태" → active_now.
"""
from __future__ import annotations

import pandas as pd

from .indicators import rsi, sma
from .gap_rebound_features import gap_close_scan_mask
from .logging_setup import get_logger

log = get_logger("baserate")

# 앞뒤 봉이 필요한 셋업들의 최소 캔들 수(지표 안정화 포함)
MIN_BARS = 60
# 이 표본 미만 통계는 small_sample 로 표시(과신 금지)
MIN_SAMPLE = 10
# 수익률 측정 지평(봉 수). 일봉이면 1주/2주/1개월.
HORIZONS = (5, 10, 20)


# ── 셋업 정의 ──────────────────────────────────────────────────────
def pullback_reversal(df: pd.DataFrame) -> pd.Series:
    """급락 후 과매도 반등 후보: 10봉 수익률 ≤ -12% AND RSI(14) < 32."""
    close = df["close"].astype(float)
    ret10 = close / close.shift(10) - 1
    return (ret10 <= -0.12) & (rsi(close, 14) < 32)


def breakout_pullback(df: pd.DataFrame) -> pd.Series:
    """돌파 후 눌림: 최근 7봉 내 20봉 신고가 갱신 후, 현재가가 돌파 레벨 ±3% 눌림.

    '달리는 종목의 첫 쉼표'를 잡는다 — 추격 매수보다 유리한 진입 자리 후보.
    """
    close = df["close"].astype(float)
    high20 = close.rolling(20).max()
    broke = close >= high20                                   # 그 봉이 20봉 최고가
    broke_recent = broke.rolling(7).max().fillna(0) > 0       # 최근 7봉 내 돌파 있었나
    level = close.where(broke).ffill()                        # 마지막 돌파가
    near_level = (close / level - 1).abs() <= 0.03
    return broke_recent & near_level & ~broke                 # 돌파봉 자체는 제외(눌림만)


def golden_cross(df: pd.DataFrame) -> pd.Series:
    """골든크로스 초입: SMA5 가 SMA20 을 이번/직전 봉에 상향 돌파."""
    close = df["close"].astype(float)
    s5, s20 = sma(close, 5), sma(close, 20)
    cross = (s5 > s20) & (s5.shift(1) <= s20.shift(1))
    return cross | cross.shift(1).fillna(False)


def gap_close_scan(df: pd.DataFrame) -> pd.Series:
    """갭반등 close_scan: 장중 -5% & 종가가 당일 레인지 하단(프록시)."""
    return gap_close_scan_mask(df)


SETUPS = {
    "pullback_reversal": pullback_reversal,
    "breakout_pullback": breakout_pullback,
    "golden_cross": golden_cross,
    "gap_close_scan": gap_close_scan,
}

# 익일 시가 청산 셋업 — setup_stats 의 exit_mode=next_open
OVERNIGHT_OPEN_SETUPS = frozenset({"gap_close_scan"})


# ── 통계 ───────────────────────────────────────────────────────────
def setup_stats(df: pd.DataFrame, fired: pd.Series,
                horizons: tuple[int, ...] = HORIZONS,
                *, exit_mode: str = "close") -> dict:
    """셋업 발동 시점들의 이후 N봉 수익률 분포. {h: {n, win_rate, avg_ret, med_ret}}.

    exit_mode=next_open 이면 종가 진입·익일 시가 청산(갭반등 close_scan). h=1만 의미 있음.
    같은 신호가 연속 봉에서 반복 발동하면 첫 봉만 센다(에피소드 dedup).
    """
    close = df["close"].astype(float)
    open_ = df["open"].astype(float) if "open" in df.columns else close
    # bool dtype 강제: object dtype 이면 ~ 가 비트반전(-1/-2=truthy)으로 동작해 dedup 이 깨진다.
    fired = fired.fillna(False).astype(bool)
    episode_start = fired & ~fired.shift(1, fill_value=False)
    idx = [i for i, v in enumerate(episode_start.tolist()) if v]
    out: dict = {}
    for h in horizons:
        rets = []
        for i in idx:
            if exit_mode == "next_open":
                j = i + h
                if j >= len(open_):
                    continue
                entry = close.iloc[i]
                exit_px = open_.iloc[j]
            else:
                j = i + h
                if j >= len(close):
                    continue
                entry = close.iloc[i]
                exit_px = close.iloc[j]
            if entry:
                rets.append(exit_px / entry - 1)
        n = len(rets)
        s = pd.Series(rets)
        out[str(h)] = {           # 키는 문자열 — JSON 왕복(배치 파일) 후에도 동일 접근
            "n": n,
            "win_rate": round(float((s > 0).mean()), 2) if n else None,
            "avg_ret_pct": round(float(s.mean()) * 100, 2) if n else None,
            "med_ret_pct": round(float(s.median()) * 100, 2) if n else None,
        }
    return out


def analyze(df: pd.DataFrame, setups: dict | None = None) -> dict:
    """한 종목의 전 셋업 베이스레이트 + 현재 활성 셋업.

    반환: {"setups": {name: {"active_now": bool, "stats": {h: {...}}, "small_sample": bool}},
           "active_now": [지금 발동 중인 셋업 이름들]}
    """
    setups = setups or SETUPS
    if df is None or len(df) < MIN_BARS:
        return {"setups": {}, "active_now": []}
    result: dict = {"setups": {}, "active_now": []}
    for name, fn in setups.items():
        try:
            fired = fn(df).fillna(False)
        except Exception as e:                 # 셋업 하나의 실패가 전체를 막지 않게
            log.warning("셋업 %s 계산 실패: %s", name, e)
            continue
        stats = setup_stats(
            df, fired,
            horizons=(1, 5, 10) if name in OVERNIGHT_OPEN_SETUPS else HORIZONS,
            exit_mode="next_open" if name in OVERNIGHT_OPEN_SETUPS else "close",
        )
        max_n = max((v["n"] for v in stats.values()), default=0)
        active = bool(fired.iloc[-1])
        result["setups"][name] = {"active_now": active, "stats": stats,
                                  "small_sample": max_n < MIN_SAMPLE}
        if active:
            result["active_now"].append(name)
    return result


def brief(analysis: dict) -> dict | None:
    """뇌 컨텍스트용 압축본 — 지금 활성인 셋업만, 대표 지평(10봉) 중심.

    활성 셋업이 없으면 None(컨텍스트에 싣지 않아 토큰 절약).
    """
    active = analysis.get("active_now") or []
    if not active:
        return None
    out = {}
    for name in active:
        info = analysis["setups"].get(name, {})
        stats = info.get("stats", {})
        rep = stats.get("1") or stats.get("10") or next(iter(stats.values()), {})
        out[name] = {"n": rep.get("n"), "win_rate": rep.get("win_rate"),
                     "avg_ret_pct": rep.get("avg_ret_pct"),
                     "small_sample": info.get("small_sample", True),
                     "horizons": stats}
    return out
