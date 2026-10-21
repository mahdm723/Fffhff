#!/usr/bin/env bash
# =============================================================================
# V6 phase 17 — pictures in chats are switched off for good: delete the old ones everywhere
# (the server, its cache, and the Telegram storage channel), except those tied to a report
# (kept as evidence for the moderators until MEDIA_EVIDENCE_RETENTION_DAYS, then deleted as before).
#
#   sudo /opt/dzplay/dzplay/deploy/chat-images-purge.sh
#
# 1. nothing left to do → says so and stops (the installer runs it on every update);
# 2. a normal encrypted full backup first (deploy/backup.sh) — if it fails, nothing is deleted;
# 3. `admin_cli purge-chat-images`. What the bot could not delete from the channel is listed (message ids) and
#    retried on the next run.
# =============================================================================
set -euo pipefail
umask 077

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$APP_DIR"
COMPOSE="${COMPOSE:-docker compose}"

ok()  { printf '    \033[1;32m✓\033[0m %s\n' "$*"; }
die() { printf '\n\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || [ "${V6_CLEANUP_ALLOW_USER:-0}" = 1 ] || die "Run as root (sudo)."
[ -f .env ] || die "No .env in $APP_DIR"
cli() { $COMPOSE exec -T app python -m app.admin_cli "$@" </dev/null; }

set +e
status="$(cli purge-chat-images --check)"; code=$?
set -e
case "$code" in
  0) ok "Chat pictures: nothing left to delete"; exit 0 ;;
  1) echo "$status" ;;
  *) echo "$status" >&2; die "Could not read the status (is the app running?)" ;;
esac

"$APP_DIR/deploy/backup.sh" || die "The full backup failed: nothing was deleted."
ok "Full encrypted backup taken"
cli purge-chat-images || die "The purge stopped (see above). Run this script again."
ok "Chat pictures deleted (reported ones kept as evidence)"
