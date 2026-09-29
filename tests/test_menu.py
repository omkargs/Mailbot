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
    from mailbot import ui as U

    m = Menu(["a", "b"])
    m.toggle()
    out = m.render("Pick:", multi=True)
    assert f"{U.ARROW} [x] a" in out
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


def test_visual_height_accounts_for_wrapping():
    """A line longer than the terminal wraps to extra rows.

    Counting newlines undercounts, the rewind lands inside the old frame,
    and frames stack. This is the bug that smeared the channel menu across
    the whole screen.
    """
    from mailbot.menu import _visual_height

    short = "a\nb\nc"
    assert _visual_height(short, 80) == 3
    # 25 chars at width 10 -> 3 visual rows
    assert _visual_height("x" * 25, 10) == 3
    # empty line still occupies a row
    assert _visual_height("a\n\nb", 80) == 3
    # exact multiple must not collapse to one row short
    assert _visual_height("y" * 20, 10) == 2


def test_rewind_never_exceeds_frame():
    from mailbot.menu import _visual_height

    frame = "p\na\nb"
    assert max(0, _visual_height(frame, 80) - 1) == 2


def test_typing_a_nonmatching_id_offers_it_back():
    """Type-or-select: a typed id is an answer, not a dead end.

    Typing a model id that is not in the list used to print "no matches"
    and leave the user with no way to use what they had just typed.
    """
    from mailbot.menu import Menu, FREE

    m = Menu(["gpt-4", "claude-3"])
    for ch in "llama-3.1-70b":
        m.type_char(ch)
    assert m.visible() == []
    assert m.cursor == FREE
    out = m.render("Model:", False)
    assert "llama-3.1-70b" in out
    assert "no matches" not in out
    assert m.pick() == FREE


def test_typing_matching_text_still_selects_from_the_list():
    from mailbot.menu import Menu, FREE

    m = Menu(["gpt-4", "claude-3", "claude-3-haiku"])
    for ch in "haiku":
        m.type_char(ch)
    assert m.visible() == [2]
    assert m.cursor == 2
    assert m.pick() == 2
    assert "Use " not in m.render("Model:", False)


def test_backspacing_to_empty_restores_the_list():
    from mailbot.menu import Menu

    m = Menu(["a", "b"])
    m.type_char("z")
    assert m.cursor == -1
    m.backspace()
    assert m.query == ""
    assert m.cursor == 0
    assert m.visible() == [0, 1]


def test_toggle_ignores_the_free_sentinel():
    from mailbot.menu import Menu, FREE

    m = Menu(["a"])
    m.type_char("z")
    assert m.cursor == FREE
    m.toggle()          # must not raise, must not select anything
    assert m.picked_values() == []


def test_render_never_exceeds_the_terminal_width():
    """No line may exceed the width — wrapping is what caused the smear.

    A menu line wider than the pane occupies two rows, so the frame's real
    height stops matching its newline count and the redraw lands in the
    wrong place. Clipping is the fix, so assert the invariant directly.
    """
    from mailbot.menu import Menu

    opts = [
        "Bynara router — combo models, cheapest (recommended)",
        "Local via LiteLLM gateway — your own box (needs LiteLLM on :4000)",
        "a-very-long-custom-anthropic-compatible-base-url-value",
    ]
    for multi in (False, True):
        m = Menu(opts, active=opts[0])
        for w in (20, 30, 40, 60, 80, 100):
            frame = m.render("Where should the brain run?", multi, width=w)
            for line in frame.split("\n"):
                assert len(line) <= w, (w, multi, len(line), line)


def test_narrow_width_keeps_marker_and_checkbox_whole():
    from mailbot.menu import Menu

    m = Menu(["x" * 200])
    line = m.render("p", True, width=30).split("\n")[1]
    assert line.startswith("> [ ] "), line
    assert len(line) <= 30
    assert line.endswith("…")
