"""지정가·재대사·세션별 체결 실측 요약."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from src.ops_exec import slip_pct, summarize_exec


def test_slip_buy_sell():
    assert abs(slip_pct("BUY", 100.0, 101.0) - 1.0) < 1e-9
    assert abs(slip_pct("SELL", 100.0, 99.0) - 1.0) < 1e-9
    assert slip_pct("BUY", 100.0, 99.0) < 0
    assert slip_pct("BUY", None, 100.0) is None


def test_summarize_exec_sessions_and_recon(monkeypatch):
    import src.ops_exec as oe

    def fake_session(market, now=None):
        # map ts → session for test
        if now and now < 100:
            return "premarket"
        if now and now < 200:
            return "regular"
        return "aftermarket"

    monkeypatch.setattr(oe, "current_session", fake_session)
    rows = [
        {"ts": 50, "kind": "live_order", "symbol": "005930",
         "payload": {"side": "BUY", "limit_price": 100, "price": 100.5, "qty": 1}},
        {"ts": 150, "kind": "live_order", "symbol": "005930",
         "payload": {"side": "SELL", "limit_price": 110, "price": 109, "qty": 1}},
        {"ts": 250, "kind": "live_order_pending", "symbol": "000660",
         "payload": {"status": "pending"}},
        {"ts": 260, "kind": "wide_spread_skip", "symbol": "000660",
         "payload": {}},
        {"ts": 300, "kind": "reconcile", "symbol": None,
         "payload": {"adopted": ["111"], "closed": [], "updated": ["005930"],
                     "holdings": 3}},
        {"ts": 400, "kind": "reconcile", "symbol": None,
         "payload": {"adopted": [], "closed": [], "updated": [], "holdings": 3}},
    ]
    out = summarize_exec(rows, market="KR")
    assert out["n_fills"] == 2
    assert out["n_pending"] == 1
    assert out["n_spread_skip"] == 1
    assert out["by_session"]["premarket"] == 1
    assert out["by_session"]["regular"] == 1
    assert out["avg_slip_pct"] is not None
    assert out["reconcile"]["n"] == 2
    assert out["reconcile"]["adopted"] == 1
    assert out["reconcile"]["noisy"] == 1
    assert out["reconcile_health"] in ("drift", "noisy", "quiet")
    assert "체결 2" in out["line"]


def test_exec_ops_html_renders():
    import dashboard as dash
    html = dash._exec_ops_html({
        "names": {"005930": "삼성전자"},
        "exec_ops": {
            "line": "체결 1 · 미체결 0 · 실패 0",
            "by_session": {"premarket": 1, "regular": 0, "aftermarket": 0},
            "session_labels": {"premarket": "프리", "regular": "정규",
                               "aftermarket": "애프터"},
            "avg_slip_pct": 0.12,
            "worst_slip_pct": 0.12,
            "reconcile": {"n": 2, "adopted": 0, "closed": 0, "updated": 0},
            "reconcile_health": "quiet",
            "fills_preview": [{
                "ts": 1_700_000_000, "symbol": "005930", "session": "premarket",
                "limit": 100, "fill": 100.1, "slip_pct": 0.1,
            }],
        },
    })
    assert "체결·재대사" in html
    assert "프리" in html
    assert "조용" in html
