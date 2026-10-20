#!/usr/bin/env bash
# V6 phase 21: run load scenarios against STAGING and build the report. Never production.
#
#   APP_ENV=staging loadtest/run.sh [--quick|--full] [--label NAME] A B C D E F G     (or: all; S = a 1-minute smoke)
#
# Before anything: APP_ENV=staging must be set, staging must be up (loadtest/staging/up.sh) and must answer
# /healthz/env as staging (loadtest/lib/safety.py). Each scenario gets loadtest/results/<run>/<S>/:
#   summary.json (k6, client side), timeseries.csv (10 s buckets, k6sink), system.csv (2 s, collect.py),
#   pg_top.csv (20 slowest queries), server_endpoints.json (server-side timings), app_errors.txt, break.json (C)
# A run stops at once when the disk passes 85 %, a staging container stops, or the app logs an unexpected error.
#
# Where the load comes from (the collector and the sink always run next to staging):
#   here / the same server  k6 on this machine: pinned to K6_CPUS (default "2,3" when staging is pinned, see
#                           staging/local-2cpu.yml) or capped at K6_CPU_LIMIT cores (default 1).
#   a separate VPS          GEN_HOST=ssh://root@<generator> SERVER_IP=<this server's address> (phase 2, option 1):
#                           k6 runs there through Docker over SSH; staging (8443), the sink (8086) and the mock
#                           mailbox (9000) are opened to that address only (loadtest/phase2/firewall.sh).
# Other settings: STRESS_MAX, WS_STEP / WS_HOLD / WS_MAX (scenario F), STAGING_IMAGE, K6_IMAGE.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

[ "${APP_ENV:-}" = "staging" ] || { echo "✗ REFUSED: set APP_ENV=staging (load tests touch staging only)"; exit 2; }
PACE=quick LABEL=run SCENARIOS=()
while [ $# -gt 0 ]; do
    case "$1" in
        --quick) PACE=quick ;;
        --full) PACE=full ;;
        --label) LABEL="$2"; shift ;;
        all) SCENARIOS+=(A B C D E F G) ;;
        [A-Ga-gSs]) SCENARIOS+=("$(echo "$1" | tr a-gs A-GS)") ;;
        *) echo "unknown argument: $1"; exit 2 ;;
    esac
    shift
done
[ ${#SCENARIOS[@]} -gt 0 ] || { echo "usage: APP_ENV=staging loadtest/run.sh [--quick|--full] [--label NAME] A B C D E F G | all"; exit 2; }

PROJECT=dzstaging
NET=${PROJECT}_default
IMAGE=${STAGING_IMAGE:-dzstaging-app:latest}
K6_IMAGE=${K6_IMAGE:-grafana/k6:1.3.0}
GEN_HOST=${GEN_HOST:-}
SERVER_IP=${SERVER_IP:-}
OUT="$HERE/out"
TOKEN=$(grep '^LOADTEST_TOKEN=' "$OUT/staging.env" 2>/dev/null | cut -d= -f2-) || true
[ -n "$TOKEN" ] || { echo "✗ staging is not set up (loadtest/staging/up.sh)"; exit 2; }
compose() { docker compose -p "$PROJECT" -f "$HERE/staging/docker-compose.yml" "$@"; }

# CPUs of the helpers (collector, sink, one-off checks) and of a local generator: away from the server's when pinned
if [ -n "$(docker inspect -f '{{.HostConfig.CpusetCpus}}' ${PROJECT}-app-1 2>/dev/null)" ]; then
    LOCAL_CPU=(--cpuset-cpus "${K6_CPUS:-2,3}")
else
    LOCAL_CPU=(--cpus "${K6_CPU_LIMIT:-1}")
fi
in_net() {  # a one-off container of the staging image on the staging network
    docker run --rm --no-healthcheck --network "$NET" "${LOCAL_CPU[@]}" -e APP_ENV=staging -e LOADTEST_TOKEN="$TOKEN" \
        -v "$HERE:/app/loadtest:ro" "$@"
}

if [ -n "$GEN_HOST" ]; then  # phase 2, option 1: the generator is another machine
    [[ "$SERVER_IP" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || { echo "✗ GEN_HOST needs SERVER_IP (this server's IPv4)"; exit 2; }
    gen() { docker -H "$GEN_HOST" "$@"; }
    GEN_RUN=(--add-host "staging.test:$SERVER_IP" -v dzlt:/lt -v dzlt-results:/results)
    SINK_URL=http://$SERVER_IP:8086/k6
    MOCKS_URL=http://$SERVER_IP:9000
    SINK_PORT=(-p "$SERVER_IP:8086:8086")
    echo "• copying the scenarios and the test accounts to the generator"
    tar -C "$HERE" -c k6 ws lib out/vu_users.csv out/meta.json \
        | gen run --rm -i -v dzlt:/lt --entrypoint sh "$K6_IMAGE" -c 'rm -rf /lt/* && tar -x -C /lt'
    gen run --rm -v dzlt-results:/results --user 0 --entrypoint sh "$K6_IMAGE" -c 'chmod 0777 /results'
else
    gen() { docker "$@"; }
    GEN_RUN=(--network "$NET" "${LOCAL_CPU[@]}" -v "$HERE:/lt:ro")
    SINK_URL=http://${PROJECT}-k6sink:8086/k6
    MOCKS_URL=http://mocks:9000
    SINK_PORT=()
fi

echo "• safety: is https://staging.test:8443 staging?"
in_net "$IMAGE" python /app/loadtest/lib/safety.py target https://staging.test:8443
[ -s "$OUT/vu_users.csv" ] || { echo "• seeding (50,000 users)…"; compose exec -T -e APP_ENV=staging app python /app/loadtest/seed/seed.py; }

RUN="$HERE/results/$(date -u +%Y%m%d-%H%M)-$LABEL"
mkdir -p "$RUN"
cat > "$RUN/meta.json" <<EOF
{"label": "$LABEL", "pace": "$PACE", "started": "$(date -u +%FT%TZ)", "scenarios": "${SCENARIOS[*]}",
 "web_concurrency": "${WEB_CONCURRENCY:-1}", "git": "$(git -C "$HERE" rev-parse --short HEAD 2>/dev/null || echo -)",
 "generator": "$([ -n "$GEN_HOST" ] && echo separate || echo local)",
 "server_cpus": "$(docker inspect -f '{{.HostConfig.CpusetCpus}}' ${PROJECT}-app-1 2>/dev/null)",
 "server_memory": "$(docker inspect -f '{{.HostConfig.Memory}}' ${PROJECT}-app-1 2>/dev/null)", "seed": $(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); d.pop("password", None); print(json.dumps(d))' "$OUT/meta.json")}
EOF

cleanup() {
    docker rm -f ${PROJECT}-k6sink ${PROJECT}-collector >/dev/null 2>&1 || true
    gen rm -f ${PROJECT}-k6 ${PROJECT}-wsflood >/dev/null 2>&1 || true
}
trap cleanup EXIT

# LT_KNOWN_ERRORS: a regex of errors already found and understood, when measuring the code from before their fix
# (e.g. LT_KNOWN_ERRORS=FileNotFoundError for the media-cache race fixed in phase 21) — never set on the server.
unexpected_errors() {  # app tracebacks other than the expected overload ones (pool exhausted, too many connections)
    compose logs --no-color --since "$1" app 2>/dev/null | grep -A30 'Traceback' \
        | grep -E '^\S*app-1\s+\|\s+[A-Za-z_.]+(Error|Exception):' \
        | grep -vE 'QueuePool limit|TimeoutError|too many (clients|connections)|ClientDisconnect|CancelledError' \
        | grep -vE "${LT_KNOWN_ERRORS:-^$}" || true
}

run_one() {
    local S=$1 R="$RUN/$1" since
    install -d -m 0777 "$R"
    since=$(date -u +%FT%TZ)
    echo "━━ scenario $S ($PACE) → $R"
    in_net -v "$R:/results" "$IMAGE" python /app/loadtest/monitor/collect.py pg-reset
    docker run -d --no-healthcheck --name ${PROJECT}-collector --network "$NET" "${LOCAL_CPU[@]}" --user 0 \
        -e APP_ENV=staging -e LOADTEST_TOKEN="$TOKEN" -e COMPOSE_PROJECT=$PROJECT \
        -v /var/run/docker.sock:/var/run/docker.sock -v "$HERE:/app/loadtest:ro" -v "$R:/results" \
        "$IMAGE" python /app/loadtest/monitor/collect.py run >/dev/null
    local breaks=()
    if [ "$S" = C ]; then  # the breaking point: break.json (and, for a local k6, its REST API is asked to stop)
        breaks=(-e BREAK_P95_MS=3000 -e BREAK_ERR_PCT=5)
        [ -z "$GEN_HOST" ] && breaks+=(-e K6_API=http://${PROJECT}-k6:6565)
    fi
    docker run -d --no-healthcheck --name ${PROJECT}-k6sink --network "$NET" "${LOCAL_CPU[@]}" "${SINK_PORT[@]}" --user 0 "${breaks[@]}" \
        -v "$HERE:/app/loadtest:ro" -v "$R:/results" "$IMAGE" python /app/loadtest/monitor/k6sink.py >/dev/null
    sleep 3
    local t0 status=0 box
    t0=$(date +%s)
    if [ "$S" = F ]; then
        box=${PROJECT}-wsflood
        local flood=(python /app/loadtest/ws/wsflood.py --users /app/loadtest/out/vu_users.csv --out /results
                     --step "${WS_STEP:-1000}" --hold "${WS_HOLD:-40}" --max "${WS_MAX:-19000}")
        if [ -n "$GEN_HOST" ]; then  # the generator has no staging image: a plain Python with websockets
            gen run -d --name $box --add-host "staging.test:$SERVER_IP" -v dzlt:/app/loadtest -v dzlt-results:/results \
                -e APP_ENV=staging -e LOADTEST_TOKEN="$TOKEN" -e LT_RESOLVE="$SERVER_IP" python:3.11-slim \
                sh -c "pip install -q 'websockets>=13' && python /app/loadtest/ws/wsflood.py --users /app/loadtest/out/vu_users.csv \
                --out /results --step ${WS_STEP:-1000} --hold ${WS_HOLD:-40} --max ${WS_MAX:-19000}" >/dev/null
        else
            docker run -d --no-healthcheck --name $box --network "$NET" "${LOCAL_CPU[@]}" -e APP_ENV=staging -e LOADTEST_TOKEN="$TOKEN" \
                -v "$HERE:/app/loadtest:ro" -v "$R:/results" "$IMAGE" "${flood[@]}" >/dev/null
        fi
    else
        box=${PROJECT}-k6
        gen run -d --name $box "${GEN_RUN[@]}" $([ -z "$GEN_HOST" ] && echo "-v $R:/results") \
            -e LOADTEST_TOKEN="$TOKEN" -e SCENARIO="$S" -e PACE="$PACE" -e STRESS_MAX="${STRESS_MAX:-8000}" \
            -e VU_USERS=/lt/out/vu_users.csv -e SEED_META=/lt/out/meta.json -e PROFILE=/lt/k6/profile.json \
            -e SUMMARY_OUT=/results/summary.json -e MOCKS_URL="$MOCKS_URL" \
            -e K6_INFLUXDB_PUSH_INTERVAL=2s -e K6_INFLUXDB_CONCURRENT_WRITES=4 \
            "$K6_IMAGE" run --quiet --no-color --address 0.0.0.0:6565 --out "influxdb=$SINK_URL" /lt/k6/main.js >/dev/null
    fi
    while [ "$(gen inspect -f '{{.State.Running}}' $box 2>/dev/null)" = "true" ]; do
        sleep 10
        local bad
        bad=$(unexpected_errors "$since" | head -3)
        if [ -n "$bad" ] && [ ! -e "$R/STOP" ]; then echo "unexpected app error: $bad" > "$R/STOP"; fi
        if [ -e "$R/STOP" ]; then
            echo "✗ STOP: $(cat "$R/STOP")"
            gen stop -t 60 $box >/dev/null 2>&1 || true
            status=1
        elif [ -e "$R/break.json" ] && [ -n "$GEN_HOST" ]; then  # breaking point reached: stop the remote k6 (summary kept)
            gen stop -t 60 $box >/dev/null 2>&1 || true
        fi
    done
    gen logs $box > "$R/generator.log" 2>&1 || true
    gen rm -f $box >/dev/null 2>&1 || true
    if [ -n "$GEN_HOST" ]; then  # the generator's result files come back here
        for f in summary.json wsflood.json wsflood.csv; do
            gen run --rm -v dzlt-results:/results --entrypoint sh "$K6_IMAGE" -c "cat /results/$f 2>/dev/null && rm -f /results/$f" \
                > "$R/$f" || true
            [ -s "$R/$f" ] || rm -f "$R/$f"
        done
    fi
    local minutes=$(( ( $(date +%s) - t0 ) / 60 + 1 ))
    docker exec ${PROJECT}-k6sink python -c "import urllib.request; urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8086/flush', method='POST'))" || true
    in_net -v "$R:/results" "$IMAGE" python /app/loadtest/monitor/collect.py pg-top
    in_net -v "$R:/results" "$IMAGE" python /app/loadtest/monitor/collect.py app-endpoints --minutes $(( minutes > 180 ? 180 : minutes ))
    unexpected_errors "$since" > "$R/app_errors.txt"
    compose logs --no-color --since "$since" app 2>/dev/null | grep -cE 'Traceback' > "$R/app_tracebacks.txt" || true
    docker stop -t 5 ${PROJECT}-collector ${PROJECT}-k6sink >/dev/null 2>&1 || true
    docker rm -f ${PROJECT}-collector ${PROJECT}-k6sink >/dev/null 2>&1 || true
    in_net "$IMAGE" python -c "import urllib.request,json; print('mock calls:', urllib.request.urlopen('http://mocks:9000/stats').read().decode())" \
        | tee "$R/mock_calls.txt"
    return $status
}

for S in "${SCENARIOS[@]}"; do
    if ! run_one "$S"; then echo "✗ the run stopped at scenario $S"; break; fi
    [ "$S" = "${SCENARIOS[-1]}" ] || { echo "• cooling down 60 s"; sleep 60; }
done
python3 "$HERE/report/build.py" "$RUN" || echo "(report: see loadtest/report/build.py)"
echo "✓ results: $RUN"
