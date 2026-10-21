#!/usr/bin/env bash
# V6 phase 21 (phase 2, inside the night window): staging on the production server, with production's capacity settings
# and panel limits — never its secrets, its data or its addresses.
#   sudo loadtest/phase2/staging-up.sh                                       the generator runs on this server
#   sudo GEN_IP=<generator> SERVER_IP=<this server> loadtest/phase2/staging-up.sh    a separate generator (option 1)
# From production: WEB_CONCURRENCY / DB_POOL_SIZE / DB_MAX_OVERFLOW / THREADPOOL_SIZE / PASSWORD_HASH_* /
# WS_SESSION_RECHECK_SECONDS (.env) and the panel tunables (tun.*, exported by night.sh to loadtest/out/tunables.tsv).
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$APP_DIR"
env_get() { [ -f .env ] && grep -E "^$1=" .env | tail -1 | cut -d= -f2- | sed 's/[[:space:]]*#.*$//; s/^"//; s/"$//' || true; }
export APP_ENV=staging
for k in WEB_CONCURRENCY DB_POOL_SIZE DB_MAX_OVERFLOW THREADPOOL_SIZE PASSWORD_HASH_CONCURRENCY PASSWORD_HASH_QUEUE \
         PASSWORD_HASH_WAIT_SECONDS AUTH_MAX_IN_FLIGHT WS_SESSION_RECHECK_SECONDS; do
    v="$(env_get "$k")"
    if [ -n "$v" ]; then export "$k=$v"; echo "  $k=$v (production)"; fi
done
args=()
if [ -n "${GEN_IP:-}" ]; then
    [ -n "${SERVER_IP:-}" ] || { echo "✗ a separate generator needs SERVER_IP (this server's address)"; exit 2; }
    "$APP_DIR/loadtest/phase2/firewall.sh" on "$GEN_IP"  # before anything is published
    export STAGING_BIND="$SERVER_IP"
    args=(--remote)
else
    export STAGING_BIND=127.0.0.1
fi
"$APP_DIR/loadtest/staging/up.sh" "${args[@]}"

stg() { docker compose -p dzstaging -f "$APP_DIR/loadtest/staging/docker-compose.yml" "$@"; }
if [ -s loadtest/out/tunables.tsv ]; then
    stg exec -T db psql -q -U dzplay -d dzplay_staging -v ON_ERROR_STOP=1 \
        -c "CREATE TEMP TABLE t (key text, value text)" -c "\\copy t FROM STDIN" \
        -c "INSERT INTO app_settings (key, value, updated_at, updated_by) SELECT key, value, now(), 'loadtest' FROM t
            WHERE key LIKE 'tun.%' ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value" \
        -c "INSERT INTO app_settings (key, value, updated_at, updated_by) VALUES ('tun.REGISTRATION_CODE_AUTO', 'true', now(),
            'loadtest') ON CONFLICT (key) DO UPDATE SET value = 'true'" < loadtest/out/tunables.tsv
    # (the sign-up scenario reads its code in the mock mailbox: the code goes by e-mail, as with the default setting)
    stg restart app >/dev/null
    for _ in $(seq 1 60); do
        stg exec -T app python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')" 2>/dev/null && break
        sleep 2
    done
    echo "✓ production's panel limits applied to staging ($(wc -l < loadtest/out/tunables.tsv) values)"
fi
echo "✓ staging is up on ${STAGING_BIND}:8443"
