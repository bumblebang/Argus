"""에이전트가 읽는 컴팩트 컨텍스트 조립.

market_state(시황·재무·수급·심리·매크로·뉴스) + 후보 종목 피처 + 포트폴리오 +
제약을 하나의 JSON 문자열로 만든다. '뇌'는 이걸 입력으로 받는다.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from ..strategies import strategy_catalog
from ..logging_setup import get_logger

log = get_logger("agents.context")

# 제목만이라 토큰 부담이 작다 — 배치 뉴스(~80건)를 거의 다 보게 100.
# 한도에 걸려 잘리면 ntfy 로 알린다(쿨다운으로 스팸 방지).
HEADLINE_LIMIT = 100
_TRIM_STATE = Path(__file__).resolve().parents[2] / "data" / "headline_trim_notify.json"
_TRIM_COOLDOWN_SEC = 6 * 3600


def _notify_headline_trim(total: int, limit: int) -> None:
    """헤드라인이 한도에 잘렸을 때 로그 + ntfy(토픽 없으면 로그만). 6h 쿨다운."""
    log.warning("헤드라인 한도 초과 — 전체 %d건 중 앞 %d건만 뇌에 전달", total, limit)
    now = time.time()
    try:
        prev = json.loads(_TRIM_STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        prev = {}
    last_ts = float(prev.get("ts") or 0)
    last_total = prev.get("total")
    if last_ts and (now - last_ts) < _TRIM_COOLDOWN_SEC and last_total == total:
        return
    topic = (os.getenv("NTFY_TOPIC") or "").strip()
    if topic:
        try:
            import requests
            msg = (f"뇌 headlines 한도 초과: 전체 {total}건 → {limit}건만 전달. "
                   f"한도 상향 또는 소스 축소를 검토하세요.")
            requests.post(f"https://ntfy.sh/{topic}",
                          data=msg.encode("utf-8"),
                          headers={"Title": "Argus headlines trim"},
                          timeout=5)
        except Exception as e:
            log.warning("헤드라인 잘림 ntfy 실패: %s", e)
    try:
        _TRIM_STATE.parent.mkdir(parents=True, exist_ok=True)
        _TRIM_STATE.write_text(json.dumps(
            {"ts": now, "total": total, "limit": limit}, ensure_ascii=False),
            encoding="utf-8")
    except OSError:
        pass


def _trim_news(news: list[dict], limit: int = HEADLINE_LIMIT) -> list[dict]:
    raw = news or []
    if len(raw) > limit:
        _notify_headline_trim(len(raw), limit)
    return [{"source": n.get("source"), "title": n.get("title"), "symbol": n.get("symbol")}
            for n in raw[:limit]]


def build_context(market_state: dict, candidates: list[dict], portfolio: dict,
                  constraints: dict, track_record: dict | None = None,
                  recent_disclosures: list[dict] | None = None,
                  earnings_results: list[dict] | None = None,
                  focus: dict | None = None,
                  wake: dict | None = None) -> str:
    """candidates: [{symbol,name,market,price,ma20,rsi,momentum,fundamentals,flows,news[],strategy_fit?}]

    track_record(선택): 라이브 성과 귀속(전략별 승률/최근 거래/결정 통계) — 뇌가 자기
    과거 판단의 실제 결과를 보고 다음 판단을 조정하게 하는 되먹임 입력.
    focus(선택): 주의층 렌즈(매크로 이벤트·수급 이상·포지셔닝 급변). 코드가 만든
    '오늘 무엇에 집중할지' — 없으면 평소처럼 regime·dossier·수급으로 판단.
    wake(선택): 이번 사이클을 깨운 사유(reason)와 트리거 요약 — periodic/vol_spike/
    regime_flip/disclosure 등. 없으면 정기 각성으로 보면 된다.
    """
    ms = market_state or {}
    ctx = {
        "asof": ms.get("asof"),
        "market": {
            "regime": ms.get("regime"),
            "sentiment": ms.get("sentiment"),
            "macro": ms.get("macro"),
            "macro_kr": ms.get("macro_kr"),
            "markets": ms.get("markets"),
            "sectors": ms.get("sectors"),
            "fx": ms.get("fx"),
            "flows_market": ms.get("flows_market"),
        },
        "headlines": _trim_news(ms.get("news", [])),
        # 뇌가 전략·파라미터를 고를 때 참고할 도구 출력(전략 카탈로그 + 후보별 strategy_fit).
        "strategies": strategy_catalog(),
        "candidates": candidates,
        "portfolio": portfolio,
        "constraints": constraints,
    }
    if focus:
        ctx["focus"] = focus
    if wake:
        ctx["wake"] = wake
    if track_record:
        ctx["track_record"] = track_record
    if recent_disclosures:
        ctx["recent_disclosures"] = recent_disclosures   # 워처가 잡은 최근 중대 공시
    if earnings_results:
        ctx["earnings_results"] = earnings_results       # 발표된 실적의 컨센서스 대비 편차
    return json.dumps(ctx, ensure_ascii=False, indent=2)
