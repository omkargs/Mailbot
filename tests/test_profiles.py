"""Profiles step A: model + migration, zero behaviour change."""
from __future__ import annotations


def _isolate(monkeypatch, tmp_path):
    from mailbot import profiles as P
    from mailbot import config as C
    from mailbot.storage import db as D

    monkeypatch.setattr(C, "CONFIG_DIR", tmp_path / "cfg")
    monkeypatch.setattr(C, "CONFIG_PATH", tmp_path / "cfg" / "config.json")
    monkeypatch.setattr(C, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(C, "DB_PATH", tmp_path / "data" / "agent.db")
    monkeypatch.setattr(D, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(D, "DB_PATH", tmp_path / "data" / "agent.db")
    monkeypatch.setattr(P, "CONFIG_PATH", tmp_path / "cfg" / "config.json")


def test_migration_creates_personal_profile(monkeypatch, tmp_path):
    from mailbot import profiles as P

    _isolate(monkeypatch, tmp_path)
    assert P.ensure_migrated() is True
    rows = P.list_profiles()
    assert len(rows) == 1
    assert rows[0]["id"] == "personal"
    assert rows[0]["account"] == "google"
    assert P.get_current()["id"] == "personal"
    # config.json mirrors it
    import json

    data = json.loads((tmp_path / "cfg" / "config.json").read_text())
    assert data["profiles"][0]["id"] == "personal"


def test_migration_is_idempotent(monkeypatch, tmp_path):
    from mailbot import profiles as P

    _isolate(monkeypatch, tmp_path)
    assert P.ensure_migrated() is True
    assert P.ensure_migrated() is False
    assert len(P.list_profiles()) == 1


def test_model_override_falls_back():
    from mailbot import profiles as P

    assert P.model_for(None, "main/x") == "main/x"
    assert P.model_for({"model_override": ""}, "main/x") == "main/x"
    assert P.model_for({"model_override": "cheap/y"}, "main/x") == "cheap/y"


def test_header_format():
    from mailbot import profiles as P

    assert P.header_for(None) == ""
    assert P.header_for(None, "a@b.com") == "[a@b.com]"
    assert P.header_for({"name": "Family", "account": "google"},
                        "me@gmail.com") == "[Family · me@gmail.com]"
    assert P.header_for({"name": "Work", "account": "ms"}) == "[Work · ms]"


def test_active_binds_profiles_to_providers(provider, cfg):
    from mailbot import profiles as P

    P.ensure_migrated()
    bound = P.active(cfg, {"google": provider})
    assert len(bound) == 1
    prof, p = bound[0]
    assert prof["id"] == "personal" and p is provider


def test_run_once_uses_profile_model_override(provider, cfg, monkeypatch):
    from types import SimpleNamespace
    import mailbot.agent.runner as R
    import mailbot.agent.client as C

    monkeypatch.setattr(C, "build_client", lambda rc: object())
    seen = {}

    def fake_call(client, **kw):
        seen["model"] = kw["model"]
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="Nothing needed doing.")],
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=1, output_tokens=1,
                                  cache_read_input_tokens=0,
                                  cache_creation_input_tokens=0))

    monkeypatch.setattr(R, "guarded_call", fake_call)
    provider.add_message(sender="a@b.com", subject="Hello",
                         body="Just saying hello to you friend")
    R.run_once("google", provider, cfg, messages=list(provider.inbox),
               model="cheap/custom-1")
    assert seen["model"] == "cheap/custom-1"


def test_run_once_defaults_to_global_model(provider, cfg, monkeypatch):
    from types import SimpleNamespace
    import mailbot.agent.runner as R
    import mailbot.agent.client as C

    monkeypatch.setattr(C, "build_client", lambda rc: object())
    seen = {}

    def fake_call(client, **kw):
        seen["model"] = kw["model"]
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="Nothing needed doing.")],
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=1, output_tokens=1,
                                  cache_read_input_tokens=0,
                                  cache_creation_input_tokens=0))

    monkeypatch.setattr(R, "guarded_call", fake_call)
    provider.add_message(sender="a@b.com", subject="Hello",
                         body="Just saying hello to you friend")
    R.run_once("google", provider, cfg, messages=list(provider.inbox))
    assert seen["model"] == cfg.router.model


def test_set_current_switches_exactly_one(monkeypatch, tmp_path):
    from mailbot import profiles as P
    from mailbot.storage import db

    _isolate(monkeypatch, tmp_path)
    P.ensure_migrated()
    with db.db() as c:
        c.execute("INSERT INTO profiles (id, name, account, model_override, is_current, created_at)"
                  " VALUES (?, ?, ?, ?, ?, ?)",
                  ("work", "Work", "google", "", 0, "2026-01-01"))
    assert P.set_current("work")["id"] == "work"
    assert P.get_current()["id"] == "work"
    assert P.set_current("nope") is None
    assert P.get_current()["id"] == "work"


def test_chat_profiles_and_switch_and_whoami(provider, cfg):
    from mailbot.agent.chat import handle_text
    from mailbot import profiles as P

    P.ensure_migrated()
    out = handle_text("/profiles", cfg, {"google": provider}, {})
    assert "Personal" in out and "/change-profile" in out
    assert "No profile" in handle_text("/change-profile nope", cfg, {"google": provider}, {})
    out = handle_text("/whoami", cfg, {"google": provider}, {})
    assert "Personal" in out and "brain:" in out


def test_toolbox_tags_carry_profile(provider, cfg):
    from mailbot.agent.tools import ToolBox
    from mailbot import profiles as P

    P.ensure_migrated()
    cur = P.get_current()
    sent = []
    box = ToolBox(provider, cfg, run_id=1,
                  notify=lambda t, approval_id="": sent.append(t) or "1",
                  profile=cur)
    box.run("send_message", {"to": ["new@x.com"], "subject": "Hi",
                             "body": "Just saying hello to you friend"})
    assert sent and sent[0].startswith("[Personal ·")


def test_slug():
    from mailbot import profiles as P

    assert P.slug("Family Mail") == "family-mail"
    assert P.slug("  Work! ") == "work"
    assert P.slug("") == "inbox"


def test_add_profile_registers_inbox(monkeypatch, tmp_path):
    import json as _json
    from mailbot import profiles as P

    _isolate(monkeypatch, tmp_path)
    row = P.add_profile("Family", "google:family")
    assert row["id"] == "family"
    assert row["account"] == "google:family"
    assert row["is_current"] == 0  # personal stays current
    data = _json.loads((tmp_path / "cfg" / "config.json").read_text())
    assert any(p["id"] == "family" for p in data["profiles"])
    import pytest as _pytest

    with _pytest.raises(ValueError):
        P.add_profile("Family", "google:family")


def test_factory_builds_extra_inbox(monkeypatch, tmp_path):
    import json as _json
    from mailbot import profiles as P
    from mailbot.config import load as _load
    from mailbot.providers import build_providers

    _isolate(monkeypatch, tmp_path)
    P.ensure_migrated()
    P.add_profile("Family", "google:family")
    creds = P.creds_file("family")
    creds.parent.mkdir(parents=True, exist_ok=True)
    creds.write_text(_json.dumps({"installed": {"client_id": "x"}}))
    provs = build_providers(_load())
    assert "google:family" in provs
    assert provs["google:family"].display_name == "Family"


def test_factory_skips_inbox_without_creds(monkeypatch, tmp_path):
    from mailbot import profiles as P
    from mailbot.config import load as _load
    from mailbot.providers import build_providers

    _isolate(monkeypatch, tmp_path)
    P.ensure_migrated()
    P.add_profile("Family", "google:family")
    provs = build_providers(_load())
    assert "google:family" not in provs
