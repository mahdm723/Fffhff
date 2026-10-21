"""V6 phase 19 (browser): the official account, from the panel to a user's phone.

1. «الحساب الرسمي»: the owner writes a post, enters a fresh authenticator code and publishes it as DALTA.BIT.
2. «تثبيت» (24 h): a user opens Home and the post is first, «📌 مثبّت», with the gold badge; a tap on the badge
   explains it («الحساب الرسمي لـ …»).
3. «إلغاء التثبيت», then «حذف»: the post leaves the user's ideas.
Every screen fits a 390 px phone (the overflow checks of run_overflow_e2e).

    PW_CHROMIUM=… python e2e/run_official_e2e.py
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import run_admin_e2e as ae  # noqa: E402
import run_overflow_e2e as ro  # noqa: E402
from run_e2e import Run  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TEXT = "إعلان رسمي: نسخة جديدة من التطبيق متاحة الآن."


def panel_login(page: Page, base: str) -> None:
    from app.security import totp

    page.goto(base + ae.ADMIN_PATH)
    page.fill("#admin-user", "owner")
    page.fill("#admin-pass", ae.ADMIN_PASSWORD)
    page.fill("#admin-code", totp.code_at(ae.ADMIN_TOTP["owner"], totp.current_step(time.time())))
    page.get_by_role("button", name="دخول").click()
    expect(page.locator(".admin-tile__num").first).to_be_visible(timeout=15000)


def fresh_code() -> str:
    """Each authenticator step is used once (the sign-in took this one): wait for the next."""
    from app.security import totp

    time.sleep(max(0.0, totp.STEP - (time.time() % totp.STEP)) + 1)
    return totp.code_at(ae.ADMIN_TOTP["owner"], totp.current_step(time.time()))


def user_home(page: Page, base: str) -> None:
    page.goto(base + "/#/home")
    page.reload()  # the same URL: load the ideas again
    expect(page.locator("#idea-compose")).to_be_visible(timeout=15000)
    page.wait_for_timeout(600)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8793)
    parser.add_argument("--shots", default=str(ROOT / "e2e" / "screenshots-official"))
    args = parser.parse_args()
    proc, base, db_url = ae.start_server(args.port)
    run = Run(base, Path(args.shots))
    try:
        ae.seed(base, db_url)
        reader = ae.register(base, "reader@example.com")
        chromium = os.environ.get("PW_CHROMIUM", "/opt/pw-browsers/chromium-1194/chrome-linux/chrome")
        with sync_playwright() as p:
            browser = p.chromium.launch(**({"executable_path": chromium} if Path(chromium).exists() else {}))
            admin_ctx = browser.new_context(**ae.MOBILE, color_scheme="dark", locale="ar-DZ", timezone_id="Africa/Algiers")
            panel = admin_ctx.new_page()
            panel.set_default_timeout(15000)
            run.watch(panel, "panel")
            user_ctx = browser.new_context(**ae.MOBILE, color_scheme="light", locale="ar-DZ", timezone_id="Africa/Algiers")
            user_ctx.add_cookies([{"name": c.name, "value": c.value, "domain": "127.0.0.1", "path": "/"} for c in reader.cookies.jar])
            app = user_ctx.new_page()
            app.set_default_timeout(15000)
            run.watch(app, "app")

            print("1. Publish from the panel", flush=True)
            panel_login(panel, base)
            panel.locator("#tab-official").click()
            expect(panel.locator(".admin-official__who .gold-mark--metal")).to_be_visible()
            panel.wait_for_timeout(700)
            ro.check(panel, "panel «الحساب الرسمي»", admin=True)
            panel.fill("#official-text", TEXT)
            panel.get_by_role("button", name="نشر باسم الحساب الرسمي").click()  # no code yet: refused in the page
            expect(panel.locator(".toast", has_text="أدخل رمز التحقق")).to_be_visible()
            panel.fill("#official-code", fresh_code())
            panel.get_by_role("button", name="نشر باسم الحساب الرسمي").click()
            expect(panel.locator(".toast", has_text="نُشر باسم الحساب الرسمي.")).to_be_visible()
            card = panel.locator(".admin-list .admin-card", has_text=TEXT)
            expect(card).to_have_count(1)
            run.step("published with the 2FA code (refused without it)")

            print("2. Pin it; a user sees it first with the gold badge", flush=True)
            card.get_by_role("button", name="تثبيت").click()
            expect(panel.locator(".admin-list .admin-card", has_text="📌 مثبّت حتى")).to_have_count(1)
            panel.wait_for_timeout(500)
            ro.check(panel, "panel «الحساب الرسمي» pinned", admin=True)
            run.shot(panel, "panel-official")
            user_home(app, base)
            first = app.locator(".post-card").first
            expect(first).to_contain_text(TEXT)
            expect(first.locator(".post-card__pin")).to_be_visible()
            expect(first.locator(".gold-mark")).to_have_count(1)
            ro.problem_if("post-card--pinned" not in (first.get_attribute("class") or ""), "the pinned card is not marked")
            ro.check(app, "home with the pinned official post")
            run.shot(app, "home-pinned")
            first.locator(".gold-mark").click()
            expect(app.locator(".official-sheet")).to_contain_text("الحساب الرسمي لـ DALTA.BIT")
            app.wait_for_timeout(400)
            ro.check(app, "the gold badge sheet", ".sheet")
            run.shot(app, "badge-sheet")
            app.keyboard.press("Escape")
            run.step("first on Home, «📌 مثبّت», gold badge, tap sheet")

            print("3. Unpin, then delete", flush=True)
            panel.locator(".admin-list .admin-card", has_text=TEXT).get_by_role("button", name="إلغاء التثبيت").click()
            expect(panel.locator(".admin-list .admin-card", has_text="📌 مثبّت حتى")).to_have_count(0)
            user_home(app, base)
            ro.problem_if(app.locator(".post-card--pinned").count() != 0, "still pinned after «إلغاء التثبيت»")
            panel.locator(".admin-list .admin-card", has_text=TEXT).get_by_role("button", name="حذف").click()
            panel.locator(".sheet").get_by_role("button", name="حذف").click()
            expect(panel.locator(".toast", has_text="حُذف.")).to_be_visible()
            expect(panel.locator(".admin-list .admin-card", has_text=TEXT)).to_have_count(0)
            user_home(app, base)
            ro.problem_if(app.locator(".post-card", has_text=TEXT).count() != 0, "the deleted official post is still shown")
            run.step("unpinned and deleted")
            browser.close()
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    # the panel's first visit asks for its session (401 before signing in): expected
    problems = ro.PROBLEMS + [e for e in run.errors if "status of 401" not in e]
    if problems:
        print(f"\nOFFICIAL E2E FAILED: {len(problems)} problem(s)")
        for p_ in problems:
            print("  -", p_)
        return 1
    print(f"\nOFFICIAL E2E PASSED. Screenshots in {run.shots}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
