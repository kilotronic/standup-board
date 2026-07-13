"""Flask HTTP layer: GitHub-authenticated web page + per-owner session API.

Two ways in, both resolving to an ``owner`` (a verified GitHub email):
  * Browser  -> a signed Flask cookie session, set after GitHub OAuth login.
  * Client   -> ``Authorization: Bearer <signed-token>`` issued by this server.

The Roster is partitioned per owner, so a request can only ever see or mutate
its own sessions. Deploy with --workers 1: the Roster mutates an in-memory dict,
race-free only under a single worker process.
"""

import os
import secrets
import time
from collections.abc import Callable
from dataclasses import asdict
from functools import wraps
from typing import Any
from urllib.parse import urlencode

from flask import (
    Flask,
    jsonify,
    redirect,
    render_template_string,
    request,
    session,
    url_for,
)

from . import github
from .roster import DEFAULT_TTL_SECONDS, Roster
from .tokens import make_client_token, read_client_token

GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"

# Hide sessions on the board once they've gone quiet: a row with no activity
# heartbeat in this long is dropped from the page (the TTL prunes them entirely
# later; this just keeps the live board to what's actually happening now).
BOARD_FRESH_WINDOW_SECONDS = 4 * 3600


def _validate_pr(pr: object) -> str | None:
    """None if pr is absent/valid; an error message otherwise."""
    if pr is None:
        return None
    if not isinstance(pr, dict):
        return "pr must be an object"
    url = pr.get("url")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return "pr.url must be an http/https URL"
    if "number" not in pr:
        return "pr.number is required"
    return None


def _relative(delta: float) -> str:
    """Coarse 'Nm ago' string for a non-negative seconds delta."""
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


def _resolve(value: str | None, env: str) -> str:
    """Explicit arg wins (incl. empty, for tests); else fall back to env."""
    return value if value is not None else os.environ.get(env, "")


def _parse_allowlist(raw: str) -> frozenset[str]:
    """Normalize a comma/space-separated list of GitHub logins to a lower set."""
    return frozenset(part.lower() for part in raw.replace(",", " ").split() if part)


def create_app(
    roster: Roster | None = None,
    *,
    secret_key: str | None = None,
    github_client_id: str | None = None,
    github_client_secret: str | None = None,
    oauth_redirect_url: str | None = None,
    cookie_secure: bool | None = None,
    allowed_users: str | None = None,
    exchange_code: Callable[[str, str], str | None] | None = None,
    primary_email: Callable[[str], str | None] | None = None,
    fetch_login: Callable[[str], str | None] | None = None,
) -> Flask:
    app = Flask(__name__)
    ttl = float(os.environ.get("STANDUP_TTL_SECONDS", DEFAULT_TTL_SECONDS))
    app.config["ROSTER"] = roster if roster is not None else Roster(ttl_seconds=ttl)

    # Fail loudly at startup so a misconfigured deploy surfaces immediately
    # rather than silently breaking auth on the first request.
    secret_key = _resolve(secret_key, "SECRET_KEY")
    if not secret_key:
        raise RuntimeError("SECRET_KEY must be set")
    app.secret_key = secret_key

    gh_id = _resolve(github_client_id, "GITHUB_CLIENT_ID")
    if not gh_id:
        raise RuntimeError("GITHUB_CLIENT_ID must be set")
    gh_secret = _resolve(github_client_secret, "GITHUB_CLIENT_SECRET")
    if not gh_secret:
        raise RuntimeError("GITHUB_CLIENT_SECRET must be set")

    app.config["OAUTH_REDIRECT_URL"] = _resolve(
        oauth_redirect_url, "OAUTH_REDIRECT_URL"
    )

    # Railway terminates TLS; secure cookies in prod, relaxed for the http test
    # client. SameSite=Lax + signed state both guard the OAuth callback.
    secure = (
        cookie_secure
        if cookie_secure is not None
        else (os.environ.get("COOKIE_SECURE", "1") != "0")
    )
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=secure,
    )

    # GitHub access — injectable so tests never touch the network.
    if exchange_code is None:

        def exchange_code(code: str, redirect_uri: str) -> str | None:
            return github.exchange_code_for_token(gh_id, gh_secret, code, redirect_uri)

    if primary_email is None:
        primary_email = github.fetch_primary_verified_email
    if fetch_login is None:
        fetch_login = github.fetch_login

    # Optional sign-in allowlist by GitHub username. Empty => anyone with a
    # verified GitHub email may sign in (backward-compatible default).
    allowed = _parse_allowlist(_resolve(allowed_users, "GITHUB_ALLOWED_USERS"))

    def _redirect_uri() -> str:
        return app.config["OAUTH_REDIRECT_URL"] or url_for(
            "auth_callback", _external=True
        )

    # --- client (bearer-token) identity ---

    def require_auth(view: Callable) -> Callable:
        @wraps(view)
        def wrapper(*args, **kwargs) -> Any:
            header = request.headers.get("Authorization", "")
            token = header[7:] if header.startswith("Bearer ") else ""
            owner = read_client_token(secret_key, token)
            if owner is None:
                return jsonify({"error": "unauthorized"}), 401
            return view(owner, *args, **kwargs)

        return wrapper

    @app.get("/sessions")
    @require_auth
    def list_sessions(owner: str):
        roster: Roster = app.config["ROSTER"]
        # Treat empty string the same as absent — both mean "no filter".
        repo = request.args.get("repo") or None
        sessions = [asdict(s) for s in roster.list(owner=owner, repo=repo)]
        return jsonify({"sessions": sessions})

    @app.post("/sessions")
    @require_auth
    def register_session(owner: str):
        roster: Roster = app.config["ROSTER"]
        body = request.get_json(silent=True) or {}
        session_id = body.get("session_id")
        if not session_id:
            return jsonify({"error": "missing fields: session_id"}), 400
        # machine/repo required only when creating; preserved on update.
        if roster.get(owner, session_id) is None:
            missing = [k for k in ("machine", "repo") if not body.get(k)]
            if missing:
                return jsonify({"error": f"missing fields: {', '.join(missing)}"}), 400
        err = _validate_pr(body.get("active_pr"))
        if err:
            return jsonify({"error": err}), 400
        worktrees = body.get("worktrees")
        if worktrees is not None:
            if not isinstance(worktrees, list):
                return jsonify({"error": "worktrees must be a list"}), 400
            for wt in worktrees:
                if not isinstance(wt, dict) or not wt.get("path"):
                    return jsonify({"error": "each worktree needs a path"}), 400
                werr = _validate_pr(wt.get("pr"))
                if werr:
                    return jsonify({"error": f"worktree {werr}"}), 400
        updates = {
            k: body[k]
            for k in (
                "active_branch",
                "last_prompt",
                "goal",
                "current_step",
                "active_pr",
                "worktrees",
            )
            if k in body
        }
        session_obj = roster.register(
            owner=owner,
            session_id=session_id,
            machine=body.get("machine"),
            repo=body.get("repo"),
            **updates,
        )
        return jsonify(asdict(session_obj)), 200

    @app.delete("/sessions/<session_id>")
    @require_auth
    def deregister_session(owner: str, session_id: str):
        roster: Roster = app.config["ROSTER"]
        roster.deregister(owner, session_id)
        return "", 204

    @app.get("/healthz")
    def healthz():
        return jsonify({"status": "ok"})

    @app.get("/config")
    def config():
        # Public client bootstrap for device-flow login. client_id is not secret.
        return jsonify({"github_client_id": gh_id})

    @app.post("/auth/exchange")
    def auth_exchange():
        body = request.get_json(silent=True) or {}
        access = body.get("github_token")
        if not isinstance(access, str) or not access:
            return jsonify({"error": "missing github_token"}), 401
        login = None
        if allowed:
            login = fetch_login(access)
            if not login or login.lower() not in allowed:
                return jsonify(
                    {"error": "your GitHub account is not on the allowlist"}
                ), 403
        email = primary_email(access)
        if not email:
            return jsonify(
                {"error": "no verified primary email on your GitHub account"}
            ), 401
        return jsonify(
            {
                "token": make_client_token(secret_key, email),
                "login": login,
                "email": email,
            }
        )

    # --- browser (cookie) identity + OAuth ---

    @app.get("/")
    def home():
        owner = session.get("owner")
        if not owner:
            return render_template_string(LOGIN_HTML)
        roster: Roster = app.config["ROSTER"]
        now = time.time()
        # Newest first, and drop anything that hasn't checked in for >4h.
        # registered_at is refreshed on every facts heartbeat, so it is the
        # freshest "last seen" signal — the same value the "seen" column shows.
        live = [
            s
            for s in roster.list(owner=owner)
            if now - s.registered_at <= BOARD_FRESH_WINDOW_SECONDS
        ]
        live.sort(key=lambda s: s.registered_at, reverse=True)
        rows = []
        for s in live:
            # Stale = never posted, or older than 15m. We deliberately do NOT
            # compare against registered_at: presence is refreshed on every
            # facts heartbeat (each UserPromptSubmit), so "narrative older than
            # presence" would grey a fresh goal within seconds of the next hook.
            narrative_stale = (
                s.narrative_updated_at == 0.0
                or (now - s.narrative_updated_at) > 900  # 15m
            )
            rows.append(
                {
                    "repo": s.repo,
                    "machine": s.machine,
                    "active_branch": s.active_branch,
                    "active_pr": s.active_pr,
                    "goal": s.goal,
                    "current_step": s.current_step,
                    "last_prompt": s.last_prompt,
                    "worktrees": s.worktrees or [],
                    "live_ago": _relative(max(0.0, now - s.registered_at)),
                    "narrative_ago": (
                        _relative(max(0.0, now - s.narrative_updated_at))
                        if s.narrative_updated_at
                        else None
                    ),
                    "narrative_stale": narrative_stale,
                }
            )
        return render_template_string(
            BOARD_HTML,
            owner=owner,
            sessions=rows,
            token=make_client_token(secret_key, owner),
        )

    @app.get("/auth/login")
    def auth_login():
        state = secrets.token_urlsafe(16)
        session["oauth_state"] = state
        params = urlencode(
            {
                "client_id": gh_id,
                "redirect_uri": _redirect_uri(),
                "scope": "user:email",
                "state": state,
            }
        )
        return redirect(f"{GITHUB_AUTHORIZE_URL}?{params}")

    @app.get("/auth/callback")
    def auth_callback():
        state = request.args.get("state", "")
        expected = session.pop("oauth_state", None)
        if not state or state != expected:
            return jsonify({"error": "invalid oauth state"}), 400
        code = request.args.get("code", "")
        if not code:
            return jsonify({"error": "missing code"}), 400
        access = exchange_code(code, _redirect_uri())
        if not access:
            return jsonify({"error": "github token exchange failed"}), 502
        if allowed:
            login = fetch_login(access)
            if not login or login.lower() not in allowed:
                return jsonify(
                    {"error": "your GitHub account is not on the allowlist"}
                ), 403
        email = primary_email(access)
        if not email:
            return jsonify(
                {"error": "no verified primary email on your GitHub account"}
            ), 403
        session["owner"] = email
        return redirect(url_for("home"))

    @app.get("/auth/logout")
    def auth_logout():
        session.clear()
        return redirect(url_for("home"))

    return app


_CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { font: 15px/1.5 ui-sans-serif, system-ui, sans-serif; margin: 0;
  background: #0d1117; color: #e6edf3; }
a { color: #58a6ff; }
.muted { color: #8b949e; }
.wrap { max-width: 880px; margin: 0 auto; padding: 2rem 1.25rem; }
.card { max-width: 420px; margin: 12vh auto; padding: 2rem; text-align: center;
  background: #161b22; border: 1px solid #30363d; border-radius: 12px; }
.row { display: flex; align-items: center; justify-content: space-between; gap: .75rem; }
h1 { font-size: 1.4rem; margin: 0; letter-spacing: -.02em; }
h2 { font-size: 1rem; margin: 0 0 .25rem; }
.btn { display: inline-block; padding: .5rem .9rem; border: 0; border-radius: 8px;
  background: #238636; color: #fff; font: inherit; cursor: pointer; text-decoration: none; }
.btn-ghost { background: #21262d; color: #e6edf3; border: 1px solid #30363d; }
table { width: 100%; border-collapse: collapse; margin-top: 1.25rem; }
th, td { text-align: left; padding: .55rem .5rem; border-bottom: 1px solid #21262d; }
th { color: #8b949e; font-weight: 600; font-size: .82rem; text-transform: uppercase;
  letter-spacing: .04em; }
.token { margin-top: 2rem; padding-top: 1.5rem; border-top: 1px solid #21262d; }
.token .row { justify-content: flex-start; margin-top: .6rem; }
input#tok { flex: 1; padding: .5rem; font-family: ui-monospace, monospace; font-size: .82rem;
  background: #0d1117; color: #e6edf3; border: 1px solid #30363d; border-radius: 8px; }
code { font-family: ui-monospace, monospace; background: #161b22; padding: .05rem .3rem;
  border-radius: 4px; }
.ago {
  color: #6e7681;
  font-size: 0.78rem;
}
.wt {
  margin: 0.3rem 0 0;
  padding-left: 1.1rem;
}
details summary {
  cursor: pointer;
  color: #8b949e;
}
"""

# __CSS__ is a literal sentinel (not Jinja) so render_template_string never sees
# the CSS braces; it's substituted once at import time.
LOGIN_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>standup-board</title>
<style>__CSS__</style></head>
<body><main class="card">
  <h1>standup&middot;board</h1>
  <p class="muted">A presence board for your Claude Code agents.</p>
  <a class="btn" href="/auth/login">Sign in with GitHub</a>
</main></body></html>
""".replace("__CSS__", _CSS)


BOARD_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>standup-board</title>
<style>__CSS__</style></head>
<body><main class="wrap">
  <header class="row">
    <h1>standup&middot;board</h1>
    <span class="muted">{{ owner }} &middot; <a href="/auth/logout">sign out</a></span>
  </header>

{% if sessions %}
<table>
  <thead>
    <tr>
      <th>repo</th>
      <th>machine</th>
      <th>branch</th>
      <th>goal / step</th>
      <th>worktrees</th>
      <th>seen</th>
    </tr>
  </thead>
  <tbody>
    {% for s in sessions %}
    <tr>
      <td>{{ s.repo }}</td>
      <td>{{ s.machine }}</td>
      <td>
        {% if s.active_branch %}{{ s.active_branch }}{% else %}—{% endif %} {%
        if s.active_pr %}<a href="{{ s.active_pr.url }}"
          >#{{ s.active_pr.number }}</a
        >
        {{ s.active_pr.state }}{% endif %}
      </td>
      <td>
        {% if s.goal %}
        <div class="{{ 'muted' if s.narrative_stale else '' }}">
          {{ s.goal }}
        </div>
        {% if s.current_step %}
        <div class="muted">
          ↳ {{ s.current_step }} {% if s.narrative_ago %}<span class="ago"
            >· {{ s.narrative_ago }}</span
          >{% endif %}
        </div>
        {% endif %} {% else %}<span class="muted">— no goal posted yet</span>{%
        endif %} {% if s.last_prompt %}
        <div class="ago">⌨ {{ s.last_prompt }}</div>
        {% endif %}
      </td>
      <td>
        {% if s.worktrees %}
        <details>
          <summary>{{ s.worktrees | length }} worktrees</summary>
          <ul class="wt">
            {% for w in s.worktrees %}
            <li>
              {{ w.branch or "(detached)" }}{% if w.pr %}
              <a href="{{ w.pr.url }}">#{{ w.pr.number }}</a>{% endif %}
            </li>
            {% endfor %}
          </ul>
        </details>
        {% else %}—{% endif %}
      </td>
      <td class="ago">{{ s.live_ago }}</td>
    </tr>
    {% endfor %}
  </tbody>
</table>
{% else %}
<p class="muted">No active agent sessions.</p>
{% endif %}

  <section class="token">
    <h2>Your client token</h2>
    <p class="muted">Put this in <code>~/.config/standup/env</code> as
      <code>STANDUP_TOKEN</code> so your CLI/MCP clients post under your identity.</p>
    <div class="row">
      <input id="tok" type="password" readonly value="{{ token }}">
      <button class="btn btn-ghost" id="reveal" onclick="toggleTok()"
        aria-pressed="false">Reveal</button>
      <button class="btn" onclick="copyTok()">Copy</button>
    </div>
  </section>
</main>
<script>
function toggleTok() {
  const el = document.getElementById('tok');
  const btn = document.getElementById('reveal');
  const shown = el.type === 'text';
  el.type = shown ? 'password' : 'text';
  btn.textContent = shown ? 'Reveal' : 'Hide';
  btn.setAttribute('aria-pressed', shown ? 'false' : 'true');
}
function copyTok() {
  const el = document.getElementById('tok');
  const wasHidden = el.type === 'password';
  // Temporarily switch to text so select()/copy works even while masked.
  if (wasHidden) el.type = 'text';
  el.select();
  navigator.clipboard.writeText(el.value);
  if (wasHidden) el.type = 'password';
}
</script>
</body></html>
""".replace("__CSS__", _CSS)
