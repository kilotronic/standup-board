import argparse
import importlib.machinery
import importlib.util
import subprocess
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


def test_status_without_session_id_is_noop(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    called = []
    monkeypatch.setattr(client, "_request", lambda *a, **k: called.append(a))
    assert client.cmd_status(CFG, _status_args(session_id=None)) == 0
    assert called == []


def test_status_session_id_from_env(monkeypatch):
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


# --- _open_browser / _run_mcp_add ---


def test_open_browser_never_raises_on_subprocess_failure(monkeypatch):
    def boom(*a, **k):
        raise OSError("no xdg-open")

    monkeypatch.setattr(client.subprocess, "run", boom)
    client._open_browser("http://x")  # must not raise


def test_run_mcp_add_prints_message_on_failure(monkeypatch, capsys):
    def boom(*a, **k):
        raise OSError("no claude cli")

    monkeypatch.setattr(client.subprocess, "run", boom)
    client._run_mcp_add("user")
    assert "could not run `claude mcp add`" in capsys.readouterr().err


def test_run_mcp_add_silent_on_success(monkeypatch, capsys):
    calls = []

    def fake_run(*a, **k):
        calls.append(k)
        return _FakeCompleted(returncode=0)

    monkeypatch.setattr(client.subprocess, "run", fake_run)
    client._run_mcp_add("user")
    assert capsys.readouterr().err == ""

    client._run_mcp_add("user", cwd="/some/dir")
    assert calls[-1]["cwd"] == "/some/dir"


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


def test_cmd_register_without_session_id_is_noop(monkeypatch):
    monkeypatch.setattr(client, "_read_hook_stdin", lambda: {})
    called = []
    monkeypatch.setattr(client, "_request", lambda *a, **k: called.append(a))
    assert client.cmd_register(CFG, _reg_args()) == 0
    assert called == []


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


def test_cmd_deregister_without_session_id_is_noop(monkeypatch):
    monkeypatch.setattr(client, "_read_hook_stdin", lambda: {})
    called = []
    monkeypatch.setattr(client, "_request", lambda *a, **k: called.append(a))
    args = argparse.Namespace(session_id=None)
    assert client.cmd_deregister(CFG, args) == 0
    assert called == []


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
    assert "No active agent sessions" in capsys.readouterr().out


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
