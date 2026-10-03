#!/bin/sh
# Render coturn's configuration from the environment and start it (docker-compose "coturn").
set -eu
if [ -z "${TURN_SECRET:-}" ]; then
  echo "TURN_SECRET is not set: calls are disabled (run deploy/install.sh to generate it)."
  exec sleep infinity
fi
REALM="${TURN_REALM:?TURN_REALM missing}"
conf=/tmp/turnserver.conf
sed -e "s|__REALM__|${REALM}|g" \
    -e "s|__TURN_PORT__|${TURN_PORT:-3478}|g" \
    -e "s|__TURN_MIN_PORT__|${TURN_MIN_PORT:-49160}|g" \
    -e "s|__TURN_MAX_PORT__|${TURN_MAX_PORT:-49200}|g" \
    /etc/coturn/turnserver.conf.template > "$conf"
umask 077
echo "static-auth-secret=${TURN_SECRET}" >> "$conf"
if [ -n "${TURN_EXTERNAL_IP:-}" ]; then
  echo "external-ip=${TURN_EXTERNAL_IP}" >> "$conf"
  # both phones relay through this same server: let relay addresses reach each other
  echo "allowed-peer-ip=${TURN_EXTERNAL_IP}" >> "$conf"
fi
# TURN over TLS with the certificate Caddy already obtained for the domain (read-only mount)
crt="$(find /caddy -name "${REALM}.crt" 2>/dev/null | head -n 1 || true)"
if [ "${TURN_TLS_PORT:-5349}" != "0" ] && [ -n "$crt" ] && [ -f "${crt%.crt}.key" ]; then
  { echo "tls-listening-port=${TURN_TLS_PORT:-5349}"; echo "cert=${crt}"; echo "pkey=${crt%.crt}.key"; } >> "$conf"
  echo "TURN over TLS on port ${TURN_TLS_PORT:-5349}"
else
  echo "no-tls" >> "$conf"
  echo "no-dtls" >> "$conf"
  echo "TURN over TLS disabled (no certificate yet: Caddy gets it on first start; restart coturn afterwards)."
fi
exec turnserver -c "$conf"
