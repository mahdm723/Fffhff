#!/usr/bin/env bash
# V6 phase 20: move the app to its own domain behind Cloudflare (docs/DOMAIN_MIGRATION.md has the whole checklist).
#
#   sudo deploy/domain-switch.sh example.com    step 1 — after Cloudflare is set up and the Origin Certificate is in
#                                                deploy/origin/ (cert.pem + key.pem): the app moves to the domain; the
#                                                old address keeps serving the installed Android app and its update,
#                                                everything else gets a 301 to the domain
#   sudo deploy/domain-switch.sh check          the checks again (any time)
#   sudo deploy/domain-switch.sh finalize       last step — once phones have the new APK: the old address stops and
#                                                ports 80/443 accept Cloudflare only
#   sudo deploy/domain-switch.sh rollback       back to the old address (the .env saved by step 1)
#
# Every step keeps the data; .env is saved first (.env.before-domain, once).
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$APP_DIR"
COMPOSE="docker compose"
ORIGIN="deploy/origin"

ok()   { printf '    \033[1;32m✓\033[0m %s\n' "$*"; }
bad()  { printf '    \033[1;31m✗\033[0m %s\n' "$*"; FAILED=1; }
warn() { printf '    \033[1;33m!\033[0m %s\n' "$*"; }
say()  { printf '\n\033[1;35m==>\033[0m %s\n' "$*"; }
die()  { printf '\n\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }
FAILED=0

env_get() { grep -E "^$1=" .env | tail -1 | cut -d= -f2- | sed 's/[[:space:]]*#.*$//; s/^"//; s/"$//' || true; }
set_env() { if grep -q "^$1=" .env; then sed -i "s|^$1=.*|$1=$2|" .env; else echo "$1=$2" >> .env; fi; }

valid_domain() { [[ "$1" =~ ^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,62}[a-z0-9]$ ]] && [ ${#1} -le 253 ]; }

check_origin_cert() {  # the files, the key matches, the domain is covered, not expiring
  local d="$1"
  [ -s "$ORIGIN/cert.pem" ] && [ -s "$ORIGIN/key.pem" ] \
    || die "Put the Cloudflare Origin Certificate in $APP_DIR/$ORIGIN/cert.pem and its private key in key.pem (docs/DOMAIN_MIGRATION.md, step 1)."
  chmod 700 "$ORIGIN"; chmod 600 "$ORIGIN/key.pem"; chmod 644 "$ORIGIN/cert.pem"
  openssl x509 -in "$ORIGIN/cert.pem" -noout >/dev/null 2>&1 || die "$ORIGIN/cert.pem is not a certificate (PEM)."
  [ "$(openssl x509 -in "$ORIGIN/cert.pem" -noout -pubkey 2>/dev/null)" = "$(openssl pkey -in "$ORIGIN/key.pem" -pubout 2>/dev/null)" ] \
    || die "$ORIGIN/key.pem is not the key of $ORIGIN/cert.pem."
  openssl x509 -in "$ORIGIN/cert.pem" -noout -checkhost "$d" 2>/dev/null | grep -q "does match" \
    || die "The certificate does not cover $d (create it for $d and *.$d)."
  openssl x509 -in "$ORIGIN/cert.pem" -noout -checkend 2592000 >/dev/null || die "The certificate expires within 30 days."
  ok "Origin certificate: covers $d, key matches, valid until $(openssl x509 -in "$ORIGIN/cert.pem" -noout -enddate | cut -d= -f2)"
}

install_timer() {  # Cloudflare's ranges at boot and daily (Caddy's trusted list, and the firewall once finalized)
  cat > /etc/systemd/system/dzplay-cloudflare-ips.service <<UNIT
[Unit]
Description=DALTA.BIT: Cloudflare address ranges (Caddy trusted proxies + firewall)
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=$APP_DIR/deploy/cloudflare-ips.sh
UNIT
  cat > /etc/systemd/system/dzplay-cloudflare-ips.timer <<'UNIT'
[Unit]
Description=DALTA.BIT: refresh Cloudflare address ranges

[Timer]
OnBootSec=90s
OnCalendar=daily
RandomizedDelaySec=1h
Persistent=true

[Install]
WantedBy=timers.target
UNIT
  systemctl daemon-reload
  systemctl enable --now dzplay-cloudflare-ips.timer >/dev/null 2>&1
  ok "Cloudflare ranges refreshed at boot and daily (systemd timer dzplay-cloudflare-ips)"
}

wait_healthy() {
  local d="$1"
  for _ in $(seq 1 60); do
    curl -fsSk --max-time 5 --resolve "$d:443:127.0.0.1" "https://$d/healthz" -o /dev/null 2>/dev/null && return 0
    sleep 2
  done
  return 1
}

json_ip() { sed -n 's/.*"ip":"\([^"]*\)".*/\1/p'; }

checks() {
  local d old public cf_ip seen forged hdr
  d="$(env_get APP_DOMAIN)"; old="$(env_get LEGACY_HOSTS)"
  [ -n "$d" ] || die "APP_DOMAIN is not set: run step 1 first (deploy/domain-switch.sh example.com)."
  say "Checks for $d"
  # 1. this server, directly: the origin certificate, no Server header
  wait_healthy "$d" && ok "The app answers on https://$d (on this server)" || bad "The app does not answer on this server: docker compose logs caddy app"
  if echo | openssl s_client -connect 127.0.0.1:443 -servername "$d" 2>/dev/null | openssl x509 -noout -issuer 2>/dev/null | grep -qi cloudflare; then
    ok "TLS on the server: the Cloudflare Origin Certificate"
  else
    bad "TLS on the server: not the Origin Certificate (TLS_MODE=$(env_get TLS_MODE))"
  fi
  hdr="$(curl -sSk -D - -o /dev/null --max-time 10 --resolve "$d:443:127.0.0.1" "https://$d/healthz" 2>/dev/null | tr -d '\r' || true)"
  grep -qiE '^(server|via):' <<< "$hdr" && bad "A Server/Via header is still sent" || ok "No Server/Via header"
  # 2. a forged CF-Connecting-IP from outside Cloudflare is ignored
  forged="$(curl -sSk --max-time 10 --resolve "$d:443:127.0.0.1" -H 'CF-Connecting-IP: 203.0.113.77' "https://$d/healthz/ip" 2>/dev/null | json_ip || true)"
  [ -n "$forged" ] && [ "$forged" != "203.0.113.77" ] && ok "CF-Connecting-IP from a non-Cloudflare address is ignored" \
    || bad "CF-Connecting-IP was trusted from a non-Cloudflare address ($forged)"
  # 3. through Cloudflare: proxied, and the app sees the real client
  public="$(getent ahosts "$d" 2>/dev/null | awk '{print $1}' | sort -u | tr '\n' ' ' || true)"
  hdr="$(curl -sS -D - -o /dev/null --max-time 15 "https://$d/healthz" 2>/dev/null | tr -d '\r' || true)"
  if grep -qi '^cf-ray:' <<< "$hdr"; then
    ok "Through Cloudflare: https://$d answers (DNS: ${public:-?}— Cloudflare's addresses, not this server's)"
    cf_ip="$(curl -sS --max-time 15 "https://$d/cdn-cgi/trace" 2>/dev/null | sed -n 's/^ip=//p' || true)"
    seen="$(curl -sS --max-time 15 "https://$d/healthz/ip" 2>/dev/null | json_ip || true)"
    [ -n "$seen" ] && [ "$seen" = "$cf_ip" ] && ok "The app sees the real client address through Cloudflare" \
      || bad "The app sees $seen, Cloudflare saw $cf_ip (CF-Connecting-IP chain)"
    curl -sS --max-time 15 -o /dev/null -w '%{http_code}' -H 'Connection: Upgrade' -H 'Upgrade: websocket' \
      -H 'Sec-WebSocket-Version: 13' -H 'Sec-WebSocket-Key: ZGFsdGFiaXRjaGVja3dzMQ==' "https://$d/api/ws" 2>/dev/null \
      | grep -qE '^(101|401|403)$' && ok "WebSocket upgrade reaches the app through Cloudflare" \
      || warn "WebSocket through Cloudflare: check «Network ← WebSockets» is on in Cloudflare"
  else
    bad "https://$d does not go through Cloudflare yet (DNS: ${public:-none}). Check the A record is «Proxied» (orange cloud)."
  fi
  # 4. the old address during the move
  if [ -n "$old" ]; then
    local code loc
    code="$(curl -sS --max-time 15 -o /dev/null -w '%{http_code} %{redirect_url}' "https://$old/" 2>/dev/null || true)"
    [[ "$code" == "301 https://$d/" ]] && ok "https://$old/ → 301 https://$d/" || bad "https://$old/ answers «$code» instead of a 301 to https://$d/"
    code="$(curl -sS --max-time 15 -o /dev/null -w '%{http_code}' "https://$old/download" 2>/dev/null || true)"
    [ "$code" = "200" ] && ok "https://$old/download still served (the installed app updates from it)" || bad "https://$old/download answers $code"
  fi
  # 5. the bot
  if [ -n "$(env_get TELEGRAM_BOT_TOKEN)" ]; then
    $COMPOSE exec -T app python -m app.admin_cli set-webhook "https://$d" </dev/null >/dev/null 2>&1 \
      && ok "Telegram webhook: https://$d/api/telegram/webhook" || bad "Telegram webhook not set (admin_cli set-webhook https://$d)"
  fi
  "$APP_DIR/deploy/cloudflare-ips.sh" status | sed 's/^/    · /'
  [ "$FAILED" = 0 ] && say "All checks passed" || say "Some checks failed (above)"
}

switch() {
  local d="$1" old
  valid_domain "$d" || die "«$d» is not a domain name (example: dalta.example)."
  [ -f .env ] || die "No .env here: install the app first."
  say "Moving to $d"
  check_origin_cert "$d"
  [ -f .env.before-domain ] || { cp -p .env .env.before-domain; chmod 600 .env.before-domain; ok "Saved .env as .env.before-domain"; }
  old="$(env_get DOMAIN)"
  set_env APP_DOMAIN "$d"
  set_env DOMAIN "$d"
  set_env PUBLIC_URL "https://$d"
  set_env TLS_MODE cloudflare
  grep -q '^CF_FIREWALL=' .env || set_env CF_FIREWALL off
  rm -f deploy/caddy/legacy-*.caddy
  if [ -n "$old" ] && [ "$old" != "$d" ] && [ "$old" != ":80" ] && [ -z "$(env_get LEGACY_HOSTS)" ]; then
    set_env LEGACY_HOSTS "$old"
  fi
  local legacy; legacy="$(env_get LEGACY_HOSTS)"
  if [ -n "$legacy" ]; then  # the old name keeps its own (Let's Encrypt) certificate until "finalize"
    printf '# Written by deploy/domain-switch.sh: the old address during the move (removed by "finalize")\n%s {\n\timport app\n}\n' \
      "$legacy" > deploy/caddy/legacy-old.caddy
    ok "The old address $legacy: the app and its update stay; everything else → 301 https://$d"
  fi
  chmod 600 .env
  "$APP_DIR/deploy/cloudflare-ips.sh" >/dev/null
  ok "Cloudflare ranges written for Caddy"
  $COMPOSE up -d --force-recreate caddy app </dev/null >/dev/null
  ok "Caddy (TLS_MODE=cloudflare) and the app restarted"
  install_timer
  checks
  cat <<NEXT

Next (docs/DOMAIN_MIGRATION.md):
  • Build and publish the Android app 3.3.0 for $d (mandatory update) — phones update from the old address.
  • Wait until the panel shows (almost) every device on 3.3.0, then:  sudo deploy/domain-switch.sh finalize
  • To go back:  sudo deploy/domain-switch.sh rollback
NEXT
}

finalize() {
  local d; d="$(env_get APP_DOMAIN)"
  [ -n "$d" ] && [ "$(env_get TLS_MODE)" = "cloudflare" ] || die "Run step 1 first (deploy/domain-switch.sh example.com)."
  say "Finalizing $d: the old address stops, ports 80/443 accept Cloudflare only"
  rm -f deploy/caddy/legacy-*.caddy
  set_env LEGACY_HOSTS ""
  set_env CF_FIREWALL on
  $COMPOSE up -d --force-recreate caddy app </dev/null >/dev/null
  ok "The old address is no longer served"
  "$APP_DIR/deploy/cloudflare-ips.sh"
  install_timer
  checks
  cat <<NEXT

From a phone on mobile data (not through Cloudflare), the server's address must not answer anymore:
  curl -m 8 -k https://<the server's IP>/healthz     → a time-out
Emergency (opens 80/443 to everyone again):  sudo deploy/cloudflare-ips.sh firewall-off ; then CF_FIREWALL=off in .env
NEXT
}

rollback() {
  [ -f .env.before-domain ] || die "No .env.before-domain: nothing to go back to."
  say "Back to the address before the move"
  "$APP_DIR/deploy/cloudflare-ips.sh" firewall-off >/dev/null 2>&1 || true
  cp -p .env .env.after-domain && chmod 600 .env.after-domain
  cp -p .env.before-domain .env && chmod 600 .env
  rm -f deploy/caddy/legacy-*.caddy
  systemctl disable --now dzplay-cloudflare-ips.timer >/dev/null 2>&1 || true
  $COMPOSE up -d --force-recreate caddy app </dev/null >/dev/null
  local url; url="$(env_get PUBLIC_URL)"
  if [ -n "$(env_get TELEGRAM_BOT_TOKEN)" ] && [ "${url#https://}" != "$url" ]; then
    $COMPOSE exec -T app python -m app.admin_cli set-webhook "$url" </dev/null >/dev/null 2>&1 && ok "Telegram webhook: $url"
  fi
  ok "Back on $url (the domain's settings are kept in .env.after-domain)"
}

[ "$(id -u)" = 0 ] || die "Run with sudo."
case "${1:-}" in
  "") die "usage: sudo $0 example.com | check | finalize | rollback" ;;
  check) checks ;;
  finalize) finalize ;;
  rollback) rollback ;;
  -*) die "usage: sudo $0 example.com | check | finalize | rollback" ;;
  *) switch "$(tr '[:upper:]' '[:lower:]' <<< "$1")" ;;
esac
[ "$FAILED" = 0 ]
