import asyncio
from unittest.mock import patch

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
