"""Direct tests of standup_board.github's request/parsing logic.

The real network call (`with urllib.request.urlopen(...)`) is pragma'd — these
tests monkeypatch urllib.request.urlopen with fakes so the surrounding
extraction/validation logic (the real behavior) gets exercised without
touching the network.
"""

import urllib.error

from standup_board import github


class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_urlopen(payload_bytes):
    def opener(req, timeout=None):
        return _FakeResp(payload_bytes)

    return opener


def _raising_urlopen(exc):
    def opener(req, timeout=None):
        raise exc

    return opener


# --- exchange_code_for_token ---


def test_exchange_code_returns_token_on_success(monkeypatch):
    monkeypatch.setattr(
        github.urllib.request, "urlopen", _fake_urlopen(b'{"access_token": "gh-tok"}')
    )
    tok = github.exchange_code_for_token("cid", "csecret", "code", "https://cb")
    assert tok == "gh-tok"


def test_exchange_code_returns_none_on_network_failure(monkeypatch):
    monkeypatch.setattr(
        github.urllib.request,
        "urlopen",
        _raising_urlopen(urllib.error.URLError("down")),
    )
    assert (
        github.exchange_code_for_token("cid", "csecret", "code", "https://cb") is None
    )


def test_exchange_code_returns_none_when_payload_missing_token(monkeypatch):
    monkeypatch.setattr(
        github.urllib.request, "urlopen", _fake_urlopen(b'{"error": "bad_code"}')
    )
    assert (
        github.exchange_code_for_token("cid", "csecret", "code", "https://cb") is None
    )


def test_exchange_code_returns_none_when_token_not_string(monkeypatch):
    monkeypatch.setattr(
        github.urllib.request, "urlopen", _fake_urlopen(b'{"access_token": 5}')
    )
    assert (
        github.exchange_code_for_token("cid", "csecret", "code", "https://cb") is None
    )


def test_exchange_code_returns_none_on_malformed_json(monkeypatch):
    monkeypatch.setattr(github.urllib.request, "urlopen", _fake_urlopen(b"not-json"))
    assert (
        github.exchange_code_for_token("cid", "csecret", "code", "https://cb") is None
    )


# --- fetch_primary_verified_email ---


def test_fetch_primary_email_returns_verified_primary(monkeypatch):
    body = b'[{"email": "a@e.com", "primary": false, "verified": true}, {"email": "b@e.com", "primary": true, "verified": true}]'
    monkeypatch.setattr(github.urllib.request, "urlopen", _fake_urlopen(body))
    assert github.fetch_primary_verified_email("tok") == "b@e.com"


def test_fetch_primary_email_returns_none_when_no_entry_matches(monkeypatch):
    body = b'[{"email": "a@e.com", "primary": false, "verified": true}]'
    monkeypatch.setattr(github.urllib.request, "urlopen", _fake_urlopen(body))
    assert github.fetch_primary_verified_email("tok") is None


def test_fetch_primary_email_returns_none_when_response_not_list(monkeypatch):
    monkeypatch.setattr(
        github.urllib.request, "urlopen", _fake_urlopen(b'{"oops": true}')
    )
    assert github.fetch_primary_verified_email("tok") is None


def test_fetch_primary_email_returns_none_when_matching_email_empty(monkeypatch):
    body = b'[{"email": "", "primary": true, "verified": true}]'
    monkeypatch.setattr(github.urllib.request, "urlopen", _fake_urlopen(body))
    assert github.fetch_primary_verified_email("tok") is None


def test_fetch_primary_email_returns_none_on_network_failure(monkeypatch):
    monkeypatch.setattr(
        github.urllib.request, "urlopen", _raising_urlopen(OSError("down"))
    )
    assert github.fetch_primary_verified_email("tok") is None


# --- fetch_login ---


def test_fetch_login_returns_login_on_success(monkeypatch):
    monkeypatch.setattr(
        github.urllib.request, "urlopen", _fake_urlopen(b'{"login": "octocat"}')
    )
    assert github.fetch_login("tok") == "octocat"


def test_fetch_login_returns_none_when_not_dict(monkeypatch):
    monkeypatch.setattr(github.urllib.request, "urlopen", _fake_urlopen(b"[]"))
    assert github.fetch_login("tok") is None


def test_fetch_login_returns_none_when_login_missing_or_empty(monkeypatch):
    monkeypatch.setattr(
        github.urllib.request, "urlopen", _fake_urlopen(b'{"login": ""}')
    )
    assert github.fetch_login("tok") is None


def test_fetch_login_returns_none_on_network_failure(monkeypatch):
    monkeypatch.setattr(
        github.urllib.request,
        "urlopen",
        _raising_urlopen(urllib.error.URLError("down")),
    )
    assert github.fetch_login("tok") is None
