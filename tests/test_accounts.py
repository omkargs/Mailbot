"""Account isolation: one inbox in the schema, but the tables are
shared — per-account separation must hold anyway."""
from __future__ import annotations


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


