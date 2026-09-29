"""Dossier reads stored data only — no mailbox, no model needed."""
from __future__ import annotations


def _seed(cfg):
    from mailbot.storage import db

    db.migrate()
    for i in range(3):
        db.upsert_message({
            "id": f"d{i}", "account": "google", "thread_id": f"t{i}",
            "sender": "priya@studio.com", "sender_name": "", "recipients": "me@x.com",
            "subject": f"Hi {i}", "snippet": "hey", "body": "hey there friend",
            "date": f"2026-09-2{i}T09:15:00+00:00", "label_ids": ["INBOX"],
            "has_attach": False, "size_bytes": 10,
        })
    db.bump_contact("google", "priya@studio.com", sent=True, received=True)
    db.bump_contact("google", "priya@studio.com", sent=True)


def test_dossier_reports_contacts_and_peak(cfg):
    from mailbot.dossier import build_dossier, render

    _seed(cfg)
    d = build_dossier("google", cfg)
    assert d["messages_seen"] == 3
    assert d["peak_hours"].startswith("09:00")
    assert d["top_contacts"][0]["address"] == "priya@studio.com"
    assert d["top_contacts"][0]["sent"] == 2
    out = render(d)
    assert "priya@studio.com" in out and "09:00" in out


def test_dossier_empty_inbox_is_honest(cfg):
    from mailbot.dossier import build_dossier, render

    d = build_dossier("google", cfg)
    assert d["peak_hours"] == "not enough data yet"
    assert "not learned yet" in render(d)


def test_dossier_json_flag(cfg, capsys):
    from mailbot.dossier import cmd_dossier

    _seed(cfg)

    class Args:
        json = True
        account = "google"

    assert cmd_dossier(Args(), cfg) == 0
    import json as _json

    d = _json.loads(capsys.readouterr().out)
    assert d["account"] == "google"
    assert d["messages_seen"] == 3
