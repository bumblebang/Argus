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

    def fake_post(url, data=None, headers=None, timeout=None):
        calls.append({"url": url, "data": data, "headers": headers})
        return None

    monkeypatch.setattr(ac.requests, "post", fake_post)
    ac._push("Argus 체결", "본문 한글 메시지")
    assert len(calls) == 1
    assert calls[0]["url"] == "https://ntfy.sh/mytopic"
    assert "본문" in calls[0]["data"].decode("utf-8")     # 한글은 본문(utf-8)에
    # Title 헤더는 ASCII 안전화(비ASCII 는 '?' 로 치환) — 예외 없이 전송돼야 함
    calls[0]["headers"]["Title"].encode("ascii")


def test_push_live_orders_reads_events(tmp_path, monkeypatch):
    db = tmp_path / "bot.db"
    store = Store(db)
    store.log_event("live_order", "005930",
                    {"side": "BUY", "qty": 1, "price": 70000, "order_id": "ORD1"})
    store.log_event("live_order_error", "000660", {"error": "HTTP 400"})
    monkeypatch.setattr(ac, "DB", db)
    monkeypatch.setattr(ac, "_PUSH_STATE", tmp_path / "push_state.json")
    monkeypatch.setattr(ac, "_ntfy_topic", lambda: "mytopic")
    pushed = []
    monkeypatch.setattr(ac, "_push", lambda title, msg: pushed.append((title, msg)))
    ac._push_live_orders(0)
    assert any("005930" in m and "ORD1" in m for _, m in pushed)     # 체결 픽업
    assert any("000660" in m for _, m in pushed)                     # 에러 픽업
    # 상태 저장 → 두 번째 호출은 중복 푸시 안 함(멱등)
    pushed.clear()
    ac._push_live_orders(0)
    assert pushed == []
