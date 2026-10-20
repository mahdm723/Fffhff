#!/usr/bin/env bash
# V6 phase 21: the seeded staging database, saved once and put back before each measured run, so "before" and "after"
# run on the same data (the scenarios add ideas, comments, messages, sessions…).
#   loadtest/staging/snapshot.sh save       → loadtest/out/seed.dump (+ the generator's files)
#   loadtest/staging/snapshot.sh restore    → database back to the dump, Redis emptied (rate limits, caches), app restarted
# Staging only: the database must be the marked *_staging one (loadtest/lib/safety.py).
set -euo pipefail
cd "$(dirname "$0")"
OUT=../out
compose() { docker compose -p dzstaging -f docker-compose.yml "$@"; }
marked() { compose exec -T db psql -U dzplay -d dzplay_staging -Atc "SELECT value FROM app_settings WHERE key = 'loadtest.staging'"; }
[ "$(marked)" = "1" ] || { echo "✗ REFUSED: not the marked staging database"; exit 2; }
case "${1:-}" in
    save)
        compose exec -T db pg_dump -U dzplay -d dzplay_staging -Fc -Z 3 > "$OUT/seed.dump"
        cp "$OUT/vu_users.csv" "$OUT/seed-vu_users.csv"; cp "$OUT/meta.json" "$OUT/seed-meta.json"
        echo "✓ saved $(du -h "$OUT/seed.dump" | cut -f1)"
        ;;
    restore)
        [ -s "$OUT/seed.dump" ] || { echo "no $OUT/seed.dump (snapshot.sh save)"; exit 2; }
        compose stop app media-worker >/dev/null
        compose exec -T db psql -U dzplay -d postgres -qc "DROP DATABASE IF EXISTS dzplay_staging WITH (FORCE)" \
            -c "CREATE DATABASE dzplay_staging OWNER dzplay"
        compose exec -T db pg_restore -U dzplay -d dzplay_staging --no-owner -j 2 < "$OUT/seed.dump" 2>/dev/null \
            || compose exec -T db pg_restore -U dzplay -d dzplay_staging --no-owner < "$OUT/seed.dump"
        compose exec -T db psql -U dzplay -d dzplay_staging -qc "ANALYZE"
        cp "$OUT/seed-vu_users.csv" "$OUT/vu_users.csv"; cp "$OUT/seed-meta.json" "$OUT/meta.json"
        compose exec -T redis redis-cli FLUSHALL >/dev/null
        compose start app media-worker >/dev/null
        for _ in $(seq 1 60); do
            compose exec -T app python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')" 2>/dev/null && break
            sleep 2
        done
        compose exec -T app sh -c 'find /data/media-cache -mindepth 1 -delete' || true  # pictures: a cold cache each run
        echo "✓ staging database restored, Redis and the picture cache emptied, app restarted"
        ;;
    *) echo "usage: $0 save | restore"; exit 2 ;;
esac
