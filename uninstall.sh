#!/usr/bin/env bash
# mail-agent uninstall. Shows what lives where, then removes code only
# (keep data, the default) or everything. Never deletes without asking.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="${MAIL_AGENT_CONFIG_DIR:-$HOME/.config/mail-agent}"
DATA_DIR="${MAIL_AGENT_DATA_DIR:-$HOME/.local/share/mail-agent}"
VENV="$REPO/.venv"

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ] && [ "${TERM:-}" != "dumb" ]; then
  BOLD=$'\033[1m'; DIM=$'\033[2m'; GRN=$'\033[32m'; YLW=$'\033[33m'; RST=$'\033[0m'
else
  BOLD=''; DIM=''; GRN=''; YLW=''; RST=''
fi

printf '\n%s\n' "${BOLD}┌──────────────────────────────────────────────────┐"
printf '%s\n' "│              Mailbot Uninstaller                   │"
printf '%s%s\n' "└──────────────────────────────────────────────────┘" "$RST"
printf '\nCurrent installation:\n'
printf '  Code:    %s\n' "$REPO"
printf '  Config:  %s\n' "$CONFIG_DIR"
printf '  Data:    %s\n' "$DATA_DIR"
printf '\nUninstall options:\n'
printf '\n1) Keep data — remove code only, keep configs/sessions/logs'
printf '\n   (Recommended — reinstall later with settings intact)'
printf '\n2) Full uninstall — remove everything including all data'
printf '\n   (Warning: deletes configs, sessions, and logs permanently)'
printf '\n3) Cancel — do nothing\n'

choice=""
if [ ! -t 0 ]; then
  echo "stdin is not a terminal — refusing to guess. Re-run interactively." >&2
  exit 2
fi
read -r -p "Select option [1/2/3]: " choice
case "${choice:-3}" in
  1)
    "$REPO/start.sh" stop >/dev/null 2>&1 || true
    rm -rf "$VENV"
    rm -rf "$REPO/.pytest_cache" "$REPO/src/mailbot/__pycache__"
    find "$REPO/src" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
    printf '%s\n' "${GRN}✔ Code removed. Config, sessions, and logs kept in:${RST}"
    printf '  %s\n  %s\n' "$CONFIG_DIR" "$DATA_DIR"
    ;;
  2)
    printf '%s\n' "${YLW}This deletes configs, sessions, and logs permanently.${RST}"
    read -r -p "Type DELETE to confirm: " confirm
    if [ "${confirm:-}" != "DELETE" ]; then
      echo "Cancelled."
      exit 0
    fi
    "$REPO/start.sh" stop >/dev/null 2>&1 || true
    rm -rf "$VENV" "$CONFIG_DIR" "$DATA_DIR"
    rm -rf "$REPO/.pytest_cache"
    find "$REPO/src" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
    printf '%s\n' "${GRN}✔ Everything removed.${RST}"
    ;;
  *)
    echo "Cancelled — nothing touched."
    ;;
esac
