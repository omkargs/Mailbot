"""Follow-up watches: armed on send, disarmed on reply, capped at two."""
from __future__ import annotations

import time


def test_watch_due_resolve_lifecycle():
    from mailbot.agent import followups as F

    assert F.due("google") == []
    F.watch("google", "t1", "m1", "a@b.com", "Re: friday", days=-1)
    rows = F.due("google")
    assert len(rows) == 1 and rows[0]["thread_id"] == "t1"
    assert F.resolve("google", "t1") is True
    assert F.due("google") == []
    assert F.resolve("google", "t1") is False


def test_watch_rearm_moves_deadline():
    from mailbot.agent import followups as F

    F.watch("google", "t2", "m1", "a@b.com", "s", days=-1)
    assert len(F.due("google")) == 1
    F.watch("google", "t2", "m2", "a@b.com", "s2", days=3)
    assert F.due("google") == []


def test_fresh_send_arms_nothing():
    from mailbot.agent import followups as F

    F.arm_for_send("google", None, "", ["a@b.com"], "hi")
    assert F.open_watches("google") == []


def test_send_arms_watch_with_thread(provider, cfg):
    from mailbot.agent import followups as F
    from mailbot.agent.tools import ToolBox
    from mailbot.storage import db

    provider.auto_send = True
    db.upsert_account("google", "me@example.com", "Me")
    db.bump_contact("google", "msk@x.com", sent=True, received=True)
    provider.add_message(sender="msk@x.com", subject="trip", body="hey join us",
                         id="m10", thread_id="t10")
    box = ToolBox(provider, cfg, run_id=1)
    out = box.run("send_message", {"to": ["msk@x.com"], "subject": "Re: trip",
                                   "body": "hey msk, confirming friday works fine",
                                   "in_reply_to": "m10"})
    assert out.get("mode") == "auto"
    watches = F.open_watches("google")
    assert len(watches) == 1 and watches[0]["thread_id"] == "t10"


def test_reply_disarms_watch(provider, cfg, monkeypatch):
    import datetime as _dt

    import mailbot.agent.runner as R
    from mailbot.agent import followups as F
    from mailbot.agent import jev as J

    cfg.jev.enabled = True
    monkeypatch.setattr(J, "decide",
                        lambda mail, _c: J.interpret({"answers": {
                            "action": {"choice": "FILE", "confidence": 0.95}}}))
    F.watch("google", "t11", "m11", "a@b.com", "s", days=30)
    now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    provider.add_message(sender="a@b.com", subject="Re: s",
                         body="hey friend, replying back now", date=now,
                         id="m12", thread_id="t11")
    R.scan(provider, cfg, notify=lambda t, approval_id="": None)
    assert F.open_watches("google") == []


def test_due_watch_fires_nudge_once(provider, cfg, monkeypatch):
    from mailbot.agent import followups as F

    import mailbot.agent.act as ACT

    F.watch("google", "t20", "m20", "a@b.com", "Re: invoice", days=-1)
    calls = []
    monkeypatch.setattr(ACT, "decide_and_act",
                        lambda data, q, cfg_, p, notify=None: (
                            calls.append((data, q)) or ("Nudge them gently.", {})))
    out = F.run_due(cfg, {"google": provider})
    assert len(out) == 1 and "Nudge" in out[0]
    # One shot: fired watches never refire.
    out2 = F.run_due(cfg, {"google": provider})
    assert out2 == [] and len(calls) == 1


def test_nudge_cap_silences_third(provider, cfg, monkeypatch):
    from mailbot.agent import followups as F
    from mailbot.storage import db

    import mailbot.agent.act as ACT

    F.watch("google", "t21", "m21", "a@b.com", "s", days=-1)
    with db.db() as c:
        c.execute("UPDATE followups SET nudge_count=2 WHERE account='google' AND thread_id='t21'")
    calls = []
    monkeypatch.setattr(ACT, "decide_and_act",
                        lambda *a, **k: (calls.append(1) or ("x", {})))
    assert F.run_due(cfg, {"google": provider}) == []
    assert calls == []


def test_followups_command_lists_watches(provider, cfg):
    from mailbot.agent import followups as F
    from mailbot.agent.chatops import build_chat_ops

    ops = build_chat_ops(cfg, lambda: {"google": provider}, notify=None)
    assert "Nobody owes" in ops["followups"]()
    F.watch("google", "t30", "m30", "boss@corp.com", "Re: deal", days=2)
    out = ops["followups"]()
    assert "boss@corp.com" in out and "deal" in out
