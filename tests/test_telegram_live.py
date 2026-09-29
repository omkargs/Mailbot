"""Does the agent actually reach Telegram, in both directions?

Answering a question the user asked, with evidence rather than a reading
of the source. A local HTTP server stands in for api.telegram.org, the
real TelegramNotifier talks to it, and we check what went over the wire.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from mailbot.notify.channels import TelegramNotifier

SENT = []
UPDATES = []


class FakeTelegram(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _reply(self, payload):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        SENT.append(json.loads(self.rfile.read(n) or b"{}"))
        self._reply({"ok": True, "result": {"message_id": 42}})

    def do_GET(self):
        if self.path.startswith("/bot") and "getUpdates" in self.path:
            batch = UPDATES.pop(0) if UPDATES else []
            self._reply({"ok": True, "result": batch})
        else:
            self._reply({"ok": True, "result": {}})


@pytest.fixture()
def tg(monkeypatch):
    SENT.clear()
    UPDATES.clear()
    srv = HTTPServer(("127.0.0.1", 0), FakeTelegram)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(TelegramNotifier, "API",
                        f"http://127.0.0.1:{srv.server_address[1]}")
    yield srv
    srv.shutdown()


def test_it_sends_you_a_message(tg):
    n = TelegramNotifier("tok", "12345", long_poll=False)
    assert n.send("Your inbox is handled. 2 things need you.") == "42"
    assert len(SENT) == 1
    assert SENT[0]["chat_id"] == "12345"
    assert "inbox is handled" in SENT[0]["text"]


def test_an_approval_arrives_with_the_commands_to_answer_it(tg):
    n = TelegramNotifier("tok", "12345", long_poll=False)
    n.send("Invoice from Acme needs you.", approval_id="abc123")
    text = SENT[0]["text"]
    assert "/approve abc123" in text
    assert "/discard abc123" in text


def test_it_chats_back(tg):
    UPDATES.append([{"update_id": 1, "message": {
        "message_id": 1, "from": {"id": 7, "is_bot": False},
        "chat": {"id": 12345}, "text": "what needs me?"}}])
    n = TelegramNotifier("tok", "12345", long_poll=False)
    out = n.poll_once()
    assert out == [{"action": "chat", "text": "what needs me?", "update_id": 1}]


def test_approve_and_discard_are_understood(tg):
    for cmd, act in (("/approve", "approve"), ("/discard", "deny")):
        UPDATES.append([{"update_id": 9, "message": {
            "message_id": 2, "from": {"id": 7, "is_bot": False},
            "chat": {"id": 12345}, "text": f"{cmd} deadbeef"}}])
        n = TelegramNotifier("tok", "12345", long_poll=False)
        assert n.poll_once() == [{"action": act, "approval_id": "deadbeef"}]


def test_a_stranger_cannot_talk_to_your_agent(tg):
    """The security gate. Telegram delivers from any chat that messages the bot."""
    UPDATES.append([{"update_id": 1, "message": {
        "message_id": 1, "from": {"id": 999, "is_bot": False},
        "chat": {"id": 999}, "text": "/approve deadbeef"}}])
    n = TelegramNotifier("tok", "12345", long_poll=False)
    assert n.poll_once() == [], "a foreign chat id got through"


def test_it_does_not_answer_itself(tg):
    UPDATES.append([{"update_id": 1, "message": {
        "message_id": 1, "from": {"id": 7, "is_bot": True},
        "chat": {"id": 12345}, "text": "echo"}}])
    n = TelegramNotifier("tok", "12345", long_poll=False)
    assert n.poll_once() == []


def test_a_message_is_not_answered_twice(tg):
    """Without an advancing offset, every poll re-answers the same message."""
    UPDATES.append([{"update_id": 500, "message": {
        "message_id": 1, "from": {"id": 7, "is_bot": False},
        "chat": {"id": 12345}, "text": "hello"}}])
    n = TelegramNotifier("tok", "12345", long_poll=False)
    assert len(n.poll_once()) == 1
    assert n.poll_once() == [], "same message answered again"
    # And the offset went out on the wire.
    assert n._offset == 501


def test_markdown_and_angles_survive(tg):
    """A bare < in a subject used to make Telegram reject the whole message."""
    n = TelegramNotifier("tok", "12345", long_poll=False)
    n.send("Re: 3 < 5 pricing from <sales@acme.com> — **urgent**")
    assert "<sales@acme.com>" not in SENT[0]["text"]
    assert "&lt;" in SENT[0]["text"]
    assert "<b>urgent</b>" in SENT[0]["text"]


def test_only_telegram_is_interactive():
    """Discord and Slack cannot poll button clicks, so they notify only."""
    from mailbot.notify.channels import (DiscordNotifier, SlackNotifier,
                                         TelegramNotifier as T)
    assert T.interactive(T("t", "1")) is True
    assert DiscordNotifier("t", "1").interactive() is False
    assert SlackNotifier("t", "c").interactive() is False
