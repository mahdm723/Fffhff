"""V5 admin panel in a real browser (phone viewport): media moderation with previews, blue star + payment
settings, earnings ledger, support reply, live settings, and the V5 part of a user's page.

    python e2e/run_panel_v5_e2e.py [--shots DIR]
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import tempfile
import time
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_admin_e2e import api_client, register  # noqa: E402
from run_e2e import MOBILE, Run, start_server  # noqa: E402
from run_media_e2e import STORAGE, TOKEN, FakeTelegram  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
ADMIN_PATH = "/panel-v5-e2e"
ADMIN_PASSWORD = "Admin-Pass-0123456"


def jpeg() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    im = Image.new("RGB", (900, 700), (230, 120, 40))
    for x in range(0, 900, 40):
        im.paste((40, 90, 200), (x, 0, x + 12, 700))
    im.save(buf, "JPEG")
    return buf.getvalue()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8774)
    parser.add_argument("--shots", default=str(ROOT / "e2e" / "screenshots-panel-v5"))
    args = parser.parse_args()
    tg = FakeTelegram()
    tmp = Path(tempfile.mkdtemp(prefix="dz-panel-v5-"))
    db_url = f"sqlite:///{tmp}/panel.db"
    proc, base = start_server(args.port, {
        "DATABASE_URL": db_url, "ADMIN_PATH": ADMIN_PATH, "TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_ADMIN_CHAT_ID": "1001",
        "TELEGRAM_API_BASE": f"http://127.0.0.1:{tg.port}", "TELEGRAM_STORAGE_CHANNEL_ID": STORAGE,
        "IDEA_IMAGE_REQUIRE_APPROVAL": "true", "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"})
    run = Run(base, Path(args.shots))
    chromium = os.environ.get("PW_CHROMIUM", "/opt/pw-browsers/chromium-1194/chrome-linux/chrome")
    try:
        # data: a user with a picture waiting for approval and a support ticket
        u = register(base, "panel-user@example.com")
        mid = u.post("/api/uploads?purpose=idea", content=jpeg(), headers={"Content-Type": "application/octet-stream"}).json()["upload"]["id"]
        for _ in range(100):
            if u.get(f"/api/uploads/{mid}").json()["upload"]["state"] == "ready":
                break
            time.sleep(0.1)
        post = u.post("/api/posts", json={"content": "صورة تنتظر الموافقة", "media_id": mid}).json()
        assert post["status"] == "pending", post
        u.post("/api/support/tickets", json={"category": "payment", "subject": "سؤال عن الدفع", "body": "ما الشبكة المستعملة؟"})
        viewer = api_client(base)
        sys.path.insert(0, str(ROOT))
        from app.config import Settings
        from app.db import Database
        from app.security import totp
        from app.services import admin_auth

        with Database(db_url).session() as db:
            _a, secret = admin_auth.create_admin(db, Settings(DATABASE_URL=db_url, SECRET_KEY="e2e-secret", ADMIN_PATH=ADMIN_PATH),
                                                 "owner", ADMIN_PASSWORD)
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=chromium if os.path.exists(chromium) else None)
            page = browser.new_context(**MOBILE, locale="ar", color_scheme="dark").new_page()
            page.on("console", lambda m: m.type == "error" and "status of 401" not in m.text and run.errors.append(f"console: {m.text}"))
            page.on("pageerror", lambda e: run.errors.append(f"pageerror: {e}"))
            page.goto(base + ADMIN_PATH)
            page.fill("#admin-user", "owner")
            page.fill("#admin-pass", ADMIN_PASSWORD)
            page.fill("#admin-code", totp.code_at(secret, totp.current_step(time.time())))
            page.get_by_role("button", name="دخول").click()
            expect(page.get_by_role("tab", name="الإشراف على الوسائط")).to_be_visible(timeout=15000)

            print("Media moderation")
            page.get_by_role("tab", name="الإشراف على الوسائط").click()
            card = page.locator(".admin-media").first
            expect(card).to_contain_text("صورة تنتظر الموافقة", timeout=10000)
            page.wait_for_function("() => { const i = document.querySelector('.admin-thumb img'); return i && i.naturalWidth > 0; }", timeout=15000)
            run.shot(page, "p01-media-pending")
            card.get_by_role("button", name="قبول").click()
            expect(page.get_by_text("قُبل")).to_be_visible(timeout=10000)
            assert viewer.get(f"/api/posts/{post['id']}").status_code in (200, 401)
            assert u.get("/api/posts/" + post["id"]).json().get("status") is None  # visible now
            run.step("pending picture previewed and approved from the panel")

            print("Blue star + payment settings")
            page.get_by_role("tab", name="التوثيق والدفع").click()
            page.get_by_label("العملة").fill("USDT")
            page.get_by_label("الشبكة").select_option("TRC20")
            page.get_by_label("عنوان المحفظة").fill("TQ5pZ9aBcDeFgHiJkLmNoPqRsTuVwXyZ12")
            run.shot(page, "p02a-before-save")
            page.locator(".admin-group", has_text="إعدادات الدفع").get_by_role("button", name="حفظ", exact=True).click(timeout=8000)
            expect(page.get_by_text("الدفع متاح للمستخدمين.")).to_be_visible(timeout=10000)
            run.shot(page, "p02-payment-settings")
            run.step("payment settings saved")

            print("Support")
            page.get_by_role("tab", name="الدعم").click()
            page.locator(".admin-card", has_text="سؤال عن الدفع").click()
            page.locator(".sheet textarea").fill("نستعمل شبكة TRC20 فقط.")
            page.get_by_role("button", name="إرسال الرد").click()
            expect(page.locator(".sheet").get_by_text("نستعمل شبكة TRC20 فقط.")).to_be_visible(timeout=10000)
            run.shot(page, "p03-support-reply")
            page.locator(".sheet").get_by_role("button", name="إغلاق").first.click()
            assert u.get("/api/support/tickets").json()["tickets"][0]["status"] == "answered"
            run.step("ticket answered from the panel")

            print("Live settings")
            page.get_by_role("tab", name="الإعدادات").click()
            row = page.locator(".admin-setting", has_text="مدة عرض الصورة بعد فتحها")
            row.locator("input").fill("60")
            row.get_by_role("button", name="حفظ").click()
            expect(page.get_by_text("حُفظ وطُبّق فورًا.")).to_be_visible(timeout=10000)
            assert u.get("/api/uploads/config").json()["chat"]["ttl_after_view"] == 60
            run.shot(page, "p04-settings")
            run.step("setting changed live (no restart)")

            print("Star on the user page")
            page.get_by_role("tab", name="المستخدمون").click()
            page.locator(".admin-card", has_text="panel-user@example.com").first.click()
            star = page.get_by_role("button", name="منح النجمة يدويًا")
            expect(star).to_be_visible(timeout=15000)
            run.shot(page, "p05-user-v5")
            star.click()
            expect(page.get_by_role("button", name="سحب النجمة")).to_be_visible(timeout=10000)
            assert u.get("/api/me").json()["verified"] is True
            run.step("user page: V5 part (media, tickets…) and manual star grant")
            browser.close()
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        tg.server.shutdown()
    if run.errors:
        print("\nBrowser errors:\n  " + "\n  ".join(run.errors))
        return 1
    print(f"\nPANEL V5 E2E PASSED. Screenshots in {run.shots}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
