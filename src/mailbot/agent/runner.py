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
    client = build_client(cfg.router)
    # Email-driven: standing authority changes are refused in this context.
    box = ToolBox(provider, cfg, run_id, notify=notify,
                  allow_permission_change=False, profile=profile)

    pending = messages if messages is not None else db.unprocessed(account, limit=cfg.agent.max_drafts_per_run)
    if not pending:
        db.finish_run(run_id, status="ok")
        return {"status": "empty", "run_id": run_id}

    # Cheap triage pass first: a small model sorts, IGNOREs get archived
    # here, and the flagship only ever sees what matters. Any failure falls
    # back to the full flagship pass — mail is never dropped to save money.
    if triage.wanted(cfg):
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

    if completed:
        for m in pending:
            db.mark_processed(m["id"])
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


def fetch_new(provider: MailProvider, limit: int = 30) -> list[dict[str, Any]]:
    """Pull new mail into the store, honouring the per-stream cursor.

    The cursor is only advanced by advance_cursor(), after a successful run —
    never here. Advancing on fetch would mark mail as seen even if the agent
    run that follows crashes, and that mail would never be retried.
    """
    cursor = db.get_cursor(provider.account, "inbox")
    msgs = provider.list_messages(folder="INBOX", limit=limit, after_id=cursor)
    # upsert_message returns True when the row is new, so keep the ones that
    # were True — not the ones that were not.
    new_ids = {m["id"] for m in msgs if db.upsert_message(m)}
    if not new_ids:
        return []

    # Fetch the bodies of what is actually new, in one batch. The run prompt
    # includes them, so the model does not spend a round trip per message.
    try:
        full = provider.get_messages(new_ids[:12])
    except Exception as e:
        log.warning("body prefetch failed: %s", type(e).__name__)
        full = []

    by_id = {m["id"]: m for m in full}
    new = []
    with db.db() as c:
        for m in msgs:
            if m["id"] not in new_ids:
                continue
            if m["id"] in by_id and by_id[m["id"]].get("body"):
                body = by_id[m["id"]]["body"]
                c.execute("UPDATE messages SET body=? WHERE id=?", (body, m["id"]))
                m["body"] = body
            new.append(m)
    return new


def advance_cursor(provider: MailProvider) -> None:
    """Move the high-water mark to the newest stored message for this account.

    Called only after a run completes without error, so an interrupted run
    leaves the cursor where it was and the mail is re-fetched next cycle.
    """
    with db.db() as c:
        row = c.execute(
            "SELECT id FROM messages WHERE account=? ORDER BY date DESC LIMIT 1",
            (provider.account,),
        ).fetchone()
    if row:
        db.set_cursor(provider.account, "inbox", row["id"])


def scan(provider: MailProvider, cfg: Config, notify=None, model: str | None = None,
         profile: dict[str, Any] | None = None) -> dict[str, Any]:
    """Full cycle: fetch, run the agent, and only then advance the cursor."""
    new = fetch_new(provider)
    if not new:
        return {"status": "empty", "new": 0}
    log.info("%s: %d new messages", provider.account, len(new))
    res = run_once(provider.account, provider, cfg, trigger="scan", notify=notify, messages=new,
                   model=model, profile=profile)
    # Advance only on a clean run so a crash does not swallow pending mail.
    if res.get("status") == "ok":
        advance_cursor(provider)

    # Report the outcome. Sends already notified themselves; without this the
    # operator hears nothing at all when the agent escalated something, which
    # is exactly the moment they most need to know.
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
            # or filed something worth knowing about. Filed-mail reports
            # always go out; silent archiving is how mail disappears.
            low = summary.lower()
            if len(summary) > 20 and (low.startswith("filed ") or
                                      not low.startswith(("no ", "nothing "))):
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
    return {"ok": ok, "sent": ok}


def harvest_voice_samples(account: str, provider: MailProvider, limit: int = 40) -> int:
    """Compare agent drafts against what was actually sent, so the voice learns."""
    drafts = db.list_drafts(account, limit=limit)
    if not drafts:
        return 0
    learned = 0
    for d in drafts:
        if not d.get("in_reply_to"):
            continue
        try:
            actual = provider.get_message(d["in_reply_to"])
        except Exception:
            continue
        if not actual:
            continue
        # If the user edited and sent our draft, the thread contains a later
        # message. We compare the draft against the sent copy when we can find it.
        if actual.get("body") and d.get("body"):
            row = brain_style.record_voice_sample(
                account, d["in_reply_to"], d["to_addr"], d["subject"],
                d["body"], actual["body"], context=d.get("in_reply_to", ""),
            )
            if row:
                learned += 1
    return learned
