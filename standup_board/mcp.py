#!/usr/bin/env python3
"""MCP server for the standup board.

Exposes the presence board to Claude Code agents on demand, so an agent can ask
"who else is on this repo right now?" mid-session, or register/deregister a
session itself. Reads ``STANDUP_URL``/``STANDUP_TOKEN`` from the environment
or ``~/.config/standup/env`` — the same config the CLI client uses.

Run (stdio), once installed with the ``mcp`` extra
(``uv tool install 'standup-board[mcp]'`` / ``pipx install 'standup-board[mcp]'``
/ ``pip install 'standup-board[mcp]'``):
    standup-mcp

Enable in Claude Code (this is what `standup init` wires up automatically):
    claude mcp add standup -- standup-mcp

From a source checkout, without installing the console script:
    uv run --with mcp python -m standup_board.mcp
"""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

try:  # SDK after the FastMCP -> MCPServer rename
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # older SDK
    from mcp.server.fastmcp import FastMCP as _Server

CONFIG_PATH = Path.home() / ".config" / "standup" / "env"
TIMEOUT = 4.0

mcp = _Server("standup")


def _config() -> dict[str, str]:
    cfg: dict[str, str] = {}
    if CONFIG_PATH.is_file():
        for raw in CONFIG_PATH.read_text().splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                cfg[key.strip()] = val.strip().strip('"').strip("'")
    for key in ("STANDUP_URL", "STANDUP_TOKEN"):
        if os.environ.get(key):
            cfg[key] = os.environ[key]
    if not cfg.get("STANDUP_URL") or not cfg.get("STANDUP_TOKEN"):
        raise RuntimeError(
            "standup MCP not configured: set STANDUP_URL/STANDUP_TOKEN "
            "or write ~/.config/standup/env"
        )
    return cfg


def _machine() -> str:
    host = socket.gethostname()
    return host[:-6] if host.endswith(".local") else host


def _request(method: str, path: str, body: dict | None = None):
    cfg = _config()
    url = cfg["STANDUP_URL"].rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {cfg['STANDUP_TOKEN']}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # pragma: no cover
        raw = resp.read().decode()
        return json.loads(raw) if raw.strip() else None


@mcp.tool()
def list_sessions(repo: str | None = None, include_runners: bool = False) -> list[dict]:
    """List active agent sessions on the board, newest registrations included.

    Returns only agent sessions (``type == "agent"``) by default — these are the
    coordination peers to check before starting, rebasing, or merging work.
    Non-agent sessions (e.g. CI runners posting ``type="runner"``) are shown on
    the web board for visibility but are NOT coordination peers, so they are
    excluded here unless ``include_runners=True``.

    Pass ``repo`` to filter to one repository; omit it to see every active
    session across all repos.
    """
    path = "/sessions"
    if repo:
        path += "?repo=" + urllib.parse.quote(repo)
    result = _request("GET", path)
    sessions = (result or {}).get("sessions", [])
    if not include_runners:
        sessions = [s for s in sessions if (s.get("type") or "agent") == "agent"]
    return sessions


@mcp.tool()
def register_session(
    session_id: str,
    machine: str,
    repo: str,
    active_branch: str | None = None,
    last_prompt: str | None = None,
) -> dict:
    """Register or update (upsert) an agent session on the board."""
    body = {"session_id": session_id, "machine": machine, "repo": repo}
    if active_branch is not None:
        body["active_branch"] = active_branch
    if last_prompt is not None:
        body["last_prompt"] = last_prompt
    return _request("POST", "/sessions", body)


@mcp.tool()
def update_status(
    goal: str | None = None,
    current_step: str | None = None,
    active_branch: str | None = None,
) -> dict:
    """Post this session's standup narrative to the board.

    Use when work materially changes: set ``goal`` (the destination, kept stable
    across the session) and ``current_step`` (what you're doing now). Pass
    ``active_branch`` if you've switched branches. Identifies the session via
    ``CLAUDE_CODE_SESSION_ID``; the server preserves machine/repo and the
    auto-gathered worktree facts. Prefer the ``standup status`` CLI when
    you can run it from your worktree — it also detects the active branch + PR.
    """
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not session_id:
        raise RuntimeError(
            "CLAUDE_CODE_SESSION_ID is not set; cannot identify this session"
        )
    body: dict = {"session_id": session_id, "machine": _machine()}
    if goal is not None:
        body["goal"] = goal
    if current_step is not None:
        body["current_step"] = current_step
    if active_branch is not None:
        body["active_branch"] = active_branch
    return _request("POST", "/sessions", body)


@mcp.tool()
def deregister_session(session_id: str) -> str:
    """Remove an agent session from the board (idempotent)."""
    _request("DELETE", "/sessions/" + urllib.parse.quote(session_id))
    return f"deregistered {session_id}"


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
