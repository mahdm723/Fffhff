"""V5 phase D in a real browser (phone viewport): «استوديو DZPLAY» → review → Reels feed with the
creator's name and star → profile reels; monetization request → «أموالي» with an earning and a Red Packet payout.

    python e2e/run_creator_e2e.py [--shots DIR]
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_account_e2e import ADMIN_PATH, admin_client  # noqa: E402
from run_e2e import MOBILE, Run, register, start_server  # noqa: E402
from run_media_e2e import MODCHAT, STORAGE, TOKEN, FakeTelegram  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def make_clip(path: Path) -> Path:
    sys.path.insert(0, str(ROOT))
    from tests.conftest import ffmpeg_binary

    subprocess.run([ffmpeg_binary(), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc=size=360x640:rate=25", "-f", "lavfi", "-i", "sine=frequency=500", "-t", "5",
                    "-c:v", "libvpx", "-b:v", "600k", "-c:a", "libvorbis", "-shortest", str(path)], check=True, timeout=120)
    return path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8773)
    parser.add_argument("--shots", default=str(ROOT / "e2e" / "screenshots-creator"))
    args = parser.parse_args()
    tg = FakeTelegram()
    tmp = Path(tempfile.mkdtemp(prefix="dz-creator-e2e-"))
    db_url = f"sqlite:///{tmp}/creator.db"
    env = {"DATABASE_URL": db_url, "ADMIN_PATH": ADMIN_PATH, "TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_ADMIN_CHAT_ID": "1001",
           "TELEGRAM_API_BASE": f"http://127.0.0.1:{tg.port}", "TELEGRAM_STORAGE_CHANNEL_ID": STORAGE,
           "TELEGRAM_MODERATION_CHAT_ID": MODCHAT, "MONETIZE_MIN_REELS": "1", "MONETIZE_MIN_LIKES": "0",
           "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}
    sys.path.insert(0, str(ROOT))
    from tests.conftest import ffmpeg_binary, ffprobe_shim
    import shutil

    env["FFMPEG_BINARY"] = ffmpeg_binary()
    env["FFPROBE_BINARY"] = "ffprobe" if shutil.which("ffprobe") else ffprobe_shim()
    proc, base = start_server(args.port, env)
    run = Run(base, Path(args.shots))
    clip = make_clip(tmp / "reel.webm")  # WebM: Playwright's Chromium cannot decode H.264 (phones can)
    chromium = os.environ.get("PW_CHROMIUM", "/opt/pw-browsers/chromium-1194/chrome-linux/chrome")
    suffix = str(int(time.time()))
    try:
        adm = admin_client(base, db_url)
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=chromium if os.path.exists(chromium) else None)
            a = browser.new_context(**MOBILE, locale="ar", color_scheme="dark").new_page()
            b = browser.new_context(**MOBILE, locale="ar").new_page()
            run.watch(a, "A")
            run.watch(b, "B")
            email = f"creator{suffix}@example.com"
            register(run, a, email)
            register(run, b, f"viewer{suffix}@example.com")
            a.goto(base + "/#/profile")
            a.get_by_role("button", name="تعديل الملف").click()
            a.locator("#display-name").fill("Nour")
            a.locator(".sheet").get_by_role("button", name="حفظ").click()
            expect(a.get_by_text("حُفظ ملفك.")).to_be_visible(timeout=10000)
            uid = adm.get("/api/admin/access/users", params={"q": email}).json()["users"][0]["id"]
            assert adm.post(f"/api/admin/users/{uid}/verified", json={"verified": True}).status_code == 200

            print("Studio")
            a.goto(base + "/#/profile")
            a.reload()
            a.get_by_role("button", name="استوديو DZPLAY").click()
            expect(a.get_by_text("مرحبًا في استوديو DZPLAY")).to_be_visible(timeout=10000)
            with a.expect_file_chooser() as fc:
                a.get_by_role("button", name="اختيار فيديو").click()
            fc.value.set_files(str(clip))
            expect(a.locator(".studio__preview video")).to_be_visible(timeout=60000)
            a.locator("#studio-caption").fill("أول فيديو من الاستوديو")
            run.shot(a, "d01-studio-draft")
            a.get_by_role("button", name="إرسال", exact=True).click()
            expect(a.get_by_text("قيد المراجعة").first).to_be_visible(timeout=90000)
            run.shot(a, "d02-studio-pending")
            run.step("picked → checked on the phone → uploaded → re-encoded → pending review")
            assert any(c.get("chat_id") == MODCHAT for c in tg.copies)
            item = adm.get("/api/admin/media", params={"filter": "pending"}).json()["items"][0]
            assert adm.post(f"/api/admin/media/{item['id']}/action", json={"action": "ok"}).json()["result"]
            expect(a.get_by_text("نُشر الفيديو في Reels")).to_be_visible(timeout=15000)
            run.shot(a, "d03-studio-published")
            run.step("approved from the panel → the studio says it is published (live)")

            b.goto(base + "/#/home")
            b.reload()
            author = b.locator(".reel__author", has_text="Nour").first
            expect(author).to_be_visible(timeout=20000)
            expect(author.locator(".star-mark")).to_be_visible()
            run.shot(b, "d04-reel-with-author")
            author.click()
            expect(b.locator(".profile-reels img")).to_be_visible(timeout=15000)
            run.shot(b, "d05-profile-reels")
            run.step("in the Reels feed with name + star → profile lists the reel")

            print("Monetization + «أموالي»")
            a.goto(base + "/#/profile")
            a.reload()
            a.get_by_role("button", name="تحقيق الدخل").click()
            a.locator("#monetize-content").fill("مقاطع تعليمية قصيرة")
            a.locator("#monetize-terms").check()
            run.shot(a, "d06-monetize-form")
            a.get_by_role("button", name="إرسال الطلب").click()
            expect(a.get_by_text("قيد المراجعة").first).to_be_visible(timeout=10000)
            app_id = adm.get("/api/admin/monetization").json()["applications"][0]["id"]
            assert adm.post(f"/api/admin/monetization/{app_id}/decide", json={"action": "accept"}).status_code == 200
            expect(a.get_by_text("رصيدك الحالي")).to_be_visible(timeout=15000)
            assert adm.post(f"/api/admin/users/{uid}/ledger", json={"kind": "earning", "amount": "10.50", "note": "أكتوبر"}).status_code == 201
            assert adm.post(f"/api/admin/users/{uid}/ledger", json={"kind": "payout", "amount": "4", "paid_on": "2026-10-04"}).status_code == 201
            expect(a.get_by_text("6.50 USDT")).to_be_visible(timeout=15000)
            expect(a.get_by_text("دفعة (الظرف الأحمر)")).to_be_visible()
            run.shot(a, "d07-money")
            run.step("accepted → «أموالي»: earning + Red Packet payout, balance updates live")
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
    print(f"\nCREATOR E2E PASSED. Screenshots in {run.shots}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
