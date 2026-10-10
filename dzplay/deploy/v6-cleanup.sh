#!/usr/bin/env bash
# =============================================================================
# DZPLAY V6 — save, then remove, the data of the features V6 removed
# (Reels, the creator studio + earnings, voice/video calls, the "boost" of likes).
#
#   sudo /opt/dzplay/dzplay/deploy/v6-cleanup.sh          phase 1: Reels, studio, earnings, calls, boost
#   sudo /opt/dzplay/dzplay/deploy/v6-cleanup.sh anon     phase 1b: the old anonymous chats (read-only), allowed
#                                                         LEGACY_ANON_RETENTION_DAYS after the update; before that
#                                                         it only prints the date. The installer runs it daily (cron).
#
# 1. a normal encrypted full backup first (deploy/backup.sh);
# 2. `admin_cli export-legacy` → encrypted with BACKUP_PASSPHRASE (AES-256)
#    → BACKUP_DIR/v6-legacy-<time>.json.gpg (root only, never rotated away);
# 3. decrypts it again and checks it is byte-for-byte the export (SHA-256);
# 4. `admin_cli drop-legacy --sha <sha>`: removes exactly what was saved, in one transaction
#    (refused if anything changed since the export). Users, blue stars, ideas, chats stay.
#
# Safe to run again: when there is nothing left it says so and changes nothing.
# Read the saved file later:  gpg --decrypt FILE > legacy.json   (asks the backup passphrase)
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
env_get() { grep -E "^$1=" .env | tail -1 | cut -d= -f2- || true; }
PASSPHRASE="${BACKUP_PASSPHRASE:-$(env_get BACKUP_PASSPHRASE)}"
DIR="${BACKUP_DIR:-$(env_get BACKUP_DIR)}"; DIR="${DIR:-/var/backups/dzplay}"
[ "${#PASSPHRASE}" -ge 16 ] || die "BACKUP_PASSPHRASE is missing or too short in .env"
command -v gpg >/dev/null || die "gpg is missing: apt-get install -y gnupg"
cli() { $COMPOSE exec -T app python -m app.admin_cli "$@" </dev/null; }

STAGE="${1:-legacy}"
case "$STAGE" in
  legacy) FLAGS=(); NAME="v6-legacy"; WHAT="Old Reels / calls / earnings / boost data" ;;
  anon)   FLAGS=(--anon); NAME="v6-anon"; WHAT="Old anonymous chats" ;;
  *) die "Unknown stage '$STAGE' (use: anon, or nothing)" ;;
esac

set +e
status="$(cli legacy-status "${FLAGS[@]}")"; code=$?
set -e
case "$code" in
  0) ok "$status"; exit 0 ;;
  2) ok "Not yet: $status"; exit 0 ;;   # anon: before LEGACY_ANON_RETENTION_DAYS have passed
  1) echo "$status" ;;
  *) echo "$status" >&2; die "Could not read the status (is the app running?)" ;;
esac

"$APP_DIR/deploy/backup.sh" || die "The full backup failed: nothing was removed."
ok "Full encrypted backup taken"

mkdir -p "$DIR"
chmod 700 "$DIR"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
out="$DIR/$NAME-$(date -u +%Y%m%dT%H%M%SZ).json.gpg"
gpg_pass() { gpg --batch --yes --quiet --pinentry-mode loopback --passphrase-fd 3 "$@" 3<<<"$PASSPHRASE"; }

# The export never touches the disk unencrypted.
cli export-legacy "${FLAGS[@]}" 2>"$work/err" | gpg_pass --symmetric --cipher-algo AES256 --s2k-digest-algo SHA512 \
  --s2k-count 65011712 --output "$out.part" || { cat "$work/err" >&2; die "The export failed: nothing was removed."; }
sha="$(grep -o 'sha256=[0-9a-f]\{64\}' "$work/err" | cut -d= -f2 || true)"
[ -n "$sha" ] || { cat "$work/err" >&2; die "The export did not report its SHA-256: nothing was removed."; }
got="$(gpg_pass --decrypt "$out.part" | sha256sum | cut -d' ' -f1)"
[ "$got" = "$sha" ] || die "The encrypted copy does not match the export: nothing was removed."
mv "$out.part" "$out"
chmod 600 "$out"
ok "Saved (encrypted, verified): $out"
ok "Rows: $(grep -o 'rows=.*' "$work/err" | cut -d= -f2-)"

cli drop-legacy "${FLAGS[@]}" --sha "$sha" || die "Removal refused (see above). The saved copy stays in $out"
ok "$WHAT removed"
[ "$STAGE" = legacy ] && cli stars-count
exit 0
