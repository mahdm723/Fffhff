#!/usr/bin/env bash
# =============================================================================
# DZPLAY — connect your Telegram bot (stats, password recovery, moderation buttons) in one step.
#
#   sudo /opt/dzplay/dzplay/deploy/telegram-setup.sh
#
# Before running: in Telegram, open your bot and press "Start" (a bot cannot write
# to someone who never started it).
#
# It asks for the bot token (hidden, never shown or logged) and your chat ID,
# saves them in .env (root-only), restarts the app, registers the webhook with
# Telegram and sends you a test message.
# =============================================================================
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$APP_DIR"
COMPOSE="${COMPOSE:-docker compose}"

ok()   { printf '  \033[1;32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[1;33m!\033[0m %s\n' "$*"; }
die()  { printf '\n\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

[ -f .env ] || die "No .env in $APP_DIR — run the installer first."
[ "$(id -u)" -eq 0 ] || [ -w .env ] || die "Run with sudo."

set_env() {  # set_env KEY VALUE — replace or append (value never printed)
  local tmp
  tmp="$(mktemp)"
  grep -v "^$1=" .env > "$tmp" || true
  printf '%s=%s\n' "$1" "$2" >> "$tmp"
  cat "$tmp" > .env
  rm -f "$tmp"
}
env_get() { grep -E "^$1=" .env | tail -1 | cut -d= -f2- || true; }

echo
echo "DZPLAY — Telegram bot setup"
echo "  1) Get the token from @BotFather (use a NEW token: /revoke the one shared in chats)."
echo "  2) Your chat ID: send any message to @userinfobot in Telegram, it replies with your Id."
echo "  3) Open your own bot and press Start."
echo

TOKEN="${TELEGRAM_BOT_TOKEN_INPUT:-}"
if [ -z "$TOKEN" ]; then
  read -r -s -p "Bot token (hidden): " TOKEN </dev/tty
  echo
fi
[[ "$TOKEN" =~ ^[0-9]{6,12}:[A-Za-z0-9_-]{30,}$ ]] || die "That does not look like a bot token (format 123456789:AA…)."

CHAT="${TELEGRAM_CHAT_ID_INPUT:-}"
if [ -z "$CHAT" ]; then
  current="$(env_get TELEGRAM_ADMIN_CHAT_ID)"
  read -r -p "Your chat ID${current:+ [$current]}: " CHAT </dev/tty
  CHAT="${CHAT:-$current}"
fi
[[ "$CHAT" =~ ^-?[0-9]{3,20}$ ]] || die "The chat ID must be a number (e.g. 323530056)."

set_env TELEGRAM_BOT_TOKEN "$TOKEN"
set_env TELEGRAM_ADMIN_CHAT_ID "$CHAT"
[ -n "$(env_get TELEGRAM_WEBHOOK_SECRET)" ] || set_env TELEGRAM_WEBHOOK_SECRET "$(openssl rand -hex 32)"
chmod 600 .env
unset TOKEN
ok "Saved in .env (readable by root only)"

$COMPOSE up -d app </dev/null >/dev/null
for i in $(seq 1 60); do
  $COMPOSE exec -T app python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')" >/dev/null 2>&1 </dev/null && break
  [ "$i" -eq 60 ] && die "The app did not start: $COMPOSE logs --tail 50 app"
  sleep 2
done
ok "App restarted with the bot settings"

PUBLIC_URL="$(env_get PUBLIC_URL)"
[ "${PUBLIC_URL#https://}" != "$PUBLIC_URL" ] || die "PUBLIC_URL in .env must be https:// (Telegram requires HTTPS)."
$COMPOSE exec -T app python -m app.admin_cli set-webhook "$PUBLIC_URL" </dev/null >/dev/null \
  || die "Telegram refused the webhook (check the token, then run again)."
ok "Webhook registered: $PUBLIC_URL/api/telegram/webhook"

if out="$($COMPOSE exec -T app python -m app.admin_cli bot-test </dev/null 2>&1)"; then
  ok "Test message sent — check Telegram"
else
  warn "$out"
  warn "If it says 'chat not found': open your bot in Telegram, press Start, then run this again."
  exit 1
fi

cat <<EOF

Done. In your bot chat:
  • /stats  → users, ideas of the last 24 h, password resets waiting
  • password-reset requests and moderation buttons (pictures, blue-star payments) arrive here
  • /help
EOF
