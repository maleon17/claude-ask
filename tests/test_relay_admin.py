"""Relay credential management and private runtime state."""
import hashlib
import importlib.util
import json
import os
import stat
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _load_relay(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("JARVIS_RELAY_CONFIG_DIR", str(tmp_path / "config"))
    for name in ("JARVIS_RELAY_TOKENS_FILE", "JARVIS_RELAY_OWNERS_FILE"):
        monkeypatch.delenv(name, raising=False)
    name = "_cmd_queue_admin_test"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, ROOT / "cmd_queue.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def _status(url, token):
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=3) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


def test_runtime_state_is_private_and_outside_tmp(monkeypatch, tmp_path):
    relay = _load_relay(monkeypatch, tmp_path)
    base = tmp_path / "state"
    assert _mode(base) == 0o700
    for directory in (
        relay.ASK_QUEUE_DIR, relay.ASK_RESULT_DIR, relay.XASK_QUEUE_DIR,
        relay.XASK_RESULT_DIR, relay.XASK_RESET_DIR, relay.TOOL_QUEUE_DIR,
        relay.TOOL_RESULT_DIR, relay.RELAY_STATE_DIR, relay.ARTIFACT_DIR,
    ):
        assert Path(directory).parent == base
        assert _mode(directory) == 0o700


def test_explicit_directory_override_still_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_ASK_QUEUE_DIR", str(tmp_path / "elsewhere"))
    relay = _load_relay(monkeypatch, tmp_path)
    assert relay.ASK_QUEUE_DIR == str(tmp_path / "elsewhere")


def test_cli_registers_an_instance_the_relay_user_can_read(monkeypatch, tmp_path, capsys):
    relay = _load_relay(monkeypatch, tmp_path)
    assert relay.main(["--set-owner", "mybot", "111"]) == 0
    assert relay.main(["--add-token", "mybot"]) == 0
    token = capsys.readouterr().out.strip()
    tokens_file = tmp_path / "config" / "relay-tokens.json"
    owners_file = tmp_path / "config" / "relay-owners.json"
    assert _mode(tokens_file) == 0o600 and _mode(owners_file) == 0o600
    assert _mode(tmp_path / "config") == 0o700
    assert os.stat(tokens_file).st_uid == os.geteuid()
    stored = json.loads(tokens_file.read_text())
    assert stored == {hashlib.sha256(token.encode()).hexdigest(): "mybot"}
    assert token not in tokens_file.read_text()
    assert json.loads(owners_file.read_text()) == {"mybot": 111}
    assert relay.main(["--check-config"]) == 0


def test_cli_rejects_bad_input(monkeypatch, tmp_path):
    relay = _load_relay(monkeypatch, tmp_path)
    for argv in (
        ["--set-owner", "mybot", "not-a-number"],
        ["--set-owner", "../evil", "1"],
        ["--add-token", "bad id"],
        ["--add-token"],
        ["--unknown"],
    ):
        with pytest.raises(RuntimeError):
            relay.main(argv)


def test_revoke_and_list_never_expose_tokens(monkeypatch, tmp_path, capsys):
    relay = _load_relay(monkeypatch, tmp_path)
    relay.main(["--set-owner", "mybot", "111"])
    relay.main(["--set-owner", "other", "222"])
    relay.main(["--add-token", "mybot"])
    relay.main(["--add-token", "mybot"])
    relay.main(["--add-token", "other"])
    tokens = capsys.readouterr().out.split()
    relay.main(["--list"])
    listing = capsys.readouterr().out
    assert "mybot\towner=111\ttokens=2" in listing
    assert "other\towner=222\ttokens=1" in listing
    assert not any(token in listing for token in tokens)
    relay.main(["--revoke-tokens", "mybot"])
    assert capsys.readouterr().out.strip() == "2"
    remaining = json.loads((tmp_path / "config" / "relay-tokens.json").read_text())
    assert list(remaining.values()) == ["other"]


def test_running_relay_picks_up_new_and_revoked_tokens(monkeypatch, tmp_path, capsys):
    """Adding an instance must not require a restart that drops live requests."""
    relay = _load_relay(monkeypatch, tmp_path)
    relay.main(["--set-owner", "first", "111"])
    relay.main(["--add-token", "first"])
    first = capsys.readouterr().out.strip()
    relay._load_relay_config()
    server = relay.ThreadingHTTPServer(("127.0.0.1", 0), relay.Queue)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/tool_call_pending"
    try:
        assert _status(url, first) == 200
        assert _status(url, "not-registered-yet") == 401
        relay.main(["--set-owner", "second", "222"])
        relay.main(["--add-token", "second"])
        second = capsys.readouterr().out.strip()
        assert _status(url, second) == 200
        relay.main(["--revoke-tokens", "first"])
        capsys.readouterr()
        assert _status(url, first) == 401
        assert _status(url, second) == 200
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_broken_edit_keeps_the_loaded_maps(monkeypatch, tmp_path, capsys):
    relay = _load_relay(monkeypatch, tmp_path)
    relay.main(["--set-owner", "first", "111"])
    relay.main(["--add-token", "first"])
    capsys.readouterr()
    relay._load_relay_config()
    before = dict(relay.RELAY_TOKENS)
    tokens_file = tmp_path / "config" / "relay-tokens.json"
    tokens_file.write_text("{ not json")
    os.utime(tokens_file, ns=(1, 1))
    relay._reload_relay_config_if_changed()
    assert relay.RELAY_TOKENS == before


def test_legacy_unscoped_endpoints_are_gone(monkeypatch, tmp_path, capsys):
    relay = _load_relay(monkeypatch, tmp_path)
    relay.main(["--set-owner", "first", "111"])
    relay.main(["--add-token", "first"])
    token = capsys.readouterr().out.strip()
    relay._load_relay_config()
    server = relay.ThreadingHTTPServer(("127.0.0.1", 0), relay.Queue)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        for path in ("/cmd", "/result", "/ocr", "/no-such-endpoint"):
            assert _status(base + path, token) == 404
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_worker_env_keeps_one_token_per_instance(monkeypatch, tmp_path, capsys):
    relay = _load_relay(monkeypatch, tmp_path)
    env_file = tmp_path / "config" / "relay-token-claude.env"
    relay.main(["--set-owner", "first", "111"])
    relay.main(["--set-owner", "second", "222"])
    relay.main(["--add-token", "first", "--worker-env", str(env_file)])
    first = capsys.readouterr().out.strip()
    env_file.write_text("JARVIS_RELAY_TOKEN=legacy\nOTHER=kept\n" + env_file.read_text())
    relay.main(["--add-token", "second", "--worker-env", str(env_file)])
    second = capsys.readouterr().out.strip()
    assert _mode(env_file) == 0o600
    lines = env_file.read_text().splitlines()
    assert "OTHER=kept" in lines and not any(l.startswith("JARVIS_RELAY_TOKEN=") for l in lines)
    (mapping,) = [l for l in lines if l.startswith("JARVIS_RELAY_TOKENS_JSON=")]
    assert json.loads(mapping.split("=", 1)[1]) == {"first": first, "second": second}
