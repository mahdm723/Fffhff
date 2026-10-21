"""V6 phase 14: Android Key Attestation — proof that a request comes from OUR app on a real phone.

The app creates a key inside the phone's secure hardware (TEE or StrongBox), bound to a challenge from the server;
the secure hardware then signs a certificate chain for that key, up to one of Google's attestation roots. Here:

* the chain: each certificate is signed by the next one, the intermediates are valid now, none is on Google's
  revocation list (cached, refreshed daily), and the top one has the public key of a Google root
  (app/security/attestation_roots.py);
* the KeyDescription extension (OID 1.3.6.1.4.1.11129.2.1.17) of the leaf: the challenge is the one we issued, the
  attestation happened in hardware (software attestation is refused), and attestationApplicationId names our package
  (io.dzplay.app) signed with OUR certificate (ANDROID_CERT_SHA256 — the same key since 1.0, never in git).

Later launches only prove that the app still holds that key (an ECDSA signature over a fresh challenge).
A minimal DER reader is enough for the extension (no extra dependency); X.509 is handled by `cryptography`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

log = logging.getLogger("dzplay.attestation")

KEY_DESCRIPTION_OID = x509.ObjectIdentifier("1.3.6.1.4.1.11129.2.1.17")
SECURITY_LEVELS = {0: "software", 1: "tee", 2: "strongbox"}
BOOT_STATES = {0: "verified", 1: "self_signed", 2: "unverified", 3: "failed"}


class AttestationError(Exception):
    """The chain or its content is not acceptable (the reason is for the logs and the tests, never shown raw)."""


# ---------------------------------------------------------------------------------------------- DER (read only)


@dataclass
class Tlv:
    cls: int  # 0 universal, 1 application, 2 context, 3 private
    constructed: bool
    tag: int
    value: bytes


def _read(data: bytes, i: int) -> tuple[Tlv, int]:
    if i >= len(data):
        raise AttestationError("DER: truncated")
    first = data[i]
    i += 1
    cls, constructed, tag = first >> 6, bool(first & 0x20), first & 0x1F
    if tag == 0x1F:  # high tag number (e.g. [709])
        tag = 0
        while True:
            b = data[i]
            i += 1
            tag = (tag << 7) | (b & 0x7F)
            if not b & 0x80:
                break
    length = data[i]
    i += 1
    if length & 0x80:
        n = length & 0x7F
        if n == 0 or n > 4:
            raise AttestationError("DER: bad length")
        length = int.from_bytes(data[i:i + n], "big")
        i += n
    if i + length > len(data):
        raise AttestationError("DER: truncated value")
    return Tlv(cls, constructed, tag, data[i:i + length]), i + length


def children(data: bytes) -> list[Tlv]:
    out, i = [], 0
    while i < len(data):
        t, i = _read(data, i)
        out.append(t)
    return out


def _int(t: Tlv) -> int:
    return int.from_bytes(t.value, "big", signed=True) if t.value else 0


def _fields(auth_list: Tlv) -> dict[int, Tlv]:
    """AuthorizationList: SEQUENCE of [tag] EXPLICIT values → {tag: inner TLV}."""
    out = {}
    for t in children(auth_list.value):
        if t.cls == 2 and t.constructed:
            inner = children(t.value)
            if inner:
                out[t.tag] = inner[0]
    return out


@dataclass
class KeyDescription:
    attestation_version: int
    security_level: str
    keymaster_security_level: str
    challenge: bytes
    packages: list[tuple[str, int]] = field(default_factory=list)
    signature_digests: list[bytes] = field(default_factory=list)
    os_version: int | None = None
    os_patch_level: int | None = None
    boot_state: str | None = None
    device_locked: bool | None = None


def parse_key_description(der: bytes) -> KeyDescription:
    top = children(der)
    if len(top) != 1 or top[0].tag != 16:
        raise AttestationError("KeyDescription: not a SEQUENCE")
    items = children(top[0].value)
    if len(items) < 8:
        raise AttestationError("KeyDescription: too short")
    software, hardware = _fields(items[6]), _fields(items[7])
    kd = KeyDescription(attestation_version=_int(items[0]), security_level=SECURITY_LEVELS.get(_int(items[1]), "unknown"),
                        keymaster_security_level=SECURITY_LEVELS.get(_int(items[3]), "unknown"), challenge=items[4].value)
    app_id = software.get(709) or hardware.get(709)  # attestationApplicationId: OCTET STRING holding its own DER
    if app_id is not None:
        seq = children(app_id.value)
        if seq and seq[0].tag == 16:
            parts = children(seq[0].value)
            if len(parts) >= 2:
                for info in children(parts[0].value):  # SET OF AttestationPackageInfo
                    p = children(info.value)
                    if len(p) >= 2:
                        kd.packages.append((p[0].value.decode("utf-8", "replace"), _int(p[1])))
                kd.signature_digests = [d.value for d in children(parts[1].value)]
    if 705 in hardware:
        kd.os_version = _int(hardware[705])
    if 706 in hardware:
        kd.os_patch_level = _int(hardware[706])
    root_of_trust = hardware.get(704)
    if root_of_trust is not None:
        rot = children(root_of_trust.value)
        if len(rot) >= 3:
            kd.device_locked = bool(rot[1].value and rot[1].value[0])
            kd.boot_state = BOOT_STATES.get(_int(rot[2]), "unknown")
    return kd


# ---------------------------------------------------------------------------------------------- trust


def _spki(cert: x509.Certificate) -> bytes:
    return cert.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)


def google_roots() -> set[bytes]:
    from app.security.attestation_roots import ROOTS_PEM

    return {_spki(x509.load_pem_x509_certificate(p.encode())) for p in ROOTS_PEM}


class RevocationList:
    """Google's attestation status list (revoked certificate serials), refreshed at most once a day in the background.
    Unreachable: the last good list is kept (empty at first) — a missing list never blocks the app."""

    def __init__(self, url: str, transport=None):
        self.url = url
        self.transport = transport
        self.serials: set[str] = set()
        self.fetched_at = 0.0
        self._lock = threading.Lock()

    def refresh(self) -> None:
        import httpx

        try:
            with httpx.Client(timeout=15, transport=self.transport, trust_env=True) as c:
                data = c.get(self.url).json()
            entries = (data or {}).get("entries") or {}
            self.serials = {str(k).lower().lstrip("0") for k, v in entries.items() if (v or {}).get("status") == "REVOKED"}
            self.fetched_at = time.time()
        except Exception as exc:  # noqa: BLE001
            log.info("attestation status list not refreshed: %s", type(exc).__name__)
            self.fetched_at = time.time() - 23 * 3600  # try again in an hour

    def current(self) -> set[str]:
        if self.url and time.time() - self.fetched_at > 86400 and self._lock.acquire(blocking=False):
            def run():
                try:
                    self.refresh()
                finally:
                    self._lock.release()
            threading.Thread(target=run, daemon=True).start()
        return self.serials


@dataclass
class Attested:
    public_key_der: bytes  # SubjectPublicKeyInfo of the app's key (checks later proofs)
    key_id: str  # sha256 of it (hex)
    description: KeyDescription
    package_version: int | None


def verify_chain(chain_der: list[bytes], *, challenge: bytes, package: str, cert_sha256: list[bytes],
                 roots: set[bytes], revoked: set[str] | None = None, now: datetime | None = None) -> Attested:
    """Check an attestation chain (leaf first). Raises AttestationError; returns the key and its description."""
    if not 2 <= len(chain_der) <= 10:
        raise AttestationError("chain length")
    try:
        certs = [x509.load_der_x509_certificate(c) for c in chain_der]
    except ValueError:
        raise AttestationError("not a certificate") from None
    now = now or datetime.now(timezone.utc)
    revoked = revoked or set()
    for i, cert in enumerate(certs):
        issuer = certs[i + 1] if i + 1 < len(certs) else cert  # the root signs itself
        try:
            cert.verify_directly_issued_by(issuer)
        except (InvalidSignature, ValueError, TypeError):
            raise AttestationError(f"signature of certificate {i}") from None
        if i > 0 and not (cert.not_valid_before_utc <= now <= cert.not_valid_after_utc):
            raise AttestationError(f"certificate {i} expired or not yet valid")  # (leaf dates are not reliable)
        if format(cert.serial_number, "x").lstrip("0") in revoked:
            raise AttestationError(f"certificate {i} revoked")
    if _spki(certs[-1]) not in roots:
        raise AttestationError("not a Google attestation root")
    try:
        ext = certs[0].extensions.get_extension_for_oid(KEY_DESCRIPTION_OID)
    except x509.ExtensionNotFound:
        raise AttestationError("no KeyDescription") from None
    desc = parse_key_description(ext.value.value)
    if desc.security_level not in ("tee", "strongbox"):
        raise AttestationError(f"security level {desc.security_level}")
    if desc.challenge != challenge:
        raise AttestationError("challenge")
    versions = [v for name, v in desc.packages if name == package]
    if not versions:
        raise AttestationError("package")
    if not set(desc.signature_digests) & set(cert_sha256):
        raise AttestationError("signing certificate")
    if not isinstance(certs[0].public_key(), ec.EllipticCurvePublicKey):
        raise AttestationError("key type")
    spki = _spki(certs[0])
    return Attested(spki, hashlib.sha256(spki).hexdigest(), desc, max(versions))


def verify_proof(public_key_der: bytes, challenge: bytes, signature: bytes) -> bool:
    """A later launch: an ECDSA (SHA-256) signature of the challenge with the attested key."""
    try:
        key = serialization.load_der_public_key(public_key_der)
        if not isinstance(key, ec.EllipticCurvePublicKey):
            return False
        key.verify(signature, challenge, ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def describe(desc: KeyDescription) -> str:
    return json.dumps({"level": desc.security_level, "attestation": desc.attestation_version, "os": desc.os_version,
                       "patch": desc.os_patch_level, "boot": desc.boot_state, "locked": desc.device_locked})
