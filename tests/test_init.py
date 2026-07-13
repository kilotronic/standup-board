import argparse
import importlib.machinery
import importlib.util
import json
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_CLIENT = _ROOT / "client" / "standup"
_loader = importlib.machinery.SourceFileLoader("standup_client_init", str(_CLIENT))
client = importlib.util.module_from_spec(
    importlib.util.spec_from_loader("standup_client_init", _loader)
)
_loader.exec_module(client)

CFG = {"STANDUP_URL": "http://x", "STANDUP_TOKEN": "t"}


def _init_repo(tmp_path, monkeypatch, shared=False):
    monkeypatch.setattr(
        client,
        "_git",
        lambda cwd, *a: str(tmp_path) if a[:1] == ("rev-parse",) else None,
    )
    monkeypatch.setattr(
        client, "_run_mcp_add", lambda root: None
    )  # avoid shelling to `claude`
    args = argparse.Namespace(shared=shared, cwd=str(tmp_path))
    return client.cmd_init(CFG, args)


def test_init_vendors_skill(tmp_path, monkeypatch):
    assert _init_repo(tmp_path, monkeypatch) == 0
    skill = tmp_path / ".claude" / "skills" / "standup" / "SKILL.md"
    assert skill.is_file()
    assert "name: standup" in skill.read_text()


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
