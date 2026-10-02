#!/usr/bin/env bash
# =============================================================================
# DZPLAY — basic VPS hardening (Ubuntu/Debian). Safe to run several times.
#
#   sudo /opt/dzplay/dzplay/deploy/harden.sh              firewall + fail2ban + automatic security updates
#   sudo /opt/dzplay/dzplay/deploy/harden.sh --ssh-keys-only   … and turn off SSH password logins
#
# 1. ufw firewall: deny incoming except your SSH port (detected), 80/tcp, 443/tcp+udp.
#    (Docker publishes only Caddy's 80/443; PostgreSQL and Redis are never published.)
# 2. fail2ban: bans IPs that keep failing SSH logins.
# 3. unattended-upgrades: installs security updates automatically.
# 4. SSH: reports password/root login settings. With --ssh-keys-only it disables password
#    logins, but ONLY if an authorized SSH key exists (so you cannot lock yourself out).
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
  if [ "$has_key" = true ]; then
    cat > /etc/ssh/sshd_config.d/99-dzplay-hardening.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
EOF
    sshd -t && (systemctl reload ssh 2>/dev/null || systemctl reload sshd)
    ok "password logins disabled (SSH keys only)"
  else
    warn "no authorized SSH key found: password logins left ON so you are not locked out."
    warn "add your key first (ssh-copy-id root@SERVER), then run again with --ssh-keys-only"
  fi
else
  [ "$pw" = "yes" ] && warn "SSH password login is ON. Recommended: add an SSH key, then run: $0 --ssh-keys-only" \
    || ok "SSH password login is off"
  [ "$root" = "yes" ] && warn "root may log in with a password (PermitRootLogin yes)" || true
fi

say "Permissions"
if [ -f "$APP_DIR/.env" ]; then
  chown root:root "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
  ok ".env: root only (600)"
fi
[ -d /var/backups/dzplay ] && chmod 700 /var/backups/dzplay && ok "backups: root only (700)"

echo
ok "Done. Status: ufw status verbose · fail2ban-client status sshd"
