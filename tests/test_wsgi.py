import importlib
import sys

from flask import Flask


def test_wsgi_builds_app_from_env(monkeypatch):
    # wsgi.py runs create_app() at import time, which requires these env vars
    # (create_app raises RuntimeError otherwise) — set them and force a fresh
    # import so the module body actually executes under test.
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("GITHUB_CLIENT_ID", "cid")
    monkeypatch.setenv("GITHUB_CLIENT_SECRET", "csecret")
    sys.modules.pop("standup_board.wsgi", None)
    try:
        wsgi = importlib.import_module("standup_board.wsgi")
        assert isinstance(wsgi.app, Flask)
    finally:
        sys.modules.pop("standup_board.wsgi", None)
