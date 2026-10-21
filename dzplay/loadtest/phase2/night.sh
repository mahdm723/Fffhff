#!/usr/bin/env bash
# V6 phase 21 (phase 2): the night maintenance window, in order, with times. Production comes back by itself on any
# failure (and loadtest/phase2/abort.sh does the same by hand, at any moment, from another terminal).
#   sudo loadtest/phase2/night.sh [--quick|--full] [A B C D E F G]           the generator on this server (option 2)
#   sudo GEN_HOST=ssh://root@<gen> GEN_IP=<gen> SERVER_IP=<this server> loadtest/phase2/night.sh …   (option 1)
# Approximate durations: steps 0–5 ≈ 20–30 min; scenarios --quick ≈ 1 h 30, --full ≈ 4 h; steps 7–9 ≈ 5 min.
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$APP_DIR"
PACE=--quick SC=()
for a in "$@"; do
    case "$a" in --quick|--full) PACE=$a ;; [A-Ga-g]) SC+=("${a^^}") ;; *) echo "unknown argument: $a"; exit 2 ;; esac
done
[ ${#SC[@]} -gt 0 ] || SC=(A B C D E F G)
mkdir -p /var/log/dzplay-loadtest
LOG=/var/log/dzplay-loadtest/night-$(date +%Y%m%d-%H%M).log
exec > >(tee -a "$LOG") 2>&1
T0=$(date +%s)
step() { printf '\n[%s +%dmin] ▶ %s\n' "$(date '+%H:%M')" $(( ($(date +%s) - T0) / 60 )) "$*"; }
fail() {
    trap - ERR INT TERM
    echo "✗ $1 — production is being restored (abort.sh)"
    "$APP_DIR/loadtest/phase2/abort.sh" || true
    exit 1
}
trap 'fail "a step failed (line $LINENO)"' ERR
trap 'fail "interrupted"' INT TERM
stg() { docker compose -p dzstaging -f "$APP_DIR/loadtest/staging/docker-compose.yml" "$@"; }

step "0/9 checks and preparation (production still running)"
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 2; }
avail=$(df --output=avail -k / | tail -1)
[ "$avail" -gt $(( 10 * 1024 * 1024 )) ] || { echo "✗ less than 10 GB free"; exit 2; }
docker compose ps --status running --services | grep -qx app || { echo "✗ production is not running"; exit 2; }
if docker ps --format '{{.Names}}' | grep -q '^dzstaging-'; then echo "✗ a staging stack already runs"; exit 2; fi
stg build app  # the staging image, built while production still serves (light)
docker pull -q grafana/k6:1.3.0 >/dev/null || true

step "1/9 encrypted backup + its check (≈ 2–5 min)"
"$APP_DIR/deploy/backup.sh"

step "2/9 production's panel limits (tun.*) → loadtest/out/tunables.tsv"
mkdir -p loadtest/out
docker compose exec -T db psql -U dzplay -d dzplay -c "\\copy (SELECT key, value FROM app_settings WHERE key LIKE 'tun.%') TO STDOUT" \
    > loadtest/out/tunables.tsv

step "3/9 maintenance page on; production stopped (Caddy stays to show the page)"
"$APP_DIR/loadtest/phase2/maintenance.sh" on 8
docker compose stop app media-worker
docker compose stop db redis

step "4/9 staging up with production's settings"
"$APP_DIR/loadtest/phase2/staging-up.sh"

step "5/9 50,000 users seeded (≈ 5–10 min)"
stg exec -T -e APP_ENV=staging app python /app/loadtest/seed/seed.py

step "6/9 scenarios ${SC[*]} ($PACE) — stops by itself at 85 % disk or an unexpected error"
APP_ENV=staging "$APP_DIR/loadtest/run.sh" "$PACE" --label server "${SC[@]}" || echo "(the run stopped early: see above)"

step "7/9 results kept outside the repository"
RES=$(ls -td "$APP_DIR"/loadtest/results/*-server | head -1)
mkdir -p /var/backups/dzplay-loadtest && cp -r "$RES" /var/backups/dzplay-loadtest/
echo "  $RES → /var/backups/dzplay-loadtest/"

step "8/9 staging removed (containers, volumes, generated secrets, firewall rule)"
"$APP_DIR/loadtest/staging/down.sh"
"$APP_DIR/loadtest/phase2/firewall.sh" off >/dev/null 2>&1 || true

step "9/9 production back, checked, maintenance page off"
docker compose up -d
for _ in $(seq 1 60); do
    docker compose exec -T app python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)" \
        >/dev/null 2>&1 && break
    sleep 3
done
"$APP_DIR/loadtest/phase2/check-production.sh" --internal
"$APP_DIR/loadtest/phase2/maintenance.sh" off
"$APP_DIR/loadtest/phase2/check-production.sh"
trap - ERR INT TERM
echo
echo "✓ done in $(( ($(date +%s) - T0) / 60 )) min — log: $LOG — results: /var/backups/dzplay-loadtest/$(basename "$RES")"
echo "  last check by a person: log in from the phone."
