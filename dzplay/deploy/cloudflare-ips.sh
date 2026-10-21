#!/usr/bin/env bash
# V6 phase 20: Cloudflare's address ranges, kept up to date (systemd timer dzplay-cloudflare-ips: at boot + daily).
#
#   sudo deploy/cloudflare-ips.sh                 fetch the ranges → deploy/caddy/cloudflare-ips.caddy (Caddy reads
#                                                 CF-Connecting-IP only from these addresses) + the firewall when on
#   sudo deploy/cloudflare-ips.sh status          what is applied now
#   sudo deploy/cloudflare-ips.sh firewall-off    remove the firewall rules at once (emergency; CF_FIREWALL stays
#                                                 as it is in .env, so set it to off there too)
#
# The firewall is applied only with CF_FIREWALL=on in .env (set by `deploy/domain-switch.sh finalize`, the last step
# of the move): ports 80 and 443 then accept Cloudflare's ranges only. Docker's published ports bypass ufw, so the
# rules live in Docker's DOCKER-USER chain (iptables and ip6tables), for new connections arriving on the public
# interface. SSH and everything else are never touched.
# A list that cannot be fetched or does not look right is never used: the last good one (or the copy below) stays.
set -euo pipefail

APP_DIR="${APP_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$APP_DIR"
STATE_DIR="${CF_STATE_DIR:-/var/lib/dzplay-cloudflare}"
SNIPPET="${CF_SNIPPET:-deploy/caddy/cloudflare-ips.caddy}"
URL4="${CF_IPS_V4_URL:-https://www.cloudflare.com/ips-v4}"
URL6="${CF_IPS_V6_URL:-https://www.cloudflare.com/ips-v6}"
CHAIN="DZ-CLOUDFLARE"
COMPOSE="${COMPOSE:-docker compose}"

# Cloudflare's published ranges when this file was written: used only until a fetched list has been validated once.
BUNDLED_V4="173.245.48.0/20 103.21.244.0/22 103.22.200.0/22 103.31.4.0/22 141.101.64.0/18 108.162.192.0/18
190.93.240.0/20 188.114.96.0/20 197.234.240.0/22 198.41.128.0/17 162.158.0.0/15 104.16.0.0/13 104.24.0.0/14
172.64.0.0/13 131.0.72.0/22"
BUNDLED_V6="2400:cb00::/32 2606:4700::/32 2803:f800::/32 2405:b500::/32 2405:8100::/32 2a06:98c0::/29 2c0f:f248::/32"

log() { printf '%s %s\n' "$(date '+%F %T')" "$*"; }
env_get() { [ -f .env ] && grep -E "^$1=" .env | tail -1 | cut -d= -f2- | sed 's/[[:space:]]*#.*$//; s/^"//; s/"$//' || true; }

valid_v4() {  # a.b.c.d/n with every part in range
  local ip="${1%/*}" bits="${1##*/}" o
  [[ "$1" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}/[0-9]{1,2}$ ]] || return 1
  (( bits >= 8 && bits <= 32 )) || return 1
  IFS=. read -r -a o <<< "$ip"
  for x in "${o[@]}"; do (( 10#$x <= 255 )) || return 1; done
}

valid_v6() {  # hex groups with :: at most once, /n
  local ip="${1%/*}" bits="${1##*/}"
  [[ "$1" =~ ^[0-9a-fA-F:]+/[0-9]{1,3}$ ]] || return 1
  (( bits >= 16 && bits <= 128 )) || return 1
  [[ "$ip" == *:* && "$ip" != *:::* ]] || return 1
  [[ "$(grep -o '::' <<< "$ip" | wc -l)" -le 1 ]] || return 1
  local g
  IFS=: read -r -a g <<< "$ip"
  for x in "${g[@]}"; do [ ${#x} -le 4 ] || return 1; done
}

# parse FAMILY TEXT → the ranges, one per line; fails (prints nothing) if any line is not a range or the count is odd
parse() {
  local fam="$1" text="$2" out=() line min max
  if [ "$fam" = 4 ]; then min=8; max=100; else min=4; max=100; fi
  while IFS= read -r line; do
    line="${line//$'\r'/}"; line="${line#"${line%%[![:space:]]*}"}"; line="${line%"${line##*[![:space:]]}"}"
    [ -n "$line" ] || continue
    if [ "$fam" = 4 ]; then valid_v4 "$line" || return 1; else valid_v6 "$line" || return 1; fi
    out+=("$line")
  done <<< "$text"
  (( ${#out[@]} >= min && ${#out[@]} <= max )) || return 1
  printf '%s\n' "${out[@]}"
}

fetch() {  # fetch FAMILY URL → validated ranges, or nothing
  local body
  body="$(curl -fsS --max-time 20 --proto '=https,file' "$2" 2>/dev/null)" || return 1
  parse "$1" "$body"
}

ranges() {  # ranges FAMILY URL → the list to use (fetched, else last good, else bundled), saved as the last good
  local fam="$1" url="$2" good="$STATE_DIR/ips-v$1.txt" got
  mkdir -p "$STATE_DIR"
  if got="$(fetch "$fam" "$url")" && [ -n "$got" ]; then
    printf '%s\n' "$got" > "$good.tmp" && mv "$good.tmp" "$good"
  else
    log "Cloudflare IPv$fam list could not be fetched or did not look right: keeping the last good one (or the built-in copy)" >&2
    if [ -s "$good" ] && got="$(parse "$fam" "$(cat "$good")")"; then :
    else
      local b; if [ "$fam" = 4 ]; then b="$BUNDLED_V4"; else b="$BUNDLED_V6"; fi
      got="$(tr ' ' '\n' <<< "$b" | grep .)"
    fi
  fi
  printf '%s\n' "$got"
}

write_snippet() {  # the Caddy part; reload Caddy when it changed and Caddy runs in cloudflare mode
  local all="$1" new
  new="# Written by deploy/cloudflare-ips.sh — Cloudflare's ranges (do not edit; refreshed daily)
trusted_proxies static $(tr '\n' ' ' <<< "$all" | sed 's/ *$//')"
  if [ -f "$SNIPPET" ] && [ "$(cat "$SNIPPET")" = "$new" ]; then
    return 0
  fi
  mkdir -p "$(dirname "$SNIPPET")"
  printf '%s\n' "$new" > "$SNIPPET.tmp" && mv "$SNIPPET.tmp" "$SNIPPET"
  log "Caddy: $(wc -l <<< "$all") Cloudflare ranges written to $SNIPPET"
  if [ "$(env_get TLS_MODE)" = "cloudflare" ] && $COMPOSE ps -q caddy 2>/dev/null </dev/null | grep -q .; then
    $COMPOSE exec -T caddy caddy reload --config /etc/caddy/Caddyfile </dev/null >/dev/null 2>&1 \
      && log "Caddy reloaded" || log "Caddy reload failed (it keeps the previous ranges)" >&2
  fi
}

public_if() {  # the interface of the default route for the family
  local probe="1.1.1.1" cmd=(ip -4)
  [ "$1" = 6 ] && { probe="2606:4700:4700::1111"; cmd=(ip -6); }
  "${cmd[@]}" route get "$probe" 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "dev") { print $(i + 1); exit }}'
}

hooks() {  # hooks IPT IFACE ACTION(-C|-I|-D) — the two jumps from DOCKER-USER to our chain
  local ipt="$1" ifc="$2" act="$3" rc=0
  for port in 443 80; do
    local rule=(DOCKER-USER -i "$ifc" -p tcp -m conntrack --ctstate NEW --ctdir ORIGINAL --ctorigdstport "$port" -j "$CHAIN")
    if [ "$act" = "-I" ]; then
      "$ipt" -C "${rule[@]}" 2>/dev/null || "$ipt" -I "${rule[0]}" 1 "${rule[@]:1}" || rc=1
    elif [ "$act" = "-D" ]; then
      while "$ipt" -D "${rule[@]}" 2>/dev/null; do :; done
    else
      "$ipt" -C "${rule[@]}" 2>/dev/null || rc=1
    fi
  done
  return $rc
}

firewall_on() {  # firewall_on FAMILY RANGES
  local fam="$1" list="$2" ipt=iptables ifc
  [ "$fam" = 6 ] && ipt=ip6tables
  command -v "$ipt" >/dev/null || { log "$ipt not found: IPv$fam not filtered" >&2; return 0; }
  "$ipt" -n -L DOCKER-USER >/dev/null 2>&1 || { log "IPv$fam: no DOCKER-USER chain (is Docker running?)" >&2; return 0; }
  ifc="$(public_if "$fam")"
  [ -n "$ifc" ] || { log "IPv$fam: no default route, nothing to filter"; return 0; }
  "$ipt" -N "$CHAIN" 2>/dev/null || true
  # refilled in place: during the refill the chain lets connections through (never locks the site out)
  "$ipt" -F "$CHAIN"
  while IFS= read -r c; do [ -n "$c" ] && "$ipt" -A "$CHAIN" -s "$c" -j RETURN; done <<< "$list"
  "$ipt" -A "$CHAIN" -j DROP
  hooks "$ipt" "$ifc" -I
  log "Firewall IPv$fam ($ifc): ports 80/443 open to $(grep -c . <<< "$list") Cloudflare ranges only"
}

firewall_off() {
  for ipt in iptables ip6tables; do
    command -v "$ipt" >/dev/null || continue
    local fam=4; [ "$ipt" = ip6tables ] && fam=6
    local ifc; ifc="$(public_if "$fam")"
    [ -n "$ifc" ] && hooks "$ipt" "$ifc" -D
    "$ipt" -F "$CHAIN" 2>/dev/null || true
    "$ipt" -X "$CHAIN" 2>/dev/null || true
  done
  log "Firewall: ports 80/443 open to everyone again"
}

status() {
  echo "TLS_MODE=$(env_get TLS_MODE) CF_FIREWALL=$(env_get CF_FIREWALL)"
  [ -f "$SNIPPET" ] && echo "Caddy: $(grep -o '/' "$SNIPPET" | wc -l) trusted Cloudflare ranges ($SNIPPET)" || echo "Caddy: no ranges file yet"
  for ipt in iptables ip6tables; do
    command -v "$ipt" >/dev/null || continue
    local fam=4; [ "$ipt" = ip6tables ] && fam=6
    local ifc; ifc="$(public_if "$fam")"
    if [ -n "$ifc" ] && hooks "$ipt" "$ifc" -C 2>/dev/null; then
      echo "Firewall IPv$fam: ON ($("$ipt" -S "$CHAIN" 2>/dev/null | grep -c RETURN) ranges)"
    else
      echo "Firewall IPv$fam: off"
    fi
  done
}

case "${1:-update}" in
  update)
    V4="$(ranges 4 "$URL4")"; V6="$(ranges 6 "$URL6")"
    write_snippet "$(printf '%s\n%s' "$V4" "$V6")"
    if [ "$(env_get CF_FIREWALL)" = "on" ]; then
      [ "$(id -u)" = 0 ] || { log "the firewall needs root (sudo)" >&2; exit 1; }
      firewall_on 4 "$V4"; firewall_on 6 "$V6"
    fi
    ;;
  status) status ;;
  firewall-off) [ "$(id -u)" = 0 ] || { echo "needs root (sudo)" >&2; exit 1; }; firewall_off ;;
  parse4) parse 4 "$(cat)" ;;  # tests: validate a list on stdin
  parse6) parse 6 "$(cat)" ;;
  *) echo "usage: $0 [update|status|firewall-off]" >&2; exit 2 ;;
esac
