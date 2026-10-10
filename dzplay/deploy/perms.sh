#!/usr/bin/env bash
# =============================================================================
# DZPLAY — permissions of the sensitive files on the server.
#
#   sudo /opt/dzplay/dzplay/deploy/perms.sh          check only (changes nothing)
#   sudo /opt/dzplay/dzplay/deploy/perms.sh fix      tighten what is too open (never loosens anything)
#
# Checked: .env (and the .env copies kept by restore.sh), the encrypted backups and exports,
# the backup/monitor logs and state folders, the deploy scripts, the cron files, SSH keys and
# host keys, the Docker volumes (database files, TLS private keys), and that no Android signing
# key (*.jks / *.keystore / *.p12) is anywhere on this server.
# Exit code: 0 = all good, 1 = something is too open (or a signing key was found).
# =============================================================================
set -uo pipefail

MODE="${1:-check}"
case "$MODE" in check|fix) ;; *) echo "usage: $0 [check|fix]" >&2; exit 2 ;; esac

APP_DIR="${APP_DIR:-$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)}"
SRC_DIR="${SRC_DIR:-$(dirname "$APP_DIR")}"
env_get() { [ -f "$APP_DIR/.env" ] && grep -E "^$1=" "$APP_DIR/.env" | tail -1 | cut -d= -f2- || true; }
BACKUP_DIR="${BACKUP_DIR:-$(env_get BACKUP_DIR)}"; BACKUP_DIR="${BACKUP_DIR:-/var/backups/dzplay}"
LOG_DIR="${PERMS_LOG_DIR:-/var/log}"
LIB_DIR="${PERMS_LIB_DIR:-/var/lib}"
CRON_DIR="${PERMS_CRON_DIR:-/etc/cron.d}"
SSHD_DIR="${PERMS_SSHD_DIR:-/etc/ssh}"
HOMES="${PERMS_HOMES:-/root /home/*}"
DOCKER_DIR="${PERMS_DOCKER_DIR:-/var/lib/docker}"

bad=0
ok()   { printf '  \033[1;32m✓\033[0m %s\n' "$*"; }
fail() { printf '  \033[1;31m✗\033[0m %s\n' "$*"; bad=$((bad + 1)); }

# limit PATH MAX_MODE [OWNER] — PATH may not have any permission bit outside MAX_MODE (octal)
# (so 600 accepts 600 and 400); with OWNER the file must also belong to that user.
limit() {
  local path="$1" max="$2" owner="${3:-}" cur extra want who
  [ -e "$path" ] || return 0
  cur="$(stat -c %a "$path")"
  extra=$(( 8#$cur & ~8#$max & 8#7777 ))
  who="$(stat -c %U "$path")"
  if [ "$extra" -eq 0 ] && { [ -z "$owner" ] || [ "$who" = "$owner" ]; }; then
    ok "$path ($cur $who)"
    return 0
  fi
  if [ "$MODE" = fix ]; then
    want="$(printf '%o' $(( 8#$cur & 8#$max )))"
    chmod "$want" "$path"
    [ -n "$owner" ] && [ "$who" != "$owner" ] && chown "$owner" "$path"
    ok "$path fixed: $cur $who → $want ${owner:-$who}"
  else
    fail "$path is $cur $who (should be at most $max${owner:+, owner $owner})"
  fi
}

echo "Sensitive files ($MODE):"
limit "$APP_DIR/.env" 600 root
for f in "$APP_DIR"/.env.*; do
  [ -e "$f" ] || continue
  case "$f" in */.env.example) continue ;; esac
  limit "$f" 600 root
done
if [ -d "$BACKUP_DIR" ]; then
  limit "$BACKUP_DIR" 700 root
  while IFS= read -r -d '' f; do limit "$f" 600 root; done < <(find "$BACKUP_DIR" -maxdepth 1 -type f -print0)
else
  ok "$BACKUP_DIR (no backups yet)"
fi
for f in "$LOG_DIR/dzplay-backup.log" "$LOG_DIR/dzplay-monitor.log" "$LOG_DIR/dzplay-perms.log"; do limit "$f" 600; done
for d in "$LIB_DIR/dzplay-monitor" "$LIB_DIR/dzplay-ssh"; do limit "$d" 700; done
for f in "$APP_DIR"/deploy/*.sh; do limit "$f" 700; done
for f in "$CRON_DIR"/dzplay-*; do [ -e "$f" ] && limit "$f" 644; done
for home in $HOMES; do
  [ -d "$home/.ssh" ] || continue
  limit "$home/.ssh" 700
  for f in "$home/.ssh/authorized_keys" "$home"/.ssh/id_*; do
    [ -e "$f" ] || continue
    case "$f" in *.pub) limit "$f" 644 ;; *) limit "$f" 600 ;; esac
  done
done
for f in "$SSHD_DIR"/ssh_host_*_key; do [ -e "$f" ] && limit "$f" 600; done
# Docker data (database files, Caddy's TLS private keys): other users may not list it.
limit "$DOCKER_DIR" 711
limit "$DOCKER_DIR/volumes" 711

# The Android signing key must never be on the server (nor in the repository).
keys="$(find "$SRC_DIR" "$BACKUP_DIR" -type f \( -name '*.jks' -o -name '*.keystore' -o -name '*.p12' \) 2>/dev/null)"
if [ -n "$keys" ]; then
  while IFS= read -r k; do fail "signing key found on the server: $k (remove it; keep it only offline)"; done <<<"$keys"
else
  ok "no Android signing key on this server"
fi

if [ "$bad" -eq 0 ]; then
  echo "All sensitive files have safe permissions."
  exit 0
fi
echo "$bad problem(s). Fix them with: sudo $APP_DIR/deploy/perms.sh fix" >&2
exit 1
