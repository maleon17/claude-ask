"""Claude worker: prompt composition and per-session serialisation."""
import sys
import threading
import types
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# The queue encryption dependency is not installed in the isolated test venv;
# this test only exercises prompt composition, not encryption.
crypto = types.ModuleType("cryptography.fernet")
crypto.Fernet = object
crypto.InvalidToken = Exception
sys.modules.setdefault("cryptography", types.ModuleType("cryptography"))
sys.modules.setdefault("cryptography.fernet", crypto)
import claude_watcher  # noqa: E402


def test_watcher_places_trigger_context_before_the_request(monkeypatch):
    captured = {}
    monkeypatch.setattr(claude_watcher, "load_persona", lambda *_: "persona")
    get_session_id = patch.object(claude_watcher, "get_session_id", return_value="thread")
    set_session_id = patch.object(claude_watcher, "set_session_id")

    def run(system, prompt, on_progress, **kwargs):
        captured.update(system=system, prompt=prompt, kwargs=kwargs)
        return "answer", [], "thread"

    monkeypatch.setattr(claude_watcher, "run_claude_streaming", run)
    with get_session_id as get_session, set_session_id as set_session:
        assert claude_watcher.call_llm(
            "reply naturally", "-100watched", "chat", "request-7",
            chat_context="[id=1]: hello", session_chat_id="-100owner",
        ) == ("answer", [])
    get_session.assert_called_once_with(claude_watcher.DEFAULT_INSTANCE, "-100owner")
    set_session.assert_called_once_with(claude_watcher.DEFAULT_INSTANCE, "-100owner", "thread")
    assert captured["prompt"] == (
        "Контекст текущего чата:\n[id=1]: hello\n\nЗапрос пользователя:\nreply naturally"
    )
    assert captured["kwargs"]["chat_id"] == "-100watched"


def test_watcher_serializes_requests_sharing_a_session_chat_id(monkeypatch):
    """Different observed chats must not concurrently resume one owner session."""
    first_started = threading.Event()
    release = threading.Event()
    entered = []

    def process(*_args, **_kwargs):
        entered.append(True)
        if len(entered) == 1:
            first_started.set()
            assert release.wait(2)

    monkeypatch.setattr(claude_watcher, "_process_request", process)
    first = threading.Thread(
        target=claude_watcher._process_request_serialized,
        args=("first", "question", "-100watched-a", "chat"),
        kwargs={"session_chat_id": "-100owner"},
    )
    second = threading.Thread(
        target=claude_watcher._process_request_serialized,
        args=("second", "question", "-100watched-b", "chat"),
        kwargs={"session_chat_id": "-100owner"},
    )
    first.start()
    assert first_started.wait(1)
    second.start()
    second.join(0.1)
    assert entered == [True]
    release.set()
    first.join(2)
    second.join(2)
    assert entered == [True, True]


# --- .stop: cancelling a request -------------------------------------------------
import json  # noqa: E402
import os  # noqa: E402
import stat  # noqa: E402
import time  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture
def queues(monkeypatch, tmp_path):
    queue_dir, result_dir = tmp_path / "queue", tmp_path / "result"
    queue_dir.mkdir()
    result_dir.mkdir()
    monkeypatch.setattr(claude_watcher, "QUEUE_DIR", str(queue_dir))
    monkeypatch.setattr(claude_watcher, "RESULT_DIR", str(result_dir))
    monkeypatch.setattr(claude_watcher, "_RUNNING", {})
    monkeypatch.setattr(claude_watcher, "_CANCELLED", set())
    return queue_dir, result_dir


def _result(result_dir, req_id):
    return json.loads((result_dir / f"{req_id}.json").read_text())


def test_request_stopped_before_it_started_never_reaches_claude(monkeypatch, queues):
    queue_dir, result_dir = queues
    (queue_dir / "r1.cancel").write_text("{}")
    called = []
    monkeypatch.setattr(claude_watcher, "call_llm", lambda *a, **k: called.append(a) or ("x", []))
    claude_watcher._process_request("r1", "question", "7", "chat")
    assert called == []
    assert _result(result_dir, "r1") == {
        "done": True, "request_id": "r1", "cancelled": True, "answer": "", "thoughts": [],
    }
    assert not (queue_dir / "r1.cancel").exists()


def test_stop_kills_the_running_process_group_and_reports_cancelled(monkeypatch, queues, tmp_path):
    queue_dir, result_dir = queues
    child_pid_file = tmp_path / "child.pid"
    fake_claude = tmp_path / "claude"
    # Stands in for `claude -p`: starts a child of its own (as real tool calls
    # do) and then hangs, as a long turn would.
    fake_claude.write_text(f"#!/bin/sh\nsleep 60 &\necho $! > {child_pid_file}\nwait\n")
    fake_claude.chmod(fake_claude.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(claude_watcher, "CLAUDE_BIN", str(fake_claude))
    monkeypatch.setattr(claude_watcher, "load_persona", lambda *_: "persona")
    monkeypatch.setattr(claude_watcher, "get_session_id", lambda *a: None)
    monkeypatch.setattr(claude_watcher, "set_session_id", lambda *a: None)

    worker = threading.Thread(
        target=claude_watcher._process_request, args=("r2", "question", "7", "chat"),
    )
    worker.start()
    deadline = time.time() + 5
    while time.time() < deadline and not child_pid_file.exists():
        time.sleep(0.05)
    assert child_pid_file.exists(), "the fake claude never started"
    child_pid = int(child_pid_file.read_text())

    (queue_dir / "r2.cancel").write_text("{}")
    claude_watcher._scan_cancel_markers()
    worker.join(8)
    assert not worker.is_alive(), "the request thread did not stop"

    assert _result(result_dir, "r2")["cancelled"] is True
    assert not (queue_dir / "r2.cancel").exists()
    assert claude_watcher._RUNNING == {} and claude_watcher._CANCELLED == set()
    # The tool process `claude` started is gone too, not orphaned.
    deadline = time.time() + 3
    alive = True
    while time.time() < deadline and alive:
        try:
            os.kill(child_pid, 0)
            time.sleep(0.05)
        except ProcessLookupError:
            alive = False
    assert not alive


def test_abandoned_cancel_marker_is_eventually_dropped(queues):
    queue_dir, _ = queues
    marker = queue_dir / "ghost.cancel"
    marker.write_text("{}")
    claude_watcher._scan_cancel_markers(now=time.time())
    assert marker.exists()
    claude_watcher._scan_cancel_markers(now=time.time() + claude_watcher.CANCEL_MARKER_TTL_S + 1)
    assert not marker.exists() and "ghost" not in claude_watcher._CANCELLED


@pytest.fixture
def fake_claude(monkeypatch, tmp_path):
    executable = tmp_path / "fake-claude"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "with open(os.environ['FAKE_CLAUDE_ARGS_PATH'], 'w') as output:\n"
        "    json.dump(sys.argv[1:], output)\n"
        "print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 'new-session'}))\n"
        "for event in json.loads(os.environ['FAKE_CLAUDE_EVENTS']):\n"
        "    print(json.dumps(event))\n"
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    args_path = tmp_path / "args.json"
    saved_sessions = []
    monkeypatch.setattr(claude_watcher, "CLAUDE_BIN", str(executable))
    monkeypatch.setattr(claude_watcher, "load_persona", lambda *_: "persona")
    monkeypatch.setattr(claude_watcher, "get_session_id", lambda *_: None)
    monkeypatch.setattr(claude_watcher, "set_session_id", lambda *args: saved_sessions.append(args))
    monkeypatch.setenv("FAKE_CLAUDE_ARGS_PATH", str(args_path))
    monkeypatch.setenv("FAKE_CLAUDE_EVENTS", json.dumps([{"type": "result", "result": "answer", "is_error": False}]))
    monkeypatch.delenv("JARVIS_LOGIN_HINT", raising=False)
    return args_path, saved_sessions


@pytest.mark.parametrize(
    ("mode", "requester_id", "restricted"),
    [
        ("chat", "trigger:rule-1", True),
        ("chat", "owner-1", False),
        ("classify", "trigger:rule-1", False),
    ],
)
def test_builtin_tools_are_disabled_only_for_trigger_turns(
    monkeypatch, queues, fake_claude, mode, requester_id, restricted,
):
    args_path, _ = fake_claude
    claude_watcher._process_request_serialized(
        "tool-check", "question", "chat-1", mode, requester_id=requester_id,
    )
    args = json.loads(args_path.read_text())
    if restricted:
        assert args[args.index("--tools") + 1] == ""
        assert "--mcp-config" in args
    else:
        assert "--tools" not in args
    if mode == "classify":
        assert "--mcp-config" not in args


@pytest.mark.parametrize("language", claude_watcher.LANGUAGES)
def test_auth_failure_is_localized_and_does_not_save_session(
    monkeypatch, queues, fake_claude, language,
):
    _, saved_sessions = fake_claude
    monkeypatch.setenv("FAKE_CLAUDE_EVENTS", json.dumps([{
        "type": "result", "is_error": True,
        "result": "Failed to authenticate: OAuth token has expired. Run claude auth login.",
    }]))
    monkeypatch.setattr(claude_watcher, "_config_dir", lambda _: "/isolated-instance")
    answer, _ = claude_watcher.call_llm(
        "question", "chat-1", "chat", "auth-check", instance_id="instance-2", language=language,
    )
    assert answer.startswith("[[backend_error:claude_auth]] ")
    assert claude_watcher._text("auth_expired", language, instance_id="instance-2") in answer
    assert claude_watcher._text("auth_config_hint", language) in answer
    assert "Failed to authenticate" not in answer
    assert saved_sessions == []


def test_auth_failure_appends_login_hint_verbatim(monkeypatch, queues, fake_claude):
    monkeypatch.setenv("JARVIS_LOGIN_HINT", "Login via your configured operator command")
    monkeypatch.setenv("FAKE_CLAUDE_EVENTS", json.dumps([{
        "type": "result", "is_error": True, "result": "authentication_error: not logged in",
    }]))
    answer, _ = claude_watcher.call_llm("question", "chat-1", "chat", "auth-hint")
    assert answer.endswith("\nLogin via your configured operator command")
    assert "CLAUDE_CONFIG_DIR" not in answer


def test_auth_error_event_without_an_answer_is_localized(monkeypatch, queues, fake_claude):
    _, saved_sessions = fake_claude
    monkeypatch.setenv("FAKE_CLAUDE_EVENTS", json.dumps([{
        "type": "error", "error": {"message": "OAuth token expired"},
    }]))
    answer, _ = claude_watcher.call_llm("question", "chat-1", "chat", "auth-event")
    assert answer.startswith("[[backend_error:claude_auth]] ")
    assert saved_sessions == []


def test_empty_error_result_is_localized(monkeypatch, queues, fake_claude):
    _, saved_sessions = fake_claude
    monkeypatch.setenv("FAKE_CLAUDE_EVENTS", json.dumps([{
        "type": "result", "is_error": True, "result": "",
        "errors": ["Not logged in; please run /login"],
    }]))
    answer, _ = claude_watcher.call_llm("question", "chat-1", "chat", "auth-empty")
    assert answer.startswith("[[backend_error:claude_auth]] ")
    assert "Not logged in" not in answer
    assert saved_sessions == []


@pytest.mark.parametrize("normal_answer", [
    "To sign in, run claude auth login --claudeai.",
    "Failed to authenticate is the error message you asked about; run claude auth login.",
])
def test_normal_answer_mentioning_login_is_not_intercepted(
    monkeypatch, queues, fake_claude, normal_answer,
):
    _, saved_sessions = fake_claude
    monkeypatch.setenv("FAKE_CLAUDE_EVENTS", json.dumps([{
        "type": "result", "is_error": False, "result": normal_answer,
    }]))
    answer, _ = claude_watcher.call_llm("question", "chat-1", "chat", "normal-answer")
    assert answer == normal_answer
    assert saved_sessions == [(claude_watcher.DEFAULT_INSTANCE, "chat-1", "new-session")]


def test_auth_error_event_does_not_override_later_answer(monkeypatch, queues, fake_claude):
    monkeypatch.setenv("FAKE_CLAUDE_EVENTS", json.dumps([
        {"type": "error", "error": {"message": "not logged in"}},
        {"type": "result", "is_error": False, "result": "The login has been restored."},
    ]))
    answer, _ = claude_watcher.call_llm("question", "chat-1", "chat", "auth-recovered")
    assert answer == "The login has been restored."
