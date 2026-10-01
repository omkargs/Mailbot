"""Chat operations: the implementations behind each /command.

Separated from chat.py so the routing is testable without a live mailbox.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from typing import Any, Callable

from ..storage import db

log = logging.getLogger("mailbot.chatops")


def build_chat_ops(cfg, providers_factory: Callable[[], dict[str, Any]], notify=None) -> dict[str, Any]:
    """Return the callables chat.handle_text dispatches to."""

    def _providers() -> dict[str, Any]:
        return providers_factory()

    def _provider_for(account: str) -> Any:
        """The provider that owns an account's mail.

        Picking the first provider is wrong when more than one is configured:
        an approval for account B must execute against B's mailbox, or the
        agent sends from the wrong identity.
        """
        provs = _providers()
        for name, p in provs.items():
            if name == account or p.account == account:
                return p
        return None

    # ---------------------------------------------------------------- status
    def status() -> str:
        u = db.usage_today()
        pend = db.pending_approvals()
        with db.db() as c:
            row = c.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        lines = ["*mail-agent status*", ""]
        provs = _providers()
        lines.append(f"Accounts: {', '.join(provs) if provs else 'none connected'}")
        if row:
            lines.append(
                f"Last run: {row['triaged']} triaged, {row['drafted']} drafted, "
                f"{row['sent']} sent, {row['escalated']} waiting"
            )
        else:
            lines.append("No runs yet.")
        lines.append(f"Waiting on you: {len(pend)}")
        lines.append(f"Tokens today: {u['input_tokens']:,} in / {u['output_tokens']:,} out")
        return "\n".join(lines)

    # ----------------------------------------------------------------- brief
    def brief() -> str:
        from .brief import build_brief

        return build_brief(cfg)

    def quiet() -> str:
        from .brief import brief_quiet_threads

        return brief_quiet_threads(cfg)

    # ----------------------------------------------------------------- inbox
    def inbox() -> str:
        provs = _providers()
        if not provs:
            return "No account connected. Run mail-agent auth."
        p = next(iter(provs.values()))
        try:
            msgs = p.list_messages(folder="INBOX", limit=5)
        except Exception as e:
            return f"Could not read the inbox: {type(e).__name__}"
        if not msgs:
            return "Inbox is empty."
        # Count what is actually unprocessed in the store, rather than
        # guessing from the newest five.
        with db.db() as c:
            unread = c.execute(
                "SELECT COUNT(*) n FROM messages WHERE account=? AND processed_at IS NULL"
                " AND label_ids NOT LIKE '%SENT%'",
                (p.account,),
            ).fetchone()["n"]
        lines = [f"*Inbox* — {unread} awaiting triage", ""]
        for m in msgs:
            sender = (m.get("sender") or "?").split("@")[0][:22]
            lines.append(f"• {sender} — {(m.get('subject') or '(no subject)')[:44]}")
        lines.append("")
        lines.append("/scan to have me work through them.")
        return "\n".join(lines)

    # ------------------------------------------------------------------ scan
    def scan() -> str:
        from .runner import scan as run_scan

        provs = _providers()
        if not provs:
            return "No account connected. Run mail-agent auth."
        out = []
        for p in provs.values():
            res = run_scan(p, cfg, notify=notify)
            new = res.get("new", 0)
            if new:
                st = res.get("stats", {})
                out.append(
                    f"{p.account}: {new} new — {st.get('drafted', 0)} drafted, "
                    f"{st.get('sent', 0)} sent, {st.get('escalated', 0)} waiting on you"
                )
            else:
                out.append(f"{p.account}: nothing new")
        return "\n".join(out)

    # ---------------------------------------------------------------- drafts
    def drafts() -> str:
        pend = db.pending_approvals()
        if not pend:
            return "Nothing waiting for approval."
        lines = [f"*Waiting on you* ({len(pend)})", ""]
        for p in pend[:10]:
            lines.append(f"`{p['id']}` [{p['account']}]")
            lines.append(f"   {p['reason'][:80]}")
        lines.append("")
        lines.append(f"/approve <id>  or  /discard <id>")
        return "\n".join(lines)

    def approve(approval_id: str, yes: bool) -> str:
        from .runner import run_approval

        with db.db() as c:
            row = c.execute("SELECT account FROM approvals WHERE id=?", (approval_id,)).fetchone()
        if not row:
            return f"No pending approval with id {approval_id!r}."
        p = _provider_for(row["account"])
        if not p:
            return f"Account {row['account']} is not connected. Run mail-agent auth."
        try:
            res = run_approval(p.account, p, cfg, approval_id, yes, notify=notify)
        except Exception as e:
            return f"Failed: {type(e).__name__}: {e}"
        if not res.get("ok"):
            return f"Could not: {res.get('error', 'unknown')}"
        return "Sent." if res.get("sent") else "Discarded."

    # ---------------------------------------------------------------- unsub
    def unsub(target: str) -> str:
        """Leave a sender: archive everything from them, show the exits.

        For legit lists you never read. The archive is the enforcement (their
        mail stops surfacing); the links are the cure (stop it at the source).
        Refuses anyone you know — unsubscribing from your boss is not a
        feature, it is an incident.
        """
        from ..agent import guards as _guards

        target = (target or "").strip().strip("`").lower()
        if not target:
            return "Usage: /unsub <sender> — archives their mail, shows exit links."
        provs = _providers()
        if not provs:
            return "No account connected."
        p = next(iter(provs.values()))
        contact = db.get_contact(p.account, target)
        if contact and (contact.get("sent_count") or contact.get("auto_send_ok")):
            return (f"{target} looks like someone you know "
                    f"(sent={contact.get('sent_count', 0)}). Not touching them — "
                    f"say which address you meant if I'm wrong.")
        try:
            found = p.search(query=f"from:{target}", limit=50)
        except Exception as e:
            return f"Could not search: {type(e).__name__}"
        n = 0
        links: list[str] = []
        for m in found:
            mid = m.get("id", "")
            if not mid:
                continue
            try:
                if p.archive(mid):
                    n += 1
            except Exception:
                continue
            if n == 1:
                try:
                    full = p.get_message(mid) or {}
                    links = _guards.find_unsub_links(full.get("body") or m.get("body", ""))
                except Exception:
                    pass
        db.log_action("unsub", p.account, target, detail=f"archived {n}")
        out = f"Purged {n} from {target}."
        out += "\nExit: " + (" | ".join(links) if links else "no link found — use Gmail's unsubscribe")
        return out

    def spam(message_id: str) -> str:
        """Report one message as spam. For the junk in front of you right
        now — the automatic rule handles the rest without being asked."""
        from .tools import ToolBox

        message_id = (message_id or "").strip().strip("`")
        if not message_id:
            return "Usage: /spam <message-id> — or let the scan rule handle bulk."
        provs = _providers()
        if not provs:
            return "No account connected."
        p = next(iter(provs.values()))
        box = ToolBox(p, cfg, run_id=0, notify=notify)
        out = box.run("report_spam", {"message_id": message_id})
        if out.get("ok"):
            return f"Reported as spam. Gmail learns from this."
        return f"Did not report: {out.get('error', 'unknown reason')}"

    def show(approval_id: str) -> str:
        """The full text of a queued item. Cards carry excerpts; approvals
        should never be granted on an excerpt, so the whole thing is one
        command away."""
        approval_id = (approval_id or "").strip().strip("`")
        if not approval_id:
            return "Usage: /show <id> — run /drafts to see the waiting ids."
        row = db.get_approval(approval_id)
        if not row:
            return f"No approval with id {approval_id!r}."
        try:
            payload = json.loads(row["payload"])
        except (ValueError, TypeError):
            payload = {}
        lines = [f"*{row['kind']}* `{row['id']}` [{row['account']}]",
                 f"status: {row['status']}",
                 f"why it waited: {row['reason'] or '—'}", ""]
        if row["kind"] == "send":
            lines.append(f"To: {', '.join(payload.get('to', []))}")
            lines.append(f"Subject: {payload.get('subject', '')}")
            lines.append("")
            lines.append(payload.get("body", "") or "(empty body)")
            atts = payload.get("attachments") or []
            if atts:
                lines.append("")
                lines.append(f"Attachments: {', '.join(atts)}")
        elif row["kind"] == "calendar_delete":
            lines.append(f"Delete event: {payload.get('event_id', '')}")
            lines.append(f"Reason: {payload.get('reason', '')}")
        elif row["kind"] == "calendar_invite":
            lines.append(f"Invite: {payload.get('summary', '')} ({payload.get('start', '')})")
            lines.append(f"Attendees: {', '.join(payload.get('attendees', []))}")
        else:
            lines.append(json.dumps(payload, indent=2)[:1500])
        lines += ["", f"/approve {row['id']}  or  /discard {row['id']}"]
        return "\n".join(lines)

    def approve_all() -> str:
        """Approve everything queued, in one word.

        Convenience, not a bypass. Each approval is still claimed atomically
        and re-validated at execution (kill switch, injection, hash), so a bulk
        approve cannot send something a single approve would have held. A
        summary names every id that went out, because "sent 3" without saying
        which 3 is not a report.
        """
        from .runner import run_approval

        pend = db.pending_approvals()
        if not pend:
            return "Nothing waiting for approval."
        sent: list[str] = []
        held: list[str] = []
        for p in pend:
            prov = _provider_for(p["account"])
            if not prov:
                held.append(f"{p['id']} (account not connected)")
                continue
            try:
                res = run_approval(p["account"], prov, cfg, p["id"], True, notify=None)
            except Exception as e:
                held.append(f"{p['id']} ({type(e).__name__})")
                continue
            (sent if res.get("sent") else held).append(p["id"])
        out = f"Sent {len(sent)}."
        if sent:
            out += "\n" + "\n".join(f"  ✓ {i}" for i in sent)
        if held:
            out += "\nStill held:\n" + "\n".join(f"  · {i}" for i in held)
        return out

    # -------------------------------------------------------------- security
    def security() -> str:
        """The authority model, in the operator's hands.

        Nine gates, in order, and a run of them is the reason a message was
        held. Printed rather than paraphrased: "the agent decided not to send"
        is not an answer anybody can act on.
        """
        from ..agent import guards

        j = cfg.jev
        url, key, model = j.endpoint(cfg.router)
        allowed = [r["address"] for r in db.list_contacts(limit=200) if r["auto_send_ok"]]
        lines = [
            "*Send authority* — the model proposes, this code decides.",
            "",
            f"send_mode: {cfg.agent.send_mode}"
            + ("  ← kill switch is ON, nothing sends at all"
               if cfg.agent.send_mode == "never" else ""),
            f"account auto-send: {'ON' if getattr(cfg.google, 'auto_send', False) else 'off'}",
            f"auto-send contacts: {', '.join(allowed) if allowed else 'none — everything queues'}",
            f"never-auto-send: {', '.join(cfg.agent.never_auto_send) or 'none'}",
            f"escalation keywords: {len(cfg.agent.escalation_keywords)} armed",
            "",
            "*Gates, in order* — each one found can only make it stricter:",
            "  1. kill switch  (send_mode=never)",
            "  2. never-list   (wins over the allowlist)",
            "  3. account authority",
            "  4. contact standing: allowlist, an approved contact, or an established two-way thread",
            "  5. prompt-injection signals in the content",
            "  6. escalation keywords (money, contract, credential, health)",
            "  7. attachments on an outbound reply",
            "  8. reply to a contact with no prior thread",
            "  9. body too short to have been individually written",
            "",
            f"*Decider* — Jev {'on' if j.enabled and url and key else 'off'}"
            + (f" at {url}/v1/systemone, model {model}" if url and key else
               " — unset, so the flagship reads every message"),
            "",
            "A queued send is re-checked at execution: kill switch, injection and",
            "address validity are all re-validated after you approve, because the",
            "world can change between the card and the send.",
        ]
        assert guards  # gates live here; referenced so the import is honest
        return "\n".join(lines)

    # ------------------------------------------------------------------ jev
    def jev(text: str = "") -> str:
        """The decider's live config, its recent verdicts, and how to tune it.

        A second brain nobody can inspect is just an unaccountable one. This
        shows the thresholds actually in force and what Jev last decided, so
        "why did it file that" has an answer that is not the source code.
        """
        from ..agent import jev as jev_mod
        from ..config import CONFIG_DIR

        import re as _re
        body = _re.sub(r"^/jev\s*", "", (text or "").strip())
        parts = body.split()
        tuning = {"conf": ("JEV_MIN_CONFIDENCE", "min_confidence"),
                  "needs": ("JEV_NEEDS_CUT", "needs_cut"),
                  "file": ("JEV_FILE_NEEDS_CUT", "file_needs_cut"),
                  "ask": ("JEV_ASK_P", "ask_prob_floor"),
                  "margin": ("JEV_MARGIN", "min_margin"),
                  "act": ("JEV_ACT_P", "act_p"),
                  "automargin": ("JEV_AUTO_MARGIN", "auto_margin")}
        if len(parts) >= 2 and parts[0] in tuning:
            env, attr = tuning[parts[0]]
            try:
                val = float(parts[1])
            except ValueError:
                return f"usage: /jev {parts[0]} <0-1>"
            if not 0.0 < val < 1.0:
                return f"{parts[0]} must be between 0 and 1."
            return (f"To set {env}={val}, edit {CONFIG_DIR / '.secrets'} and restart "
                    f"the daemon.\nCurrently {env}={getattr(cfg.jev, attr)}")

        j = cfg.jev
        url, key, model = j.endpoint(cfg.router)
        out = [
            "*Jev — the fast decider*",
            "",
            f"state: {'on' if jev_mod.enabled(cfg) else 'off'}",
            f"endpoint: {(url + '/v1/systemone') if url else 'unset'}",
            f"key: {'set' if key else 'UNSET'}",
            f"model: {model}",
            "",
            f"thresholds: conf ≥ {j.min_confidence} · needs > {j.needs_cut} · "
            f"file-needs > {j.file_needs_cut} · sensitivity ≥ 2 → ASK",
            f"margins: P(ask) ≥ {j.ask_prob_floor} → ASK · "
            f"margin < {j.min_margin} → ASK · "
            f"auto: P(act) ≥ {j.act_p} + margin ≥ {j.auto_margin}",
            f"calls: {'2 (double-check on)' if j.double_check else '1'}",
        ]
        if not jev_mod.enabled(cfg):
            out += ["", "Jev is off, so every message reaches the flagship "
                        "directly. Set JEV_ENABLED=1 and give it an endpoint."]
        with db.db() as c:
            rows = c.execute(
                "SELECT ts, detail FROM actions_log WHERE action='jev' "
                "ORDER BY id DESC LIMIT 5").fetchall()
        out += ["", "*last verdicts*"]
        out += [f"  {r['ts'][:19]}  {r['detail']}" for r in rows] or ["  none yet"]
        out += ["", "tune: /jev conf 0.7 · /jev ask 0.4 · /jev margin 0.2 · "
                    "/jev act 0.85"]
        return "\n".join(out)

    # ----------------------------------------------------------------- brain
    def brain() -> str:
        provs = _providers()
        if not provs:
            return "No account connected."
        out = []
        for name, p in provs.items():
            from ..brain.style import build_profile

            # Microsoft names the folder sentitems; Gmail uses SENT. Try the
            # provider's own name first, then fall back.
            msgs: list = []
            for folder in ("SENT", "sentitems"):
                try:
                    msgs = p.list_messages(folder=folder, limit=300)
                except Exception:
                    msgs = []
                if msgs:
                    break
            seeded = []
            for m in msgs:
                full = p.get_message(m["id"])
                if full:
                    m.update(full)
                    db.upsert_message(m)
                    db.bump_contact(name, m.get("sender", ""), sent=True)
                    seeded.append(m["id"])
            db.mark_processed_many(seeded, getattr(p, "account", ""))
            path = cfg.brain_path() / f"profile-{name}.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(build_profile(name))
            out.append(f"{name}: profile rebuilt from {len(msgs)} sent messages")
        return "\n".join(out)

    def voice() -> str:
        from ..brain.style import proposal_due, voice_state

        s = voice_state()
        if not s["total_samples"]:
            return "No voice samples yet. Every send teaches it — confirmations when "
            "it sends as drafted, corrections when you rewrite a draft instead."
        acc = f"{s['accuracy']:.0%}" if s["accuracy"] is not None else "n/a"
        lines = [
            "*Voice learning*",
            "",
            f"Samples: {s['total_samples']} ({s['confirmed']} sent unchanged, {s['edited']} edited by you)",
            f"Match rate: {acc}",
        ]
        if s["most_corrected"]:
            lines.append("")
            lines.append("What you change most:")
            for f, n in s["most_corrected"][:4]:
                lines.append(f"• {f} — {n}x")
        due, why = proposal_due()
        if due:
            lines += ["", f"Profile review due ({why}) — run /brain to rebuild from these samples."]
        else:
            need = 12 - s["total_samples"]
            if need > 0:
                lines += ["", f"{need} more send{'s' if need != 1 else ''} until the next profile review."]
        return "\n".join(lines)

    def skill(name: str) -> str:
        s = db.get_skill(name)
        if not s:
            avail = [x["name"] for x in db.list_skills()]
            return f"No skill named {name!r}." + (f" Available: {', '.join(avail)}" if avail else " None saved yet.")
        return f"Skill *{s['name']}* loaded: {s['description'] or s['instructions'][:100]}"

    # ------------------------------------------------------------------- ask
    def ask(question: str, chat: str = "owner") -> str:
        """Answer a plain-language question against the real mailbox.

        This is the path that makes the agent a teammate rather than a menu.
        It gets the same ToolBox as the triage loop, so the send gate applies
        unchanged: the model can read freely, but anything that sends or
        deletes is still gated by the user's own rules.

        Carries this chat's last few turns so follow-ups resolve. "Reply to
        him" only works if the agent still knows who "him" is — and "him" on
        Telegram is not "him" on the terminal, so history is per-chat.
        """
        from .ask import answer
        from .chatlog import recent

        provs = _providers()
        if not provs:
            return "No mailbox is connected. Run mail-agent auth."
        p = next(iter(provs.values()))
        q = question
        try:
            from .learning import pending_proposal

            outstanding = pending_proposal(p.account)
        except Exception:
            outstanding = None
        if outstanding and q.strip().lower() in (
                "yea", "yeah", "yes", "yep", "yup", "ok", "okay", "sure",
                "do it", "go ahead", "please do"):
            # A bare yes answers the outstanding proposal — the standing
            # change — not the last message. Say so explicitly, because the
            # model cannot see the proposal otherwise and "yea" would draft
            # yet another reply instead of confirming anything.
            q = (f"{question}\n\n[System note: you previously proposed letting "
                 f"replies to {outstanding} go out without asking. This yes "
                 f"means that. Call set_contact_permission for {outstanding}.]")
        return answer(q, cfg, p, history=recent(chat=chat), notify=notify)

    # -------------------------------------------------------------- schedule
    def schedule(text: str) -> str:
        """Set a job from plain language, e.g. '/schedule brief at 5'.

        The whole message is passed, not just the argument, because the parser
        needs the surrounding words to know what kind of job this is.
        """
        import uuid
        from .schedule import ScheduleError, parse

        request = re.sub(r"^/(schedule|remind|at)\s*", "", text or "").strip()
        if not request:
            return ("Tell me what and when, e.g. `/schedule full inbox brief at 5` "
                    "or `/schedule check the inbox at 7 every morning`.")
        try:
            spec = parse(request)
        except ScheduleError as e:
            return str(e)

        provs = _providers()
        account = next(iter(provs), "google")
        jid = f"job_{uuid.uuid4().hex[:10]}"
        db.create_job(jid, account, spec["kind"], at_time=spec["at_time"],
                      at_minutes=spec["in_minutes"], repeat=spec["repeat"],
                      prompt=spec["prompt"])

        if spec["in_minutes"] is not None:
            from datetime import datetime, timedelta
            fire = (datetime.now() + timedelta(minutes=spec["in_minutes"])).strftime("%H:%M")
            with db.db() as c:
                c.execute("UPDATE scheduled_jobs SET at_time=? WHERE id=?", (fire, jid))
            when = f"in {spec['in_minutes']} minutes"
        else:
            when = {"daily": "every day at", "weekly": "every week at"}.get(
                spec["repeat"], "once at") + f" {spec['at_time']}"
        db.log_action("schedule", account, jid, detail=request[:200])
        return f"Scheduled *{spec['kind']}* — {when}.\nid `{jid}`\n\n/cancel {jid} to drop it."

    def tasks() -> str:
        jobs = db.list_jobs()
        if not jobs:
            return ("Nothing scheduled. Try `/schedule full inbox brief at 5` "
                    "or `/schedule check the inbox at 7 every morning`.")
        lines = [f"*Scheduled jobs* ({len(jobs)})", ""]
        for j in jobs:
            rep = {"daily": "every day", "weekly": "every week"}.get(j["repeat"], "once")
            lines.append(f"`{j['id']}` — {j['kind']} at {j['at_time']} ({rep})")
            if j["prompt"]:
                lines.append(f"    {j['prompt'][:70]}")
        lines.append("")
        lines.append("/cancel <id> to remove one.")
        return "\n".join(lines)

    def cancel(job_id: str) -> str:
        job_id = job_id.strip().strip("`")
        ok = db.cancel_job(job_id)
        return f"Cancelled `{job_id}`." if ok else f"No enabled job with id `{job_id}`."

    def reset(chat: str = "owner") -> str:
        """Drop the conversation. Use this when changing topics, so an old
        'him' or 'that one' cannot bleed into a new question."""
        from .chatlog import clear

        n = clear(chat=chat)
        return f"Forgot {n} earlier message{'s' if n != 1 else ''}. Fresh start."

    return {
        "status": status,
        "ask": ask,
        "brief": brief,
        "quiet": quiet,
        "inbox": inbox,
        "scan": scan,
        "drafts": drafts,
        "approve": approve,
        "approve_all": approve_all,
        "show": show,
        "unsub": unsub,
        "spam": spam,
        "jev": jev,
        "security": security,
        "brain": brain,
        "voice": voice,
        "skill": skill,
        "schedule": schedule,
        "tasks": tasks,
        "cancel": cancel,
        "reset": reset,
    }
