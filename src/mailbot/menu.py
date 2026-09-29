"""Tiny arrow-key menus. Zero dependencies, stdlib only.

- `choose(prompt, options)` — ↑/↓ to move, Enter to pick. Returns the
  picked option (the value, not the index), or `default` on Ctrl-C / EOF.
- `multi(prompt, options)` — ↑/↓ to move, Space to toggle, Enter to
  confirm. Returns the list of picked values (possibly empty).

Both degrade gracefully: no TTY, no termios (Windows), or dumb terminal
falls back to a numbered list read from stdin. Non-interactive callers
never hang — they get `default` / `[]` immediately.
"""
from __future__ import annotations

import sys

_UP = ("\x1b[A", "k")
_DOWN = ("\x1b[B", "j")


class Menu:
    """Testable menu state: movement, toggle, render. No I/O here."""

    def __init__(self, options: list[str], selected: set[int] | None = None):
        self.options = list(options)
        self.cursor = 0
        self.selected: set[int] = set(selected or ())

    def move(self, delta: int) -> None:
        if self.options:
            self.cursor = (self.cursor + delta) % len(self.options)

    def toggle(self) -> None:
        if self.cursor in self.selected:
            self.selected.discard(self.cursor)
        else:
            self.selected.add(self.cursor)

    def pick(self) -> int:
        return self.cursor

    def picked_values(self) -> list[str]:
        return [self.options[i] for i in sorted(self.selected)]

    def render(self, prompt: str, multi: bool = False) -> str:
        out = [prompt]
        for i, opt in enumerate(self.options):
            arrow = "❯" if i == self.cursor else " "
            if multi:
                box = "[x]" if i in self.selected else "[ ]"
                out.append(f"{arrow} {box} {opt}")
            else:
                out.append(f"{arrow} {opt}")
        out.append("↑↓ move  •  " + ("space select  •  " if multi else "") + "enter confirm")
        return "\n".join(out)


def _can_ansi() -> bool:
    try:
        import termios  # noqa: F401
    except ImportError:
        return False
    return sys.stdin.isatty() and sys.stdout.isatty()


def _read_key() -> str:
    import tty
    import termios

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch == "\x1b":
            ch += sys.stdin.read(2)
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _run(menu: Menu, prompt: str, multi: bool) -> int | set[int] | None:
    """Raw-mode loop. Returns cursor / selected set, None on abort."""
    while True:
        sys.stdout.write("\x1b[2J\x1b[H" + menu.render(prompt, multi))
        sys.stdout.flush()
        try:
            key = _read_key()
        except (EOFError, KeyboardInterrupt, OSError):
            return None
        if key in ("\r", "\n"):
            return set(menu.selected) if multi else menu.pick()
        if key in _UP:
            menu.move(-1)
        elif key in _DOWN:
            menu.move(1)
        elif multi and key == " ":
            menu.toggle()
        elif key == "\x03":
            return None


def _fallback(prompt: str, options: list[str], multi: bool,
              default):
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


def choose(prompt: str, options: list[str], default=None):
    """Single pick. Returns the option value or `default`."""
    if not options:
        return default
    if not _can_ansi():
        if not sys.stdin.isatty():
            return default
        return _fallback(prompt, options, False, options[0] if default is None else default)
    menu = Menu(options)
    try:
        got = _run(menu, prompt, False)
    except (KeyboardInterrupt, OSError):
        got = None
    print()
    if got is None:
        return default
    return options[got]


def multi(prompt: str, options: list[str]) -> list[str]:
    """Multi pick. Returns picked values, possibly empty."""
    if not options:
        return []
    if not _can_ansi():
        if not sys.stdin.isatty():
            return []
        return _fallback(prompt, options, True, [])
    menu = Menu(options)
    try:
        got = _run(menu, prompt, True)
    except (KeyboardInterrupt, OSError):
        got = None
    print()
    if got is None:
        return []
    return [options[i] for i in sorted(got)]
