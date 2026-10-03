"""V4 browser test on two phone-sized browsers: names, DZ-ID, people search, message
requests, Messenger-style chat (typing, seen, time on tap), reveal identity, mute,
privacy switches and the one-time gender prompt for accounts created before V4.

    python e2e/run_messenger_e2e.py [--shots DIR]
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_e2e import MOBILE, Run, register  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def start_server(port: int) -> tuple[subprocess.Popen, str, str]:
    tmp = tempfile.mkdtemp(prefix="dz-e2e-v4-")
    db = f"{tmp}/e2e.db"
    env = dict(os.environ, DATABASE_URL=f"sqlite:///{db}", SECRET_KEY="e2e-secret", ENV="development",
               CLEANUP_INTERVAL="60", LOG_LEVEL="WARNING", REDIS_URL="", GOOGLE_CLIENT_ID="", MAX_ACCOUNTS_PER_IP="10")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port),
                             "--no-access-log", "--timeout-graceful-shutdown", "2"], cwd=ROOT, env=env)
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(base + "/healthz", timeout=1)
            return proc, base, db
        except Exception:  # noqa: BLE001
            time.sleep(0.1)
    proc.kill()
    raise RuntimeError("server did not start")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--shots", default=str(ROOT / "e2e" / "screenshots-v4"))
    args = parser.parse_args()
    proc, base, dbfile = start_server(args.port)
    run = Run(base, Path(args.shots))
    chromium = os.environ.get("PW_CHROMIUM", "/opt/pw-browsers/chromium-1194/chrome-linux/chrome")
    suffix = str(int(time.time()))
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=chromium if os.path.exists(chromium) else None)
            ctx_a = browser.new_context(**MOBILE, color_scheme="dark", locale="ar")
            ctx_b = browser.new_context(**MOBILE, color_scheme="light", locale="ar")
            ctx_a.grant_permissions(["clipboard-read", "clipboard-write"], origin=base)
            a, b = ctx_a.new_page(), ctx_b.new_page()
            run.watch(a, "A")
            run.watch(b, "B")

            print("Register with gender + 18+")
            register(run, a, f"amine{suffix}@example.com", gender="رجل")
            register(run, b, f"lina{suffix}@example.com", gender="أنثى")
            run.step("A (رجل) and B (أنثى) registered")

            print("Name + DZ-ID")
            a.goto(base + "/#/profile")
            chip = a.locator(".id-card .id-chip")
            expect(chip).to_contain_text("DZ-")
            a_id = chip.inner_text().strip()
            a.get_by_role("button", name="تعديل الملف").click()
            a.locator("#display-name").fill("ADM1N")
            a.locator(".sheet").get_by_role("button", name="حفظ").click()
            expect(a.locator(".sheet .form-error")).not_to_be_empty()
            run.step("A reserved / look-alike name is refused with a message")
            a.locator("#display-name").fill("أحمد")
            run.shot(a, "v4-01-edit-profile")
            a.locator(".sheet").get_by_role("button", name="حفظ").click()
            expect(a.locator(".id-card__name .name-line__text")).to_have_text("أحمد")
            expect(a.locator(".id-card .gender--male")).to_have_count(1)
            chip.click()
            assert a.evaluate("navigator.clipboard.readText()") == a_id
            run.shot(a, "v4-02-profile-named")
            run.step(f"A is now 'أحمد' with ♂ and ID {a_id} (copied)")
            a.get_by_role("button", name="تعديل الملف").click()
            expect(a.locator("#display-name")).to_be_disabled()
            expect(a.locator(".sheet .field__hint").first).to_contain_text("يمكنك تغيير الاسم مجددًا")
            a.keyboard.press("Escape")
            run.step("Name is locked until the cooldown ends (date shown)")

            print("Search (profile page)")
            b.goto(base + "/#/profile")
            b.locator(".people-search input").fill("احمد")  # no hamza: Arabic normalization
            b.locator(".people-search input").press("Enter")
            res = b.locator(".person")
            expect(res).to_have_count(1, timeout=10000)
            expect(res.locator(".person__name")).to_contain_text("أحمد")
            expect(res.locator(".person__id")).to_have_text(a_id)
            expect(res.locator(".gender--male")).to_have_count(1)
            assert "@example.com" not in b.locator(".people").inner_text()
            run.shot(b, "v4-03-search")
            b.locator(".people-search input").fill(a_id.lower())
            b.locator(".people-search input").press("Enter")
            expect(b.locator(".person__id")).to_have_text(a_id, timeout=10000)
            run.step("B found A by normalized name and by exact ID (no e-mail shown)")

            print("Message request")
            b.locator(".person").get_by_role("button", name="مراسلة").click()
            b.locator(".sheet textarea").fill("مرحبًا أحمد، وجدتك بالبحث.")
            b.locator(".sheet").get_by_role("button", name="إرسال طلب المراسلة").click()
            expect(b.locator(".chat__name")).to_contain_text("أحمد", timeout=10000)
            expect(b.locator(".chat__hint")).to_contain_text("طلب مراسلة")
            expect(b.get_by_role("button", name="مكالمة صوتية")).to_be_disabled()
            run.shot(b, "v4-04-request-sent")
            run.step("B sent a message request; calls stay disabled until A replies")

            a.goto(base + "/#/messages")
            req_tab = a.get_by_role("tab", name="طلبات الرسائل")
            expect(req_tab.locator(".badge")).to_have_text("1", timeout=10000)
            req_tab.click()
            item = a.locator(".conv-item").first
            expect(item.locator(".conv-item__name")).to_contain_text("dzplay")
            item.click()
            expect(a.locator(".request-bar")).to_be_visible()
            expect(a.locator(".chat__name")).to_contain_text("dzplay")
            run.shot(a, "v4-05-request-received")
            a.locator(".request-bar").get_by_role("button", name="قبول").click()
            expect(a.locator(".chat__composer textarea")).to_be_visible(timeout=10000)
            run.step("A saw the request in its own tab, accepted it")

            print("Typing indicator + seen")
            a.locator(".chat__composer textarea").press_sequentially("أهلًا لينا", delay=40)
            expect(b.locator(".typing-row")).to_be_visible(timeout=8000)
            run.shot(b, "v4-06-typing")
            a.get_by_role("button", name="إرسال").click()
            expect(b.locator(".bubble-row.theirs .bubble").last).to_contain_text("أهلًا لينا", timeout=10000)
            expect(b.locator(".typing-row")).to_have_count(0, timeout=8000)
            expect(a.locator(".bubble__status--read")).to_have_count(1, timeout=10000)
            expect(b.get_by_role("button", name="مكالمة صوتية")).to_be_enabled(timeout=10000)
            run.step("B saw 'typing…', got the message; A's message shows 'seen'; calls now enabled")

            b.locator(".bubble-row.theirs .bubble").last.click()
            expect(b.locator(".bubble-row.theirs.show-meta .bubble__detail time")).to_be_visible()
            run.shot(b, "v4-07-time-on-tap")
            run.step("Tapping a bubble shows its time")

            print("Mute")
            a.get_by_role("button", name="معلومات وخيارات").click()
            run.shot(a, "v4-08-chat-info")
            a.locator(".sheet").get_by_role("button", name="كتم الإشعارات").click()
            a.goto(base + "/#/messages")
            expect(a.locator(".conv-item.is-muted")).to_have_count(1, timeout=10000)
            run.step("A muted the chat (bell-off in the list)")

            print("Anonymous chat stays dzplay until a reveal")
            ctx_c = browser.new_context(**MOBILE, color_scheme="dark", locale="ar")
            c = ctx_c.new_page()
            run.watch(c, "C")
            register(run, c, f"sami{suffix}@example.com")  # A already chats with B: the random message goes to C
            a.locator(".msg-dock textarea").fill("رسالة عشوائية لشخص مجهول")
            a.locator(".msg-dock .send-btn").click()
            expect(a.get_by_text("وصلت رسالتك إلى شخص ما")).to_be_visible(timeout=10000)
            c.goto(base + "/#/messages")
            anon = c.locator(".conv-item").first
            expect(anon.locator(".conv-item__kind")).to_have_count(1, timeout=10000)
            expect(anon.locator(".conv-item__name")).to_have_text("dzplay")
            a.goto(base + "/#/messages")
            expect(a.locator(".conv-item")).to_have_count(2)
            run.shot(a, "v4-09-list-mixed")
            a.locator(".conv-item", has=a.locator(".conv-item__kind")).click()
            expect(a.get_by_role("button", name="مكالمة فيديو")).to_be_disabled()
            a.get_by_role("button", name="معلومات وخيارات").click()
            a.locator(".sheet").get_by_role("button", name="كشف هويتي").click()
            a.locator(".sheet").get_by_role("button", name="كشف هويتي").click()
            expect(a.locator(".sys-msg")).to_contain_text("كشفتَ هويتك", timeout=10000)
            expect(a.locator(".chat__name")).to_contain_text("dzplay")  # C stays anonymous to A
            anon.click()
            expect(c.locator(".sys-msg")).to_contain_text("أحمد", timeout=10000)
            expect(c.locator(".chat__name")).to_contain_text("أحمد")
            expect(c.locator(".chat__name .gender--male")).to_have_count(1)
            run.shot(c, "v4-10-revealed")
            run.step("A revealed in an anonymous chat: C sees a system message + A's name; C stays dzplay for A")
            ctx_c.close()

            print("Privacy switches")
            b.goto(base + "/#/profile")
            b.get_by_role("button", name="الخصوصية والتواصل").click()
            sw = b.locator(".sheet .switch[aria-label='الظهور في البحث بالاسم']")
            expect(sw).to_have_attribute("aria-checked", "true")
            b.locator(".sheet .menu__item", has=b.locator(".switch[aria-label='الظهور في البحث بالاسم']")).click()
            expect(sw).to_have_attribute("aria-checked", "false")
            run.shot(b, "v4-11-privacy")
            b.keyboard.press("Escape")
            b.reload()
            b.get_by_role("button", name="الخصوصية والتواصل").click()
            expect(b.locator(".sheet .switch[aria-label='الظهور في البحث بالاسم']")).to_have_attribute("aria-checked", "false")
            b.keyboard.press("Escape")
            run.step("Privacy switch saved server-side")

            print("Keyboard + RTL")
            assert a.evaluate("document.documentElement.dir") == "rtl"
            a.goto(base + "/#/messages")
            a.locator(".conv-item").first.click()
            ta = a.locator(".chat__composer textarea")
            ta.focus()
            box = ta.bounding_box()
            assert box and box["y"] + box["height"] <= 844, box
            run.step("Composer stays inside the viewport; page is RTL")

            print("Accounts from before V4: one gentle gender prompt")
            with sqlite3.connect(dbfile) as conn:
                conn.execute("UPDATE users SET gender = NULL, gender_asked_at = NULL WHERE email LIKE ?", (f"lina{suffix}%",))
            b.goto(base + "/#/home")
            b.reload()
            expect(b.locator(".sheet h2", has_text="جديد في DZPLAY")).to_be_visible(timeout=10000)
            run.shot(b, "v4-12-gentle-gender")
            b.locator(".sheet").get_by_role("button", name="لاحقًا").click()
            b.reload()
            b.wait_for_timeout(1500)
            expect(b.locator(".sheet h2", has_text="جديد في DZPLAY")).to_have_count(0)
            run.step("Old account got the gender prompt once; 'later' is remembered")

            for page, who in ((a, "A"), (b, "B")):
                assert "@example.com" not in page.content(), f"{who} page leaks an e-mail"
            run.step("No e-mail in either page")
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    noise = ("favicon", "Failed to load resource")
    errors = [e for e in run.errors if not any(n in e for n in noise)]
    if errors:
        print("\nBrowser errors:\n  " + "\n  ".join(errors))
        return 1
    print(f"\nV4 messenger E2E passed. Screenshots in {run.shots}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
