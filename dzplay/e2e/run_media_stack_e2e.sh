#!/usr/bin/env bash
# V5: the isolated media worker in a THROWAWAY Docker stack (own project name and volumes).
#
#   e2e/run_media_stack_e2e.sh            (STACK_EXTRA=dir: extra files, e.g. an offline-build override)
#
# Checks: worker runs as uid 10002 with no secrets in its environment, read-only root, no capabilities,
# no internet; a picture with GPS EXIF written by the app into the shared tmpfs is processed through
# Redis (re-encoded, metadata gone, NSFW scored); a fake file is refused; the temp area ends empty.
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
PROJECT="dzplay-media-e2e-$$"
export COMPOSE="docker compose -p $PROJECT"
cleanup() { (cd "$WORK/stack" && $COMPOSE down -v >/dev/null 2>&1) || true; rm -rf "$WORK"; }
trap cleanup EXIT

mkdir "$WORK/stack"
tar -C "$SRC" --exclude=.venv --exclude=.env --exclude=__pycache__ --exclude='e2e/screenshots*' -cf - . | tar -C "$WORK/stack" -xf -
if [ -n "${STACK_EXTRA:-}" ]; then cp -r "$STACK_EXTRA"/. "$WORK/stack/"; fi
cd "$WORK/stack"
cat > .env <<ENV
ENV=production
SECRET_KEY=$(openssl rand -hex 32)
POSTGRES_PASSWORD=$(openssl rand -hex 16)
DOMAIN=localhost
PUBLIC_URL=https://localhost
TELEGRAM_BOT_TOKEN=123456:never-given-to-the-worker-000000000000
ENV
chmod 600 .env
pass() { printf '  ✓ %s\n' "$*"; }
fail() { printf '  ✗ %s\n' "$*" >&2; exit 1; }

$COMPOSE up -d --build app media-worker >/dev/null 2>&1 </dev/null || fail "stack did not start"
for _ in $(seq 1 60); do
  $COMPOSE exec -T app python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')" >/dev/null 2>&1 && break
  sleep 2
done
w="$($COMPOSE ps -q media-worker)"
[ "$(docker inspect -f '{{.HostConfig.ReadonlyRootfs}} {{.HostConfig.CapDrop}}' "$w")" = "true [ALL]" ] || fail "worker not hardened"
[ "$($COMPOSE exec -T media-worker id -u)" = 10002 ] || fail "worker uid"
$COMPOSE exec -T media-worker env | grep -qiE 'SECRET|TOKEN|POSTGRES|DATABASE' && fail "secrets in the worker environment"
$COMPOSE exec -T media-worker python -c "
import socket; s=socket.socket(); s.settimeout(4)
try: s.connect(('149.154.167.220', 443)); print('ONLINE')
except OSError: print('offline')" | grep -q offline || fail "worker can reach the internet"
pass "worker: uid 10002, read-only, no capabilities, no secrets, no internet"

$COMPOSE exec -T app python - <<'PY' || fail "processing through the worker"
import io, json, time, uuid
from pathlib import Path
import redis
from PIL import Image
from app.services.media_pipeline import HEARTBEAT, QUEUE, RESULT

r = redis.Redis.from_url("redis://redis:6379/0")
for _ in range(60):
    if r.exists(HEARTBEAT): break
    time.sleep(1)
assert r.exists(HEARTBEAT), "no worker heartbeat"
tmp = Path("/uploads")

def job(data, kind="image", types=("jpeg", "png", "webp", "heic")):
    jid = uuid.uuid4().hex
    (tmp / f"{jid}.in").write_bytes(data)
    r.lpush(QUEUE, json.dumps({"id": jid, "kind": kind, "types": list(types), "min_side": 64, "max_side": 12000,
                               "max_pixels": 40000000, "nsfw": True, "nsfw_block": 0.7, "nsfw_sexy": 0.92, "blur": True}))
    got = r.brpop(RESULT + jid, timeout=120)
    assert got, "no result"
    return jid, json.loads(got[1])

im = Image.new("RGB", (1600, 1200), (200, 60, 40))
exif = Image.Exif(); exif[0x010F] = "PhoneMaker"; exif[0x8825] = {1: "N", 2: (36.0, 49.0, 0.0)}
buf = io.BytesIO(); im.save(buf, "JPEG", exif=exif)
jid, res = job(buf.getvalue())
assert res["ok"] and res["nsfw"] < 0.5 and res["blur"].startswith("data:image/jpeg"), res
out = (tmp / f"{jid}.img.webp").read_bytes()
assert b"PhoneMaker" not in out and not Image.open(io.BytesIO(out)).getexif()
assert not (tmp / f"{jid}.in").exists()
print(f"    image ok in {res['ms']} ms (NSFW {res.get('nsfw_ms')} ms)")
for p in tmp.glob(f"{jid}*"): p.unlink()
jid, res = job(b"<html>not a picture</html>" * 30)
assert not res["ok"] and res["code"] == "bad_type", res
assert not list(tmp.iterdir()), list(tmp.iterdir())
print("    fake file refused, temp area empty")
PY
pass "app → Redis → worker → app: re-encoded, metadata stripped, NSFW scored; fake refused; tmpfs empty"
echo "MEDIA STACK E2E PASSED"
