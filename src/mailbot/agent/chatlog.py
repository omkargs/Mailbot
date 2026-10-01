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

# Generous on purpose: long sagas (a draft, a correction, a "yea", an
# approval) span many turns, and anything outside the window never happened
# as far as the model is concerned. Twelve turns is ~6 exchanges — cheap in
# tokens, expensive to lose.
MAX_TURNS = 12
MAX_CHARS = 1200       # truncate a long reply; the gist is what matters

SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_history (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    role    TEXT NOT NULL,          -- 'user' | 'agent'
    content TEXT NOT NULL
);
"""

# A conversation is scoped to the inbox it happened in AND the chat it
# happened on. With one account there is nothing to scope and the account
# key is "". With two inboxes, an unscoped log means the agent answers "what
# did she say?" using the other mailbox's history - which is both wrong and
# a privacy leak between profiles. With two chats (Telegram + terminal),
# an unscoped log means the terminal sees what you told Telegram — same
# leak, smaller room. The key is always both.
_COLUMNS = [
    "ALTER TABLE chat_history ADD COLUMN account TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE chat_history ADD COLUMN chat TEXT NOT NULL DEFAULT 'owner'",
]

# Chat identities. Telegram's owner chat is the control channel; the terminal
# REPL is a second one. Discord/Slack are notify-only and never write here.
CHAT_OWNER = "owner"
CHAT_TERMINAL = "terminal"


def _current_account() -> str:
    """The account this conversation belongs to (the active profile)."""
    try:
        from .. import profiles as _p

        prof = _p.current_profile()
        return (prof.get("account", "") if prof else "") or ""
    except Exception:
        return ""


def _key(account: str, chat: str = CHAT_OWNER) -> tuple[str, str]:
    return (account if account else _current_account(), chat or CHAT_OWNER)


def _ensure() -> None:
    with db.db() as c:
        c.execute(SCHEMA)
        cols = {r["name"] for r in c.execute("PRAGMA table_info(chat_history)")}
        for stmt in _COLUMNS:
            col = stmt.split("ADD COLUMN")[1].split()[0]
            if col not in cols:
                c.execute(stmt)


def record(role: str, content: str, account: str = "", chat: str = CHAT_OWNER) -> None:
    """Append one turn. Never let a logging failure break a conversation."""
    if not content or not content.strip():
        return
    try:
        _ensure()
        acct, ch = _key(account, chat)
        with db.db() as c:
            c.execute(
                "INSERT INTO chat_history (ts, role, content, account, chat) VALUES (?,?,?,?,?)",
                (db.now(), role, content.strip()[:MAX_CHARS], acct, ch))
    except Exception as e:
        log.warning("could not record chat history: %s", type(e).__name__)


def recent(limit: int = MAX_TURNS, account: str = "",
           chat: str = CHAT_OWNER) -> list[dict[str, str]]:
    """The last few turns for THIS inbox on THIS chat, oldest first."""
    try:
        _ensure()
        acct, ch = _key(account, chat)
        with db.db() as c:
            rows = c.execute(
                "SELECT role, content FROM chat_history WHERE account=? AND chat=? "
                "ORDER BY id DESC LIMIT ?",
                (acct, ch, limit),
            ).fetchall()
    except Exception as e:
        log.warning("could not read chat history: %s", type(e).__name__)
        return []
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def clear(account: str = "", chat: str = CHAT_OWNER) -> int:
    """Forget the conversation. Used by /reset and after a topic change.

    Scoped to one inbox on one chat: forgetting the Work thread must not wipe
    Personal, and clearing the terminal must not wipe Telegram.
    """
    try:
        _ensure()
        acct, ch = _key(account, chat)
        with db.db() as c:
            cur = c.execute("DELETE FROM chat_history WHERE account=? AND chat=?",
                            (acct, ch))
        return cur.rowcount
    except Exception as e:
        log.warning("could not clear chat history: %s", type(e).__name__)
        return 0
