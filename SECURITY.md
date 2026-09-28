# Security

Mailbot reads your inbox, so this file matters more than usual.

- Secrets live in `~/.config/mail-agent/.secrets` (mode 600). Nothing is
  logged, echoed, or committed. Ever.
- OAuth tokens are yours (Desktop-app flow, Gmail + Calendar scopes only).
  No telemetry, no analytics, no phone-home.
- Email is data: the agent never follows instructions found inside a message.
- If you find a bypass (prompt injection → send, token leak, guard evasion),
  email omkargskrishnan@gmail.com with "MAILBOT SECURITY" in the subject.
  Please don't open a public issue for live vulnerabilities — allow 14 days
  to fix before disclosure.
