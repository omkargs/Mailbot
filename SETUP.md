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
   # Optional two-brain triage (cheap model sorts, flagship only thinks):
   # export ROUTER_TRIAGE_MODEL="<cheap-model-id>"
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

6. Leave a review + send a PR from my git account (omkargs):
   - Review: run ~/Mailbot/.venv/bin/python -m pytest -q and
     ~/Mailbot/.venv/bin/mail-agent doctor. Write up what passed,
     what failed, and what you changed to fix it. Put that review
     in the PR body.
   - PR: git -C ~/Mailbot checkout -b agent/<short-topic>
     Commit only the files you touched, with a plain one-line message.
     git -C ~/Mailbot push -u origin agent/<short-topic>
     gh pr create --repo omkargs/Mailbot --title "<one line>" --body "<review>"
   - Rules: one topic per PR. Never commit secrets, tokens, or
     ~/.config/mail-agent/. Never force-push main. Small diffs only.

Report back ONLY: `doctor` output, `status` output, first 5 daemon-log
lines, and the PR URL. Never print secret values.
```

Env vars the wizard imports (`--import-env`): `ROUTER_BASE_URL`,
`ROUTER_API_KEY`, `ROUTER_MODEL`, `ROUTER_TRIAGE_MODEL` (optional — enables
the cheap triage pass; blank means off), `GOOGLE_CREDENTIALS` (path or raw JSON),
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

   On a box with no browser at all, get just the URL and nothing else:

   ```bash
   mail-agent setup --step google --print-auth-url
   ```

   Open it anywhere, approve, then paste what you get back — the bare code
   or the whole redirected URL both work. Auth codes are **single-use**; if
   one is rejected, approve again for a fresh one rather than pasting the
   same code twice.

   **If you get `access_denied` after doing every step above**, the consent
   screen is still in *Testing*, which only admits the accounts you added
   as test users. That is the one gate Google's own walkthrough buries:

   <https://console.cloud.google.com/apis/credentials/consent> →
   Publishing status → **In production** (no review needed while Testing).

   Google requires a human to click Approve. No tool can automate that
   without storing your credentials, so this one step is yours.

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

## Every environment variable

Anything set in your shell is read at runtime and wins over the secrets file
(`~/.config/mail-agent/.secrets`). `mail-agent setup --import-env` copies
these into that file so they survive a reboot.

### AI provider

| Key | Default | What it does |
|---|---|---|
| `ROUTER_BASE_URL` | `https://router.bynara.id` | Anthropic-compatible endpoint. |
| `ROUTER_API_KEY` | — | Required. The provider key. |
| `ROUTER_MODEL` | `combo/claude2mail` | Flagship model for drafting. |
| `ROUTER_TRIAGE_MODEL` | *(off)* | Cheap model for the triage pass. Empty = flagship does triage, costing more. |
| `ROUTER_MAX_TOKENS` | `16000` | Max tokens per reply. |

### Google

| Key | Default | What it does |
|---|---|---|
| `GOOGLE_CREDENTIALS` | `~/.config/mail-agent/google-credentials.json` | OAuth Desktop client. |
| `GOOGLE_TOKEN` | `~/.config/mail-agent/google-token.json` | Refreshed token (mode 600). |
| `GOOGLE_ACCOUNT` | — | The Gmail address. |
| `GOOGLE_DISPLAY_NAME` | — | Name used in the `From:` header. |
| `GOOGLE_CALENDAR_ENABLED` | `true` | Calendar read/write. |
| `GOOGLE_AUTO_SEND` | `false` | Account-level unattended sends. |
| `GOOGLE_IMAP_HOST` / `_PORT` | `imap.gmail.com` / `993` | IMAP IDLE push listener. |
| `GOOGLE_IMAP_PASSWORD` | — | App password. Without it, falls back to interval polling. |

### Microsoft 365

| Key | What it does |
|---|---|
| `MS_TENANT_ID` / `MS_CLIENT_ID` / `MS_CLIENT_SECRET` | App registration. |
| `MS_ACCOUNT` / `MS_DISPLAY_NAME` | Identity. |
| `MS_CALENDAR_ENABLED` / `MS_AUTO_SEND` | Same meaning as Google. |

### Chat channels

| Key | What it does |
|---|---|
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | Telegram. The only fully interactive channel (chat **and** approvals). |
| `DISCORD_BOT_TOKEN` / `DISCORD_USER_ID` | Discord. Notify-only — Discord cannot poll button clicks, so approve on Telegram. |
| `SLACK_BOT_TOKEN` / `SLACK_CHANNEL` | Slack. Notify-only. |

### Agent behaviour

| Key | Default | What it does |
|---|---|---|
| `AGENT_SEND_MODE` | `auto` | `auto` (unattended for approved contacts) or `never` (drafts only). `never` is the kill switch. |
| `AGENT_SCAN_INTERVAL` | `300` | Seconds between scans when not using IMAP IDLE. |
| `AGENT_DAILY_TOKEN_CAP` | `500000` | Hard daily spend ceiling. |
| `AGENT_MAX_CALLS_PER_MIN` | `20` | Runaway-loop brake. |
| `AGENT_BREAKER_THRESHOLD` | `5` | Failures before the circuit opens. |
| `AGENT_MAX_DRAFTS` | `40` | Cap per run. |
| `AGENT_BRIEF_HOUR` | `7` | Morning brief, local time. |
| `AGENT_FETCH_LIMIT` | `30` | Messages pulled in per run. Lower it on a slow or local provider — a big batch is what makes a run time out. |
| `AGENT_BODY_PREFETCH` | `12` | Full bodies inlined into the prompt. These are what make a run long. |
| `AGENT_FIRST_RUN_DAYS` | `7` | On the **first** run only, ignore mail older than this. With no cursor "everything new" means your entire mailbox, and an agent's first action should be near-invisible. Set `0` to sweep everything. |
| `AGENT_WARN_SPEND_PCT` | `80` | Warn at this share of the cap. |
| `MAIL_AGENT_BRAIN` | `./brain` | Where voice profiles live. |

### Escalation keywords

`escalation_keywords` in `~/.config/mail-agent/config.json` decides what always
stops for your review. Matched three ways — raw text, de-obfuscated (leet,
zero-width), and spaceless — so `p a s s w o r d` and `p.a.s.s.w.o.r.d` both
trip it. Adding a word makes the agent more cautious; removing one makes it
more autonomous.

## Look before it acts

```bash
mail-agent scan --dry-run     # the batch, and every gate that would fire
mail-agent explain <id>       # why this one message would be held
```

`--dry-run` sends nothing, files nothing, drafts nothing, marks nothing
read, moves no cursor and records no run. It cannot tell you what the
model *will* decide — that is the model's call — but it shows which mail
it would see and which of your own safety gates fire on each. Given that
the first run used to sweep the whole mailbox, look before you point it
at a real inbox.

## Uninstalling

```bash
./uninstall.sh
```

Shows where code, config, and data live, then offers: **1)** keep data (removes
code only — reinstall later with settings intact), **2)** full wipe (requires
typing `DELETE`), **3)** cancel. It stops the daemon first, and refuses to run
without a TTY so it can never delete anything unattended.
