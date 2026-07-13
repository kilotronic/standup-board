"""Thin stdlib wrappers over the GitHub OAuth web flow.

Kept dependency-free (urllib only) and side-effect-isolated so create_app can
inject fakes in tests. Every network failure degrades to None — the caller turns
that into an HTTP error rather than a stack trace.
"""

import json
import urllib.error
import urllib.parse
import urllib.request

TOKEN_URL = "https://github.com/login/oauth/access_token"
EMAILS_URL = "https://api.github.com/user/emails"
USER_URL = "https://api.github.com/user"
TIMEOUT = 10.0


def exchange_code_for_token(
    client_id: str, client_secret: str, code: str, redirect_uri: str
) -> str | None:
    """Trade an OAuth ``code`` for a GitHub access token; None on failure."""
    data = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
        }
    ).encode()
    req = urllib.request.Request(TOKEN_URL, data=data, method="POST")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # pragma: no cover
            payload = json.loads(resp.read().decode())
    except (urllib.error.URLError, OSError, ValueError):
        return None
    token = payload.get("access_token") if isinstance(payload, dict) else None
    return token if isinstance(token, str) and token else None


def fetch_primary_verified_email(access_token: str) -> str | None:
    """Return the user's primary, verified email, or None if absent/unreachable."""
    req = urllib.request.Request(EMAILS_URL)
    req.add_header("Authorization", f"Bearer {access_token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "standup-board")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # pragma: no cover
            emails = json.loads(resp.read().decode())
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if not isinstance(emails, list):
        return None
    for entry in emails:
        if isinstance(entry, dict) and entry.get("primary") and entry.get("verified"):
            email = entry.get("email")
            return email if isinstance(email, str) and email else None
    return None


def fetch_login(access_token: str) -> str | None:
    """Return the authenticated user's GitHub login (username), or None.

    Used only when an allowlist is configured. ``login`` is public profile data,
    returned for any valid token — no extra OAuth scope beyond ``user:email``.
    """
    req = urllib.request.Request(USER_URL)
    req.add_header("Authorization", f"Bearer {access_token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "standup-board")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # pragma: no cover
            data = json.loads(resp.read().decode())
    except (urllib.error.URLError, OSError, ValueError):
        return None
    login = data.get("login") if isinstance(data, dict) else None
    return login if isinstance(login, str) and login else None
