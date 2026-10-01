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


def test_offset_survives_restart(tg, tmp_path, monkeypatch):
    from mailbot.notify.channels import TelegramNotifier

    import mailbot.config as C

    monkeypatch.setattr(C, "DATA_DIR", tmp_path / "data")

    class R:
        def __init__(self, result):
            self._result = result

        def raise_for_status(self):
            pass

        def json(self):
            return {"result": self._result}

    import mailbot.notify.channels as CH

    seen_params = []

    def fake_get(url, params=None, timeout=None):
        seen_params.append(dict(params or {}))
        return R([{"update_id": 41, "message": {
            "message_id": 1, "from": {"id": 7, "is_bot": False},
            "chat": {"id": 12345, "type": "private"}, "text": "hi"}}])

    monkeypatch.setattr(CH.requests, "get", fake_get)
    n1 = TelegramNotifier("tok", "12345", long_poll=False)
    assert n1._offset is None
    out = n1.poll_once()
    assert out and out[0]["update_id"] == 41
    # A fresh notifier (a restart) resumes past the consumed update.
    n2 = TelegramNotifier("tok", "12345", long_poll=False)
    assert n2._offset == 42
    n2.poll_once()
    assert seen_params[-1].get("offset") == 42


def test_approval_card_carries_buttons(tg, monkeypatch):
    from mailbot.notify.channels import TelegramNotifier

    import mailbot.notify.channels as CH

    posted = []

    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"result": {"message_id": 9}}

    def fake_post(url, json=None, timeout=None):
        posted.append(json)
        return R()

    monkeypatch.setattr(CH.requests, "post", fake_post)
    n = TelegramNotifier("tok", "12345", long_poll=False)
    n.send("Approval needed: reply — hi", approval_id="ap_abc123")
    kb = posted[0]["reply_markup"]["inline_keyboard"][0]
    assert kb[0] == {"text": "✅ Send", "callback_data": "ap:ap_abc123"}
    assert kb[1] == {"text": "🗑 Discard", "callback_data": "deny:ap_abc123"}
    assert len(kb[0]["callback_data"]) <= 64


def test_button_tap_routes_to_approval(tg, monkeypatch):
    from mailbot.notify.channels import TelegramNotifier

    import mailbot.notify.channels as CH

    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"result": [{
                "update_id": 5,
                "callback_query": {
                    "id": "cb1",
                    "from": {"id": 12345},
                    "data": "ap:ap_abc123",
                    "message": {"message_id": 9, "chat": {"id": 12345}},
                }}]}

    monkeypatch.setattr(CH.requests, "get", lambda *a, **k: R())
    n = TelegramNotifier("tok", "12345", long_poll=False)
    out = n.poll_once()
    assert out == [{"action": "approve", "approval_id": "ap_abc123",
                    "callback_id": "cb1", "callback_chat": "12345",
                    "callback_msg": 9}]


def test_stranger_tap_dropped(tg, monkeypatch):
    from mailbot.notify.channels import TelegramNotifier

    import mailbot.notify.channels as CH

    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"result": [{
                "update_id": 6,
                "callback_query": {
                    "id": "cb2",
                    "from": {"id": 666},
                    "data": "ap:ap_abc123",
                    "message": {"message_id": 9, "chat": {"id": 12345}},
                }}]}

    monkeypatch.setattr(CH.requests, "get", lambda *a, **k: R())
    n = TelegramNotifier("tok", "12345", long_poll=False)
    assert n.poll_once() == []


def test_it_chats_back(tg):
    UPDATES.append([{"update_id": 1, "message": {
        "message_id": 1, "from": {"id": 7, "is_bot": False},
        "chat": {"id": 12345}, "text": "what needs me?"}}])
    n = TelegramNotifier("tok", "12345", long_poll=False)
    out = n.poll_once()
    assert out == [{"action": "chat", "text": "what needs me?", "update_id": 1,
                      "chat": "telegram:12345"}]


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
