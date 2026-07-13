import pytest
from standup_board import app as app_module
from standup_board.app import _relative, _validate_pr, create_app
from standup_board.roster import Roster
from standup_board.tokens import make_client_token, read_client_token

SECRET = "test-secret"
ALICE = "alice@example.com"
BOB = "bob@example.com"


def build_app(roster=None, **overrides):
    """Create an app with test-friendly GitHub injectables; override per test."""
    opts = dict(
        secret_key=SECRET,
        github_client_id="cid",
        github_client_secret="csecret",
        oauth_redirect_url="https://app.example/auth/callback",
        cookie_secure=False,  # test client speaks http
        # default: only "good-code" exchanges successfully
        exchange_code=lambda code, redirect_uri: (
            "gh-tok" if code == "good-code" else None
        ),
        primary_email=lambda access_token: ALICE,
    )
    opts.update(overrides)
    app = create_app(roster=roster if roster is not None else Roster(), **opts)
    app.config["TESTING"] = True
    return app


@pytest.fixture
def roster():
    r = Roster()
    r.register(owner=ALICE, session_id="s1", machine="mini", repo="partygame")
    return r


@pytest.fixture
def client(roster):
    return build_app(roster).test_client()


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def alice_auth():
    return _auth(make_client_token(SECRET, ALICE))


def bob_auth():
    return _auth(make_client_token(SECRET, BOB))


def login(client, owner):
    with client.session_transaction() as sess:
        sess["owner"] = owner


# --- bearer-authenticated session API ---


def test_get_sessions_requires_auth(client):
    assert client.get("/sessions").status_code == 401


def test_get_sessions_rejects_invalid_token(client):
    assert client.get("/sessions", headers=_auth("garbage")).status_code == 401


def test_get_sessions_returns_own_roster(client):
    resp = client.get("/sessions", headers=alice_auth())
    assert resp.status_code == 200
    assert [s["session_id"] for s in resp.get_json()["sessions"]] == ["s1"]


def test_get_sessions_isolated_per_owner(client):
    # Bob has a valid token but no sessions — must not see Alice's.
    resp = client.get("/sessions", headers=bob_auth())
    assert resp.status_code == 200
    assert resp.get_json()["sessions"] == []


def test_get_sessions_filters_by_repo(client):
    resp = client.get("/sessions?repo=other", headers=alice_auth())
    assert resp.get_json()["sessions"] == []


def test_get_sessions_empty_repo_returns_all(client):
    resp = client.get("/sessions?repo=", headers=alice_auth())
    assert [s["session_id"] for s in resp.get_json()["sessions"]] == ["s1"]


def test_post_sessions_requires_auth(client):
    assert client.post("/sessions", json={}).status_code == 401


def test_post_sessions_registers_under_caller(client):
    resp = client.post(
        "/sessions",
        headers=bob_auth(),
        json={"session_id": "b1", "machine": "studio", "repo": "dotfiles"},
    )
    assert resp.status_code == 200
    assert resp.get_json()["owner"] == BOB
    # visible to Bob...
    assert client.get("/sessions", headers=bob_auth()).get_json()["sessions"]
    # ...but not to Alice
    alice = client.get("/sessions", headers=alice_auth()).get_json()["sessions"]
    assert [s["session_id"] for s in alice] == ["s1"]


def test_post_sessions_missing_field_is_400(client):
    resp = client.post(
        "/sessions", headers=alice_auth(), json={"session_id": "x", "machine": "y"}
    )
    assert resp.status_code == 400


def test_delete_is_scoped_to_owner(client):
    # Bob cannot delete Alice's session even knowing its id.
    assert client.delete("/sessions/s1", headers=bob_auth()).status_code == 204
    alice = client.get("/sessions", headers=alice_auth()).get_json()["sessions"]
    assert [s["session_id"] for s in alice] == ["s1"]  # untouched


def test_delete_removes_own_session(client):
    assert client.delete("/sessions/s1", headers=alice_auth()).status_code == 204
    assert client.get("/sessions", headers=alice_auth()).get_json()["sessions"] == []


def test_healthz_is_open(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok"}


def test_config_exposes_client_id_unauthenticated():
    app = build_app(github_client_id="pub-client-id")
    client = app.test_client()
    resp = client.get("/config")
    assert resp.status_code == 200
    assert resp.get_json() == {"github_client_id": "pub-client-id"}


# --- startup guard ---


def test_create_app_requires_secret_key():
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        create_app(
            roster=Roster(),
            secret_key="",
            github_client_id="cid",
            github_client_secret="csecret",
        )


def test_create_app_requires_github_credentials():
    with pytest.raises(RuntimeError, match="GITHUB_CLIENT_ID"):
        create_app(
            roster=Roster(),
            secret_key=SECRET,
            github_client_id="",
            github_client_secret="csecret",
        )


def test_create_app_requires_github_client_secret():
    with pytest.raises(RuntimeError, match="GITHUB_CLIENT_SECRET"):
        create_app(
            roster=Roster(),
            secret_key=SECRET,
            github_client_id="cid",
            github_client_secret="",
        )


# --- _validate_pr / _relative helpers ---


def test_validate_pr_rejects_non_dict():
    assert _validate_pr("not-a-dict") == "pr must be an object"


def test_validate_pr_rejects_missing_number():
    assert _validate_pr({"url": "http://h/1"}) == "pr.number is required"


def test_validate_pr_accepts_none():
    assert _validate_pr(None) is None


def test_relative_days_ago():
    assert _relative(90000) == "1d ago"


# --- default (non-injected) GitHub callables ---


def test_default_exchange_code_delegates_to_github_module(roster, monkeypatch):
    calls = []

    def fake_exchange(client_id, client_secret, code, redirect_uri):
        calls.append((client_id, client_secret, code, redirect_uri))
        return "gh-tok"

    monkeypatch.setattr(app_module.github, "exchange_code_for_token", fake_exchange)
    app = create_app(
        roster,
        secret_key=SECRET,
        github_client_id="real-cid",
        github_client_secret="real-csecret",
        oauth_redirect_url="https://app.example/auth/callback",
        cookie_secure=False,
        primary_email=lambda access_token: ALICE,
        # exchange_code intentionally omitted -> exercises the default closure
    )
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["oauth_state"] = "xyz"
    resp = c.get("/auth/callback?code=good-code&state=xyz")
    assert resp.status_code == 302
    assert calls == [
        ("real-cid", "real-csecret", "good-code", "https://app.example/auth/callback")
    ]


def test_default_primary_email_delegates_to_github_module(roster, monkeypatch):
    monkeypatch.setattr(
        app_module.github,
        "fetch_primary_verified_email",
        lambda tok: "real@example.com",
    )
    app = create_app(
        roster,
        secret_key=SECRET,
        github_client_id="cid",
        github_client_secret="csecret",
        oauth_redirect_url="https://app.example/auth/callback",
        cookie_secure=False,
        exchange_code=lambda code, redirect_uri: "gh-tok",
        # primary_email intentionally omitted -> exercises the default assignment
    )
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["oauth_state"] = "xyz"
    resp = c.get("/auth/callback?code=good-code&state=xyz")
    assert resp.status_code == 302
    with c.session_transaction() as sess:
        assert sess["owner"] == "real@example.com"


# --- request-shape validation ---


def test_register_sessions_missing_session_id_is_400(client):
    resp = client.post(
        "/sessions", headers=alice_auth(), json={"machine": "m", "repo": "r"}
    )
    assert resp.status_code == 400
    assert "session_id" in resp.get_json()["error"]


def test_callback_missing_code_is_400(client):
    with client.session_transaction() as sess:
        sess["oauth_state"] = "xyz"
    resp = client.get("/auth/callback?state=xyz")
    assert resp.status_code == 400
    assert "code" in resp.get_json()["error"]


# --- web page ---


def test_home_logged_out_shows_signin(client):
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "/auth/login" in body
    assert "Sign in with GitHub" in body


def test_home_logged_in_shows_own_sessions_and_token(client):
    login(client, ALICE)
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "partygame" in body  # Alice's session
    assert make_client_token(SECRET, ALICE) in body  # her client token, inline


def test_token_is_masked_by_default_with_reveal_control(client):
    login(client, ALICE)
    body = client.get("/").get_data(as_text=True)
    # Token field renders masked (password) by default, not as visible text.
    assert 'id="tok"' in body
    assert 'type="password"' in body
    # A reveal toggle exists alongside the copy button.
    assert 'id="reveal"' in body
    assert "Reveal" in body


def test_home_logged_in_does_not_leak_other_owners(client):
    login(client, BOB)  # Bob has no sessions
    body = client.get("/").get_data(as_text=True)
    assert "partygame" not in body


# --- OAuth flow ---


def test_login_redirects_to_github_with_state(client):
    resp = client.get("/auth/login")
    assert resp.status_code == 302
    loc = resp.headers["Location"]
    assert loc.startswith("https://github.com/login/oauth/authorize")
    assert "client_id=cid" in loc
    assert "scope=user" in loc  # user:email url-encoded
    assert "state=" in loc
    with client.session_transaction() as sess:
        assert sess.get("oauth_state")


def test_callback_happy_path_sets_owner(client):
    with client.session_transaction() as sess:
        sess["oauth_state"] = "xyz"
    resp = client.get("/auth/callback?code=good-code&state=xyz")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/")
    with client.session_transaction() as sess:
        assert sess["owner"] == ALICE


def test_callback_rejects_state_mismatch(client):
    with client.session_transaction() as sess:
        sess["oauth_state"] = "expected"
    resp = client.get("/auth/callback?code=good-code&state=forged")
    assert resp.status_code == 400
    with client.session_transaction() as sess:
        assert "owner" not in sess


def test_callback_rejects_when_no_verified_email(roster):
    app = build_app(roster, primary_email=lambda access_token: None)
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["oauth_state"] = "xyz"
    resp = c.get("/auth/callback?code=good-code&state=xyz")
    assert resp.status_code == 403
    with c.session_transaction() as sess:
        assert "owner" not in sess


# --- optional GitHub username allowlist ---


def _callback(app):
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["oauth_state"] = "xyz"
    return c, c.get("/auth/callback?code=good-code&state=xyz")


def test_callback_allows_user_on_allowlist(roster):
    app = build_app(
        roster,
        allowed_users="alice-gh, bob-gh",
        fetch_login=lambda access_token: "alice-gh",
    )
    c, resp = _callback(app)
    assert resp.status_code == 302
    with c.session_transaction() as sess:
        assert sess["owner"] == ALICE


def test_callback_rejects_user_not_on_allowlist(roster):
    app = build_app(
        roster,
        allowed_users="bob-gh",
        fetch_login=lambda access_token: "mallory",
    )
    c, resp = _callback(app)
    assert resp.status_code == 403
    with c.session_transaction() as sess:
        assert "owner" not in sess


def test_callback_allowlist_ignores_case_and_whitespace(roster):
    app = build_app(
        roster,
        allowed_users=" Alice-GH ,bob-gh",
        fetch_login=lambda access_token: "alice-gh",
    )
    _, resp = _callback(app)
    assert resp.status_code == 302


def test_callback_rejects_when_login_unavailable_and_allowlist_set(roster):
    app = build_app(
        roster,
        allowed_users="alice-gh",
        fetch_login=lambda access_token: None,
    )
    c, resp = _callback(app)
    assert resp.status_code == 403
    with c.session_transaction() as sess:
        assert "owner" not in sess


def test_callback_empty_allowlist_allows_anyone_without_fetching_login(roster):
    # No allowlist -> any verified GitHub user is accepted, and fetch_login is
    # never called (the default would hit the network). Backward-compatible.
    def explode(access_token):  # pragma: no cover - must never run
        raise AssertionError("fetch_login called with an empty allowlist")

    app = build_app(roster, allowed_users="", fetch_login=explode)
    c, resp = _callback(app)
    assert resp.status_code == 302
    with c.session_transaction() as sess:
        assert sess["owner"] == ALICE


def test_allowlist_resolves_from_env(roster, monkeypatch):
    monkeypatch.setenv("GITHUB_ALLOWED_USERS", "bob-gh")
    # allowed_users not passed -> falls back to the env var.
    app = build_app(roster, fetch_login=lambda access_token: "mallory")
    _, resp = _callback(app)
    assert resp.status_code == 403


def test_callback_handles_exchange_failure(client):
    with client.session_transaction() as sess:
        sess["oauth_state"] = "xyz"
    resp = client.get("/auth/callback?code=bad-code&state=xyz")
    assert resp.status_code == 502
    with client.session_transaction() as sess:
        assert "owner" not in sess


def test_logout_clears_session(client):
    login(client, ALICE)
    resp = client.get("/auth/logout")
    assert resp.status_code == 302
    with client.session_transaction() as sess:
        assert "owner" not in sess


def test_register_stores_and_returns_active_pr(client):
    pr = {"number": 7, "title": "x", "state": "OPEN", "url": "http://h/7"}
    resp = client.post(
        "/sessions",
        headers=alice_auth(),
        json={"session_id": "s9", "machine": "m", "repo": "partygame", "active_pr": pr},
    )
    assert resp.status_code == 200
    assert resp.get_json()["active_pr"] == pr


def test_register_rejects_active_pr_invalid_url(client):
    resp = client.post(
        "/sessions",
        headers=alice_auth(),
        json={
            "session_id": "s10",
            "machine": "m",
            "repo": "r",
            "active_pr": {"number": 1, "url": "javascript:alert(1)"},
        },
    )
    assert resp.status_code == 400


def test_register_rejects_worktrees_wrong_type(client):
    resp = client.post(
        "/sessions",
        headers=alice_auth(),
        json={
            "session_id": "s13",
            "machine": "m",
            "repo": "r",
            "worktrees": "not-a-list",
        },
    )
    assert resp.status_code == 400


def test_register_rejects_worktree_without_path(client):
    resp = client.post(
        "/sessions",
        headers=alice_auth(),
        json={
            "session_id": "s14",
            "machine": "m",
            "repo": "r",
            "worktrees": [{"branch": "main"}],
        },
    )
    assert resp.status_code == 400


def test_register_rejects_worktree_bad_pr(client):
    resp = client.post(
        "/sessions",
        headers=alice_auth(),
        json={
            "session_id": "s15",
            "machine": "m",
            "repo": "r",
            "worktrees": [
                {"path": "/a", "branch": "x", "pr": {"number": 1, "url": "ftp://no"}}
            ],
        },
    )
    assert resp.status_code == 400


def test_narrative_update_omits_machine_repo_when_session_exists(client):
    # Alice's fixture session "s1" already exists; a narrative-only update
    # (no machine/repo) must succeed and preserve repo.
    resp = client.post(
        "/sessions",
        headers=alice_auth(),
        json={"session_id": "s1", "goal": "ship it", "current_step": "coding"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["goal"] == "ship it"
    assert body["repo"] == "partygame"  # preserved from the fixture


def test_new_session_still_requires_machine_repo(client):
    resp = client.post(
        "/sessions",
        headers=alice_auth(),
        json={"session_id": "brand-new", "goal": "x"},
    )
    assert resp.status_code == 400


def test_web_table_renders_pr_link(client):
    client.post(
        "/sessions",
        headers=alice_auth(),
        json={
            "session_id": "s9",
            "machine": "m",
            "repo": "partygame",
            "active_pr": {
                "number": 7,
                "title": "x",
                "state": "OPEN",
                "url": "http://h/7",
            },
        },
    )
    login(client, ALICE)
    body = client.get("/").get_data(as_text=True)
    assert "http://h/7" in body
    assert "#7" in body


def test_board_shows_goal_step_and_worktrees(roster):
    roster.register(
        owner=ALICE,
        session_id="s1",
        machine="mini",
        repo="partygame",
        active_branch="feat/timer",
        goal="ship the timer",
        current_step="writing tests",
        last_prompt="fix the timer",
        worktrees=[
            {"path": "/a", "branch": "main", "pr": None},
            {
                "path": "/b",
                "branch": "feat/timer",
                "pr": {"number": 9, "state": "OPEN", "url": "http://h/9"},
            },
        ],
    )
    c = build_app(roster).test_client()
    login(c, ALICE)
    body = c.get("/").get_data(as_text=True)
    assert "ship the timer" in body  # goal
    assert "writing tests" in body  # current step
    assert "feat/timer" in body  # active branch
    assert "#9" in body  # active branch's PR (worktree pr)
    assert "2 worktrees" in body or "2&nbsp;worktrees" in body  # count badge


def test_exchange_returns_usable_client_token():
    app = build_app(
        primary_email=lambda tok: ALICE if tok == "gh-tok" else None,
    )
    client = app.test_client()
    resp = client.post("/auth/exchange", json={"github_token": "gh-tok"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["email"] == ALICE
    assert read_client_token(SECRET, body["token"]) == ALICE


def test_exchange_rejects_token_with_no_verified_email():
    app = build_app(primary_email=lambda tok: None)
    resp = app.test_client().post("/auth/exchange", json={"github_token": "bad"})
    assert resp.status_code == 401


def test_exchange_rejects_missing_or_nonstring_token():
    app = build_app(primary_email=lambda tok: ALICE)
    client = app.test_client()
    assert client.post("/auth/exchange", json={}).status_code == 401
    assert client.post("/auth/exchange", json={"github_token": 123}).status_code == 401
    assert client.post("/auth/exchange", json={"github_token": ""}).status_code == 401


def test_exchange_enforces_allowlist():
    app = build_app(
        allowed_users="onlybob",
        primary_email=lambda tok: ALICE,
        fetch_login=lambda tok: "alice",
    )
    resp = app.test_client().post("/auth/exchange", json={"github_token": "gh-tok"})
    assert resp.status_code == 403


def test_board_drops_stale_rows_and_orders_newest_first(roster):
    import time

    now = time.time()
    # Three sessions last seen at different times; the fixture's s1 is "now".
    roster.register(
        owner=ALICE, session_id="recent", machine="m-recent", repo="r", now=now - 60
    )  # 1m ago — kept
    roster.register(
        owner=ALICE, session_id="mid", machine="m-mid", repo="r", now=now - 2 * 3600
    )  # 2h ago — kept, older
    roster.register(
        owner=ALICE, session_id="old", machine="m-old", repo="r", now=now - 5 * 3600
    )  # 5h ago — dropped (>4h)
    c = build_app(roster).test_client()
    login(c, ALICE)
    body = c.get("/").get_data(as_text=True)
    assert "m-old" not in body  # no heartbeat in >4h → off the board
    assert "m-recent" in body and "m-mid" in body
    assert body.index("m-recent") < body.index("m-mid")  # newest first
