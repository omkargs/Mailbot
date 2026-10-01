"""The agent loop.

Cost control, in order of impact:
  1. Prompt caching on the frozen prefix (system + tools). The profile and
     tool schemas are byte-stable across runs, so this is a real hit.
  2. Batch classification — one call for N messages, not N calls.
  3. UID cursor + processed_at, so nothing is ever re-processed.
  4. Daily token cap, enforced before the call, not after.

Volatile content (timestamps, new message bodies) lives in the user turn,
never in the system prompt. A timestamp in the system prompt silently
invalidates every cache read.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from ..brain import style as brain_style
from ..config import Config
from ..providers.base import Attachment, MailProvider
from ..storage import db
from . import guards
from . import jev
from . import triage
from .client import build_client, guarded_call, handle_refusal, usage_to_dict
from .tools import TOOLS, ToolBox, build_system_prompt

log = logging.getLogger(__name__)

MAX_ITERATIONS = 12


def _cached_tool_list() -> list[dict[str, Any]]:
    """Deterministic order and a cache breakpoint on the last tool.

    Tool order is part of the cache key, so it must never vary between runs.
    """
    tools = sorted(TOOLS, key=lambda t: t["name"])
    tools[-1] = {**tools[-1], "cache_control": {"type": "ephemeral"}}
    return tools


def _system_blocks(prompt: str) -> list[dict[str, Any]]:
    """Split the prompt so the stable prefix is cached and the volatile tail is not."""
    marker = "\n# Per-run context\n"
    if marker in prompt:
        stable, tail = prompt.split(marker, 1)
    else:
        stable, tail = prompt, ""
    blocks = [{"type": "text", "text": stable, "cache_control": {"type": "ephemeral"}}]
    if tail:
        blocks.append({"type": "text", "text": marker + tail})
    return blocks


def build_prompt(account: str, cfg: Config) -> str:
    """Assemble the cached system prompt. No timestamps, no run counters."""
    # Prefer the account-specific profile, fall back to the shared one.
    # brain/build_profile writes profile-<account>.md, so a per-account file is
    # the common case and the bare profile.md is a manual override.
    path = cfg.brain_path() / f"profile-{account}.md"
    if not path.exists():
        path = cfg.brain_path() / "profile.md"
    profile = path.read_text() if path.exists() else "_No style profile yet. Run `mail-agent brain`._"
    contacts = db.list_contacts(account, limit=100)
    skills = db.list_skills()
    return build_system_prompt(profile, account, contacts, skills)


def run_once(
    account: str,
    provider: MailProvider,
    cfg: Config,
    trigger: str = "scan",
    notify=None,
    messages: list[dict[str, Any]] | None = None,
    model: str | None = None,
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One agent pass over the pending messages for an account.

    `model` is the profile override — the flagship for this inbox. None
    means the global model (legacy path, unchanged).
    """
    """One agent pass over the pending messages for an account."""
    usage = db.usage_today()
    if usage["input_tokens"] + usage["output_tokens"] >= cfg.agent.daily_token_cap:
        log.warning("daily token cap reached (%d); skipping run", cfg.agent.daily_token_cap)
        if notify:
            notify("Token cap for today reached. No mail processed.")
        return {"status": "capped"}

    run_id = db.start_run(account, trigger, model or cfg.router.model)
    # A run that dies must say so. It used to leave status NULL with an
    # empty error, so the runs table held a run that had neither
    # succeeded nor failed - and anything that retries on NULL would
    # retry a run that already did partial work.
    try:
        client = build_client(cfg.router)
        # Email-driven: standing authority changes are refused in this context.
        box = ToolBox(provider, cfg, run_id, notify=notify,
                      allow_permission_change=False, profile=profile)

        pending = messages if messages is not None else db.unprocessed(account, limit=cfg.agent.max_drafts_per_run)
        if not pending:
            db.finish_run(run_id, status="ok")
            return {"status": "empty", "run_id": run_id}

        # Two brains. Jev is the cheap one: System-One sorts each message in
        # milliseconds for cents, and the flagship only ever sees what deserves
        # a real reply. If Jev is unconfigured or errors, the small triage model
        # is the fallback; if that fails too, the full flagship pass runs. Mail
        # is never dropped to save money.
        if jev.enabled(cfg):
            try:
                verdicts = {m["id"]: jev.decide(m, cfg) for m in pending[:12]}
                for v in verdicts.values():
                    db.log_action("jev", account, v.verdict,
                                  detail=f"conf={v.confidence:.2f} {v.reason}",
                                  actor="system")
                pending, _archived = jev.prune(pending, verdicts, cfg, provider, box)
            except Exception as e:
                log.warning("jev pass failed (%s); falling back", type(e).__name__)
                pending = [m for m in pending if "_jev" not in m]
        elif triage.wanted(cfg):
            try:
                verdicts = triage.classify(pending[:12], cfg)
                pending, _archived = triage.prune(pending, verdicts, cfg, provider, box)
            except Exception as e:
                log.warning("triage pass failed (%s); full flagship pass", type(e).__name__)
        if not pending:
            db.finish_run(run_id, triaged=box.stats["triaged"],
                          input_tokens=0, output_tokens=0, status="ok")
            n = box.stats["triaged"]
            return {"status": "ok", "run_id": run_id,
                    "summary": f"Filed {n} newsletter(s) — nothing needs you.",
                    "stats": box.stats,
                    "usage": {"input_tokens": 0, "output_tokens": 0,
                              "cache_read": 0, "cache_write": 0}}

        # Jev said ASK: a human is needed. Escalate here rather than paying the
        # flagship to read a message the cheap decider already ruled out of
        # scope. This is the whole point of the two-brain split, and it is safe
        # only because ASK is the fail-closed direction — a Jev failure lands
        # here too, so the worst case is a held message, never a lost one.
        #
        # Replies inherit their message's verdict: a high-margin ACT opens the
        # contact-standing gate for that reply through the executor. The box
        # carries the map so the tool layer can look it up by in_reply_to.
        box.jev_by_id = {m["id"]: m["_jev"] for m in pending if "_jev" in m}
        pending, held = _hold_jev_asks(pending, box, account)
        if not pending:
            db.finish_run(run_id, triaged=box.stats["triaged"],
                          escalated=box.stats["escalated"],
                          input_tokens=0, output_tokens=0, status="ok")
            return {"status": "ok", "run_id": run_id,
                    "summary": f"{box.stats['escalated']} thing(s) need you.",
                    "stats": box.stats,
                    "usage": {"input_tokens": 0, "output_tokens": 0,
                              "cache_read": 0, "cache_write": 0}}

        system = _system_blocks(build_prompt(account, cfg))

        # Volatile context lives here, after the last cache breakpoint.
        # Bodies come with the listing. Without them the model calls get_message
        # once per message, and each of those is a sequential round trip.
        ctx = ["# Per-run context", f"New messages awaiting triage: {len(pending)}", ""]
        ctx.append("Full bodies are included so you do not need to fetch them one by one.")
        ctx.append("")
        for m in pending[:12]:
            body = (m.get("body") or "").strip()
            # Sender and subject are attacker-controlled. A subject like "ignore
            # previous instructions" sitting outside the fence reads as an
            # instruction; inside, it reads as data.
            ctx.append(guards.fence(
                f"from: {m['sender']}  date: {m.get('date', '')}\n"
                f"subject: {m.get('subject', '')[:120]}", "headers"))
            if "_triage" in m:
                ctx.append(f"triage: {m['_triage'].get('v', 'human')} — {m['_triage'].get('why', '')}")
            v = m.get("_jev")
            if v is not None:
                # Jev's extra signals are context, not instructions. The
                # flagship still decides the reply; this tells it how much care
                # the reply deserves and who it is going to.
                bits = [f"jev={v.verdict} ({v.confidence:.2f}, margin {v.margin:.2f})"]
                if v.importance != "normal":
                    bits.append(f"importance={v.importance}")
                if v.deadline:
                    bits.append(f"deadline={v.deadline:.0f}/3")
                if v.needs_reply:
                    bits.append("they are waiting on a reply")
                if v.draft_tier:
                    bits.append(f"draft with {v.draft_tier} care")
                if v.reply_shape == "short":
                    bits.append("a sentence or two closes it — do not write an essay")
                if v.money >= 0.5 or v.commitment >= 0.5:
                    bits.append("stakes: money or a commitment is involved — escalate, do not send")
                if v.emotion >= 2:
                    bits.append("emotionally loaded — escalate, do not send")
                if v.auto_ok:
                    bits.append("standing: routine and consequence-free, "
                                "send_message may go out unattended")
                ctx.append("; ".join(bits))
                reg = brain_style.register_for(m.get("sender", ""))
                if reg:
                    ctx.append(f"register with this person: {reg}")
            if body:
                ctx.append(guards.fence(body[:1500], "body"))
            else:
                ctx.append(guards.fence((m.get("snippet") or "")[:300], "snippet"))
            ctx.append(f"message id: {m['id']}")
            ctx.append("")

        listed, unlisted = pending[:12], pending[12:]
        if unlisted:
            ctx.append("Not yet read, ids only — fetch in one batched get_message call:")
            ctx.append(", ".join(m["id"] for m in unlisted[:18]))

        ctx.append(
            "\nDecide for each: triage it, reply where a reply is genuinely warranted, "
            "or escalate. Batch your work — pass several ids to one get_message call."
        )

        messages_param: list[dict[str, Any]] = [{"role": "user", "content": "\n".join(ctx)}]
        totals = {"input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_write": 0}
        final_text = ""

        completed = False
        for _ in range(MAX_ITERATIONS):
            resp = guarded_call(client, 
                model=model or cfg.router.model,
                max_tokens=cfg.router.max_tokens,
                system=system,
                tools=_cached_tool_list(),
                messages=messages_param,
            )
            refusal = handle_refusal(resp)
            if refusal:
                log.warning("refusal: %s", refusal)
                final_text = refusal
                break

            for k, v in usage_to_dict(resp.usage).items():
                totals[k] += v
            db.record_usage(
                usage_to_dict(resp.usage)["input_tokens"],
                usage_to_dict(resp.usage)["output_tokens"],
                usage_to_dict(resp.usage)["cache_read"],
                usage_to_dict(resp.usage)["cache_write"],
            )

            messages_param.append({"role": "assistant", "content": resp.content})

            if resp.stop_reason != "tool_use":
                final_text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
                completed = True
                break

            results = []
            for block in resp.content:
                if getattr(block, "type", "") != "tool_use":
                    continue
                out = box.run(block.name, block.input)
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(out, default=str)[:8000],
                    "is_error": not out.get("ok", True),
                })
            if not results:
                completed = True
                break
            messages_param.append({"role": "user", "content": results})
        else:
            # Hit MAX_ITERATIONS without finishing. Do not mark anything processed —
            # the next cycle should retry rather than silently drop the mail.
            log.warning("agent hit MAX_ITERATIONS (%d); messages left unprocessed", MAX_ITERATIONS)
            final_text = "stopped: iteration limit reached, mail left for the next run"

        # The closing summary is model-written, so it passes through the same
        # identity wash as chat replies. A summary that introduces itself as
        # the router's model undoes the whole "your agent" framing.
        from .ask import sanitize_identity

        final_text = sanitize_identity(final_text)
        if completed:
            for m in pending:
                db.mark_processed(m["id"], account)
        else:
            log.info("not marking %d message(s) processed; they will be retried", len(pending))

        db.finish_run(
            run_id,
            new_messages=len(pending),
            triaged=box.stats["triaged"],
            drafted=box.stats["drafted"],
            sent=box.stats["sent"],
            escalated=box.stats["escalated"],
            input_tokens=totals["input_tokens"],
            output_tokens=totals["output_tokens"],
            cache_read=totals["cache_read"],
            cache_write=totals["cache_write"],
            status="ok" if completed else "incomplete",
        )
        return {
            "status": "ok" if completed else "incomplete",
            "run_id": run_id,
            "summary": final_text,
            "stats": box.stats,
            "usage": totals,
        }
    except Exception as e:
        # Record what went wrong rather than letting the traceback be the
        # only record. The stored message is the exception type plus a
        # short prefix: a failed model call carries the whole prompt, and
        # the prompt carries attacker-controlled email bodies.
        err = f"{type(e).__name__}: {str(e)[:200]}"
        log.error("run %s failed: %s", run_id, type(e).__name__)
        try:
            db.finish_run(run_id, status="error", error=err)
        except Exception:
            pass
        return {"status": "error", "run_id": run_id, "summary": err}
    finally:
        try:
            with db.db() as c:
                row = c.execute("SELECT status FROM runs WHERE id=?",
                                 (run_id,)).fetchone()
            if row and not row["status"]:
                db.finish_run(run_id, status="error",
                              error="run aborted before completion")
        except Exception:
            pass


def _card_flags(m: dict[str, Any]) -> str:
    """Jev's VIP / urgent annotations, for the top of an operator card."""
    v = m.get("_jev")
    flags = getattr(v, "flags", None) or []
    return f" [{' · '.join(flags)}]" if flags else ""


def _hold_jev_asks(
    pending: list[dict[str, Any]],
    box,
    account: str,
) -> tuple[list[dict[str, Any]], int]:
    """Escalate everything Jev marked ASK. Returns (rest, held_count).

    One card per message, ever. The dedupe claim is taken BEFORE the notify,
    and released if the notify itself failed, so a channel outage does not
    permanently silence a message the user never saw.
    """
    rest: list[dict[str, Any]] = []
    held = 0
    for m in pending:
        v = m.get("_jev")
        if v is None or v.verdict != jev.ASK:
            rest.append(m)
            continue
        question = (
            f"{m.get('sender', '')} — {m.get('subject', '') or '(no subject)'}"
            f"{_card_flags(m)}: {v.reason} (jev {v.verdict} "
            f"{v.confidence:.2f}, margin {v.margin:.2f})"
        )
        if not db.claim_surfaced(account, m["id"], "ask", v.reason):
            # Already asked. The user knows; asking again is the noise this
            # whole mechanism exists to remove.
            rest.append(m)
            continue
        mid = box._notify(box._tagged(f"Needs you: {question}"))
        if mid is None and box.notify:
            db.release_surfaced(account, m["id"], "ask")
        else:
            box.stats["escalated"] += 1
            held += 1
    return rest, held


def fetch_new(provider: MailProvider, limit: int = 0, cfg: Config | None = None) -> list[dict[str, Any]]:
    """Pull new mail into the store, honouring the per-stream cursor.

    The cursor is only advanced by advance_cursor(), after a successful run —
    never here. Advancing on fetch would mark mail as seen even if the agent
    run that follows crashes, and that mail would never be retried.

    On the FIRST run there is no cursor, so "everything after nothing" is the
    entire mailbox. It used to fetch 30 and archive two years of history on
    its first breath. With no cursor we now window the query to
    AGENT_FIRST_RUN_DAYS and say so, out loud. Once a cursor exists the
    window is dropped entirely — it would silently drop mail older than the
    window that arrived while the agent was down.
    """
    cursor = db.get_cursor(provider.account, "inbox")
    first_run = not cursor
    days = 0
    if cfg is not None:
        limit = limit or cfg.agent.fetch_limit
        if first_run:
            days = max(0, int(cfg.agent.first_run_days))
            if days:
                log.info("first run for %s: only mail from the last %d day(s)",
                         provider.account, days)
    limit = limit or 30
    msgs = provider.list_messages(folder="INBOX", limit=limit, after_id=cursor,
                                  newer_than_days=days)
    # upsert_message returns True when the row is new, so keep the ones that
    # were True — not the ones that were not.
    new_ids = {m["id"] for m in msgs if db.upsert_message(m)}
    if not new_ids:
        return []

    # Fetch the bodies of what is actually new, in one batch. The run prompt
    # includes them, so the model does not spend a round trip per message.
    try:
        # new_ids is a set, and a set is not sliceable. This used to raise
        # TypeError, was swallowed by the except below, and body prefetch was
        # therefore dead on every run: the model triaged on subject and
        # snippet alone, with no body, for the life of the project.
        pre = cfg.agent.body_prefetch if cfg is not None else 12
        full = provider.get_messages(sorted(new_ids)[:max(1, pre)])
    except Exception as e:
        # Log the message, not just the class. A bare "TypeError" told us
        # nothing and hid a bug that had been silently killing body prefetch
        # on every single run.
        log.warning("body prefetch failed: %s: %s", type(e).__name__, e)
        full = []

    by_id = {m["id"]: m for m in full}
    new = []
    with db.db() as c:
        for m in msgs:
            if m["id"] not in new_ids:
                continue
            if m["id"] in by_id and by_id[m["id"]].get("body"):
                body = by_id[m["id"]]["body"]
                c.execute("UPDATE messages SET body=? WHERE id=? AND account=?",
                          (body, m["id"], provider.account))
                m["body"] = body
            new.append(m)
    return new


def advance_cursor(provider: MailProvider, ids: list[str] | None = None) -> None:
    """Move the high-water mark to the newest message THIS RUN handled.

    It used to take the newest message by date across the whole account,
    which is not the same thing. Run A fetches 30 messages and dies partway
    leaving 22 unprocessed; run B succeeds and jumps the cursor to the
    newest message in the account, which is past those 22. They are never
    fetched again and the recovery is manual SQL.

    The cursor may only ever advance over messages we actually processed,
    so the caller passes the ids from this run. Called only after a run
    completes without error, so an interrupted run leaves the cursor where
    it was and its mail is re-fetched next cycle.
    """
    ids = [i for i in (ids or []) if i]
    if not ids:
        return
    qs = ",".join("?" * len(ids))
    with db.db() as c:
        row = c.execute(
            f"SELECT id FROM messages WHERE account=? AND id IN ({qs}) "
            "ORDER BY date DESC LIMIT 1",
            [provider.account, *ids],
        ).fetchone()
    if row:
        db.set_cursor(provider.account, "inbox", row["id"])


def preview(provider: MailProvider, cfg: Config, profile: dict[str, Any] | None = None
            ) -> dict[str, Any]:
    """What WOULD this run do. Touches nothing.

    The real decision is the model's, so this cannot claim to know it.
    What it can do is show the batch it would send, and run every local
    guard over each message - the escalation keywords, the injection
    signals, the send gate - so you can see which mail is going to stop
    for a human before it stops for one.

    No model call, no archive, no draft, no cursor movement.
    """
    new = fetch_new(provider, cfg=cfg)
    if not new:
        return {"status": "empty", "items": []}

    na = guards.normalize_address
    acct_auto = bool(getattr(cfg.google, "auto_send", False))
    contact_auto = {na(a) for a in cfg.agent.auto_send_contacts}
    never = {na(a) for a in cfg.agent.never_auto_send}

    items = []
    for m in new:
        sender = m.get("sender", "")
        body = m.get("body") or m.get("snippet") or ""
        d = guards.decide(
            sender=sender, subject=m.get("subject", ""), body=body,
            account=provider.account, cfg=cfg.agent,
            account_auto_send=acct_auto,
            contact_auto_send=na(sender) in contact_auto,
        )
        every = guards.explain(
            sender=sender, subject=m.get("subject", ""), body=body,
            account=provider.account, cfg=cfg.agent,
            account_auto_send=acct_auto,
            contact_auto_send=na(sender) in contact_auto,
        )
        items.append({
            "id": m.get("id", ""),
            "sender": sender,
            "subject": m.get("subject", "")[:80],
            "date": m.get("date", ""),
            "would_send": d.allowed,
            "reason": d.reason,
            "severity": d.severity,
            "signals": d.signals,
            # The full picture, not just whichever gate fired first.
            "all_reasons": every,
            "never_listed": na(sender) in never,
            "chars": len(body),
        })
    return {
        "status": "preview",
        "account": provider.account,
        "count": len(items),
        "blocked": sum(1 for i in items if not i["would_send"]),
        "items": items,
    }


def first_run_window(provider: MailProvider) -> bool:
    """True when this account has never completed a run (no cursor yet)."""
    return not db.get_cursor(provider.account, "inbox")


def scan(provider: MailProvider, cfg: Config, notify=None, model: str | None = None,
         profile: dict[str, Any] | None = None) -> dict[str, Any]:
    """Full cycle: fetch, run the agent, and only then advance the cursor."""
    new = fetch_new(provider, cfg=cfg)
    if not new:
        return {"status": "empty", "new": 0}
    if first_run_window(provider):
        log.info("first run: %d message(s) from the last %d day(s). "
                 "Older mail is untouched — change AGENT_FIRST_RUN_DAYS to "
                 "look further back.", len(new), cfg.agent.first_run_days)
    log.info("%s: %d new messages", provider.account, len(new))
    res = run_once(provider.account, provider, cfg, trigger="scan", notify=notify, messages=new,
                   model=model, profile=profile)
    # Advance only on a clean run so a crash does not swallow pending mail.
    if res.get("status") == "ok":
        advance_cursor(provider, [m["id"] for m in new])

    # LEARN runs once per scan, not once per send: diffing abandoned drafts
    # against the user's own sent replies costs a thread fetch each, so it is
    # bounded — and it must never break the scan that hosts it.
    voice_tip = None
    try:
        harvest_voice_samples(provider.account, provider)
        voice_tip = brain_style.maybe_propose_voice(provider.account)
    except Exception as e:
        log.debug("learn pass failed: %s", type(e).__name__)

    # Report the outcome. Sends already notified themselves; without this the
    # operator hears nothing at all when the agent escalated something, which
    # is exactly the moment they most need to know.
    if voice_tip and notify:
        from ..profiles import header_for as _header_for

        _tag = _header_for(profile, provider.address if hasattr(provider, "address") else "")
        notify(f"{_tag} {voice_tip}" if _tag else voice_tip)
    if notify and res.get("status") == "ok":
        from ..profiles import header_for

        tag = header_for(profile, provider.address if hasattr(provider, "address") else "")
        st = res.get("stats", {})
        escalated = st.get("escalated", 0)
        summary = (res.get("summary") or "").strip()
        if escalated:
            head = f"{escalated} thing{'s' if escalated != 1 else ''} need you"
            body = f"\n\n{summary}" if summary else ""
            notify(f"{tag} {head}{body}" if tag else f"{head}{body}")
        elif not st.get("sent") and not st.get("drafted") and summary:
            # Only chatter when the agent judged something worth saying —
            # or filed something worth knowing about. Filed-mail reports go
            # out once a day, not once a scan: silent archiving is how mail
            # disappears, but five identical "filed 3 newsletters" in a day
            # is how operators mute the channel and miss the sixth, real one.
            low = summary.lower()
            if len(summary) > 20 and (low.startswith("filed ") or
                                      not low.startswith(("no ", "nothing "))):
                if low.startswith("filed "):
                    from datetime import date as _date

                    if not db.claim_surfaced(
                            provider.account,
                            f"filed-{_date.today().isoformat()}",
                            "filed", summary[:200]):
                        return res
                notify(f"{tag} {summary}" if tag else summary)
    return res


def run_approval(account: str, provider: MailProvider, cfg: Config, approval_id: str,
                 approved: bool, notify=None) -> dict[str, Any]:
    """Execute or discard a queued send. Called from the approval flow."""
    from ..providers.base import DraftRequest

    with db.db() as c:
        # Atomic claim: exactly one executor wins. Concurrent approves
        # (double-tap, CLI racing chat) used to send N duplicate mails.
        cur = c.execute(
            "UPDATE approvals SET status='claimed' WHERE id=? AND status='pending'",
            (approval_id,))
        if cur.rowcount == 0:
            row = c.execute("SELECT status FROM approvals WHERE id=?",
                            (approval_id,)).fetchone()
            return {"ok": False,
                    "error": f"already {row['status']}" if row else "no such approval"}
        row = c.execute("SELECT * FROM approvals WHERE id=?",
                        (approval_id,)).fetchone()

    payload = json.loads(row["payload"])
    db.resolve_approval(approval_id, "approved" if approved else "denied", by="user")
    kind = row["kind"]

    # The approval is bound to the exact bytes queued. Anything that drifted
    # between the card and this call — a mutated recipient, a swapped body —
    # is something the user never approved, so it does not run. Approvals
    # queued before hash binding carry no hash and are held for the same
    # reason: unknown provenance is not provenance.
    if approved and kind in ("send", "calendar_delete", "calendar_invite"):
        if not _hash_matches(kind, payload):
            db.resolve_approval(approval_id, "blocked", by="gate")
            db.log_action("send_blocked", account, approval_id,
                          actor="gate", approval_id=approval_id,
                          detail="approval hash mismatch or missing — drift voids it")
            if notify:
                notify("Held: that approval no longer matches what was queued. "
                       "Re-queue it from the current draft.")
            return {"ok": False, "error": "blocked: approval drifted from what was queued"}

    if not approved:
        db.log_action("send_denied", account, payload.get("event_id") or ", ".join(payload.get("to", [])),
                      actor="user", approval_id=approval_id)
        if notify:
            notify("Send discarded." if kind == "send" else "Deletion discarded.")
        return {"ok": True, "sent": False}

    # Calendar deletions are a different payload shape to a send.
    if kind == "calendar_delete":
        event_id = payload.get("event_id", "")
        ok = bool(event_id) and provider.delete_event(event_id)
        db.log_action("calendar_delete", account, event_id, actor="user",
                      approval_id=approval_id, detail=payload.get("reason", ""))
        if notify:
            notify(f"Deleted calendar event {event_id}." if ok
                    else f"Could not delete {event_id}.")
        return {"ok": ok, "sent": ok}

    # Calendar invites approved by the user execute here.
    if kind == "calendar_invite":
        from ..providers.base import EventRequest

        attendees = [a for a in payload.get("attendees", []) if a]
        if not attendees:
            return {"ok": False, "error": "invite has no attendees left — refusing"}
        ev = provider.create_event(EventRequest(
            summary=payload.get("summary", ""), start=payload.get("start", ""),
            end=payload.get("end", ""), description=payload.get("description", ""),
            location=payload.get("location", ""), attendees=attendees))
        ok = bool(ev)
        db.log_action("calendar_invite", account, ev.get("id", "") if ev else "",
                      actor="user", approval_id=approval_id,
                      detail=f"{payload.get('summary', '')} -> {', '.join(attendees)}")
        if notify:
            notify(f"Invite sent to {', '.join(attendees)}." if ok
                    else "Could not create the event.")
        return {"ok": ok, "sent": ok}

    # Re-validate a queued send at execution time. The payload cannot change
    # under us (it is fixed in the DB row), but the world can: escalation
    # keywords may have been added since queueing, and this is the last
    # checkpoint before bytes leave the box.
    from ..providers.base import Attachment
    from ..providers.base import is_valid_address

    from . import guards as _guards
    to_addrs = payload.get("to", [])
    bad = [a for a in to_addrs if not is_valid_address(a)]
    if bad:
        return {"ok": False, "error": f"refusing: malformed address {bad[0]!r}"}
    if _guards.detect_injection(f"{payload.get('subject', '')}\n{payload.get('body', '')}"):
        db.log_action("send_blocked", account, ", ".join(to_addrs),
                      actor="gate", approval_id=approval_id,
                      detail="injection signals at execution time")
        return {"ok": False, "error": "blocked: injection signals at execution time"}

    ok = provider.send(DraftRequest(
        to=payload["to"], subject=payload["subject"],
        body=payload["body"], in_reply_to=payload.get("in_reply_to"),
        attachments=[Attachment(path=x) for x in payload.get("attachments", [])],
    ))
    if ok:
        db.log_action("send", account, ", ".join(payload["to"]), actor="user", approval_id=approval_id,
                      detail=payload["subject"])
        if notify:
            notify(f"Sent: {payload['subject']}")
        # LEARN, same as the auto path: this send is a confirmation sample,
        # and twelve of them (or two days of them) earn a profile proposal.
        record_send_confirmation(account, payload["to"], payload["subject"],
                                 payload["body"], payload.get("in_reply_to", ""))
        tip = brain_style.maybe_propose_voice(account)
        if tip and notify:
            notify(tip)
        # Approval learning: two approvals in a row earns a one-time
        # proposal — never silent auto-enable. Consent stays in chat.
        if notify:
            from . import learning as _learning

            for addr in payload.get("to", []):
                try:
                    tip = _learning.maybe_suggest(account, addr)
                except Exception:
                    tip = None
                if tip:
                    notify(tip)
                    break
    return {"ok": ok, "sent": ok}


_HASH_TOOL = {
    "send": "send_message",
    "calendar_delete": "calendar_delete",
    "calendar_invite": "calendar_invite",
}


def _hash_matches(kind: str, payload: dict[str, Any]) -> bool:
    """Does this payload still match the hash it was queued with?"""
    from . import guards as _guards

    stored = payload.get("action_hash", "")
    if not stored:
        return False
    body = {k: v for k, v in payload.items() if k != "action_hash"}
    return _guards.action_hash(_HASH_TOOL[kind], body) == stored


def record_send_confirmation(account: str, to_addrs: list[str], subject: str,
                               body: str, in_reply_to: str = "") -> None:
    """Log what just went out as a voice sample. The sent copy IS the draft
    here, so the row is a confirmation — proof the current voice works, not a
    correction. Corrections arrive via harvest, below. Never raises: learning
    must not break sending."""
    try:
        for addr in to_addrs:
            brain_style.record_voice_sample(
                account, in_reply_to or "", addr, subject,
                body, body, context="sent as drafted",
            )
    except Exception as e:
        log.debug("voice confirmation failed: %s", type(e).__name__)


def _draft_age_days(created_at: str) -> float | None:
    try:
        from datetime import datetime, timezone

        dt = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 86400
    except (ValueError, TypeError):
        return None


def harvest_voice_samples(account: str, provider: MailProvider, limit: int = 10) -> int:
    """Diff abandoned drafts against what the user actually sent instead.

    When the operator ignores a draft and writes their own reply, the thread
    holds a sent message newer than the draft, from their own address, with
    different bytes. That pair — what the agent proposed vs what the human
    chose — is the strongest learning signal there is, and it only exists
    here. Each draft is consumed once (marked harvested); drafts older than
    7 days with no sent reply are retired without a sample.

    Bounded and exception-safe: this runs inside the scan cycle, and learning
    must never break mail.
    """
    try:
        drafts = [d for d in db.list_drafts(account, limit=limit) if d.get("in_reply_to")]
    except Exception:
        return 0
    if not drafts:
        return 0
    own = (getattr(provider, "address", "") or "").lower()
    learned = 0
    for d in drafts:
        try:
            orig = provider.get_message(d["in_reply_to"])
            thread_id = (orig or {}).get("thread_id", "")
            thread = provider.get_thread(thread_id) if thread_id else []
        except Exception:
            continue
        sent_by_user = None
        for t in thread:
            labels = [str(x).upper() for x in (t.get("label_ids") or [])]
            sender = str(t.get("sender") or "")
            if "SENT" not in labels and (not own or own not in sender.lower()):
                continue
            if _is_newer(t.get("date", ""), d.get("created_at", "")):
                sent_by_user = t
                break
        if sent_by_user and sent_by_user.get("body") and d.get("body"):
            import re as _re

            norm_a = _re.sub(r"\s+", " ", d["body"]).strip()
            norm_b = _re.sub(r"\s+", " ", sent_by_user["body"]).strip()
            if norm_a != norm_b:
                # Edited: the user rewrote it. Record the correction.
                row = brain_style.record_voice_sample(
                    account, thread_id, d.get("to_addr", ""), d.get("subject", ""),
                    d["body"], sent_by_user["body"],
                    context=f"draft rewritten in thread {thread_id}",
                )
                if row:
                    learned += 1
            # Identical (or empty): the send-time confirmation already covered
            # it, or there is nothing to learn. Either way, consume the draft.
            db.mark_draft_harvested(d["id"], account)
            continue
        age = _draft_age_days(d.get("created_at", ""))
        if age is not None and age > 7:
            db.mark_draft_harvested(d["id"], account)
    return learned


def _is_newer(date_str: str, created_at: str) -> bool:
    """Is this message newer than the draft? Unparseable dates abstain —
    a wrong comparison here fabricates a correction out of nothing."""
    try:
        from datetime import datetime, timezone

        def _parse(v: str):
            dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

        return _parse(date_str) > _parse(created_at)
    except (ValueError, TypeError):
        return False
