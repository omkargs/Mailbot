# Setup prompt for a coding agent

Give this whole block to any coding agent with shell access to the target
machine. It performs a headless, non-interactive Mailbot install.

```text
Set up Mailbot from https://github.com/omkargs/Mailbot.git in ~/Mailbot:

1. Clone (or pull) and install:
   git clone https://github.com/omkargs/Mailbot.git ~/Mailbot 2>/dev/null || git -C ~/Mailbot pull
   python3 -m venv ~/Mailbot/.venv
   ~/Mailbot/.venv/bin/pip install -q -e ~/Mailbot

2. Write the secrets I give you as env vars (never echo values):
   export ROUTER_API_KEY="<bynara/router key>"
   export ROUTER_BASE_URL="https://router.bynara.id"
   export ROUTER_MODEL="combo/claude2mail"
   # Optional chat channel:
   # export TELEGRAM_BOT_TOKEN="<token>" TELEGRAM_CHAT_ID="<id>"

3. Run the headless wizard (no prompts, reads env):
   ~/Mailbot/.venv/bin/mail-agent setup --non-interactive --import-env --skip-voice --skip-service
   ~/Mailbot/.venv/bin/mail-agent doctor

4. For Gmail + Calendar: I will place my Desktop-app OAuth JSON at
   ~/.config/mail-agent/google-credentials.json (mode 600).
   Then run interactively ONCE (TTY required, prints a URL I open on my phone):
   ~/Mailbot/.venv/bin/mail-agent setup --step google
   Verify with:
   ~/Mailbot/.venv/bin/mail-agent status
   ~/Mailbot/.venv/bin/mail-agent cal --limit 3

5. Finish with:
   ~/Mailbot/.venv/bin/mail-agent setup --step voice
   cd ~/Mailbot && ./start.sh bg && ./start.sh status

Report back ONLY: `mail-agent doctor` output, `mail-agent status` output,
and the first 5 lines of the daemon log. Never print secret values.
```

## Why this shape

- `setup --non-interactive --import-env` is the agent path: env in, secrets
  file out (mode 600), honest non-zero exit when something is missing.
- `setup --step google` is the only interactive step, because Google forbids
  pure headless OAuth. It prints one URL that works from any device, then
  stores `google-token.json`.
- `doctor` is the verification gate. Green doctor + `status ok` means done.
