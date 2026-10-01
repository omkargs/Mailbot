"""Phase 5: per-chat memory, one handler everywhere, sanitized identity."""
from __future__ import annotations


def test_chats_remember_separately():
    from mailbot.agent import chatlog

    chatlog.record("user", "the msk tour question", chat="owner")
    chatlog.record("agent", "msk asked about friday", chat="owner")
    chatlog.record("user", "unrelated terminal thread", chat="terminal")

    owner = [t["content"] for t in chatlog.recent(chat="owner")]
    term = [t["content"] for t in chatlog.recent(chat="terminal")]
    assert owner == ["the msk tour question", "msk asked about friday"]
    assert term == ["unrelated terminal thread"]


def test_clear_is_per_chat():
    from mailbot.agent import chatlog

    chatlog.record("user", "keep me", chat="owner")
    chatlog.record("user", "drop me", chat="terminal")
    assert chatlog.clear(chat="terminal") == 1
    assert [t["content"] for t in chatlog.recent(chat="owner")] == ["keep me"]
    assert chatlog.recent(chat="terminal") == []


def test_memory_still_scoped_by_account():
    from mailbot.agent import chatlog

    chatlog.record("user", "google thread", account="google", chat="owner")
    chatlog.record("user", "work thread", account="work", chat="owner")
    assert [t["content"] for t in chatlog.recent(account="google")] == ["google thread"]
    assert [t["content"] for t in chatlog.recent(account="work")] == ["work thread"]


def test_handle_text_routes_plain_chat_per_chat(cfg):
    from mailbot.agent.chat import handle_text

    seen: list[tuple[str, str]] = []

    def fake_ask(question, chat="owner"):
        seen.append((question, chat))
        return f"heard on {chat}"

    ops = {"ask": fake_ask, "reset": lambda chat="owner": "reset"}
    assert handle_text("reply to him", cfg, {}, ops, chat="owner") == "heard on owner"
    assert handle_text("reply to him", cfg, {}, ops, chat="terminal") == "heard on terminal"
    assert seen == [("reply to him", "owner"), ("reply to him", "terminal")]


def test_reset_clears_only_that_chat(cfg):
    from mailbot.agent import chatlog
    from mailbot.agent.chat import handle_text

    ops = {"ask": lambda q, chat="owner": "ok", "reset": None}
    from mailbot.agent.chatops import build_chat_ops

    real_ops = build_chat_ops(cfg, lambda: {}, notify=None)
    ops["reset"] = real_ops["reset"]
    chatlog.record("user", "owner memory", chat="owner")
    chatlog.record("user", "terminal memory", chat="terminal")
    out = handle_text("/reset", cfg, {}, ops, chat="terminal")
    assert "Fresh start" in out
    assert [t["content"] for t in chatlog.recent(chat="owner")] == ["owner memory"]
    assert chatlog.recent(chat="terminal") == []


def test_telegram_poll_tags_owner_chat(monkeypatch):
    from mailbot.notify.channels import TelegramNotifier

    n = TelegramNotifier("tok", "8791132013")

    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"result": [{
                "update_id": 7,
                "message": {"message_id": 1,
                            "from": {"id": 8791132013, "is_bot": False},
                            "chat": {"id": 8791132013, "type": "private"},
                            "text": "hello"}}]}

    import mailbot.notify.channels as C

    monkeypatch.setattr(C.requests, "get", lambda *a, **k: R())
    out = n.poll_once()
    assert out and out[0]["action"] == "chat"
    assert out[0]["chat"] == "telegram:8791132013"


def test_sanitize_rewrites_model_cards():
    from mailbot.agent.ask import sanitize_identity

    assert "Mailbot" in sanitize_identity("Hi, I'm agnes-3-flash at your service")
    assert "Sapiens AI" not in sanitize_identity("built by Sapiens AI for you")
    out = sanitize_identity("Under the hood I'm claude-opus thinking away.")
    assert "claude-opus" not in out and "provider routed to" in out
    # SDK references are documentation, not identity claims — untouched.
    assert "Claude Agent SDK" in sanitize_identity("gated by the Claude Agent SDK hooks")
    # Ordinary prose passes through byte-identical.
    plain = "You have 3 approvals waiting, all routine."
    assert sanitize_identity(plain) == plain
    assert sanitize_identity("") == ""
