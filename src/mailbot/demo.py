"""Zero-credential demo: the whole pitch in ~10 seconds, no Gmail needed.

`mail-agent demo` prints a fake inbox, shows what Mailbot would do with
each message and WHY, then points at real setup. This is the file to send
to skeptics — and the reason the README can say "try it before you
trust it with your inbox".
"""
from __future__ import annotations

INBOX = [
    {
        "from": "priya@studio.com",
        "subject": "Re: shoot on the 12th?",
        "snippet": "12th works for us — confirm?",
        "verdict": "SEND",
        "why": "existing thread, routine confirmation, reversible",
        "draft": "hey priya good to hear from u! the 12th works, i'm in",
    },
    {
        "from": "venue@grandhall.com",
        "subject": "Deposit of 5000 due Thursday",
        "snippet": "Please confirm the 5000 deposit by Thursday.",
        "verdict": "ASK",
        "why": "money — needs the human, always",
        "draft": "(no draft sent — queued for approval)",
    },
    {
        "from": "newsletter@deepseek.com",
        "subject": "V4 launch postponed",
        "snippet": "We postponed the V4 launch to next quarter…",
        "verdict": "FILE",
        "why": "newsletter, no action, archived + labeled",
        "draft": "—",
    },
    {
        "from": "clinic@citycare.com",
        "subject": "Re: your appointment",
        "snippet": "Shall we move it to Friday?",
        "verdict": "ASK",
        "why": "health — 'no problem, whatever works' to a doctor is not routine",
        "draft": "(no draft sent — queued for approval)",
    },
    {
        "from": "security@bank.com",
        "subject": "Your OTP is 441902",
        "snippet": "Use this code to verify…",
        "verdict": "ASK",
        "why": "credentials — never auto-act, never forward",
        "draft": "—",
    },
]


def run_demo() -> int:
    print("\n  Mailbot demo — 5 messages, 0 credentials, ~10 seconds\n")
    sent = filed = asked = 0
    for m in INBOX:
        print(f"  From: {m['from']}\n  Subj: {m['subject']}\n  Snip: {m['snippet']}")
        print(f"  => {m['verdict']:<4}  ({m['why']})")
        if m["verdict"] == "SEND":
            sent += 1
            print(f'     draft: "{m["draft"]}"')
        elif m["verdict"] == "FILE":
            filed += 1
        else:
            asked += 1
        print()
    print(f"  Result: {sent} sent, {filed} filed, {asked} waiting on you.")
    print("  Rule: routine → acts + reports. Money/health/credentials → asks.")
    print("  Authority lives in code, not in a prompt — a clever model can't talk past it.")
    print("\n  Your inbox next: ./setup.sh --fast  (see SETUP.md)\n")
    return 0
