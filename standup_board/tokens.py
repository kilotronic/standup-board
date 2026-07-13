"""Stateless client tokens: an itsdangerous-signed wrapper around an email.

A client token encodes the owner's verified email and is signed with the
server's SECRET_KEY. The server recovers the email by verifying the signature —
no database, no per-request GitHub call. Rotating SECRET_KEY invalidates every
issued token (the revocation lever). Tokens do not expire by default.
"""

from itsdangerous import BadSignature, URLSafeSerializer

# Namespaces the signature so a client token can't be swapped for a cookie/other
# value signed with the same SECRET_KEY.
_SALT = "standup-client-token"


def make_client_token(secret_key: str, email: str) -> str:
    """Return a signed, URL-safe token encoding ``email``."""
    return URLSafeSerializer(secret_key, salt=_SALT).dumps(email)


def read_client_token(secret_key: str, token: str) -> str | None:
    """Recover the email from a token, or None if absent/invalid/tampered."""
    if not token:
        return None
    try:
        email = URLSafeSerializer(secret_key, salt=_SALT).loads(token)
    except BadSignature:
        return None
    return email if isinstance(email, str) else None
