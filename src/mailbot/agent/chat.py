"""Chat commands — the operator's remote control.

Telegram is the control channel; email is the work channel. You message the
agent here, it acts on your mailbox, and results come back here.

Commands are deliberately small and explicit. Anything that could send mail
routes through the normal approval queue; a chat message can never bypass it.
"""
from __future__ import annotations

import json
import logging
import shlex
from typing import Any

log = logging.getLogger("mailbot.chat")

HELP = """\
*mail-agent* — your inbox, on autopilot.

*/status* — is it alive, what did it do
*/brief* — the morning digest
*/quiet* — threads going cold
*/inbox* — unread count and the 5 newest
*/scan* — process new mail now
*/drafts* — drafts waiting for you
*/approve <id>* — send a queued reply
*/approve all* — send everything queued (each still re-validated)
*/discard <id>* — drop a queued reply
*/show <id>* — read the full queued text before you decide
*/unsub <sender>* — stop hearing from them, show exit links
*/spam <id>* — report one message as spam
*/followups* — threads still waiting on someone else
*/jev* — the fast decider: state, thresholds, last verdicts
*/security* — the nine send gates, in order
*/brain* — rebuild the voice profile
*/voice* — how well it has learned your writing
*/schedule <what and when>* — e.g. "brief at 5", "inbox at 7 every morning"
*/tasks* — list scheduled jobs
*/cancel <id>* — cancel a scheduled job
*/skill <name>* — run a saved automation
*/whoami* — which inbox and brain you're talking to
*/help* — this message
"""


def handle_text(
    text: str,
    cfg,
    providers: dict[str, Any],
    chat_ops: dict[str, Any],
    chat: str = "owner",
) -> str | None:
    """Route one inbound chat message. Returns the reply text, or None to stay silent.

    `chat` is which conversation this is (Telegram owner chat, terminal,
    ...). Telegram and the terminal run this same function with different
    chat keys, so both share every command and both remember separately.
    """
    t = (text or "").strip()
    if not t:
        return None

    if not t.startswith("/"):
        # Plain language, not a command. Answer it against the real mailbox
        # with the same tools the triage loop uses, under the same send gate.
        from .chatlog import record

        record("user", t, chat=chat)
        reply = chat_ops["ask"](t, chat)
        if reply:
            record("agent", reply, chat=chat)
        return reply

    try:
        parts = shlex.split(t)
    except ValueError:
        parts = t.split()
    cmd = parts[0].lower().lstrip("/")
    arg = parts[1] if len(parts) > 1 else ""

    if cmd in ("help", "start"):
        return HELP

    if cmd == "status":
        return chat_ops["status"]()

    if cmd == "brief":
        return chat_ops["brief"]()

    if cmd == "quiet":
        return chat_ops["quiet"]()

    if cmd == "inbox":
        return chat_ops["inbox"]()

    if cmd == "scan":
        return chat_ops["scan"]()

    if cmd == "drafts":
        return chat_ops["drafts"]()

    if cmd in ("approve", "discard", "deny"):
        if cmd == "approve" and arg.lower() == "all":
            return chat_ops["approve_all"]()
        if not arg:
            # Exactly one thing waiting and the operator said "send it" —
            # asking them to copy an id they can already see is bureaucracy.
            # Anything else still needs the id named.
            from ..storage import db as _db

            pend = _db.pending_approvals()
            if cmd == "approve" and len(pend) == 1:
                return chat_ops["approve"](pend[0]["id"], True)
            return (f"Usage: /{cmd} <id> — or /approve all — "
                    f"run /drafts to see the waiting ids.")
        return chat_ops["approve"](arg, cmd in ("approve",))

    if cmd == "show":
        return chat_ops["show"](arg)

    if cmd in ("unsub", "unsubscribe"):
        return chat_ops["unsub"](arg)

    if cmd == "spam":
        return chat_ops["spam"](arg)

    if cmd in ("followups", "waiting", "nudges"):
        return chat_ops["followups"]()

    if cmd == "jev":
        return chat_ops["jev"](t)

    if cmd == "security":
        return chat_ops["security"]()

    if cmd == "brain":
        return chat_ops["brain"]()

    if cmd == "voice":
        return chat_ops["voice"]()

    if cmd in ("schedule", "remind", "at"):
        return chat_ops["schedule"](t)

    if cmd in ("tasks", "jobs"):
        return chat_ops["tasks"]()

    if cmd in ("cancel", "unschedule"):
        if not arg:
            return "Usage: /cancel <job-id> — run /tasks to see the ids."
        return chat_ops["cancel"](arg)

    if cmd == "skill":
        if not arg:
            return "Usage: /skill <name>"
        return chat_ops["skill"](arg)

    if cmd in ("reset", "forget", "clear"):
        return chat_ops["reset"](chat)

    if cmd in ("profiles", "change-profile", "profile"):
        # Multi-inbox profiles were removed: one inbox, deliberately. A
        # second inbox doubled the authority surface for nobody's benefit.
        return ("Single inbox — profiles were removed. "
                "This is your only mailbox, and every command already acts on it.")

    if cmd == "whoami":
        provs = providers or {}
        p = next(iter(provs.values()), None)
        addr = (getattr(p, "address", "") or "") if p is not None else ""
        acct = (getattr(p, "account", "") or "") if p is not None else "google"
        lines = ["You are talking to your inbox agent.",
                 f"inbox: {addr or acct}",
                 f"brain: {cfg.router.model}"]
        try:
            from pathlib import Path as _P

            prof = _P(cfg.brain_path()) / f"profile-{acct}.md"
            lines.append(f"voice: {'learned' if prof.exists() else 'not learned yet — /brain'}")
        except Exception:
            pass
        return "\n".join(lines)

    return f"Unknown command /{cmd}. Try /help."
