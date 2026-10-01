"""Push honesty: loud once on dead credentials, silent retries, live state."""
from __future__ import annotations


def _listener(**kw):
    from mailbot.agent.idle import IdleListener
    from mailbot.config import GoogleConfig

    base = dict(cfg=GoogleConfig(), on_new=lambda: None, password="pw")
    base.update(kw)
    return IdleListener(**base)


def test_classify_auth_vs_transient():
    import imaplib

    from mailbot.agent.idle import classify_failure

    assert classify_failure(imaplib.IMAP4.error(
        b"[AUTHENTICATIONFAILED] Invalid credentials (Failure)")) == "auth"
    assert classify_failure(ConnectionError("reset by peer")) == "transient"
    assert classify_failure(OSError("timed out")) == "transient"


def test_auth_failure_notifies_once_and_flags(monkeypatch):
    import imaplib

    import mailbot.agent.idle as I

    told = []
    L = _listener(notify=told.append)

    class BadIMAP:
        def __init__(self, *a, **k):
            pass

        def login(self, *a):
            raise imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] Invalid credentials")

    monkeypatch.setattr(I.imaplib, "IMAP4_SSL", BadIMAP)
    assert L._connect() is None
    assert L.auth_failed is True
    assert len(told) == 1 and "App passwords" in told[0]
    # Second failure: flagged, no second ping.
    assert L._connect() is None
    assert len(told) == 1


def test_transient_failure_stays_quiet(monkeypatch):
    import mailbot.agent.idle as I

    told = []
    L = _listener(notify=told.append)

    def boom(*a, **k):
        raise ConnectionError("peer reset")

    monkeypatch.setattr(I.imaplib, "IMAP4_SSL", boom)
    assert L._connect() is None
    assert L.auth_failed is False and told == []


def test_fresh_password_heals_without_restart(monkeypatch):
    import imaplib

    import mailbot.agent.idle as I
    from mailbot import setup as S

    told = []
    L = _listener(password="old-dead", notify=told.append)
    L.auth_failed = True
    L._told_auth_dead = True

    class GoodIMAP:
        def __init__(self, *a, **k):
            pass

        def login(self, user, pw):
            assert pw == "fresh-password-12"
            self.user = user

        def select(self, box):
            return ("OK", [])

    monkeypatch.setattr(I.imaplib, "IMAP4_SSL", GoodIMAP)
    # _current_password imports read_secrets from ..setup at call time,
    # so patching the module attribute is enough — no restart needed.
    monkeypatch.setattr(S, "read_secrets",
                        lambda: {"GOOGLE_IMAP_PASSWORD": "fresh-password-12"},
                        raising=False)
    assert L._connect() is not None
    assert L.auth_failed is False
    assert any("back" in t for t in told)
