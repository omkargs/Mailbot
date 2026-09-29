"""Onboarding UX pass: bare help, did-you-mean, honest colors, early validation."""
from __future__ import annotations


def test_supports_color_respects_env(monkeypatch):
    import importlib
    import mailbot.ui as U

    monkeypatch.setenv("NO_COLOR", "1")
    importlib.reload(U)
    assert U.supports_color() is False
    assert U.BOLD == ""
    monkeypatch.delenv("NO_COLOR")
    importlib.reload(U)


def test_did_you_mean_suggests(capsys):
    from mailbot import cli as C
    import sys

    monkeypatch_argv = ["mail-agent", "statuz"]
    old = sys.argv
    sys.argv = monkeypatch_argv
    try:
        try:
            C.main()
        except SystemExit as e:
            assert e.code == 2
    finally:
        sys.argv = old
    err = capsys.readouterr().err
    assert "did you mean" in err and "status" in err


def test_bare_run_shows_concise_help(capsys):
    from mailbot import cli as C
    import sys

    old = sys.argv
    sys.argv = ["mail-agent"]
    try:
        try:
            rc = C.main()
        except SystemExit as e:
            rc = e.code
    finally:
        sys.argv = old
    out = capsys.readouterr().out
    assert rc == 2
    assert "setup --fast" in out and "demo" in out


def test_telegram_token_shape():
    import re

    good = "123456:ABCdefGHIjklMNOpqrSTUvwxYZ123456789"
    assert re.match(r"^\d+:[\w-]{30,}$", good)
    assert not re.match(r"^\d+:[\w-]{30,}$", "mybot")
    assert not re.match(r"^\d+:[\w-]{30,}$", "123:short")
