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


# --- what is sent to GitHub: the URL, method, headers, body and timeout


def _recording_urlopen(payload_bytes, sent):
    def opener(req, timeout=None):
        sent.append((req, timeout))
        return _FakeResp(payload_bytes)

    return opener


def test_exchange_code_posts_the_credentials_as_json_request(monkeypatch):
    import urllib.parse

    sent = []
    monkeypatch.setattr(
        github.urllib.request,
        "urlopen",
        _recording_urlopen(b'{"access_token": "gh-tok"}', sent),
    )
    github.exchange_code_for_token("cid", "csecret", "the-code", "https://cb")
    [(req, timeout)] = sent
    assert req.full_url == "https://github.com/login/oauth/access_token"
    assert req.get_method() == "POST"
    assert req.get_header("Accept") == "application/json"
    assert urllib.parse.parse_qs(req.data.decode()) == {
        "client_id": ["cid"],
        "client_secret": ["csecret"],
        "code": ["the-code"],
        "redirect_uri": ["https://cb"],
    }
    assert timeout == github.TIMEOUT


def test_exchange_code_ignores_a_payload_that_is_not_an_object(monkeypatch):
    monkeypatch.setattr(github.urllib.request, "urlopen", _fake_urlopen(b'["gh-tok"]'))
    assert github.exchange_code_for_token("c", "s", "code", "https://cb") is None


def _assert_api_get(req, timeout, url):
    assert req.full_url == url
    assert req.get_method() == "GET"
    assert req.get_header("Authorization") == "Bearer gh-tok"
    assert req.get_header("Accept") == "application/vnd.github+json"
    assert req.get_header("User-agent") == "standup-board"
    assert timeout == github.TIMEOUT


def test_fetch_email_sends_the_token_to_the_emails_api(monkeypatch):
    sent = []
    body = b'[{"email": "a@x.com", "primary": true, "verified": true}]'
    monkeypatch.setattr(
        github.urllib.request, "urlopen", _recording_urlopen(body, sent)
    )
    assert github.fetch_primary_verified_email("gh-tok") == "a@x.com"
    [(req, timeout)] = sent
    _assert_api_get(req, timeout, "https://api.github.com/user/emails")


def test_fetch_login_sends_the_token_to_the_user_api(monkeypatch):
    sent = []
    monkeypatch.setattr(
        github.urllib.request,
        "urlopen",
        _recording_urlopen(b'{"login": "alice"}', sent),
    )
    assert github.fetch_login("gh-tok") == "alice"
    [(req, timeout)] = sent
    _assert_api_get(req, timeout, "https://api.github.com/user")
