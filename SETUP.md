# Mailbot setup guide (humans + coding agents)

One wizard, three ways to run it. Start here, not in the README.

## 60-second quickstart (normal person, normal laptop)

```bash
git clone https://github.com/omkargs/Mailbot.git
cd Mailbot
./setup.sh --fast
```

That does provider → Google → verify with defaults, skipping the slow
voice-profile build and the optional chat prompts. Then, only if needed:

```bash
.venv/bin/mail-agent setup --step voice   # learn your writing (~1 min)
.venv/bin/mail-agent setup --step chat    # link Telegram (optional)
./start.sh bg                             # background daemon
.venv/bin/mail-agent doctor               # green = done
```

Re-run any step any time — setup is idempotent and resumes where it stopped
(state in `~/.config/mail-agent/.setup-state.json`).

## Flags

| Flag | Effect |
|---|---|
| `--fast` | Defaults for everything, skip voice/service/chat prompts. Fastest human path. |
| `--yes` | Accept defaults, skip optional prompts (keeps voice + service steps). |
| `--non-interactive` | Never prompt; read from env. The coding-agent path. |
| `--import-env` | Copy known env vars into `~/.config/mail-agent/.secrets` (mode 600). |
| `--dry-run` | Print the plan (`[1/3] provider…`), change nothing. |
| `--step X` | Run one step: `provider`, `google`, `chat`, `voice`, `start`, `verify`. |
| `--skip-voice` / `--skip-service` | Skip that step this run. |

## Coding-agent path (headless box, e.g. Daytona/SSH)

Give the agent this block with real values. Secrets must never be echoed.

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
   # Optional: export TELEGRAM_BOT_TOKEN="<t>" TELEGRAM_CHAT_ID="<id>"

3. Headless wizard (no prompts, reads env):
   ~/Mailbot/.venv/bin/mail-agent setup --non-interactive --import-env --skip-voice --skip-service
   ~/Mailbot/.venv/bin/mail-agent doctor

4. Gmail + Calendar: I will place my Desktop-app OAuth JSON at
   ~/.config/mail-agent/google-credentials.json (mode 600).
   Then run interactively ONCE (prints a URL I open on my phone):
   ~/Mailbot/.venv/bin/mail-agent setup --step google
   Verify: ~/Mailbot/.venv/bin/mail-agent status
           ~/Mailbot/.venv/bin/mail-agent cal --limit 3

5. Finish: ~/Mailbot/.venv/bin/mail-agent setup --step voice
   cd ~/Mailbot && ./start.sh bg && ./start.sh status

Report back ONLY: `doctor` output, `status` output, first 5 daemon-log
lines. Never print secret values.
```

Env vars the wizard imports (`--import-env`): `ROUTER_BASE_URL`,
`ROUTER_API_KEY`, `ROUTER_MODEL`, `GOOGLE_CREDENTIALS` (path or raw JSON),
`GOOGLE_ACCOUNT`, `GOOGLE_DISPLAY_NAME`, `GOOGLE_CALENDAR_ENABLED`,
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `DISCORD_BOT_TOKEN`,
`DISCORD_USER_ID`, `AGENT_SEND_MODE`, `AGENT_SCAN_INTERVAL`,
`AGENT_DAILY_TOKEN_CAP`, `AGENT_BRIEF_HOUR`.

## Google OAuth (Gmail + Calendar, one consent)

Scopes requested (`src/mailbot/providers/gmail.py`): `gmail.modify`,
`gmail.compose`, `gmail.labels`, `gmail.settings.basic`, `calendar`.

1. https://console.cloud.google.com/apis/credentials — create a project.
2. Enable https://console.cloud.google.com/apis/library/gmail.googleapis.com
   and https://console.cloud.google.com/apis/library/calendar-json.googleapis.com
3. OAuth consent screen → External → app name/email → add the scopes above →
   add your Gmail as test user.
4. Credentials → Create → OAuth client ID → **Desktop app** → download JSON →
   save as `~/.config/mail-agent/google-credentials.json` (mode 600).
5. `mail-agent setup --step google` — open the printed URL on any device.
   Over plain SSH with no browser, either forward first
   (`ssh -L 8080:localhost:8080 user@host`) or auth on your laptop and copy
   `~/.config/mail-agent/google-token.json` to the server.

## Files

- Secrets: `~/.config/mail-agent/.secrets` (mode 600, never logged/committed).
- Google OAuth client: `~/.config/mail-agent/google-credentials.json`.
- Google token: `~/.config/mail-agent/google-token.json`.
- State: `~/.config/mail-agent/.setup-state.json` (resume info, no secrets).
- Overridable via `MAIL_AGENT_CONFIG_DIR`, `MAIL_AGENT_DATA_DIR`, `MAIL_AGENT_DB`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `ROUTER_API_KEY not set` | `export ROUTER_API_KEY=…` + `mail-agent setup --import-env`, or `./set-secret.sh ROUTER_API_KEY` on a TTY |
| `router FAILED: 401` | Wrong key. Top up / re-issue, re-run `--step provider`. |
| `402 / out of credit` | Provider balance empty. Top up, re-run. Wizard says so instead of hanging. |
| `google credentials not found` | Place the Desktop JSON (step 4 above), re-run `--step google`. |
| `google token missing` (non-interactive) | Run `--step google` once on a TTY, or copy a valid `google-token.json` up. |
| `calendar not reachable` | Enable Calendar API in Cloud Console, re-consent (remove old token, re-run). |
| Daytona/container: systemd missing | Expected. Use `./start.sh bg` / `./start.sh status`, not `systemctl --user`. |
| Voice profile slow | Normal (~1 min, reads sent mail). Skip with `--fast`, run `--step voice` later. |

`SETUP_PROMPT.md` holds the raw copy-paste agent block above for convenience.
