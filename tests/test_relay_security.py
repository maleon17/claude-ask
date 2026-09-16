import hashlib
import importlib.util
import json
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _load_relay():
    name = "_cmd_queue_security_test"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, ROOT / "cmd_queue.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@contextmanager
def relay_server(tmp_path):
    relay = _load_relay()
    token_a, token_b = "token-for-andrey", "token-for-anatoly"
    relay.RELAY_TOKENS = {
        hashlib.sha256(token_a.encode()).hexdigest(): "andrey",
        hashlib.sha256(token_b.encode()).hexdigest(): "anatoly",
    }
    relay.RELAY_OWNER_IDS = {"andrey": "101", "anatoly": "202"}
    relay.ASK_QUEUE_DIR = str(tmp_path / "ask")
    relay.ASK_RESULT_DIR = str(tmp_path / "result")
    relay.XASK_QUEUE_DIR = str(tmp_path / "xask")
    relay.XASK_RESULT_DIR = str(tmp_path / "xresult")
    relay.XASK_RESET_DIR = str(tmp_path / "xreset")
    relay.TOOL_QUEUE_DIR = str(tmp_path / "tool")
    relay.TOOL_RESULT_DIR = str(tmp_path / "tool-result")
    relay.RELAY_STATE_DIR = str(tmp_path / "relay-state")
    relay.ARTIFACT_DIR = str(tmp_path / "artifacts")
    for directory in (
        relay.ASK_QUEUE_DIR, relay.ASK_RESULT_DIR, relay.XASK_QUEUE_DIR,
        relay.XASK_RESULT_DIR, relay.XASK_RESET_DIR, relay.TOOL_QUEUE_DIR,
        relay.TOOL_RESULT_DIR, relay.RELAY_STATE_DIR, relay.ARTIFACT_DIR,
    ):
        Path(directory).mkdir(parents=True, exist_ok=True)
    server = relay.ThreadingHTTPServer(("127.0.0.1", 0), relay.Queue)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield relay, f"http://127.0.0.1:{server.server_port}", token_a, token_b
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def _request(url, *, token=None, data=None, content_type="application/json"):
    headers = {"Content-Type": content_type} if data is not None else {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers)
    # Userbot-module tests install a tailnet proxy globally at import time;
    # relay integration tests must always use their own loopback server.
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=3) as response:
        return response.status, response.read()


def _status(url, **kwargs):
    try:
        return _request(url, **kwargs)[0]
    except urllib.error.HTTPError as error:
        return error.code


def test_relay_requires_bearer_and_scopes_instance_resources(tmp_path):
    """S01/S02: a caller cannot select another instance or read its artifact."""
    with relay_server(tmp_path) as (relay, base, token_a, token_b):
        body = json.dumps({
            "request_id": "request-a", "question": "hello",
            "instance_id": "andrey", "requester_id": "101",
        }).encode()
        assert _status(f"{base}/ask", data=body) == 401
        assert _status(f"{base}/ask", token=token_a, data=json.dumps({
            "request_id": "wrong-instance", "question": "hello",
            "instance_id": "anatoly",
        }).encode()) == 403
        assert _status(f"{base}/ask", token=token_a, data=body) == 200
        queued = json.loads((Path(relay.ASK_QUEUE_DIR) / "request-a.json").read_text())
        assert queued["instance_id"] == "andrey"
        assert queued["owner_authorized"] is True
        assert _status(f"{base}/ask", token=token_b, data=json.dumps({
            "request_id": "request-a", "question": "collision", "instance_id": "anatoly",
        }).encode()) == 403

        non_owner = json.dumps({
            "request_id": "request-non-owner", "question": "hello",
            "instance_id": "andrey", "requester_id": "202", "owner_authorized": True,
        }).encode()
        assert _status(f"{base}/ask", token=token_a, data=non_owner) == 200
        queued = json.loads((Path(relay.ASK_QUEUE_DIR) / "request-non-owner.json").read_text())
        assert queued["owner_authorized"] is False

        result = Path(relay.ASK_RESULT_DIR) / "request-a.json"
        result.write_text(json.dumps({"done": True, "answer": "private"}))
        assert _status(f"{base}/ask?request_id=request-a", token=token_b) == 403

        artifact = Path(relay.ARTIFACT_DIR) / "andrey" / "report.bin"
        artifact.parent.mkdir(parents=True)
        artifact.write_bytes(b"private artifact")
        assert _status(
            f"{base}/download?path={urllib.parse.quote(str(artifact))}", token=token_a,
        ) == 404
        relay._register_artifact("andrey", str(artifact))
        assert _status(
            f"{base}/download?path={urllib.parse.quote(str(artifact))}", token=token_b,
        ) == 403


def test_relay_configuration_rejects_missing_or_empty_token_file(monkeypatch, tmp_path):
    """S01: the service cannot accidentally start in unauthenticated mode."""
    relay = _load_relay()
    monkeypatch.delenv("JARVIS_RELAY_TOKENS_FILE", raising=False)
    monkeypatch.setenv("JARVIS_RELAY_OWNERS_FILE", str(tmp_path / "owners.json"))
    (tmp_path / "owners.json").write_text('{"andrey": "101"}')
    with pytest.raises(RuntimeError, match="JARVIS_RELAY_TOKENS_FILE"):
        relay._load_relay_config()


def test_upload_preserves_binary_multipart_payload(tmp_path):
    """S03: MIME parsing must not split arbitrary file bytes at CRLFCRLF."""
    with relay_server(tmp_path) as (relay, base, token_a, _):
        boundary = "boundary-for-regression"
        payload = b"start\r\n\r\n\x00binary\r\n--not-the-boundary\xfftail"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="proof.bin"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode() + payload + f"\r\n--{boundary}--".encode()
        status, raw = _request(
            f"{base}/upload", token=token_a, data=body,
            content_type=f"multipart/form-data; boundary={boundary}",
        )
        assert status == 200
        saved = json.loads(raw)["path"]
        assert Path(saved).read_bytes() == payload


def test_persona_post_sends_exactly_one_response_on_instance_mismatch(tmp_path):
    """Regression: a mismatched instance_id in /persona used to trigger a
    second send_response() on the same socket on top of the 403 that
    _body_instance_is_current() already sent -- corrupting the HTTP
    response framing. Every other _body_instance_is_current() call site
    just returns; /persona must behave the same way."""
    with relay_server(tmp_path) as (relay, base, token_a, _):
        body = json.dumps({
            "instance_id": "not-andrey", "persona": "hi",
        }).encode()
        try:
            _request(f"{base}/persona", token=token_a, data=body)
            raise AssertionError("expected HTTPError")
        except urllib.error.HTTPError as error:
            assert error.code == 403
            payload = json.loads(error.read())
            assert payload["status"] == "error"

        # The connection must be left in a clean state for the next request
        # -- a stray second write would show up as extra unread bytes.
        status2, raw2 = _request(
            f"{base}/persona?instance_id=andrey", token=token_a, data=None,
        )
        assert status2 == 200
        assert json.loads(raw2)["status"] == "ok"


def test_tool_call_requires_a_same_instance_parent_ask_request(tmp_path):
    """S01: a tool call must be linked to an /ask or /xask request the SAME
    authenticated instance actually enqueued -- not an arbitrary or
    cross-instance parent_request_id."""
    with relay_server(tmp_path) as (relay, base, token_a, token_b):
        # No parent at all.
        orphan = json.dumps({
            "request_id": "tool-orphan", "instance_id": "andrey",
            "parent_request_id": "no-such-request", "chat_id": "1",
            "tool": "send_message", "args": {},
        }).encode()
        assert _status(f"{base}/tool_call", token=token_a, data=orphan) == 403

        # Enqueue a real /ask as andrey, then try to anchor a tool call to it
        # while authenticated as anatoly.
        ask_body = json.dumps({
            "request_id": "ask-parent", "question": "hi",
            "instance_id": "andrey", "requester_id": "101",
        }).encode()
        assert _status(f"{base}/ask", token=token_a, data=ask_body) == 200
        cross_instance = json.dumps({
            "request_id": "tool-cross", "instance_id": "anatoly",
            "parent_request_id": "ask-parent", "chat_id": "1",
            "tool": "send_message", "args": {},
        }).encode()
        assert _status(f"{base}/tool_call", token=token_b, data=cross_instance) == 403

        # Same instance, correct parent: accepted, and requester_id/
        # owner_authorized come from the parent record, not the tool call body.
        linked = json.dumps({
            "request_id": "tool-linked", "instance_id": "andrey",
            "parent_request_id": "ask-parent", "chat_id": "1",
            "tool": "send_message", "args": {}, "requester_id": "999",
        }).encode()
        assert _status(f"{base}/tool_call", token=token_a, data=linked) == 200
        stored = json.loads((Path(relay.TOOL_QUEUE_DIR) / "tool-linked.json").read_text())
        assert stored["requester_id"] == "101"
        assert stored["owner_authorized"] is True
