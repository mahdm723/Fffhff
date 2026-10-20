#!/usr/bin/env bash
# V6 phase 21 (phase 2, the night window): the production site shows «صيانة مؤقتة» while it is stopped on purpose.
#   sudo loadtest/phase2/maintenance.sh on [HOURS]   (default 6: the flag expires by itself, never forgotten "on")
#   sudo loadtest/phase2/maintenance.sh off
#   sudo loadtest/phase2/maintenance.sh status
# "on": Caddy (production, still running) answers every request with a 503 page (JSON for /api/*), Retry-After;
#       deploy/monitor.sh watches only the disk until the window ends. The app itself is not touched here.
# The file is deploy/caddy/maintenance-<TLS_MODE>.caddy: the staging Caddy (TLS_MODE=staging) never matches it.
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$APP_DIR"
env_get() { [ -f .env ] && grep -E "^$1=" .env | tail -1 | cut -d= -f2- | sed 's/[[:space:]]*#.*$//; s/^"//; s/"$//' || true; }
MODE="$(env_get TLS_MODE)"; MODE="${MODE:-auto}"
NAME="$(env_get APP_NAME)"; NAME="${NAME:-DALTA.BIT}"
FILE="deploy/caddy/maintenance-$MODE.caddy"
STATE="${MONITOR_STATE_DIR:-/var/lib/dzplay-monitor}"
reload() { docker compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile >/dev/null; }

case "${1:-status}" in
  on)
    hours="${2:-6}"
    [[ "$hours" =~ ^[0-9]+$ ]] && [ "$hours" -ge 1 ] && [ "$hours" -le 12 ] || { echo "HOURS: 1-12" >&2; exit 2; }
    name="$(printf '%s' "$NAME" | tr -cd 'A-Za-z0-9 ._-')"
    cat > "$FILE" <<CADDY
# written by loadtest/phase2/maintenance.sh on — removed by "maintenance.sh off"
route {
	header Cache-Control "no-store"
	header Retry-After "3600"
	@api path /api/* /healthz*
	handle @api {
		header Content-Type "application/json; charset=utf-8"
		respond \`{"error":{"code":"maintenance","message":"صيانة مؤقتة، نعود قريبًا."}}\` 503
	}
	handle {
		header Content-Type "text/html; charset=utf-8"
		respond <<HTML
			<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
			<title>$name — صيانة مؤقتة</title><style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0b0d12;color:#f2f2f2;font:17px/1.7 system-ui,sans-serif;text-align:center;padding:24px}b{font-size:22px}p{margin:6px 0;color:#c8c8c8}</style></head>
			<body><main><b>$name</b><p>صيانة مؤقتة لتحسين الأداء، نعود خلال ساعات قليلة.</p><p dir="ltr">Maintenance en cours — Maintenance in progress.</p></main></body></html>
			HTML 503
	}
}
CADDY
    reload
    mkdir -p "$STATE" && chmod 700 "$STATE"
    echo $(( $(date +%s) + hours * 3600 )) > "$STATE/.maintenance"
    echo "✓ maintenance page ON (monitor paused for ${hours} h, disk still watched)"
    ;;
  off)
    rm -f "$FILE" "$STATE/.maintenance"
    reload
    echo "✓ maintenance page OFF"
    ;;
  status)
    if [ -f "$FILE" ]; then echo "ON ($FILE)"; else echo "OFF"; fi
    ;;
  *) echo "usage: $0 on [HOURS] | off | status" >&2; exit 2 ;;
esac
