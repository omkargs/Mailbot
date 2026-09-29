"""Profiles step A: model + migration, zero behaviour change."""
from __future__ import annotations


def _isolate(monkeypatch, tmp_path):
    from mailbot import profiles as P
    from mailbot import config as C
    from mailbot.storage import db as D

    monkeypatch.setattr(C, "CONFIG_DIR", tmp_path / "cfg")
    monkeypatch.setattr(C, "CONFIG_PATH", tmp_path / "cfg" / "config.json")
    monkeypatch.setattr(C, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(C, "DB_PATH", tmp_path / "data" / "agent.db")
    monkeypatch.setattr(D, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(D, "DB_PATH", tmp_path / "data" / "agent.db")
    monkeypatch.setattr(P, "CONFIG_PATH", tmp_path / "cfg" / "config.json")


def test_migration_creates_personal_profile(monkeypatch, tmp_path):
    from mailbot import profiles as P

    _isolate(monkeypatch, tmp_path)
    assert P.ensure_migrated() is True
    rows = P.list_profiles()
    assert len(rows) == 1
    assert rows[0]["id"] == "personal"
    assert rows[0]["account"] == "google"
    assert P.get_current()["id"] == "personal"
    # config.json mirrors it
    import json

    data = json.loads((tmp_path / "cfg" / "config.json").read_text())
    assert data["profiles"][0]["id"] == "personal"


def test_migration_is_idempotent(monkeypatch, tmp_path):
    from mailbot import profiles as P

    _isolate(monkeypatch, tmp_path)
    assert P.ensure_migrated() is True
    assert P.ensure_migrated() is False
    assert len(P.list_profiles()) == 1


def test_model_override_falls_back():
    from mailbot import profiles as P

    assert P.model_for(None, "main/x") == "main/x"
    assert P.model_for({"model_override": ""}, "main/x") == "main/x"
    assert P.model_for({"model_override": "cheap/y"}, "main/x") == "cheap/y"


def test_header_format():
    from mailbot import profiles as P

    assert P.header_for(None) == ""
    assert P.header_for(None, "a@b.com") == "[a@b.com]"
    assert P.header_for({"name": "Family", "account": "google"},
                        "me@gmail.com") == "[Family · me@gmail.com]"
    assert P.header_for({"name": "Work", "account": "ms"}) == "[Work · ms]"


def test_active_binds_profiles_to_providers(provider, cfg):
    from mailbot import profiles as P

    P.ensure_migrated()
    bound = P.active(cfg, {"google": provider})
    assert len(bound) == 1
    prof, p = bound[0]
    assert prof["id"] == "personal" and p is provider


def test_run_once_uses_profile_model_override(provider, cfg, monkeypatch):
    from types import SimpleNamespace
    import mailbot.agent.runner as R
    import mailbot.agent.client as C

    monkeypatch.setattr(C, "build_client", lambda rc: object())
    seen = {}

    def fake_call(client, **kw):
        seen["model"] = kw["model"]
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="Nothing needed doing.")],
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=1, output_tokens=1,
                                  cache_read_input_tokens=0,
                                  cache_creation_input_tokens=0))

    monkeypatch.setattr(R, "guarded_call", fake_call)
    provider.add_message(sender="a@b.com", subject="Hello",
                         body="Just saying hello to you friend")
    R.run_once("google", provider, cfg, messages=list(provider.inbox),
               model="cheap/custom-1")
    assert seen["model"] == "cheap/custom-1"


def test_run_once_defaults_to_global_model(provider, cfg, monkeypatch):
    from types import SimpleNamespace
    import mailbot.agent.runner as R
    import mailbot.agent.client as C

    monkeypatch.setattr(C, "build_client", lambda rc: object())
    seen = {}

    def fake_call(client, **kw):
        seen["model"] = kw["model"]
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="Nothing needed doing.")],
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=1, output_tokens=1,
                                  cache_read_input_tokens=0,
                                  cache_creation_input_tokens=0))

    monkeypatch.setattr(R, "guarded_call", fake_call)
    provider.add_message(sender="a@b.com", subject="Hello",
                         body="Just saying hello to you friend")
    R.run_once("google", provider, cfg, messages=list(provider.inbox))
    assert seen["model"] == cfg.router.model


def test_set_current_switches_exactly_one(monkeypatch, tmp_path):
    from mailbot import profiles as P
    from mailbot.storage import db

    _isolate(monkeypatch, tmp_path)
    P.ensure_migrated()
    with db.db() as c:
        c.execute("INSERT INTO profiles (id, name, account, model_override, is_current, created_at)"
                  " VALUES (?, ?, ?, ?, ?, ?)",
                  ("work", "Work", "google", "", 0, "2026-01-01"))
    assert P.set_current("work")["id"] == "work"
    assert P.get_current()["id"] == "work"
    assert P.set_current("nope") is None
    assert P.get_current()["id"] == "work"


def test_chat_profiles_and_switch_and_whoami(provider, cfg):
    from mailbot.agent.chat import handle_text
    from mailbot import profiles as P

    P.ensure_migrated()
    out = handle_text("/profiles", cfg, {"google": provider}, {})
    assert "Personal" in out and "/change-profile" in out
    assert "No profile" in handle_text("/change-profile nope", cfg, {"google": provider}, {})
    out = handle_text("/whoami", cfg, {"google": provider}, {})
    assert "Personal" in out and "brain:" in out


def test_toolbox_tags_carry_profile(provider, cfg):
    from mailbot.agent.tools import ToolBox
    from mailbot import profiles as P

    P.ensure_migrated()
    cur = P.get_current()
    sent = []
    box = ToolBox(provider, cfg, run_id=1,
                  notify=lambda t, approval_id="": sent.append(t) or "1",
                  profile=cur)
    box.run("send_message", {"to": ["new@x.com"], "subject": "Hi",
                             "body": "Just saying hello to you friend"})
    assert sent and sent[0].startswith("[Personal ·")


def test_slug():
    from mailbot import profiles as P

    assert P.slug("Family Mail") == "family-mail"
    assert P.slug("  Work! ") == "work"
    assert P.slug("") == "inbox"


def test_add_profile_registers_inbox(monkeypatch, tmp_path):
    import json as _json
    from mailbot import profiles as P

    _isolate(monkeypatch, tmp_path)
    row = P.add_profile("Family", "google:family")
    assert row["id"] == "family"
    assert row["account"] == "google:family"
    assert row["is_current"] == 0  # personal stays current
    data = _json.loads((tmp_path / "cfg" / "config.json").read_text())
    assert any(p["id"] == "family" for p in data["profiles"])
    import pytest as _pytest

    with _pytest.raises(ValueError):
        P.add_profile("Family", "google:family")


def test_factory_builds_extra_inbox(monkeypatch, tmp_path):
    import json as _json
    from mailbot import profiles as P
    from mailbot.config import load as _load
    from mailbot.providers import build_providers

    _isolate(monkeypatch, tmp_path)
    P.ensure_migrated()
    P.add_profile("Family", "google:family")
    creds = P.creds_file("family")
    creds.parent.mkdir(parents=True, exist_ok=True)
    creds.write_text(_json.dumps({"installed": {"client_id": "x"}}))
    provs = build_providers(_load())
    assert "google:family" in provs
    assert provs["google:family"].display_name == "Family"


def test_factory_skips_inbox_without_creds(monkeypatch, tmp_path):
    from mailbot import profiles as P
    from mailbot.config import load as _load
    from mailbot.providers import build_providers

    _isolate(monkeypatch, tmp_path)
    P.ensure_migrated()
    P.add_profile("Family", "google:family")
    provs = build_providers(_load())
    assert "google:family" not in provs


def test_extra_inbox_gets_its_own_account_id():
    """Every provider must carry its own account key on the INSTANCE.

    `account` used to be a class attribute defaulting to "google", so a
    second inbox reported account="google" as well. That key is the primary
    key on messages, cursors, runs, approvals and the per-inbox voice
    profile - so a second inbox read the first one's cursor, stored its
    mail under the same account, and shared its voice. Exactly the
    isolation add-inbox exists to provide.
    """
    from mailbot.providers.gmail import GmailProvider

    a = GmailProvider("c1", "t1", account="google")
    b = GmailProvider("c2", "t2", account="google:work")
    assert a.account == "google"
    assert b.account == "google:work"
    # Mutating one must not touch the other.
    b.account = "google:work"
    assert a.account == "google"
    # And the class default is untouched, so new instances are sane.
    assert GmailProvider("c3", "t3").account == "google"


def test_two_inboxes_do_not_share_a_cursor(provider, cfg):
    """The end-to-end consequence: separate cursors, separate stored mail."""
    from mailbot.storage import db
    from mailbot.agent import runner

    provider.account = "google"
    # Distinct ids, as two real mailboxes would have. (The fake provider
    # counts from 1 per instance, which is its own limitation, not the
    # agent's - see test_message_ids_are_unique_within_an_account.)
    provider.add_message(sender="a@x.com", subject="personal mail",
                         body="hi", id="19fda0eb83f4805e")
    other = type(provider)(account="google:work")
    other.add_message(sender="b@y.com", subject="work mail",
                      body="hi", id="1a0049c45ba7fea5")

    assert len(runner.fetch_new(provider, cfg=cfg)) == 1
    assert len(runner.fetch_new(other, cfg=cfg)) == 1

    # Separate cursors, and fetching must not move either.
    assert db.get_cursor("google", "inbox") is None
    assert db.get_cursor("google:work", "inbox") is None
    # Each inbox's mail is filed under its own account.
    with db.db() as c:
        n_p = c.execute("SELECT COUNT(*) n FROM messages WHERE account='google'").fetchone()["n"]
        n_w = c.execute("SELECT COUNT(*) n FROM messages WHERE account='google:work'").fetchone()["n"]
    assert (n_p, n_w) == (1, 1), (n_p, n_w)
    # And advancing one inbox's cursor leaves the other alone.
    db.set_cursor("google:work", "inbox", "x1")
    assert db.get_cursor("google", "inbox") is None
    assert db.get_cursor("google:work", "inbox") == "x1"


def test_message_ids_are_unique_within_an_account():
    """The same provider id in two inboxes must be two distinct messages.

    The messages table used to be keyed on the provider message id alone.
    Gmail ids are unique per mailbox but not guaranteed unique ACROSS
    mailboxes, so two inboxes sharing an id collided: the second message
    looked already-stored, was reported as seen, and was silently never
    triaged. The key is now (id, account).
    """
    from mailbot.storage import db

    base = {"thread_id": "t", "sender": "a@x.com", "subject": "s",
            "date": "2026-01-01T00:00:00Z", "label_ids": ["INBOX"]}
    assert db.upsert_message({**base, "id": "same-id", "account": "google"}) is True
    # Same id, different account: a different message, and therefore new.
    assert db.upsert_message({**base, "id": "same-id", "account": "google:work"}) is True
    # Re-fetching either one is still idempotent.
    assert db.upsert_message({**base, "id": "same-id", "account": "google"}) is False
    assert db.upsert_message({**base, "id": "same-id", "account": "google:work"}) is False

    with db.db() as c:
        rows = c.execute("SELECT account FROM messages WHERE id='same-id'").fetchall()
    assert sorted(r["account"] for r in rows) == ["google", "google:work"]

    # Each inbox sees only its own copy as awaiting triage.
    assert db.count_unprocessed("google") == 1
    assert db.count_unprocessed("google:work") == 1

    # Marking one processed must not touch the other inbox's copy.
    db.mark_processed("same-id", "google")
    assert db.count_unprocessed("google") == 0
    assert db.count_unprocessed("google:work") == 1


def test_upsert_account_never_blanks_a_known_address():
    """A provider with no resolved address must not erase the stored one.

    Every CLI command re-registers its providers, so an empty address on a
    cold start used to wipe the row permanently.
    """
    from mailbot.storage import db

    db.upsert_account("google:work", "me@work.com", "Work")
    db.upsert_account("google:work", "", "")
    got = db.get_account("google:work")
    assert got["address"] == "me@work.com"
    assert got["display_name"] == "Work"


def test_a_new_inbox_does_not_inherit_auto_send(monkeypatch):
    """A freshly added mailbox must not be trusted to send unattended.

    _providers() runs on every single command, so writing the global
    auto-send value into the account row there meant a per-inbox decision
    could never stick - and a newly added mailbox inherited auto-send
    before anyone had approved a single contact.
    """
    from mailbot import cli

    recorded = {}

    class P:
        account = "google:work"
        address = "me@work.com"
        display_name = "Work"
        auto_send = True          # the global setting is on
        calendar_enabled = True

        def valid(self):
            return True

        def resolve_address(self):
            return self.address

    class Cfg:
        agent = type("A", (), {"enabled_accounts": []})()

    monkeypatch.setattr(cli, "build_providers", lambda cfg: {"google:work": P()})
    monkeypatch.setattr(cli.db, "get_account", lambda i: None)   # brand new
    monkeypatch.setattr(cli.db, "upsert_account",
                        lambda i, a="", d="", **kw: recorded.update(
                            {"id": i, "address": a, "auto_send": kw.get("auto_send")}))
    assert "google:work" in cli._providers(Cfg())
    assert recorded["auto_send"] is False, recorded


def test_an_existing_inbox_keeps_its_own_auto_send(monkeypatch):
    """A per-inbox decision must survive every later command."""
    from mailbot import cli

    recorded = {}

    class P:
        account = "google:work"
        address = "me@work.com"
        display_name = "Work"
        auto_send = True          # global says on
        calendar_enabled = True

        def valid(self):
            return True

        def resolve_address(self):
            return self.address

    class Cfg:
        agent = type("A", (), {"enabled_accounts": []})()

    monkeypatch.setattr(cli, "build_providers", lambda cfg: {"google:work": P()})
    # A stored account that had auto-send deliberately turned off.
    monkeypatch.setattr(cli.db, "get_account",
                        lambda i: {"id": i, "auto_send": 0, "calendar": 1})
    monkeypatch.setattr(cli.db, "upsert_account",
                        lambda i, a="", d="", **kw: recorded.update(
                            {"auto_send": kw.get("auto_send")}))
    cli._providers(Cfg())
    assert recorded["auto_send"] == 0, recorded


def test_chat_history_is_scoped_to_one_inbox():
    """Two inboxes must not read each other's conversation.

    chat_history had no account column at all, so with a second inbox
    configured the agent answered "what did she say?" using the other
    mailbox's history. That is both wrong and a leak between profiles.
    """
    from mailbot.agent import chatlog

    chatlog.record("user", "the invoice question", account="google")
    chatlog.record("agent", "she said the 3rd", account="google")
    chatlog.record("user", "unrelated work thread", account="work")

    google = [t["content"] for t in chatlog.recent(account="google")]
    work = [t["content"] for t in chatlog.recent(account="work")]

    assert google == ["the invoice question", "she said the 3rd"]
    assert work == ["unrelated work thread"]

    # Forgetting one inbox's thread must not wipe the other's.
    assert chatlog.clear(account="google") == 2
    assert chatlog.recent(account="google") == []
    assert len(chatlog.recent(account="work")) == 1
