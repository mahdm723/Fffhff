"""Google Sign-In (OpenID Connect ID token verification).

The browser obtains a Google-signed ID token through Google Identity Services
and sends it to the backend once. We verify signature, audience (our client
id), issuer, expiry, the `email_verified` claim and a one-time nonce. Only the
stable `sub` identifier is kept; no Google access/refresh token is requested
or stored.
"""

from __future__ import annotations

GOOGLE_ISSUERS = ("accounts.google.com", "https://accounts.google.com")


def verify_google_id_token(token: str, client_id: str) -> dict:
    """Return the verified claims or raise ValueError. Patched in tests."""
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token

    return id_token.verify_oauth2_token(token, google_requests.Request(), client_id)
