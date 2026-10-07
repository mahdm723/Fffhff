#!/usr/bin/env bash
# =============================================================================
# DZPLAY — encrypted database backup.
#
#   sudo /opt/dzplay/dzplay/deploy/backup.sh          (the installer runs it daily from cron)
#
# What it does:
#   1. pg_dump of the whole database (custom format, compressed) from the db container,
#      plus a copy of .env (SECRET_KEY is needed to keep admin 2FA, sessions and hashes valid
#      after a restore; set BACKUP_INCLUDE_ENV=false to leave it out);
#   2. encrypts both with GnuPG (AES-256, integrity-protected) using BACKUP_PASSPHRASE;
#   3. checks the file can be decrypted and read back (pg_restore --list);
#   4. keeps the newest BACKUP_KEEP files in BACKUP_DIR and deletes older ones;
#   5. optionally copies it off the server with rclone (BACKUP_RCLONE_REMOTE).
#
# Settings (in .env): BACKUP_PASSPHRASE (required), BACKUP_DIR (/var/backups/dzplay),
# BACKUP_KEEP (14), BACKUP_INCLUDE_ENV (true), BACKUP_RCLONE_REMOTE (empty = off).
# The media cache is not backed up: the original pictures stay in Telegram and are re-downloaded.
# Restore: deploy/restore.sh <file>
# =============================================================================
set -euo pipefail
umask 077

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$APP_DIR"
[ -f .env ] || { echo "No .env in $APP_DIR" >&2; exit 1; }

# Read a value from .env without executing it (no `source`).
env_get() { grep -E "^$1=" .env | tail -1 | cut -d= -f2- || true; }

PASSPHRASE="${BACKUP_PASSPHRASE:-$(env_get BACKUP_PASSPHRASE)}"
DIR="${BACKUP_DIR:-$(env_get BACKUP_DIR)}"; DIR="${DIR:-/var/backups/dzplay}"
KEEP="${BACKUP_KEEP:-$(env_get BACKUP_KEEP)}"; KEEP="${KEEP:-14}"
WITH_ENV="${BACKUP_INCLUDE_ENV:-$(env_get BACKUP_INCLUDE_ENV)}"; WITH_ENV="${WITH_ENV:-true}"
REMOTE="${BACKUP_RCLONE_REMOTE:-$(env_get BACKUP_RCLONE_REMOTE)}"
COMPOSE="${COMPOSE:-docker compose}"

[ -n "$PASSPHRASE" ] || { echo "BACKUP_PASSPHRASE is empty: set it in .env (openssl rand -hex 32)" >&2; exit 1; }
[ "${#PASSPHRASE}" -ge 16 ] || { echo "BACKUP_PASSPHRASE is too short (16+ characters)" >&2; exit 1; }
[[ "$KEEP" =~ ^[0-9]+$ ]] && [ "$KEEP" -ge 1 ] || { echo "BACKUP_KEEP must be a positive number" >&2; exit 1; }
command -v gpg >/dev/null || { echo "gpg is missing: apt-get install -y gnupg" >&2; exit 1; }

mkdir -p "$DIR"
chmod 700 "$DIR"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
out="$DIR/dzplay-$stamp.tar.gpg"

$COMPOSE exec -T db pg_dump -U dzplay -d dzplay --format=custom --no-owner --no-privileges > "$work/db.dump" </dev/null
[ -s "$work/db.dump" ] || { echo "pg_dump produced nothing" >&2; exit 1; }
files=(db.dump)
if [ "$WITH_ENV" = "true" ]; then cp .env "$work/env"; files+=(env); fi

gpg_pass() { gpg --batch --yes --quiet --pinentry-mode loopback --passphrase-fd 3 "$@" 3<<<"$PASSPHRASE"; }
tar -C "$work" -cf - "${files[@]}" | gpg_pass --symmetric --cipher-algo AES256 --s2k-digest-algo SHA512 \
  --s2k-count 65011712 --output "$out.part"
mv "$out.part" "$out"

# Verify: decrypt, unpack and let pg_restore read the table of contents.
mkdir "$work/check"
gpg_pass --decrypt "$out" | tar -C "$work/check" -xf -
$COMPOSE exec -T db pg_restore --list < "$work/check/db.dump" > /dev/null
size="$(du -h "$out" | cut -f1)"

# Rotation: keep the newest $KEEP backups.
ls -1t "$DIR"/dzplay-*.tar.gpg 2>/dev/null | tail -n +"$((KEEP + 1))" | xargs -r rm -f --

if [ -n "$REMOTE" ]; then
  if command -v rclone >/dev/null; then
    rclone copy "$out" "$REMOTE" && echo "copied off-server to $REMOTE"
  else
    echo "BACKUP_RCLONE_REMOTE is set but rclone is not installed" >&2
  fi
fi
echo "backup ok: $out ($size, verified; keeping $KEEP)"
