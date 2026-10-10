#!/usr/bin/env bash
# =============================================================================
# DZPLAY — server monitoring with Telegram alerts (to the admin chat of the bot).
#
#   sudo deploy/monitor.sh            one check (the installer runs it every 5 minutes from cron)
#   sudo deploy/monitor.sh install    (re)install the cron job + log rotation
#   sudo deploy/monitor.sh test       send a test alert
#   sudo deploy/monitor.sh status     print the checks without sending anything
#
# Checks: disk usage, load, memory, every container (app, db, redis, caddy, media-worker),
# the media worker heartbeat, the public /healthz, and repeated intrusion attempts (fail2ban bans).
# One message when a problem starts, a reminder every MONITOR_REMIND_HOURS while it lasts,
# and "✅ عاد طبيعيًا" when it is over.
#
# Alerts go through the app container (admin_cli alert: the bot token never leaves the server and is
# never put on a command line). If the app itself is down, the bot token from .env is used with curl
# (passed on stdin, never as an argument). If the bot was connected only from the panel and the app is
# down, the alert can only be written to /var/log/dzplay-monitor.log.
#
# Settings (.env, optional): MONITOR_DISK_PCT (85), MONITOR_LOAD_PER_CPU (2.0), MONITOR_MEM_PCT (92),
# MONITOR_BANS_PER_HOUR (20), MONITOR_REMIND_HOURS (6).
# =============================================================================
set -uo pipefail
umask 077

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$APP_DIR"
SELF="$APP_DIR/deploy/monitor.sh"
COMPOSE="${COMPOSE:-docker compose}"
STATE="${MONITOR_STATE_DIR:-/var/lib/dzplay-monitor}"
F2B_LOG="${MONITOR_F2B_LOG:-/var/log/fail2ban.log}"
SERVICES="app db redis caddy media-worker"

env_get() { [ -f .env ] && grep -E "^$1=" .env | tail -1 | cut -d= -f2- | sed 's/[[:space:]]*#.*$//; s/^"//; s/"$//' || true; }
num() { local v; v="$(env_get "$1")"; [[ "$v" =~ ^[0-9]+([.][0-9]+)?$ ]] && echo "$v" || echo "$2"; }

DISK_PCT="$(num MONITOR_DISK_PCT 85)"
LOAD_PER_CPU="$(num MONITOR_LOAD_PER_CPU 2.0)"
MEM_PCT="$(num MONITOR_MEM_PCT 92)"
BANS_PER_HOUR="$(num MONITOR_BANS_PER_HOUR 20)"
REMIND_HOURS="$(num MONITOR_REMIND_HOURS 6)"
DOMAIN="$(env_get DOMAIN)"
HOST="$(hostname 2>/dev/null || echo server)"

log() { printf '%s %s\n' "$(date '+%F %T')" "$*"; }

# --- sending -----------------------------------------------------------------
send_via_curl() {  # last resort when the app container cannot send: token from .env, given to curl on stdin
  local token chat
  token="$(env_get TELEGRAM_BOT_TOKEN)"; chat="$(env_get TELEGRAM_ADMIN_CHAT_ID)"
  [ -n "$token" ] && [ -n "$chat" ] || return 1
  printf 'url = "https://api.telegram.org/bot%s/sendMessage"\n' "$token" \
    | curl -fsS --max-time 20 -K - --data-urlencode "chat_id=$chat" \
        --data-urlencode "text=🚨 DZPLAY — تنبيه الخادم (${HOST})
$1" -o /dev/null
}

send() {
  local text="$1"
  if $COMPOSE exec -T app python -m app.admin_cli alert "$text" >/dev/null 2>&1; then
    log "sent: ${text//$'\n'/ | }"; return 0
  fi
  if send_via_curl "$text" 2>/dev/null; then
    log "sent (direct): ${text//$'\n'/ | }"; return 0
  fi
  log "NOT SENT (Telegram unreachable or not configured): ${text//$'\n'/ | }"
  return 1
}

# --- checks: each prints "key<TAB>message" for every current problem ------------
check_disk() {
  df -P / /var/lib/docker 2>/dev/null | awk -v max="$DISK_PCT" 'NR>1 && !seen[$1]++ {
      p=$5; sub("%","",p); if (p+0 >= max+0) printf "disk:%s\tالقرص %s ممتلئ بنسبة %s%% (الحد %s%%)\n", $6, $6, p, max }'
}

check_load() {
  local cpus l5
  cpus="$(nproc 2>/dev/null || echo 1)"
  l5="$(awk '{print $2}' /proc/loadavg 2>/dev/null || echo 0)"
  awk -v l="$l5" -v c="$cpus" -v f="$LOAD_PER_CPU" 'BEGIN {
      if (l+0 > c*f) printf "load\tحمل مرتفع: %s خلال 5 دقائق (%d معالج، الحد %.1f)\n", l, c, c*f }'
}

check_mem() {
  awk -v max="$MEM_PCT" '/^MemTotal:/{t=$2} /^MemAvailable:/{a=$2} END {
      if (t>0) { used=int((t-a)*100/t); if (used >= max+0) printf "mem\tالذاكرة مستعملة بنسبة %d%% (الحد %s%%)\n", used, max } }' /proc/meminfo 2>/dev/null
}

check_containers() {
  local running
  if ! running="$($COMPOSE ps --status running --services 2>/dev/null)"; then
    printf 'docker\tDocker لا يستجيب: كل الخدمات قد تكون متوقفة\n'
    return
  fi
  for s in $SERVICES; do
    if ! grep -qx "$s" <<<"$running"; then
      # media-worker may be absent on old installs: only alert if the service is defined
      $COMPOSE config --services 2>/dev/null | grep -qx "$s" || continue
      printf 'svc:%s\tالخدمة %s متوقفة\n' "$s" "$s"
    fi
  done
  if grep -qx app <<<"$running" && grep -qx redis <<<"$running" && grep -qx media-worker <<<"$running"; then
    local mode
    mode="$($COMPOSE exec -T app printenv MEDIA_WORKER 2>/dev/null | tr -d '\r')"
    if [ "$mode" = "queue" ]; then
      [ "$($COMPOSE exec -T redis redis-cli EXISTS dz:media:worker 2>/dev/null | tr -dc '0-9')" = "1" ] \
        || printf 'worker\tعامل الوسائط لا يرسل نبضاته: رفع الصور والفيديو متوقف\n'
    fi
  fi
}

check_http() {
  [ -n "$DOMAIN" ] || return 0
  curl -fsS --max-time 15 --resolve "$DOMAIN:443:127.0.0.1" "https://$DOMAIN/healthz" -o /dev/null 2>/dev/null \
    || printf 'http\tالموقع لا يجيب: https://%s/healthz\n' "$DOMAIN"
}

check_bans() {
  [ -r "$F2B_LOG" ] || return 0
  local since n
  since="$(date -d '-1 hour' '+%Y-%m-%d %H:%M:%S' 2>/dev/null)" || return 0
  n="$(awk -v s="$since" 'substr($0,1,19) >= s && / Ban / {n++} END {print n+0}' "$F2B_LOG")"
  [ "$n" -ge "${BANS_PER_HOUR%.*}" ] && printf 'bans\tمحاولات اختراق متكررة: %s عنوان IP حُظر خلال ساعة (fail2ban)\n' "$n"
  return 0
}

all_checks() { check_disk; check_load; check_mem; check_containers; check_http; check_bans; }

# --- state: one file per open problem (first seen, last sent) -------------------
key_file() { printf '%s/%s' "$STATE" "$(printf '%s' "$1" | tr -c 'A-Za-z0-9_.-' '_')"; }

run_check() {
  mkdir -p "$STATE"; chmod 700 "$STATE"
  exec 9>"$STATE/.lock"; flock -n 9 || exit 0   # never two runs at once
  local now problems new="" again="" fixed="" k msg f first last
  now="$(date +%s)"
  problems="$(all_checks)"
  declare -A open=()
  while IFS=$'\t' read -r k msg; do
    [ -n "$k" ] || continue
    open["$k"]=1
    f="$(key_file "$k")"
    if [ ! -f "$f" ]; then
      printf '%s %s\t%s\n' "$now" "$now" "$msg" > "$f"
      new+="• $msg"$'\n'
    else
      read -r first last _ < "$f"
      if [ $(( now - last )) -ge $(( ${REMIND_HOURS%.*} * 3600 )) ]; then
        printf '%s %s\t%s\n' "$first" "$now" "$msg" > "$f"
        again+="• $msg (منذ $(( (now - first) / 60 )) دقيقة)"$'\n'
      fi
    fi
  done <<<"$problems"
  for f in "$STATE"/*; do
    [ -f "$f" ] || continue
    k="$(basename "$f")"
    local still=0
    for o in "${!open[@]}"; do [ "$(basename "$(key_file "$o")")" = "$k" ] && still=1; done
    if [ "$still" = 0 ]; then
      fixed+="• $(cut -f2- "$f")"$'\n'
      rm -f "$f"
    fi
  done
  local text=""
  [ -n "$new" ] && text+="مشكلة جديدة:"$'\n'"$new"
  [ -n "$again" ] && text+="ما زالت قائمة:"$'\n'"$again"
  [ -n "$fixed" ] && text+="✅ عاد طبيعيًا:"$'\n'"$fixed"
  [ -n "$text" ] && send "${text%$'\n'}"
  return 0
}

cmd_install() {
  [ "$(id -u)" -eq 0 ] || { echo "Run as root (sudo)." >&2; exit 1; }
  chmod 700 "$SELF"
  cat > /etc/cron.d/dzplay-monitor <<CRON
# DZPLAY: server monitoring with Telegram alerts (deploy/monitor.sh)
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
*/5 * * * * root $SELF >> /var/log/dzplay-monitor.log 2>&1
CRON
  chmod 644 /etc/cron.d/dzplay-monitor
  cat > /etc/logrotate.d/dzplay-monitor <<'ROTATE'
/var/log/dzplay-monitor.log {
  weekly
  rotate 8
  compress
  missingok
  notifempty
}
ROTATE
  echo "Monitoring installed: every 5 minutes (log: /var/log/dzplay-monitor.log)."
}

case "${1:-check}" in
  check) run_check ;;
  install) cmd_install ;;
  test) send "رسالة اختبار من المراقبة: إن وصلتك فالتنبيهات تعمل." ;;
  status) out="$(all_checks)"; [ -n "$out" ] && cut -f2- <<<"$out" || echo "OK: no problem found." ;;
  *) echo "usage: $0 [check | install | test | status]" >&2; exit 2 ;;
esac
