#!/usr/bin/env bash
# =============================================================================
# DZPLAY — one-command installer / updater for an Ubuntu or Debian VPS.
#
#   curl -fsSL https://raw.githubusercontent.com/mahdm723/Fffhff/claude/github-access-check-c1exvo/dzplay/deploy/install.sh | sudo bash
#
# Options (environment variables, all optional):
#   DOMAIN=chat.example.com   use your own domain (its DNS A record must point to this server)
#   MODE=http                 no HTTPS, serve on http://<server-ip> (quick test only)
#   BRANCH=...                git branch to deploy (default: the DZPLAY branch)
#   PUBLIC_IP=1.2.3.4         override the auto-detected public IP
#
# Without DOMAIN, a free hostname <ip>.sslip.io is used so you still get real
# HTTPS (needed to install the app on a phone and for notifications).
# Running the command again updates the code and keeps your settings/data.
# =============================================================================
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/mahdm723/Fffhff.git}"
BRANCH="${BRANCH:-claude/github-access-check-c1exvo}"
SRC_DIR="${SRC_DIR:-/opt/dzplay}"
APP_DIR="$SRC_DIR/dzplay"
MODE="${MODE:-https}"

say()  { printf '\n\033[1;35m==>\033[0m %s\n' "$*"; }
ok()   { printf '    \033[1;32m✓\033[0m %s\n' "$*"; }
warn() { printf '    \033[1;33m!\033[0m %s\n' "$*"; }
die()  { printf '\n\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

main() {
[ "$(id -u)" -eq 0 ] || die "Run as root: put 'sudo' before 'bash' (… | sudo bash)."
command -v apt-get >/dev/null || die "This installer supports Ubuntu/Debian (apt-get) only."

# --- 1. system packages ---------------------------------------------------------
say "1/6 Checking system packages"
missing=""
for bin in git curl openssl; do command -v "$bin" >/dev/null || missing="$missing $bin"; done
if [ -n "$missing" ]; then
  apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq $missing ca-certificates >/dev/null
fi
ok "git, curl, openssl"

if ! command -v docker >/dev/null; then
  say "Installing Docker (official script)"
  curl -fsSL https://get.docker.com | sh >/dev/null
fi
systemctl enable --now docker >/dev/null 2>&1 || true
docker compose version >/dev/null 2>&1 || die "Docker Compose plugin missing. Install it: apt-get install docker-compose-plugin"
ok "Docker $(docker version --format '{{.Server.Version}}' 2>/dev/null) + Compose"

# --- 2. code ------------------------------------------------------------------------
say "2/6 Downloading DZPLAY ($BRANCH)"
if [ -d "$SRC_DIR/.git" ]; then
  git -C "$SRC_DIR" fetch -q --depth 1 origin "$BRANCH"
  git -C "$SRC_DIR" checkout -q -B "$BRANCH" FETCH_HEAD
  ok "Updated to $(git -C "$SRC_DIR" rev-parse --short HEAD)"
else
  git clone -q --depth 1 --branch "$BRANCH" "$REPO_URL" "$SRC_DIR"
  ok "Cloned into $SRC_DIR"
fi
cd "$APP_DIR"

# --- 3. configuration (only on first install; your .env is never overwritten) ----
say "3/6 Configuration"
PUBLIC_IP="${PUBLIC_IP:-$(curl -fsS4 --max-time 8 https://api.ipify.org || curl -fsS4 --max-time 8 https://ifconfig.me || true)}"
set_env() {  # set_env KEY VALUE  — replace or append in .env
  if grep -q "^$1=" .env; then sed -i "s|^$1=.*|$1=$2|" .env; else echo "$1=$2" >> .env; fi
}
if [ ! -f .env ]; then
  # Clean copy of the documented defaults (comments removed so every tool parses it the same way).
  sed -E 's/[[:space:]]+#.*$//' .env.example | grep -E '^[A-Z_]+=' > .env
  chmod 600 .env
  set_env ENV production
  set_env SECRET_KEY "$(openssl rand -hex 32)"
  set_env POSTGRES_PASSWORD "$(openssl rand -hex 24)"
  set_env ADMIN_API_TOKEN "$(openssl rand -hex 32)"
  set_env TRUST_PROXY_HEADERS true
  set_env TRUSTED_PROXY_COUNT 1
  if [ "$MODE" = "http" ]; then
    [ -n "$PUBLIC_IP" ] || die "Could not detect the public IP."
    set_env DOMAIN ":80"
    set_env COOKIE_SECURE false
    set_env PUBLIC_URL "http://$PUBLIC_IP"
  else
    if [ -z "${DOMAIN:-}" ]; then
      [ -n "$PUBLIC_IP" ] || die "Could not detect the public IP. Re-run with DOMAIN=your.domain"
      DOMAIN="$(echo "$PUBLIC_IP" | tr '.' '-').sslip.io"
    fi
    set_env DOMAIN "$DOMAIN"
    set_env PUBLIC_URL "https://$DOMAIN"
  fi
  ok "Created .env with fresh secrets"
else
  ok "Keeping existing .env"
fi
PUBLIC_URL="$(grep '^PUBLIC_URL=' .env | cut -d= -f2-)"
SITE="$(grep '^DOMAIN=' .env | cut -d= -f2-)"
ok "Address: $PUBLIC_URL"

# --- 4. network -------------------------------------------------------------------
say "4/6 Network"
for port in 80 443; do
  holder="$(ss -ltnpH "( sport = :$port )" 2>/dev/null | grep -v docker-proxy | head -1 || true)"
  if [ -n "$holder" ] && ! docker compose ps -q caddy 2>/dev/null | grep -q .; then
    die "Port $port is already used by another program: $holder
    Stop it (e.g. 'systemctl stop nginx' or 'systemctl stop apache2') and run this command again."
  fi
done
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow 80/tcp >/dev/null && ufw allow 443/tcp >/dev/null
  ok "Firewall (ufw): opened ports 80 and 443"
fi
ok "Ports 80/443 available"

# --- 5. build & start ---------------------------------------------------------------
say "5/6 Building and starting (first time takes a few minutes)"
docker compose up -d --build --remove-orphans </dev/null
if [ "${SITE}" != ":80" ] && ! grep -q '^VAPID_PRIVATE_KEY=.\+' .env; then
  # Keys for phone notifications (Web Push), generated once.
  keys="$(docker compose run --rm --no-deps -T app python -m app.admin_cli gen-vapid </dev/null)"
  set_env VAPID_PUBLIC_KEY "$(echo "$keys" | grep '^VAPID_PUBLIC_KEY=' | cut -d= -f2-)"
  set_env VAPID_PRIVATE_KEY "$(echo "$keys" | grep '^VAPID_PRIVATE_KEY=' | cut -d= -f2-)"
  docker compose up -d app >/dev/null </dev/null
  ok "Notification keys generated"
fi

# --- 6. checks ----------------------------------------------------------------------
say "6/6 Checking"
for i in $(seq 1 60); do
  if docker compose exec -T app python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')" >/dev/null 2>&1 </dev/null; then
    ok "Application is running"; break
  fi
  [ "$i" -eq 60 ] && { docker compose logs --tail 50 app </dev/null; die "The application did not start (logs above)."; }
  sleep 2
done
public_ok=0
for i in $(seq 1 45); do
  if curl -fsS --max-time 5 "$PUBLIC_URL/healthz" >/dev/null 2>&1; then public_ok=1; break; fi
  sleep 4   # HTTPS certificate issuance can take a minute
done
if [ "$public_ok" -eq 1 ]; then
  ok "Reachable at $PUBLIC_URL"
else
  warn "Not reachable from outside yet at $PUBLIC_URL"
  warn "Check that your VPS provider's firewall/security group allows TCP 80 and 443."
  warn "Certificate logs: cd $APP_DIR && docker compose logs caddy"
fi

ADMIN_TOKEN="$(grep '^ADMIN_API_TOKEN=' .env | cut -d= -f2-)"
cat <<EOF

=====================================================================
  DZPLAY is installed 🎉

  Open on your phone:   $PUBLIC_URL

  Admin statistics:     curl -H "Authorization: Bearer $ADMIN_TOKEN" $PUBLIC_URL/api/admin/stats
  (keep this token secret — it is stored in $APP_DIR/.env)

  Update to the latest version:   run the same install command again
  Logs:      cd $APP_DIR && docker compose logs -f app
  Stop:      cd $APP_DIR && docker compose down
  Admin CLI: cd $APP_DIR && docker compose exec app python -m app.admin_cli stats
=====================================================================
EOF
}

# Everything above is only definitions: bash has read the whole file before
# anything runs, so commands reading stdin cannot swallow the rest of the script
# when it is piped (curl … | bash).
main "$@" </dev/null
