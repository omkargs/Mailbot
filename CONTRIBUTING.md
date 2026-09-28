# Contributing

Small, honest PRs beat big clever ones.

- One change per PR. Say what you tested (`pytest -q`, `mail-agent doctor`).
- Never log, echo, or commit secrets. Tests enforce mode 600 on `.secrets`.
- Authority rules stay in code (`agent/guards.py`), never in prompts.
- Failing loudly beats silent success — if a step didn't succeed, say so.
- Run `python -m pytest -q` before pushing. 149 tests must stay green.
