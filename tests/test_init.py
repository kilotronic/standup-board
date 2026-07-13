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
    monkeypatch.setattr(
        client, "_run_mcp_add", lambda *a, **k: None
    )  # avoid shelling to `claude`
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


def test_init_shared_writes_committed_files(tmp_path, monkeypatch):
    _init_repo(tmp_path, monkeypatch, shared=True)
    assert (tmp_path / ".claude" / "settings.json").is_file()
    mcp = json.loads((tmp_path / ".mcp.json").read_text())
    assert "standup" in mcp["mcpServers"]


def test_init_is_idempotent(tmp_path, monkeypatch):
    _init_repo(tmp_path, monkeypatch, shared=True)
    _init_repo(tmp_path, monkeypatch, shared=True)
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert len(settings["hooks"]["SessionStart"][0]["hooks"]) == 1


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


def test_merge_hooks_sets_matcher_prompt_and_timeout():
    out = client._merge_hooks({}, "/bin/standup")
    ss = out["hooks"]["SessionStart"][0]
    assert ss["matcher"] == "startup|resume|clear|compact"
    assert ss["hooks"][0]["timeout"] == 5
    ups = out["hooks"]["UserPromptSubmit"][0]["hooks"]
    assert "/bin/standup register" in [h["command"] for h in ups]
    assert all(h["timeout"] == 5 for h in ups)
    dr = out["hooks"]["SessionEnd"][0]["hooks"]
    assert "/bin/standup deregister" in [h["command"] for h in dr]
    assert all(h["timeout"] == 5 for h in dr)


def test_merge_hooks_prompt_hook_is_idempotent():
    once = client._merge_hooks({}, "/bin/standup")
    twice = client._merge_hooks(once, "/bin/standup")
    ups = [h["command"] for g in twice["hooks"]["UserPromptSubmit"] for h in g["hooks"]]
    assert ups.count("/bin/standup register") == 1


def test_init_global_wires_user_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(client, "_run_mcp_add", lambda *a, **k: None)
    rc = client.cmd_init(
        CFG, argparse.Namespace(shared=False, is_global=True, cwd=None)
    )
    assert rc == 0
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert (
        settings["hooks"]["SessionStart"][0]["matcher"]
        == "startup|resume|clear|compact"
    )
    assert "UserPromptSubmit" in settings["hooks"]
    assert (tmp_path / ".claude" / "skills" / "standup" / "SKILL.md").is_file()
    # no per-repo files created
    assert not (tmp_path / ".mcp.json").exists()


def test_init_global_wires_even_without_login(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(client, "_run_mcp_add", lambda *a, **k: None)
    rc = client.cmd_init({}, argparse.Namespace(shared=False, is_global=True, cwd=None))
    assert rc == 0
    assert (tmp_path / ".claude" / "settings.json").is_file()


def test_init_global_registers_user_scope_mcp(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    calls = []
    monkeypatch.setattr(
        client, "_run_mcp_add", lambda scope, cwd=None: calls.append(scope)
    )
    client.cmd_init(CFG, argparse.Namespace(shared=False, is_global=True, cwd=None))
    assert calls == ["user"]
