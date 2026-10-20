#!/usr/bin/env bash
# V6 phase 21: stop the STAGING stack and delete ALL its data (database, cache, certificates, the generated secrets
# and session tokens). Production (project dzplay) is never touched.
set -euo pipefail
cd "$(dirname "$0")"
docker compose -p dzstaging -f docker-compose.yml down --volumes --remove-orphans
rm -f ../out/staging.env ../out/staging-prod.env ../out/tunables.tsv ../out/vu_users.csv ../out/meta.json \
      ../out/seed.dump ../out/seed-vu_users.csv ../out/seed-meta.json
echo "✓ staging removed (containers, volumes, secrets)"
