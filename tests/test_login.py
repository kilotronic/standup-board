import argparse
import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

_CLIENT = Path(__file__).resolve().parent.parent / "client" / "standup"
_loader = importlib.machinery.SourceFileLoader("standup_client_login", str(_CLIENT))
client = importlib.util.module_from_spec(
    importlib.util.spec_from_loader("standup_client_login", _loader)
)
_loader.exec_module(client)


def test_poll_for_token_handles_pending_then_success(monkeypatch):
    calls = iter(
        [
            {"error": "authorization_pending"},
            {"error": "slow_down", "interval": 1},
            {"access_token": "gh-user-tok"},
        ]
    )
    monkeypatch.setattr(
        client, "_http_json", lambda url, data=None, headers=None: next(calls)
    )
    monkeypatch.setattr(client, "_sleep", lambda s: None)
    tok = client._poll_for_token("cid", "devcode", interval=1, expires_in=900)
    assert tok == "gh-user-tok"


def test_poll_for_token_raises_on_access_denied(monkeypatch):
    monkeypatch.setattr(
        client,
        "_http_json",
        lambda url, data=None, headers=None: {"error": "access_denied"},
    )
    monkeypatch.setattr(client, "_sleep", lambda s: None)
    try:
        client._poll_for_token("cid", "devcode", interval=1, expires_in=900)
        assert False, "expected RuntimeError"
    except RuntimeError:
        pass


def test_poll_for_token_times_out(monkeypatch):
    # Only ever "authorization_pending" and a small expires_in relative to the
    # interval must drive the while-loop past `waited >= expires_in`.
    monkeypatch.setattr(
        client,
        "_http_json",
        lambda url, data=None, headers=None: {"error": "authorization_pending"},
    )
    monkeypatch.setattr(client, "_sleep", lambda s: None)
    with pytest.raises(RuntimeError, match="device login timed out"):
        client._poll_for_token("cid", "devcode", interval=5, expires_in=10)


def test_write_config_preserves_other_lines(tmp_path, monkeypatch):
    cfg = tmp_path / "env"
    cfg.write_text("OTHER=keep\nSTANDUP_URL=old\n")
    monkeypatch.setattr(client, "CONFIG_PATH", cfg)
    client._write_config("https://new.example", "signed-tok")
    text = cfg.read_text()
    assert "OTHER=keep" in text
    assert "STANDUP_URL=https://new.example" in text
    assert "STANDUP_TOKEN=signed-tok" in text
    assert "old" not in text


def test_cmd_login_end_to_end(tmp_path, monkeypatch):
    cfg_file = tmp_path / "env"
    monkeypatch.setattr(client, "CONFIG_PATH", cfg_file)
    monkeypatch.setattr(client, "_sleep", lambda s: None)
    monkeypatch.setattr(client, "_open_browser", lambda url: None)

    def fake_http(url, data=None, json_body=False):
        if url.endswith("/config"):
            return {"github_client_id": "cid"}
        if url == client.DEVICE_CODE_URL:
            return {
                "device_code": "dc",
                "user_code": "WXYZ-1234",
                "verification_uri": "https://github.com/login/device",
                "interval": 1,
                "expires_in": 900,
            }
        if url == client.ACCESS_TOKEN_URL:
            return {"access_token": "gh-user-tok"}
        if url.endswith("/auth/exchange"):
            assert data == {"github_token": "gh-user-tok"}
            return {"token": "signed-tok", "login": "alice", "email": "a@e.com"}
        raise AssertionError(url)

    monkeypatch.setattr(client, "_http_json", fake_http)
    args = argparse.Namespace(url="https://board.example")
    rc = client.cmd_login({}, args)
    assert rc == 0
    text = cfg_file.read_text()
    assert "STANDUP_URL=https://board.example" in text
    assert "STANDUP_TOKEN=signed-tok" in text


def test_cmd_login_without_url_errors(capsys):
    rc = client.cmd_login({}, argparse.Namespace(url=None))
    assert rc == 1
    assert "url" in capsys.readouterr().err.lower()


def test_cmd_login_uses_config_url_when_flag_absent(tmp_path, monkeypatch):
    # --url omitted but STANDUP_URL in cfg → still logs in against that base.
    cfg_file = tmp_path / "env"
    monkeypatch.setattr(client, "CONFIG_PATH", cfg_file)
    monkeypatch.setattr(client, "_sleep", lambda s: None)
    monkeypatch.setattr(client, "_open_browser", lambda url: None)

    def fake_http(url, data=None, json_body=False):
        if url.endswith("/config"):
            assert url.startswith("https://cfg.example")
            return {"github_client_id": "cid"}
        if url == client.DEVICE_CODE_URL:
            return {
                "device_code": "dc",
                "user_code": "AB-CD",
                "verification_uri": "https://gh/device",
                "interval": 1,
                "expires_in": 900,
            }
        if url == client.ACCESS_TOKEN_URL:
            return {"access_token": "t"}
        if url.endswith("/auth/exchange"):
            assert url.startswith("https://cfg.example")
            return {"token": "sig", "login": None, "email": "e@x.com"}
        raise AssertionError(url)

    monkeypatch.setattr(client, "_http_json", fake_http)
    rc = client.cmd_login(
        {"STANDUP_URL": "https://cfg.example"}, argparse.Namespace(url=None)
    )
    assert rc == 0
    assert "STANDUP_TOKEN=sig" in cfg_file.read_text()


def test_cmd_login_reports_error_when_start_fails(monkeypatch, capsys):
    def boom(url, data=None, json_body=False):
        raise ValueError("bad json")

    monkeypatch.setattr(client, "_http_json", boom)
    rc = client.cmd_login({}, argparse.Namespace(url="https://board.example"))
    assert rc == 1
    assert "could not start login" in capsys.readouterr().err


def test_cmd_login_reports_error_when_poll_or_exchange_fails(monkeypatch, capsys):
    monkeypatch.setattr(client, "_sleep", lambda s: None)
    monkeypatch.setattr(client, "_open_browser", lambda url: None)

    def fake_http(url, data=None, json_body=False):
        if url.endswith("/config"):
            return {"github_client_id": "cid"}
        if url == client.DEVICE_CODE_URL:
            return {
                "device_code": "dc",
                "user_code": "AB-CD",
                "verification_uri": "https://gh/device",
                "interval": 1,
                "expires_in": 900,
            }
        if url == client.ACCESS_TOKEN_URL:
            return {"error": "access_denied"}
        raise AssertionError(url)

    monkeypatch.setattr(client, "_http_json", fake_http)
    rc = client.cmd_login({}, argparse.Namespace(url="https://board.example"))
    assert rc == 1
    assert "login failed" in capsys.readouterr().err
