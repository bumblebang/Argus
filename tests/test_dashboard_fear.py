"""대시보드 시장 심리(공포·탐욕) 패널 — 스파크라인·행 렌더·전체 render 무결성.

대시보드는 라이브 운영자의 유일한 관제창이라 '공포 패널이 죽어도 페이지는 산다'가
핵심 계약이다. 망가진 입력에서 예외가 안 나는지를 특히 고정한다.
"""
import time

import pytest

from scripts.dashboard import _fear_html, _sparkline, render


# ── 스파크라인 ─────────────────────────────────────────────────────
def test_sparkline_needs_two_points():
    assert _sparkline([], "#fff") == ""
    assert _sparkline(None, "#fff") == ""
    assert _sparkline([[100, 30.0]], "#fff") == ""


def test_sparkline_shape():
    svg = _sparkline([[100, 30.0], [200, 40.0], [300, 50.0]], "#ff5c63", w=240, h=44)
    assert svg.startswith("<svg viewBox='0 0 240 44'")
    assert "<polyline" in svg and "stroke='#ff5c63'" in svg
    assert "points='0.0," in svg and "239.0," in svg          # 좌우 끝까지 채운다
    assert "<circle" in svg                                    # 마지막 점 강조
    assert "stroke-dasharray='3 3'" in svg                     # 중립선 50
    assert "preserveAspectRatio" not in svg                    # 선 굵기 왜곡 금지


def test_sparkline_y_scale_is_absolute_not_autoscaled():
    """공포지수는 절대 스케일 — 같은 모양이 +50 옮겨지면 좌표도 달라져야 한다."""
    a = _sparkline([[1, 10.0], [2, 20.0], [3, 15.0]], "#fff")
    b = _sparkline([[1, 60.0], [2, 70.0], [3, 65.0]], "#fff")
    assert a != b
    # 0/100 을 넘어가는 값은 클램프(게이지 밖으로 선이 나가지 않게)
    assert _sparkline([[1, -50.0], [2, 0.0]], "#fff") == _sparkline(
        [[1, 0.0], [2, 0.0]], "#fff")


def test_sparkline_skips_garbage_points():
    assert _sparkline([[1, "쓰레기"], [2, 30.0]], "#fff") == ""   # 유효 1개 → 빈 문자열
    assert "<polyline" in _sparkline([[1, "x"], [2, 30.0], [3, 40.0], "쓰레기"], "#fff")


# ── 패널 ───────────────────────────────────────────────────────────
_US = {"score": 39.0, "rating": "fear", "prev_close": 38.9, "prev_1w": 41.3,
       "prev_1m": 30.0, "prev_1y": 63.7,
       "components": {"market_momentum_sp500": 30.8, "junk_bond_demand": 68.8,
                      "stock_price_breadth": 23.8},
       "market": "US", "source": "cnn"}
_KR = {"score": 17.2, "rating": "extreme_fear",
       "components": {"drawdown": 0.0, "ret_5d": 43.0},
       "inputs": {"index_drawdown_pct": -18.5, "index_ret_5d_pct": -1.4},
       "market": "KR", "source": "proxy_kr"}


def test_fear_html_empty_without_data():
    assert _fear_html({}) == ""
    assert _fear_html({"sentiment": {}, "fear_history": {}}) == ""
    assert _fear_html({"sentiment": {"vix": 16.9}}) == ""       # vix 만으론 안 그린다


def test_fear_html_kr_only_renders_one_row():
    h = _fear_html({"sentiment": {"fear_kr": _KR}})
    assert h.count("class=fg-wrap") == 1
    assert "국내" in h and "미국" not in h
    assert "시장 심리 (공포 · 탐욕)" in h


def test_fear_html_full():
    h = _fear_html({"sentiment": {"fear_kr": _KR, "fear_greed": _US},
                    "fear_history": {"us": [[i, 30.0 + i] for i in range(10)],
                                     "kr": [[1, 20.0], [2, 17.2]]}})
    assert h.count("class=fg-wrap") == 2
    assert h.index("국내") < h.index("미국")                    # KR 이 위
    # 점수·등급 한글·게이지 마커
    assert "17.2" in h and "39.0" in h
    assert "극단적 공포" in h and "공포</span>" in h
    assert "left:17.2%" in h and "left:39.0%" in h
    assert "class=fg-ef" in h and "class=fg-f" in h
    # 성분 한글 이름 + 스파크라인
    assert "지수낙폭" in h and "정크본드" in h and "브레드스" in h
    assert h.count("<polyline") == 2
    assert "누적 2포인트" in h and "최근 1년" in h
    # 부가정보
    assert "KOSPI 20일 고점 대비 -18.5%" in h and "5일 -1.4%" in h
    assert "브레드스 성분 없음(장전 배치 경로)" in h
    assert "VKOSPI 미제공" in h
    assert "1년 63.7" in h and "전일 38.9" in h


def test_fear_html_marks_stale_us():
    h = _fear_html({"sentiment": {"fear_greed": dict(_US, stale=True)}})
    assert "freshwarn" in h and "갱신 실패" in h


def test_fear_html_kr_with_breadth():
    kr = dict(_KR, inputs=dict(_KR["inputs"], breadth_above_ma20=0.263, n=19),
              missing=[], incomplete=False)
    h = _fear_html({"sentiment": {"fear_kr": kr}})
    assert "20일선 상회 26% (n=19)" in h
    assert "브레드스 성분 없음" not in h


def test_fear_html_kr_percentile_note():
    kr = dict(_KR, rating_basis="percentile", score_pct=12.0, rating="extreme_fear")
    h = _fear_html({"sentiment": {"fear_kr": kr}})
    assert "이력대비 12백분위 (50=평년)" in h


def test_fear_html_kr_vkospi_note():
    kr = dict(_KR, inputs=dict(_KR["inputs"], vkospi=17.5, put_call_ratio=0.73))
    h = _fear_html({"sentiment": {"fear_kr": kr}})
    assert "VKOSPI 17.50 (전일)" in h
    assert "풋콜 0.73" in h
    assert "점수 외 부가" in h


def test_fear_html_kr_incomplete_note():
    kr = dict(_KR, incomplete=True, missing=["breadth"], rating_basis="absolute")
    h = _fear_html({"sentiment": {"fear_kr": kr}})
    assert "불완전" in h
    assert "등급=합성 원점수 (이력 부족)" in h


def test_fear_html_thin_kr_series_shows_accumulating():
    h = _fear_html({"sentiment": {"fear_kr": _KR}, "fear_history": {"kr": [[1, 20.0]]}})
    assert "추이 누적 중" in h and "<polyline" not in h


@pytest.mark.parametrize("bad", [
    {"sentiment": None, "fear_history": None},
    {"sentiment": "쓰레기"},
    {"sentiment": {"fear_kr": "쓰레기", "fear_greed": None}},
    {"sentiment": {"fear_kr": {}, "fear_greed": {"score": None}}},
    {"sentiment": {"fear_greed": {"score": "없음", "rating": "몰라"}}},
    {"sentiment": {"fear_kr": {"score": 20, "rating": 3, "components": "쓰레기",
                               "inputs": ["나쁨"]}}},
    {"sentiment": {"fear_greed": dict(_US, prev_1y="쓰레기")},
     "fear_history": {"us": "쓰레기"}},
    {"sentiment": {"fear_greed": _US}, "fear_history": {"us": [None, [1], [2, 3]]}},
])
def test_fear_html_never_raises(bad):
    out = _fear_html(bad)
    assert isinstance(out, str)


def test_fear_html_unknown_rating_falls_back_to_score():
    h = _fear_html({"sentiment": {"fear_greed": {"score": 80.0, "rating": "wat"}}})
    assert "극단적 탐욕" in h and "class=fg-eg" in h


# ── 전체 render ────────────────────────────────────────────────────
def _base_d(**kw) -> dict:
    d = {"db": True, "now": time.time(), "hb": {"ticks": 1}, "hb_age": 3.0}
    d.update(kw)
    return d


def test_render_with_and_without_fear_data():
    plain = render(_base_d())
    # CSS 에는 클래스 정의가 늘 들어가므로 실제 패널 마크업(게이지)으로 판정한다.
    assert plain.startswith("<!doctype html>") and "class=fg-gauge" not in plain
    withfg = render(_base_d(sentiment={"fear_kr": _KR, "fear_greed": _US},
                            fear_history={"kr": [[1, 20.0], [2, 17.2]]}))
    assert "시장 심리 (공포 · 탐욕)" in withfg
    assert "class=fg-gauge" in withfg and withfg.endswith("</body></html>")
