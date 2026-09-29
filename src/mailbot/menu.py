"""Tiny arrow-key menus. Zero dependencies, stdlib only.

- `choose(prompt, options)` — ↑/↓ move, Enter to pick, `/` to search.
- `multi(prompt, options)` — ↑/↓ move, Space to toggle, Enter to confirm.

Search is not a nicety: OpenRouter lists 300+ models, and scrolling
blind through them is the difference between a 10-second setup and a
2-minute one. Filtering is a plain substring match, case-insensitive.

Both degrade gracefully: no TTY, no termios (Windows), or a dumb terminal
falls back to a numbered list read from stdin. Non-interactive callers
never hang — they get `default` / `[]` immediately.
"""
from __future__ import annotations

import os
import shutil
import sys

from .ui import ARROW

_UP = ("\x1b[A", "k")
_DOWN = ("\x1b[B", "j")
_MAX_VISIBLE = 12


class Menu:
    """Testable menu state: movement, toggle, search, render. No I/O here."""

    def __init__(self, options: list[str], selected: set[int] | None = None,
                 active: str = ""):
        self.options = list(options)
        self.cursor = 0
        self.selected: set[int] = set(selected or ())
        self.active = active
        self.query = ""

    # ------------------------------------------------------------- movement
    def visible(self) -> list[int]:
        """Indices still matching the search query."""
        if not self.query:
            return list(range(len(self.options)))
        q = self.query.lower()
        return [i for i, o in enumerate(self.options) if q in o.lower()]

    def move(self, delta: int) -> None:
        vis = self.visible()
        if not vis:
            return
        try:
            pos = vis.index(self.cursor)
        except ValueError:
            pos = 0
        self.cursor = vis[(pos + delta) % len(vis)]

    def toggle(self) -> None:
        if self.cursor in self.selected:
            self.selected.discard(self.cursor)
        else:
            self.selected.add(self.cursor)

    def pick(self) -> int:
        return self.cursor

    def picked_values(self) -> list[str]:
        return [self.options[i] for i in sorted(self.selected)]

    # --------------------------------------------------------------- search
    def type_char(self, ch: str) -> None:
        self.query += ch
        vis = self.visible()
        if vis and self.cursor not in vis:
            self.cursor = vis[0]

    def backspace(self) -> None:
        self.query = self.query[:-1]
        vis = self.visible()
        if vis and self.cursor not in vis:
            self.cursor = vis[0]

    def clear_search(self) -> None:
        self.query = ""
        self.cursor = 0

    # --------------------------------------------------------------- render
    def render(self, prompt: str, multi: bool = False) -> str:
        out = [prompt]
        if self.query:
            out.append(f"  search: {self.query}   (backspace to edit, esc clears)")
        vis = self.visible()
        if not vis:
            out.append(f"  no matches for {self.query!r}")
        else:
            # Window around the cursor so 300 rows never scroll off.
            start = 0
            if len(vis) > _MAX_VISIBLE:
                pos = vis.index(self.cursor) if self.cursor in vis else 0
                start = max(0, min(pos - _MAX_VISIBLE // 2, len(vis) - _MAX_VISIBLE))
            for i in vis[start:start + _MAX_VISIBLE]:
                arrow = ARROW if i == self.cursor else " "
                active = " (active)" if self.active and self.options[i] == self.active else ""
                if multi:
                    box = "[x]" if i in self.selected else "[ ]"
                    out.append(f"{arrow} {box} {self.options[i]}{active}")
                else:
                    out.append(f"{arrow} {self.options[i]}{active}")
            if len(vis) > _MAX_VISIBLE:
                out.append(f"  … {len(vis) - _MAX_VISIBLE} more (type to search)")
        keys = "↑↓ move  •  type to search  •  esc cancel"
        if multi:
            keys = "↑↓ move  •  space select  •  type to search  •  esc cancel"
        out.append(keys + "  •  enter confirm")
        return "\n".join(out)


def _can_ansi() -> bool:
    try:
        import termios  # noqa: F401
    except ImportError:
        return False
    return sys.stdin.isatty() and sys.stdout.isatty()


# How long to wait for the rest of an escape sequence before deciding a
# lone ESC was meant on its own. Too short and arrow keys arrive as ESC
# then three junk keys; too long and ESC feels unresponsive.
_ESC_DELAY = 0.05


def _read_key(fd: int) -> str:
    """Read one keypress, disambiguating a lone ESC from a CSI sequence.

    The old version did `ch += sys.stdin.read(2)` unconditionally, so a bare
    ESC swallowed the next two characters. ESC followed by "yes<Enter>" then
    committed whatever sat under the cursor — a choice the user never made.
    Now we wait a short window for a CSI tail; if none arrives, it is a
    standalone ESC.
    """
    import select

    ch = os.read(fd, 1)
    if ch != b"\x1b":
        return ch.decode("utf-8", "replace")

    ready, _, _ = select.select([fd], [], [], _ESC_DELAY)
    if not ready:
        return "\x1b"                      # lone ESC
    rest = os.read(fd, 2)
    return (b"\x1b" + rest).decode("utf-8", "replace")


def _term_width() -> int:
    try:
        return max(20, os.get_terminal_size(sys.stdout.fileno()).columns)
    except OSError:
        try:
            return max(20, shutil.get_terminal_size((80, 24)).columns)
        except Exception:
            return 80


def _visual_height(frame: str, width: int) -> int:
    """How many terminal rows `frame` occupies.

    Counting '\\n' is wrong: a long menu line wraps to several rows, so the
    frame occupies more rows than it has newlines. Redrawing by newline
    count moved the cursor up too little, leaving the top of the old frame
    on screen — frames then stacked on every keypress and the list smeared
    sideways.
    """
    h = 0
    for line in frame.split("\n"):
        h += 1 if not line else -(-len(line) // width)   # ceil
    return h


def _run(menu: Menu, prompt: str, multi: bool):
    """Raw-mode loop. Returns cursor / selected set, None on abort.

    Redraws only the region it owns. The old code emitted ESC[2J ESC[H on
    every keypress, wiping the whole screen — inside the setup wizard that
    erased the step counter and every earlier line, scrollback included.
    """
    import termios
    import tty

    fd = sys.stdin.fileno()
    width = _term_width()
    prev_rows = 0
    try:
        old = termios.tcgetattr(fd)
    except termios.error:
        return None
    try:
        tty.setraw(fd)
        while True:
            frame = menu.render(prompt, multi)
            rows = _visual_height(frame, width)
            # Rewind to the first row of our previous frame, clear below it,
            # then repaint. Rewind is rows-1 because the cursor rests on the
            # frame's last row once written.
            if prev_rows:
                sys.stdout.write(f"\x1b[{max(0, prev_rows - 1)}A\r")
            sys.stdout.write("\x1b[J" + frame)
            sys.stdout.flush()
            prev_rows = rows

            try:
                key = _read_key(fd)
            except (EOFError, KeyboardInterrupt, OSError):
                return None
            if key in ("\r", "\n"):
                return set(menu.selected) if multi else menu.pick()
            if key in _UP:
                menu.move(-1)
            elif key in _DOWN:
                menu.move(1)
            elif key == " " and multi:
                menu.toggle()
            elif key in ("\x7f", "\b"):
                menu.backspace()
            elif key == "\x1b":
                # First ESC clears an active search; a second cancels out.
                if menu.query:
                    menu.clear_search()
                else:
                    return None
            elif key == "\x03":           # Ctrl-C always aborts
                return None
            elif key and len(key) == 1 and key.isprintable():
                if key == " " and not multi:
                    menu.move(1)          # space moves in single-select menus
                elif not key.isspace():
                    menu.type_char(key)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _fallback(prompt: str, options: list[str], multi: bool, default):
    print(prompt)
    for i, opt in enumerate(options, 1):
        print(f"  {i}. {opt}")
    try:
        ans = input(f"  Pick separated by comma{'' if multi else ' (number)'} "
                    f"[1]: ").strip() or "1"
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    try:
        if multi:
            idx = [int(x) - 1 for x in ans.split(",")]
            return [options[i] for i in idx if 0 <= i < len(options)]
        i = int(ans) - 1
        return options[i] if 0 <= i < len(options) else default
    except ValueError:
        return default


def choose(prompt: str, options: list[str], default=None, active: str = ""):
    """Single pick. Returns the option value or `default`."""
    if not options:
        return default
    if not _can_ansi():
        if not sys.stdin.isatty():
            # Not hanging is right; going silent is not. In Docker/CI/systemd
            # the menu otherwise looks like it simply doesn't exist.
            print(f"  {prompt} — no TTY, keeping {default!r}")
            return default
        return _fallback(prompt, options, False, options[0] if default is None else default)
    menu = Menu(options, active=active)
    try:
        got = _run(menu, prompt, False)
    except (KeyboardInterrupt, OSError):
        got = None
    print()
    if got is None:
        return default
    return options[got]


def multi(prompt: str, options: list[str], active: str = "") -> list[str]:
    """Multi pick. Returns picked values, possibly empty."""
    if not options:
        return []
    if not _can_ansi():
        if not sys.stdin.isatty():
            print(f"  {prompt} — no TTY, selecting nothing")
            return []
        return _fallback(prompt, options, True, [])
    menu = Menu(options, active=active)
    try:
        got = _run(menu, prompt, True)
    except (KeyboardInterrupt, OSError):
        got = None
    print()
    if got is None:
        return []
    return [options[i] for i in sorted(got)]
