#!/usr/bin/env bash
# =============================================================================
# DZPLAY — SSH: keys only, no password logins, no root login — WITHOUT locking you out.
#
#   sudo deploy/ssh-harden.sh status            what is on now, which accounts have SSH keys
#   sudo deploy/ssh-harden.sh add-user NAME     create a sudo user NAME and copy root's SSH keys to it
#   sudo deploy/ssh-harden.sh apply             interactive; refuses unless you PROVE a key login works:
#                                                 1) a key login of that user appears in the SSH log, and
#                                                 2) you type the confirmation sentence yourself.
#                                               A safety timer restores the old settings after 10 minutes
#                                               unless you run "confirm" from a NEW session.
#   sudo deploy/ssh-harden.sh confirm           keep the new settings (cancels the safety timer)
#   sudo deploy/ssh-harden.sh rollback          restore the previous settings at once
#
# Never run by the installer. Current SSH sessions stay open while the settings change.
# =============================================================================
set -euo pipefail

CONF=/etc/ssh/sshd_config.d/00-dzplay-ssh.conf   # 00-: sshd keeps the FIRST value it reads (cloud images ship 50-cloud-init.conf)
OLD_CONF=/etc/ssh/sshd_config.d/99-dzplay-hardening.conf   # written by older harden.sh --ssh-keys-only
STATE=/var/lib/dzplay-ssh
TIMER=dzplay-ssh-rollback
SELF="$(readlink -f "$0")"
PHRASE="دخلت بالمفتاح"

say()  { printf '\n\033[1;35m==>\033[0m %s\n' "$*"; }
ok()   { printf '    \033[1;32m✓\033[0m %s\n' "$*"; }
warn() { printf '    \033[1;33m!\033[0m %s\n' "$*"; }
die()  { printf '\n\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Run as root (sudo)."
command -v sshd >/dev/null || die "OpenSSH server not found."
mkdir -p "$STATE" /etc/ssh/sshd_config.d
chmod 700 "$STATE"

reload_ssh() { systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || service ssh reload 2>/dev/null || pkill -HUP -x sshd; }
has_keys() { [ -s "$1" ] && grep -qE '^(ssh-(ed25519|rsa|dss)|ecdsa-|sk-)' "$1"; }
ssh_port() { sshd -T 2>/dev/null | awk '/^port /{print $2; exit}'; }
key_users() {  # non-root users with an SSH key and sudo rights
  for d in /home/*; do
    u="$(basename "$d")"
    id "$u" >/dev/null 2>&1 || continue
    has_keys "$d/.ssh/authorized_keys" || continue
    id -nG "$u" | grep -qwE 'sudo|wheel|admin' || continue
    echo "$u"
  done
}

cmd_status() {
  say "Current SSH settings"
  sshd -T 2>/dev/null | grep -E '^(port|passwordauthentication|kbdinteractiveauthentication|permitrootlogin|pubkeyauthentication) ' | sed 's/^/    /'
  say "Accounts with SSH keys"
  has_keys /root/.ssh/authorized_keys && ok "root" || warn "root: no key"
  users="$(key_users || true)"
  [ -n "$users" ] && for u in $users; do ok "$u (sudo)"; done || warn "no sudo user with an SSH key yet: run  $0 add-user NAME"
  [ -f "$CONF" ] && ok "DZPLAY SSH hardening is applied ($CONF)" || warn "DZPLAY SSH hardening is not applied"
  { systemctl is-active --quiet "$TIMER.timer" 2>/dev/null || [ -f "$STATE/rollback.pid" ]; } \
    && warn "safety rollback timer is running: run '$0 confirm' after testing" || true
}

cmd_add_user() {
  local u="${1:-}"
  [[ "$u" =~ ^[a-z_][a-z0-9_-]{1,31}$ ]] || die "Give a user name (lowercase letters/digits), e.g.: $0 add-user dz"
  has_keys /root/.ssh/authorized_keys || die "root has no SSH key to copy. First add your Termux key: see docs (ssh-copy-id)."
  if ! id "$u" >/dev/null 2>&1; then
    adduser --disabled-password --gecos "" "$u" >/dev/null
    ok "user $u created"
  fi
  usermod -aG sudo "$u"
  install -d -m 700 -o "$u" -g "$u" "/home/$u/.ssh"
  install -m 600 -o "$u" -g "$u" /root/.ssh/authorized_keys "/home/$u/.ssh/authorized_keys"
  ok "root's SSH keys copied to $u; $u can use sudo"
  if ! passwd -S "$u" 2>/dev/null | grep -qE "^$u P "; then
    warn "set a password for sudo (it will NOT allow SSH logins once hardened):  passwd $u"
  fi
  echo
  echo "    Now, from Termux, in a NEW window (keep this one open):"
  echo "      ssh -p $(ssh_port) $u@<SERVER-IP>"
  echo "      sudo -v"
  echo "    Then run:  sudo $0 apply"
}

recent_key_login() {  # a successful public-key login of $1 in the last 60 minutes
  local u="$1"
  if command -v journalctl >/dev/null; then
    journalctl --since "-60 min" -u ssh -u sshd 2>/dev/null | grep -q "Accepted publickey for $u " && return 0
  fi
  for f in /var/log/auth.log /var/log/secure; do
    [ -r "$f" ] && tail -n 5000 "$f" | grep -q "Accepted publickey for $u " && return 0
  done
  return 1
}

stop_timer() {
  systemctl stop "$TIMER.timer" "$TIMER.service" 2>/dev/null || true
  if [ -f "$STATE/rollback.pid" ]; then
    pid="$(cat "$STATE/rollback.pid")"
    pkill -P "$pid" 2>/dev/null || true
    kill "$pid" 2>/dev/null || true
    rm -f "$STATE/rollback.pid"
  fi
}

arm_timer() {  # safety net: automatic rollback in 10 minutes unless confirmed
  stop_timer
  if command -v systemd-run >/dev/null \
     && systemd-run --quiet --unit="$TIMER" --on-active=600 "$SELF" rollback --from-timer 2>/dev/null; then
    ok "safety timer: the old settings come back automatically in 10 minutes"
    return
  fi
  # no systemd: a detached background shell does the same
  nohup bash -c 'echo $$ > "$1/rollback.pid"; sleep 600; "$2" rollback --from-timer' _ "$STATE" "$SELF" \
    >/dev/null 2>&1 < /dev/null &
  for _ in 1 2 3 4 5 6 7 8 9 10; do [ -s "$STATE/rollback.pid" ] && break; sleep 0.1; done
  [ -s "$STATE/rollback.pid" ] || { rm -f "$CONF"; cp -a "$STATE/backup-$ts/sshd_config" /etc/ssh/sshd_config;
                                    die "could not start the safety timer. Nothing changed."; }
  ok "safety timer: the old settings come back automatically in 10 minutes"
}

cmd_apply() {
  [ -t 0 ] || die "Interactive only: run it yourself in a terminal (it asks for a confirmation)."
  users="$(key_users || true)"
  [ -n "$users" ] || die "No sudo user with an SSH key: root login would be turned off and you would be locked out.
   First:  sudo $0 add-user NAME   then log in with that user's key from Termux."
  user=""
  for u in $users; do recent_key_login "$u" && { user="$u"; break; }; done
  [ -n "$user" ] || die "No SSH key login of $(echo $users | tr ' ' '/') in the last hour (SSH log).
   From Termux, in a NEW window:  ssh -p $(ssh_port) <user>@<SERVER-IP>  — then run apply again from that session."
  ok "key login of '$user' found in the SSH log"
  echo
  echo "    This will turn OFF password logins and root logins. Only SSH keys will work."
  echo "    Confirm that you yourself just logged in from Termux with your key as '$user'."
  printf '    Type exactly «%s» to continue: ' "$PHRASE"
  read -r answer
  [ "$answer" = "$PHRASE" ] || die "Not confirmed. Nothing changed."

  ts="$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$STATE/backup-$ts"
  cp -a /etc/ssh/sshd_config "$STATE/backup-$ts/"
  cp -a /etc/ssh/sshd_config.d "$STATE/backup-$ts/" 2>/dev/null || true
  echo "$ts" > "$STATE/last-backup"
  rm -f "$OLD_CONF"
  cat > "$CONF" <<CONF
# DZPLAY: SSH keys only (deploy/ssh-harden.sh). Rollback: sudo $SELF rollback
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
MaxAuthTries 3
LoginGraceTime 30
X11Forwarding no
CONF
  chmod 644 "$CONF"
  if ! sshd -t; then
    rm -f "$CONF"
    die "sshd rejected the configuration: nothing changed."
  fi
  # sshd keeps the first value it reads: check the EFFECTIVE settings before going live.
  if ! grep -qE '^Include /etc/ssh/sshd_config.d/\*\.conf' /etc/ssh/sshd_config; then
    sed -i '1i Include /etc/ssh/sshd_config.d/*.conf' /etc/ssh/sshd_config
  fi
  eff="$(sshd -T)"
  if ! echo "$eff" | grep -q '^passwordauthentication no' || ! echo "$eff" | grep -q '^permitrootlogin no' \
     || ! echo "$eff" | grep -q '^pubkeyauthentication yes'; then
    rm -f "$CONF"
    cp -a "$STATE/backup-$ts/sshd_config" /etc/ssh/sshd_config
    die "another setting overrides ours (see: sshd -T). Nothing changed."
  fi
  arm_timer
  reload_ssh
  ok "applied: keys only, no passwords, no root login (your open sessions stay connected)"
  echo
  echo "    NOW, in a NEW Termux window (keep this one open):"
  echo "      ssh -p $(ssh_port) $user@<SERVER-IP>        → must work"
  echo "      ssh -p $(ssh_port) root@<SERVER-IP>         → must be refused"
  echo "    If it works:      sudo $SELF confirm"
  echo "    If not:           sudo $SELF rollback   (or just wait 10 minutes)"
}

cmd_confirm() {
  stop_timer
  ok "kept. SSH: keys only, no passwords, no root login."
}

cmd_rollback() {
  rm -f "$CONF"
  ts="$(cat "$STATE/last-backup" 2>/dev/null || true)"
  if [ -n "$ts" ] && [ -f "$STATE/backup-$ts/sshd_config" ]; then
    cp -a "$STATE/backup-$ts/sshd_config" /etc/ssh/sshd_config
  fi
  sshd -t && reload_ssh
  if [ "${1:-}" = "--from-timer" ]; then rm -f "$STATE/rollback.pid"; else stop_timer; fi
  ok "previous SSH settings restored"
}

case "${1:-status}" in
  status) cmd_status ;;
  add-user) cmd_add_user "${2:-}" ;;
  apply) cmd_apply ;;
  confirm) cmd_confirm ;;
  rollback) cmd_rollback "${2:-}" ;;
  *) die "usage: $0 status | add-user NAME | apply | confirm | rollback" ;;
esac
