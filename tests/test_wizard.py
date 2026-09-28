"""Wizard: env import, defaults, doctor. No network, no TTY needed."""
from __future__ import annotations

import json
import os


def _fresh_config(monkeypatch, tmp_path):
    import mailbot.config as C

    monkeypatch.setattr(C, "CONFIG_DIR", tmp_path / "cfg")
    monkeypatch.setattr(C, "CONFIG_PATH", tmp_path / "cfg" / "config.json")
    monkeypatch.setattr(C, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(C, "DB_PATH", tmp_path / "data" / "agent.db")
    from mailbot import setup as S

    monkeypatch.setattr(S, "config_dir", lambda: tmp_path / "cfg")
    from mailbot import wizard as W

    monkeypatch.setattr(W, "config_dir", lambda: tmp_path / "cfg")
    return C, S, W


def test_import_env_writes_secrets_without_tty(monkeypatch, tmp_path):
    C, S, W = _fresh_config(monkeypatch, tmp_path)
    monkeypatch.setenv("ROUTER_API_KEY", "sk-test-123")
    monkeypatch.setenv("ROUTER_MODEL", "combo/claude2mail")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    written = W.import_env()
    assert "ROUTER_API_KEY" in written
    secrets = S.read_secrets()
    assert secrets["ROUTER_API_KEY"] == "sk-test-123"
    mode = (tmp_path / "cfg" / ".secrets").stat().st_mode & 0o777
    assert mode == 0o600


def test_import_env_accepts_inline_google_json(monkeypatch, tmp_path):
    C, S, W = _fresh_config(monkeypatch, tmp_path)
    blob = json.dumps({"installed": {"client_id": "x"}})
    monkeypatch.setenv("GOOGLE_CREDENTIALS", blob)
    W.import_env()
    dest = tmp_path / "cfg" / "google-credentials.json"
    assert dest.exists()
    assert json.loads(dest.read_text())["installed"]["client_id"] == "x"


def test_apply_defaults_fills_gaps_only(monkeypatch, tmp_path):
    C, S, W = _fresh_config(monkeypatch, tmp_path)
    monkeypatch.setenv("ROUTER_MODEL", "custom/model")
    for k in ("ROUTER_BASE_URL", "ROUTER_API_KEY", "ROUTER_MODEL",
              "GOOGLE_CREDENTIALS", "GOOGLE_ACCOUNT", "TELEGRAM_BOT_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    W.apply_defaults()
    s = S.read_secrets()
    assert s["ROUTER_BASE_URL"] == "https://router.bynara.id"
    assert s["ROUTER_MODEL"] == "combo/claude2mail"
    assert s["AGENT_SEND_MODE"] == "auto"


def test_setup_non_interactive_fails_loudly_without_key(monkeypatch, tmp_path, capsys):
    C, S, W = _fresh_config(monkeypatch, tmp_path)
    for k in ("ROUTER_API_KEY", "ROUTER_BASE_URL", "ROUTER_MODEL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("MAIL_AGENT_CONFIG_DIR", raising=False)

    class Args:
        step = "provider"
        non_interactive = True
        yes = True
        import_env = False
        skip_voice = True
        skip_service = True

    rc = W.cmd_setup(Args(), C.load())
    assert rc == 1
    out = capsys.readouterr().out
    assert "ROUTER_API_KEY" in out


def test_doctor_reports_problems_not_ok(monkeypatch, tmp_path, capsys):
    C, S, W = _fresh_config(monkeypatch, tmp_path)
    for k in ("ROUTER_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(k, raising=False)

    class Args:
        pass

    rc = W.cmd_doctor(Args(), C.load())
    assert rc == 1
    out = capsys.readouterr().out
    assert "doctor:" in out


def _setup_args(**kw):
    class Args:
        step = ""
        non_interactive = False
        yes = False
        fast = False
        dry_run = False
        import_env = False
        skip_voice = False
        skip_service = False

    for k, v in kw.items():
        setattr(Args, k, v)
    return Args()


def test_dry_run_writes_nothing(monkeypatch, tmp_path, capsys):
    C, S, W = _fresh_config(monkeypatch, tmp_path)
    rc = W.cmd_setup(_setup_args(dry_run=True), C.load())
    assert rc == 0
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert not (tmp_path / "cfg" / ".secrets").exists()
    assert not (tmp_path / "cfg" / "config.json").exists()


def test_fast_skips_voice_service_chat(monkeypatch, tmp_path, capsys):
    C, S, W = _fresh_config(monkeypatch, tmp_path)
    for k in ("ROUTER_API_KEY", "ROUTER_BASE_URL", "ROUTER_MODEL"):
        monkeypatch.delenv(k, raising=False)
    rc = W.cmd_setup(_setup_args(fast=True), C.load())
    assert rc == 1  # missing key + creds: honest failure, not a crash
    out = capsys.readouterr().out
    assert "[1/" in out
    assert "finished in" in out
    state = __import__("json").loads((tmp_path / "cfg" / ".setup-state.json").read_text())
    assert state.get("provider") == "missing-key"
    assert "voice" not in state  # skipped, not failed
    assert "service" not in state


def test_import_env_picks_up_triage_model(monkeypatch, tmp_path):
    from mailbot import wizard as W
    from mailbot import setup as S
    from mailbot import config as C

    monkeypatch.setattr(C, "CONFIG_DIR", tmp_path / "cfg")
    monkeypatch.setattr(S, "config_dir", lambda: tmp_path / "cfg")
    monkeypatch.setattr(W, "config_dir", lambda: tmp_path / "cfg")
    monkeypatch.setenv("ROUTER_TRIAGE_MODEL", "cheap/triage-1")
    assert "ROUTER_TRIAGE_MODEL" in W.import_env()
    assert S.read_secrets()["ROUTER_TRIAGE_MODEL"] == "cheap/triage-1"
