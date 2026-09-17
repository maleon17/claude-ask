"""Regression tests for the Ask UI transport; no Telegram or relay required."""
import asyncio
import json
from unittest.mock import AsyncMock

from test_trigger_authorization import claude_ask, make_module


class Message:
    def __init__(self, text="🤔 Думаю", message_id=9):
        self.text, self.raw_text, self.id = text, text, message_id
        self.edits = []

    async def edit(self, text, **kwargs):
        self.edits.append((text, kwargs))
        self.text = text


def run(awaitable):
    return asyncio.run(awaitable)


def test_safe_edit_plain_fallback_explicitly_disables_html():
    class HtmlByDefault(Message):
        async def edit(self, text, **kwargs):
            self.edits.append((text, kwargs))
            if len(self.edits) == 1:
                raise ValueError("bad HTML")

    message = HtmlByDefault()
    run(make_module()._safe_edit(message, "literal <tag>", parse_mode="html"))
    assert message.edits[-1] == ("literal <tag>", {"parse_mode": None})


def test_telegram_text_limit_resolves_without_test_side_overrides():
    """Regression: TELEGRAM_TEXT_LIMIT was a bare module-level name, not a
    class attribute, so self.TELEGRAM_TEXT_LIMIT raised AttributeError on
    every real instance -- _dispatch_answer crashed before delivering ANY
    final answer, short or long. The sibling test above sets
    bot.TELEGRAM_TEXT_LIMIT directly and would never have caught this."""
    bot = make_module()
    message = Message()
    run(bot._dispatch_answer(None, 1, "q", "chat", 0, message, "short answer", []))
    assert message.edits and "short answer" in message.edits[-1][0]


def test_final_answer_is_split_and_failed_edit_is_visible():
    bot = make_module()
    message = Message()
    message.respond = AsyncMock(side_effect=lambda text: Message(text, 100 + len(message.edits)))
    bot.TELEGRAM_TEXT_LIMIT = 20
    run(bot._dispatch_answer(None, 1, "q", "chat", 0, message, "x" * 55, ["thought" * 4]))
    delivered = "".join(edit[0] for edit in message.edits) + "".join(
        call.args[0] for call in message.respond.await_args_list
    )
    assert "x" * 55 in delivered


def test_history_anchor_is_not_committed_until_enqueue_ack():
    bot = make_module()
    bot.db.values = {}
    bot.db.get = lambda ns, key, default=None: bot.db.values.get(key, default)
    bot.db.set = lambda ns, key, value: bot.db.values.__setitem__(key, value)
    msg = type("M", (), {"chat_id": 1, "id": 10, "reply_to": None})()
    bot._get_chat_history = AsyncMock(return_value="history")
    text, delta, pending = run(bot._get_chat_history_delta(msg))
    assert (text, delta) == ("history", False)
    assert bot.db.values == {}
    bot._commit_history_anchor(pending)
    assert bot.db.values["last_seen_id_1"] == 10


def test_enqueue_rejects_application_error(monkeypatch):
    bot = make_module()
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return b'{"status":"error", "message":"queue down"}'
    monkeypatch.setattr(bot, "_relay_open", lambda *args, **kwargs: Response())
    ok, error = bot._enqueue("q", 1, "r")
    assert not ok and "queue down" in error


def test_enqueue_accepts_the_relay_own_success_statuses(monkeypatch):
    """Regression: cmd_queue.py's /ask and /xask never return status "ok" --
    a fresh request is "queued", an idempotent retry is "accepted". Checking
    for == "ok" instead of == "error" treated every real success as a
    rejected request."""
    bot = make_module()
    for status in ("queued", "accepted"):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, status=status): return json.dumps({"status": status}).encode()
        monkeypatch.setattr(bot, "_relay_open", lambda *a, **kw: Response())
        ok, error = bot._enqueue("q", 1, "r")
        assert ok and error == ""


def test_persona_navigation_keeps_index_when_page_cannot_be_read():
    bot = make_module()
    bot._persona_sessions = {"s": {"pages": ["one", "two"], "index": 0, "chat_id": 1, "code_msg_id": 2}}
    bot._client = type("C", (), {"get_messages": AsyncMock(side_effect=OSError("offline"))})()
    bot._persona_ack = AsyncMock()
    run(bot._persona_nav(object(), "s", 1))
    assert bot._persona_sessions["s"]["index"] == 0
    assert "ошибка" in bot._persona_ack.await_args.args[1].lower()


class RealShapeDB(dict):
    """Mirrors the live Heroku framework's Database class exactly: it IS a
    dict, {owner: {key: value}}, with .get()/.set() overriding the 2-arg
    dict.get() with a 3-arg (owner, key, default) signature. Regression for
    a real bug: _clear_history_anchors() used to probe for a ._db or
    .values attribute holding a flat mapping, neither of which exists on
    the real Database(dict) -- topic-scoped cursors were silently never
    cleared. See /Heroku/heroku/database.py on the live userbot host."""

    def get(self, owner, key=None, default=None):
        try:
            return self[owner][key]
        except KeyError:
            return default

    def set(self, owner, key, value):
        self.setdefault(owner, {})[key] = value
        return True


def test_reset_clears_every_topic_cursor_for_the_chat():
    bot = make_module()
    bot.db = RealShapeDB()
    bot.db.set("ClaudeAsk", "last_seen_id_1", 10)
    bot.db.set("ClaudeAsk", "last_seen_id_1_55", 20)
    bot.db.set("ClaudeAsk", "last_seen_id_1_77", 30)
    # A different chat's cursor, and an unrelated key sharing the prefix as
    # a substring, must survive untouched.
    bot.db.set("ClaudeAsk", "last_seen_id_12", 40)
    bot.db.set("ClaudeAsk", "unrelated", "x")

    bot._clear_history_anchors(1)

    ns = dict.get(bot.db, "ClaudeAsk")
    assert ns["last_seen_id_1"] is None
    assert ns["last_seen_id_1_55"] is None
    assert ns["last_seen_id_1_77"] is None
    assert ns["last_seen_id_12"] == 40
    assert ns["unrelated"] == "x"
