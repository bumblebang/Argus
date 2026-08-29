"""alert_check 푸시(ntfy) — 토픽 빈 값 no-op / 토픽 있으면 POST / 라이브 주문 이벤트 픽업."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import alert_check as ac  # noqa: E402
from src.engine.store import Store  # noqa: E402


def test_push_noop_when_topic_empty(monkeypatch):
    monkeypatch.setattr(ac, "_ntfy_topic", lambda: "")
    calls = []
    monkeypatch.setattr(ac.requests, "post",
                        lambda *a, **k: calls.append((a, k)))
    ac._push("제목", "본문")
    assert calls == []                                   # 토픽 없으면 전송 안 함


def test_push_posts_when_topic_set(monkeypatch):
    monkeypatch.setattr(ac, "_ntfy_topic", lambda: "mytopic")
    calls = []

    class _Resp:
        status_code = 200

    def fake_post(url, data=None, headers=None, timeout=None):
        calls.append({"url": url, "data": data, "headers": headers})
        return _Resp()

    monkeypatch.setattr(ac.requests, "post", fake_post)
    assert ac._push("Argus 체결", "본문 한글 메시지") is True
    assert len(calls) == 1
    assert calls[0]["url"] == "https://ntfy.sh/mytopic"
    assert "본문" in calls[0]["data"].decode("utf-8")     # 한글은 본문(utf-8)에
    # Title 헤더는 ASCII 안전화(비ASCII 는 '?' 로 치환) — 예외 없이 전송돼야 함
    calls[0]["headers"]["Title"].encode("ascii")


def test_push_live_orders_reads_events(tmp_path, monkeypatch):
    db = tmp_path / "bot.db"
    store = Store(db)
    store.log_event("live_order", "005930",
                    {"side": "BUY", "qty": 1, "price": 70000, "order_id": "ORD1",
                     "reason": "[agent] 모멘텀 반등 확인"})
    store.log_event("live_order_error", "000660",
                    {"error": "HTTP 400", "side": "SELL",
                     "exit_reason": "stop_hit", "reason": "[exit] stop_hit"})
    monkeypatch.setattr(ac, "DB", db)
    monkeypatch.setattr(ac, "_PUSH_STATE", tmp_path / "push_state.json")
    monkeypatch.setattr(ac, "_ntfy_topic", lambda: "mytopic")
    monkeypatch.setattr(ac, "_load_symbol_names",
                        lambda: {"005930": "삼성전자", "000660": "SK하이닉스"})
    pushed = []
    monkeypatch.setattr(ac, "_push", lambda title, msg: (pushed.append((title, msg)) or True))
    ac._push_live_orders(0)
    fill = next(m for _, m in pushed if "삼성전자" in m)
    assert "매수" in fill and "근거:" in fill and "모멘텀 반등" in fill
    assert "ORD1" not in fill and "id=" not in fill and "005930" not in fill
    err = next(m for _, m in pushed if "SK하이닉스" in m)
    assert "HTTP 400" in err and "근거:" in err and "손절" in err
    assert "000660" not in err
    # 상태 저장 → 두 번째 호출은 중복 푸시 안 함(멱등)
    pushed.clear()
    ac._push_live_orders(0)
    assert pushed == []


def test_format_live_order_msg_sell_with_thesis():
    names = {"005930": "삼성전자"}
    msg = ac._format_live_order_msg(
        "live_order", "005930",
        {"side": "SELL", "qty": 2, "price": 68000,
         "exit_reason": "brain", "reason": "[agent] thesis 깨짐 — 수급 이탈"},
        names)
    assert "매도 삼성전자" in msg
    assert "근거: 뇌 판단 — [agent] thesis 깨짐" in msg
    assert "id=" not in msg
