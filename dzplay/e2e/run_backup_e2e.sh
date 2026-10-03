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
# and the hardened app container (read-only root fs, no capabilities, no-new-privileges).
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
echo "BACKUP E2E PASSED"
