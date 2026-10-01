"""Command line entry point."""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

from . import config, logging_setup
from .agent import brief as brief_mod
from .agent import runner
from .agent.guards import Decision
from .brain import style as brain_style
from .notify.channels import build_notifiers, ApprovalListener
from .providers import build_providers
from .storage import db

log = logging.getLogger("mailagent")

_stop = False


def _handle_stop(signum, frame):
    global _stop
    _stop = True
    log.info("stopping on signal %s", signum)


def _providers(cfg):
    """Build and authenticate providers. Returns only the usable ones."""
    out = {}
    for name, p in build_providers(cfg).items():
        if p.valid():
            # Ask the provider which account it is before recording it, so
            # a second inbox is stored under its real address rather than
            # an empty string.
            try:
                p.resolve_address()
            except Exception:
                pass
            out[name] = p
            # A NEW inbox starts with auto-send OFF. It must not inherit the
            # global setting, and an existing inbox's own setting must not
            # be clobbered either: this runs on every single command, so
            # writing the global value here meant a per-inbox decision could
            # never stick, and a freshly added mailbox was trusted to send
            # before anyone approved a single contact.
            existing = db.get_account(name)
            auto = False if existing is None else bool(existing.get("auto_send"))
            cal = p.calendar_enabled if existing is None else bool(existing.get("calendar"))
            db.upsert_account(name, p.address, p.display_name,
                              auto_send=auto, calendar=cal)
        else:
            log.warning("account %s has no valid session; skipping (run `mail-agent auth`)", name)
    return out


# ------------------------------------------------------------------ commands

def cmd_auth(args, cfg):
    provs = build_providers(cfg)
    if not provs:
        print("No providers configured. Run setup.sh first.")
        return 1
    for name, p in provs.items():
        print(f"\n=== {name} ===")
        if p.valid():
            print(f"  already authenticated as {p.address}")
            continue
        ok = p.authenticate()
        print(f"  {'OK' if ok else 'FAILED'} — {name}")
        if ok:
            db.upsert_account(name, p.address, p.display_name,
                              auto_send=p.auto_send, calendar=p.calendar_enabled)
    return 0


def _cmd_scan_dry_run(args, cfg, providers):
    """Show what a scan would do, and touch nothing.

    The model's actual decision is unknowable in advance; what this shows
    is the batch and every local gate that would fire, so the mail that
    stops for a human is visible before it stops for one.
    """
    from . import profiles as _profiles

    for prof, p in _profiles.active(cfg, providers):
        res = runner.preview(p, cfg, profile=prof)
        if res.get("status") == "empty":
            print(f"[{prof.get('name', p.account) or p.account}] nothing new.")
            continue
        print(f"\n[{res['account']}] {res['count']} message(s) would be triaged, "
              f"{res['blocked']} would stop for you.")
        print("  (preview only — nothing sent, filed, drafted or marked read)\n")
        for it in res["items"]:
            mark = "SEND " if it["would_send"] else "HOLD "
            print(f"  {mark}{it['sender']}")
            if it["subject"]:
                print(f"        {it['subject']}")
            for r in (it.get("all_reasons") or [it["reason"]]):
                print(f"        · {r}")
            print()
    return 0


def cmd_explain(args, cfg):
    """Why would this message be held? Which gate fires, in what order."""
    providers = _providers(cfg)
    if not providers:
        print("No authenticated accounts.")
        return 1
    from .agent import guards
    from .storage import db as _db

    mid = args.message_id
    row = None
    with _db.db() as c:
        # An id is unique within one mailbox, not across mailboxes. With two
        # inboxes configured a collision is possible, and explaining whichever
        # row happened to sort first would be confidently wrong about the
        # other inbox. Say so instead.
        rows = c.execute(
            "SELECT * FROM messages WHERE id=? ORDER BY account", (mid,)
        ).fetchall()
    if not rows:
        print(f"No stored message with id {mid!r}.")
        print("  Ids look like 19fda0eb83f4805e and change per mailbox.")
        return 1
    if len({r["account"] for r in rows}) > 1:
        where = ", ".join(sorted({r["account"] for r in rows}))
        print(f"Id {mid!r} exists in more than one inbox: {where}")
        print("  Provider ids are only unique per mailbox, so this cannot be")
        print("  resolved without knowing which inbox you mean.")
        return 1
    row = rows[0]
    provider = next((p for p in providers if p.account == row["account"]), None)
    if provider is None:
        print(f"No provider for account {row['account']}.")
        return 1

    m = dict(row)
    m.setdefault("sender", row["sender"])
    body = m.get("body") or ""
    if not body:
        full = provider.get_message(mid) or {}
        body = full.get("body", "")

    na = guards.normalize_address
    contact_auto = na(m.get("sender", "")) in {na(a) for a in cfg.agent.auto_send_contacts}
    d = guards.decide(
        sender=m.get("sender", ""), subject=m.get("subject", ""), body=body,
        account=row["account"], cfg=cfg.agent,
        account_auto_send=bool(getattr(cfg.google, "auto_send", False)),
        contact_auto_send=contact_auto,
    )
    print(f"  from     {m.get('sender','')}")
    print(f"  subject  {m.get('subject','')}")
    print(f"  date     {m.get('date','')}")
    print(f"  id       {mid}")
    print()
    print(f"  verdict  {'would send unattended' if d.allowed else 'would stop for you'}")
    print(f"  reason   {d.reason}")
    print(f"  severity {d.severity}")
    if d.signals:
        print(f"  signals  {', '.join(d.signals)}")
    print()
    print("  The gates run in order, and each one found can only make the")
    print("  answer stricter. This is code, not a prompt — a clever model")
    print("  cannot talk past it.")
    return 0


def cmd_scan(args, cfg):
    if args.capped:
        cfg.agent.daily_token_cap = 0
    providers = _providers(cfg)
    if not providers:
        print("No authenticated accounts.")
        return 1
    if getattr(args, "dry_run", False):
        return _cmd_scan_dry_run(args, cfg, providers)
    notifier = build_notifiers(cfg)
    total = {"new": 0, "sent": 0, "drafted": 0, "escalated": 0}
    from . import profiles as _profiles
    for prof, p in _profiles.active(cfg, providers):
        model = _profiles.model_for(prof, cfg.router.model)
        res = runner.scan(p, cfg, notify=notifier.send, model=model, profile=prof)
        total["new"] += res.get("new", 0)
        st = res.get("stats", {})
        total["sent"] += st.get("sent", 0)
        total["drafted"] += st.get("drafted", 0)
        total["escalated"] += st.get("escalated", 0)
        if res.get("summary"):
            tag = _profiles.header_for(prof, getattr(p, "address", ""))
            print(f"[{tag or p.account}] {res['summary']}")
    print(json.dumps(total, indent=2))
    return 0


def cmd_brain(args, cfg):
    from .brain.style import build_profile
    providers = _providers(cfg)
    if not providers:
        print("No authenticated accounts.")
        return 1
    for name, p in providers.items():
        for folder, key in (("SENT", "sent"), ("SENT", "sentitems")):
            try:
                msgs = p.list_messages(folder=folder, limit=args.limit)
            except Exception:
                continue
                seeded = []
            # Batched bodies in small chunks with breathing room. Fetching
            # hundreds of sent messages at once trips Gmail's per-minute
            # quota partway through — and the old code then fell back to
            # single fetches, which is exactly how a quota blip becomes a
            # 403 storm. A failed chunk is skipped, not retried into the
            # ground; re-running later fills the gaps.
            import time as _time

            ids = [m["id"] for m in msgs][:args.limit]
            fulls = []
            for i in range(0, len(ids), 25):
                try:
                    fulls += p.get_messages(ids[i:i + 25])
                except Exception as e:
                    log.warning("sent-folder chunk %d failed (%s); skipping",
                                i // 25, type(e).__name__)
                    if "quota" in str(e).lower() or "403" in str(e):
                        print("  Gmail quota hit — keeping what was mined; "
                              "re-run `mail-agent brain` later for the rest.")
                        break
                _time.sleep(2)
            by_id = {f["id"]: f for f in fulls}
            for m in msgs:
                full = by_id.get(m["id"])
                if full:
                    m.update(full)
                    db.upsert_message(m)
                    db.bump_contact(name, m.get("sender", ""), sent=True)
                    seeded.append(m["id"])
            # The sent folder is training data, not inbox. Mark it consumed so
            # it never resurfaces as untriaged mail.
            db.mark_processed_many(seeded, name)
            break
        profile = build_profile(name)
        path = cfg.brain_path() / f"profile-{name}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(profile)
        print(f"[{name}] wrote {path} ({len(profile)} bytes)")
    return 0


def cmd_voice(args, cfg):
    state = brain_style.voice_state()
    print(json.dumps(state, indent=2))
    if args.log:
        for row in brain_style.read_voice_log()[-args.log:]:
            print(f"\n--- {row['recipient']} ({row['subject']}) ---")
            for e in row["edits"]:
                print(f"  {e['field']}: {e['agent'][:60]!r} -> {e['actual'][:60]!r}")
    return 0


def cmd_brief(args, cfg):
    # Honesty guard. With no connected account this used to print
    # "**Needs you:** nothing. Inbox handled." and exit 0 — a silent agent
    # and a broken one look identical from outside, which is the one thing
    # this project claims not to be. cmd_approve already guards this way.
    providers = _providers(cfg)
    if not providers:
        print("No authenticated account — there is no inbox to brief.")
        print("Run: mail-agent setup --step google")
        return 1
    notifier = build_notifiers(cfg)
    text = brief_mod.build_brief(cfg)
    print(text)
    if notifier.interactive() or notifier.send:
        notifier.send(text)
    return 0


def cmd_quiet(args, cfg):
    if not _providers(cfg):
        print("No authenticated account — nothing to review.")
        return 1
    text = brief_mod.brief_quiet_threads(cfg)
    print(text)
    notifier = build_notifiers(cfg)
    if notifier.send:
        notifier.send(text)
    return 0


def cmd_approve(args, cfg):
    providers = _providers(cfg)
    notifier = build_notifiers(cfg)
    if not providers:
        print("No authenticated accounts.")
        return 1
    if args.list:
        pend = db.pending_approvals()
        if not pend:
            print("Nothing pending.")
        for p in pend:
            print(f"{p['id']}  [{p['account']}]  {p['kind']}  {p['reason'][:70]}")
        return 0
    if not args.id:
        print("need --id or --list")
        return 1
    p = next(iter(providers.values()))
    res = runner.run_approval(p.account, p, cfg, args.id, not args.deny, notify=notifier.send)
    print(json.dumps(res, indent=2))
    return 0 if res.get("ok") else 1


def cmd_skill(args, cfg):
    if args.remove:
        ok = db.delete_skill(args.remove)
        print("removed" if ok else "no such skill")
        return 0 if ok else 1
    if args.show:
        for s in db.list_skills():
            print(f"- {s['name']}: {s['description']} (used {s['run_count']}x)")
        return 0
    if not args.name or not args.instructions:
        print("usage: skill save <name> --instructions '...' | --list | --remove <name>")
        return 1
    db.save_skill(args.name, args.description or "", args.instructions)
    print(f"saved skill: {args.name}")
    return 0


def cmd_contacts(args, cfg):
    """Review who the agent may email unattended."""
    for a in db.list_accounts():
        rows = db.list_contacts(a["id"], limit=100)
        print(f"\n=== {a['id']} (auto-send: {'ON' if a['auto_send'] else 'off'}) ===")
        if not rows:
            print("  no contacts yet — run `mail-agent brain`")
            continue
        for r in rows[:25]:
            flag = "✓ may auto-reply" if r["auto_send_ok"] else "·"
            print(f"  {flag} {r['address']:<40} sent={r['sent_count']:<4} recv={r['received_count']}")
    if args.approve:
        with db.db() as c:
            a = c.execute("SELECT id FROM accounts WHERE id=?", (args.account,)).fetchone()
        if a:
            db.set_contact_auto_send(args.account, args.approve, True)
            print(f"approved {args.approve} for unattended replies on {args.account}")
            return 0
        print(f"unknown account {args.account}")
        return 1
    if args.revoke:
        db.set_contact_auto_send(args.account, args.revoke, False)
        print(f"revoked {args.revoke}")
        return 0
    return 0


def cmd_daemon(args, cfg):
    """Always-on runtime.

    Detection is push via IMAP IDLE when an app password is configured,
    otherwise interval polling. Either way the supervisor drives the loop,
    backs off on failure, and tells you when something breaks.
    """
    from .agent.idle import IdleListener
    from .agent.supervisor import Supervisor

    notifier = build_notifiers(cfg)

    def providers_factory():
        return _providers(cfg)

    def scan_cycle():
        from . import profiles as _profiles

        provs = providers_factory()
        for prof, p in _profiles.active(cfg, provs):
            runner.scan(p, cfg, notify=notifier.send,
                        model=_profiles.model_for(prof, cfg.router.model),
                        profile=prof)

    # One listener for the process: reusing it keeps the getUpdates offset and
    # the seen-set consistent, and avoids a second poll each cycle.
    listener = ApprovalListener(cfg, {})

    def tick():
        listener.providers = providers_factory()
        listener.tick()

    sup = Supervisor(cfg, providers_factory, scan_cycle, tick, notifier.send)

    # IDLE pokes the supervisor's wake event. The loop's interruptible sleep
    # returns on it, so new mail is handled immediately instead of at the next
    # poll tick. No method patching — both paths set the same Event.
    if cfg.google.imap_password and cfg.google.account:
        try:
            # sup.wake is an Event; on_new is called as a function, so hand it
            # the bound method. Passing the Event itself raised TypeError on
            # every push, so mail was detected and then never processed.
            idle = IdleListener(cfg.google, sup.wake.set, cfg.google.imap_password,
                                notify=notifier.send)
            idle.start()
            sup.idle = idle
            log.info("IMAP IDLE active — push detection, zero polling cost")
        except Exception as e:
            log.warning("IDLE unavailable, using interval polling: %s", type(e).__name__)
    else:
        log.info("No GOOGLE_IMAP_PASSWORD set — using interval polling (%ds). "
                 "Add an app password for push detection.",
                 cfg.agent.scan_interval_sec)

    return sup.start()


def cmd_health(args, cfg):
    """Is the daemon alive and doing its job?"""
    import json as _json
    from pathlib import Path
    from datetime import datetime, timezone, timedelta

    pid_file = Path(os.environ.get("MAIL_AGENT_STATE", str(Path.home() / ".local/share/mail-agent")))
    pid_file.mkdir(parents=True, exist_ok=True)
    pf = pid_file / "daemon.json"

    up = False
    cycles = None
    last_err = ""
    if pf.exists():
        try:
            st = _json.loads(pf.read_text())
            pid = st.get("pid", 0)
            # Existence of /proc/<pid> proves nothing — the pid is reused, so a
            # dead daemon still "exists" and health reported it running for
            # nine minutes after it stopped. Judge on the heartbeat's age.
            from datetime import datetime as _dt, timezone as _tz
            ts = st.get("updated_at") or st.get("started_at") or ""
            fresh = False
            if ts:
                age = (_dt.now(_tz.utc) - _dt.fromisoformat(ts)).total_seconds()
                # Two scan intervals plus slack.
                fresh = age < (cfg.agent.scan_interval_sec * 2 + 120)
            up = bool(pid) and Path(f"/proc/{pid}").exists() and fresh
            cycles = st.get("cycles")
            last_err = st.get("last_error", "")
        except Exception:
            pass

    print("=== daemon ===")
    print(f"  running: {'yes' if up else 'NO'}")
    if pf.exists():
        try:
            st = _json.loads(pf.read_text())
            print(f"  pid: {st.get('pid')}  cycles: {st.get('cycles')}  "
                  f"uptime: {st.get('uptime_sec', 0) // 60}m")
            if st.get("idle_connected"):
                print("  push: IDLE connected — new mail wakes the agent")
            elif st.get("idle_note"):
                print(f"  push: {st['idle_note']}")
        except Exception:
            pass
    if last_err:
        print(f"  last error: {last_err[:100]}")

    print("\n=== activity ===")
    with db.db() as c:
        last = c.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        if last:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(last["started_at"])).total_seconds()
            print(f"  last run: {int(age)}s ago ({last['account']}, {last['status']})")
            print(f"  last: {last['triaged']} triaged, {last['drafted']} drafted, "
                  f"{last['sent']} sent, {last['escalated']} escalated")
        else:
            print("  no runs recorded yet")

    pend = db.pending_approvals()
    print(f"\n=== pending ===\n  approvals waiting: {len(pend)}")

    u = db.usage_today()
    cap = cfg.agent.daily_token_cap
    used = u["input_tokens"] + u["output_tokens"]
    print(f"\n=== budget ===\n  {used:,} / {cap:,} tokens today ({100*used/cap:.1f}%)")
    return 0 if up else 1


def cmd_status(args, cfg):
    problems = cfg.validate()
    print("=== config ===")
    if problems:
        for p in problems:
            print(f"  ! {p}")
    else:
        print("  ok")
    print(f"\n=== accounts ===")
    for a in db.list_accounts(enabled_only=False):
        print(f"  {a['id']:<12} {a['address'] or '(unknown)':<32} "
              f"auto_send={'ON' if a['auto_send'] else 'off'} enabled={'y' if a['enabled'] else 'n'}")
    u = db.usage_today()
    print(f"\n=== today ===")
    print(f"  tokens in/out: {u['input_tokens']:,} / {u['output_tokens']:,}  runs: {u.get('runs',0)}")
    print(f"  pending approvals: {len(db.pending_approvals())}")
    return 0 if not problems else 1


def cmd_check(args, cfg):
    """Verify the router answers. Cheap smoke test before going live."""
    from .agent.client import build_client, guarded_call
    try:
        client = build_client(cfg.router)
        r = guarded_call(client, 
            model=cfg.router.model, max_tokens=32,
            messages=[{"role": "user", "content": "Reply with the single word: OK"}],
        )
        print(f"router OK: {cfg.router.base_url} model={r.model}")
        for b in r.content:
            if getattr(b, "type", "") == "text":
                print(f"  said: {b.text.strip()[:80]}")
        return 0
    except Exception as e:
        print(f"router FAILED: {type(e).__name__}: {e}")
        return 1


def cmd_schedule(args, cfg):
    """Set a scheduled job from plain language."""
    import uuid
    from .agent.schedule import ScheduleError, parse

    request = " ".join(args.request)
    try:
        spec = parse(request)
    except ScheduleError as e:
        print(f"could not schedule: {e}")
        return 1

    providers = _providers(cfg)
    account = next(iter(providers), "google")
    jid = f"job_{uuid.uuid4().hex[:10]}"
    repeat = args.repeat or spec["repeat"]
    if repeat not in ("none", "daily", "weekly"):
        repeat = "none"
    db.create_job(jid, account, spec["kind"], at_time=spec["at_time"],
                  at_minutes=spec["in_minutes"],
                  repeat=repeat, prompt=spec["prompt"])

    if spec["in_minutes"] is not None:
        from datetime import datetime, timedelta
        fire = (datetime.now() + timedelta(minutes=spec["in_minutes"])).strftime("%H:%M")
        with db.db() as c:
            c.execute("UPDATE scheduled_jobs SET at_time=? WHERE id=?", (fire, jid))
        when = f"in {spec['in_minutes']} minutes"
    else:
        rep = {"daily": "every day", "weekly": "every week"}.get(args.repeat or spec["repeat"], "once")
        when = f"{rep} at {spec['at_time']}"
        if spec.get("rolled_to_tomorrow"):
            when += " (tomorrow — that time has passed today)"

    db.log_action("schedule", account, jid, detail=request[:200])
    print(f"scheduled {spec['kind']} — {when}\n  id: {jid}\n  cancel: mail-agent cancel {jid}")
    return 0


def cmd_tasks(args, cfg):
    jobs = db.list_jobs(include_disabled=args.all)
    if not jobs:
        print("nothing scheduled. try: mail-agent schedule 'full inbox brief at 5'")
        return 0
    for j in jobs:
        rep = {"daily": "every day", "weekly": "every week"}.get(j["repeat"], "once")
        state = "" if j["enabled"] else "  [cancelled]"
        print(f"{j['id']}  {j['kind']:<8} {str(j['at_time']):<6} {rep:<11} "
              f"ran={j['run_count']}{state}")
        if j["prompt"]:
            print(f"    {j['prompt'][:80]}")
    return 0


def cmd_cancel(args, cfg):
    ok = db.cancel_job(args.id)
    print("cancelled" if ok else f"no enabled job with id {args.id!r}")
    return 0 if ok else 1


def cmd_requeue(args, cfg):
    """Re-queue mail the cursor skipped. Sends nothing by itself.

    If the cursor ever jumped past mail that was fetched but never
    processed, that mail is still in Gmail and still in the local store.
    Clearing the cursor makes the next scan pick it up again.
    """
    from .storage import db as _db

    with _db.db() as c:
        n = c.execute(
            "SELECT COUNT(*) n FROM messages WHERE processed_at IS NULL").fetchone()["n"]
        rows = c.execute("SELECT account FROM cursors WHERE stream='inbox'").fetchall()
    if not rows:
        print("No cursor set — nothing to re-queue.")
        return 0
    for r in rows:
        _db.set_cursor(r["account"], "inbox", "")
    print(f"Cleared the inbox cursor for {len(rows)} account(s).")
    print(f"{n} unprocessed message(s) will be re-fetched on the next scan.")
    print("Run `mail-agent scan --dry-run` first if you want to see them.")
    return 0


def cmd_reset(args, cfg):
    """Wipe the agent's working memory. Credentials and the voice profile stay.

    Operational state — messages, runs, approvals, contacts, cursors, chat
    history — is all re-derivable from Gmail. What is not re-derivable is the
    learned voice profile and the credentials, so those are never touched.
    """
    import shutil
    import time
    from .config import DB_PATH

    if not args.yes:
        print("This deletes all local agent state. Credentials and the learned")
        print("voice profile are kept. Re-run with --yes to confirm.")
        return 1

    backup = Path(str(DB_PATH) + f".backup-{time.strftime('%Y%m%d-%H%M%S')}")
    try:
        shutil.copy2(DB_PATH, backup)
        print(f"backup: {backup}")
    except Exception as e:
        print(f"could not back up: {type(e).__name__} — refusing to wipe")
        return 1

    tables = ("drafts", "approvals", "actions_log", "messages", "cursors",
              "runs", "contacts", "labels", "usage_daily", "scheduled_jobs",
              "chat_history", "skills", "surfaced", "accounts")
    cleared = 0
    with db.db() as c:
        for t in tables:
            try:
                c.execute(f"DELETE FROM {t}")
                cleared += 1
            except Exception as e:
                print(f"  skipped {t}: {type(e).__name__}")
    print(f"cleared {cleared} tables — fresh start")
    return 0


def cmd_jev(args, cfg):
    """Show the Jev decider's live config, thresholds and recent verdicts."""
    from .agent import jev as jev_mod
    from .agent.chatops import build_chat_ops

    ops = build_chat_ops(cfg, lambda: {}, notify=None)
    extra = ""
    if getattr(args, "set", ""):
        extra = f"/jev {args.set}"
    print(ops["jev"](extra))
    if not jev_mod.enabled(cfg):
        return 0
    return 0


def cmd_chat(args, cfg):
    """Talk to the agent from the terminal. Same handler as Telegram.

    Not a second interface with its own rules — every line goes through
    handle_text with chat="terminal", so every command works identically and
    conversation memory is separate from the Telegram thread. Standing
    authority changes are allowed here: the owner is in the room, typing.
    """
    from .agent.chat import handle_text
    from .agent.chatops import build_chat_ops
    from .notify.channels import build_notifiers

    providers = _providers(cfg)
    if not providers:
        print("No authenticated accounts.")
        return 1
    notifier = build_notifiers(cfg)
    ops = build_chat_ops(cfg, lambda: providers, notify=notifier.send)
    print("Talking to mail-agent. Same commands as Telegram (/help). Ctrl-D to quit.")
    while True:
        try:
            line = input("you> ")
        except (EOFError, KeyboardInterrupt):
            print()
            break
        try:
            reply = handle_text(line, cfg, providers, ops, chat="terminal")
        except Exception as e:
            reply = f"Something went wrong: {type(e).__name__}. Check the logs."
        if reply:
            print(reply)
    return 0


def cmd_cal(args, cfg):
    """Read the calendar from the terminal."""
    providers = _providers(cfg)
    if not providers:
        print("No authenticated accounts.")
        return 1
    p = next(iter(providers.values()))
    if not p.calendar_enabled:
        print("calendar is disabled for this account")
        return 1
    try:
        evs = p.list_events(limit=args.limit)
    except Exception as e:
        print(f"calendar failed: {type(e).__name__}: {e}")
        return 1
    if not evs:
        print("nothing on the calendar")
        return 0
    for e in evs:
        start = e.get("start") or {}
        when = start.get("dateTime") or start.get("date") or "?"
        print(f"{when[:16]:<17} {e.get('summary') or '(no title)'}  [{e.get('id','')}]")
    return 0


def main() -> int:
    import re as _re

    from .ui import suggest_command

    class _Parser(argparse.ArgumentParser):
        def error(self, message):
            m = _re.search(r"invalid choice: ([^ ]+)", message)
            if m:
                cmds = []
                for a in self._actions:
                    if isinstance(a, argparse._SubParsersAction):
                        cmds = list(a.choices)
                sug = suggest_command(m.group(1).strip("'\""), cmds)
                if sug:
                    message += ("\n\ndid you mean:\n" + "\n".join(
                        f"  mail-agent {s}" for s in sug))
            self.print_usage(sys.stderr)
            self.exit(2, f"{self.prog}: error: {message}\n")

    ap = _Parser(prog="mail-agent", description="Autonomous inbox agent")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=False)

    p = sub.add_parser("auth", help="authenticate configured providers")
    p.set_defaults(fn=cmd_auth)

    p = sub.add_parser("scan", help="process new mail once")
    p.add_argument("--capped", action="store_true", help="no sends at all")
    p.add_argument("--dry-run", action="store_true",
                   help="show what would happen; send, file and draft nothing")
    p.set_defaults(fn=cmd_scan)

    p = sub.add_parser("requeue-unprocessed",
                       help="re-fetch mail the cursor skipped; sends nothing")
    p.set_defaults(fn=cmd_requeue)

    p = sub.add_parser("explain", help="why would this message be held?")
    p.add_argument("message_id")
    p.set_defaults(fn=cmd_explain)

    p = sub.add_parser("brain", help="rebuild the style profile from sent mail")
    p.add_argument("--limit", type=int, default=300,
                   help="how many sent messages to learn from (default 300)")
    p.set_defaults(fn=cmd_brain)

    p = sub.add_parser("voice", help="how much the agent has learned about your voice")
    p.add_argument("--log", type=int, default=0, help="show the last N corrections")
    p.set_defaults(fn=cmd_voice)

    p = sub.add_parser("brief", help="morning digest")
    p.set_defaults(fn=cmd_brief)

    p = sub.add_parser("quiet", help="threads going cold")
    p.set_defaults(fn=cmd_quiet)

    p = sub.add_parser("approve", help="approve or deny a queued send")
    p.add_argument("id", nargs="?")
    p.add_argument("--deny", action="store_true")
    p.add_argument("--list", action="store_true")
    p.set_defaults(fn=cmd_approve)

    p = sub.add_parser("skill", help="manage saved automations")
    p.add_argument("name", nargs="?")
    p.add_argument("--instructions")
    p.add_argument("--description")
    p.add_argument("--show", action="store_true")
    p.add_argument("--remove")
    p.set_defaults(fn=cmd_skill)

    p = sub.add_parser("contacts", help="review who may get unattended replies")
    p.add_argument("--approve", metavar="EMAIL")
    p.add_argument("--revoke", metavar="EMAIL")
    p.add_argument("--account", default="google")
    p.set_defaults(fn=cmd_contacts)

    p = sub.add_parser("daemon", help="run continuously")
    p.set_defaults(fn=cmd_daemon)

    p = sub.add_parser("status", help="show config and run state")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("health", help="is the daemon alive and working?")
    p.set_defaults(fn=cmd_health)

    p = sub.add_parser("check", help="verify the AI router is reachable")
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser("schedule", help="set a job: 'brief at 5', 'inbox at 7 every morning'")
    p.add_argument("request", nargs="+")
    p.add_argument("--repeat", choices=["none", "daily", "weekly"], default=None)
    p.set_defaults(fn=cmd_schedule)

    p = sub.add_parser("tasks", help="list scheduled jobs")
    p.add_argument("--all", action="store_true", help="include cancelled")
    p.set_defaults(fn=cmd_tasks)

    p = sub.add_parser("cancel", help="cancel a scheduled job by id")
    p.add_argument("id")
    p.set_defaults(fn=cmd_cancel)

    p = sub.add_parser("reset", help="wipe local agent state (keeps credentials + voice profile)")
    p.add_argument("--yes", action="store_true", help="confirm the wipe")
    p.set_defaults(fn=cmd_reset)

    p = sub.add_parser("chat", help="talk to the agent (same handler as Telegram)")
    p.set_defaults(fn=cmd_chat)

    p = sub.add_parser("cal", help="show upcoming calendar events")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(fn=cmd_cal)

    p = sub.add_parser("jev", help="the fast decider: config, thresholds, verdicts")
    p.add_argument("set", nargs="?", default="",
                   help="optional tuning hint, e.g. 'conf 0.7'")
    p.set_defaults(fn=cmd_jev)

    p = sub.add_parser("setup", help="guided setup: provider, google, chat (headless-friendly)")
    p.add_argument("--yes", action="store_true", help="accept defaults, skip optional prompts")
    p.add_argument("--fast", action="store_true", help="fast path: defaults, skip voice/service/chat prompts")
    p.add_argument("--non-interactive", action="store_true", help="never prompt; read from env")
    p.add_argument("--import-env", action="store_true", help="copy known env vars into .secrets")
    p.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")
    p.add_argument("--step", default="",
                   help="run one step: provider|jev|google|push|chat|voice|start|verify")
    p.add_argument("--print-auth-url", action="store_true",
                   help="print the Google consent URL and exit (headless boxes)")
    p.add_argument("--for-profile", default="",
                   help="use this profile's OAuth client with --print-auth-url")
    p.add_argument("--skip-voice", action="store_true")
    p.add_argument("--skip-service", action="store_true")
    def _fn_setup(a, c):
        from .wizard import cmd_setup
        return cmd_setup(a, c)
    p.set_defaults(fn=_fn_setup)

    p = sub.add_parser("doctor", help="pre-flight checks with fix hints")
    def _fn_doctor(a, c):
        from .wizard import cmd_doctor
        return cmd_doctor(a, c)
    p.set_defaults(fn=_fn_doctor)

    p = sub.add_parser("demo", help="10-second fake-inbox demo, no credentials needed")
    def _fn_demo(a, c):
        from .demo import run_demo
        return run_demo()
    p.set_defaults(fn=_fn_demo)

    p = sub.add_parser("dossier", help="your persona, mined from your own mailbox")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--account", default="", help="inbox account (default: current profile)")
    def _fn_dossier(a, c):
        from .dossier import cmd_dossier
        return cmd_dossier(a, c)
    p.set_defaults(fn=_fn_dossier)

    args = ap.parse_args()
    if not args.cmd:
        # Bare run: concise help with the happy path first (clig.dev).
        print("mail-agent — your inbox, on autopilot.")
        print()
        print("  mail-agent demo           see it work, zero credentials needed")
        print("  mail-agent setup --fast   set up in ~60 seconds")
        print("  mail-agent status         what it has done")
        print("  mail-agent brief          the morning digest")
        print()
        print("Full help: mail-agent --help   Guide: SETUP.md")
        return 2
    logging_setup.setup(logging.DEBUG if args.verbose else logging.INFO)

    from .storage import db as _db
    _db.migrate()

    cfg = config.load()
    # Load the spend limits for EVERY entry point, not just the daemon.
    # from_config() was only ever called by the supervisor, so `mail-agent
    # scan`, `brief`, `brain`, `voice` and the approval flow all ran
    # against the hardcoded 500k default and ignored
    # AGENT_DAILY_TOKEN_CAP entirely. The README presents these as limits
    # that are "on by default"; for anyone driving the agent from cron or
    # a terminal, two of the four were decorative.
    try:
        from .limits import from_config as _limits_from_config
        _limits_from_config(cfg)
    except Exception:
        pass
    try:
        return args.fn(args, cfg)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
