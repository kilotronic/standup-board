import argparse
import importlib.machinery
import importlib.util
import json
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_CLIENT = _ROOT / "client" / "standup"
_loader = importlib.machinery.SourceFileLoader("standup_client_init", str(_CLIENT))
client = importlib.util.module_from_spec(
    importlib.util.spec_from_loader("standup_client_init", _loader)
)
_loader.exec_module(client)

CFG = {"STANDUP_URL": "http://x", "STANDUP_TOKEN": "t"}


@pytest.fixture(autouse=True)
def _redirect_install(tmp_path, monkeypatch):
    # `init` now self-installs the CLI symlink; keep tests off the real
    # ~/.local/bin and quiet the PATH warning by putting the temp dir on PATH.
    monkeypatch.setattr(client, "INSTALL_PATH", tmp_path / "bin" / "standup")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))


def _init_repo(tmp_path, monkeypatch, shared=False):
    monkeypatch.setattr(
        client,
        "_git",
        lambda cwd, *a: str(tmp_path) if a[:1] == ("rev-parse",) else None,
    )
    args = argparse.Namespace(shared=shared, cwd=str(tmp_path))
    return client.cmd_init(CFG, args)


def test_init_vendors_skill(tmp_path, monkeypatch):
    assert _init_repo(tmp_path, monkeypatch) == 0
    skill = tmp_path / ".claude" / "skills" / "standup" / "SKILL.md"
    assert skill.is_file()
    assert "name: standup" in skill.read_text()


def test_init_self_installs_symlink(tmp_path, monkeypatch):
    _init_repo(tmp_path, monkeypatch)
    link = tmp_path / "bin" / "standup"
    assert link.is_symlink()
    assert link.resolve() == Path(client.__file__).resolve()


def test_init_local_writes_settings_local(tmp_path, monkeypatch):
    _init_repo(tmp_path, monkeypatch, shared=False)
    settings = json.loads((tmp_path / ".claude" / "settings.local.json").read_text())
    cmds = json.dumps(settings["hooks"])
    assert "standup register" in cmds and "standup deregister" in cmds


def test_init_local_writes_cursor_hooks(tmp_path, monkeypatch):
    _init_repo(tmp_path, monkeypatch, shared=False)
    hooks = json.loads((tmp_path / ".cursor" / "hooks.json").read_text())
    assert hooks["version"] == 1
    cmds = json.dumps(hooks["hooks"])
    assert "standup register" in cmds and "standup deregister" in cmds
    assert "sessionStart" in hooks["hooks"]
    assert "beforeSubmitPrompt" not in hooks["hooks"]
    assert "sessionEnd" in hooks["hooks"]


def test_init_shared_writes_committed_files(tmp_path, monkeypatch):
    _init_repo(tmp_path, monkeypatch, shared=True)
    assert (tmp_path / ".claude" / "settings.json").is_file()
    assert (tmp_path / ".cursor" / "hooks.json").is_file()


def test_init_is_idempotent(tmp_path, monkeypatch):
    _init_repo(tmp_path, monkeypatch, shared=True)
    _init_repo(tmp_path, monkeypatch, shared=True)
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert len(settings["hooks"]["SessionStart"][0]["hooks"]) == 1
    cursor = json.loads((tmp_path / ".cursor" / "hooks.json").read_text())
    assert len(cursor["hooks"]["sessionStart"]) == 1


def test_init_requires_login(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(client, "_git", lambda cwd, *a: str(tmp_path))
    rc = client.cmd_init({}, argparse.Namespace(shared=False, cwd=str(tmp_path)))
    assert rc == 0
    assert not (tmp_path / ".claude").exists()
    assert "standup login" in capsys.readouterr().err


def test_embedded_skill_matches_repo_file():
    disk = (_ROOT / "skills" / "standup" / "SKILL.md").read_text().strip()
    assert client.SKILL_MD.strip() == disk


def test_merge_hooks_adds_both_events():
    out = client._merge_hooks({}, "/bin/standup")
    cmds = [
        h["command"]
        for e in ("SessionStart", "SessionEnd")
        for g in out["hooks"][e]
        for h in g["hooks"]
    ]
    assert "/bin/standup register" in cmds
    assert "/bin/standup deregister" in cmds


def test_merge_hooks_is_idempotent_and_preserves_foreign_hooks():
    existing = {
        "hooks": {
            "SessionStart": [{"hooks": [{"type": "command", "command": "other-tool"}]}]
        }
    }
    once = client._merge_hooks(existing, "/bin/standup")
    twice = client._merge_hooks(once, "/bin/standup")
    starts = [h["command"] for g in twice["hooks"]["SessionStart"] for h in g["hooks"]]
    assert starts.count("/bin/standup register") == 1  # not duplicated on re-run
    assert "other-tool" in starts  # foreign hook preserved


def test_merge_hooks_sets_matcher_and_timeout():
    out = client._merge_hooks({}, "/bin/standup")
    ss = out["hooks"]["SessionStart"][0]
    assert ss["matcher"] == "startup|resume|clear|compact"
    assert ss["hooks"][0]["timeout"] == 5
    dr = out["hooks"]["SessionEnd"][0]["hooks"]
    assert "/bin/standup deregister" in [h["command"] for h in dr]
    assert all(h["timeout"] == 5 for h in dr)


def test_merge_hooks_wires_no_prompt_or_subagent_hooks():
    """The board is being retired: no post on every prompt or helper agent."""
    out = client._merge_hooks({}, "/bin/standup")
    assert set(out["hooks"]) == {"SessionStart", "SessionEnd", "StopFailure"}


def test_merge_hooks_is_idempotent():
    once = client._merge_hooks({}, "/bin/standup")
    twice = client._merge_hooks(once, "/bin/standup")
    starts = [h["command"] for g in twice["hooks"]["SessionStart"] for h in g["hooks"]]
    assert starts.count("/bin/standup register") == 1


def test_merge_cursor_hooks_adds_events_idempotently():
    once = client._merge_cursor_hooks({}, "/bin/standup")
    twice = client._merge_cursor_hooks(once, "/bin/standup")
    assert twice["version"] == 1
    for event, verb in (
        ("sessionStart", "register"),
        ("sessionEnd", "deregister"),
    ):
        cmds = [h["command"] for h in twice["hooks"][event]]
        assert cmds.count(f"/bin/standup {verb}") == 1
        assert all(h.get("timeout") == 5 for h in twice["hooks"][event])
    assert "beforeSubmitPrompt" not in twice["hooks"]


def test_merge_cursor_hooks_preserves_foreign_hooks():
    existing = {
        "version": 1,
        "hooks": {"sessionStart": [{"command": "other-tool", "timeout": 2}]},
    }
    out = client._merge_cursor_hooks(existing, "/bin/standup")
    cmds = [h["command"] for h in out["hooks"]["sessionStart"]]
    assert "other-tool" in cmds
    assert "/bin/standup register" in cmds


def test_init_global_wires_user_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    rc = client.cmd_init(
        CFG, argparse.Namespace(shared=False, is_global=True, cwd=None)
    )
    assert rc == 0
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert (
        settings["hooks"]["SessionStart"][0]["matcher"]
        == "startup|resume|clear|compact"
    )
    assert "UserPromptSubmit" not in settings["hooks"]
    assert (tmp_path / ".claude" / "skills" / "standup" / "SKILL.md").is_file()
    cursor = json.loads((tmp_path / ".cursor" / "hooks.json").read_text())
    assert "sessionStart" in cursor["hooks"]
    # no per-repo files created
    assert not (tmp_path / ".mcp.json").exists()


def test_init_global_wires_even_without_login(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    rc = client.cmd_init({}, argparse.Namespace(shared=False, is_global=True, cwd=None))
    assert rc == 0
    assert (tmp_path / ".claude" / "settings.json").is_file()


def test_plugin_hooks_match_hook_spec():
    """The plugin's hooks/hooks.json carries the same events, verbs, matchers and
    timeouts as `init` wires, so the two install paths never drift apart."""
    plugin = json.loads((_ROOT / "hooks" / "hooks.json").read_text())["hooks"]
    got = []
    for event, groups in plugin.items():
        for g in groups:
            for h in g["hooks"]:
                got.append((event, h["command"], g.get("matcher"), h["timeout"]))
    want = [
        (
            event,
            f'[ -x "{client.HOOK_CMD}" ] && "{client.HOOK_CMD}" {verb} || true',
            matcher,
            timeout,
        )
        for event, verb, matcher, timeout in client._HOOK_SPEC
    ]
    assert sorted(got, key=str) == sorted(want, key=str)


def test_merge_hooks_removes_its_own_retired_hooks():
    """A machine set up by an older `init` loses standup's prompt and helper
    agent hooks on the next `init`; another tool's hooks stay."""
    old = {
        "hooks": {
            "UserPromptSubmit": [
                {"hooks": [{"type": "command", "command": "/bin/standup register"}]},
                {"hooks": [{"type": "command", "command": "other-tool"}]},
            ],
            "SubagentStart": [
                {"hooks": [{"type": "command", "command": "/bin/standup subagent-start"}]}
            ],
            "SubagentStop": [
                {"hooks": [{"type": "command", "command": "/bin/standup subagent-stop"}]}
            ],
        }
    }
    out = client._merge_hooks(old, "/bin/standup")
    assert "SubagentStart" not in out["hooks"]
    assert "SubagentStop" not in out["hooks"]
    ups = [h["command"] for g in out["hooks"]["UserPromptSubmit"] for h in g["hooks"]]
    assert ups == ["other-tool"]


def test_merge_cursor_hooks_removes_its_own_prompt_hook():
    old = {
        "version": 1,
        "hooks": {
            "beforeSubmitPrompt": [
                {"command": "/bin/standup register", "timeout": 5},
                {"command": "other-tool"},
            ]
        },
    }
    out = client._merge_cursor_hooks(old, "/bin/standup")
    assert out["hooks"]["beforeSubmitPrompt"] == [{"command": "other-tool"}]
    only = client._merge_cursor_hooks(
        {"hooks": {"beforeSubmitPrompt": [{"command": "/bin/standup register"}]}},
        "/bin/standup",
    )
    assert "beforeSubmitPrompt" not in only["hooks"]
