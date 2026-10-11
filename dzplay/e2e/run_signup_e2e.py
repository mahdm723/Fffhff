"""V6 phase 15: e-mail confirmation at signup, in a phone browser (a local SMTP server receives the codes).

1. The signup form → «أدخل الرمز المرسل إلى بريدك» (no account yet); a wrong code is refused; «إعادة الإرسال» waits;
   the code from the e-mail creates the account and opens the app.
2. In French: the e-mail and the code screen in French (left to right).
3. The owner sets the admin signup code in the panel (step-up 2FA); typed on the code screen instead of the e-mail
   code it creates the account; the panel shows «رمز إداري» next to it.
Every screen fits a 360 / 390 px phone (the checks of run_overflow_e2e).

    PW_CHROMIUM=… python e2e/run_signup_e2e.py
"""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import run_admin_e2e as ae  # noqa: E402
import run_overflow_e2e as ro  # noqa: E402
from run_e2e import Run  # noqa: E402

from tests.smtp_sink import SmtpSink  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
ADMIN_CODE = "Owner-Signup-Code-2026!"


def phone(browser, width: int = 390, locale: str = "ar"):
    return browser.new_context(viewport={"width": width, "height": 844}, device_scale_factor=2, is_mobile=True,
                               has_touch=True, user_agent=ae.IPHONE_UA, locale=locale, color_scheme="dark")


def fill_signup(page: Page, base: str, email: str, name: str, tab: str = "حساب جديد", submit: str = "إنشاء الحساب") -> None:
    page.goto(base + "/")
    page.get_by_role("tab", name=tab).click()
    page.locator("#email").fill(email)
    page.locator("#display-name").fill(name)
    page.locator("#password").fill(ae.PASSWORD)
    page.locator("#password_confirm").fill(ae.PASSWORD)
    page.locator(".gender-pick__opt").first.click()
    page.locator("#age_confirmed").check()
    page.locator("form").get_by_role("button", name=submit).click()


def code_in(smtp, to: str) -> str:
    for m in reversed(smtp.messages):
        if to in m["to"]:
            return re.search(r"\b([0-9]{6})\b", m["msg"].get_body(preferencelist=("plain",)).get_content()).group(1)
    raise AssertionError(f"no e-mail to {to}")


def wait_mail(smtp, to: str, count: int = 1, seconds: float = 15) -> None:
    end = time.time() + seconds
    while time.time() < end:
        if sum(1 for m in smtp.messages if to in m["to"]) >= count:
            return
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for the e-mail to {to}")


def make_owner(db_url: str) -> None:
    """The owner's panel account (ae.seed signs people up through the API, which here waits for a code)."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app.config import Settings
    from app.services import admin_auth

    with Session(create_engine(db_url)) as db:
        _admin, ae.ADMIN_TOTP["owner"] = admin_auth.create_admin(db, Settings(SECRET_KEY=ae.SECRET, DATABASE_URL=db_url),
                                                                 "owner", ae.ADMIN_PASSWORD)
        db.commit()


def main() -> int:
    problems: list[str] = []
    with SmtpSink() as smtp:
        proc, base, db_url = ae.start_server(8781, REGISTRATION_EMAIL_VERIFICATION="true", SMTP_HOST="127.0.0.1",
                                             SMTP_PORT=str(smtp.port), SMTP_SECURITY="none",
                                             SMTP_FROM="DALTA.BIT <no-reply@dzplay.test>", REGISTER_RESEND_SECONDS="5")
        run = Run(base, ROOT / "e2e" / "screenshots-signup")
        try:
            make_owner(db_url)
            chromium = os.environ.get("PW_CHROMIUM", "")
            with sync_playwright() as p:
                browser = p.chromium.launch(executable_path=chromium if os.path.exists(chromium) else None)

                # 1. Arabic, with the e-mail code ----------------------------------------------------------------
                for width in ro.WIDTHS:
                    ctx = phone(browser, width)
                    pg = ctx.new_page()
                    pg.set_default_timeout(20000)
                    run.watch(pg, f"signup-{width}")
                    email = f"code{width}@example.com"
                    fill_signup(pg, base, email, f"مستخدم {width}")
                    screen = pg.locator(".signup-verify")
                    expect(screen.get_by_role("heading", name="أدخل الرمز المرسل إلى بريدك")).to_be_visible()
                    expect(screen).to_contain_text("c***@example.com")
                    expect(screen.locator(".signup-verify__resend")).to_be_disabled()
                    pg.evaluate(ro.INJECT_SAFE_TOP)
                    ro.check(pg, f"{width} signup code screen")
                    run.shot(pg, f"{width}-1-code-screen")
                    wait_mail(smtp, email)
                    code = code_in(smtp, email)
                    pg.locator("#signup-code").fill("000000" if code != "000000" else "111111")
                    screen.get_by_role("button", name="تأكيد وإنشاء الحساب").click()
                    expect(screen.locator(".form-error")).to_have_text("الرمز غير صحيح.")
                    if width == 390:  # «إعادة الإرسال» after its wait: a new e-mail, the old code stops working
                        expect(screen.locator(".signup-verify__resend")).to_be_enabled(timeout=10000)
                        screen.locator(".signup-verify__resend").click()
                        expect(pg.locator(".reset__sent")).to_contain_text("أرسلنا رمزًا جديدًا")
                        wait_mail(smtp, email, 2)
                        code = code_in(smtp, email)
                    pg.locator("#signup-code").fill(code)
                    screen.get_by_role("button", name="تأكيد وإنشاء الحساب").click()
                    expect(pg.locator("#idea-compose")).to_be_visible()
                    run.shot(pg, f"{width}-1b-signed-in")
                    ctx.close()
                run.step("the form → «أدخل الرمز المرسل إلى بريدك»; a wrong code is refused; «إعادة الإرسال» waits and "
                         "sends a new code; the right code creates the account and opens the app")

                # 2. French ------------------------------------------------------------------------------------------
                ctx = phone(browser, 390, "fr-FR")
                pg = ctx.new_page()
                pg.set_default_timeout(20000)
                run.watch(pg, "signup-fr")
                fill_signup(pg, base, "francais@example.com", "Camille", tab="Nouveau compte", submit="Créer le compte")
                screen = pg.locator(".signup-verify")
                expect(screen.get_by_role("heading", name="Saisissez le code envoyé à votre adresse e-mail")).to_be_visible()
                expect(pg.locator("html")).to_have_attribute("dir", "ltr")
                ro.check(pg, "390 signup code screen (fr)")
                run.shot(pg, "2-code-screen-fr")
                wait_mail(smtp, "francais@example.com")
                msg = [m for m in smtp.messages if "francais@example.com" in m["to"]][-1]["msg"]
                assert "code de confirmation de votre compte" in msg["Subject"], msg["Subject"]
                pg.locator("#signup-code").fill(code_in(smtp, "francais@example.com"))
                screen.get_by_role("button", name="Confirmer et créer le compte").click()
                expect(pg.locator("#idea-compose")).to_be_visible()
                ctx.close()
                run.step("French: the e-mail and the code screen in French, left to right")

                # 3. the admin signup code -----------------------------------------------------------------------
                actx = browser.new_context(**ae.MOBILE, locale="ar", color_scheme="dark")
                ap = actx.new_page()
                ap.set_default_timeout(20000)
                run.watch(ap, "panel")
                panel = ae.Run(base, run.shots)
                panel.sign_in(ap, "owner")
                expect(ap.locator(".admin-tile__num").first).to_be_visible()
                ap.locator("#tab-system").click()
                card = ap.locator(".admin-group", has=ap.get_by_role("heading", name="رمز إنشاء الحساب الإداري"))
                expect(card).to_contain_text("لم يُضبط رمز بعد")
                card.locator("#signup-admin-code").fill(ADMIN_CODE)
                card.locator("#signup-code-enabled").check()
                card.locator("#signup-code-uses").fill("3")
                from app.security import totp

                time.sleep(max(0.0, totp.STEP - (time.time() % totp.STEP)) + 1)  # a fresh 2FA step (each is used once)
                card.locator("input[autocomplete='one-time-code']").fill(
                    totp.code_at(ae.ADMIN_TOTP["owner"], totp.current_step(time.time())))
                card.get_by_role("button", name="حفظ").click()
                expect(card).to_contain_text("مفعّل")
                expect(card).to_contain_text("0 من 3")
                card.screenshot(path=str(run.shots / "3-panel-signup-code.png"))
                ro.check(ap, "390 panel signup code", admin=True)

                ctx = phone(browser, 390)
                pg = ctx.new_page()
                pg.set_default_timeout(20000)
                run.watch(pg, "signup-admin-code")
                fill_signup(pg, base, "noinbox@example.com", "ضيف المالك")
                pg.locator("#signup-code").fill(ADMIN_CODE)
                pg.locator(".signup-verify").get_by_role("button", name="تأكيد وإنشاء الحساب").click()
                expect(pg.locator("#idea-compose")).to_be_visible()
                ctx.close()

                ap.locator("#tab-users").click()
                row = ap.locator(".admin-card", has_text="noinbox@example.com")
                expect(row).to_contain_text("رمز إداري")
                expect(ap.locator(".admin-card", has_text="code390@example.com")).to_contain_text("تأكيد بالبريد")
                ro.check(ap, "390 panel users (verified chips)", admin=True)
                ap.locator("#tab-system").click()
                expect(card).to_contain_text("1 من 3")
                run.shot(ap, "3b-panel-after-use")
                actx.close()
                run.step("the owner's admin code (set in the panel with 2FA) creates an account without the e-mail; "
                         "the panel shows «رمز إداري» and the use count")
                browser.close()
            if ro.PROBLEMS:
                problems.extend(ro.PROBLEMS)
        except AssertionError as exc:
            problems.append(str(exc))
        finally:
            proc.terminate()
            proc.wait(timeout=10)
    noise = ("favicon", "Failed to load resource")
    errors = [e for e in run.errors if not any(n in e for n in noise)]
    if problems or errors:
        print("\nSIGNUP E2E FAILED:\n  " + "\n  ".join(problems + errors))
        return 1
    print(f"\nSIGNUP E2E PASSED. Screenshots in {run.shots}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
