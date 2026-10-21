"""V6 phase 21: receives k6's live samples (k6 run --out influxdb=http://k6sink:8086/k6 — InfluxDB line protocol) and
keeps 10-second buckets: client-side latency percentiles per kind (read / write / static / media / auth) and per
endpoint, failures, active users (vus), WebSocket opens / drops. Every 10 s the finished buckets are appended to
<results>/timeseries.csv (the report's charts).

Scenario C: with BREAK_P95_MS / BREAK_ERR_PCT set, the moment the last 30 s of user requests (read + write) cross one of
them is the breaking point: it is written to <results>/break.json and k6 is asked to stop (its REST API, K6_API).

Runs in the staging app image (python /app/loadtest/monitor/k6sink.py), next to k6 on the staging network.
"""

from __future__ import annotations

import asyncio
import csv
import gzip
import json
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, "/app")
from app.services.metrics import BOUNDS_MS, percentile  # noqa: E402  (same buckets as the server's own timings)

import bisect  # noqa: E402

import httpx  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import JSONResponse, Response  # noqa: E402
from starlette.routing import Route  # noqa: E402

RESULTS = Path(os.environ.get("RESULTS", "/results"))
BUCKET = 10
BREAK_P95 = float(os.environ.get("BREAK_P95_MS", "0") or 0)
BREAK_ERR = float(os.environ.get("BREAK_ERR_PCT", "0") or 0)
BREAK_MIN = int(os.environ.get("BREAK_MIN_REQUESTS", "300"))
K6_API = os.environ.get("K6_API", "")
USER_KINDS = {"read", "write"}
SUMS = {"ws_opened", "ws_dropped", "ws_failed", "server_errors", "rate_limited_429", "refused_4xx", "signups_done",
        "signups_failed", "logins_done", "logins_failed", "sessions"}
TRENDS = {"ws_connect_ms", "signup_total_ms", "signup_code_delivery_ms"}
_SPLIT = re.compile(r"(?<!\\) ")
_COMMA = re.compile(r"(?<!\\),")


class Hist:
    __slots__ = ("n", "h", "bad")

    def __init__(self) -> None:
        self.n, self.bad, self.h = 0, 0, [0] * (len(BOUNDS_MS) + 1)

    def add(self, ms: float) -> None:
        self.n += 1
        self.h[bisect.bisect_left(BOUNDS_MS, ms)] += 1

    def merge(self, o: "Hist") -> None:
        self.n += o.n
        self.bad += o.bad
        for i, v in enumerate(o.h):
            self.h[i] += v

    def q(self, x: float) -> float:
        return percentile(self.h, self.n, x)


hists: dict[tuple[int, str, str], Hist] = defaultdict(Hist)  # (bucket, group, key)
sums: dict[tuple[int, str], float] = defaultdict(float)
vus: dict[int, float] = {}
written: set[int] = set()
state = {"broken": None, "lines": 0}


def _unescape(v: str) -> str:
    return v.replace("\\ ", " ").replace("\\,", ",").replace("\\=", "=")


def ingest(body: str) -> None:
    for line in body.splitlines():
        if not line:
            continue
        parts = _SPLIT.split(line)
        if len(parts) < 2:
            continue
        head, fields = parts[0], parts[1]
        ts = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else time.time_ns()
        t = (ts // 1_000_000_000 if ts > 10**15 else ts // 1000 if ts > 10**12 else ts) // BUCKET * BUCKET
        items = _COMMA.split(head)
        metric, tags = items[0], {}
        for it in items[1:]:
            k, _, v = it.partition("=")
            tags[k] = _unescape(v)
        value = None
        for f in _COMMA.split(fields):
            k, _, v = f.partition("=")
            if k == "value":
                value = float(v.rstrip("i"))
                break
        if value is None:
            continue
        state["lines"] += 1
        if metric == "http_req_duration":
            kind, name = tags.get("kind", "other"), tags.get("name", "?")
            hists[(t, "kind", kind)].add(value)
            hists[(t, "name", name)].add(value)
        elif metric == "http_req_failed" and value >= 1:
            hists[(t, "kind", tags.get("kind", "other"))].bad += 1
            hists[(t, "name", tags.get("name", "?"))].bad += 1
        elif metric == "vus":
            vus[t] = max(vus.get(t, 0), value)
        elif metric in SUMS:
            sums[(t, metric)] += value
        elif metric in TRENDS:
            hists[(t, "trend", metric)].add(value)


def _write(final_before: int) -> None:
    ready = sorted({t for t, _, _ in hists} | {t for t, _ in sums} | set(vus))
    ready = [t for t in ready if t < final_before and t not in written]
    if not ready:
        return
    path = RESULTS / "timeseries.csv"
    new = not path.exists()
    with path.open("a", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["t", "group", "key", "count", "errors", "p50", "p95", "p99", "value"])
        for t in ready:
            if t in vus:
                w.writerow([t, "gauge", "vus", "", "", "", "", "", vus[t]])
            for (bt, group, key), h in list(hists.items()):
                if bt == t:
                    w.writerow([t, group, key, h.n, h.bad, h.q(0.5), h.q(0.95), h.q(0.99), ""])
            for (bt, metric), v in list(sums.items()):
                if bt == t:
                    w.writerow([t, "sum", metric, "", "", "", "", "", v])
            written.add(t)
    old = final_before - 120  # written long ago: the break check only looks at the last 30 s
    for d in (hists, sums):
        for k in [k for k in d if k[0] < old]:
            del d[k]
    for t in [t for t in vus if t < old]:
        del vus[t]


def _check_break(now_bucket: int) -> None:
    if state["broken"] or not (BREAK_P95 or BREAK_ERR):
        return
    window = [now_bucket - BUCKET * i for i in range(1, 4)]  # the last 30 s that are complete
    total = Hist()
    for (t, group, key), h in list(hists.items()):
        if group == "kind" and key in USER_KINDS and t in window:
            total.merge(h)
    if total.n < BREAK_MIN:
        return
    p95, err = total.q(0.95), 100 * total.bad / total.n
    why = []
    if BREAK_P95 and p95 > BREAK_P95:
        why.append(f"p95 {p95:.0f} ms > {BREAK_P95:.0f} ms")
    if BREAK_ERR and err > BREAK_ERR:
        why.append(f"errors {err:.1f} % > {BREAK_ERR:g} %")
    if not why:
        return
    users = max((vus.get(t, 0) for t in window), default=0)
    state["broken"] = {"at": window[0] + BUCKET, "users": users, "p95_ms": p95, "error_pct": round(err, 2),
                       "requests_30s": total.n, "reason": " and ".join(why)}
    (RESULTS / "break.json").write_text(json.dumps(state["broken"], indent=1))
    print(f"BREAKING POINT at {users:.0f} users: {state['broken']['reason']}", flush=True)
    if K6_API:
        try:
            httpx.patch(f"{K6_API.rstrip('/')}/v1/status", timeout=5,
                        json={"data": {"type": "status", "id": "default", "attributes": {"stopped": True}}})
        except httpx.HTTPError as exc:
            print(f"could not stop k6: {exc}", flush=True)


async def _loop() -> None:
    while True:
        await asyncio.sleep(BUCKET)
        now_bucket = int(time.time()) // BUCKET * BUCKET
        _check_break(now_bucket)
        _write(now_bucket - 2 * BUCKET)  # k6 sends with ~1 s delay; 20 s of margin


async def write(request: Request) -> Response:
    raw = await request.body()
    if request.headers.get("content-encoding") == "gzip":
        raw = gzip.decompress(raw)
    ingest(raw.decode("utf-8", errors="replace"))
    return Response(status_code=204)


async def query(request: Request) -> Response:  # k6 asks to CREATE DATABASE on start
    return JSONResponse({"results": [{"statement_id": 0}]})


async def status(request: Request) -> Response:
    return JSONResponse({"lines": state["lines"], "broken": state["broken"], "buckets": len(written)})


async def flush(request: Request) -> Response:  # run.sh calls it after k6 ends: every bucket is written
    _write(int(time.time()) + BUCKET)
    return JSONResponse({"ok": True})


async def ping(request: Request) -> Response:
    return Response(status_code=204)


async def _lifespan(_app):
    RESULTS.mkdir(parents=True, exist_ok=True)
    task = asyncio.create_task(_loop())
    yield
    task.cancel()
    _write(int(time.time()) + BUCKET)


app = Starlette(routes=[Route("/write", write, methods=["POST"]), Route("/query", query, methods=["GET", "POST"]),
                        Route("/ping", ping, methods=["GET", "HEAD"]), Route("/status", status),
                        Route("/flush", flush, methods=["POST"])], lifespan=_lifespan)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8086, log_level="warning", access_log=False)
