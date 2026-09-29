"""The installer splash: it is branding, not a countdown.

Regression: one commit removed the entire splash to delete a single line of
text ("showing off for 5s — press any key to skip"). The line is gone and
so was the animation. These tests fail if the animation goes again, and
fail if the line ever comes back.
"""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SETUP = (ROOT / "setup.sh").read_text(encoding="utf-8")


def test_setup_is_valid_bash():
    r = subprocess.run(["bash", "-n", str(ROOT / "setup.sh")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_no_showing_off_countdown_line():
    """The line the user asked to remove must not come back.

    Checked against the code with comments stripped: the words survive in a
    comment explaining that the line is gone, and that is fine.
    """
    code = "\n".join(l for l in SETUP.splitlines()
                     if not l.lstrip().startswith("#"))
    assert "showing off" not in code.lower()
    assert "press any key to skip" not in code.lower()


def test_splash_still_exists_and_draws_the_mailbox():
    assert "splash()" in SETUP
    assert "MAIL" in SETUP                       # the mailbox body
    assert "_mbox_frame" in SETUP               # the animation frames
    # The Mailbot wordmark.
    assert "__  __" in SETUP


def test_splash_runs_before_anything_expensive():
    """First thing you see, not after the Python hunt."""
    at = SETUP.index("\nsplash\n")
    # Compare against the call sites, not the function definitions.
    assert at < SETUP.index("\ncheck_network\n")
    assert at < SETUP.index('PY="$(pick_python)"')


def test_splash_is_skippable_and_never_blocks_automation():
    # Any key ends it early.
    assert "read -rsn1 -t" in SETUP
    # And the non-interactive paths bail out before drawing anything.
    for flag in ("--fast", "--stage", "--non-interactive", "--dry-run",
                 "--manifest", "--import-env"):
        assert flag in SETUP
    assert "MAIL_AGENT_NO_SPLASH" in SETUP
    assert "[ -t 0 ] && [ -t 1 ]" in SETUP
    assert "CI" in SETUP


def test_splash_guards_against_narrow_terminals():
    """Art this wide wraps into soup; a narrow pane must just skip it."""
    assert "tput cols" in SETUP
    assert "tput lines" in SETUP


def test_case_patterns_are_not_split_across_lines():
    """bash reads a backslash inside a case pattern list as a literal.

    Wrapping that list over two lines is a syntax error, and it took the
    whole installer down with it once already.
    """
    body = SETUP
    for m in re.finditer(r"^\s+\*\".*\\$", body, re.M):
        pytest_fail = f"line-continued case pattern: {m.group(0).strip()}"
        raise AssertionError(pytest_fail)
