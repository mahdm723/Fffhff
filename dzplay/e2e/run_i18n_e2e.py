"""V6 phase 12: three languages in a phone browser.

1. First visit: the phone's language (fr) — the sign-in screen in French, left to right, the logo not mirrored.
2. After sign-in the language in use is saved on the account; the account's language wins on another device.
3. Settings → Language: French → English → Arabic, each time the whole app redraws in that language and direction.
4. The panel («اللغات والنصوص», always Arabic): an English text edited there reaches an open app at once, without a
   reload or a deployment; back to the default text the same way; history keeps both.
5. Pages without JavaScript (policies, download) follow ?lang= and remember it.

    PW_CHROMIUM=… python e2e/run_i18n_e2e.py
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import run_admin_e2e as ae  # noqa: E402
from run_e2e import Run  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PHONE = {"viewport": {"width": 390, "height": 844}, "device_scale_factor": 2, "is_mobile": True, "has_touch": True}

MIRRORED_JS = r"""(sel) => {
  // is any ancestor of the logo flipped horizontally (a negative x scale)?
  const el = document.querySelector(sel);
  if (!el) return 'missing';
  for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
    const t = getComputedStyle(n).transform;
    if (t && t !== 'none' && parseFloat(t.slice(t.indexOf('(') + 1)) < 0) return n.tagName + '.' + n.className;
  }
  const r = el.getBoundingClientRect();
  return r.width > 0 ? '' : 'hidden';
}"""


def html_lang(page: Page) -> tuple[str, str]:
    return tuple(page.evaluate("() => [document.documentElement.lang, document.documentElement.dir]"))


def nav_labels(page: Page) -> list[str]:
    return [x.strip() for x in page.locator(".nav__btn").all_inner_texts()]


def main() -> int:
    proc, base, db_url = ae.start_server(8776)
    run = Run(base, ROOT / "e2e" / "screenshots-i18n")
    problems: list[str] = []
    try:
        ae.seed(base, db_url)
        admin = "owner"  # created by the seed (run_admin_e2e)
        chromium = os.environ.get("PW_CHROMIUM", "")
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=chromium if os.path.exists(chromium) else None)

            # 1. first visit in French
            ctx = browser.new_context(**PHONE, locale="fr-FR", color_scheme="dark")
            page = ctx.new_page()
            page.set_default_timeout(15000)
            run.watch(page, "fr")
            page.goto(base + "/")
            expect(page.get_by_role("tab", name="Se connecter")).to_be_visible()
            assert html_lang(page) == ("fr", "ltr"), html_lang(page)
            expect(page.locator(".lang-btn")).to_contain_text("Français")
            flipped = page.evaluate(MIRRORED_JS, ".auth .logo-mark")
            assert flipped == "", f"the logo is mirrored or missing on the sign-in screen: {flipped}"
            run.shot(page, "fr-signin")
            page.get_by_role("tab", name="Nouveau compte").click()
            expect(page.get_by_role("button", name="Créer le compte")).to_be_visible()
            run.shot(page, "fr-signup")
            ctx.close()
            run.step("first visit: the phone's language (French), left to right, the logo not mirrored")

            # 2. an account without a language takes the one in use; the account's language wins later
            c = ae.register(base, "lang-user@example.com")
            assert c.get("/api/me").json().get("language") is None
            ctx = browser.new_context(**PHONE, locale="en-GB", color_scheme="dark")
            ctx.add_cookies([{"name": k.name, "value": k.value, "domain": "127.0.0.1", "path": "/"} for k in c.cookies.jar])
            page = ctx.new_page()
            page.set_default_timeout(15000)
            run.watch(page, "en")
            page.goto(base + "/#/home")
            expect(page.locator(".nav__btn").first).to_contain_text("Home")
            assert html_lang(page) == ("en", "ltr")
            for _ in range(50):
                if c.get("/api/me").json().get("language") == "en":
                    break
                time.sleep(0.1)
            assert c.get("/api/me").json()["language"] == "en", "the language in use was not saved on the account"
            run.shot(page, "en-home")
            run.step("after sign-in the language in use (English) is saved on the account")

            # 3. Settings → Language
            for code, name, home, direction in (("fr", "Français", "Accueil", "ltr"), ("ar", "العربية", "الرئيسية", "rtl"),
                                                ("en", "English", "Home", "ltr")):
                page.goto(base + "/#/profile")
                expect(page.locator(".id-card__name")).to_be_visible()
                page.get_by_role("button", name={"en": "Language", "fr": "Langue", "ar": "اللغة"}[html_lang(page)[0]]).click()
                with page.expect_navigation():
                    page.locator(f".sheet [data-id={code}]").click()
                expect(page.locator(".nav__btn").first).to_contain_text(home)
                assert html_lang(page) == (code, direction), html_lang(page)
                assert c.get("/api/me").json()["language"] == code
                flipped = page.evaluate(MIRRORED_JS, ".logo-mark")
                assert flipped in ("", "missing"), f"{code}: the logo is mirrored: {flipped}"
                run.shot(page, f"switch-{code}")
            run.step("Settings → Language: French, Arabic (right to left), English — saved on the account each time")

            other = browser.new_context(**PHONE, locale="ar", color_scheme="dark")  # another device, Arabic phone
            other.add_cookies([{"name": k.name, "value": k.value, "domain": "127.0.0.1", "path": "/"} for k in c.cookies.jar])
            op = other.new_page()
            op.goto(base + "/#/home")
            expect(op.locator(".nav__btn").first).to_contain_text("Home")
            assert html_lang(op) == ("en", "ltr")
            other.close()
            run.step("another device (an Arabic phone) opens in the account's language (English)")

            # 4. a live edit from the panel
            from app.security import totp

            actx = browser.new_context(**PHONE, locale="ar", color_scheme="dark")
            ap = actx.new_page()
            ap.set_default_timeout(15000)
            run.watch(ap, "panel")
            ap.goto(base + ae.ADMIN_PATH)
            ap.fill("#admin-user", admin)
            ap.fill("#admin-pass", ae.ADMIN_PASSWORD)
            ap.fill("#admin-code", totp.code_at(ae.ADMIN_TOTP[admin], totp.current_step(time.time())))
            ap.get_by_role("button", name="دخول").click()
            expect(ap.locator(".admin-tile__num").first).to_be_visible()
            ap.locator("#tab-texts").click()
            expect(ap.locator("#tab-texts")).to_contain_text("اللغات والنصوص")
            expect(ap.locator(".i18n-row").first).to_be_visible()
            expect(ap.locator(".admin-filters")).to_contain_text("ناقصة بالفرنسية: 0")
            assert html_lang(ap)[1] == "rtl"
            ap.locator(".admin-filter input").fill("الرئيسية")
            row = ap.locator(".i18n-row", has=ap.locator(".i18n-cell__text[lang=ar]").get_by_text("الرئيسية", exact=True)).first
            expect(row).to_be_visible()
            run.shot(ap, "panel-search")
            row.locator(".i18n-cell").nth(2).click()  # the English cell
            expect(ap.locator(".sheet textarea")).to_have_value("Home")
            ap.locator(".sheet textarea").fill("Start")
            ap.locator(".sheet").get_by_role("button", name="حفظ").click()
            expect(ap.locator(".sheet")).to_contain_text("معدّل")
            run.shot(ap, "panel-edited")
            expect(page.locator(".nav__btn").first).to_contain_text("Start", timeout=10000)  # no reload
            run.step("an English text edited in the panel shows in the open app at once (no reload, no deployment)")
            ap.locator(".sheet").get_by_role("button", name="الرجوع إلى الافتراضي").click()
            ap.locator(".sheet--confirm, .confirm, .sheet").last.get_by_role("button", name="رجوع").click()
            expect(page.locator(".nav__btn").first).to_contain_text("Home", timeout=10000)
            ap.locator(".sheet details summary").first.click()
            expect(ap.locator(".sheet details")).to_contain_text("Start")
            run.shot(ap, "panel-history")
            run.step("back to the shipped text the same way; the history keeps both versions")
            actx.close()
            ctx.close()

            # 5. pages without JavaScript
            ctx = browser.new_context(**PHONE, locale="ar")
            page = ctx.new_page()
            page.goto(base + "/policies/privacy?lang=en")
            assert html_lang(page) == ("en", "ltr")
            expect(page.locator("h1")).to_have_text("Privacy Policy")
            page.goto(base + "/download")
            assert html_lang(page) == ("en", "ltr"), "the language picked with ?lang= was not remembered"
            run.shot(page, "download-en")
            page.goto(base + "/policies/terms?lang=ar")
            assert html_lang(page) == ("ar", "rtl")
            ctx.close()
            run.step("policies and download follow ?lang= and remember it")
            browser.close()
    except AssertionError as exc:
        problems.append(str(exc))
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    noise = ("favicon", "Failed to load resource")
    errors = [e for e in run.errors if not any(n in e for n in noise)]
    if problems or errors:
        print("\nI18N E2E FAILED:\n  " + "\n  ".join(problems + errors))
        return 1
    print(f"\nI18N E2E PASSED. Screenshots in {run.shots}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
