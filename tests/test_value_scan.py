"""value_scan(저평가 주 스캔) — 전부 주입식(네트워크/실 LLM 없음)."""
from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from src.agents.llm import MockLLM
from src.agents.schemas import ValueDossier
from src.value_scan import (candidate_metrics, fresh_symbols, load_watchlist,
                            priority_refresh_symbols, weekend_map_symbols,
                            is_kst_weekend, run_scan, save_watchlist,
                            scan_candidates, _vcfg,
                            _is_kr_etf, _kr_pool, _kr_fundamentals)

_KST = ZoneInfo("Asia/Seoul")
_SAT = datetime(2026, 8, 15, 12, 0, tzinfo=_KST).timestamp()  # 토 12:00


def _cfg(**over):
    # 기존 KR 전용 테스트는 markets=["KR"] 로 고정(US 네트워크 접촉 방지). US/병합
    # 테스트는 markets 를 명시 오버라이드하고 fetch_us_value_fn 을 주입한다.
    v = {"pool": 10, "markets": ["KR"], "min_avg_turnover": 5e8,
         "max_per_run": 6, "ttl_hours": 400}
    v.update(over)
    return SimpleNamespace(raw={"value_scan": v})


def _df(days: int = 260, peak: float = 100_000.0, last_ratio: float = 0.6,
        ret20: float = 0.0, volume: float = 100_000.0) -> pd.DataFrame:
    """고점 peak → 현재 peak*last_ratio 로 내려온 합성 일봉.

    마지막 20봉은 ret20 을 만들도록 선형 보간(자유낙하 가드 검증용).
    """
    last = peak * last_ratio
    start20 = last / (1 + ret20)
    pre = [peak - (peak - start20) * i / (days - 21) for i in range(days - 20)]
    tail = [start20 + (last - start20) * (i + 1) / 20 for i in range(20)]
    close = pre + tail
    return pd.DataFrame({"close": close, "volume": [volume] * days})


VCFG = _vcfg(_cfg())


class TestCandidateMetrics:
    def test_낙폭_밴드_통과(self):
        m = candidate_metrics(_df(last_ratio=0.6), VCFG)   # -40%
        assert m is not None and m["drawdown_1y_pct"] == pytest.approx(-40, abs=1)

    def test_얕은_낙폭_제외(self):
        assert candidate_metrics(_df(last_ratio=0.9), VCFG) is None   # -10%

    def test_빈사_낙폭_제외(self):
        assert candidate_metrics(_df(last_ratio=0.2), VCFG) is None   # -80%

    def test_자유낙하_제외(self):
        assert candidate_metrics(_df(last_ratio=0.6, ret20=-0.20), VCFG) is None

    def test_유동성_플로어_제외(self):
        # 종가 6만원 × 거래량 100주 = 거래대금 ~600만원 << 5억
        assert candidate_metrics(_df(volume=100.0), VCFG) is None

    def test_캔들_부족_제외(self):
        assert candidate_metrics(_df(days=60), VCFG) is None


def _ranking_fn(rows):
    def fn(naver_market, pool):
        return rows if naver_market == "KOSPI" else []
    return fn


def _history_fn(dfs: dict):
    def fn(sym, mkt):
        return dfs[sym]
    return fn


class TestScanCandidates:
    def test_게이트_점수순_및_제외(self):
        rows = [{"symbol": s, "name": s} for s in ("AAA", "BBB", "CCC")]
        dfs = {"AAA": _df(last_ratio=0.6), "BBB": _df(last_ratio=0.9),  # BBB 얕음
               "CCC": _df(last_ratio=0.5)}
        out = scan_candidates(_cfg(), _ranking_fn(rows), _history_fn(dfs))
        syms = [c["symbol"] for c in out]
        assert "BBB" not in syms and set(syms) == {"AAA", "CCC"}
        assert out[0]["score"] >= out[1]["score"]

    def test_exclude_는_히스토리_조회_전에_거른다(self):
        rows = [{"symbol": "AAA", "name": "AAA"}]
        calls = []

        def hist(sym, mkt):
            calls.append(sym)
            return _df()
        out = scan_candidates(_cfg(), _ranking_fn(rows), hist, exclude={"AAA"})
        assert out == [] and calls == []


def _us_pool_fn(rows):
    """가짜 US 저평가 풀 소스(fetch_us_value_pool 대체)."""
    def fn(pool=400):
        return rows
    return fn


def _mkt_history_fn(dfs: dict):
    """(sym, mkt) → 미리 만든 df. mkt 검증도 하도록 sym 기준으로만 조회."""
    def fn(sym, mkt):
        return dfs[sym]
    return fn


class TestIsKrEtf:
    @pytest.mark.parametrize("name", ["KODEX 200", "TIGER 미국배당", "ACE KRX금현물"])
    def test_etf_이름_True(self, name):
        assert _is_kr_etf(name) is True

    @pytest.mark.parametrize("name", ["삼성전자", "파미셀", "SK케미칼", ""])
    def test_개별주_및_빈문자열_False(self, name):
        assert _is_kr_etf(name) is False


class TestKrPool:
    def test_ETF_제외하고_개별주만(self):
        rows = [{"symbol": "069500", "name": "KODEX 200"},
                {"symbol": "005930", "name": "삼성전자"},
                {"symbol": "411060", "name": "ACE KRX금현물"},
                {"symbol": "032830", "name": "삼성생명"}]
        out = _kr_pool(_ranking_fn(rows), pool=8)
        syms = {r["symbol"] for r in out}
        assert syms == {"005930", "032830"}          # ETF 2종 제외
        assert all(r["market"] == "KR" for r in out)


class TestScanCandidatesUS:
    def test_US_경로_market_필드_및_게이트(self):
        us_rows = [{"symbol": "INTC", "name": "Intel"},
                   {"symbol": "PYPL", "name": "PayPal"}]
        # US 유동성 플로어(3e6 달러) 통과하게 가격×거래량 충분히. -35% 낙폭.
        dfs = {"INTC": _df(peak=40.0, last_ratio=0.65, volume=5_000_000),
               "PYPL": _df(peak=120.0, last_ratio=0.6, volume=2_000_000)}
        out = scan_candidates(_cfg(markets=["US"]), _ranking_fn([]),
                              _mkt_history_fn(dfs), fetch_us_value_fn=_us_pool_fn(us_rows))
        assert {c["symbol"] for c in out} == {"INTC", "PYPL"}
        assert all(c["market"] == "US" for c in out)
        assert all(c["mcap_rank"].startswith("US#") for c in out)
        assert out[0]["score"] >= out[1]["score"]

    def test_KR_US_병합_score순_및_market(self):
        kr_rows = [{"symbol": "005930", "name": "삼성"}]
        us_rows = [{"symbol": "INTC", "name": "Intel"}]
        dfs = {"005930": _df(peak=100_000.0, last_ratio=0.6, volume=100_000),   # -40%
               "INTC": _df(peak=40.0, last_ratio=0.5, volume=5_000_000)}        # -50%
        out = scan_candidates(_cfg(markets=["KR", "US"]), _ranking_fn(kr_rows),
                              _mkt_history_fn(dfs), fetch_us_value_fn=_us_pool_fn(us_rows))
        by = {c["symbol"]: c for c in out}
        assert by["005930"]["market"] == "KR" and by["INTC"]["market"] == "US"
        # score 내림차순 정렬 확인(깊은 낙폭 INTC 가 상위)
        assert [c["symbol"] for c in out] == sorted(
            [c["symbol"] for c in out], key=lambda s: by[s]["score"], reverse=True)
        assert out[0]["symbol"] == "INTC"

    def test_US_후보엔_fundamentals_실리고_KR엔_없다(self):
        # US 풀 row 에 fundamentals 가 있으면 후보에 실리고, KR 후보엔 키 자체가 없다.
        fund = {"pe_trailing": 12.3, "pe_forward": 10.0, "pb": 1.2,
                "eps_ttm": 1.8, "market_cap_busd": 95.5}
        kr_rows = [{"symbol": "005930", "name": "삼성"}]
        us_rows = [{"symbol": "INTC", "name": "Intel", "fundamentals": fund}]
        dfs = {"005930": _df(peak=100_000.0, last_ratio=0.6, volume=100_000),
               "INTC": _df(peak=40.0, last_ratio=0.6, volume=5_000_000)}
        out = scan_candidates(_cfg(markets=["KR", "US"]), _ranking_fn(kr_rows),
                              _mkt_history_fn(dfs), fetch_us_value_fn=_us_pool_fn(us_rows))
        by = {c["symbol"]: c for c in out}
        assert by["INTC"]["fundamentals"] == fund
        assert "fundamentals" not in by["005930"]

    def test_US_fundamentals_None이면_후보에_안_실린다(self):
        # 풀 row 의 fundamentals 가 None(구버전 소스 등)이면 키를 넣지 않는다.
        us_rows = [{"symbol": "INTC", "name": "Intel", "fundamentals": None}]
        dfs = {"INTC": _df(peak=40.0, last_ratio=0.6, volume=5_000_000)}
        out = scan_candidates(_cfg(markets=["US"]), _ranking_fn([]),
                              _mkt_history_fn(dfs), fetch_us_value_fn=_us_pool_fn(us_rows))
        assert "fundamentals" not in out[0]

    def test_US_시장별_유동성_플로어_탈락(self):
        # 저유동성 US 종목: 가격 10달러 × 거래량 1000주 = 1만달러 << 3e6 → 탈락.
        us_rows = [{"symbol": "TINY", "name": "Tiny"}]
        dfs = {"TINY": _df(peak=20.0, last_ratio=0.6, volume=1_000)}
        out = scan_candidates(_cfg(markets=["US"]), _ranking_fn([]),
                              _mkt_history_fn(dfs), fetch_us_value_fn=_us_pool_fn(us_rows))
        assert out == []


class TestVcfg:
    def test_스칼라_min_turnover_는_KR_플로어로_정규화(self):
        v = _vcfg(_cfg(min_avg_turnover=5e8))
        assert v["min_avg_turnover"] == {"KR": 5e8, "US": 3e6}
        # 스칼라 cfg 로도 기존 KR 게이트가 그대로 동작(default market="KR").
        assert candidate_metrics(_df(last_ratio=0.6), v) is not None
        assert candidate_metrics(_df(last_ratio=0.6, volume=100.0), v) is None

    def test_dict_min_turnover_는_기본값_위에_병합(self):
        v = _vcfg(_cfg(min_avg_turnover={"US": 1e6}))
        assert v["min_avg_turnover"] == {"KR": 5e8, "US": 1e6}

    def test_markets_와_pool_us_기본값(self):
        v = _vcfg(SimpleNamespace(raw={"value_scan": {}}))
        assert v["markets"] == ["KR", "US"] and v["pool_us"] == 400
        assert v["refresh_hours"] == 20 and v["refresh_slots"] == 3
        assert v["weekend_refresh"] is True and v["weekend_max_per_run"] == 24


class TestWatchlist:
    def test_원자적_저장_및_병합_보존(self, tmp_path):
        p = tmp_path / "wl.json"
        save_watchlist({"OLD": {"ts": 1.0, "stance": "fair"}}, p)
        wl = load_watchlist(p)
        wl["NEW"] = {"ts": 2.0, "stance": "undervalued"}
        save_watchlist(wl, p)
        again = load_watchlist(p)
        assert set(again) == {"OLD", "NEW"} and not p.with_name(p.name + ".tmp").exists()

    def test_fresh_symbols_ttl(self):
        now = 1_000_000.0
        wl = {"F": {"ts": now - 100 * 3600}, "E": {"ts": now - 500 * 3600}}
        assert fresh_symbols(wl, ttl_hours=400, now=now) == {"F"}

    def test_priority_refresh_는_보유만_undervalued_지도는_안_먹음(self):
        now = 1_000_000.0
        wl = {
            "H": {"ts": now - 200 * 3600, "stance": "fair"},
            "U1": {"ts": now - 180 * 3600, "stance": "undervalued"},
            "U2": {"ts": now - 250 * 3600, "stance": "undervalued"},
            "FRESH": {"ts": now - 10 * 3600, "stance": "undervalued"},
            "TRAP": {"ts": now - 300 * 3600, "stance": "value_trap"},
        }
        out = priority_refresh_symbols(wl, held={"H"}, refresh_hours=20,
                                       now=now, limit=3)
        assert out == ["H"]
        assert priority_refresh_symbols(wl, held=set(), refresh_hours=20,
                                        now=now, limit=3) == []

    def test_weekend_map_오래된_undervalued_이번주말_완료분은_스킵(self):
        start = datetime(2026, 8, 15, 0, 0, tzinfo=_KST).timestamp()
        wl = {
            "OLD": {"ts": start - 10, "stance": "undervalued", "market": "KR"},
            "DONE": {"ts": start + 3600, "stance": "undervalued", "market": "KR"},
            "OLDER": {"ts": start - 100, "stance": "undervalued", "market": "KR"},
            "TRAP": {"ts": start - 200, "stance": "value_trap", "market": "KR"},
            "US": {"ts": start - 50, "stance": "undervalued", "market": "US"},
        }
        assert is_kst_weekend(_SAT)
        assert weekend_map_symbols(wl, markets=["KR"], now=_SAT,
                                   skip=set(), limit=10) == ["OLDER", "OLD"]
        assert weekend_map_symbols(wl, markets=["KR"], now=_SAT,
                                   skip={"OLDER"}, limit=10) == ["OLD"]


def _ok_llm():
    def responder(schema, system, user):
        sym = json.loads(user)["symbol"]
        return ValueDossier(stance="undervalued", conviction=0.7,
                            thesis=f"{sym} 저평가", fair_low_pct=10, fair_high_pct=30)
    return MockLLM(responder)


class TestRunScan:
    def _setup(self, tmp_path, n=3):
        rows = [{"symbol": f"S{i}", "name": f"S{i}"} for i in range(n)]
        dfs = {f"S{i}": _df(last_ratio=0.6 - i * 0.02) for i in range(n)}
        # 뉴스 fn 은 no-op 주입(실함수는 네트워크 → 단위테스트 격리).
        return (dict(fetch_ranking_fn=_ranking_fn(rows), fetch_history_fn=_history_fn(dfs),
                     news_kr_fn=lambda *a, **k: [], news_us_fn=lambda *a, **k: [],
                     watchlist_path=tmp_path / "wl.json"))

    def test_정상_스캔_저장(self, tmp_path):
        kw = self._setup(tmp_path)
        s = run_scan(_cfg(), _ok_llm(), limit=2, **kw)
        assert s["done"] == 2 and s["aborted"] is None
        wl = load_watchlist(kw["watchlist_path"])
        assert len(wl) == 2
        e = next(iter(wl.values()))
        assert e["stance"] == "undervalued" and "metrics" in e and e["ts"] > 0

    def test_US_fundamentals_watchlist_에_저장(self, tmp_path):
        # US 후보의 fundamentals 가 watchlist 항목에 남아 후속 소비 가능해야 한다.
        fund = {"pe_trailing": 12.3, "pe_forward": 10.0, "pb": 1.2,
                "eps_ttm": 1.8, "market_cap_busd": 95.5}
        us_rows = [{"symbol": "INTC", "name": "Intel", "fundamentals": fund}]
        dfs = {"INTC": _df(peak=40.0, last_ratio=0.6, volume=5_000_000)}
        s = run_scan(_cfg(markets=["US"]), _ok_llm(), limit=2,
                     fetch_ranking_fn=_ranking_fn([]),
                     fetch_history_fn=_mkt_history_fn(dfs),
                     fetch_us_value_fn=_us_pool_fn(us_rows),
                     news_us_fn=lambda *a, **k: [], news_kr_fn=lambda *a, **k: [],
                     watchlist_path=tmp_path / "wl.json")
        assert s["done"] == 1
        wl = load_watchlist(tmp_path / "wl.json")
        assert wl["INTC"]["fundamentals"] == fund

    def test_ttl_신선_스킵_만료_재스캔(self, tmp_path):
        kw = self._setup(tmp_path)
        now = 1_000_000.0
        save_watchlist({"S0": {"ts": now - 1}, "S1": {"ts": now - 500 * 3600}},
                       kw["watchlist_path"])
        called = []

        def responder(schema, system, user):
            called.append(json.loads(user)["symbol"])
            return ValueDossier(stance="fair", conviction=0.5, thesis="t")
        s = run_scan(_cfg(), MockLLM(responder), limit=10, now_fn=lambda: now, **kw)
        assert "S0" not in called and {"S1", "S2"} == set(called)
        assert s["skipped_fresh"] == 1
        assert s["refreshed"] == 0

    def test_ttl_안_미보유_undervalued_는_신규슬롯_안_먹음(self, tmp_path):
        kw = self._setup(tmp_path)
        now = 1_000_000.0
        save_watchlist({
            "S0": {"ts": now - 200 * 3600, "stance": "undervalued", "name": "S0",
                   "market": "KR", "conviction": 0.7, "fair_low_pct": 10.0,
                   "metrics": {"price": 1000.0}},
        }, kw["watchlist_path"])
        called = []

        def responder(schema, system, user):
            called.append(json.loads(user)["symbol"])
            return ValueDossier(stance="fair", conviction=0.5, thesis="t")
        s = run_scan(_cfg(), MockLLM(responder), limit=10, now_fn=lambda: now, **kw)
        assert "S0" not in called and s["refreshed"] == 0
        assert set(called) == {"S1", "S2"}
        assert s["weekend"] is False

    def test_주말_미보유_undervalued_를_신규보다_먼저_전량(self, tmp_path):
        kw = self._setup(tmp_path)
        sat = _SAT
        save_watchlist({
            "S0": {"ts": sat - 200 * 3600, "stance": "undervalued", "name": "S0",
                   "market": "KR", "metrics": {"price": 1000.0}},
            "S1": {"ts": sat - 80 * 3600, "stance": "undervalued", "name": "S1",
                   "market": "KR", "metrics": {"price": 1000.0}},
            "S2": {"ts": sat - 10 * 3600, "stance": "undervalued", "name": "S2",
                   "market": "KR", "metrics": {"price": 1000.0}},
        }, kw["watchlist_path"])
        called = []

        def responder(schema, system, user):
            called.append(json.loads(user)["symbol"])
            return ValueDossier(stance="undervalued", conviction=0.6, thesis="t",
                                fair_low_pct=12, fair_high_pct=25)
        s = run_scan(_cfg(), MockLLM(responder), limit=2, now_fn=lambda: sat, **kw)
        assert called == ["S0", "S1"] and s["refreshed"] == 2 and s["weekend"] is True

    def test_주말_이미_돈_심볼은_다시_안_먹음(self, tmp_path):
        kw = self._setup(tmp_path)
        sat = _SAT
        save_watchlist({
            "S0": {"ts": sat - 3600, "stance": "undervalued", "name": "S0",
                   "market": "KR", "metrics": {"price": 1000.0}},
            "S1": {"ts": sat - 80 * 3600, "stance": "undervalued", "name": "S1",
                   "market": "KR", "metrics": {"price": 1000.0}},
        }, kw["watchlist_path"])
        called = []

        def responder(schema, system, user):
            called.append(json.loads(user)["symbol"])
            return ValueDossier(stance="fair", conviction=0.5, thesis="t")
        s = run_scan(_cfg(), MockLLM(responder), limit=1, now_fn=lambda: sat, **kw)
        assert called == ["S1"] and s["refreshed"] == 1

    def test_보유_재검증은_낙폭게이트_밖이어도_한다(self, tmp_path):
        now = 1_000_000.0
        p = tmp_path / "wl.json"
        save_watchlist({
            "HOLD": {"ts": now - 200 * 3600, "stance": "fair", "name": "HOLD",
                     "market": "KR", "metrics": {"price": 90_000.0}},
        }, p)
        called = []

        def responder(schema, system, user):
            called.append(json.loads(user)["symbol"])
            return ValueDossier(stance="fair", conviction=0.4, thesis="t")

        s = run_scan(
            _cfg(), MockLLM(responder), limit=5, now_fn=lambda: now,
            fetch_ranking_fn=_ranking_fn([{"symbol": "HOLD", "name": "HOLD"}]),
            fetch_history_fn=_history_fn({"HOLD": _df(last_ratio=0.9)}),
            news_kr_fn=lambda *a, **k: [], news_us_fn=lambda *a, **k: [],
            watchlist_path=p, held_symbols={"HOLD"})
        assert called == ["HOLD"] and s["refreshed"] == 1

    def test_limit_예외_중단하되_기존_결과_보존(self, tmp_path):
        kw = self._setup(tmp_path)
        calls = []

        def responder(schema, system, user):
            calls.append(1)
            if len(calls) >= 2:
                raise RuntimeError("You've hit your session LIMIT · resets 3:30am")
            return ValueDossier(stance="fair", conviction=0.5, thesis="t")
        s = run_scan(_cfg(), MockLLM(responder), limit=3, **kw)
        assert s["aborted"] == "limit" and s["done"] == 1 and len(calls) == 2
        assert len(load_watchlist(kw["watchlist_path"])) == 1   # 1건은 이미 저장됨

    def test_일반_예외는_해당_심볼만_스킵(self, tmp_path):
        kw = self._setup(tmp_path)
        calls = []

        def responder(schema, system, user):
            calls.append(1)
            if len(calls) == 1:
                raise ValueError("JSON 추출 실패")
            return ValueDossier(stance="fair", conviction=0.5, thesis="t")
        s = run_scan(_cfg(), MockLLM(responder), limit=3, **kw)
        assert s["failed"] == 1 and s["done"] == 2 and s["aborted"] is None


class TestRunScanNews:
    def test_뉴스가_후보에_주입되고_watchlist에_저장(self, tmp_path, monkeypatch):
        # US 뉴스는 finnhub 키가 있어야 조회 → 테스트 env 에 강제 주입.
        monkeypatch.setenv("FINNHUB_API_KEY", "TESTKEY")
        kr_rows = [{"symbol": "005930", "name": "삼성"}]
        us_rows = [{"symbol": "INTC", "name": "Intel"}]
        dfs = {"005930": _df(peak=100_000.0, last_ratio=0.6, volume=100_000),
               "INTC": _df(peak=40.0, last_ratio=0.6, volume=5_000_000)}
        seen: dict = {}

        def kr_news(code, per=3):
            return [{"title": f"KR뉴스 {code}", "date": "2026-07-01", "source": "한경"}]

        def us_news(key, sym, per=3):
            assert key == "TESTKEY"
            return [{"title": f"US news {sym}", "date": "2026-07-01", "source": "Reuters"}]

        def responder(schema, system, user):
            c = json.loads(user)
            seen[c["symbol"]] = c.get("recent_news")
            return ValueDossier(stance="fair", conviction=0.5, thesis="t")

        s = run_scan(_cfg(markets=["KR", "US"]), MockLLM(responder), limit=5,
                     fetch_ranking_fn=_ranking_fn(kr_rows),
                     fetch_history_fn=_mkt_history_fn(dfs),
                     fetch_us_value_fn=_us_pool_fn(us_rows),
                     news_kr_fn=kr_news, news_us_fn=us_news,
                     watchlist_path=tmp_path / "wl.json")
        assert s["done"] == 2
        # 후보 dict(프롬프트 입력)에 recent_news 가 실렸다.
        assert seen["005930"][0]["title"] == "KR뉴스 005930"
        assert seen["INTC"][0]["title"] == "US news INTC"
        # watchlist 항목에도 보존.
        wl = load_watchlist(tmp_path / "wl.json")
        assert wl["005930"]["recent_news"][0]["source"] == "한경"
        assert wl["INTC"]["recent_news"][0]["source"] == "Reuters"

    def test_finnhub_키없으면_US뉴스_스킵_KR만(self, tmp_path, monkeypatch):
        monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
        us_rows = [{"symbol": "INTC", "name": "Intel"}]
        dfs = {"INTC": _df(peak=40.0, last_ratio=0.6, volume=5_000_000)}
        called = []

        def us_news(key, sym, per=3):
            called.append(sym)
            return [{"title": "x", "date": "", "source": "R"}]

        seen: dict = {}

        def responder(schema, system, user):
            c = json.loads(user)
            seen[c["symbol"]] = c.get("recent_news")
            return ValueDossier(stance="fair", conviction=0.5, thesis="t")

        run_scan(_cfg(markets=["US"]), MockLLM(responder), limit=5,
                 fetch_ranking_fn=_ranking_fn([]),
                 fetch_history_fn=_mkt_history_fn(dfs),
                 fetch_us_value_fn=_us_pool_fn(us_rows),
                 news_us_fn=us_news, news_kr_fn=lambda *a, **k: [],
                 watchlist_path=tmp_path / "wl.json")
        assert called == []                 # 키 없으면 US 뉴스 fn 호출 안 함
        assert seen["INTC"] is None         # recent_news 미주입

    def test_뉴스조회_실패해도_스캔_완주(self, tmp_path):
        kr_rows = [{"symbol": "005930", "name": "삼성"}]
        dfs = {"005930": _df(peak=100_000.0, last_ratio=0.6, volume=100_000)}

        def kr_news(code, per=3):
            raise RuntimeError("naver down")

        s = run_scan(_cfg(markets=["KR"]), _ok_llm(), limit=5,
                     fetch_ranking_fn=_ranking_fn(kr_rows),
                     fetch_history_fn=_history_fn(dfs),
                     news_kr_fn=kr_news, news_us_fn=lambda *a, **k: [],
                     watchlist_path=tmp_path / "wl.json")
        assert s["done"] == 1               # 뉴스 실패에도 도시에는 생성
        wl = load_watchlist(tmp_path / "wl.json")
        assert "recent_news" not in wl["005930"]


def _fin(fy, **over):
    """fetch_financials 반환 형태의 전체 계정 dict(over 로 개별 계정 오버라이드)."""
    d = {"revenue": 1e12, "operating_income": 1.5e11, "net_income": 1e11,
         "equity": 2e12, "total_assets": 5e12, "total_liabilities": 3e12,
         "current_assets": 1.2e12, "current_liabilities": 8e11, "fiscal_year": fy}
    d.update(over)
    return d


class TestKrFundamentals:
    def test_캐시_hit_시_DART_미호출(self):
        # 당해(2025)+전년(2024) 모두 캐시 → DART 미호출. 성장률·파생지표 캐시로 계산.
        cache = {"CORP": {
            "2025": _fin(2025, revenue=3.3e14, operating_income=4.0e13,
                         net_income=3.4e13, equity=4.02e14),
            "2024": _fin(2024, revenue=3.0e14, operating_income=3.0e13,
                         net_income=2.0e13, equity=3.5e14),
        }}
        calls = []

        def fetch(k, corp, y):
            calls.append((corp, y))
            return None
        f = _kr_fundamentals("K", {"005930": "CORP"}, "005930", 1.666e15, cache,
                             fetch_fn=fetch)
        assert calls == []                         # 당해·전년 모두 캐시 hit → DART 미호출
        assert f["pb"] == round(1.666e15 / 4.02e14, 2)
        assert f["pe_trailing"] == round(1.666e15 / 3.4e13, 2)
        assert f["net_margin"] == round(3.4e13 / 3.3e14, 4)
        assert f["revenue_eok"] == round(3.3e14 / 1e8) and f["fiscal_year"] == 2025
        # 성장률(전년 대비) — 전년 캐시로 계산
        assert f["revenue_growth"] == round((3.3e14 - 3.0e14) / 3.0e14, 4)
        assert f["net_income_growth"] == round((3.4e13 - 2.0e13) / 2.0e13, 4)
        assert f["op_income_growth"] == round((4.0e13 - 3.0e13) / 3.0e13, 4)

    def test_파생지표_계산(self):
        # debt_ratio/current_ratio/roe/roa/op_margin — 당해 재무로 계산.
        cache = {}

        def fetch(k, corp, y):
            if y == 2025:
                return _fin(2025)
            return None                            # 전년 없음 → 성장률 None
        f = _kr_fundamentals("K", {"S": "C"}, "S", 1e13, cache, fetch_fn=fetch)
        assert f["debt_ratio"] == round(3e12 / 2e12, 4)        # 부채/자본
        assert f["current_ratio"] == round(1.2e12 / 8e11, 4)  # 유동자산/유동부채
        assert f["roe"] == round(1e11 / 2e12, 4)
        assert f["roa"] == round(1e11 / 5e12, 4)
        assert f["op_margin"] == round(1.5e11 / 1e12, 4)
        assert (f["revenue_growth"] is None and f["op_income_growth"] is None
                and f["net_income_growth"] is None)           # 전년 없음

    def test_분모0또는결측이면_해당지표_None(self):
        def fetch(k, corp, y):
            if y != 2025:
                return None
            return _fin(2025, current_liabilities=0, total_assets=None,
                        operating_income=None, total_liabilities=None)
        f = _kr_fundamentals("K", {"S": "C"}, "S", 1e13, {}, fetch_fn=fetch)
        assert f["current_ratio"] is None      # 분모 0
        assert f["roa"] is None                # total_assets None
        assert f["op_margin"] is None          # operating_income None
        assert f["debt_ratio"] is None         # total_liabilities None
        assert f["roe"] == round(1e11 / 2e12, 4)   # 자본 정상 → 계산

    def test_적자면_pe_None(self):
        cache = {}

        def fetch(k, corp, y):
            if y == 2025:
                return _fin(2025, net_income=-5e11)
            return None
        f = _kr_fundamentals("K", {"S": "C"}, "S", 1e13, cache, fetch_fn=fetch)
        assert f["pe_trailing"] is None and f["pb"] == round(1e13 / 2e12, 2)
        assert f["net_margin"] == round(-5e11 / 1e12, 4)
        assert f["roe"] == round(-5e11 / 2e12, 4)            # ROE 는 음수 가능
        assert cache["C"]["2025"]["net_income"] == -5e11    # miss → 캐시 적재

    def test_자본잠식이면_pb_debt_roe_None(self):
        def fetch(k, corp, y):
            if y == 2025:
                return _fin(2025, net_income=1e11, equity=-3e11)
            return None
        f = _kr_fundamentals("K", {"S": "C"}, "S", 1e13, {}, fetch_fn=fetch)
        assert f["pb"] is None and f["debt_ratio"] is None and f["roe"] is None
        assert f["pe_trailing"] == round(1e13 / 1e11, 2)

    def test_revenue_eok_단위(self):
        def fetch(k, corp, y):
            if y == 2025:
                return _fin(2025, revenue=4e11)     # 매출 4000억
            return None
        f = _kr_fundamentals("K", {"S": "C"}, "S", 1e13, {}, fetch_fn=fetch)
        assert f["revenue_eok"] == 4000

    def test_2025_실패시_2024_폴백(self):
        cache = {}
        seen = []

        def fetch(k, corp, y):
            seen.append(y)
            if y == 2025:
                return None
            return _fin(y)                          # 2024·2023 은 성공
        f = _kr_fundamentals("K", {"S": "C"}, "S", 1e13, cache, fetch_fn=fetch)
        # 당해 2025 실패 → 2024 폴백, 이어 성장률용 전년(2023) 조회.
        assert seen == [2025, 2024, 2023] and f["fiscal_year"] == 2024
        assert "2024" in cache["C"] and "2023" in cache["C"] and "2025" not in cache["C"]
        assert f["revenue_growth"] == 0.0           # 2024·2023 매출 동일(_fin 기본)

    def test_시총_없으면_None(self):
        assert _kr_fundamentals("K", {"S": "C"}, "S", None, {},
                                fetch_fn=lambda *a: None) is None
        assert _kr_fundamentals("K", {"S": "C"}, "S", 0, {},
                                fetch_fn=lambda *a: None) is None

    def test_corp_없으면_None(self):
        assert _kr_fundamentals("K", {}, "S", 1e13, {}, fetch_fn=lambda *a: None) is None

    def test_재무_None이면_None(self):
        assert _kr_fundamentals("K", {"S": "C"}, "S", 1e13, {},
                                fetch_fn=lambda *a, **k: None) is None


class TestRunScanKrFundamentals:
    def test_KR주입_US안덮음_DART미접촉(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DART_API_KEY", "DKEY")
        import src.datasources.dart as dart
        # corp_map 로드는 가짜(네트워크/캐시 미접촉), fetch_financials 호출되면 실패(=DART 미접촉).
        monkeypatch.setattr(dart, "load_corp_map",
                            lambda key, *a, **k: {"005930": "CORP"})

        def _boom(*a, **k):
            raise AssertionError("DART fetch_financials 접촉됨")
        monkeypatch.setattr(dart, "fetch_financials", _boom)
        # fin_cache 파일 격리
        monkeypatch.setattr("src.value_scan._load_fin_cache", lambda *a, **k: {})
        saved = []
        monkeypatch.setattr("src.value_scan._save_fin_cache",
                            lambda d, *a, **k: saved.append(d))

        us_fund = {"pe_trailing": 12.3, "pb": 1.2, "eps_ttm": 1.8}
        kr_fund = {"pb": 1.1, "pe_trailing": 9.0, "net_margin": 0.1,
                   "revenue_eok": 3000, "fiscal_year": 2024}
        kr_rows = [{"symbol": "005930", "name": "삼성", "market_cap": 1.6e15}]
        us_rows = [{"symbol": "INTC", "name": "Intel", "fundamentals": us_fund}]
        dfs = {"005930": _df(peak=100_000.0, last_ratio=0.6, volume=100_000),
               "INTC": _df(peak=40.0, last_ratio=0.6, volume=5_000_000)}
        seen: dict = {}

        def fin_kr(api_key, corp_map, code, mc, cache):
            assert api_key == "DKEY" and corp_map == {"005930": "CORP"}
            assert code == "005930" and mc == 1.6e15
            return kr_fund

        def responder(schema, system, user):
            c = json.loads(user)
            seen[c["symbol"]] = c.get("fundamentals")
            return ValueDossier(stance="fair", conviction=0.5, thesis="t")

        s = run_scan(_cfg(markets=["KR", "US"]), MockLLM(responder), limit=5,
                     fetch_ranking_fn=_ranking_fn(kr_rows),
                     fetch_history_fn=_mkt_history_fn(dfs),
                     fetch_us_value_fn=_us_pool_fn(us_rows),
                     news_kr_fn=lambda *a, **k: [], news_us_fn=lambda *a, **k: [],
                     fin_kr_fn=fin_kr, watchlist_path=tmp_path / "wl.json")
        assert s["done"] == 2
        assert seen["005930"] == kr_fund           # KR 후보에 주입됨
        assert seen["INTC"] == us_fund             # US 기존 fundamentals 안 덮임
        wl = load_watchlist(tmp_path / "wl.json")
        assert wl["005930"]["fundamentals"] == kr_fund
        assert wl["INTC"]["fundamentals"] == us_fund

    def test_DART키_없으면_KR펀더멘털_스킵하고_완주(self, tmp_path, monkeypatch):
        monkeypatch.delenv("DART_API_KEY", raising=False)
        kr_rows = [{"symbol": "005930", "name": "삼성", "market_cap": 1.6e15}]
        dfs = {"005930": _df(peak=100_000.0, last_ratio=0.6, volume=100_000)}
        called = []
        seen: dict = {}

        def fin_kr(*a, **k):
            called.append(1)
            return {"pb": 1.0}

        def responder(schema, system, user):
            c = json.loads(user)
            seen[c["symbol"]] = c.get("fundamentals")
            return ValueDossier(stance="fair", conviction=0.5, thesis="t")

        s = run_scan(_cfg(markets=["KR"]), MockLLM(responder), limit=5,
                     fetch_ranking_fn=_ranking_fn(kr_rows),
                     fetch_history_fn=_history_fn(dfs),
                     news_kr_fn=lambda *a, **k: [], news_us_fn=lambda *a, **k: [],
                     fin_kr_fn=fin_kr, watchlist_path=tmp_path / "wl.json")
        assert s["done"] == 1                       # DART 키 없어도 완주
        assert called == []                         # KR 펀더멘털 스킵
        assert seen["005930"] is None


class TestSchema:
    def test_value_dossier_파싱(self):
        d = ValueDossier.model_validate({
            "stance": "value_trap", "conviction": 0.4, "thesis": "만성 저PBR",
            "risks": ["지배구조"], "evidence": ["지식 낡음 가능"]})
        assert d.stance == "value_trap" and d.fair_low_pct is None

    def test_잘못된_stance_거부(self):
        with pytest.raises(Exception):
            ValueDossier.model_validate({"stance": "bullish", "conviction": 0.5,
                                         "thesis": "t"})
