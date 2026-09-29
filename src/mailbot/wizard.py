"""Unified setup wizard: one command, headless-first, agent-friendly.

Replaces the old two-layer flow (setup.sh bash + setup.py interactive) with:

    mail-agent setup [--fast] [--yes] [--non-interactive] [--step PROVIDER|GOOGLE|CHAT|VERIFY]
                     [--import-env] [--dry-run] [--skip-voice] [--skip-service]

Fast paths:
- Human, normal machine:      ./setup.sh  (or: mail-agent setup --fast)
- Coding agent, headless box:  mail-agent setup --non-interactive --import-env
- Diagnose anything:           mail-agent doctor

Design rules:
1. Three questions max in interactive mode. Everything else has a sane default.
2. Headless-first. Never requires a local browser. Prints an OAuth URL the
   user can open on their phone/laptop, then accepts a pasted code.
3. Non-interactive from env, so a coding agent can run it with one prompt
   (see SETUP.md).
4. Idempotent + resumable. Running twice never corrupts a working install.
5. Never echoes a secret. Never claims success for a failed step.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from .setup import config_dir, read_secrets, write_secret

log = logging.getLogger(__name__)

STATE_FILE = "setup-state.json"

# Env vars the wizard will import with --import-env / --non-interactive.
# Deliberately the same names config._secrets() already honours, so env
# always wins at runtime even before it is written to .secrets.
IMPORT_KEYS = (
    "ROUTER_BASE_URL",
    "ROUTER_API_KEY",
    "ROUTER_MODEL",
    "ROUTER_TRIAGE_MODEL",
    "GOOGLE_CREDENTIALS",
    "GOOGLE_ACCOUNT",
    "GOOGLE_DISPLAY_NAME",
    "GOOGLE_CALENDAR_ENABLED",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "DISCORD_BOT_TOKEN",
    "DISCORD_USER_ID",
    "AGENT_SEND_MODE",
    "AGENT_SCAN_INTERVAL",
    "AGENT_DAILY_TOKEN_CAP",
    "AGENT_BRIEF_HOUR",
)

PROVIDER_CHOICES = [
    ("Bynara router — combo models, cheapest (recommended)",
     "https://router.bynara.id"),
    ("Anthropic direct — api.anthropic.com",
     "https://api.anthropic.com"),
    ("OpenRouter — hundreds of models, one key",
     "https://openrouter.ai/api/anthropic"),
    ("Local via LiteLLM gateway — your own box (needs LiteLLM on :4000)",
     "http://localhost:4000"),
    ("Custom Anthropic-compatible URL…", ""),
]


def provider_base_for(choice: str) -> str | None:
    """Menu label -> base URL. None means 'ask for a custom URL'.

    Only Anthropic-compatible endpoints work: Mailbot speaks the Anthropic
    protocol (tool use, prompt caching). OpenAI-only endpoints (plain
    Ollama, vanilla vLLM) do not — offering them would be a lie that fails
    at 3am. OpenRouter qualifies via its /api/anthropic endpoint.
    """
    for label, base in PROVIDER_CHOICES:
        if choice == label:
            return base or None
    return DEFAULTS["ROUTER_BASE_URL"]


DEFAULTS = {
    "ROUTER_BASE_URL": "https://router.bynara.id",
    "ROUTER_MODEL": "combo/claude2mail",
    "AGENT_SEND_MODE": "auto",
    "AGENT_SCAN_INTERVAL": "300",
    "AGENT_DAILY_TOKEN_CAP": "2000000",
    "AGENT_BRIEF_HOUR": "7",
    "GOOGLE_CALENDAR_ENABLED": "true",
}


def _tty() -> bool:
    return sys.stdin.isatty()


def _state_path() -> Path:
    return config_dir() / ".setup-state.json"


def _load_state() -> dict[str, Any]:
    try:
        return json.loads(_state_path().read_text())
    except Exception:
        return {}


def _save_state(state: dict[str, Any]) -> None:
    try:
        p = _state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state, indent=2))
    except Exception:
        pass


def import_env() -> list[str]:
    """Copy known env vars into .secrets. Returns keys written (names only)."""
    written = []
    for k in IMPORT_KEYS:
        v = os.environ.get(k, "").strip()
        if not v:
            continue
        # GOOGLE_CREDENTIALS may be a path or raw JSON; normalise to a path.
        if k == "GOOGLE_CREDENTIALS" and v.lstrip().startswith("{"):
            dest = config_dir() / "google-credentials.json"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(v)
            dest.chmod(0o600)
            write_secret("GOOGLE_CREDENTIALS", str(dest))
            written.append(k)
        else:
            write_secret(k, v)
            written.append(k)
    return written


def apply_defaults() -> None:
    s = read_secrets()
    for k, v in DEFAULTS.items():
        if not s.get(k):
            write_secret(k, v)


def ensure_config_json() -> Path:
    p = config_dir() / "config.json"
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "enabled_accounts": [],
            "auto_send_contacts": [],
            "never_auto_send": [],
            "escalation_keywords": [
                "invoice", "payment", "wire", "contract", "legal", "attorney",
                "bank", "salary", "offer", "termination", "medical", "passport",
                "ssn", "password", "otp", "verify", "account number",
                "recovery code", "2fa", "mfa", "social security",
                "one-time code", "one time code", "date of birth",
                "bank transfer", "wire transfer", "routing number", "deposit",
            ],
        }, indent=2))
        p.chmod(0o600)
    return p


def _run_loopback_once(flow, port: int = 8080, timeout: int = 60):
    """run_local_server with a guillotine. The oauthlib call blocks until
    the browser callback arrives — forever, on a box with no browser. The
    SIGALRM fires in the main thread only; elsewhere we attempt without a
    net (still better than hanging the process lifetime)."""
    import signal
    import threading

    if threading.current_thread() is not threading.main_thread():
        return flow.run_local_server(
            port=port, open_browser=False, authorization_prompt_message="",
            success_message="Approved — return to the terminal.")

    class _Timeout(Exception):
        pass

    def _alarm(signum, frame):
        raise _Timeout()

    old = signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(timeout)
    try:
        return flow.run_local_server(
            port=port, open_browser=False, authorization_prompt_message="",
            success_message="Approved — return to the terminal.")
    except _Timeout as e:
        raise TimeoutError(f"no browser callback in {timeout}s") from e
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def headless_google_auth(creds_path: Path, out_token: Path | None = None) -> bool:
    """OAuth without requiring a local browser.

    1. Builds the consent URL and prints it (open on any device).
    2. Runs a loopback server when possible; falls back to paste-the-code.
    Returns True iff a valid token was stored.
    """
    from google_auth_oauthlib.flow import InstalledAppFlow

    from .providers.gmail import SCOPES

    out = out_token or Path(os.environ.get("MAIL_AGENT_TOKEN",
                                           str(config_dir() / "google-token.json")))
    # Step 1: always show the URL first so the user can act while we listen.
    flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
    try:
        url, _ = flow.authorization_url(access_type="offline", prompt="consent")
    except Exception as e:
        print(f"  could not build consent URL: {type(e).__name__}")
        return False
    print("\n  Open this URL on ANY device (phone/laptop), approve, continue:")
    print(f"\n  {url}\n")

    # Step 2: one loopback attempt with a hard timeout. run_local_server
    # blocks FOREVER waiting for the browser callback — on an SSH box with
    # no forwarding that used to hang with zero explanation. Now: 60s,
    # then straight to paste-the-code. Works over `ssh -L 8080:localhost:8080`.
    if _tty():
        print("  Waiting up to 60s for the browser callback…")
        print("  (remote box? use: ssh -L 8080:localhost:8080 <host>)")
        print("  Ctrl-C skips straight to paste-the-code.")
    try:
        flow2 = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
        creds = _run_loopback_once(flow2, port=8080, timeout=60)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(creds.to_json())
        out.chmod(0o600)
        print("  token stored (mode 600)")
        return True
    except KeyboardInterrupt:
        print("\n  skipped the wait — paste the code instead.")
    except Exception as e:
        log.debug("loopback auth failed: %s", type(e).__name__)

    # Step 3: manual code paste (old Desktop clients still honour OOB-ish copy).
    if not _tty():
        print("  no browser flow completed and stdin is not a TTY — "
              "re-run with a terminal to paste the code, or auth locally "
              "and copy google-token.json up.")
        return False
    try:
        code = input("  Paste the code here (blank to skip): ").strip()
    except (EOFError, KeyboardInterrupt):
        return False
    if not code:
        return False
    try:
        flow.fetch_token(code=code)
    except Exception as e:
        print(f"  code rejected: {str(e)[:140]}")
        return False
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(flow.credentials.to_json())
    out.chmod(0o600)
    print("  token stored (mode 600)")
    return True


def collect_credentials_json(creds_path: Path) -> bool:
    """Paste-the-JSON flow: no file juggling, no scp.

    The user copies the OAuth client JSON in the Cloud Console (or opens
    the downloaded file and copies its text), pastes it here, ends with a
    blank line. Validated before writing: must parse and contain an
    installed/web client_id. Never echoes the content back.
    Returns True iff a valid file was stored (mode 600).
    """
    print("    Paste the OAuth client JSON below, then a blank line.")
    print("    (Copy it from the downloaded file — Ctrl-C here to skip.)")
    lines: list[str] = []
    try:
        while len(lines) < 2000:
            line = input("    │ ")
            if not line.strip():
                break
            if line.strip() == "END":
                break
            lines.append(line)
            blob = "\n".join(lines)
            if blob.count("{") <= blob.count("}") and _looks_like_client(blob):
                break
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    blob = "\n".join(lines)
    if not _looks_like_client(blob):
        print("    ! that did not parse as an OAuth client JSON "
              "(need installed/web with a client_id) — nothing written")
        return False
    try:
        creds_path.parent.mkdir(parents=True, exist_ok=True)
        creds_path.write_text(blob if blob.endswith("\n") else blob + "\n")
        creds_path.chmod(0o600)
    except OSError as e:
        print(f"    ! could not write {creds_path}: {e}")
        return False
    print(f"    saved to {creds_path} (mode 600)")
    return True


def _looks_like_client(blob: str) -> bool:
    try:
        data = json.loads(blob)
    except (json.JSONDecodeError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    for section in ("installed", "web"):
        part = data.get(section)
        if isinstance(part, dict) and part.get("client_id"):
            return True
    return False


def _key_help(base: str) -> str:
    """Where to get the key, based on the provider. A bare 'export it'
    is the most hostile sentence in onboarding — the user doesn't know
    what 'it' is or where it lives."""
    b = (base or "").lower()
    if "openrouter" in b:
        return ("Get one at https://openrouter.ai/keys "
                "(sign up → Keys → Create)")
    if "anthropic.com" in b:
        return ("Get one at https://console.anthropic.com/settings/keys")
    if "localhost" in b or "127.0.0.1" in b:
        return ("Use the key your gateway expects "
                "(LiteLLM: the master key you started it with)")
    return ("Get one from your router dashboard "
            "(Bynara: the API-keys page)")


def _run_voice(state: dict) -> None:
    if state.get("google") == "ok":
        try:
            from .setup import step_voice

            step_voice(state)
        except Exception as e:
            print(f"  ! voice profile failed: {type(e).__name__}")
            state["voice"] = "failed"
    else:
        state.setdefault("voice", "skipped-no-mailbox")


def _labelled(ids: list[str], prices: dict[str, tuple[float, float]]) -> tuple[list[str], dict[str, str]]:
    """Picker labels with prices when known. Returns (labels, label->id)."""
    from .agent.discovery import fmt_price

    labels, back = [], {}
    for m in ids:
        p = prices.get(m)
        label = f"{m}  ({fmt_price(*p)})" if p else m
        labels.append(label)
        back[label] = m
    return labels, back


def _step_discord(state: dict) -> None:
    """Discord: bot token + your user id. Notify-only."""
    import getpass

    s = read_secrets()
    if s.get("DISCORD_BOT_TOKEN") and s.get("DISCORD_USER_ID"):
        print("  discord already configured — keeping it")
        state["discord"] = "ok"
        return
    print("  Create an app at https://discord.com/developers/applications")
    print("  Bot → enable MESSAGE CONTENT INTENT, invite with 'bot' scope.")
    try:
        tok = getpass.getpass("  Bot token (hidden): ").strip()
    except (EOFError, KeyboardInterrupt):
        tok = ""
        print()
    if not tok:
        print("  ! no token — discord skipped")
        state["discord"] = "skipped"
        return
    write_secret("DISCORD_BOT_TOKEN", tok)
    try:
        uid = input("  Your Discord user ID (for DMs): ").strip()
    except (EOFError, KeyboardInterrupt):
        uid = ""
        print()
    if uid:
        write_secret("DISCORD_USER_ID", uid)
    if _test_notify("discord"):
        state["discord"] = "ok"
    else:
        state["discord"] = "partial (token saved, delivery failed)"


def _step_slack(state: dict) -> None:
    """Slack: bot token + channel id. Notify-only."""
    import getpass

    s = read_secrets()
    if s.get("SLACK_BOT_TOKEN") and s.get("SLACK_CHANNEL"):
        print("  slack already configured — keeping it")
        state["slack"] = "ok"
        return
    print("  Create an app at https://api.slack.com/apps → Bot token.")
    try:
        tok = getpass.getpass("  Bot token, xoxb-… (hidden): ").strip()
    except (EOFError, KeyboardInterrupt):
        tok = ""
        print()
    if not tok:
        print("  ! no token — slack skipped")
        state["slack"] = "skipped"
        return
    write_secret("SLACK_BOT_TOKEN", tok)
    try:
        chan = input("  Channel ID (e.g. C012345): ").strip()
    except (EOFError, KeyboardInterrupt):
        chan = ""
        print()
    if chan:
        write_secret("SLACK_CHANNEL", chan)
    if _test_notify("slack"):
        state["slack"] = "ok"
    else:
        state["slack"] = "partial (token saved, delivery failed)"


def _test_notify(which: str) -> bool:
    """One honest delivery test. Returns True iff the message landed."""
    from .config import load as _load
    from .notify.channels import build_notifiers

    try:
        n = build_notifiers(_load())
        if n.send("Mailbot connected — setup test, ignore this."):
            print(f"  {which} test message delivered")
            return True
    except Exception as e:
        print(f"  {which} test failed: {type(e).__name__}")
        return False
    print(f"  {which} test message did not deliver — check token/target")
    return False


def cmd_setup(args, cfg) -> int:
    import time as _time

    only = (getattr(args, "step", "") or "").lower()
    if only == "verify":
        only = ""  # verify = full pass ending in the honest summary
    non_interactive = bool(getattr(args, "non_interactive", False))
    fast = bool(getattr(args, "fast", False))
    dry_run = bool(getattr(args, "dry_run", False))
    yes = bool(getattr(args, "yes", False)) or non_interactive or fast
    skip_voice = bool(getattr(args, "skip_voice", False)) or fast
    skip_service = bool(getattr(args, "skip_service", False)) or fast
    state = _load_state()
    if state and not only:
        done = ", ".join(f"{k}={v}" for k, v in sorted(state.items()))
        print(f"  Resuming — already done: {done}")

    # Plan the run so output reads [1/3] [2/3] and --dry-run can print it.
    # add-inbox is standalone: a full run never invents inboxes.
    if only == "add-inbox":
        return _step_add_inbox(state)
    plan = []
    if not only or only == "provider":
        plan.append("provider")
    if not only or only == "google":
        plan.append("google")
    if (not only or only == "chat") and not yes:
        plan.append("chat")
    if (not only or only == "voice") and not skip_voice:
        plan.append("voice")
    if (not only or only == "start") and not skip_service:
        plan.append("service")
    plan.append("summary")

    if dry_run:
        print("  dry-run — would execute, in order:")
        for i, step in enumerate(plan, 1):
            print(f"    [{i}/{len(plan)}] {step}")
        print("  nothing written. Remove --dry-run to execute.")
        return 0

    t0 = _time.perf_counter()

    def _hdr(i: int, name: str):
        print(f"\n  [{i}/{len(plan)}] {name}…")

    idx = 0
    if getattr(args, "import_env", False) or non_interactive:
        written = import_env()
        if written:
            print(f"  imported from env: {', '.join(written)}")
    apply_defaults()
    ensure_config_json()

    # --- provider ---
    if not only or only == "provider":
        idx += 1
        _hdr(idx, "provider")
        from .agent import discovery as D
        from .menu import choose as _choose

        s = read_secrets()
        if not s.get("ROUTER_BASE_URL") and not non_interactive and not fast and _tty():
            prov = _choose(
                "Where should the brain run?",
                [label for label, _ in PROVIDER_CHOICES],
                default=PROVIDER_CHOICES[0][0],
            )
            picked = provider_base_for(prov or "")
            if picked:
                write_secret("ROUTER_BASE_URL", picked)
                if picked.startswith("http://localhost"):
                    print("  Local gateway: run `litellm --port 4000 --model ollama/llama3`")
                    print("  (plain Ollama alone won't work — Mailbot speaks the")
                    print("   Anthropic protocol, LiteLLM translates)")
            else:
                try:
                    custom = input("  Base URL (must be Anthropic-compatible): ").strip()
                except (EOFError, KeyboardInterrupt):
                    custom = ""
                if custom:
                    write_secret("ROUTER_BASE_URL", D.normalise_base(custom))
            s = read_secrets()
        base = (s.get("ROUTER_BASE_URL") or DEFAULTS["ROUTER_BASE_URL"]).strip()
        base = D.normalise_base(base)
        write_secret("ROUTER_BASE_URL", base)
        print(f"  provider: {base}")
        key = s.get("ROUTER_API_KEY", "")
        model = s.get("ROUTER_MODEL", "") or DEFAULTS["ROUTER_MODEL"]
        if not key:
            if non_interactive or not _tty():
                print("  ! No API key yet. This is the password your AI provider")
                print(f"    bills usage against. {_key_help(base)}")
                print("    Then: export ROUTER_API_KEY='paste-key-here'")
                print("    and re-run. (Bulk path: set it in env, then")
                print("    `mail-agent setup --import-env`.)")
            else:
                import getpass
                try:
                    key = getpass.getpass("  Router API key (hidden): ").strip()
                except (EOFError, KeyboardInterrupt):
                    key = ""
                if key:
                    write_secret("ROUTER_API_KEY", key)
        if key:
            models = D.list_models(base, key) or []
            if model not in models and models:
                ranked = D.rank(models)
                if yes:
                    model = ranked[0]
                elif _tty():
                    # Arrow-key model picker. No guessing model ids.
                    # Manual entry covers endpoints with no /models page.
                    # Prices shown when the endpoint reports them (OpenRouter).
                    MANUAL = "Type it manually…"
                    prices = D.list_pricing(base, key)
                    labels, back = _labelled(ranked[:12], prices)
                    pick = _choose("Pick the brain (best first, $in/$out per Mtok):",
                                   labels + [MANUAL], default=labels[0])
                    if pick == MANUAL:
                        try:
                            typed = input("  Model id: ").strip()
                        except (EOFError, KeyboardInterrupt):
                            typed = ""
                        if typed:
                            chk = D.probe(base, key, typed)
                            if chk["ok"]:
                                model = typed
                            else:
                                print(f"  ! that id failed ({chk['error'][:100]}); keeping {model}")
                    elif pick:
                        model = back.get(pick, pick)
                write_secret("ROUTER_MODEL", model)
            elif not models and not non_interactive and not fast and _tty():
                # Endpoint lists nothing (or unreachable for listing) — let
                # the user name their model instead of failing on a default
                # id that was never theirs.
                try:
                    typed = input(f"  This endpoint lists no models. Model id [{model}]: ").strip()
                except (EOFError, KeyboardInterrupt):
                    typed = ""
                if typed:
                    model = typed
                    write_secret("ROUTER_MODEL", model)
            res = D.probe(base, key, model)
            if res["ok"]:
                print(f"  provider OK: {model} ({res['said']!r})" if res["said"] else f"  provider OK: {model}")
                state["provider"] = "ok"
                # Cheap triage model: the two-brain pass stays off unless this
                # is set. Offer it interactively; agents pass it via env.
                existing_tri = s.get("ROUTER_TRIAGE_MODEL", "")
                if not non_interactive and not fast and _tty() and models:
                    MANUAL = "Type it manually…"
                    tri_ids = [m for m in D.rank(models) if m != model][:8]
                    prices = D.list_pricing(base, key)
                    tlabels, tback = _labelled(tri_ids, prices)
                    OFF = "Off — main model does triage"
                    opts = [OFF] + tlabels + [MANUAL]
                    ans = _choose("Cheap triage model (sorts mail, flagship only thinks):",
                                  opts, default=opts[0])
                    if ans == MANUAL:
                        try:
                            ans = input("  Triage model id: ").strip() or OFF
                        except (EOFError, KeyboardInterrupt):
                            ans = OFF
                    elif ans and ans != OFF:
                        ans = tback.get(ans, ans)
                    if ans and ans != OFF:
                        if ans in models:
                            write_secret("ROUTER_TRIAGE_MODEL", ans)
                            print(f"  triage model: {ans}")
                        else:
                            tr = D.probe(base, key, ans)
                            if tr["ok"]:
                                write_secret("ROUTER_TRIAGE_MODEL", ans)
                                print(f"  triage model: {ans}")
                            else:
                                print(f"  ! triage model rejected ({tr['error'][:100]}); leaving it off")
                elif existing_tri:
                    print(f"  triage model: {existing_tri}")
            else:
                print(f"  ! provider FAILED: {res['error'][:160]}")
                state["provider"] = "failed"
                if non_interactive:
                    _save_state(state)
                    return 1
        else:
            state["provider"] = "missing-key"

    # --- google ---
    if not only or only == "google":
        idx += 1
        _hdr(idx, "google (Gmail + Calendar)")
        from .providers import build_providers
        from .config import load as _load

        fresh = _load()
        creds = Path(fresh.google.credentials_file)
        if not creds.exists():
            print(f"  ! no client credentials at {creds}")
            print("    Mailbot needs a Google OAuth client so it can read YOUR")
            print("    mailbox with YOUR consent. 5 minutes, once:")
            print("    1. Open  https://console.cloud.google.com/apis/credentials")
            print("    2. Create a project (any name, e.g. mailbot)")
            print("    3. Enable the Gmail API + the Calendar API")
            print("       (APIs & Services → Library → search → Enable)")
            print("    4. OAuth consent screen → External → fill name + email →")
            print("       add yourself as a test user")
            print("    5. Credentials → Create → OAuth client ID → Desktop app →")
            print("       Download JSON")
            if not non_interactive and _tty():
                print("    6. Paste it below OR save the file here and re-run:")
            else:
                print("    6. Save the downloaded file here (this shell can't take a paste):")
            print(f"         {creds}")
            if not non_interactive and _tty():
                if collect_credentials_json(creds):
                    fresh = _load()
                    creds = Path(fresh.google.credentials_file)
            if not creds.exists():
                print("       Then re-run:  mail-agent setup --step google")
                state["google"] = "missing-creds"
        if creds.exists():
            p = build_providers(fresh).get("google")
            if p and p.valid():
                print(f"  google OK: already signed in as {p.address}")
                state["google"] = "ok"
            elif non_interactive:
                print("  ! google token missing — run `mail-agent setup --step google` "
                      "interactively once, or copy google-token.json up.")
                state["google"] = "missing-token"
            else:
                ok = headless_google_auth(creds)
                p2 = build_providers(_load()).get("google")
                if ok and p2 and p2.valid():
                    print(f"  google OK: signed in as {p2.address}")
                    state["google"] = "ok"
                else:
                    print("  ! google sign-in did not complete")
                    state["google"] = "failed"

    # --- chat (optional, skipped with --yes / --fast) ---
    if (not only or only == "chat") and not yes:
        idx += 1
        _hdr(idx, "chat (optional)")
        if _tty():
            from .menu import multi as _multi

            # Space to select, Enter when done. Telegram is fully
            # interactive (chat + approvals); Discord/Slack are
            # notify-only (no button polling — approve on Telegram).
            s = read_secrets()
            def _mark(label, *keys):
                return (label + " (configured)"
                        if all(s.get(k) for k in keys) else label)
            opts = [
                _mark("Telegram — chat + approvals",
                      "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"),
                _mark("Discord — notifications only",
                      "DISCORD_BOT_TOKEN", "DISCORD_USER_ID"),
                _mark("Slack — notifications only",
                      "SLACK_BOT_TOKEN", "SLACK_CHANNEL"),
                "Skip for now",
            ]
            picked = _multi("Talk to the bot where? (space = select)", opts)
            if any(p.startswith("Telegram") for p in picked):
                from .setup import step_telegram

                step_telegram(state)
            else:
                state.setdefault("telegram", "skipped")
            if any(p.startswith("Discord") for p in picked):
                _step_discord(state)
            else:
                state.setdefault("discord", "skipped")
            if any(p.startswith("Slack") for p in picked):
                _step_slack(state)
            else:
                state.setdefault("slack", "skipped")
        else:
            state.setdefault("telegram", "skipped")
            state.setdefault("discord", "skipped")
            state.setdefault("slack", "skipped")
    else:
        state.setdefault("telegram", "skipped")
        state.setdefault("discord", "skipped")
        state.setdefault("slack", "skipped")

    # --- voice (slow: reads sent mail; skipped by --fast) ---
    if (not only or only == "voice") and not skip_voice:
        idx += 1
        _hdr(idx, "voice profile (reads sent mail, ~1 min)")
        if not only and state.get("voice") == "ok":
            print("  voice: already learned — skipping (use --step voice to redo)")
        elif not yes and not non_interactive and _tty() and not only:
            try:
                ans = input("  Learn your writing now? Reads sent mail (~1 min) [Y/n]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                ans = "n"
            if ans in ("n", "no"):
                state.setdefault("voice", "skipped")
            else:
                _run_voice(state)
        else:
            _run_voice(state)

    # --- service ---
    if (not only or only == "start") and not skip_service:
        idx += 1
        _hdr(idx, "service")
        print("  service: on Daytona/container use `./start.sh bg` "
              "(systemd user units are unavailable there).")
        state.setdefault("service", "manual")

    _save_state(state)
    dt = _time.perf_counter() - t0
    if dt >= 2:
        print(f"  finished in {dt:.0f}s")

    # Honest exit code: 0 only if provider + google are OK.
    _print_availability(state, cfg)
    if state.get("provider") == "ok" and state.get("google") == "ok":
        print("\n  Mailbot is ready. Next: `mail-agent status`, `mail-agent cal`, `./start.sh bg`")
        return 0
    # One next action, not a state dump. First missing thing wins.
    if state.get("provider") != "ok":
        print("\n  Next: get the API key (see above), export it, re-run `mail-agent setup`.")
    elif state.get("google") not in ("ok",):
        print("\n  Next: finish Google sign-in above, then re-run `mail-agent setup --step google`.")
    else:
        print("\n  Next: re-run `mail-agent setup` — something above still needs you.")
    return 1


def _print_availability(state: dict, cfg) -> None:
    """What works, what's missing, where the files live. One screen."""
    from . import profiles as _profiles

    s = read_secrets()
    if state.get("provider") == "ok":
        prov = f"OK ({s.get('ROUTER_MODEL', '?')})"
    else:
        prov = "missing — need API key"
    tri = s.get("ROUTER_TRIAGE_MODEL", "")
    tri = f"two-brain ({tri})" if tri else "off (flagship does triage)"
    if state.get("google") == "ok":
        box = "OK"
    else:
        box = "missing — need OAuth"
    chats = [k for k, v in
             (("Telegram", state.get("telegram")), ("Discord", state.get("discord")),
              ("Slack", state.get("slack"))) if v == "ok"]
    chat = ", ".join(chats) if chats else "skipped (optional)"
    voice = {"ok": "learned"}.get(state.get("voice", ""), state.get("voice", "skipped"))
    cur = None
    try:
        cur = _profiles.get_current()
    except Exception:
        pass
    prof = f"{cur['name']} ({cur['account']})" if cur else "personal"
    print("\n  What works:")
    print(f"    brain ..... {prov}")
    print(f"    triage .... {tri}")
    print(f"    mailbox ... {box}")
    print(f"    chat ...... {chat}")
    print(f"    voice ..... {voice}")
    print(f"    profile ... {prof}")
    print(f"  Files: {config_dir()}/config.json, .secrets (600), google-token.json")


def _step_add_inbox(state: dict) -> int:
    """Add another Gmail inbox as its own profile: own OAuth files, own
    voice, own model override. Reuses the paste-JSON + headless auth flow."""
    from . import profiles as _profiles
    from .providers import build_providers
    from .config import load as _load

    print("\n  Add another inbox. Each inbox gets its own profile, voice,")
    print("  and credentials — family mail never trains the company voice.")
    if not _tty():
        print("  ! needs a terminal (paste + browser flow). Re-run on a TTY.")
        return 1
    try:
        name = input("  Profile name (e.g. Family, Company): ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return 1
    if not name:
        print("  ! a name is required")
        return 1
    pid = _profiles.slug(name)
    account = f"google:{pid}"
    try:
        _profiles.add_profile(name, account)
    except ValueError as e:
        print(f"  ! {e}")
        return 1
    creds = _profiles.creds_file(pid)
    print(f"\n  Step 1/2 — OAuth client JSON for {name}:")
    print("    Same 5-minute Cloud Console flow (its own OAuth client), then")
    if not collect_credentials_json(creds):
        print("  ! no credentials stored — re-run `mail-agent setup --step add-inbox`")
        return 1
    print(f"\n  Step 2/2 — sign in {name}:")
    token = _profiles.token_file(pid)
    if not headless_google_auth(creds, out_token=token):
        print("  ! sign-in did not complete")
        return 1
    p = build_providers(_load()).get(account)
    if p and p.valid():
        from .storage import db as _db

        # New inboxes start conservative: account-level auto-send off until
        # the owner approves contacts. Address lives in the accounts table,
        # never in shared secrets (GOOGLE_ACCOUNT stays the primary inbox).
        _db.upsert_account(account, p.address, name,
                           auto_send=False, calendar=True)
        print(f"  inbox OK: {name} signed in as {p.address}")
        print(f"  voice: run `mail-agent brain` to learn {name}'s writing")
        print(f"  chat: /profiles, then /change-profile {pid}")
        _save_state({**state, f"inbox-{pid}": "ok"})
        return 0
    print("  ! sign-in did not verify")
    return 1


def cmd_doctor(args, cfg) -> int:
    """Pre-flight checks with fix hints. Exit 0 iff usable."""
    import sys as _sys

    problems = 0

    def check(name: str, ok: bool, hint: str = ""):
        nonlocal problems
        print(f"  {'✔' if ok else '✘'} {name}" + ("" if ok else f" — {hint}"))
        if not ok:
            problems += 1

    check("python >= 3.11", _sys.version_info >= (3, 11),
          f"found {_sys.version.split()[0]}")
    s = read_secrets()
    check("ROUTER_API_KEY set", bool(s.get("ROUTER_API_KEY")),
          "export ROUTER_API_KEY=... ; mail-agent setup --import-env")
    creds = Path(cfg.google.credentials_file)
    check("google-credentials.json present", creds.exists(), f"missing {creds}")
    try:
        from .providers import build_providers

        p = build_providers(cfg).get("google")
        check("google token valid", bool(p and p.valid()),
              "mail-agent setup --step google")
        if p and p.valid():
            try:
                n = len(p.list_messages(folder="INBOX", limit=1))
                check("gmail readable", True)
            except Exception as e:
                check("gmail readable", False, type(e).__name__)
            if p.calendar_enabled:
                try:
                    p.list_events(limit=1)
                    check("calendar reachable", True)
                except Exception as e:
                    check("calendar reachable", False, type(e).__name__)
    except Exception as e:
        check("google provider loads", False, f"{type(e).__name__}")
    check("telegram configured",
          bool(s.get("TELEGRAM_BOT_TOKEN") and s.get("TELEGRAM_CHAT_ID")),
          "optional — mail-agent setup --step chat")
    try:
        from .storage import db

        db.migrate()
        check("database migrated", True)
    except Exception as e:
        check("database migrated", False, type(e).__name__)

    cfg_problems = cfg.validate()
    # validate() repeats router/google checks already shown; surface only extras.
    extras = [x for x in cfg_problems
              if "ROUTER_API_KEY" not in x and "google credentials" not in x]
    for x in extras:
        check(x, False, "see README / config")
        problems += 1

    print("\n  doctor: " + ("OK" if problems == 0 else f"{problems} problem(s)"))
    return 0 if problems == 0 else 1
