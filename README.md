<div align="center">

# Mailbot

**An autonomous agent for your inbox.**
Runs on your own machine. Answers to Telegram. Sleeps when you sleep.

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-142%20passing-brightgreen.svg)]()

</div>

---

## Who it is

Mailbot is not a plugin and not a dashboard. It is a colleague who reads your
mail, works out what matters, and gets on with it.

You talk to it the way you'd talk to a person:

```
you > check my mail
it  > Two things. DeepSeek postponed their V4 launch, and a venue deposit of
     5000 needs deciding by Thursday. That's money, so I left it for you.
     The rest was newsletters, filed.

you > tell priya yes and add the 12th to my calendar
it  > Replied to priya — queued as ap_7f2c, it went out automatically since
     you're corresponding with her. Calendar event added for the 12th.
```

No slash commands required. It remembers what you were just talking about, so
*"tell her yes"* works.

**Where that honesty matters:** it tells you *which* path a message took. When
something queues instead of sending, it says so and gives you the id to
approve. It never claims it sent something it only prepared. And when the
provider is down or you've run out of credit, it says that too instead of
going quiet — a silent agent and a broken one look identical from outside.

## What it actually does

| | |
|---|---|
| **Learns your voice** | Mines your sent mail for tone, greetings, vocabulary. Writes to *you* like *you*. Adapts by relationship - "hey brhh" to a friend, formal to a client. |
| **Triages** | Labels, archives, files. Pushes on new mail via IMAP IDLE - typically **under 30 seconds**, not a poll. |
| **Replies** | Drafts and sends in your voice. Auto-sends the routine; asks about money, contracts, credentials, health. |
| **Manages the calendar** | Reads a date out of an email and adds the event. Deletes only with you. |
| **Briefs you** | Morning digest, quiet-thread warnings, and anything it decided needed you. |
| **Runs on a schedule** | "Brief me at 5", "check the inbox at 7 every morning" - in plain English. |
| **Stops when told** | Hard spend limits, a circuit breaker, and a one-line kill switch. |

## The part that matters: it knows when to bother you

Most "AI email agents" either spam you with everything, or do nothing and call
it caution. Mailbot decides, per message:

- **Routine and obvious** -> acts, then tells you what it did
- **Money, contracts, credentials, health, or anything personal** -> stops and asks
- **Judged by consequence, not topic.** "Confirming Friday" to a colleague is
  routine. "No problem, whatever works" to a doctor is not.

Every unattended send is reported to you with the reason it was allowed:

```
Sent -> priya
re: tour
"hey priya good to hear from u bro! the 12th works, i'm in"
_auto - existing thread
```

The authority rules live in **code**, not in a prompt. A clever model cannot
talk its way past them.

## Install

```bash
git clone https://github.com/omkargs/Mailbot
cd Mailbot
./setup.sh --fast
```

Full guide (human + coding-agent paths, headless OAuth, troubleshooting):
**[SETUP.md](SETUP.md)**.

That one command:

1. Asks for your AI provider - **discovers the available models from the URL**
   and lets you pick. No guessing model ids.
2. Verifies the model answers *before* saving it.
3. Walks you through Google sign-in (with a console fallback for headless boxes).
4. Optionally links Telegram so you can talk to it.
5. Installs and starts the background service.
6. Reads your sent mail to learn your voice.
7. **Tells you honestly what works and what doesn't.**

Re-runnable. It never double-asks for what you have already configured.

### Manual steps

<details>
<summary>Creating Google credentials</summary>

1. Go to [console.cloud.google.com/apis/credentials](https://console.cloud.google.com/apis/credentials)
2. Create a project, then enable the **Gmail API** and the **Calendar API**
3. Create an **OAuth client ID** -> type **Desktop app**
4. Download the JSON and save it to `~/.config/mail-agent/google-credentials.json`
5. Re-run `./setup.sh`

</details>

## Talk to it

On Telegram, in plain language:

| Say | It does |
|---|---|
| "who needs a reply" | Names them and says why. Sends nothing. |
| "tell him yes" | Sends it - context from earlier in the conversation |
| "add the 12th to my calendar" | Creates the event |
| "brief me at 5" | Schedules it |
| "check the inbox at 7 every morning" | Daily job |
| "what did I agree to?" | Searches your whole mailbox, not just unread |

Or from a terminal:

```bash
mail-agent status      # what it has done
mail-agent health      # is it alive, what is it spending
mail-agent brief       # the digest
mail-agent contacts --approve someone@company.com   # allow unattended replies
mail-agent reset --yes # wipe its memory, keep your credentials
```

## Where your data goes

**Nowhere, except to the AI provider you choose.** That's the whole list:

- Your mail is read directly from Gmail over your own OAuth token.
- Message text is sent to your configured model endpoint to be triaged. Choose
  a local model if you want that to stay on your machine too.
- Nothing is written to any third-party service. No telemetry, no analytics,
  no account, no phone-home.

Secrets live in `~/.config/mail-agent/.secrets` at mode `600`. They are never
logged, never echoed, and never committed.

## Spending money safely

An agent that calls a paid API unattended can burn through a balance in an
afternoon. Four limits, all on by default:

| Limit | Default | Why |
|---|---|---|
| `AGENT_MAX_CALLS_PER_MIN` | 20 | Stops a runaway loop before it costs anything |
| `AGENT_DAILY_TOKEN_CAP` | 500,000 | Hard ceiling, checked before every call |
| `AGENT_BREAKER_THRESHOLD` | 5 | After repeated failures, stops calling entirely |
| SDK retries | **off** | Retrying a "payment required" just spends more |

When credits run out it tells you once and stops - rather than going quiet and
leaving you wondering.

## Architecture

```
Telegram  --chat-->  +----------+
                     |  Mailbot  |--> guards --> Gmail / Calendar
Gmail IDLE --push--> |  (agent)  |
                     +----+-----+
                          |
             limits <-----+-----> any Anthropic-compatible endpoint
```

- **One door to the model.** Every call goes through `guarded_call`, so the
  spend limits are real rather than decorative.
- **Batch everything.** Listing 30 messages is 2 API calls, not 31. Fetching
  bodies is one batch, not one round trip each.
- **Prompt caching** on the frozen prefix; volatile content after the
  breakpoint, or the cache never hits.
- **Push, not poll.** IMAP IDLE wakes the supervisor the instant mail lands.

## Requirements

- Linux with Python 3.11+
- An Anthropic-compatible API endpoint (Anthropic, or any compatible router)
- A Google account with the Gmail + Calendar APIs
- Optional: systemd for always-on

## Troubleshooting

| Symptom | Cause |
|---|---|
| "provider refused the call (credits)" | Out of credit. Top up, restart. |
| "daily token cap reached" | Raise `AGENT_DAILY_TOKEN_CAP`, or wait for midnight UTC. |
| "circuit open" | Provider is failing. Check the key and the endpoint. |
| No Telegram messages | Run `./setup-telegram.sh` |
| Mail not detected | Add a Gmail app password to `GOOGLE_IMAP_PASSWORD` for push. |

## License

MIT. Use it, fork it, make it yours.
