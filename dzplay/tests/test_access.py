"""V6 phase 14: access to the app — Android only through the APK (Key Attestation), iPhone / iPad in the browser,
computers by a panel setting; the panel, /download and the policies open everywhere."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from sqlalchemy import select
from starlette.websockets import WebSocketDisconnect

from app.models import AppDevice, User, UserDevice
from app.security import attestation
from app.services import access
from tests.conftest import Harness

ANDROID = "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Mobile Safari/537.36"
IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"
MAC = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15"
WINDOWS = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"
OLD_APP = ANDROID + " DZPLAYApp/3.1.0"
NEW_APP = ANDROID + " DZPLAYApp/3.2.0"


# ---------------------------------------------------------------------------------------------- a fake attestation


def _tlv(tag: bytes, value: bytes) -> bytes:
    n = len(value)
    if n < 128:
        return tag + bytes([n]) + value
    lb = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return tag + bytes([0x80 | len(lb)]) + lb + value


def _seq(*items: bytes) -> bytes:
    return _tlv(b"\x30", b"".join(items))


def _set(*items: bytes) -> bytes:
    return _tlv(b"\x31", b"".join(sorted(items)))


def _int(n: int) -> bytes:
    return _tlv(b"\x02", n.to_bytes(max(1, (n.bit_length() + 8) // 8), "big", signed=True))


def _enum(n: int) -> bytes:
    return _tlv(b"\x0a", bytes([n]))


def _octets(b: bytes) -> bytes:
    return _tlv(b"\x04", b)


def _explicit(tag: int, inner: bytes) -> bytes:
    if tag < 31:
        return _tlv(bytes([0xA0 | tag]), inner)
    enc, t = [tag & 0x7F], tag >> 7
    while t:
        enc.append(0x80 | (t & 0x7F))
        t >>= 7
    return _tlv(bytes([0xBF, *reversed(enc)]), inner)


def our_cert_digest() -> bytes:
    from app.config import Settings

    return bytes.fromhex(Settings.model_fields["ANDROID_CERT_SHA256"].default.split(",")[0].replace(":", ""))


def key_description(challenge: bytes, *, level: int = 1, package: str = "io.dzplay.app", version: int = 8,
                    digest: bytes | None = None) -> bytes:
    """The KeyDescription a phone's secure hardware writes into the leaf certificate (the fields the server reads)."""
    app_id = _seq(_set(_seq(_octets(package.encode()), _int(version))), _set(_octets(digest or our_cert_digest())))
    software = _seq(_explicit(709, _octets(app_id)))
    root_of_trust = _seq(_octets(b"\x01" * 32), _tlv(b"\x01", b"\xff"), _enum(0), _octets(b"\x02" * 32))
    hardware = _seq(_explicit(704, root_of_trust), _explicit(705, _int(140000)), _explicit(706, _int(202609)))
    return _seq(_int(300), _enum(level), _int(300), _enum(level), _octets(challenge), _octets(b""), software, hardware)


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _cert(subject_pub, subject: str, issuer_key, issuer: str, *, ca: bool, ext: bytes | None = None,
          serial: int | None = None, days: tuple[int, int] = (-1, 365)) -> x509.Certificate:
    now = datetime.now(timezone.utc)
    b = (x509.CertificateBuilder().subject_name(_name(subject)).issuer_name(_name(issuer)).public_key(subject_pub)
         .serial_number(serial or x509.random_serial_number())
         .not_valid_before(now + timedelta(days=days[0])).not_valid_after(now + timedelta(days=days[1]))
         .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True))
    if ext is not None:
        b = b.add_extension(x509.UnrecognizedExtension(attestation.KEY_DESCRIPTION_OID, ext), critical=False)
    return b.sign(issuer_key, hashes.SHA256())


class FakePhone:
    """A phone with secure hardware: a root (in the test's trust store), an intermediate, and an app key whose leaf
    certificate carries the KeyDescription."""

    def __init__(self):
        self.root_key = ec.generate_private_key(ec.SECP256R1())
        self.root = _cert(self.root_key.public_key(), "Test Root", self.root_key, "Test Root", ca=True)
        self.inter_key = ec.generate_private_key(ec.SECP256R1())
        self.inter_serial = 0xBADC0FFEE
        self.inter = _cert(self.inter_key.public_key(), "Test Inter", self.root_key, "Test Root", ca=True,
                           serial=self.inter_serial)
        self.app_key = ec.generate_private_key(ec.SECP256R1())

    def root_spki(self) -> bytes:
        return self.root.public_key().public_bytes(serialization.Encoding.DER,
                                                   serialization.PublicFormat.SubjectPublicKeyInfo)

    def chain(self, challenge: bytes, *, inter: x509.Certificate | None = None, root: x509.Certificate | None = None,
              **kd) -> list[bytes]:
        leaf = _cert(self.app_key.public_key(), "Android Keystore Key", self.inter_key, "Test Inter", ca=False,
                     ext=key_description(challenge, **kd))
        return [c.public_bytes(serialization.Encoding.DER) for c in (leaf, inter or self.inter, root or self.root)]

    def key_id(self) -> str:
        import hashlib

        spki = self.app_key.public_key().public_bytes(serialization.Encoding.DER,
                                                      serialization.PublicFormat.SubjectPublicKeyInfo)
        return hashlib.sha256(spki).hexdigest()

    def sign(self, challenge: bytes) -> str:
        return base64.b64encode(self.app_key.sign(challenge, ec.ECDSA(hashes.SHA256()))).decode()


def _b64(der: bytes) -> str:
    return base64.b64encode(der).decode()


def _challenge(c) -> tuple[str, bytes]:
    r = c.get("/api/app/challenge")
    assert r.status_code == 200, r.text
    d = r.json()
    return d["id"], base64.urlsafe_b64decode(d["challenge"] + "=" * (-len(d["challenge"]) % 4))


@pytest.fixture
def phone() -> FakePhone:
    return FakePhone()


@pytest.fixture
def hx(tmp_path, phone, monkeypatch):
    h = Harness(tmp_path)
    h.state.attestation_roots = {phone.root_spki()}
    monkeypatch.setattr(access, "min_version_code", lambda: 8)
    yield h
    h.close()


def attest(hx: Harness, phone: FakePhone, c=None, **kd):
    c = c or hx.client()
    cid, nonce = _challenge(c)
    return c, c.post("/api/app/attest", json={"challenge_id": cid, "chain": [_b64(d) for d in phone.chain(nonce, **kd)],
                                              "version_code": 8})


# ---------------------------------------------------------------------------------------------- attestation


def test_attestation_gives_the_app_its_cookie_and_later_launches_sign(hx, phone):
    c, r = attest(hx, phone)
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    assert c.cookies.get(access.APP_COOKIE) == token
    app = access.read_token(hx.settings, token)
    assert app is not None and app.version_code == 8
    with hx.db() as db:
        dev = db.get(AppDevice, app.device_id)
        assert dev.key_id == phone.key_id() and dev.security_level == "tee" and dev.version_code == 8
        assert '"boot": "verified"' in dev.details and '"locked": true' in dev.details

    # a later launch: a signature with the same key over a fresh challenge
    cid, nonce = _challenge(c)
    r = c.post("/api/app/proof", json={"challenge_id": cid, "key_id": phone.key_id(), "signature": phone.sign(nonce),
                                       "version_code": 9})
    assert r.status_code == 200, r.text
    assert access.read_token(hx.settings, r.json()["token"]).device_id == app.device_id

    # the challenge is good once
    r = c.post("/api/app/proof", json={"challenge_id": cid, "key_id": phone.key_id(), "signature": phone.sign(nonce),
                                       "version_code": 9})
    assert r.status_code == 400 and r.json()["error"]["code"] == "challenge_invalid"
    # another key's signature, an unknown key, a forged challenge id
    cid, nonce = _challenge(c)
    other = FakePhone()
    r = c.post("/api/app/proof", json={"challenge_id": cid, "key_id": phone.key_id(), "signature": other.sign(nonce),
                                       "version_code": 9})
    assert r.status_code == 403 and r.json()["error"]["code"] == "attestation_failed"
    cid, nonce = _challenge(c)
    r = c.post("/api/app/proof", json={"challenge_id": cid, "key_id": other.key_id(), "signature": other.sign(nonce),
                                       "version_code": 9})
    assert r.status_code == 404 and r.json()["error"]["code"] == "unknown_key"
    r = c.post("/api/app/proof", json={"challenge_id": "AAAA.9999999999.AAAA", "key_id": phone.key_id(),
                                       "signature": phone.sign(b"x"), "version_code": 9})
    assert r.status_code == 400


@pytest.mark.parametrize("change, reason", [
    ({"package": "com.evil.clone"}, "package"),
    ({"digest": b"\x11" * 32}, "signing certificate"),
    ({"level": 0}, "security level"),
])
def test_attestation_refuses_wrong_content(hx, phone, change, reason):
    _c, r = attest(hx, phone, **change)
    assert r.status_code == 403 and r.json()["error"]["code"] == "attestation_failed"
    with pytest.raises(attestation.AttestationError, match=reason):
        attestation.verify_chain(phone.chain(b"n", **change), challenge=b"n", package="io.dzplay.app",
                                 cert_sha256=[our_cert_digest()], roots={phone.root_spki()})


def test_attestation_refuses_wrong_challenge_untrusted_root_revoked_and_expired(hx, phone):
    roots = {phone.root_spki()}
    ok = dict(package="io.dzplay.app", cert_sha256=[our_cert_digest()])
    attestation.verify_chain(phone.chain(b"n"), challenge=b"n", roots=roots, **ok)  # the reference case passes
    with pytest.raises(attestation.AttestationError, match="challenge"):
        attestation.verify_chain(phone.chain(b"old"), challenge=b"n", roots=roots, **ok)
    stranger = FakePhone()  # a chain that ends at a root we do not trust (a software «attestation»)
    with pytest.raises(attestation.AttestationError, match="Google"):
        attestation.verify_chain(phone.chain(b"n"), challenge=b"n", roots={stranger.root_spki()}, **ok)
    with pytest.raises(attestation.AttestationError, match="revoked"):
        attestation.verify_chain(phone.chain(b"n"), challenge=b"n", roots=roots,
                                 revoked={format(phone.inter_serial, "x")}, **ok)
    expired = _cert(phone.inter_key.public_key(), "Test Inter", phone.root_key, "Test Root", ca=True, days=(-30, -1))
    with pytest.raises(attestation.AttestationError, match="expired"):
        attestation.verify_chain(phone.chain(b"n", inter=expired), challenge=b"n", roots=roots, **ok)
    # a leaf not signed by the intermediate (pieces of two chains)
    mixed = [phone.chain(b"n")[0], *stranger.chain(b"n")[1:]]
    with pytest.raises(attestation.AttestationError, match="signature"):
        attestation.verify_chain(mixed, challenge=b"n", roots={stranger.root_spki()}, **ok)
    # through the API: a challenge the server never issued is refused before the chain is looked at
    c = hx.client()
    r = c.post("/api/app/attest", json={"challenge_id": "x.1.y", "chain": [_b64(d) for d in phone.chain(b"n")],
                                        "version_code": 8})
    assert r.status_code == 400


def test_google_roots_are_loaded():
    roots = attestation.google_roots()
    assert len(roots) == 2  # the RSA root (2022–2042) and the ECDSA Key Attestation CA1


def test_revocation_list_reads_google_format():
    import httpx

    def handler(request):
        return httpx.Response(200, json={"entries": {"0badc0ffee": {"status": "REVOKED", "reason": "KEY_COMPROMISE"},
                                                     "abc": {"status": "SUSPENDED"}}})

    rl = attestation.RevocationList("https://status.example", transport=httpx.MockTransport(handler))
    rl.refresh()
    assert rl.serials == {"badc0ffee"}


# ---------------------------------------------------------------------------------------------- the gate


def _get(hx, path, ua, cookies=None, **headers):
    c = hx.client()
    for k, v in (cookies or {}).items():
        c.cookies.set(k, v)
    return c.get(path, headers={"User-Agent": ua, **headers})


def test_android_browser_sees_only_the_download_page(hx):
    hx.state.settings.WEB_DESKTOP_ACCESS = False
    r = _get(hx, "/", ANDROID)
    assert r.status_code == 200 and 'data-gate="android"' in r.text
    assert "/js/app.js" not in r.text and "/download/dzplay.apk" in r.text and "/js/gate.js" in r.text
    assert "no-store" in r.headers["cache-control"]
    r = _get(hx, "/api/config", ANDROID)
    assert r.status_code == 403 and r.json()["error"] == {
        "code": "app_required", "message": "افتح التطبيق للمتابعة.", "gate": "android"}
    # always open: the download page, the APK, the policies, the attestation API, the version check, static files
    for path in ("/download", "/policies/privacy", "/api/app/version", "/api/app/challenge", "/healthz",
                 "/css/download.css", "/js/gate.js", "/.well-known/assetlinks.json"):
        assert _get(hx, path, ANDROID).status_code == 200, path
    # in the visitor's language
    r = _get(hx, "/", ANDROID, **{"Accept-Language": "fr-FR,fr;q=0.9"})
    assert '<html lang="fr" dir="ltr">' in r.text and "Téléchargez l’application" in r.text


def test_the_panel_opens_from_any_browser(hx):
    hx.state.settings.WEB_DESKTOP_ACCESS = False
    for ua in (ANDROID, WINDOWS, OLD_APP):
        assert _get(hx, hx.settings.ADMIN_PATH, ua).status_code == 200
    hx.create_admin()
    adm = hx.admin()
    adm.client.headers["User-Agent"] = ANDROID
    assert adm.get("/api/admin/settings").status_code == 200


def test_iphone_and_ipad_use_the_browser(hx):
    hx.state.settings.WEB_DESKTOP_ACCESS = False
    r = _get(hx, "/", IPHONE)
    assert r.status_code == 200 and "/js/app.js" in r.text and "data-gate" not in r.text
    assert _get(hx, "/api/config", IPHONE).status_code == 200
    # an iPad's Safari says «Macintosh»: without the touch cookie it is a computer, with it an iPad
    assert 'data-gate="desktop"' in _get(hx, "/", MAC).text
    r = _get(hx, "/", MAC, cookies={access.TOUCH_COOKIE: "1"})
    assert "/js/app.js" in r.text and "data-gate" not in r.text


def test_computers_follow_the_panel_setting(hx):
    hx.state.settings.WEB_DESKTOP_ACCESS = False
    r = _get(hx, "/", WINDOWS)
    assert 'data-gate="desktop"' in r.text and "dl-qr" in r.text  # the QR code to open it on a phone
    assert _get(hx, "/api/me", WINDOWS).json()["error"]["gate"] == "desktop"
    hx.create_admin()
    adm = hx.admin()
    r = adm.put("/api/admin/settings", json={"changes": {"WEB_DESKTOP_ACCESS": True}})
    assert r.status_code == 200, r.text
    assert "/js/app.js" in _get(hx, "/", WINDOWS).text
    assert _get(hx, "/api/config", WINDOWS).status_code == 200


def test_the_app_with_its_cookie_old_apps_and_unproven_apps(hx):
    hx.state.settings.WEB_DESKTOP_ACCESS = False
    token, _ = access.issue_token(hx.settings, "dev1", 8)
    r = _get(hx, "/", NEW_APP, cookies={access.APP_COOKIE: token})
    assert "/js/app.js" in r.text
    assert _get(hx, "/api/config", NEW_APP, cookies={access.APP_COOKIE: token}).status_code == 200
    # the 3.1.0 app (no attestation): the mandatory update page
    r = _get(hx, "/", OLD_APP)
    assert 'data-gate="update"' in r.text and "/download/dzplay.apk" in r.text
    # a new app without a valid cookie (attestation failed, or a copied User-Agent)
    r = _get(hx, "/", NEW_APP)
    assert 'data-gate="unverified"' in r.text and 'id="gate-retry"' in r.text
    forged = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    assert 'data-gate="unverified"' in _get(hx, "/", NEW_APP, cookies={access.APP_COOKIE: forged}).text
    # an older version than the one published: every update is mandatory
    old, _ = access.issue_token(hx.settings, "dev1", 7)
    assert 'data-gate="update"' in _get(hx, "/", NEW_APP, cookies={access.APP_COOKIE: old}).text
    # an expired cookie
    from app import clock

    clock.advance(seconds=hx.settings.APP_TOKEN_HOURS * 3600 + 5)
    assert 'data-gate="unverified"' in _get(hx, "/", NEW_APP, cookies={access.APP_COOKIE: token}).text


def test_the_safety_valve_lets_android_browsers_in(hx):
    hx.create_admin()
    adm = hx.admin()
    r = adm.put("/api/admin/settings", json={"changes": {"ANDROID_APP_GATE": False, "WEB_DESKTOP_ACCESS": False}})
    assert r.status_code == 200, r.text
    assert "/js/app.js" in _get(hx, "/", ANDROID).text
    assert _get(hx, "/api/config", OLD_APP).status_code == 200
    assert 'data-gate="desktop"' in _get(hx, "/", WINDOWS).text  # computers keep their own setting
    with hx.db() as db:
        from app.models import AdminAuditLog

        assert db.scalar(select(AdminAuditLog).where(AdminAuditLog.action == "settings_change",
                                                     AdminAuditLog.detail.contains("ANDROID_APP_GATE")))


def test_the_websocket_follows_the_gate(hx):
    c = hx.user()
    hx.state.settings.WEB_DESKTOP_ACCESS = False
    origin = {"Origin": "http://testserver"}
    with pytest.raises(WebSocketDisconnect) as exc:
        with c.websocket_connect("/api/ws", headers={**origin, "User-Agent": ANDROID}) as ws:
            ws.receive_json()
    assert exc.value.code == 4403
    token, _ = access.issue_token(hx.settings, "dev1", 8)
    c.cookies.set(access.APP_COOKIE, token)
    with c.websocket_connect("/api/ws", headers={**origin, "User-Agent": NEW_APP}) as ws:
        ws.send_json({"type": "ping"})


# ---------------------------------------------------------------------------------------------- device binding


def test_accounts_per_device_and_the_panel_view(hx, phone):
    c, r = attest(hx, phone)
    assert r.status_code == 200
    device_id = access.read_token(hx.settings, r.json()["token"]).device_id
    token = r.json()["token"]
    made = []
    for i in range(hx.settings.MAX_ACCOUNTS_PER_DEVICE):
        u = hx.client()
        u.cookies.set(access.APP_COOKIE, token)
        r = hx.register(u, f"phone{i}@example.com")
        assert r.status_code == 201, r.text
        made.append(u)
    u = hx.client()
    u.cookies.set(access.APP_COOKIE, token)
    r = hx.register(u, "third@example.com")
    assert r.status_code == 403 and r.json()["error"]["code"] == "device_limit"
    # without the app cookie (iPhone): e-mail and IP limits only
    assert hx.register(hx.client(), "iphone@example.com").status_code == 201
    # a sign-in from the phone is remembered too
    other = hx.user("elsewhere@example.com")
    other.cookies.set(access.APP_COOKIE, token)
    assert hx.login(other, "elsewhere@example.com").status_code == 200
    with hx.db() as db:
        first = db.scalar(select(User).where(User.email == "phone0@example.com"))
        assert first.registered_device_id == device_id
        links = db.scalars(select(UserDevice).where(UserDevice.device_id == device_id)).all()
        assert len(links) == 3
        uid = first.id
    hx.create_admin()
    d = hx.admin().get(f"/api/admin/access/users/{uid}").json()
    assert d["devices"][0]["id"] == device_id and d["devices"][0]["registered_here"] is True
    assert d["devices"][0]["accounts"] == 3 and d["devices"][0]["security_level"] == "tee"


def test_classify_and_gated_paths():
    from app.config import Settings

    s = Settings(SECRET_KEY="x" * 40, ADMIN_PATH="/panel-secret")
    assert access.classify(s, ANDROID, {}).kind == "android"
    assert access.classify(s, IPHONE, {}).kind == "ios"
    assert access.classify(s, "Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X)", {}).kind == "ios"
    assert access.classify(s, MAC, {"dz_touch": "1"}).kind == "ipad"
    assert access.classify(s, MAC, {}).kind == "desktop"
    assert access.classify(s, OLD_APP, {}).kind == "app_old"
    assert access.classify(s, NEW_APP, {}).kind == "app_unverified"
    assert access.gated("/", "/panel-secret") and access.gated("/api/posts", "/panel-secret")
    for open_path in ("/panel-secret", "/panel-secret/api/admin/me", "/download", "/download/dzplay.apk",
                      "/policies/terms", "/api/app/attest", "/api/telegram/webhook", "/js/app.js", "/r/abc"):
        assert not access.gated(open_path, "/panel-secret"), open_path
