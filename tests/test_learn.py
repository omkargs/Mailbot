"""Phase 6: LEARN is wired. Every send teaches; proposals go via chat."""
from __future__ import annotations

import json


def _send_body():
    return "hello friend, confirming friday works fine for the call"


def test_auto_send_records_confirmation(provider, cfg):
    from mailbot.brain.style import read_voice_log
    from mailbot.storage import db

    provider.auto_send = True
    cfg.agent.auto_send_contacts = ["boss@corp.com"]
    db.upsert_account("google", "me@example.com", "Me")
    db.bump_contact("google", "boss@corp.com", sent=True, received=True)
    from mailbot.agent.tools import ToolBox

    box = ToolBox(provider, cfg, run_id=1)
    out = box.run("send_message", {"to": ["boss@corp.com"], "subject": "Re: friday",
                                   "body": _send_body()})
    assert out.get("mode") == "auto"
    rows = read_voice_log()
    assert rows and rows[-1]["identical"] is True
    assert rows[-1]["recipient"] == "boss@corp.com"
    assert rows[-1]["edits"] == []


def test_approved_send_records_confirmation(provider, cfg):
    from mailbot.agent import guards as _guards
    from mailbot.agent.runner import run_approval
    from mailbot.brain.style import read_voice_log
    from mailbot.storage import db

    p = {"to": ["boss@corp.com"], "subject": "Re: friday", "body": _send_body(),
         "in_reply_to": "", "attachments": []}
    p["action_hash"] = _guards.action_hash(
        "send_message", {k: v for k, v in p.items() if k != "action_hash"})
    db.create_approval("ap_learn101", "google", "send", p, reason="t")
    res = run_approval("google", provider, cfg, "ap_learn101", True)
    assert res["sent"] is True
    rows = read_voice_log()
    assert rows and rows[-1]["identical"] is True


def _thread_with_user_reply(provider, draft_body, user_body, thread="t9", mid="m9"):
    now = "2026-10-01T12:00:00+00:00"
    provider.inbox.append({
        "id": mid, "account": "google", "thread_id": thread,
        "sender": "a@b.com", "sender_name": "", "recipients": "me@example.com",
        "subject": "friday", "snippet": "x", "body": "are we on for friday",
        "date": "2026-10-01T10:00:00+00:00", "label_ids": ["INBOX"],
        "has_attach": False, "size_bytes": 10,
    })
    provider.inbox.append({
        "id": "msent9", "account": "google", "thread_id": thread,
        "sender": "me@example.com", "sender_name": "", "recipients": "a@b.com",
        "subject": "Re: friday", "snippet": "x", "body": user_body,
        "date": now, "label_ids": ["SENT"],
        "has_attach": False, "size_bytes": 10,
    })


def test_harvest_learns_rewritten_draft(provider, cfg):
    from mailbot.agent.runner import harvest_voice_samples
    from mailbot.brain.style import read_voice_log, voice_state
    from mailbot.storage import db

    _thread_with_user_reply(provider, "draft words here", "my own rewritten reply here friend")
    db.upsert_account("google", "me@example.com", "Me")
    db.record_draft("d9", "google", None, "m9", "a@b.com", "Re: friday",
                    "agent draft words here friend")
    # Backdate the draft so the sent reply is newer than it.
    with db.db() as c:
        c.execute("UPDATE drafts SET created_at=? WHERE id=?",
                  ("2026-10-01T09:00:00+00:00", "d9"))
    n = harvest_voice_samples("google", provider)
    assert n == 1
    rows = read_voice_log()
    assert rows[-1]["identical"] is False and rows[-1]["edits"]
    assert voice_state()["edited"] == 1
    # Consumed: a second harvest finds nothing.
    assert harvest_voice_samples("google", provider) == 0


def test_harvest_retires_abandoned_drafts(provider, cfg):
    from mailbot.agent.runner import harvest_voice_samples
    from mailbot.brain.style import voice_state
    from mailbot.storage import db

    provider.inbox.append({
        "id": "mold", "account": "google", "thread_id": "told",
        "sender": "a@b.com", "sender_name": "", "recipients": "me@example.com",
        "subject": "old", "snippet": "x", "body": "old thread here friend",
        "date": "2026-09-01T10:00:00+00:00", "label_ids": ["INBOX"],
        "has_attach": False, "size_bytes": 10,
    })
    db.upsert_account("google", "me@example.com", "Me")
    db.record_draft("dold", "google", None, "mold", "a@b.com", "Re: old",
                    "agent draft words here friend")
    with db.db() as c:
        c.execute("UPDATE drafts SET created_at=? WHERE id=?",
                  ("2026-09-01T09:00:00+00:00", "dold"))
    assert harvest_voice_samples("google", provider) == 0
    assert voice_state()["total_samples"] == 0
    # Retired, not reaped: gone from the created pool.
    assert db.list_drafts("google") == []


def test_proposal_at_twelve_samples(cfg):
    from mailbot.brain.style import maybe_propose_voice, record_voice_sample

    for i in range(12):
        record_voice_sample("google", f"t{i}", "a@b.com", "s",
                            "agent draft words here", "agent draft words here")
    text = maybe_propose_voice("google")
    assert text and "Voice check-in" in text and "/brain" in text
    # Claimed: never proposes the same batch twice.
    assert maybe_propose_voice("google") is None


def test_proposal_needs_evidence(cfg):
    from mailbot.brain.style import maybe_propose_voice, record_voice_sample

    record_voice_sample("google", "t1", "a@b.com", "s", "a b c", "a b c")
    assert maybe_propose_voice("google") is None


def test_time_based_proposal(cfg):
    import datetime as _dt

    from mailbot.brain.style import _voice_log_path, maybe_propose_voice, record_voice_sample

    for i in range(3):
        record_voice_sample("google", f"t{i}", "a@b.com", "s",
                            "agent draft words here", "agent draft words here")
    # Age the samples three days by rewriting their timestamps.
    p = _voice_log_path()
    old = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=3)).isoformat()
    lines = []
    for line in p.read_text().splitlines():
        row = json.loads(line)
        row["ts"] = old
        lines.append(json.dumps(row))
    p.write_text("\n".join(lines) + "\n")
    text = maybe_propose_voice("google")
    assert text and "days" in text


def test_proposal_never_touches_profile(cfg, tmp_path):
    from mailbot.brain.style import maybe_propose_voice, record_voice_sample

    prof = tmp_path / "profile-google.md"
    prof.write_text("# mine — hands off\n")
    for i in range(12):
        record_voice_sample("google", f"t{i}", "a@b.com", "s",
                            "agent draft words here", "agent draft words here")
    maybe_propose_voice("google")
    assert prof.read_text() == "# mine — hands off\n"


def test_clean_text_strips_markup():
    from mailbot.brain.style import clean_text

    html = ("<!DOCTYPE html><html><head><style>.x{color:red}</style></head>"
            "<body><div>Hi msk, see you friday friend</div>"
            "<img src=x><p>Thanks!</p></body></html>")
    out = clean_text(html)
    assert "doctype" not in out.lower() and "color" not in out
    assert "Hi msk" in out and "Thanks!" in out
    assert clean_text("Just plain words here friend") == "Just plain words here friend"
    assert clean_text("") == ""


def test_markup_dominated_mail_is_not_a_sample(cfg):
    from mailbot.brain import style as S
    from mailbot.storage import db

    html = ("<html><head><style>" + (".x{color:red;border:1px solid blue}" * 200)
            + "</style></head><body><div><p>Hello friend, see you friday ok</p></div>"
              "</body></html>")
    db.upsert_account("google", "me@example.com", "Me")
    with db.db() as c:
        c.execute(
            "INSERT INTO messages (id, account, sender, subject, body, date, label_ids, stored_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            ("mhtml", "google", "me@example.com", "blast",
             html, "2026-09-01T10:00:00+00:00", '["SENT"]', "2026-09-01T10:00:00+00:00"))
    samples = S._sent_samples("google")
    assert all(s["id"] != "mhtml" for s in samples)


def test_voice_command_reports_progress(cfg):
    from mailbot.agent.chatops import build_chat_ops
    from mailbot.brain.style import record_voice_sample

    ops = build_chat_ops(cfg, lambda: {}, notify=None)
    assert "No voice samples" in ops["voice"]()
    for i in range(5):
        record_voice_sample("google", f"t{i}", "a@b.com", "s",
                            "agent draft words here", "agent draft words here")
    out = ops["voice"]()
    assert "Samples: 5" in out and "7 more sends" in out
