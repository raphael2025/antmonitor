"""The Windows/terminal entry point consumes the same AMC1 packet as the web."""
import base64
import io
import json
from pathlib import Path
import socket
import warnings

import pytest

import cloud_connect as cli


PAYLOAD = {"u": "https://cloud.example.test", "t": "new-test-token", "f": "ab" * 32,
           "s": "new-site", "n": "云海 二期", "y": "hydro"}


def connection_code(**changes):
    payload = dict(PAYLOAD, **changes)
    return "AMC1-" + base64.urlsafe_b64encode(
        json.dumps(payload, ensure_ascii=False).encode()).decode().rstrip("=")


class Terminal:
    def __init__(self, *answers):
        self.answers = iter(answers)
        self.prompts = []
        self.messages = []

    def ask(self, prompt, secret=False):
        self.prompts.append((prompt, secret))
        return next(self.answers)

    def write(self, message):
        self.messages.append(message)


@pytest.fixture
def state(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    config = tmp_path / "config.yaml"
    identity = tmp_path / "cloud_site_id.txt"
    database = tmp_path / "miner_monitor.db"
    original = {"max_pps": 200, "preserve": {"nested": True}, "cloud": {
        "enabled": True, "url": "https://old.example.test", "token": "old-secret",
        "site_name": "Existing site", "site_type": "mixed", "site_id": "site-old",
        "interval": 91, "customer_hours": 24, "custom_setting": "keep"}}
    settings.write_text(json.dumps(original), encoding="utf-8")
    config.write_text("cloud:\n  site_name: YAML name\n  site_type: air\n", encoding="utf-8")
    database.write_bytes(b"Existing database must not be touched")
    monkeypatch.setattr(cli.appconfig, "CONFIG_FILE", str(config))
    monkeypatch.setattr(cli.appconfig, "SETTINGS_FILE", str(settings))
    monkeypatch.setattr(cli.cloud_report, "_ID_FILE", str(identity))
    monkeypatch.setattr(cli.cloud_report, "test", lambda cloud, sid: (True, "passed"))

    def no_identity_write(*args, **kwargs):
        pytest.fail("testing the connection must not create an identity file")

    monkeypatch.setattr(cli.cloud_report, "site_id", no_identity_write)
    return settings, config, identity, database, original


def test_complete_packet_needs_one_hidden_paste_preserves_settings_and_database(state, capsys):
    settings, _config, identity, database, original = state
    terminal = Terminal(connection_code())
    assert cli.configure("", terminal=terminal) == 0
    assert len(terminal.prompts) == 1
    assert terminal.prompts[0][1] is True
    saved = json.loads(settings.read_text(encoding="utf-8"))
    cloud = saved["cloud"]
    assert (cloud["url"], cloud["token"], cloud["site_id"], cloud["site_name"], cloud["site_type"]) == (
        PAYLOAD["u"], PAYLOAD["t"], PAYLOAD["s"], PAYLOAD["n"], PAYLOAD["y"])
    assert cloud["tls_fingerprint"] == PAYLOAD["f"]
    assert cloud["interval"] == 91
    assert cloud["custom_setting"] == "keep"
    assert saved["max_pps"] == original["max_pps"]
    assert saved["preserve"] == original["preserve"]
    assert database.read_bytes() == b"Existing database must not be touched"
    assert not identity.exists()
    output = capsys.readouterr().out + "\n".join(terminal.messages)
    assert PAYLOAD["u"] in output
    assert PAYLOAD["s"] in output
    assert PAYLOAD["t"] not in output
    assert connection_code() not in output


@pytest.mark.parametrize("value", [PAYLOAD["t"], connection_code(u=None)])
def test_token_without_address_never_tests_or_changes_existing_binding(value, state, monkeypatch, capsys):
    before = state[0].read_bytes()

    def unexpected_ping(*args):
        pytest.fail("a token without an address cannot be used for one-paste binding")

    monkeypatch.setattr(cli.cloud_report, "test", unexpected_ping)
    assert cli.configure(value) == 1
    assert state[0].read_bytes() == before
    assert not state[2].exists()
    output = capsys.readouterr().out
    assert "地址" in output
    assert "未修改" in output
    assert PAYLOAD["t"] not in output


def test_legacy_packet_keeps_existing_identity_and_asks_only_optional_defaults(state):
    terminal = Terminal("", "")
    assert cli.configure(connection_code(s="", n="", y=None), terminal=terminal) == 0
    cloud = json.loads(state[0].read_text(encoding="utf-8"))["cloud"]
    assert (cloud["site_id"], cloud["site_name"], cloud["site_type"]) == (
        "site-old", "Existing site", "mixed")
    assert len(terminal.prompts) == 2
    assert not any(secret for _, secret in terminal.prompts)


def test_failed_ping_keeps_original_binding_and_hides_credentials(state, monkeypatch, capsys):
    before = state[0].read_bytes()
    monkeypatch.setattr(cli.cloud_report, "test", lambda cloud, sid: (False, "bad " + PAYLOAD["t"]))
    assert cli.configure(connection_code()) == 1
    assert state[0].read_bytes() == before
    assert not state[2].exists()
    output = capsys.readouterr().out
    assert "未修改" in output
    assert PAYLOAD["t"] not in output
    assert connection_code() not in output


def test_concurrent_cloud_edit_during_ping_is_preserved(state, monkeypatch):
    settings = state[0]

    def ping(cloud, sid):
        saved = json.loads(settings.read_text(encoding="utf-8"))
        saved["cloud"]["token"] = "other-admin-token"
        settings.write_text(json.dumps(saved), encoding="utf-8")
        return True, "passed"

    monkeypatch.setattr(cli.cloud_report, "test", ping)
    assert cli.configure(connection_code()) == 1
    assert json.loads(settings.read_text(encoding="utf-8"))["cloud"]["token"] == "other-admin-token"


@pytest.mark.parametrize("changes", [
    {"u": "https://user:secret@cloud.example.test"},
    {"u": "https://cloud.example.test?token=secret"},
    {"u": "https://cloud.example.test#fragment"},
    {"u": "http://public.example.test"},
    {"u": "https://cloud.example.test:bad"},
    {"u": "https://cloud .example.test"}, {"u": "https://cloud.example.test/a b"},
    {"t": "test\ntoken"}, {"s": 42}, {"s": " padded "}, {"n": "site\nname"},
])
def test_invalid_packets_do_not_test_or_write(changes, state, monkeypatch):
    before = state[0].read_bytes()

    def unexpected_ping(*args):
        pytest.fail("invalid credentials must not be sent")

    monkeypatch.setattr(cli.cloud_report, "test", unexpected_ping)
    assert cli.configure(connection_code(**changes)) == 1
    assert state[0].read_bytes() == before


def test_stdin_uses_same_packet_and_explains_restart_without_leaking_it(state, monkeypatch, capsys):
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(connection_code() + "\n"))
    assert cli.main(["--stdin"]) == 0
    output = capsys.readouterr().out
    assert "重启现有监控服务使新绑定生效" in output
    assert PAYLOAD["t"] not in output
    assert "AMC1-" not in output


def test_malformed_config_is_kept_and_parser_does_not_echo_secret(state, capsys):
    before = state[0].read_bytes()
    state[1].write_text('cloud: [token: "do-not-echo-this-secret', encoding="utf-8")
    assert cli.configure(connection_code()) == 1
    assert state[0].read_bytes() == before
    output = capsys.readouterr().out
    assert "config.yaml 格式异常" in output
    assert "do-not-echo-this-secret" not in output


def test_hidden_input_never_falls_back_to_echoing_a_secret(monkeypatch):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)

    def cannot_hide(prompt):
        warnings.warn("No hidden console", cli.getpass.GetPassWarning)
        pytest.fail("getpass must not continue into echoing input")

    monkeypatch.setattr(cli.getpass, "getpass", cannot_hide)
    with pytest.raises(cli.SetupError, match="无法隐藏输入"):
        cli.Terminal().ask("Paste", secret=True)


def test_running_monitor_is_never_killed_or_started_twice(state, monkeypatch, capsys):
    monkeypatch.setattr(cli, "IS_WINDOWS", True)

    def unexpected_start(*args):
        pytest.fail("an occupied monitor port must not start another instance")

    monkeypatch.setattr(cli.os, "startfile", unexpected_start, raising=False)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        state[1].write_text("server:\n  host: 127.0.0.1\n  port: " +
                            str(listener.getsockname()[1]) + "\n", encoding="utf-8")
        assert cli.start_monitor() == 20
    output = capsys.readouterr().out
    assert "配置已保存" in output
    assert "原监控窗口" in output


def test_free_port_opens_original_run_bat_without_new_server_logic(state, monkeypatch, capsys):
    monkeypatch.setattr(cli, "IS_WINDOWS", True)
    opened = []
    monkeypatch.setattr(cli.os, "startfile", opened.append, raising=False)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    state[1].write_text("server:\n  host: 127.0.0.1\n  port: " + str(port) + "\n", encoding="utf-8")
    assert cli.start_monitor() == 0
    assert opened == [str(Path(cli.__file__).resolve().with_name("run.bat"))]
    assert "真实上报结果" in capsys.readouterr().out


def test_batch_entry_uses_installed_environment_and_never_passes_code_in_arguments():
    text = Path(cli.__file__).with_name("connect-cloud.bat").read_bytes()
    assert text.isascii()
    assert b'".venv\\Scripts\\python.exe" -X utf8 "cloud_connect.py" --start' in text
    assert b"%1" not in text
    assert b"%*" not in text
    assert b"taskkill" not in text.lower()
    assert b"\r\n" in text
