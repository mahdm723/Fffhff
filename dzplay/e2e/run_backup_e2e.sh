#!/usr/bin/env bash
# Backup → wipe → restore round trip on a THROWAWAY Docker stack (own project name, own
# volumes, temporary .env). Never touches a real deployment.
#
#   e2e/run_backup_e2e.sh
#   STACK_EXTRA=dir e2e/run_backup_e2e.sh   copy extra files (e.g. a docker-compose.override.yml with a
#                                            prebuilt image) into the stack, for networks without apt/pip
#
# Checks: encrypted file (no plaintext), verification, rotation (BACKUP_KEEP=2), wrong
# passphrase refused, data + audit chain back after restore, .env restored with --with-env,
# the hardened app container (read-only root fs, no capabilities, no-new-privileges), and the V6
# clean-up of an old database (deploy/v6-cleanup.sh: encrypted export, then removal, idempotent).
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
PROJECT="dzplay-backup-e2e-$$"
export COMPOSE="docker compose -p $PROJECT"
cleanup() { (cd "$WORK/stack" && $COMPOSE down -v >/dev/null 2>&1) || true; rm -rf "$WORK"; }
trap cleanup EXIT

mkdir "$WORK/stack"
tar -C "$SRC" --exclude=.venv --exclude=.env --exclude=__pycache__ --exclude='e2e/shots*' -cf - . | tar -C "$WORK/stack" -xf -
if [ -n "${STACK_EXTRA:-}" ]; then cp -r "$STACK_EXTRA"/. "$WORK/stack/"; fi
cd "$WORK/stack"
cat > .env <<ENV
ENV=production
SECRET_KEY=$(openssl rand -hex 32)
POSTGRES_PASSWORD=$(openssl rand -hex 16)
DOMAIN=localhost
PUBLIC_URL=https://localhost
ADMIN_PATH=/panel-backup-e2e
BACKUP_PASSPHRASE=$(openssl rand -hex 24)
BACKUP_DIR=$WORK/backups
BACKUP_KEEP=2
ENV
chmod 600 .env

pass() { printf '  ✓ %s\n' "$*"; }
fail() { printf '  ✗ %s\n' "$*" >&2; exit 1; }
run() { $COMPOSE exec -T app python - "$1" < e2e/backup_roundtrip.py | tail -1; }

$COMPOSE up -d --build app >/dev/null 2>&1 </dev/null || fail "stack did not start"
for _ in $(seq 1 60); do
  $COMPOSE exec -T app python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')" >/dev/null 2>&1 && break
  sleep 2
done
app="$($COMPOSE ps -q app)"
[ "$(docker inspect -f '{{.HostConfig.ReadonlyRootfs}} {{.HostConfig.CapDrop}}' "$app")" = "true [ALL]" ] || fail "app container not hardened"
$COMPOSE exec -T app sh -c 'touch /app/x' 2>/dev/null && fail "app code is writable"
$COMPOSE exec -T app sh -c 'grep -q "NoNewPrivs:.1" /proc/1/status' || fail "no-new-privileges missing"
pass "app container: read-only root, no capabilities, no-new-privileges"

before="$(run seed)"
for _ in 1 2 3; do ./deploy/backup.sh >/dev/null; sleep 1; done
[ "$(ls "$WORK/backups" | wc -l)" -eq 2 ] || fail "rotation"
f="$(ls -1t "$WORK"/backups/*.gpg | head -1)"
[ "$(stat -c %a "$f")" = 600 ] || fail "backup file permissions"
strings "$f" | grep -q -e roundtrip -e SECRET_KEY && fail "plaintext inside the backup"
pass "3 backups → 2 kept, mode 600, encrypted (no plaintext)"

run wipe >/dev/null
BACKUP_PASSPHRASE=wrong-passphrase-123456 ./deploy/restore.sh "$f" --yes >/dev/null 2>&1 && fail "wrong passphrase accepted"
pass "wrong passphrase refused"
./deploy/restore.sh "$f" --yes >/dev/null 2>&1 || fail "restore failed"
for _ in $(seq 1 30); do
  $COMPOSE exec -T app python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')" >/dev/null 2>&1 && break
  sleep 2
done
after="$(run check)"
[ "$before" = "$after" ] || fail "data differs after restore: $before / $after"
pass "restored: $after"

cp .env "$WORK/env.orig"
echo "CHANGED=1" >> .env
./deploy/restore.sh "$f" --yes --with-env >/dev/null 2>&1
cmp -s .env "$WORK/env.orig" || fail ".env not restored"
pass ".env restored with --with-env (previous one kept aside)"
# V6: an old database (V5 tables + rows) → deploy/v6-cleanup.sh → encrypted export, then removed
legacy() { cat tests/legacy_v5_schema.py e2e/v6_legacy_seed.py | $COMPOSE exec -T app python - "$1" | tail -1; }
seeded="$(legacy seed)"
case "$seeded" in *"old_tables=['calls', 'monetization_applications', 'reel_assets'"*"payouts=1 boosted=1"*) ;; *) fail "legacy seed: $seeded" ;; esac
$COMPOSE exec -T app python -m app.admin_cli legacy-status >/dev/null 2>&1 </dev/null && fail "legacy-status missed the old data"
./deploy/v6-cleanup.sh > "$WORK/v6.log" 2>&1 || { cat "$WORK/v6.log" >&2; fail "v6-cleanup.sh failed"; }
grep -q "blue stars: 1" "$WORK/v6.log" || { cat "$WORK/v6.log" >&2; fail "stars-count missing"; }
v6f="$(ls -1 "$WORK"/backups/v6-legacy-*.json.gpg)"
[ "$(stat -c %a "$v6f")" = 600 ] || fail "export file permissions"
strings "$v6f" | grep -q -e ab12cd -e reels && fail "plaintext inside the export"
pp="$(grep '^BACKUP_PASSPHRASE=' .env | cut -d= -f2-)"
gpg --batch --quiet --pinentry-mode loopback --passphrase-fd 3 --decrypt "$v6f" 3<<<"$pp" | grep -q '"short_id":"ab12cd"' \
  || fail "export does not decrypt to the old Reel"
after_v6="$(legacy check)"
[ "$after_v6" = "old_tables=[] payouts=0 boosted=0 star_kept=1 idea=1" ] || fail "after clean-up: $after_v6"
pass "v6-cleanup.sh: encrypted export (600, decrypts), old tables/rows removed, stars + ideas kept"
./deploy/v6-cleanup.sh 2>&1 | grep -q "Already done" || fail "second run not idempotent"
[ "$(ls -1 "$WORK"/backups/v6-legacy-*.json.gpg | wc -l)" -eq 1 ] || fail "second run exported again"
pass "second run: Already done, nothing changed"
echo "BACKUP E2E PASSED"
