"""Terminal output helpers: color only when the terminal can show it.

clig.dev: disable color when stdout is not a TTY, NO_COLOR is set,
or TERM=dumb. Piped output stays grep-able; menu glyphs degrade to ASCII.
"""
from __future__ import annotations

import os
import sys


def supports_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    return sys.stdout.isatty()


def _v(code: str) -> str:
    return code if supports_color() else ""


BOLD = _v("\033[1m")
DIM = _v("\033[2m")
GRN = _v("\033[32m")
YLW = _v("\033[33m")
RED = _v("\033[31m")
CYN = _v("\033[36m")
RST = _v("\033[0m")

ARROW = "❯" if supports_color() else ">"


def suggest_command(bad: str, valid: list[str], n: int = 3) -> list[str]:
    """git-style 'did you mean' via stdlib difflib."""
    import difflib

    return difflib.get_close_matches(bad, valid, n=n, cutoff=0.6)
