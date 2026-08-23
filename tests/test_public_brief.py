"""공개용 '오늘의 국면' 코멘트 — 계좌 무접촉 컨텍스트 / 가드 백스톱 / 캐시 TTL / 렌더.

이 기능의 계약은 "브리핑 산문에 자산 규모가 절대 안 나온다" 이고, 그 보장은 **필터가
아니라 입력**에서 온다(계좌 정보가 없는 컨텍스트로 생성). 그래서 여기서는 컨텍스트
조립이 계좌 키를 애초에 담지 않는다는 것을 먼저 고정하고, 그 다음에 가드가 백스톱으로
동작하는지를 고정한다.

네트워크·실제 LLM 0 — 가짜 llm 과 tmp_path 만 쓴다.
"""
import json
import time

import pytest

from src.public_brief import (
    PublicBrief, build_public_context, generate, load_brief,
)


# ── 픽스처 ─────────────────────────────────────────────────────────

def _market_state(**kw) -> dict:
    """화이트리스트 6종 + **계좌 오염물**이 섞인 market_state(현실보다 더 나쁜 입력)."""
    d = {
        "asof": "2026-07-31T12:09:19+00:00",
        "regime": {"KR": {"label": "risk_off", "breadth_above_ma20": 0.26, "n": 2,
                          "source": "index_proxy"},
                   "US": {"label": "risk_on", "breadth_above_ma20": 0.62, "n": 40,
                          "source": "universe"}},
        "sentiment": {
            "vix": 16.94, "vix_label": "normal",
            "fear_greed": {"score": 39.0, "rating": "fear", "prev_close": 38.9,
                           "prev_1w": 41.3, "prev_1m": 30.0, "prev_1y": 63.7,
                           "components": {"put_call_options": 30.2}},
            "fear_kr": {"score": 17.2, "rating": "extreme_fear",
                        "components": {"drawdown": 0.0, "ret_5d": 43.0},
                        "inputs": {"index_drawdown_pct": -18.5},
                        "incomplete": True, "missing": ["breadth"],
                        "rating_basis": "percentile", "score_pct": 12.0},
        },
        "markets": {"KOSPI": {"last": 6595.45, "chg_1d": 0.1791},
                    "SP500": {"last": 7437.63, "chg_1d": 0.0166}},
        "sectors": {"KR": {"by_sector": {"은행": 0.03}, "leaders": ["은행", "자동차"],
                           "laggards": ["반도체"]}},
        "news": [{"source": "한국경제", "title": f"헤드라인 {i}",
                  "url": "https://example.com", "published": "Fri, 31 Jul 2026"}
                 for i in range(20)],
        # ── 아래는 전부 공개 컨텍스트에 들어오면 안 되는 것들 ──
        "portfolio": {"cash": 888000, "total_value": 12345678},
        "positions": [{"symbol": "005930", "qty": 7, "avg_price": 13680}],
        "holdings": [{"symbol": "194700", "qty": 3}],
        "cash": 888000,
        "capital": {"KR": 3000000},
        "constraints": {"max_notional": 500000},
        "track_record": {"pnl": 123456},
        "account": {"account_no": "12345678901"},
        "paper_account": {"cash": 1000000},
        "fundamentals": {"005930": {"revenue": 333605938000000.0}},
        "flows": {"KR": {"foreign_net": -123456789}},
    }
    d.update(kw)
    return d


def _brief() -> PublicBrief:
    return PublicBrief(headline="공포 구간의 관망 국면",
                       body="공포탐욕지수 39(공포)로 내려왔고 KR 브레드스는 26%다. "
                            "코스피는 +0.18%로 보합권에 머물렀다. 섹터는 은행·자동차가 "
                            "이끌고 반도체가 밀렸다.",
                       watch=["브레드스 26% 회복 여부", "VIX 17선"])


class _LLM:
    """호출을 기록하는 가짜 LLM. out 이 예외면 던진다."""

    def __init__(self, out):
        self.out = out
        self.calls = []

    def structured(self, system, user, schema):
        self.calls.append((system, user, schema))
        if isinstance(self.out, Exception):
            raise self.out
        return self.out


# ── 1) 계좌 키는 컨텍스트에 애초에 없다 ─────────────────────────────
_ACCOUNT_KEYS = ("portfolio", "positions", "holdings", "cash", "capital",
                 "constraints", "track_record", "account", "paper_account",
                 "qty", "avg_price", "notional", "pnl", "account_no",
                 "fundamentals", "flows")
_ACCOUNT_VALUES = ("888000", "12345678", "12345678901", "3000000", "500000",
                   "123456", "333605938000000")


def test_context_never_carries_account_keys():
    blob = json.dumps(build_public_context(_market_state(), {"bullish": 3}),
                      ensure_ascii=False)
    for k in _ACCOUNT_KEYS:
        assert k not in blob, k
    for v in _ACCOUNT_VALUES:
        assert v not in blob, v


def test_context_collects_whitelist():
    ctx = build_public_context(_market_state(), {"bullish": 3, "neutral": 18,
                                                 "bearish": 3})
    assert set(ctx) == {"asof", "regime", "sentiment", "markets", "sectors",
                        "headlines", "dossier_stances"}
    assert ctx["regime"]["KR"] == {"label": "risk_off", "breadth_above_ma20": 0.26,
                                   "n": 2, "source": "index_proxy"}
    assert ctx["sentiment"]["fear_greed"]["score"] == 39.0
    assert ctx["sentiment"]["fear_greed"]["prev_1y"] == 63.7
    assert ctx["sentiment"]["fear_kr"]["rating"] == "extreme_fear"
    assert ctx["sentiment"]["fear_kr"]["inputs"]["index_drawdown_pct"] == -18.5
    assert ctx["sentiment"]["fear_kr"]["incomplete"] is True
    assert ctx["sentiment"]["fear_kr"]["missing"] == ["breadth"]
    assert ctx["sentiment"]["fear_kr"]["rating_basis"] == "percentile"
    assert ctx["sentiment"]["fear_kr"]["score_pct"] == 12.0
    assert ctx["sentiment"]["vix"] == 16.94 and ctx["sentiment"]["vix_label"] == "normal"
    assert ctx["markets"]["KOSPI"] == {"last": 6595.45, "chg_1d": 0.1791}
    assert ctx["sectors"]["KR"] == {"leaders": ["은행", "자동차"], "laggards": ["반도체"]}
    assert "by_sector" not in ctx["sectors"]["KR"]
    # 헤드라인은 **제목만** 최대 12개(url·published 는 담지 않는다)
    assert len(ctx["headlines"]) == 12
    assert set(ctx["headlines"][0]) == {"title", "source"}
    assert ctx["dossier_stances"] == {"bullish": 3, "neutral": 18, "bearish": 3}


def test_context_survives_garbage():
    """market_state 가 비어도/쓰레기여도 예외 없이 얇은 컨텍스트를 낸다."""
    for ms in ({}, None, {"regime": "쓰레기", "sentiment": None, "news": "쓰레기"}):
        ctx = build_public_context(ms)
        assert ctx["regime"] == {} and ctx["headlines"] == []


# ── 2) 생성 + 원자적 캐시 ───────────────────────────────────────────
def test_generate_writes_cache_atomically(tmp_path):
    p = tmp_path / "public_brief.json"
    llm = _LLM(_brief())
    out = generate(llm, _market_state(), {"bullish": 3}, path=p,
                   now_fn=lambda: 1785500000.0)
    assert out["headline"] == "공포 구간의 관망 국면"
    assert out["watch"] == ["브레드스 26% 회복 여부", "VIX 17선"]
    assert out["ts"] == 1785500000.0
    assert out["market_state_asof"] == "2026-07-31T12:09:19+00:00"
    assert json.loads(p.read_text(encoding="utf-8")) == out
    assert not list(tmp_path.glob("*.tmp"))          # tmp 잔재 없음(replace 완료)
    # 계좌 정보는 프롬프트에도 안 들어간다
    assert "888000" not in llm.calls[0][1]


# ── 3) 가드 백스톱: 새는 문장이면 캐시하지 않는다 ───────────────────
@pytest.mark.parametrize("bad", [
    PublicBrief(headline="ok", body="KR 현금 63.7만원 남았다.", watch=[]),
    PublicBrief(headline="계좌 상태 점검", body="담백한 산문.", watch=[]),
    PublicBrief(headline="ok", body="담백한 산문.", watch=["보유 수량 조정"]),
])
def test_generate_drops_leaking_brief(tmp_path, bad):
    p = tmp_path / "public_brief.json"
    assert generate(_LLM(bad), _market_state(), path=p) is None
    assert not p.exists()                            # 캐시 오염 없음


# ── 4) LLM 예외는 삼킨다(페이지 생성이 이것 때문에 죽으면 안 된다) ──
def test_generate_swallows_llm_error(tmp_path):
    p = tmp_path / "public_brief.json"
    assert generate(_LLM(RuntimeError("사용량 한도")), _market_state(), path=p) is None
    assert not p.exists()


# ── 5) load_brief: TTL / 부재 / 손상 ────────────────────────────────
def test_load_brief_ttl(tmp_path):
    p = tmp_path / "public_brief.json"
    now = 1785500000.0
    generate(_LLM(_brief()), _market_state(), path=p, now_fn=lambda: now)
    assert load_brief(p, ttl_hours=20, now_fn=lambda: now + 19 * 3600) is not None
    assert load_brief(p, ttl_hours=20, now_fn=lambda: now + 21 * 3600) is None


def test_load_brief_missing_or_corrupt(tmp_path):
    assert load_brief(tmp_path / "nope.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{깨진 json", encoding="utf-8")
    assert load_brief(bad) is None                   # 예외가 밖으로 안 나온다
    nots = tmp_path / "nots.json"
    nots.write_text('{"headline": "x"}', encoding="utf-8")
    assert load_brief(nots) is None                  # ts 없으면 신뢰할 수 없다


# ── 6) 페이지 렌더 ─────────────────────────────────────────────────
def _page_d(brief) -> dict:
    return {"now": 1785500000.0, "db": True, "names": {}, "sentiment": {},
            "fear_history": {}, "regime": {}, "holdings": [], "armed": [],
            "dossiers": [], "decisions": [], "strategy_perf": [],
            "closed_trades": [], "alpha": [], "brief": brief}


def test_page_renders_brief_section():
    from scripts.public_page import assert_no_leak, render_public
    b = _brief().model_dump()
    b["ts"] = 1785500000.0
    h = render_public(_page_d(b))
    assert "오늘의 국면" in h
    assert "공포 구간의 관망 국면" in h
    assert "공포탐욕지수 39(공포)" in h
    assert "브레드스 26% 회복 여부" in h
    assert "class=mv" in h
    assert assert_no_leak(h) == []


def test_page_omits_section_without_brief():
    from scripts.public_page import render_public
    for brief in (None, {}, "쓰레기", {"headline": "", "body": ""}):
        h = render_public(_page_d(brief))
        assert "오늘의 국면" not in h


def test_page_drops_only_the_leaking_section():
    """가드에 걸리면 그 섹션만 빠지고 **페이지는 계속 만들어진다**."""
    from scripts.public_page import FOOTER_NOTE, assert_no_leak, render_public
    b = {"ts": 1785500000.0, "headline": "관망 국면",
         "body": "KR 현금 63.7만원으로 여력이 없다.", "watch": []}
    h = render_public(_page_d(b))
    assert "오늘의 국면" not in h and "63.7" not in h
    assert h.startswith("<!doctype html>") and FOOTER_NOTE in h
    assert assert_no_leak(h) == []


# ── 7) 페이지 수집 경로는 LLM 을 절대 안 부른다 ─────────────────────
def test_gather_public_never_calls_llm(tmp_path, monkeypatch):
    """페이지는 하루에도 여러 번 만들 수 있어야 하므로 렌더 경로는 LLM 0콜이 계약이다."""
    import scripts.public_page as pp
    import src.public_brief as pb

    def _boom(*a, **kw):
        raise AssertionError("gather_public 이 LLM 을 불렀다")

    monkeypatch.setattr(pb, "generate", _boom)
    monkeypatch.setattr(pp.public_brief, "generate", _boom)
    monkeypatch.setattr(pp, "DB", tmp_path / "nope.db")
    monkeypatch.setattr(pp, "MARKET_STATE", tmp_path / "nope.json")
    monkeypatch.setattr(pp, "FEAR_HISTORY", tmp_path / "nope2.json")
    monkeypatch.setattr(pp, "_load_names", lambda: {})
    monkeypatch.setattr(pp, "_value_names", lambda: {})
    monkeypatch.setattr(pp.public_brief, "BRIEF_PATH", tmp_path / "nope3.json")
    monkeypatch.setattr(pp.public_brief, "load_brief",
                        lambda **kw: {"ts": time.time(), "headline": "h",
                                      "body": "b", "watch": []})
    d = pp.gather_public()
    assert d["brief"]["headline"] == "h"
    assert "오늘의 국면" in pp.render_public(d)
