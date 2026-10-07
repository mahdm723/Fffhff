#!/usr/bin/env bash
# =============================================================================
# DZPLAY — basic VPS hardening (Ubuntu/Debian). Safe to run several times.
#
#   sudo /opt/dzplay/dzplay/deploy/harden.sh              firewall + fail2ban + automatic security updates
#   sudo /opt/dzplay/dzplay/deploy/harden.sh close-turn   only close the old TURN (calls) ports
#   SSH keys-only / no root login: use deploy/ssh-harden.sh (it proves your key login works first)
#
# 1. ufw firewall: deny incoming except your SSH port (detected), 80/tcp, 443/tcp+udp.
#    (Docker publishes only Caddy's 80/443; PostgreSQL and Redis are never published.)
#    V6: calls were removed, so the old TURN rules (3478/udp+tcp, 5349/tcp, 49160-49200/udp, or the
#    values from .env) are deleted.
# 2. fail2ban: bans IPs that keep failing SSH logins.
# 3. unattended-upgrades: installs security updates automatically.
# 4. SSH: only REPORTS password/root login settings. Changing them is done by deploy/ssh-harden.sh,
#    which refuses to act until a key login is seen in the SSH log AND you confirm it yourself.
# 5. File permissions: .env readable by root only.
# =============================================================================
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
KEYS_ONLY=false
[ "${1:-}" = "--ssh-keys-only" ] && KEYS_ONLY=true

say()  { printf '\n\033[1;35m==>\033[0m %s\n' "$*"; }
ok()   { printf '    \033[1;32m✓\033[0m %s\n' "$*"; }
warn() { printf '    \033[1;33m!\033[0m %s\n' "$*"; }
die()  { printf '\n\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Run as root (sudo)."

# V6: voice/video calls (coturn) were removed — delete the firewall rules that were opened for them.
close_turn() {
  command -v ufw >/dev/null || return 0
  envv() { grep "^$1=" "$APP_DIR/.env" 2>/dev/null | cut -d= -f2- || true; }
  local tp tt tmin tmax rule closed=0
  tp="$(envv TURN_PORT)"; tp="${tp:-3478}"; tt="$(envv TURN_TLS_PORT)"; tt="${tt:-5349}"
  tmin="$(envv TURN_MIN_PORT)"; tmin="${tmin:-49160}"; tmax="$(envv TURN_MAX_PORT)"; tmax="${tmax:-49200}"
  for rule in "$tp/udp" "$tp/tcp" "$tt/tcp" "$tmin:$tmax/udp" 3478/udp 3478/tcp 5349/tcp 49160:49200/udp; do
    [ "${rule%%/*}" = "0" ] && continue
    ufw status 2>/dev/null | grep -qE "^${rule}[[:space:]]" || continue
    ufw --force delete allow "$rule" >/dev/null 2>&1 && closed=1
  done
  if [ "$closed" = 1 ]; then ok "Closed the old TURN (calls) ports"; else ok "No TURN (calls) ports open"; fi
}
if [ "${1:-}" = "close-turn" ]; then close_turn; exit 0; fi
command -v apt-get >/dev/null || die "Ubuntu/Debian only."
export DEBIAN_FRONTEND=noninteractive

say "Packages"
apt-get update -qq
apt-get install -y -qq ufw fail2ban unattended-upgrades gnupg >/dev/null
ok "ufw, fail2ban, unattended-upgrades, gnupg"

ssh_port="$(sshd -T 2>/dev/null | awk '/^port /{print $2; exit}')"
ssh_port="${ssh_port:-22}"

say "Firewall (ufw)"
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow "$ssh_port"/tcp comment 'ssh' >/dev/null
ufw allow 80/tcp comment 'http (certificates + redirect)' >/dev/null
ufw allow 443/tcp comment 'https' >/dev/null
ufw allow 443/udp comment 'http/3' >/dev/null
ufw --force enable >/dev/null
close_turn
ok "incoming allowed only on SSH ($ssh_port), 80, 443"

say "fail2ban (SSH)"
cat > /etc/fail2ban/jail.d/dzplay.local <<EOF
[sshd]
enabled = true
port = $ssh_port
backend = systemd
maxretry = 5
findtime = 10m
bantime = 1h
EOF
systemctl enable --now fail2ban >/dev/null 2>&1
systemctl restart fail2ban
ok "5 failed SSH logins in 10 min → banned for 1 hour"

say "Automatic security updates"
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF
systemctl enable --now unattended-upgrades >/dev/null 2>&1 || true
ok "security updates install daily (reboots are left to you: check /var/run/reboot-required)"

say "SSH"
cfg="$(sshd -T 2>/dev/null || true)"
pw="$(echo "$cfg" | awk '/^passwordauthentication /{print $2}')"
root="$(echo "$cfg" | awk '/^permitrootlogin /{print $2}')"
has_key=false
for f in /root/.ssh/authorized_keys /home/*/.ssh/authorized_keys; do
  [ -s "$f" ] && grep -qE '^(ssh-|ecdsa-|sk-)' "$f" && has_key=true
done
if [ "$KEYS_ONLY" = true ]; then
  warn "--ssh-keys-only moved to deploy/ssh-harden.sh (it first proves that your key login works):"
  warn "  sudo $APP_DIR/deploy/ssh-harden.sh status"
fi
if [ "$pw" = "yes" ]; then warn "SSH password login is ON. When your key works: sudo $APP_DIR/deploy/ssh-harden.sh apply"
else ok "SSH password login is off"; fi
if [ "$root" = "no" ]; then ok "root login is off"; else warn "root may log in over SSH (PermitRootLogin $root)"; fi
[ "$has_key" = true ] || warn "no SSH key installed yet (see the guide: Termux ssh-keygen + ssh-copy-id)"

say "Permissions"
if [ -f "$APP_DIR/.env" ]; then
  chown root:root "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
  ok ".env: root only (600)"
fi
[ -d /var/backups/dzplay ] && chmod 700 /var/backups/dzplay && ok "backups: root only (700)"

echo
ok "Done. Status: ufw status verbose · fail2ban-client status sshd"
