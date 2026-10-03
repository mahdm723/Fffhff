"""End-to-end browser test of the admin panel (secret ADMIN_PATH, account + 2FA) on a phone viewport.

    python e2e/run_admin_e2e.py [--shots DIR]

Starts its own server with a temporary SQLite DB, seeds real activity through
the public API (plus some back-dated rows so the 14-day chart has a shape),
then drives the dashboard in dark and light mode.
"""

from __future__ import annotations

import argparse
import os
import random
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import timedelta
from pathlib import Path

import httpx
from playwright.sync_api import Page, expect, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import clock  # noqa: E402
from app.models import AuthThrottle, Conversation, Post, SecurityEvent, User  # noqa: E402
from app.security.pow import solve  # noqa: E402

ADMIN_PATH = "/panel-e2e-test"
ADMIN_PASSWORD = "e2e-Admin-Pass-0123"
SECRET = "e2e-secret"
ADMIN_TOTP: dict[str, str] = {}
PASSWORD = "Str0ng-Pass!"
SEEDED: dict[str, str] = {}
MOBILE = {"viewport": {"width": 390, "height": 844}, "device_scale_factor": 2, "is_mobile": True, "has_touch": True}


def start_server(port: int) -> tuple[subprocess.Popen, str, str]:
    tmp = tempfile.mkdtemp(prefix="dz-admin-e2e-")
    db_url = f"sqlite:///{tmp}/e2e.db"
    env = dict(os.environ, DATABASE_URL=db_url, SECRET_KEY=SECRET, ENV="development", CLEANUP_INTERVAL="0",
               LOG_LEVEL="WARNING", REDIS_URL="", GOOGLE_CLIENT_ID="", MAX_ACCOUNTS_PER_IP="10", ADMIN_PATH=ADMIN_PATH)
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port),
                             "--no-access-log", "--timeout-graceful-shutdown", "2"], cwd=ROOT, env=env)
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(base + "/healthz", timeout=1)
            return proc, base, db_url
        except Exception:  # noqa: BLE001
            time.sleep(0.1)
    proc.kill()
    raise RuntimeError("server did not start")


def api_client(base: str) -> httpx.Client:
    return httpx.Client(base_url=base, headers={"X-DZ-Requested": "1"}, timeout=20)


def challenge(c: httpx.Client, purpose: str) -> dict:
    ch = c.post("/api/auth/challenge", json={"purpose": purpose}).json()
    ch["number"] = solve(ch)
    return ch


def register(base: str, email: str) -> httpx.Client:
    c = api_client(base)
    r = c.post("/api/auth/register", json={"email": email, "password": PASSWORD, "password_confirm": PASSWORD,
                                           "antibot": challenge(c, "register")})
    assert r.status_code == 201, r.text
    return c


def seed(base: str, db_url: str) -> str:
    a = register(base, "owner-a@example.com")
    b = register(base, "friend-b@example.com")
    pid = a.post("/api/posts", json={"content": "فكرة مخالفة للتجربة: هذا المنشور سيتم الإبلاغ عنه."}).json()["id"]
    assert b.post(f"/api/posts/{pid}/report", json={"reason": "inappropriate", "details": "محتوى مزعج"}).status_code == 201
    conv = b.post("/api/messages", json={"content": "مرحبا، هذه رسالة مجهولة مزعجة للتجربة."}).json()["conversation"]["id"]
    assert a.post(f"/api/conversations/{conv}/report", json={"reason": "spam"}).status_code == 201
    # A threatening reply is flagged automatically (and still delivered).
    assert a.post(f"/api/conversations/{conv}/messages", json={"content": "راني نعرف وين تسكن، ابعث الدراهم ولا نفضحك"}).status_code == 201
    SEEDED["idea"] = b.post("/api/posts", json={"content": "فكرة جميلة ستحصل على تعزيز وتعليقات من الفريق."}).json()["id"]
    register(base, "old-user@example.com")  # gets the privacy notice (see below)
    bad = api_client(base)
    for _ in range(2):
        bad.post("/api/auth/login", json={"email": "owner-a@example.com", "password": "wrong-pass-1", "antibot": challenge(bad, "login")})

    # Back-dated rows so the chart shows two weeks of activity.
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    rnd = random.Random(7)
    now = clock.utcnow()
    with Session(create_engine(db_url)) as db:
        users = []
        for day in range(1, 14):
            for k in range(rnd.randint(1, 6) + (13 - day) // 3):
                t = now - timedelta(days=day, hours=rnd.randint(0, 20))
                u = User(email=f"seed-{day}-{k}@example.com", password_hash="x", created_at=t, last_active_at=t)
                db.add(u)
                users.append(u)
        db.flush()
        for day in range(1, 14):
            for _ in range(rnd.randint(0, 5)):
                t = now - timedelta(days=day, hours=rnd.randint(0, 20))
                db.add(Post(author_id=rnd.choice(users).id, content="seed", created_at=t, updated_at=t))
            for _ in range(rnd.randint(0, 4)):
                t = now - timedelta(days=day, hours=rnd.randint(0, 20))
                x, y = rnd.sample(users, 2)
                db.add(Conversation(initiator_id=x.id, recipient_id=y.id, created_at=t, last_message_at=t, updated_at=t,
                                    expires_at=now + timedelta(days=3)))
            for _ in range(rnd.choice([0, 0, 1, 2, 9])):
                db.add(SecurityEvent(type="login_failed", ip_hash="ab" * 32, created_at=now - timedelta(days=day, hours=3)))
        old = db.query(User).filter(User.email == "old-user@example.com").one()
        old.privacy_ack_version = None  # an account from before the privacy update
        db.add(AuthThrottle(key="ip:" + "cd" * 32, failures=9, first_failure_at=now, last_failure_at=now,
                            blocked_until=now + timedelta(minutes=30)))  # an active network block to lift
        db.commit()

        # One admin account per browser context (a TOTP code can only be used once).
        from app.config import Settings
        from app.services import admin_auth

        settings = Settings(SECRET_KEY=SECRET, DATABASE_URL=db_url)
        for name in ("owner", "light", "desk"):
            _admin, ADMIN_TOTP[name] = admin_auth.create_admin(db, settings, name, ADMIN_PASSWORD)
        db.commit()
    return pid


class Run:
    def __init__(self, base: str, shots: Path):
        self.base = base
        self.shots = shots
        self.errors: list[str] = []
        shots.mkdir(parents=True, exist_ok=True)

    def watch(self, page: Page, who: str) -> None:
        # Expected: 401 (deliberate wrong sign-in, first visit) and 403 (deliberate wrong 2FA code on the bot form).
        page.on("console", lambda m: m.type == "error" and "status of 401" not in m.text and "status of 403" not in m.text
                and self.errors.append(f"{who} console: {m.text}"))
        page.on("pageerror", lambda e: self.errors.append(f"{who} pageerror: {e}"))

    def shot(self, page: Page, name: str, full: bool = False) -> None:
        page.wait_for_timeout(400)
        page.screenshot(path=str(self.shots / f"{name}.png"), full_page=full)

    @staticmethod
    def step(text: str) -> None:
        print(f"  ✓ {text}", flush=True)

    def sign_in(self, page: Page, username: str, code: str | None = None, goto: bool = True) -> None:
        from app.security import totp

        if goto:
            page.goto(self.base + ADMIN_PATH)
        page.fill("#admin-user", username)
        page.fill("#admin-pass", ADMIN_PASSWORD)
        page.fill("#admin-code", code or totp.code_at(ADMIN_TOTP[username], totp.current_step(time.time())))
        page.get_by_role("button", name="دخول").click()

    def dark(self, browser) -> None:
        ctx = browser.new_context(**MOBILE, color_scheme="dark", locale="ar-DZ", timezone_id="Africa/Algiers")
        page = ctx.new_page()
        try:
            self._dark(page)
        except Exception:
            page.screenshot(path=str(self.shots / "FAILED.png"), full_page=True)  # what the browser showed
            raise
        ctx.close()

    def _dark(self, page: Page) -> None:
        self.watch(page, "dark")
        assert urllib.request.urlopen(self.base + ADMIN_PATH).status == 200
        for guess in ("/admin", "/js/admin.js"):
            try:
                urllib.request.urlopen(self.base + guess)
                raise AssertionError(f"{guess} should not exist")
            except urllib.error.HTTPError as exc:
                assert exc.code == 404
        page.goto(self.base + ADMIN_PATH)
        expect(page.locator("#admin-user")).to_be_visible()
        self.shot(page, "01-login-dark")

        self.sign_in(page, "owner", code="000000", goto=False)
        expect(page.get_by_role("alert")).to_have_text("بيانات الدخول أو رمز التحقق غير صحيحة.")
        self.step("wrong 2FA code rejected; /admin does not exist")

        self.sign_in(page, "owner", goto=False)
        expect(page.locator(".admin-tile__num").first).to_be_visible()
        expect(page.locator(".viz-card")).to_have_count(4)
        assert "dz_admin" not in page.evaluate("document.cookie")  # HttpOnly
        expect(page.locator("#reports-badge")).to_have_text("3")
        self.step("signed in; stats, 4 activity charts, review badge = 2 reports + 1 flag")
        self.shot(page, "02-overview-dark")
        self.shot(page, "03-overview-dark-full", full=True)

        plot = page.locator(".viz-plot").first
        plot.scroll_into_view_if_needed()
        box = plot.bounding_box()
        page.mouse.move(box["x"] + box["width"] * 0.55, box["y"] + box["height"] / 2)
        expect(page.locator(".viz-readout__day")).to_be_visible()
        expect(page.locator(".viz-cross[visibility=visible]")).to_have_count(4)
        self.step("chart hover shows a day readout and crosshair in all 4 charts")
        page.locator(".viz-readout").scroll_into_view_if_needed()
        self.shot(page, "04-chart-hover-dark")
        page.get_by_role("button", name="عرض كجدول").click()
        expect(page.locator(".viz-table tbody tr")).to_have_count(14)
        page.get_by_role("tab", name="30 يومًا").click()
        expect(page.locator(".viz-table tbody tr")).to_have_count(30)
        self.step("table view + 30-day range")

        page.get_by_role("tab", name="البلاغات").click()
        expect(page.locator(".admin-card")).to_have_count(2)
        assert "null" not in page.locator("#admin-main").inner_text()
        self.shot(page, "05-reports-dark", full=True)
        post_card = page.locator(".admin-card", has_text="فكرة منشورة")
        post_card.get_by_role("button", name="حذف المحتوى").click()
        page.locator(".sheet").get_by_role("button", name="حذف المحتوى").click()
        expect(page.locator(".admin-card")).to_have_count(1)
        expect(page.locator("#reports-badge")).to_have_text("2")
        self.step("post report resolved with 'remove'")
        page.get_by_role("tab", name="تم حلها").click()
        expect(page.locator(".admin-resolution")).to_have_text("حُذف المحتوى")

        # Automatic flags: the threatening message, the sender's conversations, remove it.
        page.get_by_role("tab", name="مفتوحة").click()
        page.get_by_role("tab", name="رصد تلقائي").click()
        expect(page.locator(".admin-card")).to_have_count(1)
        expect(page.locator(".admin-card .chip").first).to_have_text("تهديد")
        expect(page.locator(".admin-terms mark").first).to_be_visible()
        self.shot(page, "05b-flags-dark", full=True)
        page.locator(".admin-card").get_by_role("button", name="صفحة المستخدم والمحادثات").click()
        user_sheet = page.locator(".admin-detail").last
        expect(user_sheet.locator(".admin-email").first).to_have_text("owner-a@example.com")
        user_sheet.locator("summary", has_text="المحادثات").click()
        user_sheet.locator(".admin-line--btn").first.click()
        conv_sheet = page.locator(".admin-detail").last
        expect(conv_sheet.locator(".admin-msg")).to_have_count(2)
        expect(conv_sheet.locator(".admin-msg--flagged")).to_have_count(1)
        self.step("flag listed; sender's page → stored conversation with both participants, flagged message marked")
        self.shot(page, "05c-conversations-dark")
        conv_sheet.get_by_role("button", name="إغلاق").click()
        page.locator(".admin-detail").last.get_by_role("button", name="إغلاق").click()
        expect(page.locator(".admin-detail")).to_have_count(0)
        page.locator(".admin-card").get_by_role("button", name="حذف المحتوى").click()
        page.locator(".sheet").get_by_role("button", name="حذف المحتوى").click()
        expect(page.locator(".admin-card")).to_have_count(0)
        expect(page.locator("#reports-badge")).to_have_text("1")
        self.step("flagged message removed")

        page.get_by_role("tab", name="السجل", exact=True).click()
        expect(page.locator(".admin-event").first).to_be_visible()
        page.select_option("#event-type", "login_failed")
        expect(page.locator(".admin-event__type").first).to_have_text("دخول فاشل")
        page.select_option("#event-type", "")
        expect(page.locator(".admin-event", has_text="دخول مشرف فاشل")).to_have_count(1)
        self.shot(page, "06-security-dark")
        page.get_by_role("tab", name="سجل الإدارة").click()
        expect(page.locator(".admin-chain")).to_contain_text("السجل سليم")
        expect(page.locator(".admin-event", has_text="اطّلع على محادثة")).to_have_count(1)
        expect(page.locator(".admin-event", has_text="فتح صفحة مستخدم")).to_have_count(1)
        self.step("security log shows the failed admin login; audit log shows the user page + conversation views")
        self.shot(page, "06b-audit-dark")

        self.users_and_content(page)
        self.engagement(page)
        self.system(page)

        page.get_by_role("button", name="خروج").click()
        expect(page.locator("#admin-user")).to_be_visible()
        page.reload()
        expect(page.locator("#admin-user")).to_be_visible()
        self.step("logout ends the admin session")

    def users_and_content(self, page: Page) -> None:
        page.get_by_role("tab", name="المستخدمون").click()
        expect(page.locator(".admin-card--tap").first).to_be_visible()
        page.get_by_placeholder("بحث بالبريد أو المعرّف أو مرجع الملف").fill("friend-b")
        page.locator(".admin-filters").get_by_role("button", name="بحث").click()
        expect(page.locator(".admin-card--tap")).to_have_count(1)
        expect(page.locator(".admin-card--tap .admin-email")).to_have_text("friend-b@example.com")
        self.shot(page, "07-users-dark")
        page.locator(".admin-card--tap").click()
        sheet = page.locator(".admin-detail")
        expect(sheet.locator(".admin-email").first).to_have_text("friend-b@example.com")
        sheet.locator("summary", has_text="المنشورات").click()
        expect(sheet.locator(".admin-text", has_text="فكرة جميلة")).to_be_visible()
        assert "password" not in sheet.inner_text().lower() and "null" not in sheet.inner_text()
        self.step("users: search by e-mail, full user page (posts, conversations, reports, events)")
        self.shot(page, "08-user-detail-dark")
        sheet.get_by_role("button", name="إنهاء كل الجلسات").click()
        expect(page.locator(".toast").last).to_contain_text("أُنهيت")
        sheet.get_by_role("button", name="إغلاق").click()

        page.get_by_role("tab", name="المحتوى").click()
        card = page.locator(".admin-card", has_text="فكرة جميلة")
        expect(card).to_be_visible()
        card.locator(".admin-select-box").check()
        expect(page.locator(".admin-selbar")).to_be_visible()
        expect(page.locator(".admin-selbar__n")).to_contain_text("1")
        self.shot(page, "09-content-dark")
        page.get_by_role("tab", name="المحادثات").click()
        expect(page.locator(".admin-card--tap").first).to_be_visible()
        page.get_by_role("tab", name="بحث").click()
        page.get_by_placeholder("ابحث في الأفكار والتعليقات والرسائل والأوصاف").fill("مزعجة")
        page.locator("#admin-main form").get_by_role("button", name="بحث").click()
        expect(page.locator(".admin-group__title", has_text="رسائل")).to_be_visible()
        self.step("content: ideas with selection, conversations, full-text search")
        page.get_by_role("tab", name="الأفكار").click()
        expect(page.locator(".admin-selbar")).to_be_visible()  # selection survives tab switches
        page.locator(".admin-selbar").get_by_role("button", name="تعزيز التفاعل").click()

    def engagement(self, page: Page) -> None:
        pid = SEEDED["idea"]
        expect(page.locator(".admin-ids")).to_have_value(pid)
        page.get_by_label("👍 إعجاب").fill("120")
        page.get_by_label("👎 عدم إعجاب").fill("4")
        page.locator(".admin-form").get_by_role("button", name="تطبيق").click()
        expect(page.locator(".viz-table tbody td").first).to_have_text("120 = 0 + 120")
        self.step("boost from the content selection: +120 likes / +4 dislikes applied")
        self.shot(page, "10-boost-dark", full=True)
        # set displayed = 300, gradually over 2 hours → a running job, then cancel it
        page.get_by_role("tab", name="تحديد الرقم الظاهر").click()
        page.get_by_label("👍 إعجاب").fill("300")
        page.get_by_label("👎 عدم إعجاب").fill("")
        page.get_by_role("tab", name="تدريجي").click()
        page.locator(".admin-form input[type=number]").last.fill("2")
        page.locator(".admin-form").get_by_role("button", name="تطبيق").click()
        expect(page.locator(".toast").last).to_contain_text("التدريجية")

        page.get_by_role("tab", name="المكتبة").click()
        page.get_by_role("button", name="استيراد دفعة").click()
        page.locator(".sheet textarea").fill("فكرة رائعة!\nواصل، عمل ممتاز\nفكرة رائعة!\n\nأعجبتني جدًا")
        page.locator(".sheet").get_by_role("button", name="استيراد").click()
        expect(page.locator(".toast").last).to_contain_text("أُضيف 3")
        expect(page.locator("#admin-main .admin-card")).to_have_count(3)
        self.step("library: bulk import (duplicates and blank lines skipped)")
        self.shot(page, "11-library-dark", full=True)

        page.get_by_role("tab", name="تعليقات").click()
        page.get_by_role("tab", name="عشوائي من تصنيف").click()
        page.get_by_label("العدد لكل منشور").fill("2")
        page.get_by_role("button", name="نشر التعليقات").click()
        expect(page.locator(".toast").last).to_contain_text("نُشر 2")
        self.step("team comments: 2 random library comments on the selected idea (as dzplay)")
        self.shot(page, "12-comments-dark", full=True)

        page.get_by_role("tab", name="العمليات").click()
        job = page.locator(".admin-card", has_text="تعزيز")
        expect(job).to_have_count(1)
        expect(job.locator(".admin-progress")).to_be_visible()
        self.shot(page, "13-jobs-dark")
        job.get_by_role("button", name="إلغاء العملية كلها").click()
        page.locator(".sheet").get_by_role("button", name="إلغاء العملية").click()
        expect(page.locator(".admin-empty")).to_contain_text("لا توجد عمليات جارية")
        self.step("gradual set-to-300 job listed with progress, then cancelled")

        # the team comments are marked internally on the idea page
        page.get_by_role("tab", name="المحتوى").click()
        page.locator(".admin-card", has_text="فكرة جميلة").get_by_role("button", name="كل التعليقات").click()
        expect(page.locator(".admin-detail .admin-card--team")).to_have_count(2)
        expect(page.locator(".admin-detail .chip--team").first).to_have_text("تعليق الفريق")
        page.locator(".admin-detail").get_by_role("button", name="إغلاق").click()

    def system(self, page: Page) -> None:
        page.get_by_role("tab", name="الأمان والنظام").click()
        expect(page.locator(".admin-group__title", has_text="بوت Telegram")).to_be_visible()
        expect(page.locator(".admin-row", has_text="الحالة").first).to_contain_text("غير مربوط")
        bot_card = page.locator(".admin-group", has_text="رمز البوت (Token)")
        bot_card.get_by_label("رمز البوت (Token)").fill("123456789:AAH" + "x" * 32)
        bot_card.get_by_label("رقم محادثتك (Chat ID)").fill("323530056")
        bot_card.get_by_label("رمز التحقق من تطبيق المصادقة").fill("000000")
        page.get_by_role("button", name="حفظ وربط البوت").click()
        expect(page.locator(".toast--error").last).to_contain_text("رمز التحقق غير صحيح")
        assert bot_card.get_by_label("رمز البوت (Token)").input_value() == ""  # never kept in the page
        self.step("bot form: token + chat ID + 2FA code; a wrong code is refused and the token field is cleared")
        bot_card.scroll_into_view_if_needed()
        self.shot(page, "14a-bot-form-dark")
        mail_card = page.locator(".admin-group", has_text="البريد (رموز استعادة كلمة المرور)")
        expect(mail_card.locator(".admin-row").first).to_contain_text("وضع يدوي")
        mail_card.get_by_role("button", name="إعداد Gmail").click()
        assert mail_card.get_by_label("الخادم (SMTP)").input_value() == "smtp.gmail.com"
        assert mail_card.get_by_label("المنفذ").input_value() == "587"
        self.step("e-mail form: manual mode shown, the Gmail button fills smtp.gmail.com:587")
        mail_card.scroll_into_view_if_needed()
        self.shot(page, "14b-mail-form-dark")
        block = page.locator(".admin-event", has_text="حظر دخول")
        expect(block).to_have_count(1)
        self.shot(page, "14-system-dark", full=True)
        block.get_by_role("button", name="رفع الحظر").click()
        page.locator(".sheet").get_by_role("button", name="رفع الحظر").click()
        expect(page.locator(".admin-event", has_text="حظر دخول")).to_have_count(0)
        page.get_by_role("button", name="تشغيل التنظيف الآن").click()
        expect(page.locator(".admin-row", has_text="رسائل منتهية")).to_be_visible()
        self.step("system: bot/SMTP/cache status, network block lifted, cleanup ran")

    def light(self, browser) -> None:
        ctx = browser.new_context(**MOBILE, color_scheme="light", locale="ar-DZ", timezone_id="Africa/Algiers")
        page = ctx.new_page()
        self.watch(page, "light")
        page.goto(self.base + ADMIN_PATH)
        expect(page.locator("#admin-user")).to_be_visible()
        self.shot(page, "18-login-light")
        self.sign_in(page, "light", goto=False)
        expect(page.locator(".viz-card")).to_have_count(4)
        page.wait_for_timeout(300)
        self.shot(page, "19-overview-light-full", full=True)
        page.get_by_role("tab", name="البلاغات").click()
        expect(page.locator(".admin-card")).to_have_count(1)
        self.shot(page, "20-reports-light", full=True)
        for tab, name in (("المستخدمون", "21-users-light"), ("المحتوى", "22-content-light"), ("التفاعل", "23-engage-light"),
                          ("الأمان والنظام", "24-system-light")):
            page.get_by_role("tab", name=tab, exact=True).click()
            page.wait_for_timeout(500)
            assert "null" not in page.locator("#admin-main").inner_text(), tab
            self.shot(page, name, full=True)
        # session survives a reload in the same tab
        page.reload()
        expect(page.locator(".admin-tab").first).to_be_visible()
        self.step("light mode; session kept across reload")
        ctx.close()

    def privacy_notice(self, browser) -> None:
        ctx = browser.new_context(**MOBILE, color_scheme="dark", locale="ar-DZ")
        page = ctx.new_page()
        self.watch(page, "notice")
        page.goto(self.base + "/")
        page.get_by_role("tab", name="تسجيل الدخول").click()
        page.locator("#email").fill("old-user@example.com")
        page.locator("#password").fill(PASSWORD)
        page.locator(".antibot").click()
        expect(page.locator(".antibot")).to_have_attribute("data-state", "done", timeout=20000)
        page.get_by_role("button", name="دخول").click()
        expect(page.locator(".privacy-notice h2")).to_have_text("تحديث في سياسة الخصوصية", timeout=15000)
        self.shot(page, "26-privacy-notice")
        page.get_by_role("button", name="فهمت").click()
        expect(page.locator(".privacy-notice")).to_have_count(0)
        page.wait_for_timeout(500)
        page.reload()
        expect(page.locator("#idea-compose")).to_be_visible(timeout=15000)
        page.wait_for_timeout(800)
        expect(page.locator(".privacy-notice")).to_have_count(0)
        self.step("existing user sees the privacy update once")
        ctx.close()

    def desktop(self, browser) -> None:
        ctx = browser.new_context(viewport={"width": 1280, "height": 900}, color_scheme="dark", locale="ar-DZ")
        page = ctx.new_page()
        self.watch(page, "desktop")
        self.sign_in(page, "desk")
        expect(page.locator(".viz-card")).to_have_count(4)
        self.shot(page, "25-overview-desktop", full=True)
        ctx.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shots", default=str(ROOT / "e2e" / "shots-admin"))
    args = ap.parse_args()
    proc, base, db_url = start_server(8771)
    try:
        pid = seed(base, db_url)
        run = Run(base, Path(args.shots))
        with sync_playwright() as p:
            exe = os.environ.get("PW_CHROMIUM")
            browser = p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()
            run.dark(browser)
            run.light(browser)
            run.desktop(browser)
            run.privacy_notice(browser)
            browser.close()
        viewer = register(base, "viewer-c@example.com")
        feed = viewer.get("/api/posts/feed").json()["posts"]
        assert pid not in [x["id"] for x in feed], "removed post still in feed"
        run.step("removed post is gone from the public feed")
        boosted = viewer.get(f"/api/posts/{SEEDED['idea']}").json()
        assert boosted["likes"] >= 120 and boosted["dislikes"] == 4, boosted
        run.step(f"users see the displayed numbers: {boosted['likes']} likes / {boosted['dislikes']} dislikes")
        if run.errors:
            print("\n".join(run.errors))
            raise SystemExit(1)
        print("ADMIN E2E PASSED")
    finally:
        proc.terminate()
        proc.wait(5)


if __name__ == "__main__":
    main()
