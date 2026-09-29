"""Conversation memory.

Without this the agent has amnesia between messages: ask "what did msk say"
and then "reply to him" gets "who's him?" — because each message was a
fresh, context-free call. Pronouns, "the msk one", "that invoice" all need
the last few turns.

Persisted to SQLite rather than kept in a dict so a restart does not wipe the
thread mid-conversation, which is exactly when a user is most confused.
"""
from __future__ import annotations

import json
import logging

from ..storage import db

log = logging.getLogger(__name__)

MAX_TURNS = 8          # roughly the last 4 exchanges
MAX_CHARS = 1200       # truncate a long reply; the gist is what matters

SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_history (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    role    TEXT NOT NULL,          -- 'user' | 'agent'
    content TEXT NOT NULL
);
"""

# A conversation is scoped to the inbox it happened in. With one account
# there is nothing to scope and the key is "". With two, an unscoped log
# means the agent answers "what did she say?" using the other mailbox's
# history - which is both wrong and a privacy leak between profiles.
_COLUMN = """
ALTER TABLE chat_history ADD COLUMN account TEXT NOT NULL DEFAULT ''
"""


def _current_account() -> str:
    """The account this conversation belongs to (the active profile)."""
    try:
        from .. import profiles as _p

        prof = _p.current_profile()
        return (prof.get("account", "") if prof else "") or ""
    except Exception:
        return ""


def _key(account: str) -> str:
    return account if account else _current_account()


def _ensure() -> None:
    with db.db() as c:
        c.execute(SCHEMA)
        cols = {r["name"] for r in c.execute("PRAGMA table_info(chat_history)")}
        if "account" not in cols:
            c.execute(_COLUMN)


def record(role: str, content: str, account: str = "") -> None:
    """Append one turn. Never let a logging failure break a conversation."""
    if not content or not content.strip():
        return
    try:
        _ensure()
        with db.db() as c:
            c.execute(
                "INSERT INTO chat_history (ts, role, content, account) VALUES (?,?,?,?)",
                (db.now(), role, content.strip()[:MAX_CHARS], _key(account)))
    except Exception as e:
        log.warning("could not record chat history: %s", type(e).__name__)


def recent(limit: int = MAX_TURNS, account: str = "") -> list[dict[str, str]]:
    """The last few turns for THIS inbox, oldest first, ready for the API."""
    try:
        _ensure()
        with db.db() as c:
            rows = c.execute(
                "SELECT role, content FROM chat_history WHERE account=? "
                "ORDER BY id DESC LIMIT ?",
                (_key(account), limit),
            ).fetchall()
    except Exception as e:
        log.warning("could not read chat history: %s", type(e).__name__)
        return []
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def clear(account: str = "") -> int:
    """Forget the conversation. Used by /reset and after a topic change.

    Scoped to one inbox: forgetting the Work thread must not wipe Personal.
    """
    try:
        _ensure()
        with db.db() as c:
            cur = c.execute("DELETE FROM chat_history WHERE account=?", (_key(account),))
        return cur.rowcount
    except Exception as e:
        log.warning("could not clear chat history: %s", type(e).__name__)
        return 0
