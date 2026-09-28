"""Two-brain triage: cheap model sorts, flagship only thinks."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from mailbot.agent import triage


def _resp(text):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=10, output_tokens=20,
                              cache_read_input_tokens=0,
                              cache_creation_input_tokens=0),
    )


def _client(monkeypatch, text):
    import mailbot.agent.client as C

    monkeypatch.setattr(C, "build_client", lambda rc: object())
    monkeypatch.setattr(C, "guarded_call", lambda client, **kw: _resp(text))


def _cfg_triage(cfg, model="cheap/triage-1"):
    cfg.router.triage_model = model
    return cfg


# ------------------------------------------------------------------ wanted

def test_pass_disabled_by_default(cfg):
    assert triage.wanted(cfg) is False


def test_pass_disabled_when_same_as_main(cfg):
    cfg.router.triage_model = cfg.router.model
    assert triage.wanted(cfg) is False


def test_pass_enabled_when_distinct(cfg):
    _cfg_triage(cfg)
    assert triage.wanted(cfg) is True


# ---------------------------------------------------------------- classify

def test_classify_parses_verdicts(cfg, monkeypatch):
    _cfg_triage(cfg)
    _client(monkeypatch, '{"verdicts": [{"id": "m1", "v": "ignore", "why": "newsletter"}, '
                         '{"id": "m2", "v": "bogus", "why": "x"}]}')
    out = triage.classify([{"id": "m1", "sender": "n@x.com", "subject": "News",
                            "snippet": "week in review"}],
                          cfg)
    assert out["m1"]["v"] == "ignore"
    assert "m2" not in out  # unknown buckets never pass through


def test_classify_failure_raises(cfg, monkeypatch):
    _cfg_triage(cfg)
    _client(monkeypatch, "no json here at all")
    with pytest.raises(ValueError):
        triage.classify([{"id": "m1", "sender": "a", "subject": "s",
                           "snippet": "x"}], cfg)


# ------------------------------------------------------------------- prune

def test_prune_archives_clean_ignore(provider, cfg):
    from mailbot.agent.tools import ToolBox
    from mailbot.storage import db

    box = ToolBox(provider, cfg, run_id=1)
    msgs = [{"id": "m1", "sender": "news@x.com", "subject": "Weekly deals",
             "snippet": "10% off everything this week"}]
    remaining, n = triage.prune(msgs, {"m1": {"v": "ignore", "why": "promo"}},
                                cfg, provider, box)
    assert n == 1 and remaining == []
    assert box.stats["triaged"] == 1
    with db.db() as c:
        row = c.execute("SELECT processed_at FROM messages WHERE id=?",
                        ("m1",)).fetchone()
    # FakeProvider inbox is not the DB; prune must not crash on that.
    assert row is None or row["processed_at"]


def test_prune_keeps_suspicious_ignore(provider, cfg):
    from mailbot.agent.tools import ToolBox

    box = ToolBox(provider, cfg, run_id=1)
    msgs = [{"id": "m9", "sender": "news@x.com", "subject": "Invoice inside",
             "snippet": "your invoice is ready"}]
    remaining, n = triage.prune(msgs, {"m9": {"v": "ignore", "why": "promo"}},
                                cfg, provider, box)
    assert n == 0 and len(remaining) == 1
    assert remaining[0]["_triage"]["v"] == "human"


# ------------------------------------------------------- runner integration

def _flagship(monkeypatch, seen):
    import mailbot.agent.runner as R
    import mailbot.agent.client as C

    monkeypatch.setattr(C, "build_client", lambda rc: object())

    def fake_call(client, **kw):
        seen.append(kw["messages"])
        return _resp("Nothing needed doing.")

    monkeypatch.setattr(R, "guarded_call", fake_call)


def test_newsletters_never_reach_flagship(provider, cfg, monkeypatch):
    import mailbot.agent.runner as R

    _cfg_triage(cfg)
    monkeypatch.setattr(
        R.triage, "classify",
        lambda items, c: {m["id"]: {"v": "ignore", "why": "promo"} for m in items})
    seen = []
    _flagship(monkeypatch, seen)
    provider.add_message(sender="news@x.com", subject="Weekly deals",
                         body="10% off everything this week, shop now")
    res = R.run_once("google", provider, cfg, messages=list(provider.inbox))
    assert res["status"] == "ok"
    assert seen == []  # flagship never called
    assert "newsletter" in res["summary"].lower() or "filed" in res["summary"].lower()


def test_triage_failure_falls_back_to_full_pass(provider, cfg, monkeypatch):
    import mailbot.agent.runner as R

    _cfg_triage(cfg)

    def boom(items, c):
        raise RuntimeError("triage down")

    monkeypatch.setattr(R.triage, "classify", boom)
    seen = []
    _flagship(monkeypatch, seen)
    provider.add_message(sender="a@b.com", subject="Hello",
                         body="Just saying hello to you friend")
    res = R.run_once("google", provider, cfg, messages=list(provider.inbox))
    assert res["status"] == "ok"
    assert len(seen) == 1  # flagship ran the full pass
