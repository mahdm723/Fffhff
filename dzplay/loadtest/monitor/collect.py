"""V6 phase 21: what the server does during a load test, every 2 seconds → <results>/system.csv (t, source, key, value).

* per staging container (Docker API, one-shot stats): CPU (% of one core), memory (MB, without the page cache), network
* the machine: load average, memory used %, disk used % (the results directory's filesystem = the server's disk)
* PostgreSQL: connections by state, waiting locks, transactions/s, cache hit %, temp files, deadlocks, size
* Redis: memory, clients, operations/s
* the app itself (/healthz/metrics, staging only): requests/s, p50/p95/p99, 5xx, open WebSockets, the pool
Guards (the run stops — loadtest/run.sh watches <results>/STOP): disk above 85 %, a staging container that stopped.

Runs in the staging app image on the staging network, with the Docker socket (read-only use) mounted:
    python /app/loadtest/monitor/collect.py run            # until stopped (SIGTERM)
    python /app/loadtest/monitor/collect.py pg-reset       # pg_stat_statements_reset() before a scenario
    python /app/loadtest/monitor/collect.py pg-top         # the 20 slowest queries (total time) → pg_top.csv
    python /app/loadtest/monitor/collect.py app-endpoints --minutes N   # server-side timings per endpoint → JSON
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import signal
import sys
import time
from pathlib import Path

import httpx
import psycopg
import redis

RESULTS = Path(os.environ.get("RESULTS", "/results"))
PROJECT = os.environ.get("COMPOSE_PROJECT", "dzstaging")
DSN = os.environ.get("STAGING_DSN", "postgresql://dzplay:staging@db:5432/dzplay_staging")
REDIS_URL = os.environ.get("STAGING_REDIS", "redis://redis:6379/0")
APP_URL = os.environ.get("APP_URL", "https://staging.test:8443")
TOKEN = os.environ.get("LOADTEST_TOKEN", "")
DISK_STOP_PCT = float(os.environ.get("DISK_STOP_PCT", "85"))
running = True


def _docker() -> httpx.Client:
    return httpx.Client(transport=httpx.HTTPTransport(uds="/var/run/docker.sock"), base_url="http://docker", timeout=10)


def containers(d: httpx.Client) -> dict[str, dict]:
    flt = json.dumps({"label": [f"com.docker.compose.project={PROJECT}"]})
    out = {}
    for c in d.get("/containers/json", params={"all": "true", "filters": flt}).json():
        out[c["Labels"].get("com.docker.compose.service", c["Names"][0])] = {"id": c["Id"], "state": c["State"]}
    return out


class Sampler:
    def __init__(self) -> None:
        self.prev: dict[str, tuple[float, float, float, float]] = {}
        self.pg_prev: tuple[float, ...] | None = None
        self.redis_prev: tuple[float, float] | None = None

    def docker_rows(self, d: httpx.Client, now: float) -> tuple[list, list[str]]:
        rows, stopped = [], []
        for svc, c in containers(d).items():
            if c["state"] != "running":
                stopped.append(svc)
                continue
            s = d.get(f"/containers/{c['id']}/stats", params={"stream": "false", "one-shot": "true"}).json()
            cpu = s.get("cpu_stats", {}).get("cpu_usage", {}).get("total_usage", 0) / 1e9
            mem = s.get("memory_stats", {})
            st = mem.get("stats", {})
            used = mem.get("usage", 0) - st.get("inactive_file", st.get("total_inactive_file", 0))
            net = s.get("networks") or {}
            rx = sum(n.get("rx_bytes", 0) for n in net.values())
            tx = sum(n.get("tx_bytes", 0) for n in net.values())
            if svc in self.prev:
                t0, cpu0, rx0, tx0 = self.prev[svc]
                dt = max(now - t0, 1e-3)
                rows += [(svc, "cpu_pct", round(100 * (cpu - cpu0) / dt, 1)),
                         (svc, "net_rx_kbps", round((rx - rx0) / dt / 1024, 1)), (svc, "net_tx_kbps", round((tx - tx0) / dt / 1024, 1))]
            rows.append((svc, "mem_mb", round(used / 2**20, 1)))
            if mem.get("limit"):
                rows.append((svc, "mem_limit_mb", round(mem["limit"] / 2**20)))
            self.prev[svc] = (now, cpu, rx, tx)
        return rows, stopped

    @staticmethod
    def host_rows() -> list:
        load1, load5 = (float(x) for x in Path("/proc/loadavg").read_text().split()[:2])
        info = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, v = line.split(":", 1)
            info[k] = int(v.split()[0])
        mem_pct = 100 * (1 - info.get("MemAvailable", 0) / max(info.get("MemTotal", 1), 1))
        fs = os.statvfs(RESULTS)
        used = fs.f_blocks - fs.f_bfree  # like df: used / (used + available to users)
        disk_pct = 100 * used / max(used + fs.f_bavail, 1)
        return [("host", "load1", load1), ("host", "load5", load5), ("host", "cpus", os.cpu_count()),
                ("host", "mem_used_pct", round(mem_pct, 1)), ("host", "disk_used_pct", round(disk_pct, 1))]

    def pg_rows(self, cur, now: float) -> list:
        rows = []
        cur.execute("SELECT coalesce(state, 'other'), count(*) FROM pg_stat_activity WHERE datname = current_database() "
                    "AND pid <> pg_backend_pid() GROUP BY 1")
        states = dict(cur.fetchall())
        for k in ("active", "idle", "idle in transaction"):
            rows.append(("pg", "conn_" + k.replace(" ", "_"), states.get(k, 0)))
        rows.append(("pg", "conn_total", sum(states.values())))
        cur.execute("SELECT count(*) FROM pg_locks WHERE NOT granted")
        rows.append(("pg", "locks_waiting", cur.fetchone()[0]))
        cur.execute("SELECT xact_commit + xact_rollback, blks_hit, blks_read, temp_files, deadlocks, tup_returned, "
                    "tup_fetched FROM pg_stat_database WHERE datname = current_database()")
        cur_v = tuple(float(x) for x in cur.fetchone())
        if self.pg_prev:
            t0, *old = self.pg_prev
            dt = max(now - t0, 1e-3)
            d = [a - b for a, b in zip(cur_v, old)]
            rows += [("pg", "tps", round(d[0] / dt, 1)),
                     ("pg", "cache_hit_pct", round(100 * d[1] / (d[1] + d[2]), 2) if d[1] + d[2] else 100.0),
                     ("pg", "temp_files", d[3]), ("pg", "deadlocks", d[4]), ("pg", "rows_read_per_s", round((d[5] + d[6]) / dt))]
        self.pg_prev = (now, *cur_v)
        return rows

    def redis_rows(self, r, now: float) -> list:
        info = r.info()
        rows = [("redis", "used_mb", round(info["used_memory"] / 2**20, 1)), ("redis", "clients", info["connected_clients"])]
        cmds = float(info.get("total_commands_processed", 0))
        if self.redis_prev:
            rows.append(("redis", "ops", round((cmds - self.redis_prev[1]) / max(now - self.redis_prev[0], 1e-3), 1)))
        self.redis_prev = (now, cmds)
        return rows


def _app(minutes: int = 1) -> dict | None:
    from loadtest.lib.safety import _fetch  # same SNI/Host handling as the safety check

    d = _fetch(f"{APP_URL}/healthz/metrics?minutes={minutes}", TOKEN)
    return d if isinstance(d, dict) else None


def app_rows() -> list:
    d = _app(1)
    if not d:
        return [("server", "unreachable", 1)]
    t = d["metrics"]["totals"]
    db = d.get("database") or {}
    rows = [("server", "rps_1m", t["rps"]), ("server", "p50_1m", t["p50_ms"]), ("server", "p95_1m", t["p95_ms"]),
            ("server", "p99_1m", t["p99_ms"]), ("server", "errors_1m", t["errors"]),
            ("server", "ws_open", d["metrics"].get("websockets", 0))]
    for k in ("pool_checkedout", "pool_overflow", "pool_size"):  # the pool of the worker that answered
        if k in db:
            rows.append(("server", k, db[k]))
    return rows


def run(interval: float) -> None:
    signal.signal(signal.SIGTERM, lambda *_: globals().__setitem__("running", False))
    RESULTS.mkdir(parents=True, exist_ok=True)
    path = RESULTS / "system.csv"
    new = not path.exists()
    sampler, d = Sampler(), _docker()
    conn = psycopg.connect(DSN, autocommit=True)
    r = redis.Redis.from_url(REDIS_URL, socket_timeout=2)
    tick = 0
    with path.open("a", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["t", "source", "key", "value"])
        while running:
            start = time.time()
            rows: list = []
            try:
                drows, stopped = sampler.docker_rows(d, start)
                rows += drows + sampler.host_rows()
                with conn.cursor() as cur:
                    rows += sampler.pg_rows(cur, start)
                rows += sampler.redis_rows(r, start)
                if tick % 3 == 0:
                    rows += app_rows()
            except Exception as exc:  # noqa: BLE001 - the collector keeps going; the gap shows in the charts
                rows.append(("collector", "error", 1))
                print(f"collector: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
                stopped = []
            for src, key, value in rows:
                w.writerow([round(start, 1), src, key, value])
            fh.flush()
            disk = next((v for s, k, v in rows if s == "host" and k == "disk_used_pct"), 0)
            why = []
            if disk > DISK_STOP_PCT:
                why.append(f"disk {disk:.0f} % > {DISK_STOP_PCT:.0f} %")
            if stopped and any(s in stopped for s in ("app", "db", "redis", "caddy")):
                why.append("stopped: " + ", ".join(stopped))
            if why and not (RESULTS / "STOP").exists():
                (RESULTS / "STOP").write_text("; ".join(why) + "\n")
                print("STOP: " + "; ".join(why), flush=True)
            tick += 1
            time.sleep(max(0.0, interval - (time.time() - start)))


def pg_reset() -> None:
    with psycopg.connect(DSN, autocommit=True) as c, c.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements")
        cur.execute("SELECT pg_stat_statements_reset()")
        cur.execute("SELECT pg_stat_reset()")
    print("pg_stat_statements reset")


def pg_top(n: int) -> None:
    with psycopg.connect(DSN, autocommit=True) as c, c.cursor() as cur:
        cur.execute("""SELECT calls, round(total_exec_time::numeric, 1), round(mean_exec_time::numeric, 3),
                              round(max_exec_time::numeric, 1), rows, shared_blks_hit, shared_blks_read, temp_blks_written,
                              regexp_replace(query, '\\s+', ' ', 'g')
                       FROM pg_stat_statements WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
                       ORDER BY total_exec_time DESC LIMIT %s""", (n,))
        rows = cur.fetchall()
    with (RESULTS / "pg_top.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["calls", "total_ms", "mean_ms", "max_ms", "rows", "blks_hit", "blks_read", "temp_blks", "query"])
        for row in rows:
            w.writerow([*row[:-1], row[-1][:1500]])
    print(f"{len(rows)} queries → {RESULTS / 'pg_top.csv'}")


def app_endpoints(minutes: int) -> None:
    d = _app(minutes)
    (RESULTS / "server_endpoints.json").write_text(json.dumps(d, indent=1, ensure_ascii=False))
    print(f"server timings ({minutes} min) → {RESULTS / 'server_endpoints.json'}")


if __name__ == "__main__":
    sys.path.insert(0, "/app")
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run").add_argument("--interval", type=float, default=2.0)
    sub.add_parser("pg-reset")
    sub.add_parser("pg-top").add_argument("-n", type=int, default=20)
    sub.add_parser("app-endpoints").add_argument("--minutes", type=int, default=15)
    a = p.parse_args()
    {"run": lambda: run(a.interval), "pg-reset": pg_reset, "pg-top": lambda: pg_top(a.n),
     "app-endpoints": lambda: app_endpoints(a.minutes)}[a.cmd]()
