#!/usr/bin/env bash
# V6 phase 21 (phase 2): EMERGENCY STOP — at any moment of the night window, brings production back.
#   sudo /opt/dzplay/dzplay/loadtest/phase2/abort.sh
# In order (each step runs even if an earlier one failed; safe to run twice):
#   1. stops the load generator / WebSocket flood / collector containers
#   2. removes the staging stack WITH its volumes (all staging data) and the firewall rule for a remote generator
#   3. starts production (docker compose up -d — the same images, .env and volumes as before)
#   4. waits for /healthz, then removes the maintenance page
#   5. prints the state of the production containers
set -uo pipefail
APP_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$APP_DIR"
say() { printf '\n▶ %s\n' "$*"; }

say "1/5 stopping the load generator"
docker rm -f dzstaging-k6 dzstaging-wsflood dzstaging-collector dzstaging-k6sink >/dev/null 2>&1 || true
pkill -f 'loadtest/run.sh' 2>/dev/null || true

say "2/5 removing staging (containers + volumes)"
docker compose -p dzstaging -f loadtest/staging/docker-compose.yml down --volumes --remove-orphans 2>/dev/null || true
rm -f loadtest/out/staging.env loadtest/out/staging-prod.env loadtest/out/vu_users.csv loadtest/out/meta.json
"$APP_DIR/loadtest/phase2/firewall.sh" off >/dev/null 2>&1 || true

say "3/5 starting production"
docker compose up -d
say "4/5 waiting for /healthz"
ok=0
for _ in $(seq 1 60); do
    if docker compose exec -T app python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)" >/dev/null 2>&1; then
        ok=1; break
    fi
    sleep 3
done
if [ "$ok" = 1 ]; then
    "$APP_DIR/loadtest/phase2/maintenance.sh" off || true
    echo "✓ production answers /healthz — maintenance page removed"
else
    echo "✗ production does not answer /healthz after 3 minutes: the maintenance page stays ON."
    echo "  look at: docker compose logs --tail 100 app   |   restore a backup: deploy/restore.sh <file>"
fi
say "5/5 production containers"
docker compose ps
exit $(( 1 - ok ))
