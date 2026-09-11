"""청산 사유 매핑 — 트레일링 스톱이 '손절'로 읽히던 경로 전부.

2026-09-11 삼화콘덴서(001820): 평단 111,100 → 고점 147,500 에서 래칫된 스톱
140,125 를 깨고 140,000 에 청산(+26%). 원장·대시보드·뇌 회고 모두 'stop_hit/손절'
이었다. 사유를 trail_stop 으로 분리하고, 이미 닫힌 행은 meta.trail_active 로 보정한다.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import dashboard as dash  # noqa: E402

from src import thesis_watch as tw  # noqa: E402
from src.attribution import recent_trades  # noqa: E402
from src.engine.store import Store  # noqa: E402
from src.exit_reasons import exit_reason_ko, refine_exit_reason  # noqa: E402
from src.lessons import build_symbol_lessons  # noqa: E402


_TRAIL_META = {"horizon": "swing", "trail_active": True, "trail_peak": 147500.0}


# ── 정의부: 보정 규칙 ────────────────────────────────────────────
def test_refine_only_touches_trailing_stop_hit():
    assert refine_exit_reason("stop_hit", _TRAIL_META) == "trail_stop"
    assert refine_exit_reason("stop_hit", json.dumps(_TRAIL_META)) == "trail_stop"
    # 트레일 흔적 없으면 진짜 손절 — 건드리지 않는다
    assert refine_exit_reason("stop_hit", {"horizon": "swing"}) == "stop_hit"
    assert refine_exit_reason("stop_hit", None) == "stop_hit"
    assert refine_exit_reason("stop_hit", "깨진 json{") == "stop_hit"
    # 다른 사유는 트레일 중이어도 그대로(분류를 새로 만들지 않는다)
    assert refine_exit_reason("target_hit", _TRAIL_META) == "target_hit"
    assert refine_exit_reason("brain", _TRAIL_META) == "brain"
    assert refine_exit_reason(None, _TRAIL_META) == ""


def test_labels_separate_trail_from_stop():
    assert exit_reason_ko("trail_stop") == "트레일링 스톱"
    assert exit_reason_ko("stop_hit") == "손절"
    assert exit_reason_ko("stop_hit", _TRAIL_META) == "트레일링 스톱"
    assert exit_reason_ko("strategy:macd") == "전략신호 (macd)"
    assert exit_reason_ko("live_sync") == "live_sync"      # 모르는 값은 삼키지 않음


# ── 대시보드: 구 원장도 바르게 읽힌다 ──────────────────────────
def test_reason_label_uses_meta_without_trailing_raw_suffix():
    assert dash._reason_label("trail_stop", "[exit] trail_stop") == "트레일링 스톱"
    # 구 행: exit_reason 은 stop_hit 이지만 meta 가 트레일이었다고 말한다
    assert dash._reason_label("stop_hit", "[exit] stop_hit",
                              json.dumps(_TRAIL_META)) == "트레일링 스톱"
    assert dash._reason_label("stop_hit", "[exit] stop_hit") == "손절"


def test_live_trade_label_enriched_from_closed_position_meta():
    ts = 1789087338.7
    d = {
        "closed_pos": [{"symbol": "001820", "exit_reason": "stop_hit",
                        "closed_at": ts, "pnl": 86700.0,
                        "meta": json.dumps(_TRAIL_META)}],
        "trade_theses": [],
    }
    e = {"kind": "live_order", "symbol": "001820", "ts": ts}
    pl = {"side": "SELL", "qty": 3, "price": 140000,
          "reason": "[exit] stop_hit", "exit_reason": "stop_hit"}
    why, _ = dash._live_trade_why_thesis(e, pl, d)
    assert why == "트레일링 스톱"


def test_store_trade_stats_refines_exit_reason():
    stats = dash._store_trade_stats([{
        "symbol": "001820", "market": "KR", "qty": 3, "avg_price": 111100.0,
        "exit_price": 140000.0, "pnl": 86700.0, "exit_reason": "stop_hit",
        "meta": json.dumps(_TRAIL_META), "closed_at": 1789087338.7,
    }])
    assert stats["closed"][0]["exit_reason"] == "trail_stop"


# ── 뇌 입력: 회고·최근거래 ───────────────────────────────────────
class _FakeStore:
    def __init__(self, rows):
        self._rows = rows

    def closed_trades(self, symbols=None):
        return self._rows


def test_lessons_exit_reflects_trailing():
    row = {"symbol": "001820", "qty": 3, "avg_price": 111100.0, "pnl": 86700.0,
           "exit_reason": "stop_hit", "strategy": "macd", "thesis": "MLCC 사이클",
           "opened_at": 1789000475.2, "closed_at": 1789087338.7,
           "meta": json.dumps(_TRAIL_META)}
    out = build_symbol_lessons(_FakeStore([row]), ["001820"])
    assert out["001820"]["recent"][0]["exit"] == "trail_stop"
    assert out["001820"]["wins"] == 1

    # meta 키가 아예 없는 구 행도 예외 없이(sqlite3.Row/dict 양쪽)
    bare = dict(row); bare.pop("meta")
    out2 = build_symbol_lessons(_FakeStore([bare]), ["001820"])
    assert out2["001820"]["recent"][0]["exit"] == "stop_hit"


def test_recent_trades_refines_exit_reason(tmp_path):
    store = Store(tmp_path / "t.db")
    pid = store.open_position("001820", "KR", qty=3, avg_price=111100.0,
                              strategy="macd", thesis="t", target_price=132000.0,
                              stop_price=140125.0, meta=_TRAIL_META)
    store.close_position(pid, exit_price=140000.0, reason="stop_hit")
    rows = recent_trades(store, limit=5)
    assert rows[0]["exit_reason"] == "trail_stop" and rows[0]["pnl"] > 0
    store.close()


# ── thesis 무효화 오탐: 이익 보호선은 '논거 사망'이 아니다 ────────
def test_trailing_stop_is_not_promoted_to_thesis_invalidation():
    pos = {"symbol": "001820", "stop_price": 140125.0, "opened_at": 1789000475.2,
           "meta": json.dumps(_TRAIL_META)}
    assert tw.audit_position(pos, price=140100.0, now=1789087338.7) == []

    # 트레일 전이면 stop 이 곧 무효화가 — 레거시 승격 유지
    plain = dict(pos, meta=json.dumps({"horizon": "swing"}))
    hits = tw.audit_position(plain, price=140100.0, now=1789087338.7)
    assert [h.kind for h in hits] == ["price"]

    # 명시적 thesis_invalidation.price 는 트레일과 무관하게 계속 감사한다
    explicit = dict(pos, meta=json.dumps(
        dict(_TRAIL_META, thesis_invalidation={"price": 141000.0})))
    assert [h.kind for h in tw.audit_position(
        explicit, price=140100.0, now=1789087338.7)] == ["price"]
