"""공시 워처 — 중대성 필터·기동 프라이밍·dedup·3단 라우팅·주기 테스트."""
import json
import time

import src.engine.disclosure as dmod
from src.engine.disclosure import DisclosureWatcher, is_material
from src.engine.store import Store


def _filing(rcept, sym="005930", name="유상증자결정", corp="삼성전자"):
    return {"rcept_no": rcept, "stock_code": sym, "corp_name": corp,
            "report_nm": name, "rcept_dt": "20260703"}


def _watcher(store, filings_ref, universe=("005930", "035720"), on_wake=None):
    return DisclosureWatcher(store, lambda: list(filings_ref),
                             lambda: set(universe), on_wake=on_wake)


# ── 중대성 필터 ────────────────────────────────────────────────────
def test_is_material_keywords():
    assert is_material("주요사항보고서(유상증자결정)") == "유상증자"
    assert is_material("단일판매ㆍ공급계약체결") in ("단일판매", "공급계약")
    assert is_material("연결재무제표기준영업(잠정)실적(공정공시)") == "영업(잠정)실적"
    assert is_material("기업설명회(IR)개최(안내공시)") is None      # 루틴 공시
    assert is_material(None) is None


# ── 프라이밍(재시작 폭주 방지) + dedup ─────────────────────────────
def test_first_poll_primes_without_waking(tmp_path):
    store = Store(tmp_path / "t.db")
    store.open_position("005930", "KR", 10, 70000)          # 보유 중이어도
    woke = []
    filings = [_filing("R1"), _filing("R2", sym="035720")]
    w = _watcher(store, filings, on_wake=lambda why, p: woke.append(p))
    res = w.poll_once()                                      # 첫 폴 = 마킹만
    assert res == {"new": 0, "woke": [], "queued": []} and woke == []
    res2 = w.poll_once()                                     # 같은 목록 재폴 = 신규 없음
    assert res2["new"] == 0


def test_dedup_by_rcept_no(tmp_path):
    store = Store(tmp_path / "t.db")
    filings = [_filing("R1")]
    w = _watcher(store, filings)
    w.poll_once()                                            # 프라이밍
    filings.append(_filing("R2", sym="999999"))              # 신규 1건(비유니버스)
    assert w.poll_once()["new"] == 1
    assert w.poll_once()["new"] == 0                         # 재폴 시 중복 무시


# ── 3단 라우팅 ─────────────────────────────────────────────────────
def test_routing_held_wakes_brain(tmp_path):
    store = Store(tmp_path / "t.db")
    store.open_position("005930", "KR", 10, 70000)
    woke = []
    filings = []
    w = _watcher(store, filings, on_wake=lambda why, p: woke.append((why, p)))
    w.poll_once()                                            # 프라이밍(빈 목록)
    filings.append(_filing("R1", sym="005930", name="유상증자결정"))
    res = w.poll_once()
    assert res["woke"] == ["005930"]
    assert woke and woke[0][0] == "disclosure"
    assert woke[0][1][0]["symbol"] == "005930"               # 공시 내용이 페이로드로
    ev = store.recent_events("disclosure", 0)[0]
    assert json.loads(ev["payload"])["route"] == "wake"


def test_routing_universe_queues_no_wake(tmp_path):
    store = Store(tmp_path / "t.db")                          # 보유 없음
    woke = []
    filings = []
    w = _watcher(store, filings, universe=("035720",),
                 on_wake=lambda why, p: woke.append(p))
    w.poll_once()
    filings.append(_filing("R1", sym="035720", name="단일판매ㆍ공급계약체결"))
    res = w.poll_once()
    assert res["queued"] == ["035720"] and res["woke"] == [] and woke == []
    ev = store.recent_events("disclosure", 0)[0]
    assert json.loads(ev["payload"])["route"] == "queue"


def test_routing_ignores_nonmaterial_and_outsiders(tmp_path):
    store = Store(tmp_path / "t.db")
    filings = []
    w = _watcher(store, filings, universe=("005930",))
    w.poll_once()
    filings += [_filing("R1", sym="005930", name="기업설명회(IR)개최"),   # 비중대
                _filing("R2", sym="999999", name="유상증자결정"),          # 남의 종목
                _filing("R3", sym="", name="유상증자결정")]                # 비상장
    res = w.poll_once()
    assert res["new"] == 3 and res["woke"] == [] and res["queued"] == []
    assert store.recent_events("disclosure", 0) == []        # 이벤트도 안 남김


def test_armed_symbol_also_wakes(tmp_path):
    store = Store(tmp_path / "t.db")
    store.arm_candidate("035720", "KR", strategy="rsi_reversion")   # 진입대기도 보호 대상
    woke = []
    filings = []
    w = _watcher(store, filings, on_wake=lambda why, p: woke.append(p))
    w.poll_once()
    filings.append(_filing("R1", sym="035720", name="감사의견비적정"))
    assert w.poll_once()["woke"] == ["035720"] and woke


# ── 주기(장중 10초 / 장외 완화) ────────────────────────────────────
def test_interval_adaptive(tmp_path, monkeypatch):
    store = Store(tmp_path / "t.db")
    w = _watcher(store, [])
    monkeypatch.setattr(dmod, "market_monitoring_active",
                        lambda market, **kw: True)
    assert w.interval() == 10.0
    monkeypatch.setattr(dmod, "market_monitoring_active",
                        lambda market, **kw: False)
    assert w.interval() == 60.0


# ── 뇌 컨텍스트 연결 ───────────────────────────────────────────────
def test_recent_disclosures_in_context(tmp_path):
    from src.agents.context import build_context
    store = Store(tmp_path / "t.db")
    store.log_event("disclosure", "005930",
                    {"report_nm": "유상증자결정", "keyword": "유상증자",
                     "route": "wake", "rcept_dt": "20260703"})
    rows = store.recent_events("disclosure", time.time() - 3600)
    assert len(rows) == 1
    ctx = json.loads(build_context({}, [], {}, {}, recent_disclosures=[
        {"symbol": "005930", "report_nm": "유상증자결정", "route": "wake"}]))
    assert ctx["recent_disclosures"][0]["symbol"] == "005930"
    assert "recent_disclosures" not in json.loads(build_context({}, [], {}, {}))
