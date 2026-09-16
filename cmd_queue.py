#!/usr/bin/env python3
"""Command + Ask + OCR queue for jarvis-ask."""

import json
import os
import re
import time
import base64
import hashlib
import hmac
import secrets
import socketserver
import sys
import tempfile
import threading
import urllib.request
import urllib.parse
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler

from cryptography.fernet import Fernet

# Mirrors claude_watcher.py's _key_file/_get_key/_encrypt/_decrypt/
# _sessions_file exactly (duplicated, not imported -- separate processes).
# See that file for the full rationale/caveats; short version: this is a
# casual "don't read as a plain chat_id->session_id map at a glance"
# measure, not a defense against someone with root on this box.
_JARVIS_ASK_DIR = os.environ.get("JARVIS_ASK_DIR", os.path.dirname(os.path.abspath(__file__)))


def _session_key_file(instance_id):
    return os.path.join(_JARVIS_ASK_DIR, f".session_key_{instance_id}")


def _get_session_key(instance_id):
    path = _session_key_file(instance_id)
    with open(path, "rb") as f:
        return f.read().strip()


def _sessions_enc_file(instance_id):
    # No special case for "andrey" -- mirrors claude_watcher.py's
    # _sessions_file after the 2026-08-04 naming-symmetry fix.
    return os.path.join(_JARVIS_ASK_DIR, f"ask_sessions_{instance_id}.enc")

OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY", "")
QUEUE_FILE = "/tmp/jarvisask_cmd_queue.json"
RESULT_FILE = "/tmp/jarvisask_cmd_result.json"
# Per-request_id files (not single shared files) -- claude_watcher.py now
# processes multiple .ask calls concurrently (one thread per chat), so a
# single shared queue/result file would let concurrent requests clobber
# each other's state.
ASK_QUEUE_DIR = os.environ.get("JARVIS_ASK_QUEUE_DIR", "/tmp/jarvisask_ask_queue/")
ASK_RESULT_DIR = os.environ.get("JARVIS_ASK_RESULT_DIR", "/tmp/jarvisask_ask_result/")
os.makedirs(ASK_QUEUE_DIR, exist_ok=True)
os.makedirs(ASK_RESULT_DIR, exist_ok=True)
# CodexAsk uses a separate queue so Claude and Codex workers can never race
# to claim each other's requests. The HTTP server remains shared because it
# is transport infrastructure, not a model backend.
XASK_QUEUE_DIR = os.environ.get("JARVIS_XASK_QUEUE_DIR", "/tmp/jarvisask_xask_queue/")
XASK_RESULT_DIR = os.environ.get("JARVIS_XASK_RESULT_DIR", "/tmp/jarvisask_xask_result/")
XASK_RESET_DIR = os.environ.get("JARVIS_XASK_RESET_DIR", "/tmp/jarvisask_xask_reset/")
for _path in (XASK_QUEUE_DIR, XASK_RESULT_DIR, XASK_RESET_DIR):
    os.makedirs(_path, exist_ok=True)

# Real tool-call relay (2026-08-11), opposite direction from /ask: a local
# MCP server (mcp_group_tools.py, spawned by `claude -p --mcp-config=...`
# on THIS host) enqueues a tool call here, and the REMOTE userbot host
# (claude_ask.py's tool_call_watcher loop, the only place with a live
# Telethon session) polls /tool_call_pending for it, executes the real
# action, and posts the result to /tool_call_result. Per-request_id files,
# same reasoning as ASK_QUEUE_DIR/ASK_RESULT_DIR -- concurrent tool calls
# (e.g. two different chats both mid-.ask) must not clobber each other.
TOOL_QUEUE_DIR = os.environ.get("JARVIS_TOOL_QUEUE_DIR", "/tmp/jarvisask_tool_queue/")
TOOL_RESULT_DIR = os.environ.get("JARVIS_TOOL_RESULT_DIR", "/tmp/jarvisask_tool_result/")
os.makedirs(TOOL_QUEUE_DIR, exist_ok=True)
os.makedirs(TOOL_RESULT_DIR, exist_ok=True)
_TOOL_QUEUE_LOCK = threading.Lock()
TOOL_CLAIM_LEASE_S = float(os.environ.get("JARVIS_TOOL_CLAIM_LEASE_S", "45"))
TOOL_TIMEOUT_S = float(os.environ.get("JARVIS_TOOL_TIMEOUT_S", "30"))
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_SAFE_INSTANCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
MAX_UPLOAD_BYTES = 50 * 1024 * 1024

# Authentication is intentionally loaded only by the relay entry point.  The
# module is also imported by isolated tests, which provide these maps directly.
# The token file contains only {sha256(token): instance_id}; a bearer token is
# never written to this repository or to a queue item.
RELAY_TOKENS = {}
RELAY_OWNER_IDS = {}
RELAY_STATE_DIR = os.environ.get("JARVIS_RELAY_STATE_DIR", "/tmp/jarvisask_relay_state")
ARTIFACT_DIR = os.environ.get("JARVIS_RELAY_ARTIFACT_DIR", "/tmp/jarvisask_artifacts")


def _load_mapping(path, label):
    if not path:
        raise RuntimeError(f"{label}: не задан путь к файлу")
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"{label}: не удалось прочитать {path}: {exc}") from exc
    if not isinstance(data, dict) or not data:
        raise RuntimeError(f"{label}: файл {path} пуст или имеет неверный JSON-формат")
    return data


def _load_relay_config():
    global RELAY_TOKENS, RELAY_OWNER_IDS
    tokens = _load_mapping(os.environ.get("JARVIS_RELAY_TOKENS_FILE"), "JARVIS_RELAY_TOKENS_FILE")
    owners = _load_mapping(os.environ.get("JARVIS_RELAY_OWNERS_FILE"), "JARVIS_RELAY_OWNERS_FILE")
    checked_tokens = {}
    for digest, instance_id in tokens.items():
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise RuntimeError("JARVIS_RELAY_TOKENS_FILE: ключи должны быть sha256(token) в hex")
        if not _safe_instance_id(instance_id):
            raise RuntimeError("JARVIS_RELAY_TOKENS_FILE: неверный instance_id")
        if instance_id not in owners or not str(owners[instance_id]).strip():
            raise RuntimeError(f"JARVIS_RELAY_OWNERS_FILE: нет owner id для instance {instance_id!r}")
        checked_tokens[digest] = instance_id
    RELAY_TOKENS = checked_tokens
    RELAY_OWNER_IDS = {str(key): str(value) for key, value in owners.items()}


def _atomic_json(path, data):
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _manifest_path(kind, request_id):
    return os.path.join(RELAY_STATE_DIR, "requests", kind, request_id + ".json")


def _record_request(kind, request_id, instance_id, requester_id=None):
    _atomic_json(_manifest_path(kind, request_id), {
        "instance_id": instance_id,
        "requester_id": "" if requester_id is None else str(requester_id),
        # This is deliberately server-derived.  JSON flags from a client are
        # ignored; the userbot remains the trusted source of requester_id.
        "owner_authorized": hmac.compare_digest(
            "" if requester_id is None else str(requester_id),
            str(RELAY_OWNER_IDS.get(instance_id, "")),
        ),
    })


def _request_record(kind, request_id):
    try:
        with open(_manifest_path(kind, request_id), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _artifact_path(instance_id, path):
    key = hashlib.sha256(path.encode("utf-8", "surrogateescape")).hexdigest()
    return os.path.join(RELAY_STATE_DIR, "artifacts", instance_id, key + ".json")


def _register_artifact(instance_id, path):
    resolved = os.path.realpath(path)
    if not os.path.isabs(resolved) or not os.path.isfile(resolved):
        return False
    _atomic_json(_artifact_path(instance_id, resolved), {"path": resolved})
    return True


def _artifact_registered(instance_id, path):
    resolved = os.path.realpath(path)
    try:
        with open(_artifact_path(instance_id, resolved), encoding="utf-8") as handle:
            record = json.load(handle)
    except (OSError, ValueError, TypeError):
        return False
    return isinstance(record, dict) and hmac.compare_digest(str(record.get("path", "")), resolved)


def _safe_request_id(value):
    return isinstance(value, str) and bool(_SAFE_ID_RE.fullmatch(value))


def _safe_instance_id(value):
    return isinstance(value, str) and bool(_SAFE_INSTANCE_RE.fullmatch(value))


# Editable persona files, shared with the watchers' load_persona(). Each
# backend has its own directory: /persona -> $JARVIS_PERSONA_DIR (Claude
# watcher), /xpersona -> $JARVIS_XPERSONA_DIR (Codex watcher). $JARVIS_XPERSONA_DIR
# defaults to $JARVIS_PERSONA_DIR, so point it at codex_ask_watcher.py's own
# personas/ dir whenever the two watchers live in different repos.
PERSONA_DIR = os.environ.get(
    "JARVIS_PERSONA_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "personas"),
)
XPERSONA_DIR = os.environ.get("JARVIS_XPERSONA_DIR", PERSONA_DIR)
_PERSONA_MAX_BYTES = 64 * 1024
_PERSONA_READ_CAP = 256 * 1024
_PERSONA_OWNER_NAME = os.environ.get("JARVIS_OWNER_NAME", "владелец")
_PERSONA_OWNER_TG_ID = os.environ.get("JARVIS_OWNER_TG_ID", "")


def _persona_has_control_chars(text):
    # A persona is fed to the model as a system prompt / prompt prefix and, on
    # the Claude side, passed as a --system-prompt argv value. A NUL makes that
    # Popen call fail on every later request; other C0 controls have no place
    # in a persona. Tab / newline / CR are fine.
    return any(ord(c) < 0x20 and c not in "\t\n\r" for c in text)


def _persona_dir_for(path):
    """/xpersona -> Codex persona dir, everything else -> Claude persona dir."""
    return XPERSONA_DIR if path.startswith("/xpersona") else PERSONA_DIR


def _persona_path(persona_dir, instance_id):
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", instance_id or "default")
    return os.path.join(persona_dir, safe + ".md")


def _persona_open_regular(path):
    """Read a persona file, refusing to follow a symlink -- a persona file must
    be a regular file, never a pointer to something else."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "r", encoding="utf-8") as fh:
        return fh.read(_PERSONA_READ_CAP + 1)


def _persona_read(persona_dir, instance_id):
    """Return (text, source): source is 'instance', 'default', 'template' or
    'missing' -- the same lookup order and {{OWNER_*}} filling the watchers use,
    so a GET shows exactly what the model receives."""
    for path, source in (
        (_persona_path(persona_dir, instance_id), "instance"),
        (os.path.join(persona_dir, "default.md"), "default"),
        (os.path.join(persona_dir, "default.md.example"), "template"),
    ):
        try:
            text = _persona_open_regular(path)
        except (OSError, UnicodeError):
            continue
        if len(text) > _PERSONA_READ_CAP:
            continue
        text = (text.replace("{{OWNER_NAME}}", _PERSONA_OWNER_NAME)
                    .replace("{{OWNER_TG_ID}}", _PERSONA_OWNER_TG_ID))
        if not text.strip():
            continue  # empty -> fall through, matching the watchers' load_persona
        return text, source
    return "", "missing"


def _pop_pending_tool_call(instance_id: str, now=None):
    """Atomically claim one pending tool call for this instance -- under
    ThreadingHTTPServer, two overlapping /tool_call_pending polls (unlikely
    at this scale, but not impossible) must not both grab the same file."""
    with _TOOL_QUEUE_LOCK:
        try:
            names = sorted(os.listdir(TOOL_QUEUE_DIR))
        except FileNotFoundError:
            return None
        now = time.time() if now is None else now
        for name in names:
            if not (name.endswith(".json") or name.endswith(".claimed")):
                continue
            path = os.path.join(TOOL_QUEUE_DIR, name)
            try:
                with open(path) as f:
                    data = json.load(f)
            except Exception:
                continue
            if data.get("instance_id") != instance_id:
                continue
            request_id = name.rsplit(".", 1)[0]
            if data.get("cancelled") or float(data.get("expires_at", float("inf"))) <= now:
                data["cancelled"] = True
                data["cancel_reason"] = "expired"
                _atomic_json(path, data)
                continue
            if name.endswith(".claimed") and float(data.get("lease_until", 0)) > now:
                continue
            claimed = os.path.join(TOOL_QUEUE_DIR, request_id + ".claimed")
            data["lease_until"] = now + TOOL_CLAIM_LEASE_S
            data["claimed_at"] = now
            _atomic_json(claimed, data)
            if path != claimed:
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    continue
            data["request_id"] = request_id
            return data
        return None


def _ack_tool_result(request_id, result):
    """Persist a result exactly once, then retire any outstanding claim."""
    result_path = os.path.join(TOOL_RESULT_DIR, f"{request_id}.json")
    if not os.path.exists(result_path):
        _atomic_json(result_path, {"done": True, "request_id": request_id, "result": result})
    for suffix in (".claimed", ".json"):
        try:
            os.unlink(os.path.join(TOOL_QUEUE_DIR, request_id + suffix))
        except FileNotFoundError:
            pass


# Distinct from a genuine "unsure" verdict: "unsure" means the model looked
# at the condition and text and couldn't confidently say yes/no -- a real
# answer that still escalates to a human via the confirm flow. This sentinel
# means the classify call itself never got a real answer at all (queue write
# failed, or claude_watcher.py/codex_ask_watcher.py never produced a result
# within the timeout -- e.g. the account's flat-subscription limit is hit).
# claude_ask.py/codex_ask.py's _classify_condition checks for this and
# retries via the OTHER engine's classify endpoint before ever collapsing to
# a real "unsure" -- see the 2026-08-30 engine-routing unification.
CLASSIFY_UNAVAILABLE = "__unavailable__"


def classify_semantic(text: str, condition: str, instance_id: str, timeout: float = 25.0) -> str:
    """Tier 1 trigger classifier (Phase 4): a cheap 3-way call ("yes"/"no"/
    "unsure") for conditions that don't reduce to a keyword/link/button
    check. Returns "unsure", not just "no", on genuine ambiguity -- that's
    load-bearing, not decorative: a keyword-prefiltered "verify" trigger
    (see claude_ask.py's _resolve_verified_action) auto-acts on a confident
    yes/no but escalates an "unsure" to a human via confirm buttons, per
    the owner directly (2026-08-09) -- "delete/ignore automatically only
    when actually confident, ask a human only for genuinely doubtful
    cases" was the whole point, not "keyword hit -> always ask".

    Deliberately NOT a direct external API call (tried OpenRouter first,
    even with an Anthropic model on it -- wrong call, per the owner
    directly: the entire point of this project running on `claude -p`
    subscription auth instead of metered API billing is to never pay per
    token anywhere in the pipeline, and OpenRouter bills per token
    regardless of which model you pick on it). Routed through the SAME
    queue/result files claude_watcher.py already polls for /ask, tagged
    mode="classify" -- that mode uses --model haiku (cheapest/fastest,
    still flat-subscription) and no session resume (stateless, matching
    search/translate). This function just blocks synchronously inside its
    own request thread (ThreadingHTTPServer, so it doesn't stall other
    endpoints) waiting for claude_watcher.py to pick it up and answer --
    from claude_ask.py's side, /classify still looks like one plain
    request/response call, same as before."""
    if not condition or not text:
        return "unsure"
    req_id = str(uuid.uuid4())
    prompt = (
        "Условие: " + condition + "\n\nСообщение: " + text[:2000]
    )
    try:
        _atomic_json(os.path.join(ASK_QUEUE_DIR, f"{req_id}.json"), {
                "question": prompt, "chat_id": "classify", "request_id": req_id,
                "instance_id": instance_id, "mode": "classify", "ts": time.time(), "done": False,
            })
    except Exception:
        return CLASSIFY_UNAVAILABLE

    result_path = os.path.join(ASK_RESULT_DIR, f"{req_id}.json")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(result_path):
            try:
                with open(result_path) as f:
                    data = json.load(f)
            except Exception:
                data = None
            if data and data.get("done"):
                try:
                    os.remove(result_path)
                except FileNotFoundError:
                    pass
                answer = (data.get("answer") or "").strip().lower()
                # "нет" and "не уверен" both start with "не" -- order
                # matters here, "да"/"нет" are checked as whole-prefix
                # first, anything else (including a genuine "не уверен",
                # empty, or unparseable) falls through to "unsure" as the
                # safe default rather than silently guessing either way.
                if answer.startswith(("да", "yes")):
                    return "yes"
                if answer.startswith(("нет", "no")):
                    return "no"
                return "unsure"
        time.sleep(0.3)
    return CLASSIFY_UNAVAILABLE


def classify_semantic_codex(text: str, condition: str, instance_id: str, timeout: float = 25.0) -> str:
    """Codex-side sibling of classify_semantic above -- same Tier 1 trigger
    classifier contract (3-way yes/no/unsure), same reasoning for why it's
    routed through the flat-subscription queue instead of a metered API
    (Codex CLI is ChatGPT-subscription auth too, not per-token billing).
    There's no Haiku equivalent on the Codex side, so this uses whatever
    codex_ask_watcher.py's CODEX_CLASSIFY_MODEL is configured to (gpt-5.4
    as of 2026-08-29) -- routed through XASK_QUEUE_DIR/XASK_RESULT_DIR
    (codex_ask_watcher.py's own queue, not claude_watcher.py's) tagged
    mode="classify", which codex_ask_watcher.py handles with a bare
    classify prompt, no persona, no session resume. Kept as a fully
    separate function rather than parameterizing classify_semantic --
    matches this file's existing convention of duplicating client-specific
    logic rather than sharing it (see claude_ask.py vs claude_ask_anatoly.py)."""
    if not condition or not text:
        return "unsure"
    req_id = str(uuid.uuid4())
    prompt = (
        "Условие: " + condition + "\n\nСообщение: " + text[:2000]
    )
    try:
        _atomic_json(os.path.join(XASK_QUEUE_DIR, f"{req_id}.json"), {
                "question": prompt, "chat_id": "classify", "instance_id": instance_id,
                "request_id": req_id, "mode": "classify", "ts": time.time(), "done": False,
            })
    except Exception:
        return CLASSIFY_UNAVAILABLE

    result_path = os.path.join(XASK_RESULT_DIR, f"{req_id}.json")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(result_path):
            try:
                with open(result_path) as f:
                    data = json.load(f)
            except Exception:
                data = None
            if data and data.get("done"):
                try:
                    os.remove(result_path)
                except FileNotFoundError:
                    pass
                answer = (data.get("answer") or "").strip().lower()
                if answer.startswith(("да", "yes")):
                    return "yes"
                if answer.startswith(("нет", "no")):
                    return "no"
                return "unsure"
        time.sleep(0.3)
    return CLASSIFY_UNAVAILABLE


def ocr_image(b64: str, question: str = "Извлеки весь текст с этого изображения.") -> str | None:
    """OCR via OpenRouter vision model"""
    if not OPENROUTER_KEY:
        return "OCR unavailable: OPENROUTER_API_KEY is not configured"
    try:
        data = json.dumps({
            "model": "openai/gpt-4o-mini",
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": question},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
                ]
            }],
            "max_tokens": 1000,
        }).encode()
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/chat/completions",
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {OPENROUTER_KEY}",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            result = json.loads(r.read())
        return result["choices"][0]["message"]["content"]
    except Exception as e:
        return f"OCR error: {e}"


def _multipart_file_part(content_type, raw):
    """Return one multipart part's headers and exact payload bytes.

    A multipart delimiter is a complete line, not every occurrence of a
    boundary-looking byte sequence.  In particular, a CRLFCRLF in a file is
    data, and the closing CRLF after a file is optional after ``--boundary--``.
    """
    match = re.search(r"(?:^|;)\s*boundary=(?:\"([^\"]+)\"|([^;\s]+))", content_type, re.I)
    boundary = (match.group(1) or match.group(2)).encode("ascii") if match else b""
    if not boundary or b"\r" in boundary or b"\n" in boundary:
        raise ValueError("Некорректный multipart boundary")
    delimiter = b"--" + boundary
    prefix = delimiter + b"\r\n"
    if not raw.startswith(prefix):
        raise ValueError("Некорректное multipart тело")
    header_start = len(prefix)
    header_end = raw.find(b"\r\n\r\n", header_start)
    if header_end < 0:
        raise ValueError("В multipart нет заголовков файла")
    cursor = header_end + 4
    marker = b"\r\n" + delimiter
    while True:
        end = raw.find(marker, cursor)
        if end < 0:
            raise ValueError("В multipart нет завершающего boundary")
        suffix = raw[end + len(marker):]
        if suffix.startswith(b"--") or suffix.startswith(b"\r\n"):
            return raw[header_start:header_end], raw[cursor:end]
        # A boundary-like sequence inside a file is data unless it ends a
        # delimiter line.  Continue after its leading CRLF without copying.
        cursor = end + 2


class Queue(BaseHTTPRequestHandler):
    def do_POST(self):
        instance_id = self._authenticate()
        if instance_id is None:
            return
        if self.path == "/upload":
            content_type = self.headers.get("Content-Type", "")
            if "multipart/form-data" not in content_type.lower():
                self._error(400, "Ожидался multipart/form-data")
                return
            try:
                content_length = int(self.headers.get("Content-Length", 0))
            except (TypeError, ValueError):
                self._json({"status": "error", "message": "Некорректный Content-Length"})
                return
            if content_length < 0 or content_length > MAX_UPLOAD_BYTES:
                self._json({"status": "error", "message": "Файл слишком большой"})
                return
            body_raw = self.rfile.read(content_length)
            try:
                headers_raw, file_data = _multipart_file_part(content_type, body_raw)
            except ValueError as exc:
                self._error(400, str(exc))
                return
            fname_match = re.search(rb'(?:^|;)\s*filename="([^"]*)"', headers_raw, re.I)
            raw_fname = fname_match.group(1).decode("utf-8", "replace") if fname_match else "uploaded_file"
            normalized_fname = raw_fname.replace("\\", "/")
            fname = os.path.basename(normalized_fname)
            if (
                not fname or fname in (".", "..") or "\x00" in fname
                or ".." in normalized_fname.split("/")
            ):
                self._json({"status": "error", "message": "Некорректное имя файла"})
                return
            fname = fname[:180]
            outbox = os.path.join(ARTIFACT_DIR, instance_id, "uploads")
            os.makedirs(outbox, exist_ok=True)
            save_path = os.path.join(outbox, f"{uuid.uuid4().hex}_{fname}")
            with open(save_path, "wb") as f:
                f.write(file_data)
            _register_artifact(instance_id, save_path)
            self._json({"status": "ok", "path": save_path, "filename": fname})
            return

        body = self._body()

        if self.path == "/cmd":
            _atomic_json(QUEUE_FILE, {"cmd": body.get("cmd", ""), "ts": time.time()})
            self._json({"status": "queued"})

        elif self.path == "/result":
            _atomic_json(RESULT_FILE, body)
            self._json({"status": "ok"})

        elif self.path in ("/ask", "/xask"):
            if not self._body_instance_is_current(body, instance_id):
                return
            req_id = body.get("request_id", "")
            if not _safe_request_id(req_id):
                self._json({"status": "error", "message": "Некорректный request_id"})
                return
            request_kind = "xask" if self.path == "/xask" else "ask"
            existing = _request_record(request_kind, req_id)
            if existing and not hmac.compare_digest(str(existing.get("instance_id", "")), instance_id):
                self._error(403, "request_id уже принадлежит другому instance")
                return
            ask_data = {
                "question": body.get("question", ""),
                "ts": time.time(),
                "done": False,
            }
            for k in (
                "chat_id", "request_id", "message_id", "topic_id", "mode", "requester_id",
            ):
                if k in body:
                    ask_data[k] = body[k]
            ask_data["instance_id"] = instance_id
            if existing:
                self._json({"status": "accepted", "request_id": req_id})
                return
            _record_request(request_kind, req_id, instance_id, body.get("requester_id"))
            ask_data["owner_authorized"] = _request_record(
                request_kind, req_id
            )["owner_authorized"]
            queue_dir = XASK_QUEUE_DIR if self.path == "/xask" else ASK_QUEUE_DIR
            _atomic_json(os.path.join(queue_dir, f"{req_id}.json"), ask_data)
            self._json({"status": "queued"})

        elif self.path == "/tool_call":
            if not self._body_instance_is_current(body, instance_id):
                return
            req_id = body.get("request_id", "")
            if not _safe_request_id(req_id):
                self._json({"status": "error", "message": "Некорректный request_id"})
                return
            parent_request_id = body.get("parent_request_id", "")
            parent = next(
                (
                    record for record in (
                        _request_record("ask", parent_request_id),
                        _request_record("xask", parent_request_id),
                    )
                    if record and hmac.compare_digest(str(record.get("instance_id", "")), instance_id)
                ),
                None,
            )
            if not parent:
                self._error(403, "Запрос инструмента не принадлежит instance")
                return
            call_data = {
                "instance_id": instance_id,
                "chat_id": body.get("chat_id", ""),
                "tool": body.get("tool", ""),
                "args": body.get("args", {}),
                "requester_id": parent.get("requester_id", ""),
                "owner_authorized": bool(parent.get("owner_authorized")),
                "ts": time.time(),
                "expires_at": min(
                    float(body.get("expires_at", time.time() + TOOL_TIMEOUT_S)),
                    time.time() + TOOL_TIMEOUT_S,
                ),
            }
            existing = _request_record("tool", req_id)
            if existing:
                self._json({"status": "accepted", "request_id": req_id})
                return
            _record_request("tool", req_id, instance_id, parent.get("requester_id"))
            _atomic_json(os.path.join(TOOL_QUEUE_DIR, f"{req_id}.json"), call_data)
            self._json({"status": "queued"})

        elif self.path == "/tool_call_result":
            req_id = body.get("request_id", "")
            if not _safe_request_id(req_id):
                self._json({"status": "error", "message": "Некорректный request_id"})
                return
            if not self._owns_request("tool", req_id, instance_id):
                return
            _ack_tool_result(req_id, body.get("result", ""))
            self._json({"status": "ok"})

        elif self.path == "/ocr":
            image_b64 = body.get("image", "")
            question = body.get("question", "Извлеки весь текст с этого изображения.")
            if image_b64:
                text = ocr_image(image_b64, question)
                self._json({"text": text})
            else:
                self._json({"text": None})

        elif self.path == "/classify":
            if not self._body_instance_is_current(body, instance_id):
                return
            result = classify_semantic(body.get("text", ""), body.get("condition", ""), instance_id)
            self._json({"result": result})

        elif self.path == "/xclassify":
            if not self._body_instance_is_current(body, instance_id):
                return
            result = classify_semantic_codex(body.get("text", ""), body.get("condition", ""), instance_id)
            self._json({"result": result})

        elif self.path == "/xreset":
            chat_id = body.get("chat_id", "")
            if not self._body_instance_is_current(body, instance_id):
                return
            if not chat_id:
                self._json({"status": "error", "message": "Некорректные chat_id или instance_id"})
                return
            reset_id = f"reset_{uuid.uuid4().hex}"
            _atomic_json(os.path.join(XASK_RESET_DIR, reset_id + ".json"), {"chat_id": str(chat_id), "instance_id": instance_id})
            self._json({"status": "ok", "message": f"Codex-сессия чата {chat_id} будет сброшена"})

        elif self.path in ("/persona", "/xpersona"):
            # Write (or reset) an instance's editable persona file. The watcher
            # re-reads it on mtime change, so the change lands on the next
            # .ask/.xask with no restart. Owner-only in practice: the only
            # caller is the userbot's own owner-scoped .persona/.xpersona
            # command.
            if not self._body_instance_is_current(body, instance_id):
                return
            persona_dir = _persona_dir_for(self.path)
            os.makedirs(persona_dir, exist_ok=True)
            target = _persona_path(persona_dir, instance_id)
            if body.get("reset"):
                try:
                    os.remove(target)
                    self._json({"status": "ok", "message": "Персона сброшена к шаблону"})
                except FileNotFoundError:
                    self._json({"status": "ok", "message": "Персона уже дефолтная"})
                return
            text = body.get("persona")
            if not isinstance(text, str) or not text.strip():
                self._json({"status": "error", "message": "Пустая персона"})
                return
            if _persona_has_control_chars(text):
                self._json({"status": "error", "message": "Недопустимые управляющие символы в персоне"})
                return
            payload = text.strip() + "\n"
            try:
                payload_bytes = len(payload.encode("utf-8"))
            except UnicodeEncodeError:
                self._json({"status": "error", "message": "Персона содержит недопустимые символы Unicode"})
                return
            if payload_bytes > _PERSONA_MAX_BYTES:
                self._json({"status": "error", "message": "Персона слишком большая"})
                return
            tmp = f"{target}.tmp{uuid.uuid4().hex}"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(payload)
                os.replace(tmp, target)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            self._json({"status": "ok", "message": "Персона обновлена, применится со следующего запроса"})

        elif self.path == "/reset":
            chat_id = body.get("chat_id", "")
            if chat_id:
                # claude_watcher.py now uses real Claude Code sessions
                # (--resume) per chat_id, tracked in this file -- clearing
                # the entry here is what makes the next .ask start a
                # genuinely fresh session instead of resuming the old one.
                #
                # One sessions file per userbot instance (mirrors
                # claude_watcher.py's _sessions_file -- kept duplicated here
                # rather than imported, these run as two separate processes):
                # a Telegram chat_id is the PEER's id, not scoped to which
                # account is asking, so two different userbots messaging the
                # same mutual contact would otherwise share -- and a /reset
                # from one would wipe -- the other's session with that
                # contact. Missing/default instance_id keeps the original
                # filename so the existing deployed client needs no change.
                if not self._body_instance_is_current(body, instance_id):
                    return
                sessions_file = _sessions_enc_file(instance_id)
                cleared = False
                if os.path.exists(sessions_file):
                    try:
                        key = _get_session_key(instance_id)
                        with open(sessions_file, "rb") as f:
                            token = base64.urlsafe_b64encode(f.read())
                        sessions = json.loads(Fernet(key).decrypt(token))
                    except Exception:
                        sessions = {}
                    if sessions.pop(str(chat_id), None) is not None:
                        raw = base64.urlsafe_b64decode(Fernet(key).encrypt(json.dumps(sessions).encode()))
                        with open(sessions_file, "wb") as f:
                            f.write(raw)
                        os.chmod(sessions_file, 0o600)
                        cleared = True
                msg = f"Сессия чата {chat_id} сброшена" if cleared else "Сессия уже пуста"
                self._json({"status": "ok", "message": msg})
            else:
                self._json({"status": "error", "message": "Нет chat_id"})

        elif self.path == "/artifact":
            if not self._body_instance_is_current(body, instance_id):
                return
            if not _register_artifact(instance_id, str(body.get("path") or "")):
                self._error(404, "Артефакт не найден")
                return
            self._json({"status": "ok"})

    def do_GET(self):
        instance_id = self._authenticate()
        if instance_id is None:
            return
        if self.path.startswith("/download"):
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            filepath = params.get("path", [None])[0]
            resolved = os.path.realpath(filepath) if filepath else None
            if not resolved or not _artifact_registered(instance_id, resolved):
                # A manifest belonging to another instance gets a distinct
                # forbidden response; an unregistered path never discloses
                # whether any file exists at that location.
                other_owner = any(
                    _artifact_registered(candidate, resolved)
                    for candidate in set(RELAY_TOKENS.values()) if candidate != instance_id
                )
                self._error(403 if other_owner else 404, "Артефакт недоступен")
                return
            if os.path.isfile(resolved):
                filepath = resolved
                with open(filepath, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                fname = os.path.basename(filepath)
                # BaseHTTPRequestHandler.send_header encodes header VALUES
                # as latin-1 -- any non-latin1 filename (Cyrillic, emoji,
                # etc, which Jarvis routinely generates) raises
                # UnicodeEncodeError mid-response, which breaks the HTTP
                # response the Tailscale funnel is proxying and surfaces to
                # the caller as a bare 502 with no useful error anywhere
                # (caught live 2026-08-13, jarvis-ask's send_file tool).
                # RFC 5987: plain ASCII fallback filename= plus the real
                # name percent-encoded in filename*=UTF-8''... -- nothing
                # downstream here actually reads this header at all
                # (_send_file_action gets its filename from the request
                # path client-side, not this response), so correctness of
                # the fallback name doesn't matter, only that it's ASCII.
                try:
                    self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
                except UnicodeEncodeError:
                    self.send_header(
                        "Content-Disposition",
                        f"attachment; filename=\"file\"; filename*=UTF-8''{urllib.parse.quote(fname)}",
                    )
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                self._error(404, "Артефакт не найден")
            return

        if self.path == "/cmd":
            if os.path.exists(QUEUE_FILE):
                with open(QUEUE_FILE) as f:
                    cmd = json.load(f)
                os.remove(QUEUE_FILE)
                self._json(cmd)
            else:
                self._json({"cmd": None})

        elif self.path == "/result":
            if os.path.exists(RESULT_FILE):
                with open(RESULT_FILE) as f:
                    result = json.load(f)
                self._json(result)
            else:
                self._json({"stdout": "", "stderr": "", "rc": -1})

        elif self.path.startswith("/persona") or self.path.startswith("/xpersona"):
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            if not self._query_instance_is_current(params, instance_id):
                return
            text, source = _persona_read(_persona_dir_for(self.path), instance_id)
            self._json({"status": "ok", "persona": text, "source": source})

        elif self.path.startswith("/ask") or self.path.startswith("/xask"):
            # Always return the file's actual contents, not just when
            # done -- lets a "progress" field (live thought/tool-call
            # updates from claude_watcher.py) flow through to the poller
            # on the other host. The done:true contract is unchanged.
            # Keyed by request_id now (query param) -- concurrent .ask
            # calls each poll their own result file, not a shared one.
            qs = urllib.parse.urlparse(self.path).query
            req_id = urllib.parse.parse_qs(qs).get("request_id", [""])[0]
            result_dir = XASK_RESULT_DIR if self.path.startswith("/xask") else ASK_RESULT_DIR
            kind = "xask" if self.path.startswith("/xask") else "ask"
            if not self._owns_request(kind, req_id, instance_id):
                return
            result_path = os.path.join(result_dir, f"{req_id}.json") if _safe_request_id(req_id) else None
            if result_path and os.path.exists(result_path):
                try:
                    with open(result_path) as f:
                        data = json.load(f)
                    self._json(data)
                    return
                except Exception:
                    pass
            self._json({"done": False, "answer": None})

        elif self.path.startswith("/tool_call_pending"):
            # Polled by the REMOTE userbot host (claude_ask.py's
            # tool_call_watcher loop, through the funnel) -- pops and
            # returns the oldest pending call for this instance_id, or
            # {"tool": None} if the queue's empty for it.
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            if not self._query_instance_is_current(params, instance_id):
                return
            data = _pop_pending_tool_call(instance_id)
            self._json(data or {"tool": None})

        elif self.path.startswith("/tool_call"):
            # Polled LOCALLY by mcp_group_tools.py (same host as this
            # process, no funnel involved) for the result the remote host
            # posts back via POST /tool_call_result.
            qs = urllib.parse.urlparse(self.path).query
            req_id = urllib.parse.parse_qs(qs).get("request_id", [""])[0]
            if not self._owns_request("tool", req_id, instance_id):
                return
            result_path = os.path.join(TOOL_RESULT_DIR, f"{req_id}.json") if _safe_request_id(req_id) else None
            if result_path and os.path.exists(result_path):
                try:
                    with open(result_path) as f:
                        data = json.load(f)
                    self._json(data)
                    return
                except Exception:
                    pass
            self._json({"done": False, "result": None})

    _MAX_JSON_BODY = 1 * 1024 * 1024  # every JSON endpoint here takes small bodies

    def _body(self):
        # Bound and validate before parsing: an unbounded Content-Length would
        # let a client tie up a thread / memory, and a malformed or non-object
        # body used to raise straight out of the handler. Callers already treat
        # a missing field as an error, so {} is a safe "bad body" return.
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            return {}
        if length <= 0 or length > self._MAX_JSON_BODY:
            return {}
        try:
            parsed = json.loads(self.rfile.read(length))
        except (ValueError, OSError, RecursionError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _authenticate(self):
        value = self.headers.get("Authorization", "")
        if not value.startswith("Bearer ") or not value[7:]:
            self._error(401, "Требуется Authorization: Bearer <token>")
            return None
        digest = hashlib.sha256(value[7:].encode("utf-8")).hexdigest()
        matched = None
        # Do not use a direct dictionary lookup for a credential comparison.
        # All configured digests have the same fixed length, so every compare
        # is constant-time with respect to the token value.
        for stored_digest, candidate_instance in RELAY_TOKENS.items():
            if hmac.compare_digest(digest, stored_digest):
                matched = candidate_instance
        if matched is None:
            self._error(401, "Недействительный relay token")
            return None
        return matched

    def _body_instance_is_current(self, body, instance_id):
        if "instance_id" in body and not hmac.compare_digest(str(body["instance_id"]), instance_id):
            self._error(403, "instance_id не соответствует relay token")
            return False
        return True

    def _query_instance_is_current(self, params, instance_id):
        values = params.get("instance_id")
        if values and not hmac.compare_digest(str(values[0]), instance_id):
            self._error(403, "instance_id не соответствует relay token")
            return False
        return True

    def _owns_request(self, kind, request_id, instance_id):
        if not _safe_request_id(request_id):
            self._error(404, "Запрос не найден")
            return False
        record = _request_record(kind, request_id)
        if record is None:
            self._error(404, "Запрос не найден")
            return False
        if not hmac.compare_digest(str(record.get("instance_id", "")), instance_id):
            self._error(403, "Запрос принадлежит другому instance")
            return False
        return True

    def _error(self, status, message):
        self._json({"status": "error", "message": message}, status=status)

    def _json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def log_message(self, fmt, *args):
        pass


class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True


def _add_token(instance_id):
    if not _safe_instance_id(instance_id):
        raise RuntimeError("Некорректный instance_id")
    path = os.environ.get("JARVIS_RELAY_TOKENS_FILE")
    if not path:
        raise RuntimeError("JARVIS_RELAY_TOKENS_FILE не задан")
    try:
        with open(path, encoding="utf-8") as handle:
            entries = json.load(handle)
    except FileNotFoundError:
        entries = {}
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Не удалось прочитать tokens-файл: {exc}") from exc
    if not isinstance(entries, dict):
        raise RuntimeError("tokens-файл должен быть JSON-объектом")
    token = secrets.token_urlsafe(32)
    entries[hashlib.sha256(token.encode("utf-8")).hexdigest()] = instance_id
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    _atomic_json(path, entries)
    os.chmod(path, 0o600)
    return token


def _bind_addresses():
    raw = os.environ.get("JARVIS_RELAY_BIND") or os.environ.get("JARVIS_QUEUE_BIND") or "127.0.0.1"
    addresses = [value.strip() for value in raw.split(",") if value.strip()]
    if not addresses:
        raise RuntimeError("JARVIS_RELAY_BIND не содержит адресов")
    return addresses


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--add-token"]:
        if len(argv) != 2:
            raise RuntimeError("Использование: cmd_queue.py --add-token <instance_id>")
        print(_add_token(argv[1]))  # printed once; only its hash is persisted
        return 0
    if argv not in ([], ["--check-config"]):
        raise RuntimeError("Использование: cmd_queue.py [--check-config | --add-token <instance_id>]")
    _load_relay_config()
    if argv == ["--check-config"]:
        print("relay configuration OK")
        return 0
    servers = [
        ThreadingHTTPServer((address, int(os.environ.get("JARVIS_QUEUE_PORT", "9092"))), Queue)
        for address in _bind_addresses()
    ]
    threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in servers[1:]]
    for thread in threads:
        thread.start()
    try:
        servers[0].serve_forever()
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"cmd_queue: {exc}", file=sys.stderr)
        raise SystemExit(1)
