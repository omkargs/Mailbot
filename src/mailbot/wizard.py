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
    "JEV_ENABLED",
    "JEV_BASE_URL",
    "JEV_API_KEY",
    "JEV_MODEL",
    "GOOGLE_CREDENTIALS",
    "GOOGLE_ACCOUNT",
    "GOOGLE_DISPLAY_NAME",
    "GOOGLE_CALENDAR_ENABLED",
    "GOOGLE_IMAP_PASSWORD",
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


# Google deleted the out-of-band OAuth flow in 2022. The old code still
# asked for redirect_uri=urn:ietf:wg:oauth:2.0:oob, so the headless path was
# dead and failed late - after the user had built a project, enabled two
# APIs, downloaded a JSON and approved a consent screen. The SDK is no
# help here either: InstalledAppFlow with redirect_uris:["http://localhost"]
# emits a consent URL with no redirect_uri at all (400 invalid_request), and
# passing one explicitly raises "prepare_request_body() got multiple values".
# So we build the URL and exchange the code ourselves. Thirty lines, no
# surprise, and the same redirect_uri goes into both halves.
_GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
_GOOGLE_REDIRECT = "http://localhost"


def _google_client(creds_path: Path) -> tuple[str, str]:
    """(client_id, client_secret) from a Google OAuth client JSON."""
    blob = json.loads(Path(creds_path).read_text(encoding="utf-8"))
    inner = blob.get("installed") or blob.get("web") or blob
    return inner.get("client_id", ""), inner.get("client_secret", "")


def _consent_url(client_id: str, scopes, redirect_uri: str = _GOOGLE_REDIRECT) -> str:
    from urllib.parse import urlencode

    return _GOOGLE_AUTH + "?" + urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "access_type": "offline",       # ask for a refresh token
        "prompt": "consent",            # and re-issue one every time
        "include_granted_scopes": "true",
    })


def _exchange_code(client_id: str, client_secret: str, code: str,
                   redirect_uri: str = _GOOGLE_REDIRECT) -> dict:
    """Swap an auth code for tokens. The SAME redirect_uri must go back."""
    import httpx

    r = httpx.post(_GOOGLE_TOKEN, data={
        "code": code, "client_id": client_id, "client_secret": client_secret,
        "redirect_uri": redirect_uri, "grant_type": "authorization_code",
    }, timeout=30.0)
    if r.status_code >= 400:
        try:
            detail = r.json().get("error_description") or r.json().get("error")
        except Exception:
            detail = r.text[:120]
        raise RuntimeError(f"{detail}")
    return r.json()


def _code_from_paste(raw: str) -> str:
    """Accept a bare code OR the whole redirected URL.

    People paste what is in the address bar far more often than the code
    alone, and rejecting that wastes a single-use auth code.
    """
    raw = (raw or "").strip()
    if raw.startswith("http://") or raw.startswith("https://"):
        from urllib.parse import parse_qs, urlparse

        q = parse_qs(urlparse(raw).query)
        return (q.get("code") or [""])[0]
    return raw


def _loopback_code(port: int, timeout: int) -> str:
    """Serve one callback on 127.0.0.1 and return the code. Hard timeout."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    box: dict[str, str] = {}

    class _H(BaseHTTPRequestHandler):
        def do_GET(self):                       # noqa: N802
            code = _code_from_paste(f"http://x{self.path}")
            box["code"] = code
            err = "error" in self.path
            self.send_response(400 if err else 200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                (b"<h1>Denied</h1>" if err else
                 b"<h1>Mailbot is connected.</h1>You can close this tab.")
            )
            self.server.done = True

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", port), _H)
    srv.done = False

    def _serve():
        import time as _t
        deadline = _t.time() + timeout
        srv.timeout = 1
        while not srv.done and _t.time() < deadline:
            srv.handle_request()

    th = threading.Thread(target=_serve, daemon=True)
    th.start()
    try:
        th.join(timeout + 1)
    finally:
        srv.server_close()
    if not box.get("code"):
        raise TimeoutError(f"no browser callback in {timeout}s")
    return box["code"]


def _write_google_token(out: Path, info: dict, client_id: str,
                        client_secret: str, scopes) -> None:
    """Store a token in exactly the shape google-auth reads back."""
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": info.get("refresh_token", ""),
        "token": info.get("access_token", ""),
        "token_uri": _GOOGLE_TOKEN,
        "scopes": list(scopes),
        "universe_domain": "googleapis.com",
    }
    out.write_text(json.dumps(payload, indent=2))
    out.chmod(0o600)


def _portable(creds_path: Path, port: int = 0) -> str:
    """Just the consent URL, for scripts and headless boxes."""
    from .providers.gmail import SCOPES

    cid, _ = _google_client(creds_path)
    if not cid:
        raise RuntimeError("no client_id in that credentials file")
    return _consent_url(cid, SCOPES)


_PUBLISHING_NOTE = """\
  ┌ the one step Google's own docs bury ─────────────────────────┐
  │ If the consent screen says "Testing", only the accounts you   │
  │ added as test users can connect — and yours will 403 with     │
  │ access_denied even though you followed every step above.       │
  │                                                               │
  │ Fix:  https://console.cloud.google.com/apis/credentials/consent│
  │   → Publishing status → "In production" (no review needed     │
  │     while the app is in Testing), then re-run.                 │
  └───────────────────────────────────────────────────────────────┘"""


def _note_publishing_status() -> None:
    """Warn about the 403 that a perfect setup still produces."""
    print(_PUBLISHING_NOTE)


def headless_google_auth(creds_path: Path, out_token: Path | None = None) -> bool:
    """OAuth without requiring a local browser on this machine.

    1. Prints a consent URL that works on ANY device.
    2. Tries a loopback callback for `timeout` seconds (works over
       `ssh -L PORT:localhost:PORT`).
    3. Otherwise takes a pasted code or pasted redirect URL.

    Every step is honest about what it can and cannot do: Google requires
    a human to click Approve. There is no way around that, and any tool
    claiming otherwise is either lying or storing your credentials.
    """
    from .providers.gmail import SCOPES

    out = out_token or Path(os.environ.get("MAIL_AGENT_TOKEN",
                                           str(config_dir() / "google-token.json")))
    try:
        client_id, client_secret = _google_client(creds_path)
    except Exception as e:
        print(f"  could not read that credentials file: {type(e).__name__}")
        return False
    if not client_id:
        print("  no client_id in that file — is it a Google OAuth client JSON?")
        return False

    port = _free_port()
    url = _consent_url(client_id, SCOPES, f"http://localhost:{port}")
    print("\n  Open this URL on ANY device (phone/laptop), approve, continue:")
    print(f"\n  {url}\n")
    print(f"  It will land on http://localhost:{port} — that is expected.")
    print("  On this box, forward it with:")
    print(f"    ssh -L {port}:localhost:{port} <this-host>")

    _note_publishing_status()
    if _tty():
        print(f"\n  Waiting up to 60s for the callback…  Ctrl-C to paste a code instead.")
    try:
        code = _loopback_code(port, timeout=60)
        print("  got the callback")
    except KeyboardInterrupt:
        print("\n  stopped waiting — paste the code instead.")
        code = ""
    except Exception as e:
        log.debug("loopback auth failed: %s", type(e).__name__)
        code = ""

    if not code:
        if not _tty():
            print("  no callback and stdin is not a TTY. On a headless box run:")
            print("    mail-agent setup --step google --print-auth-url")
            print("  open the URL anywhere, then come back and paste what you got.")
            return False
        try:
            code = _code_from_paste(
                input("  Paste the code, or the whole redirected URL (blank to skip): "))
        except (EOFError, KeyboardInterrupt):
            return False
    if not code:
        return False

    try:
        info = _exchange_code(client_id, client_secret, code, f"http://localhost:{port}")
    except Exception as e:
        msg = str(e)[:160]
        if "invalid_grant" in msg or "code" in msg.lower():
            # Auth codes are single-use. Saying "rejected" makes people paste
            # the same dead code again; this cost a real code once already.
            print("  that code did not work. Google auth codes are single-use —")
            print("  approve the consent screen again for a fresh code.")
            print(f"  ({msg})")
        else:
            print(f"  code rejected: {msg}")
        return False
    if not info.get("refresh_token"):
        print("  ! Google returned no refresh token. Revoke the app at")
        print("    myaccount.google.com/permissions, approve again, and pick")
        print("    the same account. (access_type=offline needs consent.)")
    _write_google_token(out, info, client_id, client_secret, SCOPES)
    print(f"  token stored (mode 600) at {out}")
    return True


def _free_port() -> int:
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


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


def _retry_provider_key(base: str, key: str, model: str, key_help,
                        err: str = "", attempts: int = 2):
    """Offer to replace an API key the provider rejected.

    Returns ``(result, key, fixed)``. A rejected key is the single most
    fixable problem in this wizard, and the wizard is the only place a
    user can fix it: the old code printed the 401 and moved on, leaving no
    way to supply a working key short of hand-editing the secrets file.
    Blank input gives up; a second Ctrl-C stops the run cleanly.
    """
    import getpass
    from .agent import discovery as D

    res = {"ok": False, "error": err, "model": model, "said": ""}
    for _ in range(attempts):
        low = (res.get("error") or "").lower()
        rejected = any(w in low for w in (
            "401", "403", "unauthorized", "authentication", "invalid",
            "api key", "permission", "forbidden", "credit", "expired"))
        print(f"  {'That key was rejected.' if rejected else 'That did not answer.'}")
        print(f"  {key_help(base)}")
        try:
            new = getpass.getpass(
                "  Working API key (hidden, blank to skip): ").strip()
        except (KeyboardInterrupt, EOFError):
            return res, key, False
        if not new:
            break
        key = new
        write_secret("ROUTER_API_KEY", key)
        res = D.probe(base, key, model)
        if res["ok"]:
            return res, key, True
        print(f"  ! still failing: {res['error'][:160]}")
    return res, key, False


def verify_jev(base_url: str, api_key: str, model: str = "jev",
               timeout: int = 20) -> tuple[bool, str]:
    """Ask the decider one trivial question. Used by the setup step AND the
    doctor, so "jev is configured" always means "jev answered", never "keys
    exist". Returns (ok, detail). Never raises."""
    import json
    import urllib.request

    if not (base_url or "").strip() or not (api_key or "").strip():
        return False, "no endpoint or key"
    try:
        req = urllib.request.Request(
            base_url.rstrip("/") + "/v1/systemone",
            data=json.dumps({
                "model": model or "jev",
                "state": "connectivity check from the mailbot installer",
                "questions": {"ok": {
                    "type": "noul",
                    "instructions": "this is a connectivity check, not a real judgment",
                }},
            }).encode(),
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read().decode() or "{}")
        if isinstance(body, dict) and "answers" in body:
            return True, f"answered as {model or 'jev'}"
        return False, "unexpected response shape"
    except Exception as e:
        return False, type(e).__name__


def _step_service(state: dict, auto: bool = False) -> None:
    """Install the systemd unit and, when the install is actually complete,
    start it. With systemd present the unit is the runtime; without it the
    fallback is `./start.sh bg`, said once."""
    import shutil
    import subprocess
    import sys as _sys

    from pathlib import Path as _Path

    repo = _Path(__file__).resolve().parent.parent.parent
    venv_bin = _Path(_sys.prefix) / "bin"
    unit_src = repo / "systemd" / "mail-agent.service"
    if shutil.which("systemctl") is None or not unit_src.exists():
        print("  no systemd here — run `./start.sh bg` to stay up without it.")
        state.setdefault("service", "manual")
        return
    dest = _Path.home() / ".config" / "systemd" / "user" / "mail-agent.service"
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        text = unit_src.read_text()
        text = text.replace("@REPO@", str(repo)).replace("@VENV@", str(_Path(_sys.prefix)))
        if dest.exists() and dest.read_text() == text:
            pass
        else:
            dest.write_text(text)
        subprocess.run(["systemctl", "--user", "daemon-reload"],
                       check=False, capture_output=True)
        subprocess.run(["systemctl", "--user", "enable", "mail-agent.service"],
                       check=False, capture_output=True)
    except OSError as e:
        print(f"  ! could not install the unit ({e}); use `./start.sh bg`.")
        state["service"] = "failed"
        return
    ready = state.get("provider") == "ok" and state.get("google") == "ok"
    if not ready:
        print("  unit installed — not starting yet (setup is incomplete).")
        print("  Finish setup, then: `./start.sh install-start`")
        state["service"] = "installed"
        return
    go = auto
    if not auto and _tty():
        try:
            ans = input("  Start the agent now? [Y/n]: ").strip().lower()
            go = ans in ("", "y", "yes")
        except (EOFError, KeyboardInterrupt, OSError):
            go = False
    if not go:
        print("  unit installed — start with `./start.sh install-start`.")
        state["service"] = "installed"
        return
    subprocess.run(["systemctl", "--user", "reset-failed", "mail-agent.service"],
                   check=False, capture_output=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", "mail-agent.service"],
                   check=False, capture_output=True)
    import time as _time

    _time.sleep(2)
    up = subprocess.run(["systemctl", "--user", "is-active", "--quiet",
                         "mail-agent.service"], check=False).returncode == 0
    if up:
        print("  service running — it survives reboot, restarts on crash.")
        state["service"] = "ok"
    # `mail-agent` on PATH: without this the next thing the user types
    # (`mail-agent status`) answers "command not found".
    try:
        bindir = _Path.home() / ".local" / "bin"
        bindir.mkdir(parents=True, exist_ok=True)
        link = bindir / "mail-agent"
        target = venv_bin / "mail-agent"
        if target.exists():
            if link.is_symlink() or not link.exists():
                if link.is_symlink():
                    link.unlink()
                link.symlink_to(target)
                print("  `mail-agent` is now on your PATH (re-open the shell if unknown).")
    except OSError:
        pass
    else:
        print("  ! installed but did not start — logs: "
              "`journalctl --user -u mail-agent.service -n 30`")
        state["service"] = "failed"


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


# Every --step value this command understands. Kept in one place so the
# validator and the plan builder cannot drift apart.
SETUP_STEPS = {"provider", "jev", "google", "push", "chat", "voice", "start"}


def cmd_setup(args, cfg) -> int:
    import time as _time

    only = (getattr(args, "step", "") or "").lower()
    if only == "verify":
        only = ""  # verify = full pass ending in the honest summary
    elif only and only not in SETUP_STEPS:
        # A typo used to be a silent no-op: the plan came back containing
        # only "summary" and the command exited 0, so a mistyped step in an
        # agent-driven script looked like it had run. setup.sh --stage
        # already refused an unknown name; this is the same rule here.
        from .ui import suggest_command
        near = suggest_command(only, sorted(SETUP_STEPS))
        print(f"  unknown step: {only!r}")
        print(f"  valid: {', '.join(sorted(SETUP_STEPS))}")
        if near:
            print("  did you mean:")
            for n in near:
                print(f"    mail-agent setup --step {n}")
        return 2
    non_interactive = bool(getattr(args, "non_interactive", False))
    fast = bool(getattr(args, "fast", False))
    dry_run = bool(getattr(args, "dry_run", False))
    yes = bool(getattr(args, "yes", False)) or non_interactive or fast
    skip_voice = bool(getattr(args, "skip_voice", False)) or fast
    skip_service = bool(getattr(args, "skip_service", False)) or fast
    state = _load_state()
    if state and not only:
        # A failed step is not "already done". Labelling provider=failed as
        # completed made a broken install look resumable-and-fine; it only
        # recovered because every step re-verifies. Separate the three.
        done = sorted(k for k, v in state.items() if v == "ok")
        failed = sorted(k for k, v in state.items() if str(v).startswith(("failed", "missing", "partial")))
        if done:
            print(f"  Resuming — already done: {', '.join(done)}")
        if failed:
            print(f"  Still needs you: {', '.join(failed)} (will retry)")

    # Plan the run so output reads [1/3] [2/3] and --dry-run can print it.
    if getattr(args, "print_auth_url", False):
        # One string and out. Headless users otherwise run a whole wizard
        # to obtain a URL, then write a script to produce it.
        from .config import load as _load_url
        creds_file = Path(_load_url().google.credentials_file)
        if not creds_file.exists():
            print(f"  no credentials file at {creds_file}")
            print("  Finish the Google console steps first, or paste the JSON:")
            print("    mail-agent setup --step google")
            return 1
        try:
            print(_portable(creds_file))
        except Exception as e:
            print(f"  could not build the URL: {e}")
            return 1
        return 0

    plan = []
    if not only or only == "provider":
        plan.append("provider")
    if not only or only == "jev":
        plan.append("jev")
    if not only or only == "google":
        plan.append("google")
    if (not only or only == "push") and not yes:
        plan.append("push")
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
        base = (s.get("ROUTER_BASE_URL") or DEFAULTS["ROUTER_BASE_URL"]).strip()
        key = s.get("ROUTER_API_KEY", "")
        model = s.get("ROUTER_MODEL", "") or DEFAULTS["ROUTER_MODEL"]

        # Probe BEFORE offering the picker. If what is already configured
        # actually works there is no decision to make, and a menu on every
        # run is how you teach someone to press Enter without reading —
        # the last thing a tool that sends mail on your behalf should do.
        # The picker appears when there is a real choice: nothing set up,
        # or what is set up does not work. `--step provider` forces it.
        probe = D.probe(base, key, model) if key else {"ok": False}
        working = bool(probe.get("ok"))
        probed_model = model      # what we actually asked, not what it echoed
        # Tracks whether `probe` still describes (base, key, model). Any
        # change to those three makes it worthless, and re-probing on a
        # known-bad key just spends the user's time twice.
        probe_valid = bool(key)

        if working and only != "provider":
            print(f"  provider: {base} — working ({model})")
            print("  to switch: mail-agent setup --step provider")
        elif not non_interactive and not fast and _tty():
            current = base
            labels = [l for l, _ in PROVIDER_CHOICES]
            for i, (_lbl, url) in enumerate(PROVIDER_CHOICES):
                if url and url == current:
                    labels[i] = f"{_lbl}  (current)"
            labels.append("Keep current" if current else "Skip for now")
            prov = _choose("Where should the brain run?", labels,
                           default="Keep current" if current else labels[0])
            base_label = prov or "Keep current"
            if not (base_label.startswith("Keep") or base_label.startswith("Skip")):
                picked = provider_base_for(base_label)
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
            new_base = (s.get("ROUTER_BASE_URL") or base).strip()
            if D.normalise_base(new_base) != D.normalise_base(base):
                # A different endpoint means the old verdict means nothing.
                base = new_base
                key = s.get("ROUTER_API_KEY", "")
                probe, working, probe_valid = {"ok": False}, False, False
        base = D.normalise_base(base)
        write_secret("ROUTER_BASE_URL", base)
        if not working:
            print(f"  provider: {base}")
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
                except KeyboardInterrupt:
                    # Ctrl-C means "stop", not "skip this field". Swallowing it
                    # marched the user through the whole wizard with a blank
                    # key and buried them in the Google step.
                    print("\n  stopped. nothing was half-written — re-run "
                          "`mail-agent setup` when ready.")
                    _save_state(state)
                    return 130
                except EOFError:
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
                    MANUAL = "✏  Type any model id…"
                    prices = D.list_pricing(base, key)
                    show = ranked if len(ranked) <= 12 else ranked[:12]
                    labels, back = _labelled(show, prices)
                    prompt = ("Pick the brain — arrows or type to search, "
                              "enter to choose, or pick the last line to type "
                              "your own id:")
                    pick = _choose(prompt, labels + [MANUAL], default=labels[0])

                    def _try_id(typed: str) -> None:
                        """Save a model id only once the endpoint agrees."""
                        nonlocal model, probe_valid
                        if not typed:
                            return
                        chk = D.probe(base, key, typed)
                        if chk["ok"]:
                            model = typed
                            probe_valid = False
                        else:
                            print(f"  ! that id failed ({chk['error'][:100]}); keeping {model}")

                    if pick == MANUAL:
                        try:
                            typed = input("  Model id: ").strip()
                        except (EOFError, KeyboardInterrupt):
                            typed = ""
                        _try_id(typed)
                    elif pick:
                        known = back.get(pick)
                        if known:
                            model = known
                        else:
                            # Typed straight into the search box and matched
                            # nothing in the list. Still a real choice, so
                            # verify it before it becomes the default.
                            _try_id(pick)
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
                    probe_valid = False
                    write_secret("ROUTER_MODEL", model)
            # Reuse the verdict from the top of this step when the model
            # has not changed since; otherwise ask again.
            res = (probe if (probe_valid and model == probed_model)
                   else D.probe(base, key, model))
            if res["ok"]:
                if not working:
                    print(f"  provider OK: {model} ({res['said']!r})" if res["said"] else f"  provider OK: {model}")
                state["provider"] = "ok"
                # Cheap triage model: the two-brain pass stays off unless this
                # is set. Offer it interactively; agents pass it via env.
                existing_tri = s.get("ROUTER_TRIAGE_MODEL", "")
                # Once this is decided, stop asking. Same rule as the
                # provider: a prompt with no decision in it trains people
                # to stop reading prompts.
                if (not non_interactive and not fast and _tty() and models
                        and only != "provider" and not existing_tri):
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
                state["provider"] = "failed"
                # Give the user a chance to fix it right here. This is the
                # only place a rejected key can be corrected.
                if not non_interactive and _tty():
                    res, key, fixed = _retry_provider_key(
                        base, key, model, _key_help, res.get("error", ""))
                    if fixed:
                        state["provider"] = "ok"
                        working = True
                        print(f"  provider OK: {model}")
                if state["provider"] != "ok":
                    print(f"  ! provider FAILED: {res['error'][:160]}")
                    print("  To supply a working key:")
                    print("    mail-agent setup --step provider")
                    print("  or, without the wizard:")
                    print("    export ROUTER_API_KEY='...'; mail-agent setup --import-env")
                    print(f"  Keys live in {config_dir() / '.secrets'} (mode 600, never echoed).")
                if non_interactive:
                    _save_state(state)
                    return 1
        else:
            state["provider"] = "missing-key"

    # --- jev: the fast decider that sorts mail before the flagship ---
    if not only or only == "jev":
        idx += 1
        _hdr(idx, "jev — the fast decider")
        from .setup import read_secrets as _read_s, write_secret as _write_s

        s = _read_s()
        if non_interactive or fast or not _tty():
            # Agents and fast runs: import_env already copied JEV_*; the
            # endpoint falls back to the provider's, so "no answers" still
            # means "on, sharing the router" unless explicitly disabled.
            if s.get("JEV_ENABLED", "1") == "0":
                print("  jev: off (JEV_ENABLED=0)")
                state["jev"] = "off"
            else:
                _write_s("JEV_ENABLED", "1")
                state["jev"] = "unverified"
        else:
            print("  Jev sorts each mail in milliseconds for cents, so the")
            print("  flagship only reads what deserves a reply. It lives on")
            print("  the same router by default — separate keys are only for")
            print("  providers that don't serve it.")
            try:
                use = input("  Use the Jev decider? [Y/n]: ").strip().lower()
            except (EOFError, KeyboardInterrupt, OSError):
                use = "n"
            if use in ("n", "no"):
                _write_s("JEV_ENABLED", "0")
                print("  jev off — the flagship reads every message itself.")
                state["jev"] = "off"
            else:
                _write_s("JEV_ENABLED", "1")
                base_d = s.get("ROUTER_BASE_URL", "")
                try:
                    url = input("  Jev endpoint [same as provider]: ").strip()
                    key = input("  Jev key [same as provider key]: ").strip()
                    mdl = input("  Jev model [jev]: ").strip() or "jev"
                except (EOFError, KeyboardInterrupt, OSError):
                    url, key, mdl = "", "", "jev"
                if url:
                    _write_s("JEV_BASE_URL", url)
                if key:
                    _write_s("JEV_API_KEY", key)
                _write_s("JEV_MODEL", mdl)
                eff_url = url or base_d
                eff_key = key or s.get("ROUTER_API_KEY", "")
                if not eff_url or not eff_key:
                    print("  ! no endpoint/key yet — Jev stays configured but "
                          "unverified; it fails closed to the flagship until then.")
                    state["jev"] = "unverified"
                else:
                    print("  asking Jev one question to prove it answers…")
                    ok, detail = verify_jev(eff_url, eff_key, mdl)
                    if ok:
                        print(f"  jev OK: {detail}")
                        state["jev"] = "ok"
                    else:
                        print(f"  ! jev did not answer ({detail}) — keys saved, "
                              "flagship covers until it does.")
                        state["jev"] = "failed"

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
            print("       (create/sign into a Google Cloud project first if it asks)")
            print("    2. Create a project (any name, e.g. mailbot)")
            print("    3. Enable the Gmail API + the Calendar API")
            print("       (APIs & Services → Library → search each → Enable)")
            print("       Direct links if search fights you:")
            print("         https://console.cloud.google.com/apis/library/gmail.googleapis.com")
            print("         https://console.cloud.google.com/apis/library/calendar-json.googleapis.com")
            print("    4. OAuth consent screen → External → fill name + email →")
            print("       add yourself as a test user")
            print("       https://console.cloud.google.com/apis/credentials/consent")
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
                print(f"  google OK: already signed in"
                      f"{f' as {p.address}' if p.address else ' (address resolves on next run)'})")
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

    # --- push: wake on new mail instead of polling for it ---
    if (not only or only == "push") and not yes:
        idx += 1
        _hdr(idx, "push (wake on new mail)")
        from .setup import read_secrets as _read_p, write_secret as _write_p

        s = _read_p()
        # Present is not the same as usable: an empty or truncated value must
        # prompt, not pass. (A stored-but-blank password once sailed through
        # here as "stored" while push stayed down.)
        have_pw = (s.get("GOOGLE_IMAP_PASSWORD") or "").replace(" ", "")
        if state.get("google") != "ok":
            print("  ! skipping — sign into Google first, then re-run "
                  "`mail-agent setup --step push`.")
            state.setdefault("push", "skipped")
        elif len(have_pw) == 16:
            print("  push: app password stored — IDLE watcher will run.")
            state["push"] = "ok"
        elif not _tty():
            state.setdefault("push", "skipped")
        else:
            print("  Two-minute setup, once:")
            print("  1. Open  https://myaccount.google.com/apppasswords")
            print("     (sign in; needs 2-Step Verification ON — it will tell")
            print("      you if it isn't, and turning it on is one toggle)")
            print("  2. Name it 'mailbot' → Generate → copy the 16 letters.")
            print("  3. Paste below. Spaces don't matter; I strip them.")
            print("  New mail then wakes the agent in seconds. Without it the")
            print("  agent polls on an interval instead — slower, same bills.")
            try:
                import getpass as _gp

                try:
                    pw = _gp.getpass("  App password (hidden, empty skips push): ")
                except Exception:
                    pw = input("  App password (empty skips push): ")
                pw = pw.strip().replace(" ", "")
            except (EOFError, KeyboardInterrupt, OSError):
                pw = ""
            if not pw:
                print("  push skipped — interval polling covers you.")
                state.setdefault("push", "skipped")
            elif len(pw) != 16:
                print(f"  ! that is {len(pw)} characters; app passwords are 16 — "
                      "not saved. Re-run `mail-agent setup --step push` with the real one.")
                state["push"] = "failed"
            else:
                _write_p("GOOGLE_IMAP_PASSWORD", pw)
                # Prove it landed: re-read and count, so "stored" always
                # means verifiably on disk, never assumed.
                got = (_read_p().get("GOOGLE_IMAP_PASSWORD") or "").replace(" ", "")
                if len(got) != 16:
                    print("  ! write did not stick — try `mail-agent setup --step push` again.")
                    state["push"] = "failed"
                else:
                    print("  push OK: app password stored (600), verified on disk.")
                    state["push"] = "ok"
    else:
        state.setdefault("push", "skipped")

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

    # --- service: always on, survives reboot ---
    if (not only or only == "start") and not skip_service:
        idx += 1
        _hdr(idx, "service")
        _step_service(state, non_interactive or fast or yes)

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
    s = read_secrets()
    if state.get("provider") == "ok":
        prov = f"OK ({s.get('ROUTER_MODEL', '?')})"
    else:
        prov = "missing — need API key"
    tri = s.get("ROUTER_TRIAGE_MODEL", "")
    tri = f"two-brain ({tri})" if tri else "off (flagship does triage)"
    jev_state = state.get("jev", "")
    if jev_state == "ok":
        decider = f"OK ({s.get('JEV_MODEL', 'jev')})"
    elif jev_state == "off":
        decider = "off (flagship reads everything)"
    elif jev_state:
        decider = f"{jev_state} — run `mail-agent setup --step jev`"
    else:
        decider = "unconfigured — run `mail-agent setup --step jev`"
    push = {"ok": "IDLE (wake on mail)"}.get(state.get("push", ""),
                                             state.get("push", "polling"))
    if state.get("google") == "ok":
        box = "OK"
    else:
        box = "missing — need OAuth"
    chats = [k for k, v in
             (("Telegram", state.get("telegram")), ("Discord", state.get("discord")),
              ("Slack", state.get("slack"))) if v == "ok"]
    chat = ", ".join(chats) if chats else "skipped (optional)"
    voice = {"ok": "learned"}.get(state.get("voice", ""), state.get("voice", "skipped"))
    print("\n  What works:")
    print(f"    brain ..... {prov}")
    print(f"    triage .... {tri}")
    print(f"    decider ... {decider}")
    print(f"    mailbox ... {box}")
    print(f"    push ...... {push}")
    print(f"    chat ...... {chat}")
    print(f"    voice ..... {voice}")
    print(f"  Files: {config_dir()}/config.json, .secrets (600), google-token.json")


def cmd_doctor(args, cfg) -> int:
    """Pre-flight checks with fix hints. Exit 0 iff usable."""
    import sys as _sys

    problems = 0

    def check(name: str, ok: bool, hint: str = "", required: bool = True):
        """required=False marks an optional integration.

        Optional items got the same red ✘ as real failures, so a perfectly
        healthy install that simply has no Telegram looked broken. They now
        get a neutral glyph and never affect the exit code.
        """
        nonlocal problems
        if not ok and not required:
            print(f"  · {name} — {hint or 'not set (optional)'}")
            return
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
          "optional — mail-agent setup --step chat", required=False)
    if s.get("JEV_ENABLED", "1") == "0":
        check("jev decider", False, "off — flagship reads everything "
              "(mail-agent setup --step jev to enable)", required=False)
    else:
        jurl = s.get("JEV_BASE_URL") or s.get("ROUTER_BASE_URL", "")
        jkey = s.get("JEV_API_KEY") or s.get("ROUTER_API_KEY", "")
        jmodel = s.get("JEV_MODEL", "jev")
        ok, detail = verify_jev(jurl, jkey, jmodel) if (jurl and jkey) else (False, "no endpoint/key")
        check("jev decider reachable", ok,
              f"{detail} — mail-agent setup --step jev" if not ok else "")
    check("gmail push (IDLE)", bool(s.get("GOOGLE_IMAP_PASSWORD")),
          "optional — app password, else interval polling "
          "(mail-agent setup --step push)", required=False)
    try:
        from .storage import db

        db.migrate()
        check("database migrated", True)

        # Mail the agent believes it has passed but has not. The cursor
        # used to jump to the newest message in the account rather than
        # the newest this run handled, so a crashed run could orphan mail
        # permanently with no symptom at all. Self-detecting the bug
        # class beats a user filing it.
        with db.db() as c:
            orphans = c.execute("""
                SELECT COUNT(*) AS n
                  FROM messages m JOIN cursors cu
                    ON cu.account = m.account AND cu.stream = 'inbox'
                 WHERE m.processed_at IS NULL
                   AND m.id = cu.last_id
            """).fetchone()["n"]
        if orphans:
            print(f"  · {orphans} message(s) sit at the cursor unprocessed.")
            print("    Nothing is lost — they are still in Gmail. To re-queue them:")
            print("      mail-agent requeue-unprocessed")
        else:
            check("no orphaned mail at the cursor", True)
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
