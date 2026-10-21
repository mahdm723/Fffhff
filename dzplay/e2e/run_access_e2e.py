"""V6 phase 14: who may open the app, in real phone browsers (the server as deployed: computers kept out).

1. An Android browser sees only «حمّل التطبيق» with the APK button (in its language); the API answers 403
   app_required; a service worker left from the web-app days is removed. /download, the policies and the panel open.
2. An iPhone uses the app in Safari. An iPad (Safari says «Macintosh»): js/gate.js sees the touch screen, sets
   dz_touch and reloads into the app. A Mac without touch gets the «for phones» page with the QR code.
3. The 3.1.0 app (no attestation): «حدّث التطبيق» with the APK link. A 3.2.0 app that could not prove itself:
   «تعذّر التحقق» with «إعادة المحاولة» (asks the app to try again). The app with its access cookie works; when the
   cookie stops being valid mid-session, the next API call asks the app to prove itself again.
4. From the panel: WEB_DESKTOP_ACCESS lets computers in; ANDROID_APP_GATE off lets Android browsers in.
Every access page fits a 360 / 390 px phone (the checks of run_overflow_e2e).

    PW_CHROMIUM=… python e2e/run_access_e2e.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import run_admin_e2e as ae  # noqa: E402
import run_overflow_e2e as ro  # noqa: E402
from run_e2e import Run  # noqa: E402

from app.services import access, app_release  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
ANDROID = ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 "
           "Mobile Safari/537.36")
WEBVIEW = ("Mozilla/5.0 (Linux; Android 14; Pixel 8 Build/UQ1A; wv) AppleWebKit/537.36 (KHTML, like Gecko) "
           "Version/4.0 Chrome/130.0.0.0 Mobile Safari/537.36 DZPLAYApp/{v}")
IPHONE = ae.IPHONE_UA
MAC = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 "
       "Safari/605.1.15")
WINDOWS = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
# the app's bridge, as 3.2.0 exposes it (window.DZPLAYAndroid); refreshAccess is recorded instead of re-attesting
BRIDGE = "window.__refresh = 0; window.DZPLAYAndroid = { refreshAccess() { window.__refresh += 1; }, versionCode() { return %d; } };"
IPAD_TOUCH = "Object.defineProperty(Navigator.prototype, 'maxTouchPoints', { get: () => 5 });"


def phone(browser, ua: str, width: int = 390, locale: str = "ar", **kw):
    return browser.new_context(viewport={"width": width, "height": 844}, device_scale_factor=2, is_mobile=True,
                               has_touch=True, user_agent=ua, locale=locale, color_scheme="dark", **kw)


def app_token(version_code: int) -> str:
    token, _ = access.issue_token(SimpleNamespace(SECRET_KEY=ae.SECRET, APP_TOKEN_HOURS=12), "e2edevice", version_code)
    return token


def gate(page: Page, kind: str) -> None:
    expect(page.locator(f'.dl-gate[data-gate="{kind}"]')).to_be_visible()
    expect(page.locator('script[src="/js/app.js"]')).to_have_count(0)


def main() -> int:
    proc, base, db_url = ae.start_server(8779, WEB_DESKTOP_ACCESS="false")  # as deployed: computers kept out
    run = Run(base, ROOT / "e2e" / "screenshots-access")
    problems: list[str] = []
    current = int((app_release.info() or {}).get("min_version_code") or 0)
    try:
        ae.seed(base, db_url)  # admin accounts (the seeding client is an iPhone)
        chromium = os.environ.get("PW_CHROMIUM", "")
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=chromium if os.path.exists(chromium) else None)

            # 1. an Android browser ------------------------------------------------------------------------------
            for width in ro.WIDTHS:
                ctx = phone(browser, ANDROID, width)
                pg = ctx.new_page()
                run.watch(pg, f"android-{width}")
                pg.goto(base + "/")
                gate(pg, "android")
                expect(pg.locator(".dl-gate h2")).to_have_text("حمّل التطبيق")
                expect(pg.locator("a.dl-btn")).to_have_attribute("href", "/download/dzplay.apk")
                expect(pg.locator(".dl-qr")).to_have_count(0)
                ro.check(pg, f"{width} gate android")
                run.shot(pg, f"{width}-1-android-gate")
                ctx.close()
            ctx = phone(browser, ANDROID)
            pg = ctx.new_page()
            run.watch(pg, "android")
            pg.goto(base + "/")
            r = pg.request.get(base + "/api/config", headers={"X-DZ-Requested": "1"})
            assert r.status == 403 and r.json()["error"]["code"] == "app_required", r.text()
            # a service worker from the web-app days (installed before the gate) is removed by the access page
            pg.evaluate("navigator.serviceWorker.register('/sw.js').then(() => navigator.serviceWorker.ready)")
            pg.reload()
            pg.wait_for_function("navigator.serviceWorker.getRegistrations().then((r) => r.length === 0)")
            pg.wait_for_function("caches.keys().then((k) => k.length === 0)")
            for path, ready in (("/download", ".dl-card h2"), ("/policies/privacy", ".policy-body"),
                                (ae.ADMIN_PATH, "#admin-user")):
                pg.goto(base + path)
                expect(pg.locator(ready).first).to_be_visible()
            expect(pg.locator(".dl-gate")).to_have_count(0)
            ctx.close()
            ctx = phone(browser, ANDROID, locale="fr-FR")
            pg = ctx.new_page()
            run.watch(pg, "android-fr")
            pg.goto(base + "/")
            gate(pg, "android")
            expect(pg.locator("html")).to_have_attribute("dir", "ltr")
            expect(pg.locator(".dl-gate h2")).to_have_text("Téléchargez l’application")
            ro.check(pg, "390 gate android (fr)")
            run.shot(pg, "1b-android-gate-fr")
            ctx.close()
            run.step("Android browser: only «حمّل التطبيق» + the APK (fr: LTR), API 403 app_required, old SW removed; "
                     "/download, policies and the panel open")

            # 2. iPhone, iPad, Mac ---------------------------------------------------------------------------------
            ctx = phone(browser, IPHONE)
            pg = ctx.new_page()
            run.watch(pg, "iphone")
            pg.goto(base + "/")
            expect(pg.locator(".auth")).to_be_visible(timeout=15000)
            run.shot(pg, "2-iphone-app")
            ctx.close()
            ctx = phone(browser, MAC, width=820)
            ctx.add_init_script(IPAD_TOUCH)
            pg = ctx.new_page()
            run.watch(pg, "ipad")
            pg.goto(base + "/")  # the «computer» page first, then gate.js sees the touch screen and reloads
            expect(pg.locator(".auth")).to_be_visible(timeout=15000)
            assert any(c["name"] == access.TOUCH_COOKIE for c in ctx.cookies())
            run.shot(pg, "2b-ipad-app")
            ctx.close()
            for width in ro.WIDTHS:
                ctx = browser.new_context(viewport={"width": width, "height": 800}, user_agent=MAC, locale="ar",
                                          color_scheme="light")
                pg = ctx.new_page()
                run.watch(pg, f"mac-{width}")
                pg.goto(base + "/")
                gate(pg, "desktop")
                expect(pg.locator(".dl-qr svg")).to_be_visible()
                pg.wait_for_timeout(500)
                gate(pg, "desktop")  # no touch screen: it stays (no reload loop)
                ro.check(pg, f"{width} gate desktop")
                run.shot(pg, f"{width}-2c-desktop-gate")
                ctx.close()
            run.step("iPhone: the app in Safari; iPad (says «Macintosh»): detected by touch → the app; "
                     "a Mac: «للهواتف» with the QR code")

            # 3. the Android app -----------------------------------------------------------------------------------
            for width in ro.WIDTHS:
                ctx = phone(browser, WEBVIEW.format(v="3.1.0"), width)
                pg = ctx.new_page()
                run.watch(pg, f"old-app-{width}")
                pg.goto(base + "/")
                gate(pg, "update")
                expect(pg.locator(".dl-gate h2")).to_have_text("حدّث التطبيق")
                expect(pg.locator("a.dl-btn")).to_have_attribute("href", "/download/dzplay.apk")
                ro.check(pg, f"{width} gate update")
                run.shot(pg, f"{width}-3-update-gate")
                ctx.close()
                ctx = phone(browser, WEBVIEW.format(v="3.2.0"), width)
                ctx.add_init_script(BRIDGE % current)
                pg = ctx.new_page()
                run.watch(pg, f"unverified-{width}")
                pg.goto(base + "/")
                gate(pg, "unverified")
                retry = pg.locator("#gate-retry")
                expect(retry).to_be_visible()
                ro.check(pg, f"{width} gate unverified")
                run.shot(pg, f"{width}-3b-unverified-gate")
                retry.click()
                pg.wait_for_function("window.__refresh === 1")
                ctx.close()
            ctx = phone(browser, WEBVIEW.format(v="3.2.0"))
            ctx.add_init_script(BRIDGE % current)
            ctx.add_cookies([{"name": access.APP_COOKIE, "value": app_token(current), "url": base}])
            pg = ctx.new_page()
            run.watch(pg, "app")
            pg.goto(base + "/")
            expect(pg.locator(".auth")).to_be_visible(timeout=15000)
            expect(pg.locator(".sheet", has_text="تحديث")).to_have_count(0)
            run.shot(pg, "3c-app")
            # the access cookie is replaced by one from an older version (a newer APK was published meanwhile)
            ctx.add_cookies([{"name": access.APP_COOKIE, "value": app_token(current - 1), "url": base}])
            pg.get_by_role("tab", name="حساب جديد").click()
            pg.locator(".antibot").click()  # its API call gets 403 app_required → the app is asked to prove itself
            pg.wait_for_function("window.__refresh >= 1")
            ctx.close()
            run.step("3.1.0: «حدّث التطبيق» + APK; 3.2.0 unproven: «تعذّر التحقق» + «إعادة المحاولة»; "
                     "with its cookie: the app; a stale cookie mid-session → the app proves itself again")

            # 4. the panel settings --------------------------------------------------------------------------------
            actx = browser.new_context(viewport={"width": 1280, "height": 900}, user_agent=WINDOWS, locale="ar")
            ap = actx.new_page()
            run.watch(ap, "panel")
            ae.Run(base, run.shots).sign_in(ap, "desk")
            expect(ap.locator(".admin-tile__num").first).to_be_visible()

            def setting(key: str, value: bool) -> None:
                r = ap.request.put(base + ae.ADMIN_PATH + "/api/admin/settings", data={"changes": {key: value}},
                                   headers={"X-DZ-Requested": "1"})
                assert r.status == 200, r.text()

            setting("WEB_DESKTOP_ACCESS", True)
            ap.goto(base + "/")
            expect(ap.locator(".auth")).to_be_visible(timeout=15000)
            setting("ANDROID_APP_GATE", False)
            ctx = phone(browser, ANDROID)
            pg = ctx.new_page()
            run.watch(pg, "android-valve")
            pg.goto(base + "/")
            expect(pg.locator(".auth")).to_be_visible(timeout=15000)
            ctx.close()
            setting("ANDROID_APP_GATE", True)
            setting("WEB_DESKTOP_ACCESS", False)
            ap.goto(base + "/")
            gate(ap, "desktop")
            actx.close()
            run.step("panel: «السماح من الكمبيوتر» lets computers in; the safety valve lets Android browsers in; "
                     "both back off")
            browser.close()
        if ro.PROBLEMS:
            problems.extend(ro.PROBLEMS)
    except AssertionError as exc:
        problems.append(str(exc))
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    # expected: the deliberate 403 (app_required) and the API calls of a page replaced by the access page
    noise = ("favicon", "Failed to load resource")
    errors = [e for e in run.errors if not any(n in e for n in noise)]
    if problems or errors:
        print("\nACCESS E2E FAILED:\n  " + "\n  ".join(problems + errors))
        return 1
    print(f"\nACCESS E2E PASSED. Screenshots in {run.shots}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
