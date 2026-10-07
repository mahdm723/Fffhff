"""V5 security audit (phase G): cross-account access to uploads and signed media URLs, Telegram identifiers
never reaching clients, and every V5 panel route inside the admin authorization matrix."""

from __future__ import annotations

import json

from app import clock
from app.models import MediaItem
from tests.conftest import reply, send
from tests.test_admin_access import _admin_routes
from tests.test_uploads import jpeg, mx, ready_upload  # noqa: F401  (mx is a fixture)


def test_someone_elses_upload_cannot_be_read_attached_or_published(mx):
    a, b = mx.user(), mx.user()
    mid = ready_upload(mx, a, jpeg())
    assert b.get(f"/api/uploads/{mid}").status_code == 404
    assert b.post("/api/posts", json={"content": "سرقة", "media_id": mid}).status_code in (403, 404)
    assert b.post("/api/studio/reels", json={"media_id": mid}).status_code in (404, 405)  # V6: the studio is gone
    # the idea picture cannot be pushed into a chat either (wrong purpose), even by its owner
    cid = send(a, b, "مرحبا").json()["conversation"]["id"]
    reply(b, cid, "أهلا")
    assert a.post(f"/api/conversations/{cid}/media", json={"media_id": mid}).status_code in (400, 404, 409)
    # still the owner's: it can be attached normally
    assert a.post("/api/posts", json={"content": "لي", "media_id": mid}).status_code == 201


def test_signed_chat_picture_url_is_useless_to_anyone_else(mx):
    a, b = mx.user(), mx.user()
    cid = send(a, b, "مرحبا، كيف الحال؟").json()["conversation"]["id"]
    reply(b, cid, "بخير")
    stranger = mx.user()
    mid = ready_upload(mx, a, jpeg(), purpose="chat", conversation_id=cid)
    msg = a.post(f"/api/conversations/{cid}/media", json={"media_id": mid}).json()["message"]
    url = b.post(f"/api/messages/{msg['id']}/open").json()["url"]
    assert b.get(url).status_code == 200
    assert stranger.get(url).status_code == 403  # bound to b's session
    assert mx.client().get(url).status_code in (401, 403)  # no session at all
    assert b.get(url.replace("/img?", "/poster?")).status_code in (403, 404)  # another variant: signature fails
    tampered = url[:-2] + ("aa" if not url.endswith("aa") else "bb")
    assert b.get(tampered).status_code == 403
    clock.advance(mx.settings.CHAT_IMAGE_TTL_AFTER_VIEW + 1)
    assert b.get(url).status_code == 404  # expired: dead even with a valid signature


def test_telegram_ids_never_reach_any_client(mx):
    a, b = mx.user(), mx.user()
    mid = ready_upload(mx, a, jpeg())
    a.post("/api/posts", json={"content": "فكرة مع صورة", "media_id": mid})
    cid = send(a, b, "سلام").json()["conversation"]["id"]
    reply(b, cid, "وعليكم")
    cmid = ready_upload(mx, a, jpeg((1, 2, 3)), purpose="chat", conversation_id=cid)
    msg = a.post(f"/api/conversations/{cid}/media", json={"media_id": cmid}).json()["message"]
    b.post(f"/api/messages/{msg['id']}/open")
    mx.state.pipeline.wait_idle()
    with mx.db() as db:
        secrets = set()
        for item in db.query(MediaItem).all():
            secrets |= {item.tg_file_id, item.tg_file_unique_id if hasattr(item, "tg_file_unique_id") else None,
                        str(item.tg_message_id) if item.tg_message_id else None}
        secrets.discard(None)
    assert secrets
    bodies = []
    for c in (a, b):
        for path in ("/api/me", "/api/posts/feed", "/api/posts/mine", "/api/conversations", f"/api/conversations/{cid}",
                     f"/api/uploads/{mid}", f"/api/uploads/{cmid}", "/api/uploads/config"):
            r = c.get(path)
            if r.status_code == 200:
                bodies.append(r.text)
    text = "\n".join(bodies)
    assert len(bodies) >= 10
    for s in secrets:
        if len(s) > 6:  # file ids; message ids are small numbers that could appear by chance
            assert s not in text
    assert "tg_" not in text and "file_id" not in text and STORAGE_HINT not in text


STORAGE_HINT = "-100111111111"  # the private storage channel id


def test_v5_panel_routes_are_inside_the_admin_authorization_matrix(mx):
    routes = {path for _, path in _admin_routes(mx)}
    for part in ("/support", "/verification", "/payment-settings", "/media", "/settings", "/v5"):
        assert any(part in p for p in routes), part
    user = mx.user()
    for path in ("/api/admin/media", "/api/admin/payment-settings", "/api/admin/settings"):
        assert user.get(path).status_code == 404  # not even reachable outside the secret prefix
    assert json.dumps(user.get("/api/me").json()).find("verified_by") == -1
