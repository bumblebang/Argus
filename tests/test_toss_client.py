"""toss_client — OAuth 429 재시도 및 rate limiter 연동."""
from unittest.mock import MagicMock

import pytest

from src.config import TossCredentials
from src.toss_client import TossAPIError, TossClient


def _creds():
    return TossCredentials(
        base_url="https://openapi.test",
        client_id="test-id",
        client_secret="test-secret",
        account_no="1",
    )


def _mock_resp(status, *, headers=None, body=None):
    r = MagicMock()
    r.status_code = status
    r.headers = headers or {}
    r.text = body or ""
    if status == 200:
        r.json.return_value = {"access_token": "fresh-token", "expires_in": 3600}
    return r


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
