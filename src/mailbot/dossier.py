"""Dossier: who you are, mined from your own mailbox. Not a cloud profile.

GenMail sells an 'Email Brain' dossier built on their servers. Ours is
built at home from data we already store: contact volumes, activity
hours, the learned voice profile, and the approval record. Nothing new
is collected — this command only READS.

`mail-agent dossier` prints it for the current profile's inbox.
`mail-agent dossier --json` emits machines-readable output.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime


def _parse_hour(iso: str) -> int | None:
    try:
        return datetime.fromisoformat(iso).hour
    except (ValueError, TypeError):
        return None


def build_dossier(account: str, cfg) -> dict:
    from .storage import db

    db.migrate()
    contacts = db.list_contacts(account, limit=8)
    top = [{"address": c["address"], "sent": c.get("sent_count", 0),
            "received": c.get("received_count", 0),
            "auto": bool(c.get("auto_send_ok"))} for c in contacts]

    hours: Counter[int] = Counter()
    n_msgs = 0
    with db.db() as c:
        rows = c.execute(
            "SELECT date FROM messages WHERE account=? LIMIT 2000",
            (account,)).fetchall()
    for r in rows:
        h = _parse_hour(r["date"])
        if h is not None:
            hours[h] += 1
            n_msgs += 1
    peak = None
    if hours:
        best = hours.most_common(1)[0][0]
        peak = f"{best:02d}:00–{(best + 2) % 24:02d}:00"

    with db.db() as c:
        sent = c.execute(
            "SELECT COUNT(*) n FROM actions_log WHERE account=? AND action='send'",
            (account,)).fetchone()["n"]
        queued = c.execute(
            "SELECT COUNT(*) n FROM approvals WHERE account=? AND kind='send'",
            (account,)).fetchone()["n"]
        approved = c.execute(
            "SELECT COUNT(*) n FROM approvals WHERE account=? AND status='approved'",
            (account,)).fetchone()["n"]

    voice_lines: list[str] = []
    try:
        prof = cfg.brain_path() / f"profile-{account}.md"
        if prof.exists():
            voice_lines = [ln.strip() for ln in prof.read_text().splitlines()
                           if ln.strip()][:6]
    except Exception:
        pass

    return {
        "account": account,
        "messages_seen": n_msgs,
        "peak_hours": peak or "not enough data yet",
        "top_contacts": top,
        "sent_unattended": sent,
        "queued_for_you": queued,
        "approved_by_you": approved,
        "voice": voice_lines,
    }


def render(d: dict) -> str:
    lines = [f"Dossier — {d['account']}",
             f"  {d['messages_seen']} messages seen · peak {d['peak_hours']}"]
    if d["top_contacts"]:
        lines.append("  Key relationships:")
        for c in d["top_contacts"]:
            flag = "auto" if c["auto"] else "ask-first"
            lines.append(f"    {c['address']:<32} "
                         f"sent={c['sent']:<4} recv={c['received']:<4} [{flag}]")
    lines.append(f"  Sent unattended: {d['sent_unattended']} · "
                 f"queued: {d['queued_for_you']} · approved: {d['approved_by_you']}")
    if d["voice"]:
        lines.append("  Voice (mined from sent mail):")
        lines.extend(f"    {ln[:90]}" for ln in d["voice"])
    else:
        lines.append("  Voice: not learned yet — run `mail-agent brain`")
    return "\n".join(lines)


def cmd_dossier(args, cfg) -> int:
    from . import profiles as _profiles

    _profiles.ensure_migrated()
    cur = _profiles.get_current()
    account = cur["account"] if cur else "google"
    if getattr(args, "account", ""):
        account = args.account
    d = build_dossier(account, cfg)
    if getattr(args, "json", False):
        import json as _json

        print(_json.dumps(d, indent=2))
    else:
        tag = _profiles.header_for(cur, account)
        print(f"{tag} " if tag else "", end="")
        print(render(d))
    return 0
