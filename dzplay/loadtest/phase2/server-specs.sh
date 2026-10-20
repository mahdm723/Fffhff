#!/usr/bin/env bash
# V6 phase 21 (phase 2, step 0): what the server has — read only, prints no secret (no .env value except the
# performance settings listed below, no address).   sudo loadtest/phase2/server-specs.sh > specs.txt
set -uo pipefail
APP_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$APP_DIR"
h() { printf '\n== %s\n' "$*"; }
env_get() { [ -f .env ] && grep -E "^$1=" .env | tail -1 | cut -d= -f2- | sed 's/[[:space:]]*#.*$//' || true; }

h "CPU"; lscpu | grep -E '^(Model name|CPU\(s\)|Thread|Core|Socket|CPU max MHz|Hypervisor vendor|Virtualization type)' || nproc
h "memory"; free -h
h "disk"; df -h / /var/lib/docker 2>/dev/null | sort -u; lsblk -d -o NAME,SIZE,ROTA,TYPE 2>/dev/null
h "disk write speed (256 MB, fsync, removed after)"
dd if=/dev/zero of=/var/tmp/dz-speed.tmp bs=1M count=256 conv=fdatasync 2>&1 | tail -1; rm -f /var/tmp/dz-speed.tmp
h "network interfaces"; ip -br link 2>/dev/null | grep -v '^lo\|^veth\|^br-\|^docker'
for i in $(ls /sys/class/net | grep -vE '^(lo|veth|br-|docker)'); do printf '%s speed: %s Mb/s\n' "$i" "$(cat /sys/class/net/$i/speed 2>/dev/null || echo ?)"; done
h "system"; uname -sr; . /etc/os-release && echo "$PRETTY_NAME"; uptime
h "limits"; echo "fs.file-max $(cat /proc/sys/fs/file-max)"; echo "somaxconn $(cat /proc/sys/net/core/somaxconn)"
echo "local ports $(cat /proc/sys/net/ipv4/ip_local_port_range)"; echo "conntrack max $(cat /proc/sys/net/netfilter/nf_conntrack_max 2>/dev/null || echo -)"
h "docker"; docker version --format 'server {{.Server.Version}}'; docker info --format 'storage {{.Driver}} · cgroup v{{.CgroupVersion}} {{.CgroupDriver}} · {{.NCPU}} CPUs · {{.MemTotal}} bytes'
docker compose version
h "production containers"; docker compose ps --format 'table {{.Service}}\t{{.State}}\t{{.Status}}'
docker stats --no-stream --format 'table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}' | grep -v dzstaging
h "open files inside the app (WebSockets need one each)"
docker compose exec -T app sh -c 'echo "soft $(ulimit -Sn) · hard $(ulimit -Hn)"' 2>/dev/null || echo "app not running"
h "app performance settings (.env)"
for k in WEB_CONCURRENCY DB_POOL_SIZE DB_MAX_OVERFLOW PASSWORD_HASH_CONCURRENCY PASSWORD_HASH_QUEUE PASSWORD_HASH_WAIT_SECONDS AUTH_MAX_IN_FLIGHT WS_SESSION_RECHECK_SECONDS WS_PING_SECONDS \
         FEED_CANDIDATE_POOL FEED_CACHE_SECONDS MEDIA_CACHE_MAX_GB MEDIA_WORKER TLS_MODE; do
    v="$(env_get $k)"; printf '%-28s %s\n' "$k" "${v:-(default)}"
done
h "PostgreSQL"
docker compose exec -T db psql -U dzplay -d dzplay -At -c "SELECT 'version ' || current_setting('server_version')" \
  -c "SELECT 'max_connections ' || current_setting('max_connections') || ' · shared_buffers ' || current_setting('shared_buffers') || ' · work_mem ' || current_setting('work_mem') || ' · effective_cache_size ' || current_setting('effective_cache_size')" \
  -c "SELECT 'database size ' || pg_size_pretty(pg_database_size('dzplay'))" \
  -c "SELECT 'rows: users ' || (SELECT count(*) FROM users) || ' · posts ' || (SELECT count(*) FROM posts) || ' · comments ' || (SELECT count(*) FROM comments) || ' · reactions ' || (SELECT count(*) FROM post_reactions) || ' · messages ' || (SELECT count(*) FROM messages)" \
  -c "SELECT 'connections now ' || count(*) FROM pg_stat_activity" 2>/dev/null || echo "database not running"
h "Redis"; docker compose exec -T redis redis-cli info memory 2>/dev/null | grep -E '^used_memory_human|^maxmemory_human' || echo "redis not running"
h "backups"; ls -lh "$(env_get BACKUP_DIR | sed 's#^$#/var/backups/dzplay#')" 2>/dev/null | tail -3
