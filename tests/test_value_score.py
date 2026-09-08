"""value_score / value_fin_backfill — 순수·주입 테스트."""
from __future__ import annotations

import pytest

from src.value_score import (
    NEUTRAL_VALUE_FACTOR, age_decay, composite_scores, drawdown_component,
    quality_tilt, quintile_coverage_report, value_factors_mcap_quintile,
)
from src.value_fin_backfill import backfill_financials


class TestAgeDecay:
    def test_결측은_1(self):
        assert age_decay(None, 1_000_000) == 1.0

    def test_절반수명(self):
        now = 1_000_000.0
        half = now - 7 * 86400
        assert age_decay(half, now, half_life_days=7) == pytest.approx(0.5)


class TestQualityTilt:
    def test_부채과다_감점(self):
        assert quality_tilt({"debt_ratio": 3.0}) < 0

    def test_빈값_0(self):
        assert quality_tilt({}) == 0.0
        assert quality_tilt(None) == 0.0


class TestDrawdown:
    def test_밴드_안(self):
        assert drawdown_component(-40) > 0
        assert drawdown_component(-10) == 0.0


class TestValueFactorQuintile:
    def _rows(self, n: int, *, cheap_first: bool = True):
        rows = []
        for i in range(n):
            pb = (i + 1) * 0.5 if cheap_first else (n - i) * 0.5
            rows.append({
                "symbol": f"S{i:03d}",
                "market_cap": (i + 1) * 1e11,
                "fundamentals": {"pb": pb, "pe_trailing": pb * 10},
            })
        return rows

    def test_표본충분_상대화(self):
        rows = self._rows(100)
        vfs = value_factors_mcap_quintile(rows, n_min=20)
        assert len(vfs) == 100
        assert vfs[0] >= vfs[19]

    def test_희소분위_pooled폴백(self):
        rows = self._rows(100)
        vfs = value_factors_mcap_quintile(rows, n_min=25)
        assert sum(1 for v in vfs if v > 0) > 50

    def test_전체미달_중립(self):
        """분위·pooled 모두 n_min 미달 → 0(최하위)이 아니라 중립 0.5."""
        rows = self._rows(10)
        vfs = value_factors_mcap_quintile(rows, n_min=20)
        assert all(v == NEUTRAL_VALUE_FACTOR for v in vfs)

    def test_결측_중립(self):
        """배수 결측은 탈락 사유가 아니다 — 중립 0.5."""
        rows = [{"symbol": "X", "market_cap": 1e12, "fundamentals": {}}]
        assert value_factors_mcap_quintile(rows, n_min=1) == [NEUTRAL_VALUE_FACTOR]

    def test_결측이_최하위보다_높다(self):
        """같은 분위에서 '가장 비싼 종목' < '배수 결측 종목'."""
        rows = self._rows(40)
        rows.append({"symbol": "MISS", "market_cap": rows[0]["market_cap"],
                     "fundamentals": {}})
        vfs = value_factors_mcap_quintile(rows, n_min=5)
        worst = min(v for v in vfs[:-1])
        assert vfs[-1] == NEUTRAL_VALUE_FACTOR > worst


class TestDrawdownShape:
    def test_역U자_최고점_구간(self):
        assert drawdown_component(-42) == 1.0
        assert drawdown_component(-35) == 1.0
        assert drawdown_component(-50) == 1.0

    def test_너무_깊으면_감점(self):
        """컷라인(-70%) 바로 위가 1등이 되면 안 된다 — 적당한 낙폭보다 낮아야."""
        assert drawdown_component(-69) < drawdown_component(-42)
        assert drawdown_component(-70) == pytest.approx(0.2)

    def test_너무_얕으면_감점(self):
        assert drawdown_component(-30) < drawdown_component(-40)
        assert drawdown_component(-25) == 0.0

    def test_밴드_밖은_0(self):
        assert drawdown_component(-71) == 0.0
        assert drawdown_component(-10) == 0.0


class TestComposite:
    def test_감쇠는_바닥_아래로_안내려간다(self):
        """오래된 후보를 뒤로 미루되 배제하지는 않는다."""
        now = 1_000_000.0
        old = now - 365 * 86400
        assert age_decay(old, now) == 0.5
        assert age_decay(old, now, floor=0.3) == 0.3
        assert age_decay(now - 30 * 86400, now, half_life_days=30) == 0.5

    def test_병기_필드(self):
        rows = [{
            "symbol": "A",
            "market_cap": 1e12,
            "drawdown_1y_pct": -40,
            "first_seen_at": 1_000_000 - 86400,
            "fundamentals": {"pb": 0.8, "pe_trailing": 8, "roe": 0.1, "debt_ratio": 0.5},
        }]
        out = composite_scores(rows, now=1_000_000, n_min=1)
        assert "composite_value" in out[0]
        assert "value_factor" in out[0]
        assert 0 <= out[0]["age_decay"] <= 1


class TestCoverageReport:
    def test_분위별_카운트(self):
        rows = [{
            "market_cap": (i + 1) * 1e11,
            "fundamentals": {"pb": 1.0},
        } for i in range(100)]
        r = quintile_coverage_report(rows, n_min=20)
        assert r["ok"] is True
        assert all(b["n_valid"] >= 20 for b in r["quintiles"].values())

    def test_빈풀_ok_false(self):
        assert quintile_coverage_report([], n_min=20)["ok"] is False

    def test_시총전부결측_ok_false(self):
        rows = [{"market_cap": None, "fundamentals": {"pb": 1.0}} for _ in range(50)]
        assert quintile_coverage_report(rows, n_min=20)["ok"] is False


class TestBackfill:
    def test_corp_map_miss_집계(self, monkeypatch):
        monkeypatch.setattr("src.value_fin_backfill._save_fin_cache", lambda *a, **k: None)
        pool = [
            {"symbol": "AAA", "name": "A", "market_cap": 1e12},
            {"symbol": "BBB", "name": "B", "market_cap": 2e12},
        ]
        cache: dict = {}

        def fetch(api, corp, year):
            return {"fiscal_year": year, "revenue": 1e11, "operating_income": 1e10,
                    "net_income": 1e10, "equity": 5e11, "total_assets": 1e12,
                    "total_liabilities": 5e11, "current_assets": 3e11,
                    "current_liabilities": 2e11}

        summary = backfill_financials(
            "K", pool,
            corp_map={"AAA": "CORP_A"},
            cache=cache,
            years=(2024, 2023),
            sleep_s=0,
            fetch_fn=fetch,
            load_corp_map_fn=lambda *a, **k: {"AAA": "CORP_A"},
        )
        assert summary["corp_map_miss"] == 1
        assert summary["fetched_year_entries"] == 2
        assert "CORP_A" in cache
        assert "2024" in cache["CORP_A"]

    def test_캐시_hit_스킵(self, monkeypatch):
        monkeypatch.setattr("src.value_fin_backfill._save_fin_cache", lambda *a, **k: None)
        pool = [{"symbol": "AAA", "market_cap": 1e12}]
        cache = {"CORP_A": {
            "2024": {"fiscal_year": 2024, "equity": 1, "net_income": 1,
                     "revenue": 1, "operating_income": 1, "total_assets": 1,
                     "total_liabilities": 1, "current_assets": 1,
                     "current_liabilities": 1},
            "2023": {"fiscal_year": 2023, "equity": 1, "net_income": 1,
                     "revenue": 1, "operating_income": 1, "total_assets": 1,
                     "total_liabilities": 1, "current_assets": 1,
                     "current_liabilities": 1},
        }}
        calls = []

        def fetch(*a, **k):
            calls.append(1)
            return None

        summary = backfill_financials(
            "K", pool, corp_map={"AAA": "CORP_A"}, cache=cache,
            years=(2024, 2023), sleep_s=0, fetch_fn=fetch,
            load_corp_map_fn=lambda *a, **k: {"AAA": "CORP_A"},
        )
        assert summary["skipped_complete"] == 1
        assert calls == []

    def test_불완전연도_재조회(self, monkeypatch):
        monkeypatch.setattr("src.value_fin_backfill._save_fin_cache", lambda *a, **k: None)
        pool = [{"symbol": "AAA", "market_cap": 1e12}]
        cache = {"CORP_A": {"2024": {"fiscal_year": 2024, "equity": None}}}
        calls = []

        def fetch(api, corp, year):
            calls.append(year)
            return {"fiscal_year": year, "equity": 1e11, "net_income": 1e10,
                    "revenue": 1, "operating_income": 1, "total_assets": 1,
                    "total_liabilities": 1, "current_assets": 1,
                    "current_liabilities": 1}

        backfill_financials(
            "K", pool, corp_map={"AAA": "CORP_A"}, cache=cache,
            years=(2024, 2023), sleep_s=0, fetch_fn=fetch,
            load_corp_map_fn=lambda *a, **k: {"AAA": "CORP_A"},
        )
        assert 2024 in calls and 2023 in calls

    def test_coverage_윈도우연도만(self, monkeypatch):
        monkeypatch.setattr("src.value_fin_backfill._save_fin_cache", lambda *a, **k: None)
        pool = [{"symbol": "AAA", "market_cap": 1e12}]
        cache = {"CORP_A": {
            "2020": {"fiscal_year": 2020, "equity": 1e11, "net_income": 1e10,
                     "revenue": 1, "operating_income": 1, "total_assets": 1,
                     "total_liabilities": 1, "current_assets": 1,
                     "current_liabilities": 1},
        }}

        def fetch(*a, **k):
            return None

        summary = backfill_financials(
            "K", pool, corp_map={"AAA": "CORP_A"}, cache=cache,
            years=(2024, 2023), sleep_s=0, fetch_fn=fetch,
            load_corp_map_fn=lambda *a, **k: {"AAA": "CORP_A"},
            n_min=1,
        )
        assert summary["coverage"]["ok"] is False
