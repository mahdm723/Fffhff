"""V5 phase C in a real browser (phone viewport): support ticket ↔ panel reply, blue-star request with the
crypto payment box, approval from the panel, star shown on the profile and on ideas.

    python e2e/run_account_e2e.py [--shots DIR]
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

import httpx
from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_e2e import MOBILE, PASSWORD, Run, register, start_server  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
ADMIN_PATH = "/panel-account-e2e"
ADMIN_PASSWORD = "Admin-Pass-0123456"
TX = "c" * 64


def admin_client(base: str, db_url: str) -> httpx.Client:
    sys.path.insert(0, str(ROOT))
    from app.config import Settings
    from app.db import Database
    from app.security import totp
    from app.services import admin_auth

    settings = Settings(DATABASE_URL=db_url, SECRET_KEY="e2e-secret", ADMIN_PATH=ADMIN_PATH)
    database = Database(db_url)
    with database.session() as db:
        _admin, secret = admin_auth.create_admin(db, settings, "owner", ADMIN_PASSWORD)
    c = httpx.Client(base_url=base + ADMIN_PATH, headers={"X-DZ-Requested": "1"}, timeout=20)
    r = c.post("/api/admin/login", json={"username": "owner", "password": ADMIN_PASSWORD,
                                         "code": totp.code_at(secret, totp.current_step(time.time()))})
    assert r.status_code == 200, r.text
    c.totp_secret = secret  # step-up codes (V6 giveaway draw, group rewards) in other e2e scripts
    return c


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8772)
    parser.add_argument("--shots", default=str(ROOT / "e2e" / "screenshots-account"))
    args = parser.parse_args()
    tmp = tempfile.mkdtemp(prefix="dz-account-e2e-")
    db_url = f"sqlite:///{tmp}/account.db"
    proc, base = start_server(args.port, {"DATABASE_URL": db_url, "ADMIN_PATH": ADMIN_PATH, "VERIFY_MIN_POSTS": "1",
                                          "VERIFY_MIN_LIKES": "0", "VERIFY_MIN_ACCOUNT_AGE_DAYS": "0",
                                          "VERIFY_ENABLED": "true"})  # V6: closed by default; the flow is still tested
    run = Run(base, Path(args.shots))
    chromium = os.environ.get("PW_CHROMIUM", "/opt/pw-browsers/chromium-1194/chrome-linux/chrome")
    try:
        adm = admin_client(base, db_url)
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=chromium if os.path.exists(chromium) else None)
            a = browser.new_context(**MOBILE, locale="ar", color_scheme="dark").new_page()
            run.watch(a, "A")
            register(run, a, f"account{int(time.time())}@example.com")

            print("Support")
            a.goto(base + "/#/profile")
            a.get_by_role("button", name="الدعم والمساعدة").click()
            a.get_by_role("radio", name="الحساب").click()
            a.locator("#ticket-subject").fill("لا أستطيع تغيير اسمي")
            a.locator("#ticket-body").fill("أحاول تغيير اسمي فتظهر رسالة انتظار.")
            run.shot(a, "c01-support-form")
            a.get_by_role("button", name="إرسال إلى الدعم").click()
            expect(a.get_by_text("أُرسلت تذكرتك رقم #1001")).to_be_visible(timeout=10000)
            expect(a.locator(".ticket-item", has_text="لا أستطيع تغيير اسمي")).to_be_visible()
            run.step("ticket #1001 sent")
            tid = adm.get("/api/admin/support").json()["tickets"][0]["id"]
            assert adm.post(f"/api/admin/support/{tid}/reply", json={"body": "يمكن تغيير الاسم كل 14 يومًا."}).status_code == 200
            expect(a.get_by_text("رد جديد").first).to_be_visible(timeout=10000)  # live, through the WebSocket
            a.locator(".ticket-item").first.click()
            expect(a.get_by_text("يمكن تغيير الاسم كل 14 يومًا.")).to_be_visible()
            run.shot(a, "c02-ticket-thread")
            run.step("panel reply appears live in «تذاكري»")

            print("Membership (V6 phase 5: the star comes with it)")
            a.goto(base + "/#/membership")
            expect(a.get_by_text("لا علاقة لها بالتداول ولا بأي عائد مالي")).to_be_visible(timeout=10000)
            expect(a.get_by_text("الدفع غير متاح حاليًا")).to_be_visible(timeout=10000)
            for word in ("أرباح", "ربح ", "عائد يومي"):
                assert word not in a.locator(".membership").inner_text()
            run.shot(a, "c03-membership-off")
            a.goto(base + "/#/home")
            expect(a.locator("#idea-compose")).to_be_visible(timeout=10000)
            a.locator("#idea-compose").fill("أول فكرة لي هنا")
            a.get_by_role("button", name="نشر", exact=True).click()
            expect(a.get_by_text("نُشرت فكرتك")).to_be_visible(timeout=10000)
            r = adm.put("/api/admin/payment-settings", json={"currency": "USDT", "network": "TRC20",
                                                             "wallet": "TQ5pZ9aBcDeFgHiJkLmNoPqRsTuVwXyZ12"})
            assert r.status_code == 200, r.text
            a.goto(base + "/#/membership")
            expect(a.locator(".pay__qr")).to_be_visible(timeout=10000)
            a.wait_for_function("() => document.querySelector('.pay__qr').naturalWidth > 0")
            expect(a.get_by_text("TRON (TRC20)").first).to_be_visible()
            expect(a.get_by_text("50.00 USDT").first).to_be_visible()
            a.locator("#mem-txid").fill("0x" + "d" * 64)
            run.shot(a, "c04-membership-pay")
            a.get_by_role("button", name="إرسال رقم العملية").click()
            expect(a.get_by_text("رقم العملية (TXID) لا يطابق")).to_be_visible(timeout=10000)
            a.locator("#mem-txid").fill(TX)
            a.get_by_role("button", name="إرسال رقم العملية").click()
            expect(a.get_by_text("طلبك: قيد المراجعة")).to_be_visible(timeout=10000)
            run.step("membership: wrong-network TXID refused; request pending")
            req = adm.get("/api/admin/membership?status=pending").json()["requests"][0]
            assert req["explorer_url"].endswith(TX)
            assert adm.post(f"/api/admin/membership/requests/{req['id']}/decide", json={"action": "accept"}).status_code == 200
            expect(a.get_by_text("عضويتك مفعّلة")).to_be_visible(timeout=10000)  # live, no reload
            expect(a.get_by_role("button", name="طلب استرجاع العضوية")).to_be_visible()
            run.shot(a, "c05-member")
            a.goto(base + "/#/profile")
            expect(a.locator(".id-card .star-mark")).to_be_visible(timeout=10000)
            a.goto(base + "/#/home")
            expect(a.locator("#idea-compose")).to_be_visible(timeout=10000)
            a.get_by_role("button", name="أفكار أخرى").click()
            expect(a.locator(".post-card", has_text="أول فكرة لي هنا").locator(".star-mark")).to_be_visible(timeout=10000)
            run.shot(a, "c06-star-on-idea")
            run.step("accepted from the panel → membership + star on the profile and on ideas; refund offered")

            print("Delete my account")
            a.goto(base + "/#/profile")
            a.get_by_role("button", name="حذف حسابي").click()
            expect(a.get_by_text("سيُحذف نهائيًا")).to_be_visible()
            a.locator("#delete-password").fill("wrong-password-1")
            a.get_by_role("button", name="حذف حسابي نهائيًا").click()
            expect(a.get_by_text("كلمة المرور غير صحيحة.")).to_be_visible(timeout=10000)
            run.shot(a, "c07-delete-account")
            a.locator("#delete-password").fill(PASSWORD)
            a.get_by_role("button", name="حذف حسابي نهائيًا").click()
            expect(a.get_by_role("tab", name="حساب جديد")).to_be_visible(timeout=15000)
            run.step("«حذف حسابي»: wrong password refused, then the account is gone and the app is signed out")
            browser.close()
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    noise = ("favicon", "Failed to load resource")
    errors = [e for e in run.errors if not any(n in e for n in noise)]
    if errors:
        print("\nBrowser errors:\n  " + "\n  ".join(errors))
        return 1
    print(f"\nACCOUNT E2E PASSED. Screenshots in {run.shots}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
