"""Deployment settings reach the running app: cookie flags, TTLs, redirect URL, GitHub wiring.

Every other test builds the app with explicit arguments and fakes, so these drive
the environment-variable and default paths a real deploy takes.
"""

import pytest
from itsdangerous import URLSafeSerializer
from standup_board import app as app_module
from standup_board.app import create_app
from standup_board.roster import Roster
from standup_board.tokens import make_client_token, read_client_token

SECRET = "test-secret"
ALICE = "alice@example.com"


def _app(**overrides):
    opts = dict(secret_key=SECRET, github_client_id="cid", github_client_secret="csec")
    opts.update(overrides)
    return create_app(**opts)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in (
        "COOKIE_SECURE",
        "STANDUP_TTL_SECONDS",
        "STANDUP_PARKED_TTL_SECONDS",
        "STANDUP_DB_PATH",
        "OAUTH_REDIRECT_URL",
        "GITHUB_ALLOWED_USERS",
    ):
        monkeypatch.delenv(var, raising=False)


# --- the session cookie


def test_session_cookie_is_httponly_lax_and_secure_by_default():
    app = _app(roster=Roster())
    assert app.config["SESSION_COOKIE_HTTPONLY"] is True
    assert app.config["SESSION_COOKIE_SAMESITE"] == "Lax"
    assert app.config["SESSION_COOKIE_SECURE"] is True


def test_the_cookie_set_at_login_carries_the_flags():
    client = _app(roster=Roster()).test_client()
    resp = client.get("/auth/login", base_url="https://board.example")
    cookie = resp.headers["Set-Cookie"]
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=Lax" in cookie


@pytest.mark.parametrize(("value", "secure"), [("0", False), ("1", True), ("", True)])
def test_cookie_secure_env(monkeypatch, value, secure):
    monkeypatch.setenv("COOKIE_SECURE", value)
    assert _app(roster=Roster()).config["SESSION_COOKIE_SECURE"] is secure


@pytest.mark.parametrize("explicit", [True, False])
def test_explicit_cookie_secure_beats_the_env(monkeypatch, explicit):
    monkeypatch.setenv("COOKIE_SECURE", "1" if not explicit else "0")
    app = _app(roster=Roster(), cookie_secure=explicit)
    assert app.config["SESSION_COOKIE_SECURE"] is explicit


# --- TTLs and storage from the environment


def test_ttl_env_reaches_the_roster(monkeypatch):
    monkeypatch.setenv("STANDUP_TTL_SECONDS", "60")
    monkeypatch.setenv("STANDUP_PARKED_TTL_SECONDS", "600")
    roster = _app().config["ROSTER"]
    assert roster._ttl == 60.0
    assert roster._parked_ttl == 600.0


def test_ttl_env_expires_sessions(monkeypatch):
    monkeypatch.setenv("STANDUP_TTL_SECONDS", "60")
    roster = _app().config["ROSTER"]
    roster.register(owner=ALICE, session_id="s", machine="m", repo="r", now=1000.0)
    assert [s.session_id for s in roster.list(owner=ALICE, now=1059.0)] == ["s"]
    assert roster.list(owner=ALICE, now=1061.0) == []


def test_without_ttl_env_the_defaults_apply():
    roster = _app().config["ROSTER"]
    assert roster._ttl == app_module.DEFAULT_TTL_SECONDS
    assert roster._parked_ttl == app_module.DEFAULT_PARKED_TTL_SECONDS


def test_db_path_in_an_existing_directory(tmp_path, monkeypatch):
    db = tmp_path / "board.db"
    monkeypatch.setenv("STANDUP_DB_PATH", str(db))
    _app()
    _app()  # a restart: the directory and file already exist
    assert db.exists()


def test_db_path_survives_a_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("STANDUP_DB_PATH", str(tmp_path / "data" / "board.db"))
    _app().config["ROSTER"].register(owner=ALICE, session_id="s", machine="m", repo="r")
    assert [s.session_id for s in _app().config["ROSTER"].list(owner=ALICE)] == ["s"]


# --- OAuth redirect URL and the default GitHub wiring


def test_oauth_redirect_url_env(monkeypatch):
    monkeypatch.setenv("OAUTH_REDIRECT_URL", "https://board.example/auth/callback")
    resp = _app(roster=Roster()).test_client().get("/auth/login")
    assert "redirect_uri=https%3A%2F%2Fboard.example%2Fauth%2Fcallback" in resp.location


def test_without_a_redirect_url_the_callback_route_is_used():
    resp = (
        _app(roster=Roster()).test_client().get("/auth/login", base_url="http://h:8080")
    )
    assert "redirect_uri=http%3A%2F%2Fh%3A8080%2Fauth%2Fcallback" in resp.location


def test_default_wiring_calls_github_with_the_app_credentials(monkeypatch):
    calls = {}

    def exchange(client_id, client_secret, code, redirect_uri):
        calls["exchange"] = (client_id, client_secret, code, redirect_uri)
        return "gh-tok"

    def email(access):
        calls["email"] = access
        return ALICE

    def login(access):
        calls["login"] = access
        return "alice"

    monkeypatch.setattr(app_module.github, "exchange_code_for_token", exchange)
    monkeypatch.setattr(app_module.github, "fetch_primary_verified_email", email)
    monkeypatch.setattr(app_module.github, "fetch_login", login)
    app = _app(
        roster=Roster(),
        oauth_redirect_url="https://b/auth/callback",
        allowed_users="alice",
        cookie_secure=False,
    )
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["oauth_state"] = "st"
    resp = client.get("/auth/callback?state=st&code=the-code")
    assert resp.status_code == 302
    assert calls == {
        "exchange": ("cid", "csec", "the-code", "https://b/auth/callback"),
        "login": "gh-tok",
        "email": "gh-tok",
    }
    with client.session_transaction() as sess:
        assert sess["owner"] == ALICE


# --- tokens


def test_a_token_whose_payload_is_not_an_email_string_is_refused():
    signed = URLSafeSerializer(SECRET, salt="standup-client-token").dumps(
        {"owner": ALICE}
    )
    assert read_client_token(SECRET, signed) is None


def test_a_token_signed_with_another_key_is_refused():
    assert read_client_token(SECRET, make_client_token("other-secret", ALICE)) is None


def test_a_cookie_value_signed_with_the_same_key_is_not_a_client_token():
    other = URLSafeSerializer(SECRET, salt="some-other-purpose").dumps(ALICE)
    assert read_client_token(SECRET, other) is None


# --- small rules the board and the API rely on


@pytest.mark.parametrize(
    ("delta", "text"),
    [
        (0, "just now"),
        (59.9, "just now"),
        (60, "1m ago"),
        (119, "1m ago"),
        (3599, "59m ago"),
        (3600, "1h ago"),
        (86399, "23h ago"),
        (86400, "1d ago"),
        (2 * 86400 - 1, "1d ago"),
    ],
)
def test_relative_time_boundaries(delta, text):
    assert app_module._relative(delta) == text


@pytest.mark.parametrize(
    "url", ["https://github.com/o/r/pull/1", "http://ghe/o/r/pull/1"]
)
def test_an_http_or_https_pr_url_is_accepted(url):
    assert app_module._validate_pr({"url": url, "number": 1}) is None


@pytest.mark.parametrize(
    "url", ["javascript:alert(1)", "ftp://x/pr/1", ""]
)
def test_any_other_pr_url_is_refused(url):
    assert app_module._validate_pr({"url": url, "number": 1}) == (
        "pr.url must be an http/https URL"
    )


def test_the_store_works_from_another_thread(tmp_path):
    """Flask serves requests on worker threads; the SQLite connection must allow that."""
    import threading

    roster = Roster(db_path=str(tmp_path / "board.db"))
    errors = []

    def work():
        try:
            roster.register(owner=ALICE, session_id="t", machine="m", repo="r")
        except Exception as err:  # noqa: BLE001 - reported below
            errors.append(err)

    thread = threading.Thread(target=work)
    thread.start()
    thread.join()
    assert errors == []
    assert [s.session_id for s in roster.list(owner=ALICE)] == ["t"]
