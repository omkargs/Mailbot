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

# Cursor value meaning "use the search text itself as the answer". Typing a
# model id that matches nothing in the list used to be a dead end: the menu
# said "no matches" and that was that, even though the user had just typed
# the exact id they wanted. Type-or-select means both are real choices.
FREE = -1


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
        if self.cursor == FREE:
            return
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
        self._settle()

    def backspace(self) -> None:
        self.query = self.query[:-1]
        self._settle()

    def _settle(self) -> None:
        """Put the cursor somewhere real after the query changed."""
        vis = self.visible()
        if not vis:
            # Nothing matched. The only thing on screen is "use this text",
            # so put the cursor there rather than on nothing at all.
            self.cursor = FREE if self.query else 0
        elif self.cursor not in vis:
            self.cursor = vis[0]

    def clear_search(self) -> None:
        self.query = ""
        self.cursor = 0

    # --------------------------------------------------------------- render
    @staticmethod
    def _clip(text: str, width: int | None) -> str:
        """Trim to the terminal width so a long line wraps to nothing.

        Wrapping is what turned the picker into a smear: a line wider than
        the pane occupies two rows, the frame's height stops matching the
        number of newlines, and the redraw lands in the wrong place. Clip
        the text, keep the marker and checkbox whole.
        """
        if not width or len(text) <= width:
            return text
        return text[:max(1, width - 1)] + "…"

    def render(self, prompt: str, multi: bool = False,
               width: int | None = None) -> str:
        out = [self._clip(prompt, width)]
        if self.query:
            out.append(self._clip(
                f"  search: {self.query}   (backspace to edit, esc clears)", width))
        vis = self.visible()
        if not vis and self.query:
            lead = f"{ARROW} ✏ Use "
            out.append(lead + self._clip(f"{self.query!r} as the value",
                                         (width - len(lead)) if width else None))
        elif not vis:
            out.append("  (nothing to pick)")
        else:
            # Window around the cursor so 300 rows never scroll off.
            start = 0
            if len(vis) > _MAX_VISIBLE:
                pos = vis.index(self.cursor) if self.cursor in vis else 0
                start = max(0, min(pos - _MAX_VISIBLE // 2, len(vis) - _MAX_VISIBLE))
            for i in vis[start:start + _MAX_VISIBLE]:
                arrow = ARROW if i == self.cursor else " "
                active = " (active)" if self.active and self.options[i] == self.active else ""
                # Reserve room for the marker, checkbox and the active tag,
                # so the option text is what gets clipped.
                lead = f"{arrow} {'[x] ' if (multi and i in self.selected) else ('[ ] ' if multi else '')}"
                tail = active if not width or len(lead) + len(active) < width else ""
                out.append(lead + self._clip(
                    f"{self.options[i]}{tail}",
                    (width - len(lead)) if width else None))
            if len(vis) > _MAX_VISIBLE:
                out.append(self._clip(
                    f"  … {len(vis) - _MAX_VISIBLE} more (type to search)", width))
        keys = "↑↓ move  •  type to search  •  esc cancel  •  enter confirm"
        if multi:
            keys = "↑↓ move  •  space select  •  type to search  •  esc cancel  •  enter confirm"
        if width and len(keys) > width:
            # Too narrow for the full legend. Drop the least important
            # parts rather than spilling onto a second row.
            keys = "↑↓ move • ␣ select • type • enter" if multi else "↑↓ move • type • enter"
            keys = self._clip(keys, width)
        out.append(keys)
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


def _set_keyboard_raw(fd: int) -> None:
    """Unbuffered keystrokes, but keep output post-processing.

    tty.setraw() also clears OPOST|ONLCR, which turns every "\n" we print
    into a bare line feed that does not return the carriage. Frames then
    draw as a staircase, each line indented by the length of the one
    above. setcbreak() clears only ICANON and ECHO - all the keyboard
    needs - and leaves the output side alone.
    """
    import termios as _t
    import tty as _tty

    _tty.setcbreak(fd, _t.TCSADRAIN)


def _cursor_row(fd: int, wait: float = 0.25) -> int | None:
    """Ask the terminal where the cursor is (DSR, ESC[6n). 0-indexed.

    Returns None if it will not say, in which case callers must assume the
    worst rather than guess.
    """
    import re
    import select as _sel

    if not sys.stdin.isatty():
        return None
    try:
        sys.stdout.write("\x1b[6n")
        sys.stdout.flush()
    except OSError:
        return None
    buf = b""
    step = 0.05
    waited = 0.0
    while waited < wait:
        ready, _, _ = _sel.select([fd], [], [], step)
        waited += step
        if not ready:
            continue
        try:
            chunk = os.read(fd, 64)
        except OSError:
            return None
        if not chunk:
            return None
        buf += chunk
        m = re.search(rb"\x1b\[(\d+);(\d+)R", buf)
        if m:
            return max(0, int(m.group(1)) - 1)
        if not buf.startswith(b"\x1b["):
            return None      # that was a keypress, not a reply
    return None


def _ensure_room(fd: int, rows: int) -> None:
    """Scroll just enough that `rows` lines fit below the cursor.

    Redrawing in place only works while the region does not move. Drawn
    near the bottom of a window, every keypress scrolls the screen, the
    cursor-up can no longer reach our own previous frame, and the list
    marches down taking the wizard's earlier output with it. Moving up
    front, the frame sits still for the rest of the run.
    """
    try:
        height = os.get_terminal_size(sys.stdout.fileno()).lines
    except OSError:
        return
    if height <= 0 or rows <= 0 or rows >= height:
        return
    row = _cursor_row(fd)
    if row is None:
        return          # terminal will not say; leave it alone
    need = row + rows - (height - 1)
    if need > 0:
        sys.stdout.write("\n" * need)
        sys.stdout.flush()


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
        _set_keyboard_raw(fd)
        # A menu drawn low in the window makes the terminal scroll on every
        # keypress, and once it scrolls, cursor-up cannot reach our own
        # previous frame. The list then marches down the screen and takes
        # the wizard's earlier output with it. Make room first, once, so
        # every redraw lands in the same place.
        _ensure_room(fd, _visual_height(menu.render(prompt, multi, width=width), width))
        while True:
            frame = menu.render(prompt, multi, width=width)
            rows = _visual_height(frame, width)
            # Rewind to the first row of our previous frame, clear below it,
            # then repaint. Rewind is rows-1 because the cursor rests on the
            # frame's last row once written.
            if prev_rows:
                sys.stdout.write(f"\x1b[{max(0, prev_rows - 1)}A\r")
            # CRLF, not LF. Raw mode strips ONLCR, so a bare \n is only a
            # line feed and leaves the column where the last line ended.
            sys.stdout.write("\x1b[J" + frame.replace("\n", "\r\n"))
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
    if got == FREE:
        # The user typed an id that is not in the list. That is a valid
        # answer, not a mistake — hand back exactly what they typed.
        return menu.query
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
