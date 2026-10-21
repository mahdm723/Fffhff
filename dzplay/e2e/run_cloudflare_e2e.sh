#!/usr/bin/env bash
# V6 phase 20: Caddy in TLS_MODE=cloudflare in a THROWAWAY Docker network (never touches a real deployment).
#
#   e2e/run_cloudflare_e2e.sh
#   STACK_EXTRA=dir CF_DOCKERFILE=Dockerfile.stacktest e2e/run_cloudflare_e2e.sh   (offline: a prebuilt image + wheels)
#
# A "Cloudflare" container (an address inside the ranges written by deploy/cloudflare-ips.sh) and an ordinary one
# talk to the real Caddyfile + the app, over the origin certificate. Checks:
#   - the certificate served is the origin certificate (verified against it), and no Server/Via header;
#   - from Cloudflare, CF-Connecting-IP is the client the app sees; from anyone else it is ignored (the peer is);
#     a client's own X-Forwarded-For / X-Real-IP never count;
#   - a WebSocket with a session upgrades through Caddy (101); the app's greeting and keep-alive come back;
#   - the old address: 301 to the domain (path + query kept), /download and /api/app/* still served, the Android
#     app (DZPLAYApp/) still served there;
#   - cloudflare-ips.sh refuses a bad list and keeps the last good one; both modes of the Caddyfile validate.
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
TAG="dz-cf-e2e-$$"
OCT=$(( (RANDOM % 200) + 20 ))
NET4="10.250.$OCT"
CADDY_IP="$NET4.5" APP_IP="$NET4.10" CF_IP="$NET4.200" OTHER_IP="$NET4.20"
DOMAIN="dalta.test" LEGACY="old.test"
cleanup() {
  docker rm -f "$TAG-caddy" "$TAG-app" >/dev/null 2>&1 || true
  docker network rm "$TAG" >/dev/null 2>&1 || true
  docker image rm "$TAG-app" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT
pass() { printf '  ✓ %s\n' "$*"; }
fail() { printf '  ✗ %s\n' "$*" >&2; [ -n "${LOGS:-}" ] && docker logs --tail 30 "$TAG-caddy" >&2; exit 1; }

mkdir "$WORK/stack"
tar -C "$SRC" --exclude=.venv --exclude=.env --exclude=__pycache__ --exclude='e2e/screenshots*' --exclude='e2e/shots*' \
  --exclude='deploy/caddy/cloudflare-ips.caddy' --exclude='deploy/caddy/legacy-*' --exclude='deploy/origin' -cf - . \
  | tar -C "$WORK/stack" -xf -
if [ -n "${STACK_EXTRA:-}" ]; then cp -r "$STACK_EXTRA"/. "$WORK/stack/"; fi
cd "$WORK/stack"

# 1. the Cloudflare ranges: a bad list is refused (the built-in copy is kept), then ours is accepted
lists="$WORK/lists"; mkdir -p "$lists"
printf '%s\n' "$NET4.128/25" 192.0.2.0/24 198.18.0.0/15 203.0.113.0/24 100.64.0.0/16 100.65.0.0/16 100.66.0.0/16 100.67.0.0/16 > "$lists/v4"
printf '%s\n' 2001:db8:1::/48 2001:db8:2::/48 2001:db8:3::/48 2001:db8:4::/48 > "$lists/v6"
printf '%s\n' "$NET4.128/25" "evil.example" 203.0.113.0/24 > "$lists/bad4"
run_ips() { CF_STATE_DIR="$WORK/state" CF_IPS_V4_URL="file://$1" CF_IPS_V6_URL="file://$lists/v6" deploy/cloudflare-ips.sh >/dev/null 2>&1; }
run_ips "$lists/bad4"
grep -q "173.245.48.0/20" deploy/caddy/cloudflare-ips.caddy && ! grep -q "$NET4.128" deploy/caddy/cloudflare-ips.caddy \
  || fail "a bad list was applied"
run_ips "$lists/v4"
grep -q "trusted_proxies static $NET4.128/25 " deploy/caddy/cloudflare-ips.caddy || fail "the ranges were not written"
run_ips "$lists/bad4"
grep -q "$NET4.128/25" deploy/caddy/cloudflare-ips.caddy || fail "the last good list was not kept"
pass "cloudflare-ips.sh: a bad list is refused, the last good one stays"

# 2. the origin certificate (self-signed here; Cloudflare's in production) and the old address's block
mkdir -p deploy/origin
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 7 -subj "/CN=$DOMAIN" \
  -addext "subjectAltName=DNS:$DOMAIN,DNS:*.$DOMAIN" -keyout deploy/origin/key.pem -out deploy/origin/cert.pem 2>/dev/null
chmod 644 deploy/origin/key.pem  # readable by the caddy container in this throwaway copy
printf '%s {\n\ttls internal\n\timport app\n}\n' "$LEGACY" > deploy/caddy/legacy-old.caddy

for mode in auto cloudflare; do
  docker run --rm -e DOMAIN="$DOMAIN" -e TLS_MODE="$mode" -v "$PWD/Caddyfile:/etc/caddy/Caddyfile:ro" \
    -v "$PWD/deploy/caddy:/etc/caddy/conf.d:ro" -v "$PWD/deploy/origin:/etc/caddy/origin:ro" caddy:2-alpine \
    caddy validate --config /etc/caddy/Caddyfile >/dev/null 2>&1 || fail "Caddyfile ($mode) does not validate"
done
docker run --rm -e DOMAIN="$DOMAIN" -v "$PWD/Caddyfile:/etc/caddy/Caddyfile:ro" -v "$PWD/deploy/caddy:/etc/caddy/conf.d:ro" \
  caddy:2-alpine caddy adapt --config /etc/caddy/Caddyfile 2>/dev/null | grep -q '"trusted_proxies"' \
  && fail "auto mode trusts a proxy" || true
pass "Caddyfile validates in both modes (auto trusts no proxy header)"

# 3. the app + Caddy in the throwaway network
docker build -q -t "$TAG-app" -f "${CF_DOCKERFILE:-Dockerfile}" . >/dev/null || fail "app image did not build"
docker network create --subnet "$NET4.0/24" "$TAG" >/dev/null
docker run -d --name "$TAG-app" --network "$TAG" --network-alias app --ip "$APP_IP" \
  -e ENV=production -e SECRET_KEY="$(openssl rand -hex 32)" -e DATABASE_URL=sqlite:////tmp/cf.db -e REDIS_URL= \
  -e TRUST_PROXY_HEADERS=true -e TRUSTED_PROXY_COUNT=1 -e APP_DOMAIN="$DOMAIN" -e LEGACY_HOSTS="$LEGACY" \
  -e WEB_DESKTOP_ACCESS=true -e CHAIN_CHECK_ENABLED=false -e MARKET_REFRESH_SECONDS=0 -e WS_PING_SECONDS=2 \
  -e CLEANUP_INTERVAL=0 -e ATTESTATION_STATUS_URL= "$TAG-app" >/dev/null
for _ in $(seq 1 60); do
  docker exec "$TAG-app" python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')" >/dev/null 2>&1 && break
  sleep 1
done
docker run -d --name "$TAG-caddy" --network "$TAG" --ip "$CADDY_IP" -e DOMAIN="$DOMAIN" -e TLS_MODE=cloudflare \
  -v "$PWD/Caddyfile:/etc/caddy/Caddyfile:ro" -v "$PWD/deploy/caddy:/etc/caddy/conf.d:ro" \
  -v "$PWD/deploy/origin:/etc/caddy/origin:ro" caddy:2-alpine >/dev/null
LOGS=1
TOKEN="$(docker exec -i "$TAG-app" python - <<'PY'
from app.config import PRIVACY_VERSION, get_settings
from app.db import Database
from app.models import User, new_public_user_id
from app import clock
from app.security.sessions import create_session

s = get_settings()
db = Database(s.DATABASE_URL)
with db.session() as x:
    u = User(email="cf-probe@example.com", password_hash="x", status="active", display_name="CF Probe",
             name_norm="cf probe", public_id=new_public_user_id(), privacy_ack_version=PRIVACY_VERSION,
             age_confirmed_at=clock.utcnow(), created_at=clock.utcnow())
    x.add(u)
    x.flush()
    print(create_session(x, s, u))
    x.commit()
PY
)"
[ -n "$TOKEN" ] || fail "no test session"
sleep 2
probe() {  # probe FROM_IP → JSON
  docker run --rm --network "$TAG" --ip "$1" -v "$PWD/e2e/cf_probe.py:/probe.py:ro" -v "$PWD/deploy/origin/cert.pem:/cert.pem:ro" \
    --entrypoint python "$TAG-app" /probe.py "$CADDY_IP" "$DOMAIN" "$LEGACY" /cert.pem "$TOKEN"
}
field() { python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(eval(sys.argv[2], {}, {'d': d}))" "$1" "$2"; }

from_cf="$(probe "$CF_IP")" || fail "probe from the Cloudflare range failed"
from_other="$(probe "$OTHER_IP")" || fail "probe from another address failed"

[ "$(field "$from_cf" "d['cf_header']['ip']")" = "198.51.100.7" ] || fail "from Cloudflare, CF-Connecting-IP not used: $from_cf"
[ "$(field "$from_cf" "d['no_cf_header']['ip']")" = "$CF_IP" ] || fail "from Cloudflare without the header: $from_cf"
pass "from a Cloudflare address: the app sees CF-Connecting-IP; without it, the peer (X-Forwarded-For/X-Real-IP ignored)"
[ "$(field "$from_other" "d['cf_header']['ip']")" = "$OTHER_IP" ] || fail "a forged CF-Connecting-IP was trusted: $from_other"
[ "$(field "$from_other" "d['no_cf_header']['ip']")" = "$OTHER_IP" ] || fail "a forged X-Forwarded-For was trusted: $from_other"
pass "from any other address: CF-Connecting-IP and X-Forwarded-For are ignored (the peer is the client)"
[ "$(field "$from_cf" "(d['cf_header']['server'], d['cf_header']['via'])")" = "(None, None)" ] || fail "Server/Via header sent: $from_cf"
pass "the origin certificate is served (verified against it); no Server/Via header"
[ "$(field "$from_cf" "d['ws']['status']")" = "101" ] && [ "$(field "$from_cf" "'hello' in d['ws']['frames'][0] and 'ping' in d['ws']['frames'][1]")" = "True" ] \
  || fail "WebSocket through Caddy: $(field "$from_cf" "d['ws']")"
pass "WebSocket: 101 through Caddy; the app's greeting and its keep-alive come back through it"
[ "$(field "$from_other" "(d['legacy_root']['status'], d['legacy_root']['location'])")" = "(301, 'https://$DOMAIN/?x=1')" ] \
  || fail "the old address: $(field "$from_other" "d['legacy_root']")"
[ "$(field "$from_other" "(d['legacy_download']['status'], d['legacy_version']['status'])")" = "(200, 200)" ] \
  || fail "the old address must still serve /download and /api/app/*: $from_other"
[ "$(field "$from_other" "d['legacy_app']['location']")" = "None" ] || fail "the installed app was redirected: $from_other"
pass "the old address: 301 to https://$DOMAIN (path + query kept); /download, /api/app/* and the Android app still served"

echo "CLOUDFLARE E2E PASSED"
