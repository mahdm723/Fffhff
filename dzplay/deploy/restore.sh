#!/usr/bin/env bash
# =============================================================================
# DZPLAY — restore a backup made by deploy/backup.sh.
#
#   sudo /opt/dzplay/dzplay/deploy/restore.sh /var/backups/dzplay/dzplay-YYYYMMDDTHHMMSSZ.tar.gpg [--with-env] [--yes]
#
#   --with-env   also put back the .env saved in the backup (moving to a new server).
#                The current .env is kept as .env.before-restore-<time>.
#   --yes        do not ask for confirmation.
#
# The database is REPLACED by the backup's content. The app is stopped during the
# restore and started again afterwards. Needs BACKUP_PASSPHRASE (from .env, or the
# environment when .env is not there yet on a new server).
# =============================================================================
set -euo pipefail
umask 077

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$APP_DIR"
COMPOSE="${COMPOSE:-docker compose}"

file="" with_env=false yes=false
for arg in "$@"; do
  case "$arg" in
    --with-env) with_env=true ;;
    --yes) yes=true ;;
    -*) echo "unknown option $arg" >&2; exit 2 ;;
    *) file="$arg" ;;
  esac
done
[ -n "$file" ] && [ -f "$file" ] || { echo "usage: $0 <backup.tar.gpg> [--with-env] [--yes]" >&2; exit 2; }

env_get() { [ -f .env ] && grep -E "^$1=" .env | tail -1 | cut -d= -f2- || true; }
PASSPHRASE="${BACKUP_PASSPHRASE:-$(env_get BACKUP_PASSPHRASE)}"
[ -n "$PASSPHRASE" ] || { echo "BACKUP_PASSPHRASE unknown: set it in .env or run with BACKUP_PASSPHRASE=… $0 …" >&2; exit 1; }

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
gpg --batch --quiet --pinentry-mode loopback --passphrase-fd 3 --decrypt "$file" 3<<<"$PASSPHRASE" | tar -C "$work" -xf - \
  || { echo "cannot decrypt: wrong passphrase or damaged file" >&2; exit 1; }
[ -s "$work/db.dump" ] || { echo "the backup has no database dump" >&2; exit 1; }

if [ "$yes" != true ]; then
  echo "This REPLACES the current DZPLAY database with: $file"
  read -r -p "Type RESTORE to continue: " answer </dev/tty
  [ "$answer" = "RESTORE" ] || { echo "cancelled"; exit 1; }
fi

if [ "$with_env" = true ]; then
  [ -s "$work/env" ] || { echo "this backup does not contain .env" >&2; exit 1; }
  [ -f .env ] && cp -p .env ".env.before-restore-$(date -u +%Y%m%dT%H%M%SZ)"
  install -m 600 "$work/env" .env
  echo ".env restored"
fi

$COMPOSE up -d db </dev/null
for _ in $(seq 1 30); do $COMPOSE exec -T db pg_isready -U dzplay -d dzplay >/dev/null 2>&1 </dev/null && break; sleep 2; done
$COMPOSE stop app </dev/null || true
# --clean --if-exists drops what the dump contains first; one transaction = all or nothing.
$COMPOSE exec -T db pg_restore -U dzplay -d dzplay --clean --if-exists --no-owner --no-privileges --single-transaction \
  < "$work/db.dump"
$COMPOSE up -d </dev/null
echo "restore ok: database replaced from $(basename "$file"); the app is starting again"
