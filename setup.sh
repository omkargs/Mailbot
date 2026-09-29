#!/usr/bin/env bash
# mail-agent setup. Collects credentials, installs, and wires systemd.
#
# Secrets are written to ~/.config/mail-agent/.secrets with mode 600 and are
# never echoed back, never logged, and never committed.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="${MAIL_AGENT_CONFIG_DIR:-$HOME/.config/mail-agent}"
SECRETS="$CONFIG_DIR/.secrets"
CONFIG="$CONFIG_DIR/config.json"
VENV="$REPO/.venv"
INSTALL_LOG="$CONFIG_DIR/logs/install.log"

# Stage protocol: --manifest prints the stages as JSON, --stage NAME runs
# one, --non-interactive skips input. Fresh install and re-run land in the
# same state — every stage is idempotent.
STAGE=""
WANT_MANIFEST=false
NON_INTERACTIVE=false
VERBOSE=false
DO_REINSTALL=false
WIZ_ARGS=()

usage() {
  cat <<EOF
Usage: ./setup.sh [--fast] [--reinstall] [--non-interactive] [--verbose]
                  [--stage NAME] [--manifest] [wizard flags]

  Stages: python, install, setup, complete
    ./setup.sh --manifest        print the stage list as JSON
    ./setup.sh --stage install   run one stage only
    ./setup.sh --fast            full run, defaults, no optional prompts
    ./setup.sh --reinstall       force a fresh venv + reinstall
    ./setup.sh --non-interactive never prompt (reads env, like CI)

  Everything else is passed to the wizard (see SETUP.md).
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --manifest) WANT_MANIFEST=true; shift ;;
    --stage)
      [ $# -lt 2 ] && { echo "--stage needs a value" >&2; exit 2; }
      STAGE="$2"; shift 2 ;;
    --non-interactive) NON_INTERACTIVE=true; WIZ_ARGS+=(--non-interactive); shift ;;
    --verbose) VERBOSE=true; shift ;;
    --reinstall) DO_REINSTALL=true; shift ;;
    *) WIZ_ARGS+=("$1"); shift ;;
  esac
done

emit_manifest() {
  printf '{"stages":['
  printf '{"name":"python","title":"Find Python 3.11+","needs_user_input":false},'
  printf '{"name":"install","title":"Create venv, install package","needs_user_input":false},'
  printf '{"name":"setup","title":"Provider, Google, chat, voice","needs_user_input":true},'
  printf '{"name":"complete","title":"Verify and show next steps","needs_user_input":false}'
  printf ']}\n'
}

if [ "$WANT_MANIFEST" = true ]; then
  emit_manifest
  exit 0
fi

mkdir -p "$(dirname "$INSTALL_LOG")"
exec 3>>"$INSTALL_LOG"
_log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&3; }

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ] && [ "${TERM:-}" != "dumb" ]; then
  BOLD=$'\033[1m'; DIM=$'\033[2m'; GRN=$'\033[32m'; YLW=$'\033[33m'; RST=$'\033[0m'
else
  BOLD=''; DIM=''; GRN=''; YLW=''; RST=''
fi

# Log a line, stripped of colour. A log full of escape codes is unreadable
# in less/grep, which is the only reason it exists.
_strip_ansi() { printf '%s' "$1" | sed -E 's/\x1b\[[0-9;]*[A-Za-z]//g'; }

say()  { printf '%s\n' "$*"; _log "$(_strip_ansi "$*")" 2>/dev/null || true; }
ok()   { printf '%s✔ %s%s\n' "$GRN" "$*" "$RST"; _log "$(_strip_ansi "✔ $*")" 2>/dev/null || true; }
warn() { printf '%s! %s%s\n' "$YLW" "$*" "$RST"; _log "$(_strip_ansi "! $*")" 2>/dev/null || true; }
die()  {
  local head="$1"
  printf '%s✘ %s%s\n' "$RST" "$head" "$RST"
  shift || true
  # Remaining args are indented detail lines. `[ $# -gt 0 ]` keeps `set -u`
  # happy when die is called with a single argument.
  if [ "$#" -gt 0 ]; then
    printf '%s\n' "$@"
  fi
  _log "FATAL: $head" 2>/dev/null || true
  exit 1
}

# Pick a Python >= 3.11, self-healing. Tries, in order:
#   1. python3.13 / 3.12 / 3.11 / 3 on PATH
#   2. uv (fetches its own Python automatically)
#   3. apt install (root containers like fresh VPS images)
# Dies with exact remediation steps only when everything failed.
pick_python() {
  local c v
  for c in python3.13 python3.12 python3.11 python3; do
    if command -v "$c" >/dev/null 2>&1; then
      # Reject prereleases. `command -v python3.11` happily matches
      # 3.11.0rc1, and an rc build is not what a stranger should be handed
      # when they are already hitting friction in the first 60 seconds.
      case "$("$c" --version 2>&1)" in
        *rc*|*alpha*|*beta*|*dev*|*'+'*)
          warn "skipping $c ($("$c" --version 2>&1)) — prerelease build"
          continue ;;
      esac
      v="$("$c" -c 'import sys; print(sys.version_info[0]*100+sys.version_info[1])' 2>/dev/null | tr -cd '0-9')"
      if [ "${v:-0}" -ge 311 ] 2>/dev/null; then
        printf '%s' "$c"
        return 0
      fi
    fi
  done
  if command -v uv >/dev/null 2>&1; then
    printf 'uv'
    return 0
  fi
  if [ "$(id -u)" -eq 0 ] && command -v apt-get >/dev/null 2>&1; then
    say "${DIM}No Python 3.11+ found — installing python3.12 (needs root once)…${RST}"
    apt-get update -qq 2>&1 | tail -n 1 || true
    if apt-get install -y -qq python3.12 python3.12-venv 2>&1 | tail -n 2; then
      if command -v python3.12 >/dev/null 2>&1; then
        printf 'python3.12'
        return 0
      fi
    fi
    warn "apt install failed — continuing to error below"
  fi
  return 1
}

make_venv() {
  # $1 = venv dir. Uses $PY (a binary) or 'uv' (fetches Python itself).
  if [ "${PY:-}" = "uv" ]; then
    uv venv --python '>=3.11' "$1"
  else
    "$PY" -m venv "$1" 2>/dev/null || {
      warn "'$PY -m venv' failed — trying with pip bundled via ensurepip"
      "$PY" -m ensurepip --upgrade 2>/dev/null || true
      "$PY" -m venv "$1"
    }
  fi
}

pip_install() {
  # $1 = venv dir, rest = pip args. Prefers uv, falls back to venv pip.
  # --verbose streams; otherwise quiet (the wizard says how long it takes).
  local _q="-q"
  [ "${VERBOSE:-false}" = true ] && _q=""
  if command -v uv >/dev/null 2>&1; then
    # shellcheck disable=SC2086
    VIRTUAL_ENV="$1" uv pip install "${@:2}" $_q 2>/dev/null && return 0
  fi
  # shellcheck disable=SC2086
  "$1/bin/pip" install "${@:2}" $_q
}

# Network preflight. pip failing on DNS prints 40 lines of urllib3 noise and
# looks like a broken repo. Check first and say what is actually wrong.
check_network() {
  command -v git >/dev/null 2>&1 || return 0
  git ls-remote --exit-code -h https://github.com/omkargs/Mailbot.git HEAD >/dev/null 2>&1 && return 0
  # git can fail for a proxy/auth reason rather than DNS. Only treat a clear
  # resolution failure as "offline" so a proxy setup is never mislabelled.
  local out
  out="$(git ls-remote https://github.com/omkargs/Mailbot.git HEAD 2>&1 || true)"
  case "$out" in
    *"Could not resolve host"*|*"Temporary failure in name resolution"*|*"Failed to connect to github.com"*)
      die "No network to GitHub — everything below needs it (pip, uv, provider).
    Check: can you open https://github.com in a browser?
    Behind a proxy? export HTTPS_PROXY=http://host:port and re-run.
    Already have the code and a wheelhouse? pip install --no-index -e ."
      ;;
  esac
  return 0
}

PY=""
check_network
if ! PY="$(pick_python)"; then
  die "No Python 3.11+ and none installable automatically.

  Fix one of these, then re-run ./setup.sh:
    1. Python 3.11+:  apt install python3.12 python3.12-venv   (Debian/Ubuntu)
                       dnf install python3.12                  (Fedora)
                       brew install python@3.12                (macOS)
    2. Or install uv (it fetches Python itself):
                       curl -LsSf astral.sh/uv/install.sh | sh
  Current: $(command -v python3 >/dev/null && python3 --version 2>&1 || echo 'no python3 on PATH')"
fi
[ "$PY" = "uv" ] && say "${DIM}Using uv-managed Python (>=3.11, auto-fetched)${RST}" \
  || say "${DIM}Using $PY ($("$PY" --version 2>&1))${RST}"

# Boxed banner: the 5-second hello. Skippable with any key. Never shows
# when output is piped, stdin is not a TTY, in fast/CI runs, or with
# MAIL_AGENT_NO_SPLASH — branding must never slow automation.
splash() {
  case " $* " in
    *" --fast "*|*" --yes "*|*" --non-interactive "*|*" --import-env "*|*" --dry-run "*|*" --step "*|*" --help "*|*" -h "*)
      return 0 ;;
  esac
  [ -t 0 ] || return 0
  [ -n "${MAIL_AGENT_NO_SPLASH:-}" ] && return 0
  printf '\n%s\n' "${BOLD}┌──────────────────────────────────────────────────┐"
  printf '%s\n' "│              __  __       _  _  _                 │"
  printf '%s\n' "│             |  \/  |  __ _ (_)| |  ___ ___        │"
  printf '%s\n' "│             | |\/| | / _\` || || | / _ / _ \       │"
  printf '%s\n' "│             | |  | || (_| || || || (_) (_) |      │"
  printf '%s\n' "│             |_|  |_| \__,_||_||_| \___/ \___/      │"
  printf '%s\n' "├──────────────────────────────────────────────────┤"
  printf '%s\n' "│  The inbox colleague that acts. MIT, yours.     │"
  printf '%s%s\n' "└──────────────────────────────────────────────────┘" "$RST"
  printf '%s\n' "${DIM}showing off for 5s — press any key to skip${RST}"
  read -t 5 -n 1 -s -r _splash_key 2>/dev/null || true
  printf '\n'
}
splash "${WIZ_ARGS[@]}"
hdr()  { printf '\n%s▸ %s%s\n\n' "$BOLD" "$*" "$RST"; }

# Secret input: visible=false, no default echo, no history.
# Returns non-zero when stdin is not a TTY, so callers can fail loudly
# instead of silently writing an empty credential.
ask_secret() {
  local prompt="$1" val
  if [ ! -t 0 ]; then
    printf '\n' >&2
    return 1
  fi
  read -r -s -p "$(printf '%s' "$prompt") " val
  printf '\n'
  printf '%s' "$val"
}

# setkv that reports failure. Returns non-zero if the value was empty, so a
# skipped step can never print a green checkmark.
setkv_required() {
  local key="$1" val="$2"
  if [ -z "$val" ]; then
    warn "no value given for $key — not written"
    return 1
  fi
  setkv "$key" "$val"
  return 0
}

ask_plain() {
  local prompt="$1" def="${2:-}" val
  if [ -n "$def" ]; then
    read -r -p "$(printf '%s' "$prompt") [$def]: " val
    printf '%s' "${val:-$def}"
  else
    read -r -p "$(printf '%s' "$prompt"): " val
    printf '%s' "$val"
  fi
}

ask_yn() {
  local prompt="$1" def="${2:-n}" val
  [ "$def" = "y" ] && printf '%s(y/N)%s' "$DIM" "$RST" || printf '%s(Y/n)%s' "$DIM" "$RST"
  read -r -p "$prompt " val
  case "${val:-$def}" in
    y|Y|yes|YES|Yes) return 0 ;;
    *) return 1 ;;
  esac
}

setkv() {
  # setkv <KEY> <value>  — idempotent upsert into the secrets file.
  #
  # The value is NEVER passed through sed. A previous version escaped it with
  # sed and then interpolated the result into a second sed replacement, so a
  # key containing a backslash, $, `, or & produced
  # "unterminated `s' command" and setup died with the credential half-written.
  #
  # Instead: strip control characters in pure bash, then use shell single
  # quotes for the value. Single-quoted values need no escaping except a
  # literal single quote, handled below.
  local key="$1" val="$2" tmp
  [ -z "$val" ] && return 0

  # Drop newlines/carriage returns and any other control chars. A credential
  # never legitimately contains these, and a stray newline would let an
  # attacker append arbitrary shell to this file.
  val="${val//[$'\n\r']/}"
  # Escape embedded single quotes the POSIX way: end quote, escaped quote.
  val="${val//\'/\'\\\'\'}"

  mkdir -p "$CONFIG_DIR"
  touch "$SECRETS"
  tmp="$(mktemp)"
  chmod 600 "$tmp"

  if [ -f "$SECRETS" ]; then
    grep -vE "^[[:space:]]*(export[[:space:]]+)?${key}=" "$SECRETS" > "$tmp" 2>/dev/null || true
  fi
  printf "export %s='%s'\n" "$key" "$val" >> "$tmp"
  mv "$tmp" "$SECRETS"
  chmod 600 "$SECRETS"
}

say "${BOLD}mail-agent setup${RST}"
say "${DIM}$REPO${RST}"

# Fast path: venv + new unified wizard. Keeps ./setup.sh working as the
# documented entry point while the real logic lives in `mail-agent setup`
# (headless-friendly, --non-interactive, resumable). Falls through to the
# legacy flow below only if the CLI is unavailable.
if [ "${MAIL_AGENT_LEGACY_SETUP:-}" != "1" ]; then
  if [ -z "$STAGE" ] || [ "$STAGE" = "install" ]; then
    if [ ! -d "$VENV" ]; then
      make_venv "$VENV" || die "could not create the venv at $VENV (see errors above)"
    fi
    # Re-runs are instant: a working venv is reused. Pass --reinstall to force.
    if [ "$DO_REINSTALL" = true ] || ! "$VENV/bin/python" -c "import mailbot" >/dev/null 2>&1; then
      say "${DIM}Installing packages (a minute or two on first run — still working if quiet)…${RST}"
      _log "==> pip install -e $REPO $(date -u +%Y-%m-%dT%H:%M:%SZ)"
      if ! pip_install "$VENV" -e "$REPO"; then
        if [ "${VERBOSE:-false}" != true ]; then
          # quiet mode swallowed the useful part. Repeat verbose so the real
          # error is on screen, not just the one-line summary.
          warn "install failed — re-running with full output for the real error"
          VERBOSE=true
          pip_install "$VENV" -e "$REPO" || true
        fi
        die "could not install mailbot into $VENV
    Full log: $INSTALL_LOG
    Common causes: no network (see above), or a proxy needing HTTPS_PROXY."
      fi
    else
      say "${DIM}Reusing the existing install (pass --reinstall to force)…${RST}"
    fi
  fi
  case "$STAGE" in
    ""|"setup")
      if [ -x "$VENV/bin/mail-agent" ] && "$VENV/bin/mail-agent" setup --help >/dev/null 2>&1; then
        exec "$VENV/bin/mail-agent" setup "${WIZ_ARGS[@]}"
      fi
      warn "new wizard unavailable, falling back to legacy prompts"
      ;;
    "python")
      [ "$PY" = "uv" ] && say "Python: uv-managed (>=3.11)" || say "Python: $("$PY" --version 2>&1)"
      exit 0
      ;;
    "install")
      say "${GRN}✔ install stage done${RST}"
      exit 0
      ;;
    "complete")
      "$VENV/bin/mail-agent" doctor
      exit "$?"
      ;;
    *) die "unknown stage: $STAGE (see --manifest)" ;;
  esac
fi

# ---------------------------------------------------------------- python (legacy)
hdr "Python environment"
if [ ! -d "$VENV" ]; then
  make_venv "$VENV" || die "could not create the venv at $VENV (see errors above)"
fi
ok "venv at $VENV"
pip_install "$VENV" -e "$REPO" || die "could not install mailbot into $VENV (see errors above)"
ok "package installed"

mkdir -p "$CONFIG_DIR"
chmod 700 "$CONFIG_DIR"

# ---------------------------------------------------------------- router
hdr "AI provider (router.bynara.id)"
setkv "ROUTER_BASE_URL" "$(ask_plain 'Base URL' 'https://router.bynara.id')"
setkv "ROUTER_MODEL" "$(ask_plain 'Model combo' 'combo/claude2mail')"
k="$(ask_secret 'Router API key (input hidden)')"
setkv "ROUTER_API_KEY" "$k"
[ -n "$k" ] && ok "router key stored" || warn "no router key — the agent cannot run yet"

# ---------------------------------------------------------------- google
hdr "Google (Gmail + Calendar)"
if ask_yn "Set up a Google account?" y; then
  creds="$CONFIG_DIR/google-credentials.json"
  say "${DIM}Download an OAuth Desktop client from:"
say "  https://console.cloud.google.com/apis/credentials"
say "  Client ID → Download JSON, then give the path here."
  src="$(ask_plain 'Path to credentials JSON' "$creds")"
  if [ -f "$src" ]; then
    cp -f "$src" "$creds"; chmod 600 "$creds"
    setkv "GOOGLE_CREDENTIALS" "$creds"
    setkv "GOOGLE_ACCOUNT" "$(ask_plain 'Gmail address' "$HOME@gmail.com")"
    setkv "GOOGLE_DISPLAY_NAME" "$(ask_plain 'Display name' "${USER}")"
    if ask_yn "Let the agent manage Google Calendar?" y; then
      setkv "GOOGLE_CALENDAR_ENABLED" "true"
    else
      setkv "GOOGLE_CALENDAR_ENABLED" "false"
    fi
    ok "google configured"
  else
    warn "no file at $src — skipping Google"
  fi
fi

# ---------------------------------------------------------------- microsoft
hdr "Microsoft 365 / Outlook"
if ask_yn "Set up a Microsoft account?" n; then
  setkv "MS_TENANT_ID" "$(ask_plain 'Tenant ID' 'common')"
  setkv "MS_CLIENT_ID" "$(ask_plain 'Application (client) ID')"
  cs="$(ask_secret 'Client secret (input hidden)')"
  setkv "MS_CLIENT_SECRET" "$cs"
  setkv "MS_ACCOUNT" "$(ask_plain 'Email address')"
  setkv "MS_DISPLAY_NAME" "$(ask_plain 'Display name' "${USER}")"
  if ask_yn "Let the agent manage Outlook Calendar?" n; then
    setkv "MS_CALENDAR_ENABLED" "true"
  else
    setkv "MS_CALENDAR_ENABLED" "false"
  fi
  ok "microsoft configured (device-code auth on first run)"
fi

# ---------------------------------------------------------------- discord
hdr "Discord (approvals + brief)"
if ask_yn "Set up Discord?" y; then
  say "${DIM}Create an app at https://discord.com/developers/applications"
  say "  Bot → enable MESSAGE CONTENT INTENT. Invite with 'bot' scope and 'Send Messages'."
  tok="$(ask_secret 'Bot token (input hidden)')"
  if setkv_required "DISCORD_BOT_TOKEN" "$tok"; then
    setkv "DISCORD_USER_ID" "$(ask_plain 'Your Discord user ID (for DM)')"
    ok "discord configured"
  else
    warn "discord skipped — approvals will have nowhere to go. Rerun setup.sh to add it."
  fi
fi

hdr "Telegram (optional)"
if ask_yn "Set up Telegram?" n; then
  tok="$(ask_secret 'Bot token (input hidden)')"
  if setkv_required "TELEGRAM_BOT_TOKEN" "$tok"; then
    setkv "TELEGRAM_CHAT_ID" "$(ask_plain 'Your chat ID')"
    ok "telegram configured"
  else
    warn "telegram skipped"
  fi
fi

hdr "Slack (optional)"
if ask_yn "Set up Slack?" n; then
  tok="$(ask_secret 'Bot token (input hidden)')"
  if setkv_required "SLACK_BOT_TOKEN" "$tok"; then
    setkv "SLACK_CHANNEL" "$(ask_plain 'Channel ID')"
    ok "slack configured"
  else
    warn "slack skipped"
  fi
fi

# ---------------------------------------------------------------- policy
hdr "Send policy"
if ask_yn "Allow the agent to send WITHOUT your approval, for contacts you approve? (recommended)" y; then
  setkv "AGENT_SEND_MODE" "auto"
  setkv "GOOGLE_AUTO_SEND" "true"
  setkv "MS_AUTO_SEND" "true"
  say "${DIM}You still approve each contact once via: mail-agent contacts --approve EMAIL${RST}"
else
  setkv "AGENT_SEND_MODE" "never"
  setkv "GOOGLE_AUTO_SEND" "false"
  setkv "MS_AUTO_SEND" "false"
  warn "agent will only ever create drafts"
fi
setkv "AGENT_SCAN_INTERVAL" "$(ask_plain 'Scan interval seconds' '300')"
setkv "AGENT_DAILY_TOKEN_CAP" "$(ask_plain 'Daily token cap' '2000000')"
setkv "AGENT_BRIEF_HOUR" "$(ask_plain 'Hour for morning brief (0-23)' '7')"

# ---------------------------------------------------------------- config.json
hdr "Writing config"
cat > "$CONFIG" <<JSON
{
  "enabled_accounts": [],
  "auto_send_contacts": [],
  "never_auto_send": [],
  "escalation_keywords": [
    "invoice","payment","wire","contract","legal","attorney","bank","salary",
    "offer","termination","medical","passport","ssn","password","otp","verify",
    "account number","recovery code","2fa","mfa","social security",
    "one-time code","one time code","date of birth","bank transfer",
    "wire transfer","routing number","deposit"
  ]
}
JSON
chmod 600 "$CONFIG"
ok "wrote $CONFIG"

# ---------------------------------------------------------------- auth
hdr "Authentication"
if "$VENV/bin/mail-agent" check; then
  ok "router reachable"
else
  warn "router check failed — fix ROUTER_API_KEY before running the agent"
fi

if ask_yn "Run the browser auth flow now?" y; then
  "$VENV/bin/mail-agent" auth || warn "auth did not complete"
fi

if ask_yn "Build the Email Brain from your sent mail now? (recommended)" y; then
  "$VENV/bin/mail-agent" brain || warn "brain build failed"
fi

# ---------------------------------------------------------------- systemd
hdr "Background service"
if command -v systemctl >/dev/null 2>&1; then
  mkdir -p "$HOME/.config/systemd/user"
  sed -e "s|@REPO@|$REPO|g" -e "s|@VENV@|$VENV|g" \
      "$REPO/systemd/mail-agent.service" > "$HOME/.config/systemd/user/mail-agent.service"
  systemctl --user daemon-reload
  # Do not start a service that will crash-loop. Only start it if the
  # prerequisites are actually present.
  CAN_START=1
  [ -z "$(grep -oE '^export ROUTER_API_KEY="[^"]+"' "$SECRETS" 2>/dev/null | grep -v '=""')" ] && CAN_START=0
  if [ ! -f "$CONFIG_DIR/google-token.json" ] && [ ! -f "$CONFIG_DIR/ms-token.json" ]; then
    CAN_START=0
  fi

  if [ "$CAN_START" -eq 0 ]; then
    warn "service installed but NOT started — router key or account login is missing."
    warn "starting it now would crash-loop. Finish setup, then:"
    say "    systemctl --user start mail-agent"
  elif ask_yn "Start the agent on login?" y; then
    systemctl --user enable --now mail-agent.service
    ok "mail-agent.service running"
  else
    warn "installed but not started — systemctl --user start mail-agent"
  fi
else
  warn "systemd not found; run the daemon manually: $VENV/bin/mail-agent daemon"
fi

hdr "Done"
cat <<EOF
Next steps:
  1. Review your voice profile:   cat $REPO/brain/profile-google.md
  2. Approve a contact:           $VENV/bin/mail-agent contacts --approve someone@example.com
  3. Watch the agent work:        $VENV/bin/mail-agent scan --capped    # drafts only, no sends
  4. Go live:                     systemctl --user start mail-agent
  5. Morning brief:               $VENV/bin/mail-agent brief

Config:   $CONFIG
Secrets:  $SECRETS  (mode 600)
EOF
