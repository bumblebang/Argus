"""실적 결과(서프라이즈) 루프 — 정규화·프라이밍·dedup·3단 라우팅·주기·뇌 연결(네트워크 없음)."""
from __future__ import annotations

import json
from datetime import date, timedelta

import src.datasources.earnings as em
from src.engine.earnings_watch import EarningsResultWatcher
from src.engine.store import Store


class _Resp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


def _cal(rows):
    return _Resp({"earningsCalendar": rows})


def _row(sym, days=0, *, eps_e=1.0, eps_a=1.1, rev_e=1000.0, rev_a=1100.0,
         q=2, year=2026, hour="bmo"):
    return {"symbol": sym, "date": (date.today() + timedelta(days=days)).isoformat(),
            "hour": hour, "quarter": q, "year": year,
            "epsEstimate": eps_e, "epsActual": eps_a,
            "revenueEstimate": rev_e, "revenueActual": rev_a}


def _result(sym="AAPL", days=0, **kw):
    """워처에 먹일 표준형(fetch_us_results 출력과 같은 모양)."""
    return em._us_result_to_std(_row(sym, days, **kw))


def _watcher(store, results_ref, universe=("NVDA",), **kw):
    return EarningsResultWatcher(store, lambda: dict(results_ref),
                                 lambda: set(universe), **kw)


# ── fetch_us_results 정규화 ────────────────────────────────────────
class TestFetchUsResults:
    def test_actual_있는행만_추리고_서프라이즈_계산(self, monkeypatch):
        rows = [_row("AAPL", -1, eps_e=1.5, eps_a=1.65, rev_e=1000.0, rev_a=950.0),
                # 아직 발표 전(actual 전무) → 결과가 아니다
                {"symbol": "NVDA", "date": date.today().isoformat(), "hour": "amc",
                 "quarter": 2, "year": 2026, "epsEstimate": 1.0,
                 "epsActual": None, "revenueActual": None},
                _row("ZZZZ", -1)]                       # 대상 심볼 밖 → 제외
        monkeypatch.setattr(em.requests, "get", lambda *a, **k: _cal(rows))
        out = em.fetch_us_results("KEY", ["AAPL", "NVDA"])
        assert set(out) == {"AAPL"}
        a = out["AAPL"]
        assert a["eps_actual"] == 1.65 and a["eps_estimate"] == 1.5
        assert a["eps_surprise_pct"] == 10.0            # (1.65-1.5)/1.5*100
        assert a["revenue_surprise_pct"] == -5.0        # (950-1000)/1000*100
        assert a["quarter"] == 2 and a["year"] == 2026 and a["hour"] == "bmo"

    def test_컨센서스_0또는None이면_서프라이즈_None(self, monkeypatch):
        """0 나눗셈 금지 — 계산 불가면 None. 추정하거나 0으로 채우지 않는다."""
        rows = [_row("A", 0, eps_e=0, eps_a=0.3, rev_e=None, rev_a=500.0),
                _row("B", 0, eps_e=None, eps_a=-0.2, rev_e=0, rev_a=100.0)]
        monkeypatch.setattr(em.requests, "get", lambda *a, **k: _cal(rows))
        out = em.fetch_us_results("KEY", ["A", "B"])
        assert out["A"]["eps_surprise_pct"] is None      # estimate=0
        assert out["A"]["revenue_surprise_pct"] is None  # estimate=None
        assert out["A"]["eps_actual"] == 0.3             # 실제값 자체는 살린다
        assert out["B"]["eps_surprise_pct"] is None
        assert out["B"]["revenue_surprise_pct"] is None

    def test_적자컨센서스도_부호대로_계산(self, monkeypatch):
        # estimate=-0.5, actual=-0.2 → 손실 축소 = 상회(+60%)
        rows = [_row("C", 0, eps_e=-0.5, eps_a=-0.2, rev_e=None, rev_a=None)]
        monkeypatch.setattr(em.requests, "get", lambda *a, **k: _cal(rows))
        assert em.fetch_us_results("KEY", ["C"])["C"]["eps_surprise_pct"] == 60.0

    def test_한심볼_여러행이면_가장최근날짜(self, monkeypatch):
        rows = [_row("F", -3, eps_e=1.0, eps_a=1.0, q=1),
                _row("F", -1, eps_e=1.0, eps_a=1.2, q=2),
                _row("F", -2, eps_e=1.0, eps_a=0.5, q=1)]
        monkeypatch.setattr(em.requests, "get", lambda *a, **k: _cal(rows))
        out = em.fetch_us_results("KEY", ["F"])
        assert out["F"]["date"] == (date.today() - timedelta(days=1)).isoformat()
        assert out["F"]["eps_surprise_pct"] == 20.0

    def test_키없거나_실패시_빈dict(self, monkeypatch):
        assert em.fetch_us_results("", ["AAPL"]) == {}
        assert em.fetch_us_results("KEY", []) == {}

        def boom(*a, **k):
            raise RuntimeError("net down")
        monkeypatch.setattr(em.requests, "get", boom)
        assert em.fetch_us_results("KEY", ["AAPL"]) == {}


# ── 프라이밍(재시작 폭주 방지) + dedup ─────────────────────────────
def test_first_poll_primes_without_waking(tmp_path):
    store = Store(tmp_path / "t.db")
    store.open_position("AAPL", "US", 10, 200)           # 보유 중이어도
    woke = []
    results = {"AAPL": _result("AAPL")}
    w = _watcher(store, results, on_wake=lambda why, p: woke.append(p))
    res = w.poll_once()                                  # 첫 폴 = 마킹만
    assert res == {"new": 0, "woke": [], "queued": []} and woke == []
    assert store.recent_events("earnings_result", 0) == []


def test_같은실적_재폴링시_재각성_안함(tmp_path):
    store = Store(tmp_path / "t.db")
    store.open_position("AAPL", "US", 10, 200)
    woke = []
    results = {}
    w = _watcher(store, results, on_wake=lambda why, p: woke.append(p))
    w.poll_once()                                        # 프라이밍(빈 결과)
    results["AAPL"] = _result("AAPL")
    assert w.poll_once()["new"] == 1 and len(woke) == 1
    assert w.poll_once()["new"] == 0 and len(woke) == 1  # 같은 분기 = dedup
    assert len(store.recent_events("earnings_result", 0)) == 1


# ── 3단 라우팅 ─────────────────────────────────────────────────────
def test_보유종목_결과는_각성(tmp_path):
    store = Store(tmp_path / "t.db")
    store.open_position("AAPL", "US", 10, 200)
    woke, results = [], {}
    w = _watcher(store, results, on_wake=lambda why, p: woke.append((why, p)))
    w.poll_once()
    results["AAPL"] = _result("AAPL", eps_e=1.5, eps_a=1.2)
    res = w.poll_once()
    assert res["woke"] == ["AAPL"] and res["queued"] == []
    assert woke and woke[0][0] == "earnings_result"
    p = woke[0][1][0]
    assert p["symbol"] == "AAPL" and p["eps_surprise_pct"] == -20.0
    assert p["detected_at"] > 0                          # 감지 지연 계측용
    ev = store.recent_events("earnings_result", 0)[0]
    assert json.loads(ev["payload"])["route"] == "wake"


def test_armed_종목도_각성(tmp_path):
    store = Store(tmp_path / "t.db")
    store.arm_candidate("TSLA", "US", strategy="rsi_reversion")
    woke, results = [], {}
    w = _watcher(store, results, on_wake=lambda why, p: woke.append(p))
    w.poll_once()
    results["TSLA"] = _result("TSLA")
    assert w.poll_once()["woke"] == ["TSLA"] and woke


def test_유니버스_종목은_각성없이_큐(tmp_path):
    store = Store(tmp_path / "t.db")                     # 보유 없음
    woke, results = [], {}
    w = _watcher(store, results, universe=("NVDA",),
                 on_wake=lambda why, p: woke.append(p))
    w.poll_once()
    results["NVDA"] = _result("NVDA")
    res = w.poll_once()
    assert res["queued"] == ["NVDA"] and res["woke"] == [] and woke == []
    ev = store.recent_events("earnings_result", 0)[0]
    assert json.loads(ev["payload"])["route"] == "queue"


def test_무관종목은_이벤트도_없음(tmp_path):
    store = Store(tmp_path / "t.db")
    results = {}
    w = _watcher(store, results, universe=("NVDA",))
    w.poll_once()
    results["ZZZZ"] = _result("ZZZZ")
    res = w.poll_once()
    assert res["new"] == 1 and res["woke"] == [] and res["queued"] == []
    assert store.recent_events("earnings_result", 0) == []


def test_on_wake_예외여도_워처가_안죽고_error이벤트(tmp_path):
    store = Store(tmp_path / "t.db")
    store.open_position("AAPL", "US", 10, 200)

    def boom(why, payloads):
        raise RuntimeError("뇌 죽음")

    results = {}
    w = _watcher(store, results, on_wake=boom)
    w.poll_once()
    results["AAPL"] = _result("AAPL")
    assert w.poll_once()["woke"] == ["AAPL"]             # 예외를 삼키고 계속
    ev = store.recent_events("error", 0)[0]
    assert json.loads(ev["payload"])["where"] == "earnings_result_wake"


# ── 주기(발표 임박 10분 / 그 외 1시간) ─────────────────────────────
def _cal_entry(days, market="US"):
    return {"market": market, "date": (date.today() + timedelta(days=days)).isoformat(),
            "dday": days}


def test_interval_발표임박이면_촘촘히(tmp_path):
    store = Store(tmp_path / "t.db")
    near = {"AAPL": _cal_entry(1)}                       # D+1(내일 발표)
    assert _watcher(store, {}, calendar_fn=lambda: near).interval() == 600.0
    just = {"AAPL": _cal_entry(-3)}                      # D-3(사흘 전 발표)
    assert _watcher(store, {}, calendar_fn=lambda: just).interval() == 600.0


def test_interval_임박없으면_idle(tmp_path):
    store = Store(tmp_path / "t.db")
    far = {"AAPL": _cal_entry(9), "F": _cal_entry(-30),
           "005930": _cal_entry(0, market="KR")}         # KR 발표는 이 워처와 무관
    assert _watcher(store, {}, calendar_fn=lambda: far).interval() == 3600.0
    assert _watcher(store, {}).interval() == 600.0       # 캘린더 없으면 보수적


def test_interval_calendar_fn_예외는_삼킴(tmp_path):
    store = Store(tmp_path / "t.db")

    def boom():
        raise RuntimeError("캘린더 깨짐")

    assert _watcher(store, {}, calendar_fn=boom).interval() == 600.0


# ── 뇌 컨텍스트 연결 ───────────────────────────────────────────────
def test_recent_earnings_results_복원(tmp_path):
    from src.agents.pipeline import CycleRunner
    store = Store(tmp_path / "t.db")
    store.log_event("earnings_result", "AAPL",
                    {"symbol": "AAPL", "date": "2026-07-21", "eps_estimate": 1.5,
                     "eps_actual": 1.2, "eps_surprise_pct": -20.0,
                     "revenue_surprise_pct": 1.3, "route": "wake",
                     "detected_at": 1.0})
    runner = CycleRunner.__new__(CycleRunner)            # LLM/게이트웨이 없이 메서드만
    runner.store = store
    out = runner._recent_earnings_results()
    assert len(out) == 1
    assert out[0] == {"symbol": "AAPL", "date": "2026-07-21", "eps_estimate": 1.5,
                      "eps_actual": 1.2, "eps_surprise_pct": -20.0,
                      "revenue_surprise_pct": 1.3, "route": "wake"}


def test_earnings_results_in_context():
    from src.agents.context import build_context
    ctx = json.loads(build_context({}, [], {}, {}, earnings_results=[
        {"symbol": "AAPL", "eps_surprise_pct": -20.0, "route": "wake"}]))
    assert ctx["earnings_results"][0]["eps_surprise_pct"] == -20.0
    assert "earnings_results" not in json.loads(build_context({}, [], {}, {}))


def test_프롬프트에_실적결과_섹션(tmp_path):
    from src.agents import decision_agent
    assert "earnings_results" in decision_agent.SYSTEM
    assert "밸류트랩" in decision_agent.SYSTEM
    assert "eps_surprise_pct" in decision_agent.SYSTEM
    assert "op_profit_actual" in decision_agent.SYSTEM
