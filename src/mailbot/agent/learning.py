"""Approval learning: auto-send is EARNED, not granted.

Rule: when the owner approves sends to the same address twice (no denials
in between), the agent proposes auto-send ONCE — "want me to reply to
them without asking from now on?" The owner confirms in chat (the existing
chat-origin permission path), never by silence.

Everything lives in SQLite (approvals + actions_log tables). No model
call, no cloud, no new collection. A denial breaks the streak; a prior
proposal is never repeated.
"""
from __future__ import annotations

import json

STREAK_TO_PROPOSE = 2


def _send_history(account: str, address: str, limit: int = 10) -> list[str]:
    """Recent send-approval outcomes for one recipient, newest first."""
    from ..storage import db

    out: list[str] = []
    with db.db() as c:
        rows = c.execute(
            "SELECT payload, status FROM approvals WHERE account=? AND kind='send'"
            " ORDER BY created_at DESC LIMIT ?",
            (account, limit)).fetchall()
    for r in rows:
        try:
            to = json.loads(r["payload"]).get("to", [])
        except (ValueError, TypeError):
            continue
        if address.lower() in [str(t).lower() for t in to]:
            out.append(r["status"])
    return out


def _already_proposed(account: str, address: str) -> bool:
    from ..storage import db

    with db.db() as c:
        row = c.execute(
            "SELECT 1 FROM actions_log WHERE account=? AND action='auto_send_proposed'"
            " AND target=? LIMIT 1",
            (account, address)).fetchone()
    return row is not None


def maybe_suggest(account: str, address: str) -> str | None:
    """Proposal text when the streak earns it, else None. Never repeats."""
    from ..storage import db

    hist = _send_history(account, address)
    streak = 0
    for s in hist:
        if s == "approved":
            streak += 1
        else:
            break
    if streak < STREAK_TO_PROPOSE or _already_proposed(account, address):
        return None
    contact = db.get_contact(account, address)
    if contact and contact.get("auto_send_ok"):
        return None
    db.log_action("auto_send_proposed", account, address,
                  detail=f"approved {streak}x in a row")
    name = address.split("@")[0]
    return (f"You've approved {streak} sends to {address} in a row. "
            f"Want me to reply to {name} without asking from now on? "
            f"Say 'add {address} to auto-send' — or ignore this and I'll keep asking.")
