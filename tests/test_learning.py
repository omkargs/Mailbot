"""Approval learning: earned proposals, never silent grants."""
from __future__ import annotations


def _approve(provider, cfg, aid, to="priya@studio.com"):
    from mailbot.agent.runner import run_approval
    from mailbot.storage import db

    db.create_approval(aid, "google", "send",
                       {"to": [to], "subject": "x",
                        "body": "hello friend, confirming friday works fine",
                        "in_reply_to": "", "attachments": []},
                       reason="t")
    notes = []
    res = run_approval("google", provider, cfg, aid, True, notify=notes.append)
    assert res["sent"] is True
    return notes


def test_second_approval_proposes_auto_send(provider, cfg):
    notes1 = _approve(provider, cfg, "ap_learn000001")
    assert not any("without asking" in n for n in notes1)
    notes2 = _approve(provider, cfg, "ap_learn000002")
    assert any("priya@studio.com" in n and "without asking" in n for n in notes2), notes2


def test_proposal_never_repeats(provider, cfg):
    _approve(provider, cfg, "ap_learn000011")
    _approve(provider, cfg, "ap_learn000012")
    notes3 = _approve(provider, cfg, "ap_learn000013")
    assert not any("without asking" in n for n in notes3)


def test_denial_breaks_the_streak(provider, cfg):
    from mailbot.agent.runner import run_approval
    from mailbot.storage import db

    _approve(provider, cfg, "ap_learn000021")
    db.create_approval("ap_learn000022", "google", "send",
                       {"to": ["priya@studio.com"], "subject": "x",
                        "body": "hello friend, confirming friday works fine",
                        "in_reply_to": "", "attachments": []},
                       reason="t")
    run_approval("google", provider, cfg, "ap_learn000022", False)
    notes = _approve(provider, cfg, "ap_learn000023")
    assert not any("without asking" in n for n in notes)


def test_no_proposal_when_already_auto(provider, cfg):
    from mailbot.storage import db

    db.set_contact_auto_send("google", "priya@studio.com", True)
    notes = _approve(provider, cfg, "ap_learn000031")
    notes += _approve(provider, cfg, "ap_learn000032")
    assert not any("without asking" in n for n in notes)
