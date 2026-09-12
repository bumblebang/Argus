"""toss_client — OAuth 429 재시도 및 rate limiter 연동.

부재조건(P0):
- 토큰 캐시는 cwd 가 아니라 ROOT/data 고정.
- 디스크 캐시 로드 시 issued_at 복원 → 최근 토큰 401 은 재발급 thrash 안 함.
"""
import json
import time
from unittest.mock import MagicMock

import pytest

from src.config import ROOT, TossCredentials
from src import toss_client as tc
from src.toss_client import TossAPIError, TossClient


def _creds():
    return TossCredentials(
        base_url="https://openapi.test",
        client_id="test-id",
        client_secret="test-secret",
        account_no="1",
    )


def _mock_resp(status, *, headers=None, body=None, json_body=None):
    r = MagicMock()
    r.status_code = status
    r.headers = headers or {}
    r.text = body or ""
    r.content = b"{}" if status == 200 else (body or "").encode()
    if json_body is not None:
        r.json.return_value = json_body
    elif status == 200:
        r.json.return_value = {"access_token": "fresh-token", "expires_in": 3600}
    return r


def test_token_cache_path_is_under_root():
    """부재: _TOKEN_CACHE 는 절대경로이며 ROOT/data 아래."""
    p = tc._TOKEN_CACHE
    assert p.is_absolute()
    assert p == ROOT / "data" / ".token.json"


def test_ensure_token_persists_issued_at(tmp_path, monkeypatch):
    cache = tmp_path / ".token.json"
    monkeypatch.setattr(tc, "_TOKEN_CACHE", cache)
    monkeypatch.setattr(tc.time, "sleep", lambda _s: None)

    client = TossClient(_creds(), rate_limiter=None)
    client.session.post = lambda *a, **k: _mock_resp(200)
    before = time.time()
    assert client._ensure_token() == "fresh-token"
    after = time.time()
    d = json.loads(cache.read_text(encoding="utf-8"))
    assert d["access_token"] == "fresh-token"
    assert before <= float(d["issued_at"]) <= after
    assert before <= client._token_issued_at <= after


def test_load_cached_restores_issued_at(tmp_path, monkeypatch):
    """부재: 캐시 로드 후 issued_at≠0 — 구버전 누락 시에도 now 로 채워 thrash 방지."""
    cache = tmp_path / ".token.json"
    monkeypatch.setattr(tc, "_TOKEN_CACHE", cache)
    issued = time.time() - 10
    cache.write_text(json.dumps({
        "client_id": "test-id",
        "access_token": "cached-tok",
        "exp": time.time() + 3600,
        "issued_at": issued,
    }), encoding="utf-8")

    client = TossClient(_creds(), rate_limiter=None)
    assert client._load_cached() is True
    assert client._token == "cached-tok"
    assert abs(client._token_issued_at - issued) < 0.01


def test_load_cached_missing_issued_at_uses_now(tmp_path, monkeypatch):
    cache = tmp_path / ".token.json"
    monkeypatch.setattr(tc, "_TOKEN_CACHE", cache)
    cache.write_text(json.dumps({
        "client_id": "test-id",
        "access_token": "legacy-tok",
        "exp": time.time() + 3600,
    }), encoding="utf-8")
    client = TossClient(_creds(), rate_limiter=None)
    t0 = time.time()
    assert client._load_cached() is True
    assert client._token_issued_at >= t0 - 1


def test_401_fresh_token_does_not_reissue(tmp_path, monkeypatch):
    """부재: 발급 후 60s 이내 401 → invalidate만, _invalidate_token 호출 없음."""
    monkeypatch.setattr(tc, "_TOKEN_CACHE", tmp_path / ".token.json")
    monkeypatch.setattr(tc.time, "sleep", lambda _s: None)

    client = TossClient(_creds(), rate_limiter=None)
    client._token = "fresh"
    client._token_exp = time.time() + 3600
    client._token_issued_at = time.time()  # 방금 발급
    invalidated = {"n": 0}
    client._invalidate_token = lambda: invalidated.__setitem__("n", invalidated["n"] + 1)

    calls = {"n": 0}

    def fake_request(method, url, **kw):
        calls["n"] += 1
        if calls["n"] <= 2:
            return _mock_resp(401, body='{"error":"invalid-token"}')
        return _mock_resp(200, json_body={"result": {"ok": True}}, body="{}")

    client.session.request = fake_request
    out = client._request("accounts")
    assert out == {"ok": True}
    assert invalidated["n"] == 0
    assert calls["n"] == 3


def test_401_stale_token_reissues_once(tmp_path, monkeypatch):
    """발급 61s+ 경과 401 → 1회 invalidate+재발급."""
    monkeypatch.setattr(tc, "_TOKEN_CACHE", tmp_path / ".token.json")
    monkeypatch.setattr(tc.time, "sleep", lambda _s: None)

    client = TossClient(_creds(), rate_limiter=None)
    client._token = "stale"
    client._token_exp = time.time() + 3600
    client._token_issued_at = time.time() - 61
    posts = {"n": 0}

    def fake_post(*a, **k):
        posts["n"] += 1
        return _mock_resp(200)

    client.session.post = fake_post
    # invalidate 후 _ensure_token 이 post 로 새 토큰
    real_invalidate = client._invalidate_token

    def wrapped_invalidate():
        real_invalidate()
        client._token = None  # ensure re-fetch

    client._invalidate_token = wrapped_invalidate

    calls = {"n": 0}

    def fake_request(method, url, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return _mock_resp(401, body='{"error":"invalid-token"}')
        return _mock_resp(200, json_body={"result": {"ok": True}}, body="{}")

    client.session.request = fake_request
    out = client._request("accounts")
    assert out == {"ok": True}
    assert posts["n"] >= 1
    assert calls["n"] == 2


def test_invalidate_clears_issued_at(tmp_path, monkeypatch):
    monkeypatch.setattr(tc, "_TOKEN_CACHE", tmp_path / ".token.json")
    client = TossClient(_creds(), rate_limiter=None)
    client._token = "x"
    client._token_exp = time.time() + 3600
    client._token_issued_at = time.time()
    (tmp_path / ".token.json").write_text("{}", encoding="utf-8")
    client._invalidate_token()
    assert client._token is None
    assert client._token_issued_at == 0.0
    assert not (tmp_path / ".token.json").exists()


def test_token_429_retries_then_success(tmp_path, monkeypatch):
    """토큰 POST 429 → Retry-After 백오프 후 재시도 → 성공."""
    monkeypatch.setattr("src.toss_client._TOKEN_CACHE", tmp_path / ".token.json")
    monkeypatch.setattr("src.toss_client.time.sleep", lambda _s: None)

    posts = iter([
        _mock_resp(429, headers={"Retry-After": "1"}),
        _mock_resp(200),
    ])
    client = TossClient(_creds(), rate_limiter=None)
    client.session.post = lambda *a, **k: next(posts)

    assert client._ensure_token() == "fresh-token"
    assert client._token == "fresh-token"


def test_token_429_exhaust_raises(tmp_path, monkeypatch):
    """토큰 POST 429 연속 → TossAPIError(429)."""
    monkeypatch.setattr("src.toss_client._TOKEN_CACHE", tmp_path / ".token.json")
    monkeypatch.setattr("src.toss_client.time.sleep", lambda _s: None)

    client = TossClient(_creds(), rate_limiter=None)
    client.session.post = lambda *a, **k: _mock_resp(429, headers={"Retry-After": "0"})

    with pytest.raises(TossAPIError) as exc:
        client._ensure_token()
    assert exc.value.status == 429


def test_request_429_retries_per_attempt(tmp_path, monkeypatch):
    """API _request: 재시도마다 _acquire 호출(429 폭주 방지)."""
    monkeypatch.setattr("src.toss_client._TOKEN_CACHE", tmp_path / ".token.json")
    monkeypatch.setattr("src.toss_client.time.sleep", lambda _s: None)

    acquires: list[str] = []

    class _RL:
        def acquire(self, group):
            acquires.append(group)

    client = TossClient(_creds(), rate_limiter=_RL())
    client._token = "cached"
    client._token_exp = 1e12

    calls = {"n": 0}

    def fake_request(method, url, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return _mock_resp(429, headers={"Retry-After": "0"})
        r = _mock_resp(200)
        r.json.return_value = {"result": {"ok": True}}
        return r

    client.session.request = fake_request
    out = client._request("accounts")
    assert out == {"ok": True}
    assert len(acquires) == 2
    assert acquires[0] == "ACCOUNT"


def test_place_order_amount_sends_us_market_amount_only():
    """소수점 BUY용 금액 주문은 quantity/price/timeInForce를 섞지 않는다."""
    client = TossClient(_creds(), rate_limiter=None)
    seen = {}

    def fake_request(name, **kw):
        seen["name"] = name
        seen.update(kw)
        return {"orderId": "A1"}

    client._request = fake_request
    out = client.place_order(
        account_seq=1, symbol="AAPL", side="BUY",
        order_amount="50.00", order_type="MARKET")

    assert out == {"orderId": "A1"}
    assert seen["name"] == "order_create"
    assert seen["json"] == {
        "symbol": "AAPL",
        "side": "BUY",
        "orderType": "MARKET",
        "orderAmount": "50.00",
    }


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"qty": 1, "order_amount": "100"},
        {"order_amount": "100", "order_type": "LIMIT"},
    ],
)
def test_place_order_rejects_invalid_quantity_amount_combinations(kwargs):
    client = TossClient(_creds(), rate_limiter=None)
    with pytest.raises(ValueError):
        client.place_order(
            account_seq=1, symbol="AAPL", side="BUY", **kwargs)


def test_get_rankings_clamps_count_over_100(monkeypatch):
    """API count>100 → 400 방지: 요청은 100으로 클램프."""
    client = TossClient(_creds(), rate_limiter=None)
    seen = {}

    def fake_request(key, *, params=None, **_kw):
        seen["key"] = key
        seen["params"] = params
        return {"rankings": []}

    monkeypatch.setattr(client, "_request", fake_request)
    out = client.get_rankings(
        rank_type="MARKET_TRADING_AMOUNT",
        market_country="KR",
        duration="realtime",
        count=250,
    )
    assert out == {"rankings": []}
    assert seen["key"] == "rankings"
    assert seen["params"]["count"] == TossClient.MAX_RANKINGS_COUNT
    assert seen["params"]["type"] == "MARKET_TRADING_AMOUNT"
