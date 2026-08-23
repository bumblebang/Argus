"""공개 공유용 정적 페이지 — 화이트리스트 렌더 / 유출 가드 / 수집 단계 차단.

이 페이지는 2차 유포를 전제한 공유물이라 "무엇이 들어가나"보다 **"무엇이 절대 안 들어가나"**
가 계약이다. 그래서 렌더 결과뿐 아니라 `gather_public()` 이 돌려주는 dict 자체에 금액·수량
키가 없다는 것까지 고정한다(렌더 필터는 한 번 빠뜨리면 그대로 샌다).

네트워크·운영 파일 무접촉 — DB 는 tmp_path 에 스키마를 흉내낸 가짜를 만들어 쓴다.
"""
import json
import sqlite3
import time

import pytest

from scripts.public_page import (
    FOOTER_NOTE, assert_no_leak, gather_public, render_public,
)


# ── 픽스처: 화이트리스트 dict ───────────────────────────────────────
_NAMES = {"005930": "삼성전자", "194700": "노바렉스", "069500": "KODEX 200"}


def _d(**kw) -> dict:
    d = {
        "now": 1785500000.0, "db": True, "names": dict(_NAMES),
        "sentiment": {"fear_kr": {"score": 17.2, "rating": "extreme_fear",
                                  "components": {"drawdown": 0.0}}},
        "fear_history": {"kr": [[1, 20.0], [2, 17.2]]},
        "regime": {"KR": {"label": "risk_off", "breadth_above_ma20": 0.0, "n": 2,
                          "source": "index_proxy"},
                   "US": {"label": "risk_on", "breadth_above_ma20": 0.62, "n": 40,
                          "source": "universe"}},
        "holdings": [{"symbol": "194700", "market": "KR", "strategy": "value",
                      "avg_price": 13680.0, "last_price": 14100.0,
                      "ret_pct": 3.07, "stop_price": 10957.48,
                      "target_price": 15489.6}],
        "armed": [{"symbol": "005930", "market": "KR", "strategy": "breakout",
                   "avg_price": None, "stop_price": 61000.0, "target_price": 78000.0,
                   "entry_low": 65000.0, "entry_high": 67000.0}],
        "dossiers": [{"symbol": "005930", "stance": "bullish", "entry_low": 65000.0,
                      "entry_high": 67000.0, "invalidation": 61000.0,
                      "target": 78000.0, "rr": 2.4, "conviction": 0.71,
                      "age_h": 12.0}],
        "decisions": [{"ts": 1785499000.0, "symbol": "005930", "action": "BUY",
                       "conviction": 0.66, "verdict": "vetoed"}],
        "strategy_perf": [{"strategy": "value", "trades": 4, "wins": 3,
                           "win_rate": 0.75, "avg_ret_pct": 2.51}],
        "closed_trades": [{"symbol": "194700", "market": "KR", "strategy": "value",
                           "ret_pct": -1.23, "opened_at": 1785000000.0,
                           "closed_at": 1785400000.0}],
        "alpha": [{"market": "KR", "since": "07-01", "bot_ret_pct": 2.51,
                   "bench": "KODEX 200", "bench_ret_pct": 1.10, "alpha": 1.41}],
    }
    d.update(kw)
    return d


# ── 1) 화이트리스트가 실제로 렌더된다 ───────────────────────────────
def test_render_shows_whitelisted_content():
    h = render_public(_d())
    assert h.startswith("<!doctype html>") and h.endswith("</body></html>")
    # 섹션 제목
    for sec in ("시장 심리", "시장 국면", "보유 종목", "진입대기", "도시에",
                "성과", "최근 판단"):
        assert sec in h, sec
    # 보유: 종목명 · 전략 · 평가수익률% · 평단 · 현재가 · 손절 · 목표
    assert "노바렉스" in h and "value" in h
    assert "+3.07%" in h
    assert "13,680" in h and "14,100" in h and "10,957" in h and "15,490" in h
    # 진입대기: 진입존
    assert "삼성전자" in h and "65,000~67,000" in h
    # 도시에: stance 뱃지 · rr · 확신도 · 나이
    assert "class=b-bull" in h and "2.40" in h and "0.71" in h and "12h" in h
    # 성과: 거래수·승률·평균수익률·알파
    assert "75%" in h and "+2.51%" in h and "+1.41%p" in h and "-1.23%" in h
    # 국면
    assert "risk_off" in h and "index_proxy" in h and "n=2" in h
    assert "20일선 상회 62%" in h
    # 최근 판단
    assert "vetoed" in h and "0.66" in h


def test_render_never_carries_llm_free_text():
    """자유서술(시황 코멘트)은 조건부로 거르지 않고 **아예 싣지 않는다** — 패턴으로는
    "여력은 63만 정도" 같은 문장을 잡을 수 없어 안전을 보장할 수 없기 때문이다."""
    h = render_public(_d())
    assert "class=mv" not in h
    assert not assert_no_leak(h)


# ── 2) 유출 가드가 실제로 잡는다 ────────────────────────────────────
@pytest.mark.parametrize("bad, hint", [
    ("<p>총자산 ₩1,234,567</p>", "₩"),
    ("<p>총자산</p>", "총자산"),
    ("<p>평가금액 98,000</p>", "평가금액"),
    ("<p>보유 수량 12주</p>", "수량"),
    ("<p>매수여력 부족</p>", "매수여력"),
    ("<p>현금 63.7만원</p>", "현금"),
    ("<p>계좌 12345678901</p>", r"\b\d{11}\b"),
    ("<p>$1,234.56</p>", r"\$"),
    ('<p>{"target_weight": 0.2}</p>', "target_weight"),
    ('<p>{"notional": 500000}</p>', "notional"),
    ('<p>{"account_seq": 1}</p>', "account_seq"),
    ("<p>order_id=0000117</p>", "order_id"),
    (r"<p>C:\Users\Operator\argus</p>", "Users"),
    ("<p>/Users/x/argus</p>", "/Users/"),
    ("<p>AppData/Local</p>", "AppData"),
])
def test_assert_no_leak_catches(bad, hint):
    bad_list = assert_no_leak(bad)
    assert bad_list, f"{hint} 를 못 잡았다"
    assert any(hint in v for v in bad_list), bad_list


def test_assert_no_leak_allows_bare_prices():
    """가격은 통화기호 없이 숫자만 찍으므로 금액 패턴에 걸리지 않아야 한다."""
    assert assert_no_leak("<td class=mono>267,500</td><td class=mono>13,680.00</td>") == []


def test_footer_note_is_the_only_exemption():
    """각주는 '제외했다'는 설명이라 라벨 단어를 품는다 — 그것만 예외."""
    assert "수량" in FOOTER_NOTE and "계좌" in FOOTER_NOTE
    assert assert_no_leak(f"<div>{FOOTER_NOTE}</div>") == []
    assert assert_no_leak(f"<div>{FOOTER_NOTE}</div><div>보유 수량 3주</div>")


# ── 3) 정상 렌더는 위반 0 ───────────────────────────────────────────
def test_rendered_page_has_no_leak():
    assert assert_no_leak(render_public(_d())) == []
    assert assert_no_leak(render_public({})) == []


# ── 4) 수집 단계에서 차단된다(가짜 sqlite) ──────────────────────────
_BANNED_KEYS = {"qty", "pnl", "value", "cash", "meta", "payload"}

_SCHEMA = """
create table positions (id integer primary key, symbol text, market text, strategy text,
  state text, qty real, avg_price real, thesis text, target_price real, stop_price real,
  opened_at real, updated_at real, closed_at real, exit_price real, pnl real,
  exit_reason text, meta text);
create table events (id integer primary key, ts real, kind text, symbol text, payload text);
create table snapshots (id integer primary key, ts real, symbol text, price real,
  payload text);
create table dossiers (id integer primary key, symbol text, market text, created_at real,
  expires_at real, thesis text, entry_low real, entry_high real, invalidation real,
  target real, rr real, conviction real, evidence text, source text);
create table decisions (id integer primary key, ts real, symbol text, action text,
  conviction real, thesis text, verdict text, payload text);
"""


@pytest.fixture()
def fake_db(tmp_path, monkeypatch):
    """운영 bot.db 를 절대 열지 않는다 — 스키마만 흉내낸 tmp DB 로 갈아끼운다."""
    import scripts.public_page as pp
    now = time.time()
    db = tmp_path / "bot.db"
    con = sqlite3.connect(db)
    con.executescript(_SCHEMA)
    con.execute("insert into positions (symbol,market,strategy,state,qty,avg_price,"
                "stop_price,target_price,opened_at,updated_at,meta) values "
                "('194700','KR','value','open',7,13680,10957.48,15489.6,?,?,?)",
                (now - 8e5, now, json.dumps({"target_weight": 0.2})))
    con.execute("insert into positions (symbol,market,strategy,state,qty,avg_price,"
                "stop_price,target_price,opened_at,updated_at) values "
                "('005930','KR','breakout','armed',0,0,61000,78000,?,?)", (now, now))
    con.execute("insert into positions (symbol,market,strategy,state,qty,avg_price,"
                "exit_price,pnl,opened_at,updated_at,closed_at,exit_reason) values "
                "('069500','KR','value','closed',10,10000,11000,10000,?,?,?,'target_hit')",
                (now - 9e5, now, now - 1e5))
    con.execute("insert into snapshots (ts,symbol,price) values (?,'194700',14100)", (now,))
    con.execute("insert into events (ts,kind,symbol,payload) values (?,'cycle',null,?)",
                (now, json.dumps({"market_view": "표본이 작아 과신하지 않는다."})))
    con.execute("insert into dossiers (symbol,market,created_at,expires_at,entry_low,"
                "entry_high,invalidation,target,rr,conviction,evidence,thesis) values "
                "('005930','KR',?,?,65000,67000,61000,78000,2.4,0.71,?,'긴 논지')",
                (now - 3600, now + 3600, json.dumps({"stance": "bullish"})))
    con.execute("insert into decisions (ts,symbol,action,conviction,verdict,payload) "
                "values (?,'005930','BUY',0.66,'vetoed',?)",
                (now, json.dumps({"notional": 500000})))
    con.commit()
    con.close()
    monkeypatch.setattr(pp, "DB", db)
    monkeypatch.setattr(pp, "MARKET_STATE", tmp_path / "market_state.json")
    monkeypatch.setattr(pp, "FEAR_HISTORY", tmp_path / "fear_history.json")
    # 운영 브리핑 캐시도 안 읽는다 — 실제 산문이 섞이면 이 파일의 단정이 비결정적이 된다.
    monkeypatch.setattr(pp.public_brief, "BRIEF_PATH", tmp_path / "public_brief.json")
    monkeypatch.setattr(pp, "_load_names", lambda: dict(_NAMES))
    (tmp_path / "market_state.json").write_text(json.dumps({
        "regime": {"KR": {"label": "risk_off", "breadth_above_ma20": 0.0, "n": 2,
                          "source": "index_proxy"}},
        "sentiment": {"fear_kr": {"score": 17.2, "rating": "extreme_fear"}},
        "fx": {"USDKRW": 1400.0},
    }, ensure_ascii=False), encoding="utf-8")
    return db


def _keys(obj) -> set:
    if isinstance(obj, dict):
        out = set(obj)
        for v in obj.values():
            out |= _keys(v)
        return out
    if isinstance(obj, list):
        out = set()
        for v in obj:
            out |= _keys(v)
        return out
    return set()


def test_gather_public_never_carries_money_keys(fake_db):
    d = gather_public()
    json.dumps(d)                                   # 직렬화 가능해야 한다
    assert _BANNED_KEYS.isdisjoint(_keys(d)), sorted(_BANNED_KEYS & _keys(d))
    # 원문 payload/meta 값도 통째로 안 따라온다
    blob = json.dumps(d, ensure_ascii=False)
    assert "target_weight" not in blob and "notional" not in blob
    assert "긴 논지" not in blob                     # 도시에 thesis 자유서술 미포함


def test_gather_public_collects_whitelist(fake_db):
    d = gather_public()
    assert d["db"] is True
    assert [x["symbol"] for x in d["holdings"]] == ["194700"]
    h = d["holdings"][0]
    assert h["avg_price"] == 13680 and h["last_price"] == 14100
    assert h["ret_pct"] == pytest.approx((14100 - 13680) / 13680 * 100)
    assert [x["symbol"] for x in d["armed"]] == ["005930"]
    assert d["armed"][0]["entry_low"] == 65000        # 도시에에서 진입존을 물어온다
    assert d["dossiers"][0]["stance"] == "bullish"
    assert d["decisions"][0]["verdict"] == "vetoed"
    assert d["regime"]["KR"]["label"] == "risk_off"
    # 청산 1건: (11000-10000)/10000 = +10%. pnl·qty 를 안 읽고도 같은 값.
    assert d["closed_trades"][0]["ret_pct"] == pytest.approx(10.0)
    assert d["strategy_perf"][0]["win_rate"] == 1.0


def test_gather_public_end_to_end_has_no_leak(fake_db):
    assert assert_no_leak(render_public(gather_public())) == []


def test_gather_public_without_db(tmp_path, monkeypatch):
    import scripts.public_page as pp
    monkeypatch.setattr(pp, "DB", tmp_path / "nope.db")
    monkeypatch.setattr(pp, "MARKET_STATE", tmp_path / "nope.json")
    monkeypatch.setattr(pp, "FEAR_HISTORY", tmp_path / "nope2.json")
    monkeypatch.setattr(pp.public_brief, "BRIEF_PATH", tmp_path / "nope3.json")
    monkeypatch.setattr(pp, "_load_names", lambda: {})
    d = gather_public()
    assert d["db"] is False and d["holdings"] == []
    assert assert_no_leak(render_public(d)) == []


def test_gather_public_never_collects_market_view(fake_db):
    """실제로 관측된 케이스: 시황 코멘트가 'KR 현금 63.7만원' 을 언급한다.
    수집 단계에서 아예 안 담으므로 이런 문장이 있어도 페이지엔 흔적이 없다."""
    import scripts.public_page as pp
    con = sqlite3.connect(fake_db)
    con.execute("insert into events (ts,kind,payload) values (?,'cycle',?)",
                (time.time() + 10,
                 json.dumps({"market_view": "KR 현금 63.7만원으로 여력이 없다."},
                            ensure_ascii=False)))
    con.commit()
    con.close()
    d = pp.gather_public()
    assert "market_view" not in d
    assert "63.7" not in render_public(d)
    assert assert_no_leak(render_public(d)) == []


# ── 5) 자기완결성 ───────────────────────────────────────────────────
def test_page_is_self_contained():
    h = render_public(_d())
    for ref in ("http://", "https://", "<script src", "<link ", "<img ", "@import"):
        assert ref not in h, ref
    assert "<script" not in h                        # 인라인 JS 도 없다(정적 공유물)
    assert "http-equiv=refresh" not in h             # 자동 새로고침 금지


# ── 6) noindex ─────────────────────────────────────────────────────
def test_page_is_noindex():
    assert '<meta name="robots" content="noindex, nofollow">' in render_public(_d())


# ── 7) 빈 데이터에서도 죽지 않는다 ──────────────────────────────────
@pytest.mark.parametrize("d", [
    {}, {"db": False}, None, {"holdings": None, "dossiers": None, "regime": "쓰레기"},
    {"sentiment": "쓰레기", "fear_history": None, "decisions": [{"ts": None}]},
    {"holdings": [{"symbol": "x", "market": "US", "ret_pct": None}]},
])
def test_render_public_never_raises(d):
    h = render_public(d)
    assert h.startswith("<!doctype html>") and FOOTER_NOTE in h
