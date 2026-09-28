# Launch kit — grab attention without being spammy

Ship in this order. One per week max. Each links the 10-second demo first,
setup second.

## 1. Show HN (the big one)

Title: `Show HN: Mailbot – an inbox agent that reports what it refused to do`

Body:
```text
I built the email agent I actually trust with my inbox.

Most AI inbox tools either auto-send everything or do nothing and call it
caution. Mailbot decides per message — routine confirmations get sent (and
reported with the reason), money/health/credentials always wait on me.
The authority rules live in code, not in a prompt, so a clever model or a
sneaky email can't talk past them.

Other bits: learns my writing voice from sent mail (formal to clients,
"hey brhh" to friends), push detection via IMAP IDLE (<30s), spend firewall
on by default, runs on my own box over SSH, MIT licensed.

Try the whole idea in 10 seconds with zero credentials:
  pip install -e . && mail-agent demo
Then ./setup.sh --fast for the real inbox.

Happy to answer anything — especially "but would you let it email your boss?"
```

## 2. Reddit r/selfhosted

Title: `Self-hosted inbox agent (Gmail+Calendar, Telegram, MIT) – tries the demo with no creds`

Post: same skeleton, lead with self-hosted angle + `mail-agent demo` command.
Reply to every comment in the first 6 hours — that thread IS the launch.

## 3. X / Twitter thread (5 posts)

1. "I let an AI read my inbox for 30 days. It sent 40 emails and I regret 0. Here's the design that made it safe 🧵"
2. The rule: routine → acts + reports with reason. Money/health/creds → asks. Judged by consequence, not topic.
3. "Confirming Friday" to a colleague = routine. "Sounds good" to a doctor = not. Same words, different stakes.
4. Authority in code, not prompts. Spend firewall on. Voice mined from sent mail.
5. MIT, self-hosted, 10-sec demo with no Gmail: `mail-agent demo`. Link + star ask.

## 4. Checklist before posting

- [ ] `mail-agent demo` runs on a fresh clone with no creds
- [ ] README hero shows the demo command above the fold
- [ ] `mail-agent doctor` output pasted in your own first comment (proof of honesty)
- [ ] Post Tuesday–Thursday, morning US time
- [ ] Answer "what stops it going rogue?" with the guards + circuit-breaker link
