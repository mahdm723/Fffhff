#!/usr/bin/env bash
# V6 phase 21 (phase 2, the end of the window): is production really back? Read only — changes nothing.
#   sudo loadtest/phase2/check-production.sh [--internal]
#   1. /healthz inside the app container (the app, its database and Redis answer)
#   2. the login path: a proof-of-work challenge, then a login with an address that cannot exist → 401
#      "invalid_credentials" (the API, the database, the Argon2 check and the anti-bot all ran; no account is touched)
#   3. (unless --internal) https://<DOMAIN>/healthz from outside, through Caddy
# Last check, by a person: log in from the phone.
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$APP_DIR"
env_get() { [ -f .env ] && grep -E "^$1=" .env | tail -1 | cut -d= -f2- | sed 's/[[:space:]]*#.*$//; s/^"//; s/"$//' || true; }

docker compose exec -T app python - <<'PY'
import sys
import httpx
from app.security.pow import solve

ua = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
      "Version/18.1 Mobile/15E148 Safari/604.1")
h = {"User-Agent": ua, "X-DZ-Requested": "1", "Accept": "application/json", "Origin": "http://127.0.0.1:8000"}
with httpx.Client(base_url="http://127.0.0.1:8000", headers=h, timeout=20) as c:
    r = c.get("/healthz")
    print(f"healthz: {r.status_code}")
    if r.status_code != 200:
        sys.exit(1)
    ch = c.post("/api/auth/challenge", json={"purpose": "login"}).json()
    r = c.post("/api/auth/login", json={"email": "production-check@invalid.invalid", "password": "not-a-password-1",
                                        "antibot": {**ch, "number": solve(ch)}})
    code = (r.json().get("error") or {}).get("code")
    print(f"login path: {r.status_code} {code}")
    sys.exit(0 if r.status_code == 401 and code == "invalid_credentials" else 1)
PY
echo "✓ app, database and login path answer"
if [ "${1:-}" != "--internal" ]; then
    domain="$(env_get APP_DOMAIN)"; domain="${domain:-$(env_get DOMAIN)}"
    curl -fsS --max-time 15 -o /dev/null -w "https://$domain/healthz: %{http_code}\n" "https://$domain/healthz"
fi
