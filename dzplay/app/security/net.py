from __future__ import annotations

from starlette.requests import HTTPConnection

from app.config import Settings


def client_ip(conn: HTTPConnection, settings: Settings) -> str:
    """Client IP, honouring X-Forwarded-For only when we sit behind trusted proxies.

    With N trusted proxies, the real client is the N-th address from the right
    (addresses further left can be forged by the client).
    """
    if settings.TRUST_PROXY_HEADERS:
        forwarded = conn.headers.get("x-forwarded-for", "")
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if parts:
            idx = max(0, len(parts) - max(1, settings.TRUSTED_PROXY_COUNT))
            return parts[idx]
    return conn.client.host if conn.client else "unknown"
