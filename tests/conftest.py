"""Shared fixtures. Every test runs against a temp DB and mock providers —
no live mailbox is ever touched."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest


def _parse_dt(v):
    import datetime as _dt
    if not v:
        return None
    try:
        d = _dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except Exception:
        return None
    return d if d.tzinfo else d.replace(tzinfo=_dt.timezone.utc)

_tmp = tempfile.mkdtemp(prefix="mailagent-test-")
os.environ["MAIL_AGENT_DB"] = str(Path(_tmp) / "test.db")
os.environ["MAIL_AGENT_DATA_DIR"] = _tmp
os.environ["MAIL_AGENT_CONFIG_DIR"] = str(Path(_tmp) / "config")
# The voice profile lives in the repo's brain/ dir by default, which on a
# real machine holds the user's actual profile. A test asserting "not learned
# yet" must not read that file — isolate the brain like everything else.
os.environ["MAIL_AGENT_BRAIN"] = str(Path(_tmp) / "brain")


@pytest.fixture(autouse=True)
def clean_db():
    from mailbot.storage import db
    from mailbot import limits

    db.migrate()
    # chat_history is created lazily by chatlog._ensure(); make sure it exists
    # before wiping, or the first chat test errors on a missing table.
    from mailbot.agent import chatlog as _chatlog

    _chatlog._ensure()
    # Child tables first — drafts references runs, messages references accounts.
    for table in ("drafts", "approvals", "actions_log", "messages", "cursors",
                  "runs", "contacts", "skills", "labels", "usage_daily",
                  "scheduled_jobs", "surfaced", "chat_history", "accounts"):
        with db.db() as c:
            c.execute(f"DELETE FROM {table}")
    # Spend limits are process-global so every thread shares one budget. Reset
    # per test so one test's usage cannot exhaust the next test's budget.
    L = limits.global_limits()
    L._calls.clear()
    L._failures = 0
    L._cooldown = 0.0
    L._open_until = 0.0
    L.daily_token_cap = 10_000_000
    yield


class FakeProvider:
    """In-memory provider. Records what it was asked to do."""

    def __init__(self, account="google", auto_send=False, calendar_enabled=True):
        from mailbot.providers.base import MailProvider

        self.account = account
        self.address = "me@example.com"
        self.display_name = "Me"
        self.auto_send = auto_send
        self.calendar_enabled = calendar_enabled
        self.sent: list[dict] = []
        self.drafted: list[dict] = []
        self.labels: dict[str, str] = {"lbl_1": "Urgent"}
        self.events: list[dict] = []
        self.inbox: list[dict] = []
        self.get_messages_calls: list[list[str]] = []
        self._next = 1
        MailProvider.register(FakeProvider)

    def _id(self):
        self._next += 1
        return f"m{self._next}"

    def add_message(self, sender="a@b.com", subject="Hello", body="Body text here", **kw):
        mid = self._id()
        msg = {
            "id": mid, "account": self.account, "thread_id": f"t{mid}",
            "sender": sender, "sender_name": "", "recipients": "me@example.com",
            "subject": subject, "snippet": body[:100], "body": body,
            "date": "2026-09-26T10:00:00+00:00", "label_ids": ["INBOX"],
            "has_attach": False, "size_bytes": 100,
        }
        msg.update(kw)
        self.inbox.insert(0, msg)
        return msg

    # --- interface ---
    def valid(self): return True
    def authenticate(self): return True
    def list_messages(self, folder="INBOX", limit=20, after_id=None, newer_than_days=0):
        # Honour the first-run window the way a real provider would, so the
        # tests exercise the same path production does.
        out = list(self.inbox)
        if newer_than_days and not after_id:
            import datetime as _dt
            cut = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=newer_than_days)
            out = [m for m in out
                   if _parse_dt(m.get("date", "")) and _parse_dt(m["date"]) >= cut]
        return out[:limit]
    def get_message(self, message_id):
        return next((m for m in self.inbox if m["id"] == message_id), None)
    def get_messages(self, message_ids):
        # Implemented rather than inherited so tests can assert the batch was
        # actually asked for. Inheriting the per-id fallback hid a real bug:
        # the caller passed a set, which is not sliceable, so the batch call
        # raised and body prefetch silently did nothing on every run.
        self.get_messages_calls.append(list(message_ids))
        return [m for m in (self.get_message(i) for i in message_ids) if m]
    def get_thread(self, thread_id):
        return [m for m in self.inbox if m["thread_id"] == thread_id]
    def search(self, query="", sender="", subject="", since="", limit=25, full=False):
        return self.inbox[:limit]
    def apply_label(self, message_id, label_id, add=True): return True
    def mark_read(self, message_id, read=True): return True
    def archive(self, message_id): return True
    def create_label(self, name): return self.labels.setdefault(name, f"lbl_{name}")
    def list_labels(self): return [{"id": k, "name": v} for k, v in self.labels.items()]
    def create_draft(self, req):
        did = f"d{self._id()}"
        self.drafted.append({"to": req.to, "subject": req.subject, "body": req.body})
        return did
    def send(self, req):
        self.sent.append({"to": req.to, "subject": req.subject, "body": req.body})
        return True
    def list_events(self, limit=20, time_min=""): return self.events
    def create_event(self, req):
        ev = {"id": f"e{self._id()}", "summary": req.summary}
        self.events.append(ev)
        return ev
    def delete_event(self, event_id): return True


@pytest.fixture
def provider():
    return FakeProvider()


@pytest.fixture
def cfg():
    from mailbot.config import Config

    c = Config()
    c.agent.send_mode = "auto"
    c.agent.auto_send_contacts = ["boss@corp.com"]
    c.agent.daily_token_cap = 10_000_000
    # A dummy key so build_client does not refuse before the mock intercepts.
    c.router.api_key = "test-key-not-real"
    # These tests exercise the flagship path, not the decider. Jev stays ON
    # in production (it falls back to the router's own URL/key, which is how
    # Bynara serves the `jev` model); here there is no network, so the suite
    # says explicitly which brain it is testing. test_jev.py enables it.
    c.jev.enabled = False
    return c
