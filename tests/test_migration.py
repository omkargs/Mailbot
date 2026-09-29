"""Schema migration: rekeying messages and drafts to (id, account).

An existing install has a messages table keyed on the provider id alone. The
migration has to rebuild it without losing a single row, without dropping the
columns, and without running twice, because it runs on every command.
"""
from __future__ import annotations

import sqlite3

import pytest


# The schema as it shipped: id alone, inline PRIMARY KEY, with the trailing
# -- comments and table-level FOREIGN KEY the parser has to cope with.
OLD_MESSAGES = """CREATE TABLE messages (
        id            TEXT PRIMARY KEY,          -- provider message id
        account       TEXT NOT NULL,
        thread_id     TEXT,
        sender        TEXT,
        subject       TEXT,
        snippet       TEXT,
        body          TEXT,
        date          TEXT,
        label_ids     TEXT,                      -- JSON array
        has_attach    INTEGER NOT NULL DEFAULT 0,
        size_bytes    INTEGER,
        processed_at  TEXT,                      -- set once the agent has triaged it
        stored_at     TEXT NOT NULL,
        FOREIGN KEY (account) REFERENCES accounts(id)
    )"""

OLD_DRAFTS = """CREATE TABLE drafts (
        id            TEXT PRIMARY KEY,
        account       TEXT NOT NULL,
        run_id        INTEGER,
        in_reply_to   TEXT,
        to_addr       TEXT,
        subject       TEXT,
        body          TEXT,
        status        TEXT NOT NULL DEFAULT 'created',
        created_at    TEXT NOT NULL
    )"""


@pytest.fixture(autouse=True)
def restore_db_path():
    """These tests repoint db.DB_PATH at throwaway files. Put it back, or the
    next test in the session inherits a database with the old schema."""
    from mailbot.storage import db

    original = db.DB_PATH
    yield
    db.DB_PATH = original


def _pk(conn, table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall() if r[5]]


def _downgrade(path):
    """Build a database in the pre-migration shape, with data in it."""
    from mailbot.storage import db

    db.DB_PATH = path
    db.migrate()
    with db.db() as c:
        c.execute("DROP TABLE messages")
        c.execute("DROP TABLE drafts")
        c.execute("INSERT OR IGNORE INTO accounts (id, address, updated_at) VALUES ('google','a@x.com','t')")
        c.execute("INSERT OR IGNORE INTO accounts (id, address, updated_at) VALUES ('work','b@y.com','t')")
        c.execute(OLD_MESSAGES)
        c.execute(OLD_DRAFTS)
        c.execute(
            """INSERT INTO messages (id, account, thread_id, sender, subject,
                                      snippet, body, date, label_ids, stored_at)
               VALUES ('m1','google','t1','a@x.com','one','snip','body one',
                       '2026-01-01T00:00:00Z','["INBOX"]','t')"""
        )
        c.execute(
            """INSERT INTO messages (id, account, thread_id, sender, subject,
                                      snippet, body, date, label_ids, stored_at)
               VALUES ('m2','work','t2','b@y.com','two','snip','body two',
                       '2026-01-02T00:00:00Z','["INBOX"]','t')"""
        )
        c.execute(
            """INSERT INTO drafts (id, account, in_reply_to, to_addr, subject, body, created_at)
               VALUES ('d1','google','m1','a@x.com','re: one','ok','t')"""
        )
    return path


def test_migration_rekeys_messages_and_drafts(tmp_path, monkeypatch):
    from mailbot.storage import db

    path = _downgrade(tmp_path / "old.db")
    with db.db() as c:
        assert _pk(c, "messages") == ["id"], "fixture did not start in the old shape"

    db.migrate()

    with db.db() as c:
        assert _pk(c, "messages") == ["id", "account"]
        assert _pk(c, "drafts") == ["id", "account"]
        # No row lost, no data mangled.
        rows = {r["id"]: (r["account"], r["subject"], r["body"]) for r in
                c.execute("SELECT id, account, subject, body FROM messages")}
        assert rows == {
            "m1": ("google", "one", "body one"),
            "m2": ("work", "two", "body two"),
        }
        assert c.execute("SELECT COUNT(*) FROM drafts").fetchone()[0] == 1


def test_migration_preserves_every_column(tmp_path):
    """A rebuild that silently drops a column loses data the agent needs."""
    from mailbot.storage import db

    before = sqlite3.connect(_downgrade(tmp_path / "cols.db"))
    old_cols = [r[1] for r in before.execute("PRAGMA table_info(messages)")]
    before.close()

    db.migrate()

    with db.db() as c:
        assert [r[1] for r in c.execute("PRAGMA table_info(messages)")] == old_cols
        # Defaults and NOT NULL constraints survive too.
        row = c.execute("SELECT has_attach, stored_at FROM messages WHERE id='m1'").fetchone()
        assert row["has_attach"] == 0
        assert row["stored_at"] == "t"


def test_migration_is_idempotent(tmp_path):
    """migrate() runs on every command, so a second pass must be a no-op."""
    from mailbot.storage import db

    db.migrate()
    with db.db() as c:
        n = c.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    db.migrate()
    db.migrate()
    with db.db() as c:
        assert c.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == n
        assert _pk(c, "messages") == ["id", "account"]
    # And no leftover rebuild tables.
    with db.db() as c:
        assert c.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name LIKE '%_old_composite'"
        ).fetchone()[0] == 0


def test_migrated_db_accepts_a_colliding_id(tmp_path):
    """The whole point: the same provider id in two inboxes now both fit."""
    from mailbot.storage import db

    db.migrate()
    base = {"thread_id": "t", "sender": "a@x.com", "subject": "s",
            "date": "2026-01-01T00:00:00Z", "label_ids": ["INBOX"]}
    assert db.upsert_message({**base, "id": "dup", "account": "google"}) is True
    assert db.upsert_message({**base, "id": "dup", "account": "work"}) is True
    # ...and each inbox's cursor advances on its own row.
    db.mark_processed("dup", "google")
    assert db.count_unprocessed("work") == 1
