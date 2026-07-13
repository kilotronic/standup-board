"""Tests for the standup_board MCP server module.

The tool functions (`list_sessions`, `register_session`, ...) are plain
functions under the `@mcp.tool()` decorator, callable directly. The real
network call inside `_request` (`with urllib.request.urlopen(...)`) is
pragma'd; these tests monkeypatch `_request` (for the tool wrappers) or
`urllib.request.urlopen` (for `_request` itself) so the surrounding body-
building/validation logic is exercised without touching the network.
"""

import pytest

from standup_board import mcp as mcpmod


class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body
        self.headers_seen = {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# --- _config ---


def test_config_raises_when_unconfigured(monkeypatch, tmp_path):
    monkeypatch.setattr(mcpmod, "CONFIG_PATH", tmp_path / "env")
    monkeypatch.delenv("STANDUP_URL", raising=False)
    monkeypatch.delenv("STANDUP_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="not configured"):
        mcpmod._config()


def test_config_reads_from_file(monkeypatch, tmp_path):
    cfg_file = tmp_path / "env"
    cfg_file.write_text("STANDUP_URL=http://board\nSTANDUP_TOKEN=filetok\n")
    monkeypatch.setattr(mcpmod, "CONFIG_PATH", cfg_file)
    monkeypatch.delenv("STANDUP_URL", raising=False)
    monkeypatch.delenv("STANDUP_TOKEN", raising=False)
    cfg = mcpmod._config()
    assert cfg == {"STANDUP_URL": "http://board", "STANDUP_TOKEN": "filetok"}


def test_config_env_overrides_file(monkeypatch, tmp_path):
    cfg_file = tmp_path / "env"
    cfg_file.write_text("STANDUP_URL=http://file\nSTANDUP_TOKEN=filetok\n")
    monkeypatch.setattr(mcpmod, "CONFIG_PATH", cfg_file)
    monkeypatch.setenv("STANDUP_URL", "http://env")
    monkeypatch.setenv("STANDUP_TOKEN", "envtok")
    cfg = mcpmod._config()
    assert cfg == {"STANDUP_URL": "http://env", "STANDUP_TOKEN": "envtok"}


def test_config_ignores_blank_and_comment_lines(monkeypatch, tmp_path):
    cfg_file = tmp_path / "env"
    cfg_file.write_text("\n# a comment\nSTANDUP_URL=http://board\nSTANDUP_TOKEN=t\n")
    monkeypatch.setattr(mcpmod, "CONFIG_PATH", cfg_file)
    monkeypatch.delenv("STANDUP_URL", raising=False)
    monkeypatch.delenv("STANDUP_TOKEN", raising=False)
    assert mcpmod._config()["STANDUP_URL"] == "http://board"


# --- _machine ---


def test_machine_trims_dot_local_suffix(monkeypatch):
    monkeypatch.setattr(mcpmod.socket, "gethostname", lambda: "jasons-mini.local")
    assert mcpmod._machine() == "jasons-mini"


def test_machine_leaves_non_local_hostname(monkeypatch):
    monkeypatch.setattr(mcpmod.socket, "gethostname", lambda: "build-box")
    assert mcpmod._machine() == "build-box"


# --- _request ---


def _configure(monkeypatch, tmp_path):
    cfg_file = tmp_path / "env"
    cfg_file.write_text("STANDUP_URL=http://board\nSTANDUP_TOKEN=tok\n")
    monkeypatch.setattr(mcpmod, "CONFIG_PATH", cfg_file)
    monkeypatch.delenv("STANDUP_URL", raising=False)
    monkeypatch.delenv("STANDUP_TOKEN", raising=False)


def test_request_get_without_body(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["req"] = req
        return _FakeResp(b'{"sessions": []}')

    monkeypatch.setattr(mcpmod.urllib.request, "urlopen", fake_urlopen)
    result = mcpmod._request("GET", "/sessions")
    assert result == {"sessions": []}
    assert seen["req"].get_method() == "GET"
    assert seen["req"].get_header("Authorization") == "Bearer tok"
    assert seen["req"].data is None


def test_request_post_with_body_sets_content_type(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["req"] = req
        return _FakeResp(b"")

    monkeypatch.setattr(mcpmod.urllib.request, "urlopen", fake_urlopen)
    result = mcpmod._request("POST", "/sessions", {"a": 1})
    assert result is None  # empty body -> None
    req = seen["req"]
    assert req.get_method() == "POST"
    assert req.get_header("Content-type") == "application/json"
    assert req.data == b'{"a": 1}'


def test_request_propagates_config_error(monkeypatch, tmp_path):
    monkeypatch.setattr(mcpmod, "CONFIG_PATH", tmp_path / "missing")
    monkeypatch.delenv("STANDUP_URL", raising=False)
    monkeypatch.delenv("STANDUP_TOKEN", raising=False)
    with pytest.raises(RuntimeError):
        mcpmod._request("GET", "/sessions")


# --- tool wrappers (unit-tested against a monkeypatched _request) ---


def test_list_sessions_defaults_to_unfiltered_path(monkeypatch):
    calls = []
    monkeypatch.setattr(
        mcpmod,
        "_request",
        lambda method, path, body=None: (
            calls.append((method, path)) or {"sessions": [{"x": 1}]}
        ),
    )
    out = mcpmod.list_sessions()
    assert calls == [("GET", "/sessions")]
    assert out == [{"x": 1}]


def test_list_sessions_filters_by_repo(monkeypatch):
    calls = []
    monkeypatch.setattr(
        mcpmod,
        "_request",
        lambda method, path, body=None: (
            calls.append((method, path)) or {"sessions": []}
        ),
    )
    mcpmod.list_sessions(repo="a b")
    assert calls == [("GET", "/sessions?repo=a%20b")]


def test_list_sessions_handles_none_result(monkeypatch):
    monkeypatch.setattr(mcpmod, "_request", lambda method, path, body=None: None)
    assert mcpmod.list_sessions() == []


def test_register_session_omits_absent_optional_fields(monkeypatch):
    bodies = []
    monkeypatch.setattr(
        mcpmod,
        "_request",
        lambda method, path, body=None: bodies.append(body) or {"ok": True},
    )
    mcpmod.register_session(session_id="s1", machine="m", repo="r")
    assert bodies[0] == {"session_id": "s1", "machine": "m", "repo": "r"}


def test_register_session_includes_optional_fields_when_given(monkeypatch):
    bodies = []
    monkeypatch.setattr(
        mcpmod,
        "_request",
        lambda method, path, body=None: bodies.append(body) or {"ok": True},
    )
    mcpmod.register_session(
        session_id="s1",
        machine="m",
        repo="r",
        active_branch="feat/x",
        last_prompt="do it",
    )
    assert bodies[0]["active_branch"] == "feat/x"
    assert bodies[0]["last_prompt"] == "do it"


def test_update_status_raises_without_session_id(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    with pytest.raises(RuntimeError, match="CLAUDE_CODE_SESSION_ID"):
        mcpmod.update_status(goal="ship it")


def test_update_status_builds_full_body(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-1")
    monkeypatch.setattr(mcpmod, "_machine", lambda: "mini")
    bodies = []
    monkeypatch.setattr(
        mcpmod,
        "_request",
        lambda method, path, body=None: bodies.append(body) or {"ok": True},
    )
    mcpmod.update_status(
        goal="ship it", current_step="writing tests", active_branch="feat/x"
    )
    assert bodies[0] == {
        "session_id": "sess-1",
        "machine": "mini",
        "goal": "ship it",
        "current_step": "writing tests",
        "active_branch": "feat/x",
    }


def test_update_status_omits_absent_optional_fields(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-1")
    monkeypatch.setattr(mcpmod, "_machine", lambda: "mini")
    bodies = []
    monkeypatch.setattr(
        mcpmod,
        "_request",
        lambda method, path, body=None: bodies.append(body) or {"ok": True},
    )
    mcpmod.update_status()
    assert bodies[0] == {"session_id": "sess-1", "machine": "mini"}


def test_deregister_session_calls_delete_and_returns_message(monkeypatch):
    calls = []
    monkeypatch.setattr(
        mcpmod,
        "_request",
        lambda method, path, body=None: calls.append((method, path)),
    )
    out = mcpmod.deregister_session("sess-1")
    assert calls == [("DELETE", "/sessions/sess-1")]
    assert out == "deregistered sess-1"
