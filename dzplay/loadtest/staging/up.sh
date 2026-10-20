#!/usr/bin/env bash
# V6 phase 21: start the STAGING stack (project dzstaging, port 8443) — never production's.
#   loadtest/staging/up.sh            on the server (phase 2): the production image, built from this checkout
#   loadtest/staging/up.sh --local    here: pinned to 2 CPUs / ~4 GB (local-2cpu.yml)
#   loadtest/staging/up.sh --remote   phase 2 with a separate generator: the mock mailbox published on STAGING_BIND
# Environment (optional): WEB_CONCURRENCY, PG_MAX_CONNECTIONS, PG_SHARED_BUFFERS, PG_WORK_MEM, STAGING_IMAGE,
# STAGING_BIND (default 127.0.0.1; phase 2 with a separate load generator: the server's address + a firewall rule).
set -euo pipefail
cd "$(dirname "$0")"
OUT=../out
FILES=(-f docker-compose.yml)
for arg in "$@"; do
    case "$arg" in
        --local) FILES+=(-f local-2cpu.yml) ;;
        --remote) FILES+=(-f remote-generator.yml) ;;
        *) echo "usage: $0 [--local] [--remote]"; exit 2 ;;
    esac
done
export APP_ENV=staging

mkdir -p "$OUT"
if [ ! -s "$OUT/staging.env" ]; then  # random secrets for this staging only (outside git, deleted by down.sh)
    umask 077
    python3 - > "$OUT/staging.env" <<'PY'
import secrets
print("SECRET_KEY=" + secrets.token_urlsafe(48))
print("LOADTEST_TOKEN=" + secrets.token_urlsafe(40))
print("TELEGRAM_BOT_TOKEN=900000001:" + secrets.token_urlsafe(26))  # the mock accepts any token
print("TELEGRAM_WEBHOOK_SECRET=" + secrets.token_urlsafe(24))
PY
fi
chown 10001 "$OUT" 2>/dev/null || true  # the seed (uid 10001 in the app container) writes vu_users.csv here

compose() { docker compose -p dzstaging "${FILES[@]}" "$@"; }
if [ -z "${STAGING_IMAGE:-}" ]; then compose build app; fi
compose up -d
echo "waiting for staging…"
for _ in $(seq 1 60); do
    if compose exec -T app python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8000/healthz')" 2>/dev/null; then
        break
    fi
    sleep 2
done
TOKEN=$(grep '^LOADTEST_TOKEN=' "$OUT/staging.env" | cut -d= -f2-)
compose exec -T -e APP_ENV=staging -e LOADTEST_TOKEN="$TOKEN" mocks python /app/loadtest/lib/safety.py target https://staging.test:8443
