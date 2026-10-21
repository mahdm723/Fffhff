"""A tiny stand-in for Bybit's public market API (e2e only): /v5/market/tickers and /v5/market/kline.

Prices move a little on every call so the market pane has something to refresh. `stop()` makes it
unreachable (to see the "stale" notice)."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

COINS = {  # base: (price, 24 h change)
    "BTC": (64000.5, 0.012), "ETH": (3100.25, -0.031), "SOL": (151.2, 0.084), "DOGE": (0.1234, -0.12),
    "PEPE": (0.0000123, 0.25), "XRP": (0.52, -0.05), "ADA": (0.45, 0.02), "LINK": (14.2, 0.03), "AVAX": (28.1, 0.04),
    "TON": (5.1, -0.07), "NEAR": (5.5, 0.061), "SUI": (1.9, -0.091), "ARB": (0.8, -0.043), "USDC": (1.0, 0.0001),
}


class FakeBybit:
    def __init__(self, port: int):
        self.calls = 0
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a):  # quiet
                pass

            def do_GET(self):  # noqa: N802
                url = urlsplit(self.path)
                q = {k: v[0] for k, v in parse_qs(url.query).items()}
                if url.path == "/v5/market/tickers":
                    fake.calls += 1
                    body = {"retCode": 0, "retMsg": "OK", "result": {"category": "spot", "list": fake.tickers()}}
                elif url.path == "/v5/market/kline":
                    price, pct = COINS[q["symbol"][:-4]]
                    start = price / (1 + pct)
                    closes = [start + (price - start) * i / 23 + (price * 0.01 if i % 5 == 0 else 0) for i in range(24)]
                    body = {"retCode": 0, "result": {"list": [[str(i), "0", "0", "0", repr(c), "0", "0"]
                                                              for i, c in reversed(list(enumerate(closes)))]}}
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.url = f"http://127.0.0.1:{port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tickers(self) -> list[dict]:
        out = []
        for base, (price, pct) in COINS.items():
            p = price * (1 + 0.001 * (self.calls % 3))
            out.append({"symbol": f"{base}USDT", "lastPrice": f"{p:.10g}", "price24hPcnt": f"{pct:.4f}",
                        "highPrice24h": f"{p * 1.05:.10g}", "lowPrice24h": f"{p * 0.93:.10g}", "turnover24h": "25000000"})
        out.append({"symbol": "BTC3LUSDT", "lastPrice": "1", "price24hPcnt": "0.9", "turnover24h": "25000000"})
        return out

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
