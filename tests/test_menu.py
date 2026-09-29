"""Menu engine: state machine + graceful fallbacks. No TTY needed."""
from __future__ import annotations

from mailbot.menu import Menu, choose, multi


def test_move_wraps_around():
    m = Menu(["a", "b", "c"])
    m.move(1)
    assert m.pick() == 1
    m.move(2)
    assert m.pick() == 0
    m.move(-1)
    assert m.pick() == 2


def test_toggle_and_picked_values():
    m = Menu(["a", "b", "c"])
    m.toggle()
    m.move(2)
    m.toggle()
    assert m.picked_values() == ["a", "c"]
    m.toggle()
    assert m.picked_values() == ["a"]


def test_render_marks_cursor_and_selection():
    m = Menu(["a", "b"])
    m.toggle()
    out = m.render("Pick:", multi=True)
    assert "❯ [x] a" in out
    assert "  [ ] b" in out
    assert "space select" in out


def test_choose_empty_options_returns_default():
    assert choose("P:", [], default="d") == "d"
    assert multi("P:", []) == []


def test_non_tty_returns_defaults(monkeypatch):
    import sys

    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert choose("P:", ["a", "b"], default="d") == "d"
    assert multi("P:", ["a", "b"]) == []


def test_fallback_numbered_pick(monkeypatch):
    import sys
    from mailbot import menu as M

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(M, "_can_ansi", lambda: False)
    monkeypatch.setattr("builtins.input", lambda *a: "2")
    assert choose("P:", ["a", "b", "c"]) == "b"


def test_fallback_multi_pick(monkeypatch):
    import sys
    from mailbot import menu as M

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(M, "_can_ansi", lambda: False)
    monkeypatch.setattr("builtins.input", lambda *a: "1,3")
    assert multi("P:", ["a", "b", "c"]) == ["a", "c"]
