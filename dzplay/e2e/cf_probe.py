"""V6 phase 20: the client side of e2e/run_cloudflare_e2e.sh, run inside the throwaway Docker network from a given
address (a "Cloudflare" one or any other). Standard library only. Prints one JSON object.

    python cf_probe.py CADDY_IP DOMAIN LEGACY CERT_PEM SESSION_TOKEN
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import socket
import ssl
import sys

CADDY, DOMAIN, LEGACY, CERT, TOKEN = sys.argv[1:6]
FORGED = "198.51.100.7"


def tls(sni: str, verify: bool) -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=CERT) if verify else ssl._create_unverified_context()  # noqa: S323
    if verify:
        ctx.check_hostname = True
    return ctx


class Conn(http.client.HTTPSConnection):
    """HTTPS to CADDY with SNI/Host `sni` (the test network has no DNS for the names)."""

    def __init__(self, sni: str, verify: bool = True):
        super().__init__(sni, 443, context=tls(sni, verify), timeout=15)

    def connect(self):
        sock = socket.create_connection((CADDY, 443), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def get(sni: str, path: str, headers: dict | None = None, verify: bool = True) -> tuple[int, dict, bytes]:
    c = Conn(sni, verify)
    c.request("GET", path, headers={"Host": sni, **(headers or {})})
    r = c.getresponse()
    body = r.read()
    c.close()
    return r.status, {k.lower(): v for k, v in r.getheaders()}, body


def websocket() -> dict:
    """A real upgrade through Caddy with a session: 101, then the app's keep-alive frame comes back."""
    raw = socket.create_connection((CADDY, 443), 15)
    s = tls(DOMAIN, True).wrap_socket(raw, server_hostname=DOMAIN)
    key = base64.b64encode(os.urandom(16)).decode()
    s.sendall((f"GET /api/ws HTTP/1.1\r\nHost: {DOMAIN}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nOrigin: https://{DOMAIN}\r\n"
               f"Cookie: dz_session={TOKEN}\r\nUser-Agent: Mozilla/5.0 (X11; Linux x86_64)\r\n\r\n").encode())
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = s.recv(1)
        if not chunk:
            break
        head += chunk
    status = int(head.split(b" ", 2)[1]) if head.startswith(b"HTTP/") else 0
    frames = []
    if status == 101:  # the app's greeting, then its keep-alive (WS_PING_SECONDS later): both through Caddy
        s.settimeout(15)
        for _ in range(2):
            hdr = s.recv(2)
            n = hdr[1] & 0x7F
            if n == 126:
                n = int.from_bytes(s.recv(2), "big")
            data = b""
            while len(data) < n:
                data += s.recv(n - len(data))
            frames.append(data.decode(errors="replace"))
    s.close()
    return {"status": status, "frames": frames}


out: dict = {}
# the domain: the origin certificate (verified against it), the client address, no Server/Via header
st, h, body = get(DOMAIN, "/healthz/ip", {"CF-Connecting-IP": FORGED, "X-Forwarded-For": "6.6.6.6"})
out["cf_header"] = {"status": st, "ip": json.loads(body)["ip"], "server": h.get("server"), "via": h.get("via")}
st, h, body = get(DOMAIN, "/healthz/ip", {"X-Forwarded-For": "6.6.6.6", "X-Real-IP": "7.7.7.7"})
out["no_cf_header"] = {"status": st, "ip": json.loads(body)["ip"]}
out["ws"] = websocket()
# the old address during the move: everything → 301 to the domain, except what the installed app needs
st, h, _ = get(LEGACY, "/?x=1", verify=False)
out["legacy_root"] = {"status": st, "location": h.get("location")}
st, h, _ = get(LEGACY, "/download", verify=False)
out["legacy_download"] = {"status": st}
st, h, _ = get(LEGACY, "/api/app/version", verify=False)
out["legacy_version"] = {"status": st}
st, h, _ = get(LEGACY, "/", {"User-Agent": "Mozilla/5.0 (Linux; Android 14) DZPLAYApp/3.2.0"}, verify=False)
out["legacy_app"] = {"status": st, "location": h.get("location")}
print(json.dumps(out))
