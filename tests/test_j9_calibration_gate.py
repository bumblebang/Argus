"""J9a — 캘리브레이션 "입증"이 노이즈로 사이징 평평 잠금을 풀던 결함.

백로그 재현(수정 전 통과하던 것):
  - rates=[0.3, 0.0, 0.6] (중간 붕괴 V자) -> calibrated=True
  - 저·고 구간 hit 둘 다 0.5 (동점)      -> calibrated=True
조건이 사실상 `len(rates)>=2 and rates[-1]>=rates[0]` 뿐이었다.
"""
import json

from src.calibration import (MIN_BIN, MIN_N, MIN_SLOPE, MIN_VALID_BINS,
                             _calibration_verdict, conviction_calibration,
                             sizing_enabled)


class _FakeStore:
    """get_closed_positions 만 흉내내는 최소 store."""

    def __init__(self, rows):
        self.rows = rows

    def get_closed_positions(self, since=None, limit=None):
        return self.rows


def _row(conviction: float, win: bool):
    return {"meta": json.dumps({"conviction": conviction}),
            "pnl": 100.0 if win else -100.0}


def _rows(spec: dict[float, tuple[int, int]]):
    """{구간대표 conviction: (표본수, 승수)} → 행 목록."""
    out = []
    for c, (n, wins) in spec.items():
        for i in range(n):
            out.append(_row(c, i < wins))
    return out


# ── 판정 함수 단위 (재현 케이스) ─────────────────────────────────
def test_v_shaped_rates_rejected():
    """중간 구간이 붕괴한 V자는 양 끝만 보면 통과했었다."""
    ok, why = _calibration_verdict(30, [0.3, 0.0, 0.6])
    assert not ok and "단조성" in why


def test_tie_rejected():
    ok, why = _calibration_verdict(30, [0.5, 0.5, 0.5])
    assert not ok and "기울기" in why


def test_two_valid_bins_rejected():
    """유효 구간 2개로는 부족하다(예전엔 통과)."""
    ok, why = _calibration_verdict(30, [0.2, 0.9])
    assert not ok and "유효 구간" in why


def test_thin_sample_rejected():
    ok, why = _calibration_verdict(MIN_N - 1, [0.2, 0.5, 0.9])
    assert not ok and "표본" in why


def test_monotone_with_slope_passes():
    ok, why = _calibration_verdict(30, [0.2, 0.5, 0.9])
    assert ok and "통과" in why


def test_slope_exactly_at_threshold_passes():
    ok, _ = _calibration_verdict(30, [0.10, 0.12, 0.10 + MIN_SLOPE])
    assert ok


# ── 통합 (store 경유) ───────────────────────────────────────────
def test_calibration_reports_reason():
    store = _FakeStore(_rows({0.2: (6, 1), 0.6: (7, 3), 0.9: (8, 7)}))
    cal = conviction_calibration(store)
    assert cal["n"] == 21 >= MIN_N
    assert cal["valid_bins"] == 3
    assert cal["calibrated"] is True
    assert "통과" in cal["calibration_reason"]


def test_v_shape_locks_sizing_flat():
    """저 0.5 / 중 0.0 / 고 0.6 — 중간 붕괴."""
    store = _FakeStore(_rows({0.2: (6, 3), 0.6: (7, 0), 0.9: (8, 5)}))
    cal = conviction_calibration(store)
    assert cal["n"] >= MIN_N
    assert cal["calibrated"] is False
    assert sizing_enabled(store, configured=True) is False


def test_sizing_stays_off_when_configured_off():
    store = _FakeStore(_rows({0.2: (6, 1), 0.6: (7, 3), 0.9: (8, 7)}))
    assert sizing_enabled(store, configured=False) is False


def test_small_bins_do_not_count_as_valid():
    """n>=MIN_N 이어도 MIN_BIN 미만 구간은 유효 구간이 아니다."""
    spec = {0.2: (MIN_BIN - 1, 0), 0.45: (MIN_BIN - 1, 1),
            0.6: (MIN_BIN - 1, 2), 0.75: (MIN_BIN - 1, 3), 0.9: (MIN_BIN - 1, 4)}
    store = _FakeStore(_rows(spec))
    cal = conviction_calibration(store)
    assert cal["n"] >= MIN_N
    assert cal["valid_bins"] < MIN_VALID_BINS
    assert cal["calibrated"] is False


def test_parent_id_slices_count_as_one_trade():
    """부분매도 3 slice / 같은 parent_id → 캘리브 표본 1건 (attribution 과 일치)."""
    rows = [
        {"id": 1, "parent_id": 1, "qty": 1, "pnl": 10,
         "meta": json.dumps({"conviction": 0.9})},
        {"id": 2, "parent_id": 1, "qty": 1, "pnl": -4,
         "meta": json.dumps({"conviction": 0.9})},
        {"id": 3, "parent_id": 1, "qty": 1, "pnl": 1,
         "meta": json.dumps({"conviction": 0.9})},
    ]
    cal = conviction_calibration(_FakeStore(rows))
    assert cal["n"] == 1
    # 순손익 +7 → 승
    assert cal["by_bin"]["0.85-1.01"]["n"] == 1
    assert cal["by_bin"]["0.85-1.01"]["hit_rate"] == 1.0


def test_brier_reported_but_not_gating():
    store = _FakeStore(_rows({0.2: (6, 1), 0.6: (7, 3), 0.9: (8, 7)}))
    cal = conviction_calibration(store)
    assert cal["brier"] is not None
    assert cal["calibrated"] is True          # Brier 값과 무관하게 통과
