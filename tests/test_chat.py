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


def test_show_prints_full_queued_text(provider, cfg):
    from mailbot.agent import guards as _guards
    from mailbot.agent.chatops import build_chat_ops
    from mailbot.storage import db

    body = "line one here friend " + "filler words " * 60
    p = {"to": ["a@b.com"], "subject": "Re: hello", "body": body,
         "in_reply_to": "m1", "attachments": []}
    p["action_hash"] = _guards.action_hash(
        "send_message", {k: v for k, v in p.items() if k != "action_hash"})
    db.create_approval("ap_show1", "google", "send", p, reason="t")
    ops = build_chat_ops(cfg, lambda: {"google": provider}, notify=None)
    out = ops["show"]("ap_show1")
    assert "filler words" in out and "/approve ap_show1" in out
    assert ops["show"]("nope") == "No approval with id 'nope'."
    assert ops["show"]("").startswith("Usage:")


def test_approve_bare_approves_lone_pending(provider, cfg):
    from mailbot.agent import guards as _guards
    from mailbot.agent.chat import handle_text
    from mailbot.agent.chatops import build_chat_ops
    from mailbot.storage import db

    provider.auto_send = True
    p = {"to": ["boss@corp.com"], "subject": "Re: hi",
         "body": "hello friend, confirming friday works fine for us",
         "in_reply_to": "", "attachments": []}
    p["action_hash"] = _guards.action_hash(
        "send_message", {k: v for k, v in p.items() if k != "action_hash"})
    db.create_approval("ap_bare1", "google", "send", p, reason="t")
    ops = build_chat_ops(cfg, lambda: {"google": provider}, notify=None)
    out = handle_text("/approve", cfg, {"google": provider}, ops)
    assert out == "Sent."
    assert len(provider.sent) == 1


def test_approve_bare_with_several_asks_for_id(provider, cfg):
    from mailbot.agent.chat import handle_text
    from mailbot.agent.chatops import build_chat_ops
    from mailbot.storage import db

    db.create_approval("ap_b1", "google", "send", {"to": ["a@b.com"]}, reason="t")
    db.create_approval("ap_b2", "google", "send", {"to": ["b@b.com"]}, reason="t")
    ops = build_chat_ops(cfg, lambda: {"google": provider}, notify=None)
    out = handle_text("/approve", cfg, {"google": provider}, ops)
    assert "Usage:" in out and provider.sent == []


def test_yea_confirms_outstanding_proposal(provider, cfg):
    from mailbot.agent.chatops import build_chat_ops
    from mailbot.storage import db

    db.log_action("auto_send_proposed", "google", "msk@x.com",
                  detail="approved 2x in a row")
    seen = {}
    import mailbot.agent.ask as ask_mod

    orig = ask_mod.answer

    def spy(question, cfg_, p, history=None, notify=None):
        seen["q"] = question
        return "ok"

    ask_mod.answer = spy
    try:
        ops = build_chat_ops(cfg, lambda: {"google": provider}, notify=None)
        ops["ask"]("yea", "owner")
        assert "msk@x.com" in seen["q"] and "set_contact_permission" in seen["q"]
    finally:
        ask_mod.answer = orig


def test_no_proposal_no_rewrite(provider, cfg):
    import mailbot.agent.ask as ask_mod
    from mailbot.agent.chatops import build_chat_ops

    seen = {}
    orig = ask_mod.answer

    def spy(question, cfg_, p, history=None, notify=None):
        seen["q"] = question
        return "ok"

    ask_mod.answer = spy
    try:
        ops = build_chat_ops(cfg, lambda: {"google": provider}, notify=None)
        ops["ask"]("yea", "owner")
        assert seen["q"] == "yea"
    finally:
        ask_mod.answer = orig


def test_profiles_commands_are_single_inbox(cfg):
    from mailbot.agent.chat import handle_text
    from mailbot.agent.chatops import build_chat_ops

    ops = build_chat_ops(cfg, lambda: {}, notify=None)
    assert "Single inbox" in handle_text("/profiles", cfg, {}, ops)
    assert "Single inbox" in handle_text("/change-profile work", cfg, {}, ops)
    out = handle_text("/whoami", cfg, {}, ops)
    assert "inbox:" in out and "brain:" in out and "voice:" in out


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
