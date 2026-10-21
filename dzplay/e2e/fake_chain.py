"""A tiny stand-in for TronGrid (e2e only): POST /wallet/gettransactioninfobyid and /wallet/getnowblock, answering
from a table of USDT transfers that the test fills (`add`)."""

from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.services import chain  # noqa: E402

HEAD = 70_000_000
USDT_HEX = chain.tron_to_hex20(chain.USDT["TRC20"]["contract"])


class FakeTronGrid:
    def __init__(self, port: int):
        self.txs: dict[str, dict] = {}
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a):  # quiet
                pass

            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if self.path == "/wallet/gettransactioninfobyid":
                    out = fake.txs.get(body.get("value"), {})
                elif self.path == "/wallet/getnowblock":
                    out = {"block_header": {"raw_data": {"number": HEAD}}}
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                data = json.dumps(out).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.url = f"http://127.0.0.1:{port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def add(self, txid: str, usdt: float, to_hex: str) -> None:
        """A confirmed transfer of `usdt` official USDT to the 20-byte hex address `to_hex`, made just now."""
        self.txs[txid] = {"id": txid, "blockNumber": HEAD - 40, "blockTimeStamp": int(time.time() * 1000) + 5000,
                          "receipt": {"result": "SUCCESS"},
                          "log": [{"address": USDT_HEX, "topics": [chain.TRANSFER_TOPIC, "0" * 24 + "33" * 20, "0" * 24 + to_hex],
                                   "data": f"{round(usdt * 1_000_000):064x}"}]}

    def stop(self) -> None:
        self.server.shutdown()
