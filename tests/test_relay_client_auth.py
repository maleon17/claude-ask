import asyncio
import json
import sys
import types
from unittest.mock import patch

# The queue encryption dependency is not installed in the isolated test venv;
# this test only exercises prompt composition, not encryption.
crypto = types.ModuleType("cryptography.fernet")
crypto.Fernet = object
crypto.InvalidToken = Exception
sys.modules.setdefault("cryptography", types.ModuleType("cryptography"))
sys.modules.setdefault("cryptography.fernet", crypto)
import claude_watcher
from test_trigger_authorization import claude_ask, make_module


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return b'{"status":"ok","path":"/artifact"}'


def test_client_enqueues_with_bearer_token(monkeypatch):
    """S01: every client relay call carries the configured bearer token."""
    bot = make_module()
    monkeypatch.setattr(claude_ask, "RELAY_TOKEN", "client-secret")
    captured = []
    monkeypatch.setattr(bot, "_relay_open", lambda request, *_: captured.append(request) or _Response())

    assert bot._enqueue("question", "7", "request-7")[0]
    assert captured[0].get_header("Authorization") == "Bearer client-secret"


def test_trigger_context_crosses_the_relay(monkeypatch):
    bot = make_module()
    captured = []
    monkeypatch.setattr(bot, "_relay_open", lambda request, *_: captured.append(request) or _Response())

    assert bot._enqueue("trigger question", "7", "request-7", chat_context="recent messages")[0]
    assert json.loads(captured[0].data)["chat_context"] == "recent messages"


def test_watcher_places_trigger_context_before_the_request(monkeypatch):
    captured = {}
    monkeypatch.setattr(claude_watcher, "load_persona", lambda _: "persona")
    monkeypatch.setattr(claude_watcher, "get_session_id", lambda *_: "thread")
    monkeypatch.setattr(claude_watcher, "set_session_id", lambda *_: None)

    def run(system, prompt, on_progress, **kwargs):
        captured.update(system=system, prompt=prompt, kwargs=kwargs)
        return "answer", [], "thread"

    monkeypatch.setattr(claude_watcher, "run_claude_streaming", run)
    assert claude_watcher.call_llm(
        "reply naturally", "7", "chat", "request-7", chat_context="[id=1]: hello"
    ) == ("answer", [])
    assert captured["prompt"] == (
        "Контекст текущего чата:\n[id=1]: hello\n\nЗапрос пользователя:\nreply naturally"
    )


def test_upload_boundary_is_not_reused_from_payload(monkeypatch):
    """S03: the multipart boundary is regenerated if it occurs in content."""
    bot = make_module()
    captured = []
    monkeypatch.setattr(claude_ask, "RELAY_TOKEN", "client-secret")
    monkeypatch.setattr(claude_ask, "RELAY_OPENER", type("O", (), {"open": lambda _, request, **__: captured.append(request) or _Response()})())
    monkeypatch.setattr(claude_ask.secrets, "token_hex", lambda _: "collision")

    with patch.object(claude_ask.secrets, "token_hex", side_effect=["collision", "safe-boundary"]):
        assert asyncio.run(bot._upload_to_lightrag(b"collision in file", "proof.bin")) == "/artifact"
    content_type = captured[0].get_header("Content-type")
    boundary = content_type.rsplit("=", 1)[1].encode()
    file_bytes = captured[0].data.split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n--", 1)[0]
    assert boundary not in file_bytes
