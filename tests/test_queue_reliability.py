import importlib.util
import json
import sys
import types
from pathlib import Path


def load_queue():
    # Queue reliability paths do not use encryption; keep the test independent
    # from the service virtualenv.
    crypto = types.ModuleType("cryptography.fernet")
    crypto.Fernet = object
    sys.modules.setdefault("cryptography", types.ModuleType("cryptography"))
    sys.modules["cryptography.fernet"] = crypto
    path = Path(__file__).parents[1] / "cmd_queue.py"
    spec = importlib.util.spec_from_file_location("queue_reliability", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_atomic_json_fsyncs_file_and_directory(monkeypatch, tmp_path):
    q = load_queue()
    calls = []
    monkeypatch.setattr(q.os, "fsync", lambda fd: calls.append(fd))
    q._atomic_json(str(tmp_path / "item.json"), {"complete": True})
    assert json.loads((tmp_path / "item.json").read_text()) == {"complete": True}
    assert len(calls) >= 2


def test_tool_claim_is_leased_and_result_ack_removes_claim(monkeypatch, tmp_path):
    q = load_queue()
    monkeypatch.setattr(q, "TOOL_QUEUE_DIR", str(tmp_path / "queue"))
    monkeypatch.setattr(q, "TOOL_RESULT_DIR", str(tmp_path / "result"))
    monkeypatch.setattr(q, "TOOL_CLAIM_LEASE_S", 10)
    (tmp_path / "queue").mkdir()
    q._atomic_json(str(tmp_path / "queue" / "r1.json"), {"instance_id": "i", "tool": "send"})
    item = q._pop_pending_tool_call("i", now=100)
    assert item["request_id"] == "r1"
    assert not (tmp_path / "queue" / "r1.json").exists()
    assert (tmp_path / "queue" / "r1.claimed").exists()
    assert q._pop_pending_tool_call("i", now=101) is None
    replay = q._pop_pending_tool_call("i", now=111)
    assert replay["request_id"] == "r1"
    q._ack_tool_result("r1", "ok")
    assert not (tmp_path / "queue" / "r1.claimed").exists()
    assert json.loads((tmp_path / "result" / "r1.json").read_text())["result"] == "ok"


def test_expired_tool_is_never_claimed(monkeypatch, tmp_path):
    q = load_queue()
    monkeypatch.setattr(q, "TOOL_QUEUE_DIR", str(tmp_path))
    q._atomic_json(str(tmp_path / "expired.json"), {"instance_id": "i", "tool": "send", "expires_at": 10})
    assert q._pop_pending_tool_call("i", now=11) is None
    assert json.loads((tmp_path / "expired.json").read_text())["cancelled"] is True
