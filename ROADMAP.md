# Mailbot architecture roadmap — escaping the drafting matrix

Every mail agent drafts. Almost none ACT. That is the whole gap and the
whole brand: Mailbot sends, files, schedules, invites, and reports — under
code-enforced authority. This doc is the plan to widen that gap until it is
unbridgeable. Ordered by leverage, not by ease.

Status terms: DONE (shipped), NOW (next), NEXT (after), LATER (big bets).

## Pillar 0 — PROVE IT (nobody believes an agent that acts)

Nobody trusts "it sends mail for me" without receipts. Proof is the feature.

- [ ] NOW — `mail-agent why <run|send|approval-id>`: replay the decision chain
  (signals, verdict, reason) from `actions_log` + `runs`. Every autonomous
  act must be explainable in one command. Touches: `cli.py`, `storage/db.py`.
- [ ] NOW — weekly honesty report: sends/queued/refused counts + spend,
  delivered as the Monday brief. Turns the existing `runs` table into trust.
- [ ] NEXT — adversarial self-tests in CI: a fixture inbox of 30 hostile
  mails (phish, fake boss, OTP bait, calendar spam) that must all resolve to
  refuse/queue. We already have 24 such tests; grow the fixture, never shrink it.

## Pillar 1 — TWO BRAINS (cheap eyes, expensive hands)

Today the flagship model reads every newsletter. That is slow and expensive.

- [ ] NOW — wire `ROUTER_TRIAGE_MODEL`: a small fast model classifies each
  message (routine / needs-human / ignore) and drafts nothing. The flagship
  only touches items the small brain escalated or must reply to. The config
  field exists (`config.py:103`) and is currently dead — half a day's work.
  Expected: ~70% fewer flagship calls, same outcomes.
- [ ] NEXT — local-first triage: Ollama/small model on the box does pass 1,
  cloud only drafts. Mail content stays home for everything routine. This is
  the privacy story no hosted agent can copy.
- [ ] LATER — dual-LLM isolation: a quarantined reader sees raw mail and
  outputs STRUCTURED FACTS only (sender, ask, date, money?); the privileged
  actor never sees raw text, only facts + tool results. Prompt injection
  becomes structurally impossible instead of filter-probable. This is the
  endgame defense; the fencing + filters we ship today are the bridge.

## Pillar 2 — MEMORY THAT COMPOUNDS (a colleague, not a script)

Today: a static voice profile + contact counters. A real colleague remembers.

- [ ] NOW — episodic memory: what the agent did and what you corrected,
  queryable (`mail-agent recall "venue"`). The `chatlog` + `actions_log`
  tables already hold this; add retrieval.
- [ ] NOW — learn from approvals: approving a queued send to X twice should
  propose X for auto-send instead of queuing forever. Denials should stick
  harder than the keyword list.
- [ ] NEXT — semantic facts: "my lawyer is Anita", "never book mornings",
  "T drives the venue deal". Small extracted-fact store, surfaced in triage
  context. This is what makes year-two Mailbot feel telepathic.
- [ ] NEXT — per-person voice: separate micro-profiles per frequent contact
  (already mined per-thread in `style.py` — split the output, don't average it).

## Pillar 3 — REAL PUSH + REAL COVERAGE

- [ ] NEXT — Gmail Pub/Sub + History API as the primary watcher. IMAP IDLE
  needs an app password and breaks on Workspace lockdowns; Pub/Sub works on
  the same OAuth token and gives deltas, not re-scans. Keep IDLE as fallback.
- [ ] NEXT — onboarding interview: 5 questions on first run (who matters,
  what is money here, quiet hours) to bootstrap allowlist + facts without
  needing 300 sent mails. Kills the cold-start problem for new users.
- [ ] LATER — thread catch-up ("brief me on the venue thread"), vacation
  mode (hold + digest, auto-decline with sense), attachment intelligence
  (read the PDF before replying "looks good").

## Pillar 4 — DISTRIBUTION (be where the users already are)

- [ ] NEXT — MCP server mode: `mail-agent mcp` exposes search/read/draft/
  calendar as MCP tools. Every Claude Desktop + agent user becomes a
  potential Mailbot user with zero new UI. Cheapest growth in this list.
- [ ] LATER — read-only web view: one local page showing queue, spend,
  decision log. No cloud, no account, just the truth on localhost.
- [ ] LATER — multi-box sync: encrypted state sync so laptop + server share
  one brain. SQLite + litestream pattern, not a rewrite.

## What we will NOT build

- Our own model. The router abstraction is the moat — ride every lab's curve.
- A cloud dashboard with accounts. The day mail touches our server, the
  privacy story dies.
- Plugin marketplaces, themes, 40 integrations. Gmail + Calendar done
  perfectly beats everything done thinly.

## Scoreboard (update as we ship)

| Metric | Today | Target |
|---|---|---|
| Flagship calls per 30-message scan | ~30 | <10 |
| Hostile fixture mails auto-sent | 0 | 0, forever |
| Median mail-to-action latency | <30s (IDLE) | <10s (Pub/Sub) |
| Setup time, laptop | ~10 min | <3 min |
| Setup time, agent-driven headless | ~15 min | <5 min |
