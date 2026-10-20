"""V6 phase 21, scenario F: how many WebSockets the server holds at once, steadily.

Opens sockets in steps (STEP each, at most 200 handshakes at a time), each for a different seeded member (the session
tokens of out/vu_users.csv, reused when more sockets than members are asked), then holds HOLD seconds — more than one
keep-alive round (WS_PING_SECONDS, 25 s), so every socket must have received a ping. A step fails when more than 1 %
of its handshakes fail, or more than 1 % of all open sockets were dropped or went silent (no ping for 45 s).
The last good total is the answer. Light on the generator: one asyncio task per socket, no other traffic.

    APP_ENV=staging LOADTEST_TOKEN=… python loadtest/ws/wsflood.py --url wss://staging.test:8443/api/ws \
        --step 1000 --hold 40 --max 30000 --out results/F
Several source addresses are needed beyond ~28,000 sockets (one address has ~28k local ports): run several copies
with --offset, each in its own container.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import ssl
import sys
import time
from pathlib import Path

import websockets

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from loadtest.lib import safety  # noqa: E402

IPHONE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
          "Version/18.1 Mobile/15E148 Safari/604.1")


class Flood:
    def __init__(self, a: argparse.Namespace, tokens: list[str]) -> None:
        self.a, self.tokens = a, tokens
        self.open = 0
        self.failed = 0
        self.dropped = 0
        self.last_msg: dict[int, float] = {}
        self.tasks: list[asyncio.Task] = []
        self.gate = asyncio.Semaphore(200)
        self.ctx = ssl.create_default_context()
        self.ctx.check_hostname, self.ctx.verify_mode = False, ssl.CERT_NONE
        self.connect_ms: list[float] = []
        self.errors: dict[str, int] = {}

    async def one(self, n: int, ready: asyncio.Event) -> None:
        token = self.tokens[(self.a.offset + n) % len(self.tokens)]
        ip = f"10.{200 + (n >> 16) % 50}.{(n >> 8) & 255}.{n & 255}"
        headers = {"Cookie": f"dz_session={token}", "User-Agent": IPHONE, "Origin": self.a.origin,
                   "X-Loadtest-Token": self.a.token, "X-Loadtest-IP": ip}
        try:
            async with self.gate:
                t0 = time.perf_counter()
                ws = await websockets.connect(self.a.url, ssl=self.ctx, additional_headers=headers, open_timeout=30,
                                              ping_interval=None, close_timeout=2, max_queue=4,
                                              server_hostname=self.a.sni,
                                              **({"host": self.a.resolve} if self.a.resolve else {}))
                first = json.loads(await asyncio.wait_for(ws.recv(), 15))
                if first.get("type") != "hello":
                    raise RuntimeError(f"first frame {first.get('type')}")
                self.connect_ms.append((time.perf_counter() - t0) * 1000)
        except Exception as exc:  # noqa: BLE001
            self.failed += 1
            key = type(exc).__name__
            self.errors[key] = self.errors.get(key, 0) + 1
            ready.set()
            return
        self.open += 1
        self.last_msg[n] = time.time()
        ready.set()
        try:
            async for _ in ws:
                self.last_msg[n] = time.time()
        except Exception:  # noqa: BLE001
            pass
        finally:
            self.open -= 1
            self.last_msg.pop(n, None)
            if not self.stopping:
                self.dropped += 1

    stopping = False

    def silent(self) -> int:
        limit = time.time() - 45
        return sum(1 for t in self.last_msg.values() if t < limit)

    async def run(self) -> dict:
        out = Path(self.a.out)
        out.mkdir(parents=True, exist_ok=True)
        rows, good, total, verdict = [], 0, 0, "max reached"
        with (out / "wsflood.csv").open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["t", "target", "open", "failed", "dropped", "silent"])
            while total < self.a.max:
                step = min(self.a.step, self.a.max - total)
                failed0 = self.failed
                events = []
                for n in range(total, total + step):
                    ev = asyncio.Event()
                    events.append(ev)
                    self.tasks.append(asyncio.create_task(self.one(n, ev)))
                await asyncio.gather(*(e.wait() for e in events))
                total += step
                end = time.time() + self.a.hold
                while time.time() < end:
                    await asyncio.sleep(5)
                    row = [round(time.time(), 1), total, self.open, self.failed, self.dropped, self.silent()]
                    w.writerow(row)
                    fh.flush()
                step_failed = self.failed - failed0
                print(f"target {total}: open {self.open}, step failures {step_failed}, dropped {self.dropped}, "
                      f"silent {self.silent()}", flush=True)
                if step_failed > 0.01 * step or (self.dropped + self.silent()) > 0.01 * max(total, 1):
                    verdict = (f"failures at {total}: {step_failed} handshakes failed in the step, "
                               f"{self.dropped} dropped, {self.silent()} silent")
                    break
                good = total
                rows.append(total)
        self.stopping = True
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        cm = sorted(self.connect_ms)
        result = {"max_stable": good, "verdict": verdict, "handshake_errors": self.errors,
                  "connect_p50_ms": round(cm[len(cm) // 2], 1) if cm else None,
                  "connect_p95_ms": round(cm[int(len(cm) * 0.95)], 1) if cm else None,
                  "step": self.a.step, "hold_s": self.a.hold}
        (out / "wsflood.json").write_text(json.dumps(result, indent=1))
        return result


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="wss://staging.test:8443/api/ws")
    p.add_argument("--users", default=str(Path(__file__).resolve().parent.parent / "out" / "vu_users.csv"))
    p.add_argument("--step", type=int, default=1000)
    p.add_argument("--hold", type=int, default=40)
    p.add_argument("--max", type=int, default=30000)
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--out", default="results/F")
    a = p.parse_args()
    safety.require_staging_env()
    a.token = os.environ.get("LOADTEST_TOKEN", "")
    http_url = a.url.replace("wss://", "https://").replace("ws://", "http://").split("/api/ws")[0]
    safety.check_target(http_url, a.token)
    from urllib.parse import urlsplit

    a.origin, a.sni = http_url, urlsplit(http_url).hostname
    a.resolve = os.environ.get("LT_RESOLVE", "")  # the server's address when staging.test does not resolve to it
    with open(a.users, encoding="utf-8") as fh:
        tokens = [row["token"] for row in csv.DictReader(fh)]
    print(json.dumps(asyncio.run(Flood(a, tokens).run()), indent=1))


if __name__ == "__main__":
    main()
