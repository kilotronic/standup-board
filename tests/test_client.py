import argparse
import importlib.machinery
import importlib.util
import subprocess
import urllib.error
import urllib.parse
from pathlib import Path

_CLIENT = Path(__file__).resolve().parent.parent / "client" / "standup"
_loader = importlib.machinery.SourceFileLoader("standup_client", str(_CLIENT))
_spec = importlib.util.spec_from_loader("standup_client", _loader)
client = importlib.util.module_from_spec(_spec)
_loader.exec_module(client)

CFG = {"STANDUP_URL": "http://x", "STANDUP_TOKEN": "t"}


class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeCompleted:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


def _reg_args():
    return argparse.Namespace(
        session_id=None,
        cwd="/tmp",
        repo="pg",
        task=None,
        type=None,
        goal=None,
        step=None,
        machine=None,
    )


def _status_args(**kw):
    base = dict(session_id="s1", cwd="/tmp", repo="pg", goal=None, step=None)
    base.update(kw)
    return argparse.Namespace(**base)


def test_system_prompt_tags_detected():
    for tag in client.SYSTEM_PROMPT_TAGS:
        assert client._is_system_prompt(f"{tag}\nstuff") is True


def test_real_prompt_is_not_system():
    assert client._is_system_prompt("fix the timer bug") is False
    assert client._is_system_prompt("") is False


def test_register_posts_facts_on_system_wrapper(monkeypatch):
    # System wakeup must still POST facts (liveness + worktrees) but omit last_prompt.
    bodies = []
    monkeypatch.setattr(
        client,
        "_read_hook_stdin",
        lambda: {"session_id": "s1", "cwd": "/tmp", "prompt": "<task-notification>\nx"},
    )
    monkeypatch.setattr(client, "_worktrees", lambda cwd: [])
    monkeypatch.setattr(client, "_enrich_worktrees", lambda c, r, w: [])
    monkeypatch.setattr(
        client,
        "_request",
        lambda cfg, m, p, body=None: bodies.append((m, p, body)) or None,
    )
    assert client.cmd_register(CFG, _reg_args()) == 0
    posts = [b for (m, p, b) in bodies if m == "POST"]
    assert posts and "last_prompt" not in posts[0]
    assert posts[0]["session_id"] == "s1"
    assert "worktrees" not in posts[0]  # empty list omitted, not clobbering


def test_register_posts_last_prompt_on_real_prompt(monkeypatch):
    bodies = []
    monkeypatch.setattr(
        client,
        "_read_hook_stdin",
        lambda: {"session_id": "s1", "cwd": "/tmp", "prompt": "real task"},
    )
    monkeypatch.setattr(
        client, "_worktrees", lambda cwd: [{"path": "/tmp", "branch": "main"}]
    )
    monkeypatch.setattr(
        client,
        "_enrich_worktrees",
        lambda c, r, w: [{"path": "/tmp", "branch": "main", "pr": None}],
    )
    monkeypatch.setattr(
        client,
        "_request",
        lambda cfg, m, p, body=None: bodies.append((m, p, body)) or None,
    )
    assert client.cmd_register(CFG, _reg_args()) == 0
    post = next(b for (m, p, b) in bodies if m == "POST")
    assert post["last_prompt"] == "real task"
    assert "worktrees" in post
    assert "branch" not in post and "task" not in post  # old fields gone


def test_register_runner_posts_type_and_skips_enrichment(monkeypatch):
    posted = {}

    def fake_request(cfg, method, path, body=None):
        if method == "POST":
            posted.update(body)
            return None
        raise AssertionError(f"unexpected {method} {path}")  # no board GET

    monkeypatch.setattr(client, "_request", fake_request)
    args = argparse.Namespace(
        session_id="runner:j-mini-1",
        cwd="/tmp",
        repo="partygame",
        task=None,
        type="runner",
        goal="CI: partygame",
        step="CI · run 42",
        machine="j-mini",
    )
    rc = client.cmd_register(CFG, args)
    assert rc == 0
    assert posted["type"] == "runner"
    assert posted["session_id"] == "runner:j-mini-1"
    assert posted["repo"] == "partygame"
    assert posted["machine"] == "j-mini"
    assert posted["goal"] == "CI: partygame"
    assert posted["current_step"] == "CI · run 42"
    assert "worktrees" not in posted  # enrichment skipped for non-agent


def test_status_posts_narrative(monkeypatch):
    bodies = []
    monkeypatch.setattr(
        client,
        "_active_context",
        lambda cwd: {"worktree_path": "/tmp", "active_branch": "feat/x"},
    )
    monkeypatch.setattr(
        client,
        "_pr_for_branch",
        lambda c, r, b: {"number": 9, "url": "http://h/9", "state": "OPEN"},
    )
    monkeypatch.setattr(
        client,
        "_request",
        lambda cfg, m, p, body=None: bodies.append((m, p, body)) or None,
    )
    assert (
        client.cmd_status(CFG, _status_args(goal="ship timer", step="writing tests"))
        == 0
    )
    post = next(b for (m, p, b) in bodies if m == "POST")
    assert post["goal"] == "ship timer"
    assert post["current_step"] == "writing tests"
    assert post["active_branch"] == "feat/x"
    assert post["active_pr"]["number"] == 9
    assert "worktrees" not in post and "last_prompt" not in post


def test_status_posts_with_agent_fallback(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CURSOR_CONVERSATION_ID", raising=False)
    monkeypatch.delenv("OPENCODE_PID", raising=False)
    monkeypatch.setattr(client, "_fallback_session_id", lambda: "anon:j-air:42")
    bodies = []
    monkeypatch.setattr(
        client,
        "_active_context",
        lambda cwd: {"worktree_path": "/tmp", "active_branch": None},
    )
    monkeypatch.setattr(
        client,
        "_request",
        lambda cfg, m, p, body=None: bodies.append((m, p, body)) or None,
    )
    assert client.cmd_status(CFG, _status_args(session_id=None, goal="g")) == 0
    assert bodies[0][2]["session_id"] == "anon:j-air:42"
    assert bodies[0][2]["goal"] == "g"


def test_status_session_id_from_env(monkeypatch):
    monkeypatch.delenv("CURSOR_CONVERSATION_ID", raising=False)
    bodies = []
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "env-sess")
    monkeypatch.setattr(
        client,
        "_active_context",
        lambda cwd: {"worktree_path": "/tmp", "active_branch": None},
    )
    monkeypatch.setattr(
        client, "_request", lambda cfg, m, p, body=None: bodies.append(body) or None
    )
    assert client.cmd_status(CFG, _status_args(session_id=None, goal="g")) == 0
    assert bodies[0]["session_id"] == "env-sess"
    assert "active_branch" not in bodies[0]  # None branch omitted


def _boom(*a, **k):
    raise AssertionError("gh should not be called")


def test_pr_skipped_on_main_or_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(client, "PR_CACHE_DIR", tmp_path)
    monkeypatch.setattr(client, "_gh_pr", _boom)
    assert client._pr_for_branch("/tmp", "partygame", "main") is None
    assert client._pr_for_branch("/tmp", "partygame", None) is None


def test_pr_caches_gh_result(monkeypatch, tmp_path):
    monkeypatch.setattr(client, "PR_CACHE_DIR", tmp_path)
    calls = []
    pr = {"number": 5, "title": "t", "state": "OPEN", "url": "u"}

    def fake_gh(cwd, branch):
        calls.append(branch)
        return pr

    monkeypatch.setattr(client, "_gh_pr", fake_gh)
    assert client._pr_for_branch("/tmp", "partygame", "feat/x") == pr
    assert client._pr_for_branch("/tmp", "partygame", "feat/x") == pr  # cache hit
    assert calls == ["feat/x"]  # gh ran once


def test_pr_none_on_gh_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(client, "PR_CACHE_DIR", tmp_path)
    monkeypatch.setattr(client, "_gh_pr", lambda cwd, branch: None)
    assert client._pr_for_branch("/tmp", "partygame", "feat") is None


def test_describe_uses_new_fields():
    s = {
        "machine": "j-air",
        "active_branch": "feat",
        "active_pr": {"number": 9, "state": "OPEN"},
        "goal": "ship it",
    }
    out = client._describe(s)
    assert "#9 OPEN" in out and "feat" in out and "ship it" in out


# --- subagent-start / subagent-stop ---


def _subagent_start_args(**kw):
    base = dict(session_id=None, agent_id=None, label=None)
    base.update(kw)
    return argparse.Namespace(**base)


def _subagent_stop_args(**kw):
    base = dict(session_id=None, agent_id=None)
    base.update(kw)
    return argparse.Namespace(**base)


def test_subagent_start_posts_agent_id_and_agent_type_label(monkeypatch):
    bodies = []
    monkeypatch.setattr(
        client,
        "_read_hook_stdin",
        lambda: {"session_id": "s1", "agent_id": "a1", "agent_type": "Explore"},
    )
    monkeypatch.setattr(
        client,
        "_request",
        lambda cfg, m, p, body=None: bodies.append((m, p, body)) or None,
    )
    assert client.cmd_subagent_start(CFG, _subagent_start_args()) == 0
    method, path, body = bodies[0]
    assert method == "POST"
    assert path == "/sessions/s1/subagents"
    assert body == {"agent_id": "a1", "label": "Explore"}


def test_subagent_start_prefers_description_over_agent_type(monkeypatch):
    bodies = []
    monkeypatch.setattr(
        client,
        "_read_hook_stdin",
        lambda: {
            "session_id": "s1",
            "agent_id": "a1",
            "agent_type": "Explore",
            "description": "find the timer bug",
        },
    )
    monkeypatch.setattr(
        client,
        "_request",
        lambda cfg, m, p, body=None: bodies.append((m, p, body)) or None,
    )
    client.cmd_subagent_start(CFG, _subagent_start_args())
    assert bodies[0][2]["label"] == "find the timer bug"


def test_subagent_start_falls_back_to_literal_label(monkeypatch):
    bodies = []
    monkeypatch.setattr(
        client, "_read_hook_stdin", lambda: {"session_id": "s1", "agent_id": "a1"}
    )
    monkeypatch.setattr(
        client,
        "_request",
        lambda cfg, m, p, body=None: bodies.append((m, p, body)) or None,
    )
    client.cmd_subagent_start(CFG, _subagent_start_args())
    assert bodies[0][2]["label"] == "subagent"


def test_subagent_start_explicit_flags_override_hook_json(monkeypatch):
    bodies = []
    monkeypatch.setattr(
        client,
        "_read_hook_stdin",
        lambda: {"session_id": "s1", "agent_id": "hook-id", "agent_type": "Explore"},
    )
    monkeypatch.setattr(
        client,
        "_request",
        lambda cfg, m, p, body=None: bodies.append((m, p, body)) or None,
    )
    client.cmd_subagent_start(
        CFG, _subagent_start_args(agent_id="flag-id", label="custom")
    )
    assert bodies[0][1] == "/sessions/s1/subagents"
    assert bodies[0][2] == {"agent_id": "flag-id", "label": "custom"}


def test_subagent_start_noop_without_agent_id(monkeypatch):
    called = []
    monkeypatch.setattr(client, "_read_hook_stdin", lambda: {"session_id": "s1"})
    monkeypatch.setattr(client, "_request", lambda *a, **k: called.append(a))
    assert client.cmd_subagent_start(CFG, _subagent_start_args()) == 0
    assert called == []


def test_subagent_start_noop_without_session_id(monkeypatch):
    called = []
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CURSOR_CONVERSATION_ID", raising=False)
    monkeypatch.setattr(client, "_read_hook_stdin", lambda: {"agent_id": "a1"})
    monkeypatch.setattr(client, "_request", lambda *a, **k: called.append(a))
    assert client.cmd_subagent_start(CFG, _subagent_start_args()) == 0
    assert called == []


def test_subagent_start_session_id_falls_back_to_env(monkeypatch):
    bodies = []
    monkeypatch.delenv("CURSOR_CONVERSATION_ID", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "env-sess")
    monkeypatch.setattr(client, "_read_hook_stdin", lambda: {"agent_id": "a1"})
    monkeypatch.setattr(
        client,
        "_request",
        lambda cfg, m, p, body=None: bodies.append((m, p, body)) or None,
    )
    client.cmd_subagent_start(CFG, _subagent_start_args())
    assert bodies[0][1] == "/sessions/env-sess/subagents"


def test_subagent_start_swallows_request_failure(monkeypatch):
    monkeypatch.setattr(
        client, "_read_hook_stdin", lambda: {"session_id": "s1", "agent_id": "a1"}
    )

    def boom(*a, **k):
        raise OSError("board down")

    monkeypatch.setattr(client, "_request", boom)
    assert client.cmd_subagent_start(CFG, _subagent_start_args()) == 0


def test_subagent_stop_deletes_by_agent_id(monkeypatch):
    calls = []
    monkeypatch.setattr(
        client, "_read_hook_stdin", lambda: {"session_id": "s1", "agent_id": "a1"}
    )
    monkeypatch.setattr(
        client,
        "_request",
        lambda cfg, m, p, body=None: calls.append((m, p)) or None,
    )
    assert client.cmd_subagent_stop(CFG, _subagent_stop_args()) == 0
    assert calls == [("DELETE", "/sessions/s1/subagents/a1")]


def test_subagent_stop_noop_without_agent_id(monkeypatch):
    called = []
    monkeypatch.setattr(client, "_read_hook_stdin", lambda: {"session_id": "s1"})
    monkeypatch.setattr(client, "_request", lambda *a, **k: called.append(a))
    assert client.cmd_subagent_stop(CFG, _subagent_stop_args()) == 0
    assert called == []


def test_subagent_stop_swallows_request_failure(monkeypatch):
    monkeypatch.setattr(
        client, "_read_hook_stdin", lambda: {"session_id": "s1", "agent_id": "a1"}
    )

    def boom(*a, **k):
        raise OSError("board down")

    monkeypatch.setattr(client, "_request", boom)
    assert client.cmd_subagent_stop(CFG, _subagent_stop_args()) == 0


def test_describe_shows_the_current_step_so_claims_are_visible():
    s = {"machine": "m", "goal": "fix bugs", "current_step": "CLAIMED: todo #3"}
    out = client._describe(s)
    assert "fix bugs" in out and "CLAIMED: todo #3" in out


def test_describe_omits_the_step_when_none_is_posted():
    assert "step" not in client._describe({"machine": "m", "goal": "g"})


def test_describe_includes_subagent_count():
    session = {
        "machine": "mini",
        "subagents": [
            {"agent_id": "a1", "label": "x"},
            {"agent_id": "a2", "label": "y"},
        ],
    }
    assert "2 subagents" in client._describe(session)


def test_describe_omits_subagent_suffix_when_empty():
    session = {"machine": "mini", "subagents": []}
    assert "subagents" not in client._describe(session)


PORCELAIN = """worktree /home/u/repo
HEAD aaaa
branch refs/heads/main

worktree /home/u/repo-feat
HEAD bbbb
branch refs/heads/feat/timer

worktree /home/u/repo-detached
HEAD cccc
detached

worktree /home/u/repo.git
bare
"""


def test_parse_worktrees_extracts_path_and_branch():
    out = client._parse_worktrees(PORCELAIN)
    assert {"path": "/home/u/repo", "branch": "main"} in out
    assert {"path": "/home/u/repo-feat", "branch": "feat/timer"} in out


def test_parse_worktrees_detached_branch_is_none():
    out = client._parse_worktrees(PORCELAIN)
    assert {"path": "/home/u/repo-detached", "branch": None} in out


def test_parse_worktrees_skips_bare():
    out = client._parse_worktrees(PORCELAIN)
    assert all(w["path"] != "/home/u/repo.git" for w in out)


def test_parse_worktrees_empty():
    assert client._parse_worktrees("") == []


def test_active_context_detached_is_none(monkeypatch):
    monkeypatch.setattr(
        client,
        "_git",
        lambda cwd, *a: (
            "HEAD" if a[0] == "rev-parse" and a[1] == "--abbrev-ref" else "/top"
        ),
    )
    ctx = client._active_context("/x")
    assert ctx["active_branch"] is None
    assert ctx["worktree_path"] == "/top"


def test_enrich_worktrees_attaches_pr(monkeypatch, tmp_path):
    monkeypatch.setattr(client, "PR_CACHE_DIR", tmp_path)
    monkeypatch.setattr(
        client,
        "_gh_pr",
        lambda cwd, branch: {
            "number": 3,
            "title": "t",
            "state": "OPEN",
            "url": "http://h/3",
        },
    )
    out = client._enrich_worktrees("/x", "repo", [{"path": "/x", "branch": "feat"}])
    assert out[0]["pr"]["number"] == 3
    assert out[0]["path"] == "/x"


# --- _load_config ---


def test_load_config_skips_blank_comment_and_malformed_lines(monkeypatch, tmp_path):
    cfg_file = tmp_path / "env"
    cfg_file.write_text(
        "\n# a comment\nmalformed-no-equals\nSTANDUP_URL=http://file\nSTANDUP_TOKEN=filetok\n"
    )
    monkeypatch.setattr(client, "CONFIG_PATH", cfg_file)
    monkeypatch.delenv("STANDUP_URL", raising=False)
    monkeypatch.delenv("STANDUP_TOKEN", raising=False)
    cfg = client._load_config()
    assert cfg == {"STANDUP_URL": "http://file", "STANDUP_TOKEN": "filetok"}


def test_load_config_env_overrides_file(monkeypatch, tmp_path):
    cfg_file = tmp_path / "env"
    cfg_file.write_text("STANDUP_URL=http://file\nSTANDUP_TOKEN=filetok\n")
    monkeypatch.setattr(client, "CONFIG_PATH", cfg_file)
    monkeypatch.setenv("STANDUP_URL", "http://env")
    monkeypatch.setenv("STANDUP_TOKEN", "envtok")
    cfg = client._load_config()
    assert cfg == {"STANDUP_URL": "http://env", "STANDUP_TOKEN": "envtok"}


# --- _read_hook_stdin ---


class _FakeStdin:
    def __init__(self, text=None, is_tty=False, raise_on_read=None):
        self._text = text
        self._is_tty = is_tty
        self._raise = raise_on_read

    def isatty(self):
        return self._is_tty

    def read(self):
        if self._raise:
            raise self._raise
        return self._text


def test_read_hook_stdin_empty_when_tty(monkeypatch):
    monkeypatch.setattr(client.sys, "stdin", _FakeStdin(is_tty=True))
    assert client._read_hook_stdin() == {}


def test_read_hook_stdin_empty_when_none(monkeypatch):
    monkeypatch.setattr(client.sys, "stdin", None)
    assert client._read_hook_stdin() == {}


def test_read_hook_stdin_empty_on_read_error(monkeypatch):
    monkeypatch.setattr(client.sys, "stdin", _FakeStdin(raise_on_read=OSError("boom")))
    assert client._read_hook_stdin() == {}


def test_read_hook_stdin_empty_on_blank(monkeypatch):
    monkeypatch.setattr(client.sys, "stdin", _FakeStdin(text="   \n  "))
    assert client._read_hook_stdin() == {}


def test_read_hook_stdin_empty_on_invalid_json(monkeypatch):
    monkeypatch.setattr(client.sys, "stdin", _FakeStdin(text="not-json"))
    assert client._read_hook_stdin() == {}


def test_read_hook_stdin_empty_when_json_not_dict(monkeypatch):
    monkeypatch.setattr(client.sys, "stdin", _FakeStdin(text="[1, 2]"))
    assert client._read_hook_stdin() == {}


def test_read_hook_stdin_returns_parsed_dict(monkeypatch):
    monkeypatch.setattr(
        client.sys, "stdin", _FakeStdin(text='{"session_id": "s1", "cwd": "/x"}')
    )
    assert client._read_hook_stdin() == {"session_id": "s1", "cwd": "/x"}


# --- _git ---


def test_git_returns_stripped_stdout_on_success(monkeypatch):
    monkeypatch.setattr(
        client.subprocess,
        "run",
        lambda *a, **k: _FakeCompleted(returncode=0, stdout=" abc123 \n"),
    )
    assert client._git("/repo", "rev-parse", "HEAD") == "abc123"


def test_git_returns_none_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr(
        client.subprocess,
        "run",
        lambda *a, **k: _FakeCompleted(returncode=1, stdout=""),
    )
    assert client._git("/repo", "status") is None


def test_git_returns_none_on_exception(monkeypatch):
    def boom(*a, **k):
        raise OSError("no git")

    monkeypatch.setattr(client.subprocess, "run", boom)
    assert client._git("/repo", "status") is None


def test_git_returns_none_on_empty_stdout(monkeypatch):
    monkeypatch.setattr(
        client.subprocess,
        "run",
        lambda *a, **k: _FakeCompleted(returncode=0, stdout="  "),
    )
    assert client._git("/repo", "status") is None


# --- _repo_name ---


def test_repo_name_uses_origin_remote_basename(monkeypatch):
    monkeypatch.setattr(
        client,
        "_git",
        lambda cwd, *a: "git@github.com:acme/widgets.git" if a[0] == "remote" else None,
    )
    assert client._repo_name("/x") == "widgets"


def test_repo_name_falls_back_to_toplevel_basename(monkeypatch):
    monkeypatch.setattr(
        client,
        "_git",
        lambda cwd, *a: None if a[0] == "remote" else "/home/u/myrepo",
    )
    assert client._repo_name("/x") == "myrepo"


# --- Cursor / Claude session + cwd detection ---


def test_session_id_prefers_explicit_then_hook_then_env(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "claude-env")
    monkeypatch.setenv("CURSOR_CONVERSATION_ID", "cursor-env")
    assert (
        client._session_id(
            {"session_id": "hook-sid", "conversation_id": "hook-cid"},
            explicit="flag",
        )
        == "flag"
    )
    assert (
        client._session_id(
            {"session_id": "hook-sid", "conversation_id": "hook-cid"},
            explicit=None,
        )
        == "hook-sid"
    )
    assert (
        client._session_id({"conversation_id": "hook-cid"}, explicit=None) == "hook-cid"
    )
    assert client._session_id({}, explicit=None) == "claude-env"
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    assert client._session_id({}, explicit=None) == "cursor-env"


def test_terminal_session_id_uses_host_and_sid(monkeypatch):
    monkeypatch.setattr(client, "_machine", lambda: "j-air")
    monkeypatch.setattr(client.os, "getsid", lambda pid: 1234)
    assert client._terminal_session_id() == "anon:j-air:1234"


def test_terminal_session_id_none_without_getsid(monkeypatch):
    monkeypatch.delattr(client.os, "getsid", raising=False)
    assert client._terminal_session_id() is None


def test_fallback_session_id_prefers_opencode_pid(monkeypatch):
    monkeypatch.setattr(client, "_machine", lambda: "j-air")
    monkeypatch.setattr(client, "_agent_process_pid", lambda: 4242)
    monkeypatch.setenv("OPENCODE_PID", "21547")
    assert client._fallback_session_id() == "anon:j-air:21547"


def test_fallback_session_id_uses_tree_walk_without_env(monkeypatch):
    monkeypatch.delenv("OPENCODE_PID", raising=False)
    monkeypatch.setattr(client, "_machine", lambda: "j-air")
    monkeypatch.setattr(client, "_agent_process_pid", lambda: 4242)
    assert client._fallback_session_id() == "anon:j-air:4242"


def test_fallback_session_id_uses_terminal_session_last(monkeypatch):
    monkeypatch.delenv("OPENCODE_PID", raising=False)
    monkeypatch.setattr(client, "_agent_process_pid", lambda: None)
    monkeypatch.setattr(client, "_terminal_session_id", lambda: "anon:j-air:999")
    assert client._fallback_session_id() == "anon:j-air:999"


def test_ps_info_parses_basename_and_strips_dash(monkeypatch):
    def fake_ps(*a, **k):
        return _FakeCompleted(returncode=0, stdout="  21495 -zsh\n")

    monkeypatch.setattr(client.subprocess, "run", fake_ps)
    assert client._ps_info(52919) == (21495, "zsh")


def test_ps_info_none_on_failure(monkeypatch):
    monkeypatch.setattr(
        client.subprocess, "run", lambda *a, **k: _FakeCompleted(returncode=1)
    )
    assert client._ps_info(1) is None


def test_agent_process_pid_finds_nearest_non_shell(monkeypatch):
    monkeypatch.setattr(client.os, "getppid", lambda: 54154)
    chain = {54154: (21547, "zsh"), 21547: (21495, "opencode")}

    def fake_ps_info(pid):
        return chain.get(pid)

    monkeypatch.setattr(client, "_ps_info", fake_ps_info)
    assert client._agent_process_pid() == 21547


def test_agent_process_pid_none_when_bottoms_out_in_login(monkeypatch):
    monkeypatch.setattr(client.os, "getppid", lambda: 54154)
    chain = {
        54154: (91953, "zsh"),
        91953: (91952, "login"),
        91952: (1182, "Terminal"),
        1182: (1, "launchd"),
    }

    def fake_ps_info(pid):
        return chain.get(pid)

    monkeypatch.setattr(client, "_ps_info", fake_ps_info)
    assert client._agent_process_pid() is None


def test_agent_process_pid_none_without_getppid(monkeypatch):
    monkeypatch.delattr(client.os, "getppid", raising=False)
    assert client._agent_process_pid() is None


def test_resolved_session_id_prefers_real_then_falls_back(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CURSOR_CONVERSATION_ID", raising=False)
    monkeypatch.setattr(client, "_fallback_session_id", lambda: "anon:j-air:42")
    assert client._resolved_session_id({"session_id": "real"}) == "real"
    assert client._resolved_session_id({}) == "anon:j-air:42"


def test_hook_cwd_prefers_explicit_cwd_workspace_roots_then_env(monkeypatch):
    monkeypatch.setenv("CURSOR_PROJECT_DIR", "/cursor/project")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/claude/project")
    monkeypatch.setattr(client.os, "getcwd", lambda: "/fallback")
    assert (
        client._hook_cwd(
            {"cwd": "/hook/cwd", "workspace_roots": ["/ws"]},
            explicit="/flag",
        )
        == "/flag"
    )
    assert (
        client._hook_cwd(
            {"cwd": "/hook/cwd", "workspace_roots": ["/ws"]},
            explicit=None,
        )
        == "/hook/cwd"
    )
    assert (
        client._hook_cwd({"workspace_roots": ["", "/ws/root"]}, explicit=None)
        == "/ws/root"
    )
    assert client._hook_cwd({}, explicit=None) == "/cursor/project"
    monkeypatch.delenv("CURSOR_PROJECT_DIR", raising=False)
    assert client._hook_cwd({}, explicit=None) == "/claude/project"
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    assert client._hook_cwd({}, explicit=None) == "/fallback"


def test_register_uses_workspace_roots_when_cwd_missing(monkeypatch):
    """Cursor third-party Claude hooks omit cwd; workspace_roots must win over getcwd."""
    posts = []
    monkeypatch.setattr(
        client,
        "_read_hook_stdin",
        lambda: {
            "session_id": "cursor-sess",
            "workspace_roots": ["/Users/u/Code/partygame"],
            "prompt": "ship the timer",
        },
    )
    monkeypatch.setattr(client.os, "getcwd", lambda: "/Users/u/.claude")
    monkeypatch.setattr(client, "_repo_name", lambda cwd: Path(cwd).name)
    monkeypatch.setattr(client, "_worktrees", lambda cwd: [])
    monkeypatch.setattr(client, "_enrich_worktrees", lambda c, r, w: [])
    monkeypatch.setattr(
        client, "_request", lambda cfg, m, p, body=None: posts.append(body) or None
    )
    args = argparse.Namespace(
        session_id=None,
        cwd=None,
        repo=None,
        task=None,
        type=None,
        goal=None,
        step=None,
        machine=None,
    )
    assert client.cmd_register(CFG, args) == 0
    assert posts[0]["repo"] == "partygame"
    assert posts[0]["session_id"] == "cursor-sess"
    assert posts[0]["last_prompt"] == "ship the timer"


def test_status_session_id_from_cursor_env(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setenv("CURSOR_CONVERSATION_ID", "cursor-sess")
    bodies = []
    monkeypatch.setattr(
        client,
        "_active_context",
        lambda cwd: {"worktree_path": "/tmp", "active_branch": None},
    )
    monkeypatch.setattr(
        client, "_request", lambda cfg, m, p, body=None: bodies.append(body) or None
    )
    assert client.cmd_status(CFG, _status_args(session_id=None, goal="g")) == 0
    assert bodies[0]["session_id"] == "cursor-sess"


# --- _gh_pr ---


def test_gh_pr_returns_none_on_subprocess_exception(monkeypatch):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="gh", timeout=3)

    monkeypatch.setattr(client.subprocess, "run", boom)
    assert client._gh_pr("/x", "feat") is None


def test_gh_pr_returns_none_on_nonzero_returncode(monkeypatch):
    monkeypatch.setattr(
        client.subprocess,
        "run",
        lambda *a, **k: _FakeCompleted(returncode=1, stdout=""),
    )
    assert client._gh_pr("/x", "feat") is None


def test_gh_pr_returns_none_on_invalid_json(monkeypatch):
    monkeypatch.setattr(
        client.subprocess,
        "run",
        lambda *a, **k: _FakeCompleted(returncode=0, stdout="not-json"),
    )
    assert client._gh_pr("/x", "feat") is None


def test_gh_pr_returns_none_when_data_missing_number(monkeypatch):
    monkeypatch.setattr(
        client.subprocess,
        "run",
        lambda *a, **k: _FakeCompleted(returncode=0, stdout='{"title": "x"}'),
    )
    assert client._gh_pr("/x", "feat") is None


def test_gh_pr_returns_parsed_dict_on_success(monkeypatch):
    body = '{"number": 9, "title": "t", "state": "OPEN", "url": "http://h/9"}'
    monkeypatch.setattr(
        client.subprocess,
        "run",
        lambda *a, **k: _FakeCompleted(returncode=0, stdout=body),
    )
    pr = client._gh_pr("/x", "feat")
    assert pr == {"number": 9, "title": "t", "state": "OPEN", "url": "http://h/9"}


# --- _pr_for_branch cache error paths ---


def test_pr_for_branch_cache_read_error_falls_through_to_gh(monkeypatch, tmp_path):
    monkeypatch.setattr(client, "PR_CACHE_DIR", tmp_path)
    cache = tmp_path / "pr-repo-feat.json"
    cache.write_text("not-json")  # is_file() True, but json.loads raises ValueError
    calls = []

    def fake_gh(cwd, branch):
        calls.append(branch)
        return {"number": 1, "title": "t", "state": "OPEN", "url": "u"}

    monkeypatch.setattr(client, "_gh_pr", fake_gh)
    pr = client._pr_for_branch("/x", "repo", "feat")
    assert pr["number"] == 1
    assert calls == ["feat"]


def test_pr_for_branch_cache_write_error_is_swallowed(monkeypatch, tmp_path):
    missing_parent = tmp_path / "nope" / "deeper"
    monkeypatch.setattr(client, "PR_CACHE_DIR", missing_parent)

    def bad_mkdir(*a, **k):
        raise OSError("no perm")

    monkeypatch.setattr(client.Path, "mkdir", bad_mkdir)
    monkeypatch.setattr(client, "_gh_pr", lambda cwd, branch: {"number": 1})
    pr = client._pr_for_branch("/x", "repo", "feat")
    assert pr == {"number": 1}  # write failure doesn't blow up the call


# --- _worktrees ---


def test_worktrees_empty_on_git_failure(monkeypatch):
    monkeypatch.setattr(client, "_git", lambda cwd, *a: None)
    assert client._worktrees("/x") == []


def test_worktrees_parses_git_porcelain_output(monkeypatch):
    monkeypatch.setattr(
        client,
        "_git",
        lambda cwd, *a: "worktree /home/u/repo\nHEAD aaaa\nbranch refs/heads/main\n",
    )
    out = client._worktrees("/x")
    assert out == [{"path": "/home/u/repo", "branch": "main"}]


# --- client _request / _http_json ---


def test_client_request_get_no_body(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["req"] = req
        return _FakeResp(b'{"sessions": []}')

    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)
    result = client._request(CFG, "GET", "/sessions")
    assert result == {"sessions": []}
    assert seen["req"].data is None
    assert seen["req"].get_header("Authorization") == "Bearer t"


def test_client_request_post_sets_content_type(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["req"] = req
        return _FakeResp(b"")

    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)
    result = client._request(CFG, "POST", "/sessions", {"a": 1})
    assert result is None
    assert seen["req"].get_header("Content-type") == "application/json"


def test_http_json_get(monkeypatch):
    def fake_urlopen(req, timeout=None):
        assert req.get_method() == "GET"
        return _FakeResp(b'{"ok": true}')

    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)
    assert client._http_json("http://x/config") == {"ok": True}


def test_http_json_post_form_encoded_by_default(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["req"] = req
        return _FakeResp(b"{}")

    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)
    client._http_json("http://x/device", data={"client_id": "cid"})
    assert seen["req"].get_method() == "POST"
    assert seen["req"].get_header("Content-type") is None
    assert seen["req"].data == b"client_id=cid"


def test_http_json_post_json_body_when_flagged(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["req"] = req
        return _FakeResp(b"{}")

    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)
    client._http_json("http://x/exchange", data={"github_token": "t"}, json_body=True)
    assert seen["req"].get_header("Content-type") == "application/json"
    assert seen["req"].data == b'{"github_token": "t"}'


# --- _last_prompt blank-line edge case ---


def test_last_prompt_none_when_only_blank_lines():
    assert client._last_prompt({"prompt": "   \n\t\n  "}) is None


# --- _open_browser ---


def test_open_browser_never_raises_on_subprocess_failure(monkeypatch):
    def boom(*a, **k):
        raise OSError("no xdg-open")

    monkeypatch.setattr(client.subprocess, "run", boom)
    client._open_browser("http://x")  # must not raise


# --- _build_parser ---


def test_build_parser_defines_all_subcommands():
    parser = client._build_parser()
    args = parser.parse_args(["register", "--repo", "r"])
    assert args.command == "register" and args.repo == "r"
    args = parser.parse_args(["status", "--goal", "g", "--step", "s"])
    assert args.goal == "g" and args.step == "s"
    args = parser.parse_args(["list", "--all"])
    assert args.all is True
    args = parser.parse_args(["login", "--url", "http://x"])
    assert args.url == "http://x"
    args = parser.parse_args(["init", "--shared"])
    assert args.shared is True


# --- cmd_register edge cases ---


def test_cmd_register_uses_agent_fallback(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CURSOR_CONVERSATION_ID", raising=False)
    monkeypatch.delenv("OPENCODE_PID", raising=False)
    monkeypatch.setattr(client, "_read_hook_stdin", lambda: {})
    monkeypatch.setattr(client, "_fallback_session_id", lambda: "anon:j-air:42")
    monkeypatch.setattr(client, "_worktrees", lambda cwd: [])
    monkeypatch.setattr(client, "_enrich_worktrees", lambda c, r, w: [])
    bodies = []
    monkeypatch.setattr(
        client, "_request", lambda cfg, m, p, body=None: bodies.append(body) or None
    )
    assert client.cmd_register(CFG, _reg_args()) == 0
    assert bodies[0]["session_id"] == "anon:j-air:42"
    assert bodies[0]["repo"] == "pg"


def test_cmd_register_swallows_post_request_error(monkeypatch):
    import urllib.error

    monkeypatch.setattr(client, "_read_hook_stdin", lambda: {"session_id": "s1"})
    monkeypatch.setattr(client, "_worktrees", lambda cwd: [])
    monkeypatch.setattr(client, "_enrich_worktrees", lambda c, r, w: [])

    def boom(*a, **k):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(client, "_request", boom)
    assert client.cmd_register(CFG, _reg_args()) == 0


def test_cmd_register_prints_other_active_sessions(monkeypatch, capsys):
    monkeypatch.setattr(client, "_read_hook_stdin", lambda: {"session_id": "s1"})
    monkeypatch.setattr(client, "_worktrees", lambda cwd: [])
    monkeypatch.setattr(client, "_enrich_worktrees", lambda c, r, w: [])

    def fake_request(cfg, method, path, body=None):
        if method == "GET":
            return {
                "sessions": [
                    {"session_id": "other", "machine": "m2", "goal": "ship it"},
                    {"session_id": "s1", "machine": "m1"},
                ]
            }
        return None

    monkeypatch.setattr(client, "_request", fake_request)
    assert client.cmd_register(CFG, _reg_args()) == 0
    out = capsys.readouterr().out
    assert "1 other active agent session" in out
    assert "ship it" in out


def test_coworker_warning_ignores_runner_rows(monkeypatch, capsys):
    monkeypatch.setattr(client, "_read_hook_stdin", lambda: {"session_id": "s1"})
    monkeypatch.setattr(client, "_worktrees", lambda cwd: [])
    monkeypatch.setattr(client, "_enrich_worktrees", lambda c, r, w: [])

    def fake_request(cfg, method, path, body=None):
        if method == "GET":
            return {
                "sessions": [
                    {
                        "session_id": "other-agent",
                        "type": "agent",
                        "machine": "air",
                        "repo": "pg",
                        "goal": "reviewing",
                    },
                    {
                        "session_id": "runner:snoopy-1",
                        "type": "runner",
                        "machine": "snoopy",
                        "repo": "pg",
                        "goal": "CI: pg",
                    },
                ]
            }
        return None

    monkeypatch.setattr(client, "_request", fake_request)
    assert client.cmd_register(CFG, _reg_args()) == 0
    out = capsys.readouterr().out
    assert "1 other active agent session(s)" in out  # the agent, not the runner
    assert "snoopy" not in out


def test_cmd_register_swallows_list_request_error(monkeypatch):
    import urllib.error

    monkeypatch.setattr(client, "_read_hook_stdin", lambda: {"session_id": "s1"})
    monkeypatch.setattr(client, "_worktrees", lambda cwd: [])
    monkeypatch.setattr(client, "_enrich_worktrees", lambda c, r, w: [])

    def fake_request(cfg, method, path, body=None):
        if method == "GET":
            raise urllib.error.URLError("down")
        return None

    monkeypatch.setattr(client, "_request", fake_request)
    assert client.cmd_register(CFG, _reg_args()) == 0


# --- cmd_status error path ---


def test_cmd_status_swallows_request_error(monkeypatch):
    import urllib.error

    monkeypatch.setattr(
        client,
        "_active_context",
        lambda cwd: {"worktree_path": "/x", "active_branch": None},
    )

    def boom(*a, **k):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(client, "_request", boom)
    assert client.cmd_status(CFG, _status_args()) == 0


# --- cmd_deregister ---


def test_cmd_deregister_uses_agent_fallback(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CURSOR_CONVERSATION_ID", raising=False)
    monkeypatch.delenv("OPENCODE_PID", raising=False)
    monkeypatch.setattr(client, "_read_hook_stdin", lambda: {})
    monkeypatch.setattr(client, "_fallback_session_id", lambda: "anon:j-air:42")
    calls = []
    monkeypatch.setattr(
        client, "_request", lambda cfg, m, p, body=None: calls.append((m, p))
    )
    args = argparse.Namespace(session_id=None)
    assert client.cmd_deregister(CFG, args) == 0
    assert calls == [("DELETE", "/sessions/anon%3Aj-air%3A42")]


def test_cmd_deregister_calls_delete(monkeypatch):
    calls = []
    monkeypatch.setattr(client, "_read_hook_stdin", lambda: {})
    monkeypatch.setattr(
        client, "_request", lambda cfg, m, p, body=None: calls.append((m, p))
    )
    args = argparse.Namespace(session_id="s1")
    assert client.cmd_deregister(CFG, args) == 0
    assert calls == [("DELETE", "/sessions/s1")]


def test_cmd_deregister_swallows_request_error(monkeypatch):
    import urllib.error

    def boom(*a, **k):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(client, "_read_hook_stdin", lambda: {"session_id": "s1"})
    monkeypatch.setattr(client, "_request", boom)
    args = argparse.Namespace(session_id=None)
    assert client.cmd_deregister(CFG, args) == 0


# --- cmd_list ---


def test_cmd_list_defaults_to_current_repo(monkeypatch):
    seen = {}

    def fake_request(cfg, m, p, body=None):
        seen["path"] = p
        return {"sessions": []}

    monkeypatch.setattr(client, "_repo_name", lambda cwd: "pg")
    monkeypatch.setattr(client, "_request", fake_request)
    args = argparse.Namespace(repo=None, all=False)
    assert client.cmd_list(CFG, args) == 0
    assert seen["path"] == "/sessions?repo=pg"


def test_cmd_list_all_omits_repo_filter(monkeypatch):
    seen = {}

    def fake_request(cfg, m, p, body=None):
        seen["path"] = p
        return {"sessions": []}

    monkeypatch.setattr(client, "_request", fake_request)
    args = argparse.Namespace(repo=None, all=True)
    assert client.cmd_list(CFG, args) == 0
    assert seen["path"] == "/sessions"


def test_cmd_list_prints_board_unavailable_on_error(monkeypatch, capsys):
    import urllib.error

    def boom(*a, **k):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(client, "_request", boom)
    args = argparse.Namespace(repo="pg", all=False)
    assert client.cmd_list(CFG, args) == 0
    assert "board unavailable" in capsys.readouterr().err


def test_cmd_list_prints_no_sessions_message(monkeypatch, capsys):
    monkeypatch.setattr(
        client, "_request", lambda cfg, m, p, body=None: {"sessions": []}
    )
    args = argparse.Namespace(repo="pg", all=False)
    assert client.cmd_list(CFG, args) == 0
    assert "No active sessions on pg." in capsys.readouterr().out


def test_cmd_list_prints_each_session(monkeypatch, capsys):
    monkeypatch.setattr(
        client,
        "_request",
        lambda cfg, m, p, body=None: {
            "sessions": [{"repo": "pg", "machine": "m1", "goal": "ship it"}]
        },
    )
    args = argparse.Namespace(repo="pg", all=False)
    assert client.cmd_list(CFG, args) == 0
    out = capsys.readouterr().out
    assert "pg" in out and "ship it" in out


def test_list_groups_by_type(monkeypatch, capsys):
    def fake_request(cfg, method, path, body=None):
        return {
            "sessions": [
                {"repo": "pg", "type": "runner", "machine": "snoopy", "goal": "CI: pg"},
                {"repo": "pg", "type": "agent", "machine": "air", "goal": "reviewing"},
            ]
        }

    monkeypatch.setattr(client, "_request", fake_request)
    args = argparse.Namespace(repo="pg", all=False)
    rc = client.cmd_list(CFG, args)
    out = capsys.readouterr().out
    assert rc == 0
    assert "agents:" in out
    assert "runners:" in out
    assert out.index("agents:") < out.index("runners:")
    assert "snoopy" in out
    assert "air" in out


# --- main() dispatch ---


def test_main_dispatches_login(monkeypatch):
    monkeypatch.setattr(client, "cmd_login", lambda cfg, args: 42)
    assert client.main(["login"]) == 42


def test_main_dispatches_init(monkeypatch):
    monkeypatch.setattr(client, "cmd_init", lambda cfg, args: 7)
    assert client.main(["init"]) == 7


def test_main_unconfigured_list_prints_message(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(client, "CONFIG_PATH", tmp_path / "missing")
    monkeypatch.delenv("STANDUP_URL", raising=False)
    monkeypatch.delenv("STANDUP_TOKEN", raising=False)
    assert client.main(["list"]) == 0
    assert "not configured" in capsys.readouterr().err


def test_main_unconfigured_non_list_is_silent(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(client, "CONFIG_PATH", tmp_path / "missing")
    monkeypatch.delenv("STANDUP_URL", raising=False)
    monkeypatch.delenv("STANDUP_TOKEN", raising=False)

    def boom(*a, **k):
        raise AssertionError("handler should not run when unconfigured")

    monkeypatch.setattr(client, "cmd_register", boom)
    assert client.main(["register"]) == 0
    assert capsys.readouterr().err == ""


def test_main_dispatches_configured_commands(monkeypatch, tmp_path):
    cfg_file = tmp_path / "env"
    cfg_file.write_text("STANDUP_URL=http://x\nSTANDUP_TOKEN=t\n")
    monkeypatch.setattr(client, "CONFIG_PATH", cfg_file)
    monkeypatch.delenv("STANDUP_URL", raising=False)
    monkeypatch.delenv("STANDUP_TOKEN", raising=False)

    calls = []
    monkeypatch.setattr(
        client, "cmd_register", lambda cfg, args: calls.append("register") or 0
    )
    monkeypatch.setattr(
        client, "cmd_deregister", lambda cfg, args: calls.append("deregister") or 0
    )
    monkeypatch.setattr(
        client, "cmd_status", lambda cfg, args: calls.append("status") or 0
    )
    monkeypatch.setattr(client, "cmd_list", lambda cfg, args: calls.append("list") or 0)

    assert client.main(["register"]) == 0
    assert client.main(["deregister"]) == 0
    assert client.main(["status"]) == 0
    assert client.main(["list"]) == 0
    assert calls == ["register", "deregister", "status", "list"]


# --- subagent rows ---


def test_status_as_subagent_derives_the_child_id_from_the_inherited_parent(monkeypatch):
    """The caller supplies only a label; the parent id comes from the session.

    That is the whole point: a subagent cannot learn its own agent id, and
    making the PARENT paste its session id into every dispatched prompt is the
    kind of step that gets forgotten — which silently reintroduces the
    clobbering this flag exists to stop.
    """
    bodies = []
    monkeypatch.setattr(client, "_active_context", lambda cwd: {"active_branch": None})
    monkeypatch.setattr(
        client,
        "_request",
        lambda cfg, m, p, body=None: bodies.append(body) or None,
    )
    args = _status_args(session_id="p1", as_subagent="lane-d", goal="port the screens")
    assert client.cmd_status(CFG, args) == 0
    assert bodies[0]["session_id"] == "p1:lane-d"
    assert bodies[0]["parent_session_id"] == "p1"
    assert bodies[0]["goal"] == "port the screens"


def test_status_omits_the_parent_field_entirely_when_not_given(monkeypatch):
    """An ordinary session must not post a null parent and self-declare as a child."""
    bodies = []
    monkeypatch.setattr(client, "_active_context", lambda cwd: {"active_branch": None})
    monkeypatch.setattr(
        client,
        "_request",
        lambda cfg, m, p, body=None: bodies.append(body) or None,
    )
    assert client.cmd_status(CFG, _status_args(goal="g")) == 0
    assert "parent_session_id" not in bodies[0]


def _row(sid, *, parent=None, goal=None, repo="pg", machine="m"):
    return {
        "session_id": sid,
        "parent_session_id": parent,
        "goal": goal,
        "repo": repo,
        "machine": machine,
    }


def test_roster_lines_nest_a_child_under_its_parent():
    lines = client._roster_lines(
        [_row("p1", goal="lead"), _row("p1:w", parent="p1", goal="worker")]
    )
    assert len(lines) == 2
    assert lines[0].startswith("  pg\t") and "lead" in lines[0]
    assert lines[1].startswith("    ↳ pg\t") and "worker" in lines[1]


def test_roster_lines_keep_an_orphan_child_visible_at_top_level():
    """Its parent is filtered out by --repo or has ended; the work is still live.

    Dropping the row would hide an agent that holds a branch — the exact
    collision the board exists to prevent.
    """
    lines = client._roster_lines([_row("p1:w", parent="gone", goal="worker")])
    assert len(lines) == 1
    assert lines[0].startswith("  pg\t")
    assert "↳" not in lines[0]


def test_roster_lines_group_every_child_under_the_right_parent():
    lines = client._roster_lines(
        [
            _row("a", goal="A"),
            _row("b", goal="B"),
            _row("a:1", parent="a", goal="A1"),
            _row("b:1", parent="b", goal="B1"),
            _row("a:2", parent="a", goal="A2"),
        ]
    )
    # `_describe` renders "<machine> — <goal>", so compare the goal tails.
    assert [line.rsplit(" — ", 1)[-1] for line in lines] == ["A", "A1", "A2", "B", "B1"]
    assert [line.startswith("    \u21b3") for line in lines] == [
        False,
        True,
        True,
        False,
        True,
    ]


# --- claim ---


class _Board:
    """In-memory /sessions: GET lists, POST upserts and stamps the narrative."""

    def __init__(self, sessions=(), on_post=None):
        self.sessions = {s["session_id"]: dict(s) for s in sessions}
        self.clock = 100.0
        self.posts = []
        self.on_post = on_post

    def request(self, cfg, method, path, body=None):
        if method == "GET":
            return {"sessions": list(self.sessions.values())}
        self.posts.append(body)
        self.clock += 1
        row = self.sessions.setdefault(body["session_id"], {})
        row.update(body, narrative_updated_at=self.clock)
        if self.on_post:
            self.on_post(self)
        return row


def _claimed(sid, item, at, machine="other"):
    return {
        "session_id": sid,
        "machine": machine,
        "repo": "pg",
        "current_step": f"CLAIMED: {item}",
        "narrative_updated_at": at,
    }


def _claim(monkeypatch, board, item="delete get_game_state"):
    monkeypatch.setattr(client, "_request", board.request)
    monkeypatch.setattr(client, "_active_context", lambda cwd: {"active_branch": None})
    args = argparse.Namespace(
        item=item, session_id="me", cwd="/tmp", repo="pg", as_subagent=None
    )
    return client.cmd_claim(CFG, args)


def test_claim_posts_the_claimed_step_when_the_item_is_free(monkeypatch):
    board = _Board()
    assert _claim(monkeypatch, board) == 0
    assert board.sessions["me"]["current_step"] == "CLAIMED: delete get_game_state"


def test_claim_refuses_an_item_another_session_holds(monkeypatch, capsys):
    board = _Board([_claimed("them", "Delete  GET_game_state ", at=50.0)])
    assert _claim(monkeypatch, board) == 1
    assert board.posts == []
    assert "other" in capsys.readouterr().err


def test_claim_lets_a_session_reclaim_its_own_item(monkeypatch):
    board = _Board([_claimed("me", "delete get_game_state", at=50.0, machine="m")])
    assert _claim(monkeypatch, board) == 0


def test_claim_loses_a_race_to_an_earlier_claim_and_restores_its_step(monkeypatch):
    def rival_claims_first(b):
        if "them" not in b.sessions:
            b.sessions["them"] = _claimed("them", "delete get_game_state", at=1.0)

    board = _Board(
        [{"session_id": "me", "repo": "pg", "current_step": "reading todo"}],
        on_post=rival_claims_first,
    )
    assert _claim(monkeypatch, board) == 1
    assert board.sessions["me"]["current_step"] == "reading todo"


def test_claim_wins_a_race_against_a_later_claim(monkeypatch):
    def rival_claims_later(b):
        b.sessions["them"] = _claimed("them", "delete get_game_state", at=10_000.0)

    board = _Board(on_post=rival_claims_later)
    assert _claim(monkeypatch, board) == 0
    assert board.sessions["me"]["current_step"] == "CLAIMED: delete get_game_state"


def test_claim_says_so_when_the_board_is_unreachable(monkeypatch, capsys):
    def down(cfg, method, path, body=None):
        raise OSError("no route")

    monkeypatch.setattr(client, "_request", down)
    args = argparse.Namespace(
        item="x", session_id="me", cwd="/tmp", repo="pg", as_subagent=None
    )
    assert client.cmd_claim(CFG, args) == 0
    assert "not recorded" in capsys.readouterr().err


# --- park and resume (docs/superpowers/specs/2026-11-02-park-and-resume-design.md) ---


class _ParkBoard(_Board):
    """_Board plus the take endpoint."""

    def request(self, cfg, method, path, body=None):
        if method == "POST" and path.endswith("/take"):
            sid = urllib.parse.unquote(path.split("/")[2])
            row = self.sessions.pop(sid, None)
            if row is None:
                raise urllib.error.HTTPError(path, 404, "gone", {}, None)
            return row
        return super().request(cfg, method, path, body)


def _git_state(monkeypatch, dirty="", unpushed="0", top="/w/pg-p8"):
    answers = {
        ("rev-parse", "--show-toplevel"): top,
        ("status", "--porcelain"): dirty or None,
        ("rev-list", "--count", "@{u}..HEAD"): unpushed,
    }
    monkeypatch.setattr(client, "_git", lambda cwd, *a: answers.get(a))
    monkeypatch.setattr(
        client, "_active_context", lambda cwd: {"active_branch": "feat/p8"}
    )
    monkeypatch.setattr(client, "_pr_for_branch", lambda cwd, repo, b: None)


def _park_args(**kw):
    base = dict(session_id=None, cwd=None, repo="pg", reason=None)
    base.update(kw)
    return argparse.Namespace(**base)


def _live(sid="s1", machine="lucy"):
    return {
        "session_id": sid,
        "machine": machine,
        "repo": "pg",
        "type": "agent",
        "goal": "ship P8",
        "current_step": "CLAIMED: P8",
        "active_branch": "feat/p8",
        "worktrees": [{"path": "/w/pg-p8", "branch": "feat/p8"}],
    }


def test_park_from_stop_failure_hook_posts_parked_twin(monkeypatch):
    board = _ParkBoard([_live()])
    monkeypatch.setattr(client, "_request", board.request)
    monkeypatch.setattr(client, "_machine", lambda: "lucy")
    monkeypatch.setattr(
        client,
        "_read_hook_stdin",
        lambda: {
            "session_id": "s1",
            "cwd": "/w/pg-p8",
            "error": "rate_limit",
            "last_assistant_message": "You've hit your session limit · resets 3:45pm",
        },
    )
    _git_state(monkeypatch, dirty=" M a.py\n?? b.py", unpushed="2")
    assert client.cmd_park(CFG, _park_args()) == 0
    row = board.sessions["parked:s1"]
    assert row["type"] == "parked"
    assert row["goal"] == "ship P8"
    assert row["current_step"] == "CLAIMED: P8"
    assert row["active_branch"] == "feat/p8"
    assert row["handoff"] == {
        "session_id": "s1",
        "reason": "You've hit your session limit · resets 3:45pm",
        "path": "/w/pg-p8",
        "dirty": 2,
        "unpushed": 2,
    }


def test_park_ignores_subagent_failures(monkeypatch):
    board = _ParkBoard([_live()])
    monkeypatch.setattr(client, "_request", board.request)
    monkeypatch.setattr(
        client,
        "_read_hook_stdin",
        lambda: {"session_id": "s1", "agent_id": "a1", "error": "rate_limit"},
    )
    assert client.cmd_park(CFG, _park_args()) == 0
    assert board.posts == []


def test_park_by_hand_takes_a_reason_and_marks_no_upstream(monkeypatch):
    board = _ParkBoard([_live()])
    monkeypatch.setattr(client, "_request", board.request)
    monkeypatch.setattr(client, "_read_hook_stdin", lambda: {})
    _git_state(monkeypatch, unpushed=None)
    args = _park_args(session_id="s1", cwd="/w/pg-p8", reason="weekly limit")
    assert client.cmd_park(CFG, args) == 0
    handoff = board.sessions["parked:s1"]["handoff"]
    assert handoff["reason"] == "weekly limit"
    assert handoff["unpushed"] is None  # no upstream: nothing is pushed


def test_park_is_silent_when_board_down(monkeypatch):
    def down(cfg, method, path, body=None):
        raise OSError("no route")

    monkeypatch.setattr(client, "_request", down)
    monkeypatch.setattr(client, "_read_hook_stdin", lambda: {"session_id": "s1"})
    _git_state(monkeypatch)
    assert client.cmd_park(CFG, _park_args(cwd="/w")) == 0


def _parked(sid="s1", machine="lucy", dirty=0, unpushed=0):
    return {
        "session_id": f"parked:{sid}",
        "machine": machine,
        "repo": "pg",
        "type": "parked",
        "goal": "ship P8",
        "current_step": "CLAIMED: P8",
        "active_branch": "feat/p8",
        "handoff": {
            "session_id": sid,
            "reason": "session limit · resets 3:45pm",
            "path": "/w/pg-p8",
            "dirty": dirty,
            "unpushed": unpushed,
        },
    }


def _resume(monkeypatch, board, target, machine="snoopy"):
    monkeypatch.setattr(client, "_request", board.request)
    monkeypatch.setattr(client, "_machine", lambda: machine)
    monkeypatch.setattr(client, "_active_context", lambda cwd: {"active_branch": None})
    args = argparse.Namespace(
        target=target, session_id="me", cwd="/tmp", repo="pg", as_subagent=None
    )
    return client.cmd_resume(CFG, args)


def test_resume_without_target_lists_parked_work(monkeypatch, capsys):
    board = _ParkBoard([_live("other"), _parked("abcdef12")])
    assert _resume(monkeypatch, board, None) == 0
    out = capsys.readouterr().out
    assert "abcdef12" in out and "resets 3:45pm" in out
    assert "other" not in out  # live agents are not resumable


def test_resume_takes_the_row_and_carries_goal_and_claim(monkeypatch, capsys):
    board = _ParkBoard([_parked("abcdef12")])
    assert _resume(monkeypatch, board, "abcd") == 0
    assert "parked:abcdef12" not in board.sessions
    me = board.sessions["me"]
    assert me["goal"] == "ship P8"
    assert me["current_step"] == "CLAIMED: P8"
    out = capsys.readouterr().out
    assert "git fetch origin feat/p8" in out
    assert "stop the original" in out.lower()


def test_resume_on_the_same_machine_prints_claude_resume(monkeypatch, capsys):
    board = _ParkBoard([_parked("abcdef12", dirty=3)])
    assert _resume(monkeypatch, board, "abcdef12", machine="lucy") == 0
    assert "cd /w/pg-p8 && claude --resume abcdef12" in capsys.readouterr().out


def test_resume_warns_when_work_is_only_on_the_parked_machine(monkeypatch, capsys):
    board = _ParkBoard([_parked("abcdef12", dirty=3, unpushed=None)])
    assert _resume(monkeypatch, board, "abcdef12") == 0
    out = capsys.readouterr().out
    assert "only on lucy" in out
    assert "git fetch" not in out


def test_resume_loses_the_race_cleanly(monkeypatch, capsys):
    board = _ParkBoard([_parked("abcdef12")])

    def rival_takes_first(cfg, method, path, body=None):
        if path.endswith("/take"):
            board.sessions.pop("parked:abcdef12", None)
        return _ParkBoard.request(board, cfg, method, path, body)

    monkeypatch.setattr(client, "_machine", lambda: "snoopy")
    monkeypatch.setattr(client, "_request", rival_takes_first)
    args = argparse.Namespace(
        target="abcdef12", session_id="me", cwd="/tmp", repo="pg", as_subagent=None
    )
    assert client.cmd_resume(CFG, args) == 1
    assert "me" not in board.sessions
    assert "already" in capsys.readouterr().err


def test_resume_rejects_an_unknown_or_ambiguous_target(monkeypatch, capsys):
    board = _ParkBoard([_parked("abc1"), _parked("abc2")])
    assert _resume(monkeypatch, board, "zzz") == 1
    assert _resume(monkeypatch, board, "abc") == 1
    assert "abc1" in capsys.readouterr().err


def test_register_notice_lists_parked_work(monkeypatch, capsys):
    board = _ParkBoard([_parked("abcdef12")])
    monkeypatch.setattr(client, "_request", board.request)
    monkeypatch.setattr(
        client, "_read_hook_stdin", lambda: {"session_id": "s9", "cwd": "/tmp"}
    )
    monkeypatch.setattr(client, "_worktrees", lambda cwd: [])
    monkeypatch.setattr(client, "_enrich_worktrees", lambda c, r, w: [])
    assert client.cmd_register(CFG, _reg_args()) == 0
    out = capsys.readouterr().out
    assert "parked" in out and "standup resume" in out
    assert "other active agent" not in out  # a parked row is not a co-worker


def test_list_titles_the_parked_section(monkeypatch, capsys):
    board = _ParkBoard([_live(), _parked()])
    monkeypatch.setattr(client, "_request", board.request)
    args = argparse.Namespace(repo="pg", all=False)
    assert client.cmd_list(CFG, args) == 0
    out = capsys.readouterr().out
    assert "parked:\n" in out and "parkeds" not in out
    assert out.index("agents:") < out.index("parked:")


def test_init_wires_park_on_rate_limit_stop_failure():
    settings = client._merge_hooks({}, "standup")
    groups = settings["hooks"]["StopFailure"]
    assert groups == [
        {
            "matcher": "rate_limit",
            "hooks": [{"type": "command", "command": "standup park", "timeout": 20}],
        }
    ]


def test_park_reads_git_state_from_the_reported_branch_worktree(monkeypatch):
    # The hook's cwd is the hub on main; the session last reported feat/p8.
    live = _live() | {
        "active_branch": "feat/p8",
        "worktrees": [
            {"path": "/w/pg", "branch": "main"},
            {"path": "/w/pg-p8", "branch": "feat/p8"},
        ],
    }
    board = _ParkBoard([live])
    monkeypatch.setattr(client, "_request", board.request)
    monkeypatch.setattr(
        client, "_read_hook_stdin", lambda: {"session_id": "s1", "cwd": "/w/pg"}
    )
    seen = []

    def git(cwd, *a):
        seen.append(cwd)
        return {("rev-parse", "--show-toplevel"): cwd}.get(a)

    monkeypatch.setattr(client, "_git", git)
    monkeypatch.setattr(
        client, "_active_context", lambda cwd: {"active_branch": "main"}
    )
    assert client.cmd_park(CFG, _park_args()) == 0
    row = board.sessions["parked:s1"]
    assert row["active_branch"] == "feat/p8"
    assert row["handoff"]["path"] == "/w/pg-p8"
    assert set(seen) == {"/w/pg-p8"}


# --- review round 1 ---


def test_park_without_a_reported_branch_marks_the_work_unknown(monkeypatch, capsys):
    # Never ran `standup status`: the hook cwd (the hub on main) proves nothing.
    board = _ParkBoard([_live() | {"active_branch": None, "worktrees": None}])
    monkeypatch.setattr(client, "_request", board.request)
    monkeypatch.setattr(client, "_machine", lambda: "lucy")
    monkeypatch.setattr(
        client, "_read_hook_stdin", lambda: {"session_id": "s1", "cwd": "/w/pg"}
    )
    _git_state(monkeypatch)
    assert client.cmd_park(CFG, _park_args()) == 0
    row = board.sessions["parked:s1"]
    assert row["active_branch"] is None
    capsys.readouterr()
    assert _resume(monkeypatch, board, "s1") == 0
    out = capsys.readouterr().out
    assert "only on lucy" in out and "git fetch" not in out


def test_park_cuts_the_reason_to_one_short_line(monkeypatch):
    board = _ParkBoard([_live()])
    monkeypatch.setattr(client, "_request", board.request)
    long = "You've hit your session limit · resets 3:45pm\n\n" + "x" * 500
    monkeypatch.setattr(
        client,
        "_read_hook_stdin",
        lambda: {"session_id": "s1", "cwd": "/w", "last_assistant_message": long},
    )
    _git_state(monkeypatch)
    assert client.cmd_park(CFG, _park_args()) == 0
    reason = board.sessions["parked:s1"]["handoff"]["reason"]
    assert reason == "You've hit your session limit · resets 3:45pm"


def test_resume_refuses_without_a_session_to_carry_the_claim(monkeypatch, capsys):
    board = _ParkBoard([_parked("abcdef12")])
    monkeypatch.setattr(client, "_request", board.request)
    monkeypatch.setattr(client, "_status_target", lambda args: None)
    args = argparse.Namespace(
        target="abcdef12", session_id=None, cwd="/tmp", repo="pg", as_subagent=None
    )
    assert client.cmd_resume(CFG, args) == 1
    assert "parked:abcdef12" in board.sessions
    assert "session" in capsys.readouterr().err


def test_resume_still_prints_the_hint_when_the_status_post_fails(monkeypatch, capsys):
    board = _ParkBoard([_parked("abcdef12")])

    def flaky(cfg, method, path, body=None):
        if method == "POST" and path == "/sessions":
            raise urllib.error.URLError("timeout")
        return board.request(cfg, method, path, body)

    monkeypatch.setattr(client, "_machine", lambda: "snoopy")
    monkeypatch.setattr(client, "_active_context", lambda cwd: {"active_branch": None})
    monkeypatch.setattr(client, "_request", flaky)
    args = argparse.Namespace(
        target="abcdef12", session_id="me", cwd="/tmp", repo="pg", as_subagent=None
    )
    assert client.cmd_resume(CFG, args) == 0
    captured = capsys.readouterr()
    assert "git fetch origin feat/p8" in captured.out
    assert "CLAIMED: P8" in captured.err  # tells the taker to re-post it


def test_resume_on_the_same_machine_offers_the_folder_as_fallback(monkeypatch, capsys):
    board = _ParkBoard([_parked("abcdef12")])
    assert _resume(monkeypatch, board, "abcdef12", machine="lucy") == 0
    out = capsys.readouterr().out
    assert "claude --resume abcdef12" in out
    assert "another account" in out


def test_resume_hint_never_reuses_a_stale_local_branch(monkeypatch, capsys):
    board = _ParkBoard([_parked("abcdef12")])
    assert _resume(monkeypatch, board, "abcdef12") == 0
    out = capsys.readouterr().out
    assert "--track -b feat/p8 ../feat-p8 origin/feat/p8" in out


def test_register_notice_skips_this_sessions_own_parked_row(monkeypatch, capsys):
    board = _ParkBoard([_parked("s9")])
    monkeypatch.setattr(client, "_request", board.request)
    monkeypatch.setattr(
        client, "_read_hook_stdin", lambda: {"session_id": "s9", "cwd": "/tmp"}
    )
    monkeypatch.setattr(client, "_worktrees", lambda cwd: [])
    monkeypatch.setattr(client, "_enrich_worktrees", lambda c, r, w: [])
    assert client.cmd_register(CFG, _reg_args()) == 0
    assert "parked" not in capsys.readouterr().out


def test_init_gives_the_park_hook_room_for_two_board_calls():
    settings = client._merge_hooks({}, "standup")
    park = settings["hooks"]["StopFailure"][0]["hooks"][0]
    assert park["timeout"] >= 12  # GET + POST at TIMEOUT each, plus git
    assert settings["hooks"]["SessionEnd"][0]["hooks"][0]["timeout"] == 5


# --- review round 2 ---


def test_park_survives_a_non_string_error_detail(monkeypatch):
    board = _ParkBoard([_live()])
    monkeypatch.setattr(client, "_request", board.request)
    monkeypatch.setattr(
        client,
        "_read_hook_stdin",
        lambda: {"session_id": "s1", "cwd": "/w", "error_details": {"code": 429}},
    )
    _git_state(monkeypatch)
    assert client.cmd_park(CFG, _park_args()) == 0
    assert "429" in board.sessions["parked:s1"]["handoff"]["reason"]


def test_resume_fallback_hint_is_shell_safe(monkeypatch, capsys):
    row = _parked("abcdef12") | {"goal": "fix $(whoami)'s bug", "current_step": None}
    board = _ParkBoard([row])

    def flaky(cfg, method, path, body=None):
        if method == "POST" and path == "/sessions":
            raise urllib.error.URLError("timeout")
        return board.request(cfg, method, path, body)

    monkeypatch.setattr(client, "_machine", lambda: "snoopy")
    monkeypatch.setattr(client, "_active_context", lambda cwd: {"active_branch": None})
    monkeypatch.setattr(client, "_request", flaky)
    args = argparse.Namespace(target="abcdef12", session_id="me", cwd="/tmp", repo="pg")
    client.cmd_resume(CFG, args)
    err = capsys.readouterr().err
    assert "--goal 'fix $(whoami)'\"'\"'s bug'" in err
    assert "--step" not in err


def test_resume_exits_1_when_the_board_fails_the_take(monkeypatch, capsys):
    board = _ParkBoard([_parked("abcdef12")])

    def down_on_take(cfg, method, path, body=None):
        if path.endswith("/take"):
            raise urllib.error.URLError("timeout")
        return board.request(cfg, method, path, body)

    monkeypatch.setattr(client, "_request", down_on_take)
    args = argparse.Namespace(target="abcdef12", session_id="me", cwd="/tmp", repo="pg")
    assert client.cmd_resume(CFG, args) == 1


def test_resume_has_no_subagent_option():
    parser = client._build_parser()
    try:
        parser.parse_args(["resume", "x", "--as-subagent", "w"])
    except SystemExit:
        return
    raise AssertionError("resume must not hand a claim to a short-lived subagent row")


def test_cross_session_message_is_not_a_real_prompt():
    # Seen on the board as a session's last prompt, attributes and all.
    msg = '<cross-session-message from="bridge:session_01" from-name="lucy">\nhi'
    assert client._is_system_prompt(msg) is True


# --- standup done ---


def test_done_marks_the_row_done_with_its_step(monkeypatch):
    board = _Board([{"session_id": "me", "repo": "pg", "type": "agent"}])
    monkeypatch.setattr(client, "_request", board.request)
    monkeypatch.setattr(client, "_active_context", lambda cwd: {"active_branch": None})
    args = argparse.Namespace(
        step="Done: #12 merged", session_id="me", cwd="/tmp", repo="pg"
    )
    assert client.cmd_done(CFG, args) == 0
    assert board.sessions["me"]["type"] == "done"
    assert board.sessions["me"]["current_step"] == "Done: #12 merged"


def test_status_makes_a_done_session_active_again(monkeypatch):
    board = _Board([{"session_id": "s1", "repo": "pg", "type": "done"}])
    monkeypatch.setattr(client, "_request", board.request)
    monkeypatch.setattr(client, "_active_context", lambda cwd: {"active_branch": None})
    client.cmd_status(CFG, _status_args(step="back at it"))
    assert board.sessions["s1"]["type"] == "agent"


def test_real_prompt_makes_a_done_session_active_again(monkeypatch):
    bodies = []
    monkeypatch.setattr(
        client,
        "_read_hook_stdin",
        lambda: {"session_id": "s1", "cwd": "/tmp", "prompt": "fix the next bug"},
    )
    monkeypatch.setattr(client, "_worktrees", lambda cwd: [])
    monkeypatch.setattr(client, "_enrich_worktrees", lambda c, r, w: [])
    monkeypatch.setattr(
        client, "_request", lambda cfg, m, p, body=None: bodies.append(body) or None
    )
    client.cmd_register(CFG, _reg_args())
    assert bodies[0]["type"] == "agent"


def test_system_wakeup_makes_a_done_session_active_again(monkeypatch):
    # Any activity is work: a wakeup a done session acts on counts too.
    bodies = []
    monkeypatch.setattr(
        client,
        "_read_hook_stdin",
        lambda: {"session_id": "s1", "cwd": "/tmp", "prompt": "<task-notification>\nx"},
    )
    monkeypatch.setattr(client, "_worktrees", lambda cwd: [])
    monkeypatch.setattr(client, "_enrich_worktrees", lambda c, r, w: [])
    monkeypatch.setattr(
        client, "_request", lambda cfg, m, p, body=None: bodies.append(body) or None
    )
    client.cmd_register(CFG, _reg_args())
    assert bodies[0]["type"] == "agent"


def test_runner_register_keeps_its_type(monkeypatch):
    bodies = []
    monkeypatch.setattr(client, "_read_hook_stdin", lambda: {})
    monkeypatch.setattr(
        client, "_request", lambda cfg, m, p, body=None: bodies.append(body) or None
    )
    args = _reg_args()
    args.session_id, args.type = "runner:x", "runner"
    client.cmd_register(CFG, args)
    assert bodies[0]["type"] == "runner"


def test_done_refuses_while_subagents_run(monkeypatch, capsys):
    # A subagent shares its parent's session id: its `done` would mark the
    # parent finished mid-work. The parent's row lists it as running.
    row = {
        "session_id": "me",
        "repo": "pg",
        "type": "agent",
        "subagents": [{"agent_id": "a1", "label": "worker", "started_at": 1.0}],
    }
    board = _Board([row])
    monkeypatch.setattr(client, "_request", board.request)
    args = argparse.Namespace(step="done", session_id="me", cwd="/tmp", repo="pg")
    assert client.cmd_done(CFG, args) == 1
    assert board.sessions["me"]["type"] == "agent"
    assert "subagent" in capsys.readouterr().err


def test_done_removes_its_finished_subagent_rows(monkeypatch):
    deleted = []
    board = _Board(
        [
            {"session_id": "me", "repo": "pg", "type": "agent"},
            {"session_id": "me:fix", "repo": "pg", "parent_session_id": "me"},
            {"session_id": "other", "repo": "pg"},
        ]
    )

    def request(cfg, method, path, body=None):
        if method == "DELETE":
            deleted.append(urllib.parse.unquote(path.rsplit("/", 1)[1]))
            return None
        return board.request(cfg, method, path, body)

    monkeypatch.setattr(client, "_request", request)
    monkeypatch.setattr(client, "_active_context", lambda cwd: {"active_branch": None})
    args = argparse.Namespace(step="done", session_id="me", cwd="/tmp", repo="pg")
    assert client.cmd_done(CFG, args) == 0
    assert deleted == ["me:fix"]


def test_register_notice_does_not_count_done_sessions(monkeypatch, capsys):
    board = _Board(
        [{"session_id": "old", "repo": "pg", "type": "done", "machine": "m"}]
    )
    monkeypatch.setattr(client, "_request", board.request)
    monkeypatch.setattr(
        client, "_read_hook_stdin", lambda: {"session_id": "s9", "cwd": "/tmp"}
    )
    monkeypatch.setattr(client, "_worktrees", lambda cwd: [])
    monkeypatch.setattr(client, "_enrich_worktrees", lambda c, r, w: [])
    client.cmd_register(CFG, _reg_args())
    assert "other active" not in capsys.readouterr().out


def test_list_titles_the_done_section_last(monkeypatch, capsys):
    board = _Board(
        [
            {"session_id": "a", "repo": "pg", "type": "done"},
            {"session_id": "b", "repo": "pg", "type": "runner"},
            {"session_id": "c", "repo": "pg", "type": "agent"},
        ]
    )
    monkeypatch.setattr(client, "_request", board.request)
    client.cmd_list(CFG, argparse.Namespace(repo="pg", all=False))
    out = capsys.readouterr().out
    assert "done:\n" in out and "dones" not in out
    assert out.index("runners:") < out.index("done:")
