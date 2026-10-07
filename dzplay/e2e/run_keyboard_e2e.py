"""V5: the on-screen keyboard never covers a text field or its send button.

The Android app (and Chrome with interactive-widget=resizes-content) shrink the page when the
keyboard opens; this test focuses each form and shrinks the viewport the same way, then checks
that the field and its send button are fully visible and the bottom navigation is hidden.

    python e2e/run_keyboard_e2e.py [--shots DIR]
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_e2e import MOBILE, Run, register, start_server  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FULL = {"width": 390, "height": 844}
WITH_KEYBOARD = {"width": 390, "height": 430}  # ≈ a phone keyboard taking half the screen


def visible(page: Page, locator) -> bool:
    box = locator.bounding_box()
    return bool(box) and box["y"] >= 0 and box["y"] + box["height"] <= page.viewport_size["height"] + 1


def check(run: Run, page: Page, field, button, name: str) -> None:
    field.click()
    page.set_viewport_size(WITH_KEYBOARD)
    page.wait_for_timeout(500)
    assert visible(page, field), f"{name}: field hidden by the keyboard"
    if button is not None:
        assert visible(page, button), f"{name}: send button hidden by the keyboard"
    nav = page.locator(".nav")
    assert nav.count() == 0 or not nav.is_visible(), f"{name}: navigation bar still shown over the keyboard"
    run.shot(page, f"kb-{name}")
    page.keyboard.press("Escape") if page.locator(".sheet").count() else None
    page.evaluate("document.activeElement && document.activeElement.blur()")
    page.set_viewport_size(FULL)
    page.wait_for_timeout(300)
    run.step(f"{name}: field + button above the keyboard, navigation hidden")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8768)
    parser.add_argument("--shots", default=str(ROOT / "e2e" / "screenshots-keyboard"))
    args = parser.parse_args()
    proc, base = start_server(args.port)
    run = Run(base, Path(args.shots))
    chromium = os.environ.get("PW_CHROMIUM", "/opt/pw-browsers/chromium-1194/chrome-linux/chrome")
    suffix = str(int(time.time()))
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=chromium if os.path.exists(chromium) else None)
            a = browser.new_context(**MOBILE, locale="ar").new_page()
            b = browser.new_context(**MOBILE, locale="ar").new_page()
            run.watch(a, "A")
            run.watch(b, "B")
            register(run, a, f"kba{suffix}@example.com")
            register(run, b, f"kbb{suffix}@example.com")

            a.goto(base + "/#/home")
            expect(a.locator("#idea-compose")).to_be_visible(timeout=10000)  # V6: Home = the ideas pane alone
            a.locator(".post-card").first.wait_for(state="attached", timeout=10000) if a.locator(".post-card").count() else None
            check(run, a, a.locator("#idea-compose"), a.get_by_role("button", name="نشر", exact=True), "idea")
            a.locator("#idea-compose").fill("فكرة لتجربة لوحة المفاتيح")
            a.get_by_role("button", name="نشر", exact=True).click()

            # V6: no random-message composer; a direct message request instead
            b_id = b.evaluate("fetch('/api/me').then((r) => r.json()).then((j) => j.public_id)")
            a.goto(base + "/#/users")
            a.locator(".people-search input").fill(b_id)
            a.locator(".people-search input").press("Enter")
            a.locator(".person").get_by_role("button", name="مراسلة").click()
            send_req = a.locator(".sheet").get_by_role("button", name="إرسال طلب المراسلة")
            check(run, a, a.locator(".sheet textarea"), send_req, "request-sheet")  # closes the sheet (Escape)
            a.locator(".person").get_by_role("button", name="مراسلة").click()
            a.locator(".sheet textarea").fill("مرحبا")
            send_req.click()
            expect(a.locator(".chat__composer textarea")).to_be_visible(timeout=10000)
            check(run, a, a.locator(".chat__composer textarea"), a.locator(".chat__composer .send-btn"), "chat")

            b.goto(base + "/#/home")
            expect(b.locator("#idea-compose")).to_be_visible(timeout=10000)  # V6: Home = the ideas pane alone
            b.get_by_role("button", name="أفكار أخرى").click()
            card = b.locator(".post-card", has_text="فكرة لتجربة لوحة المفاتيح")
            expect(card).to_be_visible(timeout=10000)
            card.get_by_role("button", name="التعليقات").click()
            check(run, b, b.locator(".comments-sheet textarea"), b.locator(".comments-sheet").get_by_role("button", name="إرسال التعليق"), "comment-sheet")

            a.goto(base + "/#/users")
            check(run, a, a.locator(".people-search input"), a.locator(".people-search button"), "people-search")
            a.goto(base + "/#/profile")
            a.get_by_role("button", name="تعديل الملف").click()
            check(run, a, a.locator("#display-name"), a.locator(".sheet").get_by_role("button", name="حفظ"), "edit-profile")
            browser.close()
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    noise = ("favicon", "Failed to load resource")
    errors = [e for e in run.errors if not any(n in e for n in noise)]
    if errors:
        print("\nBrowser errors:\n  " + "\n  ".join(errors))
        return 1
    print(f"\nKEYBOARD E2E PASSED. Screenshots in {run.shots}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
