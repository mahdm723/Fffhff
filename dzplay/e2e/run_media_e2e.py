"""V5 phase B in a real browser (phone viewport): idea picture + ephemeral chat picture.

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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8771)
    parser.add_argument("--shots", default=str(ROOT / "e2e" / "screenshots-media"))
    args = parser.parse_args()
    tg = FakeTelegram()
    env = {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_ADMIN_CHAT_ID": "1001", "TELEGRAM_API_BASE": f"http://127.0.0.1:{tg.port}",
           "TELEGRAM_STORAGE_CHANNEL_ID": STORAGE, "TELEGRAM_MODERATION_CHAT_ID": MODCHAT,
           "CHAT_IMAGE_TTL_AFTER_VIEW": "8", "MEDIA_TICK_SECONDS": "1", "CHAT_IMAGE_REPORT_GRACE": "1",
           "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}
    proc, base = start_server(args.port, env)
    run = Run(base, Path(args.shots))
    tmp = Path(tempfile.mkdtemp(prefix="dz-media-e2e-"))
    pic1, pic2 = photo(tmp / "idea.jpg"), photo(tmp / "chat.jpg", (30, 160, 90))
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
            expect(b.locator("#idea-compose")).to_be_visible(timeout=10000)  # V6: Home = the ideas pane alone
            b.get_by_role("button", name="أفكار أخرى").click()
            bcard = b.locator(".post-card", has_text="غروب جميل اليوم")
            expect(bcard.locator(".post-card__media img")).to_be_visible(timeout=15000)
            b.wait_for_function("() => [...document.querySelectorAll('.post-card__media img')].some((i) => i.naturalWidth > 0)",
                                timeout=15000)
            run.step("another user sees the picture (own signed URL)")

            print("Ephemeral chat picture")
            b_id = b.evaluate("fetch('/api/me').then((r) => r.json()).then((j) => j.public_id)")
            a.goto(base + "/#/profile")
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
            a.goto(base + "/#/messages")
            a.locator(".conv-item").first.click()
            chat_img = a.get_by_role("button", name="إرسال صورة")
            expect(chat_img).to_be_enabled(timeout=15000)
            with a.expect_file_chooser() as fc:
                chat_img.click()
            fc.value.set_files(str(pic2))
            expect(a.get_by_text("صورة · لم تُفتح بعد")).to_be_visible(timeout=60000)
            run.shot(a, "m04-chat-sent")
            run.step("sent after the other side replied (button enabled only then)")

            expect(b.get_by_text("اضغط للعرض")).to_be_visible(timeout=15000)
            expect(b.locator(".img-msg__blur")).to_be_visible()
            run.shot(b, "m05-chat-blurred")
            b.get_by_text("اضغط للعرض").click()
            expect(b.locator(".viewer img")).to_be_visible(timeout=15000)
            b.wait_for_function("() => { const i = document.querySelector('.viewer img'); return i && i.naturalWidth > 0; }")
            expect(b.locator(".viewer__timer")).to_contain_text("ث")
            run.shot(b, "m06-chat-open-countdown")
            run.step("recipient opens it: full screen with a countdown")
            expect(a.locator(".img-msg__timer")).to_be_visible(timeout=10000)
            run.step("sender sees the countdown too")
            expect(b.locator(".viewer")).to_have_count(0, timeout=15000)
            expect(b.get_by_text("انتهت صلاحية الصورة")).to_be_visible(timeout=5000)
            expect(a.get_by_text("انتهت صلاحية الصورة")).to_be_visible(timeout=10000)
            run.shot(b, "m07-chat-expired")
            run.step("after the countdown: gone on both sides")
            b.reload()
            expect(b.get_by_text("انتهت صلاحية الصورة")).to_be_visible(timeout=15000)
            deadline = time.time() + 10
            chat_doc = tg.documents[-1]
            while time.time() < deadline and (STORAGE, chat_doc["message_id"]) not in tg.deleted:
                time.sleep(0.3)
            assert (STORAGE, chat_doc["message_id"]) in tg.deleted, tg.deleted
            run.step("deleted from the Telegram storage channel after the short report window")
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
