"""The menu, driven through a real PTY.

Unit tests on Menu.render() cannot see any of this. The staircase, the
scrolling, and the rewind all live in the escape sequences the terminal
actually receives, and every one of them was invisible until someone ran
it for real and pasted the result.
"""
import os
import pty
import re
import select
import struct
import fcntl
import termios
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO, "src")


def drive(code: str, keys: list[bytes], rows: int = 30, cols: int = 100,
          settle: float = 0.25) -> str:
    """Run `code` in a PTY of the given size, send keys, return the output."""
    pid, fd = pty.fork()
    if pid == 0:
        os.environ["TERM"] = "xterm-256color"
        os.environ["PYTHONPATH"] = SRC
        os.execv(sys.executable, [sys.executable, "-c", code])
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    time.sleep(0.5)
    for k in keys:
        os.write(fd, k)
        time.sleep(settle)
    time.sleep(0.3)
    out = b""
    while select.select([fd], [], [], 0.6)[0]:
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            break
        if not chunk:
            break
        out += chunk
    # The child may still be blocked in read() waiting for a key we never
    # sent. Kill it, or waitpid hangs the whole suite.
    try:
        os.kill(pid, 9)
    except OSError:
        pass
    try:
        os.waitpid(pid, 0)
    except OSError:
        pass
    os.close(fd)
    return out.decode("utf-8", "replace")


def screen(raw: str, rows: int, cols: int) -> list[str]:
    """Replay the output into a screen, so we can see what a person sees."""
    import re as _re

    scr = [[" "] * cols for _ in range(rows)]
    r = c = 0
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch == "\x1b":
            m = _re.match(r"\x1b\[(\d*)([A-Za-z])", raw[i:])
            if m:
                n = int(m.group(1) or 1)
                cmd = m.group(2)
                if cmd == "A":
                    r = max(0, r - n)
                elif cmd == "B":
                    r = min(rows - 1, r + n)
                elif cmd == "J":
                    for rr in range(r, rows):
                        for cc in range(c if rr == r else 0, cols):
                            scr[rr][cc] = " "
                i += m.end()
                continue
            i += 1
            continue
        if ch == "\r":
            c = 0
            i += 1
            continue
        if ch == "\n":
            r = min(rows - 1, r + 1)
            i += 1
            continue
        if ch == "\x08":
            c = max(0, c - 1)
            i += 1
            continue
        if c >= cols:
            c = cols - 1
        scr[r][c] = ch
        c += 1
        i += 1
    return ["".join(row).rstrip() for row in scr]


MENU = (
    "import sys\n"
    "from mailbot.menu import choose\n"
    "r = choose('Pick one', ['alpha', 'beta', 'gamma', 'delta'])\n"
    "sys.stdout.write('GOT=' + repr(r) + '\\n')\n"
)


def test_options_are_not_drawn_as_a_staircase():
    """Every option line must start in the same column.

    tty.setraw() clears OPOST|ONLCR, so a bare \\n stops returning the
    carriage and each line begins where the one above ended. The list
    then leans down the screen, one indent per line.
    """
    raw = drive(MENU, [b"\x1b[B", b"\x1b[B", b"\r"])
    lines = screen(raw, 30, 100)
    opt_cols = {}
    for ln in lines:
        for name in ("alpha", "beta", "gamma", "delta"):
            m = re.search(rf"(\S*)\s*{name}\b", ln)
            if m:
                opt_cols.setdefault(name, m.start(1) if m.group(1) else m.start())
    assert len(opt_cols) >= 3, f"could not find options on screen: {lines}"
    # alpha/beta/gamma/delta all sit at the same indent.
    cols = set(opt_cols.values())
    assert len(cols) == 1, f"options drawn at differing columns {opt_cols}"


def test_no_full_screen_clear_ever():
    """ESC[2J wiped the wizard's earlier output and the scrollback with it."""
    raw = drive(MENU, [b"\x1b[B", b"\x1b[B", b"\x1b[B", b"\r"])
    assert "\x1b[2J" not in raw


def test_redraw_rewinds_by_the_frame_height():
    raw = drive(MENU, [b"\x1b[B", b"\x1b[B", b"\r"])
    ups = [int(m) for m in re.findall(r"\x1b\[(\d+)A", raw)]
    assert ups, "never rewound"
    # Pick one + 4 options + legend = 6 lines, so rewind is 5.
    assert all(u == 5 for u in ups), ups


def test_menu_low_in_the_window_does_not_scroll_away_the_output():
    """Drawn near the bottom, every keypress used to scroll the screen.

    Once it scrolls, cursor-up cannot reach our own previous frame, and
    the list marches down the screen taking the wizard's output with it.
    """
    code = (
        "import sys\n"
        "print('\\n'.join('  wizard line %d' % i for i in range(16)))\n"
        "from mailbot.menu import choose\n"
        "r = choose('Pick one', ['alpha', 'beta', 'gamma', 'delta'])\n"
        "sys.stdout.write('GOT=' + repr(r) + '\\n')\n"
    )
    raw = drive(code, [b"\x1b[B", b"\x1b[B", b"\r"], rows=24, cols=80)
    out = screen(raw, 24, 80)
    joined = "\n".join(out)
    assert "wizard line 0" in joined, f"wizard output scrolled off:\n{joined}"
    # And the options are still aligned.
    seen = [ln for ln in out if re.search(r"\b(alpha|beta|gamma|delta)\b", ln)]
    assert len(seen) >= 3, out


def test_menu_still_works_in_a_small_window():
    raw = drive(MENU, [b"\x1b[B", b"\r"], rows=20, cols=52)
    assert "GOT='beta'" in raw


def test_typing_filters_and_offers_the_typed_value():
    code = (
        "import sys\n"
        "from mailbot.menu import choose\n"
        "r = choose('Model', ['gpt-4', 'claude-3'])\n"
        "sys.stdout.write('GOT=' + repr(r) + '\\n')\n"
    )
    raw = drive(code, [b"my-own-model", b"\r"])
    assert "GOT='my-own-model'" in raw
