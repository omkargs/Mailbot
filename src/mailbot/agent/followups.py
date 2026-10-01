"""Follow-up watches: never drop the ball, never nag forever.

Every send arms a watch on its thread. If nobody replies within
FOLLOWUP_DAYS, the scheduler fires once: the model drafts a nudge in the
user's voice, under the same gates as everything else (it sends or queues
by standing). Any incoming reply disarms the watch. Two nudges per thread
lifetime, then the watch retires silently.

The model drafts; this module decides when. No judgment calls live here —
just clocks and counters, which is why this file has no model import at
module level.
"""
from __future__ import annotations

import logging
import time
from typing import Any

log = logging.getLogger(__name__)

FOLLOWUP_DAYS = 3
MAX_NUDGES = 2


def _now() -> float:
    return time.time()


def watch(account: str, thread_id: str, message_id: str = "",
          to_addr: str = "", subject: str = "",
          days: float = FOLLOWUP_DAYS) -> None:
    """Arm (or re-arm) a watch. A new send in the thread moves the deadline;
    the nudge count survives — each waiting episode does not get a fresh
    nagging budget."""
    if not thread_id:
        return
    now = _now()
    from ..storage import db

    with db.db() as c:
        cur = c.execute(
            """INSERT INTO followups
               (account, thread_id, message_id, to_addr, subject, due_at,
                nudge_count, status, created_at, updated_at)
               VALUES (?,?,?,?,?,?,0,'pending',?,?)
               ON CONFLICT(account, thread_id) DO UPDATE SET
                 message_id=excluded.message_id, to_addr=excluded.to_addr,
                 subject=excluded.subject, due_at=excluded.due_at,
                 status='pending', updated_at=excluded.updated_at""",
            (account, thread_id, message_id, to_addr, subject[:200],
             now + days * 86400, now, now),
        )
        _ = cur


def resolve(account: str, thread_id: str) -> bool:
    """A reply landed — disarm. Returns True if a watch was open."""
    if not thread_id:
        return False
    from ..storage import db

    with db.db() as c:
        cur = c.execute(
            "UPDATE followups SET status='done', updated_at=? "
            "WHERE account=? AND thread_id=? AND status='pending'",
            (_now(), account, thread_id))
        return cur.rowcount > 0


def due(account: str, now: float | None = None) -> list[dict[str, Any]]:
    """Pending watches past their deadline, oldest first."""
    from ..storage import db

    now = _now() if now is None else now
    with db.db() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM followups WHERE account=? AND status='pending' "
            "AND due_at<=? ORDER BY due_at",
            (account, now))]


def open_watches(account: str | None = None) -> list[dict[str, Any]]:
    """Pending watches for /followups. Read-only."""
    from ..storage import db

    q = "SELECT * FROM followups WHERE status='pending'"
    params: list[Any] = []
    if account:
        q += " AND account=?"
        params.append(account)
    with db.db() as c:
        return [dict(r) for r in c.execute(q + " ORDER BY due_at", params)]


def arm_for_send(account: str, provider, in_reply_to: str = "",
                 to_addrs: list[str] | None = None, subject: str = "") -> None:
    """Arm a watch for a just-completed send. The thread is resolved from
    the message answered; fresh sends (no in_reply_to) carry no thread
    reference, so they arm nothing — a watch that can never resolve is a
    guaranteed false nudge. Never raises: arming must not break sending."""
    if not in_reply_to:
        return
    try:
        orig = provider.get_message(in_reply_to) or {}
        thread_id = orig.get("thread_id", "")
    except Exception:
        return
    if not thread_id:
        return
    try:
        watch(account, thread_id, in_reply_to,
              ", ".join(to_addrs or []), subject)
    except Exception as e:
        log.debug("followup arm failed: %s", type(e).__name__)


def _close(account: str, thread_id: str, nudged: bool) -> None:
    from ..storage import db

    with db.db() as c:
        if nudged:
            c.execute(
                "UPDATE followups SET status='done', nudge_count=nudge_count+1, "
                "updated_at=? WHERE account=? AND thread_id=?",
                (_now(), account, thread_id))
        else:
            c.execute(
                "UPDATE followups SET status='done', updated_at=? "
                "WHERE account=? AND thread_id=?",
                (_now(), account, thread_id))


def run_due(cfg, providers: dict[str, Any], notify=None) -> list[str]:
    """Fire due watches. Returns one operator-facing text per nudge.

    Always closes the watch it fires — one shot per arming, so a failing
    model can never turn this into a per-minute nag loop. The nudge itself
    goes through the act loop, so standing, injection and queue rules apply
    unchanged; a queued nudge re-arms naturally when approved and sent.
    """
    from .act import decide_and_act

    out: list[str] = []
    for _name, p in providers.items():
        account = getattr(p, "account", "") or ""
        try:
            watches = due(account)
        except Exception as e:
            log.warning("followup check failed for %s: %s", account, type(e).__name__)
            continue
        for w in watches:
            if w["nudge_count"] >= MAX_NUDGES:
                # Lifetime budget spent. Silent close — the operator was told
                # about the earlier ones; a notice about not-noticing is noise.
                _close(account, w["thread_id"], nudged=False)
                continue
            _close(account, w["thread_id"], nudged=True)
            data = (
                f"Follow-up watch fired.\n"
                f"To: {w['to_addr']}\n"
                f"Subject: {w['subject']}\n"
                f"Thread: {w['thread_id']}\n"
                f"Waiting: {FOLLOWUP_DAYS} days, no reply. "
                f"(Nudge {w['nudge_count'] + 1} of {MAX_NUDGES} for this thread.)"
            )
            try:
                text, _stats = decide_and_act(
                    data,
                    "Draft a short nudge in the user's voice for the thread above. "
                    "If there is nothing worth nudging, say so in one line and do nothing.",
                    cfg, p, notify=notify)
            except Exception as e:
                log.warning("followup nudge failed for %s: %s", w["thread_id"], type(e).__name__)
                continue
            if text and text.strip():
                out.append(text.strip())
    return out
