import importlib.machinery
import importlib.util
import os
from pathlib import Path

_CLIENT = Path(__file__).resolve().parent.parent / "client" / "standup"
_loader = importlib.machinery.SourceFileLoader("standup_client_install", str(_CLIENT))
client = importlib.util.module_from_spec(
    importlib.util.spec_from_loader("standup_client_install", _loader)
)
_loader.exec_module(client)

_SOURCE = Path(os.path.realpath(str(_CLIENT)))


def _use_bin(monkeypatch, bin_dir, on_path=True):
    """Point INSTALL_PATH at bin_dir/standup; put bin_dir on PATH iff on_path."""
    monkeypatch.setattr(client, "INSTALL_PATH", bin_dir / "standup")
    monkeypatch.setenv("PATH", str(bin_dir) if on_path else "/nowhere")


def test_creates_symlink_when_absent(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _use_bin(monkeypatch, bin_dir)
    client._ensure_installed()
    link = bin_dir / "standup"
    assert link.is_symlink()
    assert link.resolve() == _SOURCE


def test_creates_missing_bin_dir(tmp_path, monkeypatch):
    bin_dir = tmp_path / "nested" / "bin"  # parent does not exist yet
    _use_bin(monkeypatch, bin_dir)
    client._ensure_installed()
    assert (bin_dir / "standup").is_symlink()


def test_skips_existing_regular_file_and_notes(tmp_path, monkeypatch, capsys):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    existing = bin_dir / "standup"
    existing.write_text("#!/bin/sh\necho other\n")
    _use_bin(monkeypatch, bin_dir)
    client._ensure_installed()
    assert not existing.is_symlink()  # untouched
    assert existing.read_text() == "#!/bin/sh\necho other\n"
    assert "already exists" in capsys.readouterr().err


def test_skips_foreign_symlink_and_notes(tmp_path, monkeypatch, capsys):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    other = tmp_path / "somewhere-else"
    other.write_text("x")
    link = bin_dir / "standup"
    link.symlink_to(other)
    _use_bin(monkeypatch, bin_dir)
    client._ensure_installed()
    assert link.resolve() == other.resolve()  # still points elsewhere
    assert "already exists" in capsys.readouterr().err


def test_silent_when_already_correct(tmp_path, monkeypatch, capsys):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    link = bin_dir / "standup"
    link.symlink_to(_SOURCE)
    _use_bin(monkeypatch, bin_dir)
    client._ensure_installed()
    assert capsys.readouterr().err == ""  # no noise on the already-installed case
    assert link.resolve() == _SOURCE


def test_warns_when_bin_dir_not_on_path(tmp_path, monkeypatch, capsys):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _use_bin(monkeypatch, bin_dir, on_path=False)
    client._ensure_installed()
    err = capsys.readouterr().err
    assert "PATH" in err


def test_no_path_warning_when_on_path(tmp_path, monkeypatch, capsys):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _use_bin(monkeypatch, bin_dir, on_path=True)
    client._ensure_installed()
    assert "PATH" not in capsys.readouterr().err


def test_reports_when_install_fails(tmp_path, monkeypatch, capsys):
    # Parent path is a regular file, so mkdir/symlink can't be created.
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a dir")
    monkeypatch.setattr(client, "INSTALL_PATH", blocker / "bin" / "standup")
    monkeypatch.setenv("PATH", "/nowhere")
    client._ensure_installed()
    assert "could not install" in capsys.readouterr().err


def test_install_path_matches_hook_cmd():
    # The hooks `init` writes call HOOK_CMD; the symlink must land there or the
    # hooks break. Guard against the two drifting apart.
    assert os.path.expandvars(client.HOOK_CMD) == str(client.INSTALL_PATH)
