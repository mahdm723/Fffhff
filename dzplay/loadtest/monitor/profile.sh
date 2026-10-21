#!/usr/bin/env bash
# V6 phase 21: where the staging app spends its CPU under load — py-spy, run from a throwaway container that shares the
# app container's processes (the app itself keeps no capability). Run it while a scenario is going:
#   loadtest/monitor/profile.sh [SECONDS] [OUT_DIR]        → OUT_DIR/profile.txt (stacks) + profile-top.txt (summary)
# PYSPY_IMAGE: an image with py-spy (default python:3.11-slim, which installs it first — needs internet).
set -euo pipefail
SECS=${1:-60}
OUT=$(realpath -m "${2:-.}")
IMAGE=${PYSPY_IMAGE:-python:3.11-slim}
docker inspect dzstaging-app-1 >/dev/null 2>&1 || { echo "✗ no staging app (dzstaging-app-1)"; exit 2; }
mkdir -p "$OUT" && chmod 0777 "$OUT"
docker run --rm --pid=container:dzstaging-app-1 --cap-add SYS_PTRACE --user 0 --network none -v "$OUT:/out" "$IMAGE" sh -c "
    command -v py-spy >/dev/null || pip install -q py-spy
    py-spy record --pid 1 --subprocesses --idle --duration $SECS --rate 50 --nonblocking --format raw -o /out/profile.txt >/dev/null"
python3 - "$OUT/profile.txt" > "$OUT/profile-top.txt" <<'PY'
import collections, re, sys
own, incl, total, idle = collections.Counter(), collections.Counter(), 0, 0
for line in open(sys.argv[1], encoding="utf-8", errors="replace"):
    stack, _, n = line.rstrip().rpartition(" ")
    if not n.isdigit():
        continue
    n, frames = int(n), stack.split(";")
    total += n
    if any(k in frames[-1] for k in ("select (", "epoll", "wait (threading", "_worker (concurrent", "accept (")):
        idle += n
        continue
    own[re.sub(r" \(.*/site-packages/", " (", frames[-1])] += n
    for f in {f for f in frames if "/app/app/" in f}:
        incl[f.split("/app/app/")[-1].rstrip(")")] += n
busy = max(total - idle, 1)
print(f"samples {total}, waiting {100 * idle / max(total, 1):.0f} %\n\n== own time (where the CPU is) ==")
for f, n in own.most_common(25):
    print(f"{100 * n / busy:5.1f} %  {f[:150]}")
print("\n== the app's functions (time inside, including what they call) ==")
for f, n in incl.most_common(25):
    print(f"{100 * n / busy:5.1f} %  {f[:150]}")
PY
echo "✓ $OUT/profile-top.txt"
