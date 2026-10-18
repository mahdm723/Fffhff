"""V5 phase B in a real browser (phone viewport): idea picture + profile picture. V6 phase 17: posting pictures needs
the admin's permission (not the membership); pictures in chats are off for good (no button; an old one shows
«صورة محذوفة»).

A small fake Telegram Bot API (local HTTP server) stands in for api.telegram.org: it stores the files the
server sends to the "storage channel" and records moderation copies and deletions.

    python e2e/run_media_e2e.py [--shots DIR]
"""

from __future__ import annotations

import argparse
import io
import itertools
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from run_e2e import MOBILE, Run, register, start_server  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "123456:E2E-fake-telegram-token-0000000000"
STORAGE, MODCHAT = "-1001000000001", "-1001000000002"


class FakeTelegram:
    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.documents: list[dict] = []
        self.copies: list[dict] = []
        self.deleted: list[tuple] = []
        ids = itertools.count(100)
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a):
                pass

            def _ok(self, result):
                body = json.dumps({"ok": True, "result": result}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # file download
                prefix = f"/file/bot{TOKEN}/documents/"
                fid = self.path[len(prefix):].rsplit(".", 1)[0] if self.path.startswith(prefix) else ""
                data = fake.files.get(fid)
                if data is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                method = self.path.rsplit("/", 1)[-1]
                if method == "sendDocument":
                    msg = BytesParser(policy=default).parsebytes(
                        b"Content-Type: " + self.headers["Content-Type"].encode() + b"\r\n\r\n" + raw)
                    fields, doc = {}, b""
                    for part in msg.iter_parts():
                        name = part.get_param("name", header="content-disposition")
                        if name == "document":
                            doc = part.get_payload(decode=True)
                        else:
                            fields[name] = part.get_content()
                    fid, mid = f"doc{next(ids)}", next(ids)
                    fake.files[fid] = doc
                    fake.documents.append({"chat_id": fields.get("chat_id"), "file_id": fid, "message_id": mid, "size": len(doc)})
                    return self._ok({"message_id": mid, "document": {"file_id": fid, "file_unique_id": fid, "file_size": len(doc)}})
                body = json.loads(raw or b"{}")
                if method == "getFile":
                    fid = body.get("file_id")
                    return self._ok({"file_id": fid, "file_size": len(fake.files.get(fid, b"")), "file_path": f"documents/{fid}.bin"})
                if method == "copyMessage":
                    fake.copies.append(body)
                    return self._ok({"message_id": next(ids)})
                if method == "deleteMessage":
                    fake.deleted.append((str(body.get("chat_id")), body.get("message_id")))
                    return self._ok(True)
                if method == "sendMessage":
                    return self._ok({"message_id": next(ids)})
                if method == "getMe":
                    return self._ok({"id": 123456, "is_bot": True, "username": "e2e_bot"})
                return self._ok(True)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


def photo(path: Path, color=(220, 90, 40)) -> Path:
    from PIL import Image

    im = Image.new("RGB", (1500, 1100), color)
    for x in range(0, 1500, 60):
        im.paste((40, 120, 200), (x, 0, x + 20, 1100))
    exif = Image.Exif()
    exif[0x010F] = "PhoneMaker"
    exif[0x8825] = {1: "N", 2: (36.0, 49.0, 0.0), 3: "E", 4: (0.0, 9.0, 0.0)}
    im.save(path, "JPEG", exif=exif, quality=88)
    return path


def old_picture(db_file: Path, sender: str, recipient: str) -> None:
    """A chat picture as stored before phase 17 (the purge deletes its file; the message stays as «صورة محذوفة»)."""
    from datetime import timedelta

    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session

    from app import clock
    from app.models import Conversation, MediaItem, Message, User

    with Session(create_engine(f"sqlite:///{db_file}")) as db:
        a = db.scalar(select(User).where(User.email == sender))
        b = db.scalar(select(User).where(User.email == recipient))
        conv = db.scalar(select(Conversation).order_by(Conversation.created_at.desc()))
        now = clock.utcnow()
        item = MediaItem(owner_id=a.id, owner_public_id=a.public_id, purpose="chat", kind="image", state="removed",
                         conversation_id=conv.id, attached_type="message", created_at=now, removed_at=now)
        db.add(item)
        db.flush()
        db.add(Message(conversation_id=conv.id, sender_id=a.id, recipient_id=b.id, content="", kind="image",
                       media_id=item.id, created_at=now, expires_at=now + timedelta(days=1)))
        db.commit()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8771)
    parser.add_argument("--shots", default=str(ROOT / "e2e" / "screenshots-media"))
    args = parser.parse_args()
    tg = FakeTelegram()
    env = {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_ADMIN_CHAT_ID": "1001", "TELEGRAM_API_BASE": f"http://127.0.0.1:{tg.port}",
           "TELEGRAM_STORAGE_CHANNEL_ID": STORAGE, "TELEGRAM_MODERATION_CHAT_ID": MODCHAT,
           "MEDIA_TICK_SECONDS": "1",
           "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}
    tmp = Path(tempfile.mkdtemp(prefix="dz-media-e2e-"))
    db_file = tmp / "media-e2e.db"
    env["DATABASE_URL"] = f"sqlite:///{db_file}"
    proc, base = start_server(args.port, env)
    run = Run(base, Path(args.shots))
    pic1 = photo(tmp / "idea.jpg")
    pic3 = photo(tmp / "avatar.jpg", (200, 120, 40))
    chromium = os.environ.get("PW_CHROMIUM", "/opt/pw-browsers/chromium-1194/chrome-linux/chrome")
    suffix = str(int(time.time()))
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=chromium if os.path.exists(chromium) else None)
            a = browser.new_context(**MOBILE, locale="ar", color_scheme="dark").new_page()
            b = browser.new_context(**MOBILE, locale="ar").new_page()
            run.watch(a, "A")
            run.watch(b, "B")
            register(run, a, f"media-a{suffix}@example.com")
            register(run, b, f"media-b{suffix}@example.com")
            # V6 phase 17: posting pictures is the admin's per-account permission: A has it (not a member),
            # B is a member without it
            with sqlite3.connect(db_file) as con:
                con.execute("UPDATE users SET can_post_images = 1 WHERE email = ?", (f"media-a{suffix}@example.com",))
                con.execute("UPDATE users SET member_since = datetime('now') WHERE email = ?", (f"media-b{suffix}@example.com",))
            a.reload()

            print("Idea picture")
            a.goto(base + "/#/home")
            expect(a.locator("#idea-compose")).to_be_visible(timeout=10000)  # V6: Home = the ideas pane alone
            img_btn = a.get_by_role("button", name="إضافة صورة")
            expect(img_btn).to_be_visible(timeout=10000)
            with a.expect_file_chooser() as fc:
                img_btn.click()
            fc.value.set_files(str(pic1))
            expect(a.locator(".idea-media img")).to_be_visible(timeout=60000)  # on-device NSFW check passed (CSP 'self')
            run.step("picked: checked + compressed on the phone (NSFWJS loaded under the app CSP)")
            a.locator("#idea-compose").fill("غروب جميل اليوم")
            run.shot(a, "m01-composer-preview")
            a.get_by_role("button", name="نشر", exact=True).click()
            expect(a.get_by_text("نُشرت فكرتك")).to_be_visible(timeout=60000)
            card = a.locator(".post-card", has_text="غروب جميل اليوم").first
            expect(card.locator(".post-card__media img")).to_be_visible(timeout=15000)
            a.wait_for_function("() => { const i = document.querySelector('.post-card__media img'); return i && i.naturalWidth > 0; }",
                                timeout=15000)
            assert tg.documents and tg.documents[0]["chat_id"] == STORAGE, tg.documents
            assert any(c.get("chat_id") == MODCHAT for c in tg.copies), tg.copies
            run.step("uploaded → stored in the Telegram storage channel → copy with buttons in the moderation group")
            expect(a.get_by_text("الصورة التالية بعد")).to_be_visible()
            run.step("24 h limit shown with a countdown")
            run.shot(a, "m02-idea-posted")
            card.locator(".post-card__media").click()
            expect(a.locator(".viewer img")).to_be_visible()
            run.shot(a, "m03-viewer")
            a.get_by_role("button", name="إغلاق").click()
            expect(a.locator(".viewer")).to_have_count(0)

            b.goto(base + "/#/home")
            expect(b.locator("#idea-compose")).to_be_visible(timeout=10000)
            expect(b.get_by_role("button", name="أفكار أخرى")).to_be_visible(timeout=10000)
            expect(b.get_by_role("button", name="إضافة صورة")).to_have_count(0)  # a member, without the permission
            expect(b.get_by_role("button", name="الصور للأعضاء")).to_have_count(0)  # no membership link any more
            run.shot(b, "m02a-no-permission")
            run.step("a member without the admin's permission has no picture button (and no «للأعضاء» link)")
            b.get_by_role("button", name="أفكار أخرى").click()
            bcard = b.locator(".post-card", has_text="غروب جميل اليوم")
            expect(bcard.locator(".post-card__media img")).to_be_visible(timeout=15000)
            b.wait_for_function("() => [...document.querySelectorAll('.post-card__media img')].some((i) => i.naturalWidth > 0)",
                                timeout=15000)
            run.step("another user sees the picture (own signed URL)")

            print("Profile picture (V6 phase 3)")
            a.goto(base + "/#/profile")
            a.get_by_role("button", name="تغيير الصورة الشخصية").click()
            with a.expect_file_chooser() as fc:
                a.locator(".sheet").get_by_role("button", name="اختيار صورة").click()
            fc.value.set_files(str(pic3))
            expect(a.get_by_text("تم تحديث صورتك")).to_be_visible(timeout=60000)
            a.wait_for_function("() => { const i = document.querySelector('.id-card .avatar--photo img'); return i && i.naturalWidth > 0; }",
                                timeout=15000)
            dims = a.evaluate("() => { const i = document.querySelector('.id-card .avatar--photo img'); return [i.naturalWidth, i.naturalHeight]; }")
            assert dims[0] == dims[1] and dims[0] <= 512, dims  # square crop on the phone
            assert any("صورة شخصية" in (c.get("caption") or "") for c in tg.copies), tg.copies
            run.shot(a, "m02b-avatar")
            b.goto(base + "/#/users")
            row = b.locator(".user-row", has_text=a.evaluate("fetch('/api/me').then((r) => r.json()).then((j) => j.public_id)"))
            expect(row.locator(".avatar--photo img")).to_be_visible(timeout=15000)
            b.wait_for_function("() => [...document.querySelectorAll('.user-row .avatar--photo img')].some((i) => i.naturalWidth > 0)",
                                timeout=15000)
            run.shot(b, "m02c-users-with-avatar")
            run.step("profile picture: square 512 px on the phone → scanned → moderators notified → shown to others")

            print("Chat pictures: off for good")
            b_id = b.evaluate("fetch('/api/me').then((r) => r.json()).then((j) => j.public_id)")
            a.goto(base + "/#/users")
            a.locator(".people-search input").fill(b_id)
            a.locator(".people-search input").press("Enter")
            a.locator(".person").get_by_role("button", name="مراسلة").click()
            a.locator(".sheet textarea").fill("مرحبا، هل نتحدث؟")
            a.locator(".sheet").get_by_role("button", name="إرسال طلب المراسلة").click()
            expect(a.locator(".chat__hint")).to_contain_text("طلب مراسلة", timeout=10000)
            b.goto(base + "/#/messages")
            b.get_by_role("tab", name="طلبات الرسائل").click()
            b.locator(".conv-item").first.click()
            b.locator(".request-bar").get_by_role("button", name="قبول").click()
            b.locator(".chat__composer textarea").fill("أهلا بك")
            b.locator(".chat__composer .send-btn").click()
            expect(b.locator(".bubble", has_text="أهلا بك")).to_be_visible(timeout=10000)
            old_picture(db_file, f"media-a{suffix}@example.com", f"media-b{suffix}@example.com")  # sent before phase 17
            b.reload()
            expect(b.get_by_text("صورة محذوفة")).to_be_visible(timeout=15000)
            expect(b.get_by_role("button", name="إرسال صورة")).to_have_count(0)
            expect(b.locator(".chat__img-btn")).to_have_count(0)
            run.shot(b, "m04-chat-no-pictures")
            a.goto(base + "/#/messages")
            a.locator(".conv-item").first.click()
            expect(a.get_by_text("صورة محذوفة")).to_be_visible(timeout=15000)
            expect(a.locator(".chat__img-btn")).to_have_count(0)
            status = a.evaluate("""() => fetch('/api/uploads?purpose=chat', {method: 'POST', credentials: 'same-origin',
                headers: {'X-DZ-Requested': '1', 'Content-Type': 'application/octet-stream'}, body: new Uint8Array(2048)})
                .then((r) => r.status)""")
            assert status == 410, status
            run.step("chat: no picture button on either side, an old picture shows «صورة محذوفة», an upload gets 410")
            browser.close()
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        tg.server.shutdown()
    noise = ("favicon", "Failed to load resource")
    errors = [e for e in run.errors if not any(n in e for n in noise)]
    if errors:
        print("\nBrowser errors:\n  " + "\n  ".join(errors))
        return 1
    print(f"\nMEDIA E2E PASSED. Screenshots in {run.shots}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
