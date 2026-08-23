"""공개 페이지용 '오늘의 국면' 코멘트 — 계좌 정보가 **없는** 컨텍스트로 별도 생성한다.

■ 왜 브레인의 market_view 를 그대로 쓰지 않는가
    공개 페이지에 브레인의 시황 코멘트를 실으면 안 된다. 브레인은 포트폴리오·현금이
    들어간 컨텍스트로 판단하므로 산문에 잔고가 섞여 나온다. 프롬프트로 금지해도
    "여력은 얼마 정도" 처럼 라벨도 금액기호도 없는 문장은 정규식으로 잡을 수 없다 —
    **자유서술은 필터로 안전을 보장할 수 없다.**

■ 해법: 입력에 없으면 출력에 못 나온다
    공개용 코멘트는 **계좌 정보가 아예 없는 컨텍스트**로 따로 생성한다. 모델이 잔고를
    언급하고 싶어도 모르는 상태로 둔다. `build_public_context()` 는 화이트리스트 6종
    (국면·심리·지수·섹터·헤드라인 제목·도시에 스탠스 분포)만 담고, portfolio/positions/
    cash/capital/constraints/track_record/account 같은 키는 **꺼내지도 않는다.**
    `assert_no_leak` 은 그 뒤의 백스톱으로만 남긴다(1차 방어선이 아니다).

■ 예산
    LLM 1콜/일. TTL(기본 20h) 안이면 재호출하지 않는다 — 페이지 생성은 하루에도 여러 번
    돌 수 있어야 하므로 **페이지 렌더 경로에서는 절대 LLM 을 부르지 않는다**(load_brief 만).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from pydantic import BaseModel, Field

from .config import ROOT
from .leakguard import assert_no_leak
from .logging_setup import get_logger

log = get_logger("public_brief")

BRIEF_PATH = ROOT / "data" / "public_brief.json"

HEADLINE_LIMIT = 12          # 컨텍스트에 싣는 뉴스 제목 수 상한


class PublicBrief(BaseModel):
    """공개판 국면 코멘트 — 에이전트 스키마가 아니라 **공개 공유물 전용**이라 여기 둔다.

    매매 판단에 쓰이지 않으므로 `src/agents/schemas.py` 에 넣지 않는다(계좌 컨텍스트가
    닿는 스키마들과 물리적으로 분리해 둔다는 뜻이기도 하다).
    """
    headline: str = Field(description="한 줄 요약(40자 이내 권장)")
    body: str = Field(description="3~5문장 산문 — 오늘 시장이 어떤 상태인지")
    watch: list[str] = Field(default_factory=list,
                             description="눈여겨볼 것 최대 3개(각 30자 이내)")


PUBLIC_BRIEF_SYSTEM = """\
당신은 자율 투자 에이전트 Argus 의 **공개 시황 브리핑**을 쓴다. 독자는 이 봇이 무엇을
보고 있는지 궁금해하는 친구들이다.

원칙:
- 주어진 데이터(시장 국면·공포탐욕지수·지수 등락·섹터·뉴스 헤드라인·도시에 스탠스 분포)
  **만** 근거로 오늘 시장이 어떤 상태인지 설명하라. 데이터에 없는 사실을 지어내지 마라.
- **수치를 인용하라.** "공포탐욕지수 39(공포)", "브레드스 26%", "코스피 +0.18%" 처럼
  구체적으로 쓴다. 막연한 형용사만 나열하지 마라.
- 공포지수는 **낮을수록 공포**다. fear_greed(CNN) 는 원점수 구간(25 미만 극단적 공포,
  75 초과 극단적 탐욕). fear_kr 은 합성 대리지표이고, rating_basis=percentile 이면
  등급이 우리 이력 대비(50=평년)다. incomplete 이거나 missing 이 있으면 성분 결측이니
  그 숫자를 확정 국면처럼 쓰지 마라. inputs.vkospi / put_call_ratio 가 있어도
  전일 KRX 부가일 뿐 score 에 섞인 값이 아니다.
- **브레드스(20일선 위 비율)를 인용하기 전에 regime 의 n 과 source 를 반드시 확인하라.**
  source=universe_live 는 유니버스 전 종목 실시간 집계(신뢰 가능)지만, source=index_proxy
  는 **지수 2개짜리 대략치**라 0%/50%/100% 세 값밖에 안 나온다. n 이 작은데(예: n=2)
  "0%" 를 그대로 인용하면 "단 한 종목도 20일선 위에 없다"는 실제와 다른 그림이 된다 —
  그런 경우엔 브레드스 수치를 인용하지 말고 국면 라벨과 다른 지표로 설명하거나,
  "장 마감 후라 대략치" 라는 단서를 붙여라.
- 도시에 스탠스 분포는 봇의 리서치가 지금 얼마나 강세/중립/약세로 기울어 있는지를
  보여주는 숫자다. 개별 종목명을 지어내지 말고 분포 그대로만 언급하라.
- 톤: 담백하게. 과장·훈계·이모지 금지.
- **투자 권유로 읽힐 표현을 쓰지 마라.** "사라", "지금이 기회다", "담아야 한다" 같은
  권유·선동은 금지다. 시장이 지금 어떤 상태인지 **설명만** 하라.
- **계좌·잔고·투자금액·보유수량을 언급하지 마라.** (데이터에 없지만 명시한다.)
- watch 에는 앞으로 며칠 지켜볼 지점을 최대 3개, 각 30자 이내로.
- headline 은 한 줄 요약(40자 이내), body 는 3~5문장 산문."""


# ── 컨텍스트 조립(화이트리스트 · 계좌 무접촉) ─────────────────────────

def _f(v):
    """수치 캐스팅 — 못 하면 None(0 으로 뭉개면 지표가 거짓말한다)."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _pick(src, keys: tuple[str, ...]) -> dict:
    """dict 에서 명시한 키만 뽑는다(없는 키는 생략) — 화이트리스트 방식의 유일한 통로."""
    if not isinstance(src, dict):
        return {}
    return {k: src[k] for k in keys if src.get(k) is not None}


def _sub(ms: dict, key: str) -> dict:
    """market_state 하위 dict — 값이 dict 가 아니면 빈 dict(깨진 상태 파일에도 안 죽는다)."""
    v = ms.get(key)
    return v if isinstance(v, dict) else {}


def _regime(ms: dict) -> dict:
    """시장별 국면 — label / breadth_above_ma20 / n / source 만."""
    out: dict = {}
    for m, r in _sub(ms, "regime").items():
        if isinstance(r, dict):
            out[str(m)] = _pick(r, ("label", "breadth_above_ma20", "n", "source"))
    return out


def _sentiment(ms: dict) -> dict:
    """시장 심리 — 공포탐욕(US/KR)과 VIX. 계좌와 무관한 순수 시장 지표."""
    s = ms.get("sentiment")
    if not isinstance(s, dict):
        return {}
    out: dict = {}
    fg = _pick(s.get("fear_greed"), ("score", "rating", "components",
                                     "prev_close", "prev_1w", "prev_1m", "prev_1y"))
    if fg:
        out["fear_greed"] = fg
    fk = _pick(s.get("fear_kr"), ("score", "rating", "components", "inputs",
                                  "incomplete", "missing", "rating_basis", "score_pct"))
    if fk:
        out["fear_kr"] = fk
    for k in ("vix", "vix_label"):
        if s.get(k) is not None:
            out[k] = s[k]
    return out


def _markets(ms: dict) -> dict:
    """지수·원자재·환율의 종가와 1일 등락 — 시장 가격이라 공개 가능."""
    out: dict = {}
    for name, v in _sub(ms, "markets").items():
        if not isinstance(v, dict):
            continue
        row = {"last": _f(v.get("last")), "chg_1d": _f(v.get("chg_1d"))}
        if row["last"] is not None or row["chg_1d"] is not None:
            out[str(name)] = row
    return out


def _sectors(ms: dict) -> dict:
    """주도/소외 섹터만(by_sector 전체는 산문에 필요 없다)."""
    out: dict = {}
    for m, v in _sub(ms, "sectors").items():
        if not isinstance(v, dict):
            continue
        row = _pick(v, ("leaders", "laggards"))
        if row:
            out[str(m)] = row
    return out


def _headlines(ms: dict) -> list[dict]:
    """뉴스 **제목만** 최대 12개. url·published 는 담지 않는다(산문 근거로 불필요)."""
    raw = ms.get("news")
    if not isinstance(raw, list):
        raw = ms.get("headlines")
    out: list[dict] = []
    for n in (raw or []):
        if not isinstance(n, dict) or not n.get("title"):
            continue
        item = {"title": str(n["title"])}
        if n.get("source"):
            item["source"] = str(n["source"])
        if n.get("symbol"):
            item["symbol"] = str(n["symbol"])
        out.append(item)
        if len(out) >= HEADLINE_LIMIT:
            break
    return out


def build_public_context(market_state: dict, dossier_stances: dict | None = None) -> dict:
    """공개용 브리핑 컨텍스트 — **넣는 것만 명시적으로 담는다.**

    market_state 에서 꺼내는 것은 regime / sentiment / markets / sectors / news 뿐이다.
    fundamentals·flows·macro·sessions 는 필요 없어 제외한다. portfolio/positions/holdings/
    cash/capital/constraints/track_record/account 계열은 **꺼내는 코드 자체가 없다** —
    모델이 잔고를 언급하고 싶어도 알 수 없는 상태로 두는 것이 이 함수의 목적이다.
    """
    ms = market_state if isinstance(market_state, dict) else {}
    ctx: dict = {
        "asof": str(ms.get("asof") or ""),
        "regime": _regime(ms),
        "sentiment": _sentiment(ms),
        "markets": _markets(ms),
        "sectors": _sectors(ms),
        "headlines": _headlines(ms),
    }
    # 도시에 스탠스는 **개수 분포만**(종목·가격·확신도는 담지 않는다).
    if isinstance(dossier_stances, dict) and dossier_stances:
        ctx["dossier_stances"] = {str(k): int(v) for k, v in dossier_stances.items()}
    return ctx


# ── 생성 + 캐시 ────────────────────────────────────────────────────

def _brief_text(headline: str, body: str, watch: list) -> str:
    """가드에 넣을 브리핑 전문(headline+body+watch) — 한 조각도 빠뜨리지 않게 한 곳에서."""
    return "\n".join([str(headline or ""), str(body or ""),
                      *[str(w) for w in (watch or [])]])


def generate(llm, market_state: dict, dossier_stances: dict | None = None, *,
             path: str | Path = BRIEF_PATH, now_fn=time.time) -> dict | None:
    """LLM 1콜로 공개용 국면 코멘트를 만들어 캐시에 원자적으로 저장하고 그 dict 를 반환.

    유출 가드(백스톱)에 걸리면 **캐시하지 않고 None** — 한 번 새면 회수가 안 되므로
    "의심스러우면 안 싣는다"가 기본값이다. LLM 예외도 삼키고 None(페이지 생성이
    이것 때문에 죽으면 안 된다).
    """
    ctx = build_public_context(market_state, dossier_stances)
    try:
        out = llm.structured(PUBLIC_BRIEF_SYSTEM,
                             json.dumps(ctx, ensure_ascii=False), PublicBrief)
    except Exception as e:
        log.warning("[brief] 생성 실패(브리핑 없이 진행): %s", e)
        return None

    watch = [str(w) for w in (out.watch or [])][:3]
    bad = assert_no_leak(_brief_text(out.headline, out.body, watch))
    if bad:
        log.error("[brief] 유출 가드 위반 %d건 — 캐시하지 않고 폐기합니다: %s",
                  len(bad), " | ".join(bad))
        return None

    brief = {
        "ts": now_fn(),
        "market_state_asof": str((market_state or {}).get("asof") or ""),
        "headline": out.headline,
        "body": out.body,
        "watch": watch,
    }
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")          # 원자적 쓰기(부분 파일 노출 방지)
    tmp.write_text(json.dumps(brief, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)
    log.info("[brief] 생성: %s", out.headline)
    return brief


def load_brief(path: str | Path = BRIEF_PATH, ttl_hours: float = 20.0,
               now_fn=time.time) -> dict | None:
    """캐시된 브리핑 — 없거나 TTL(기본 20h)을 넘겼거나 깨졌으면 None(예외 없음).

    TTL 은 '하루 1콜' 보장 장치이기도 하다. 페이지 생성기는 이 함수만 쓴다(LLM 미접촉).
    """
    p = Path(path)
    try:
        if not p.exists():
            return None
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log.warning("[brief] 캐시 로드 실패(브리핑 없이 진행): %s", e)
        return None
    if not isinstance(d, dict):
        return None
    ts = _f(d.get("ts"))
    if ts is None or (now_fn() - ts) > ttl_hours * 3600:
        return None
    return d
