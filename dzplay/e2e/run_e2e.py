"""End-to-end browser test: two real browser users drive the whole MVP flow.

    python e2e/run_e2e.py [--base-url http://127.0.0.1:8765] [--shots DIR]

Starts its own server (temporary SQLite DB) unless --base-url is given.
Requires Playwright + Chromium (set PW_CHROMIUM to a custom executable).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
PASSWORD = "Str0ng-Pass!"
MOBILE = {"viewport": {"width": 390, "height": 844}, "device_scale_factor": 2, "is_mobile": True, "has_touch": True}


def start_server(port: int, extra_env: dict | None = None) -> tuple[subprocess.Popen, str]:
    tmp = tempfile.mkdtemp(prefix="dz-e2e-")
    env = dict(os.environ, DATABASE_URL=f"sqlite:///{tmp}/e2e.db", SECRET_KEY="e2e-secret", ENV="development",
               CLEANUP_INTERVAL="60", LOG_LEVEL="WARNING", REDIS_URL="", GOOGLE_CLIENT_ID="", MAX_ACCOUNTS_PER_IP="10",
               MEDIA_CACHE_DIR=f"{tmp}/media-cache", UPLOAD_TMP_DIR=f"{tmp}/upload-tmp")
    env.update(extra_env or {})
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port),
                             "--no-access-log", "--timeout-graceful-shutdown", "2"],
                            cwd=ROOT, env=env)
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(base + "/healthz", timeout=1)
            return proc, base
        except Exception:  # noqa: BLE001
            time.sleep(0.1)
    proc.kill()
    raise RuntimeError("server did not start")


class Run:
    def __init__(self, base: str, shots: Path):
        self.base = base
        self.shots = shots
        self.errors: list[str] = []
        shots.mkdir(parents=True, exist_ok=True)

    def watch(self, page: Page, who: str) -> None:
        page.on("console", lambda m: m.type == "error" and self.errors.append(f"{who} console: {m.text}"))
        page.on("pageerror", lambda e: self.errors.append(f"{who} pageerror: {e}"))

    def shot(self, page: Page, name: str) -> None:
        page.wait_for_timeout(450)  # let entry animations settle
        page.screenshot(path=str(self.shots / f"{name}.png"))

    def step(self, text: str) -> None:
        print(f"  ✓ {text}", flush=True)


def register(run: Run, page: Page, email: str, shot: str | None = None, gender: str = "أفضّل عدم الذكر") -> None:
    page.goto(run.base + "/")
    page.get_by_role("tab", name="حساب جديد").click()
    page.locator("#email").fill(email)
    page.locator("#password").fill(PASSWORD)
    page.locator("#password_confirm").fill(PASSWORD)
    page.locator(".gender-pick__opt", has_text=gender).click()
    page.locator("#age_confirmed").check()
    page.locator(".antibot").click()
    expect(page.locator(".antibot")).to_have_attribute("data-state", "done", timeout=20000)
    if shot:
        run.shot(page, shot)
    page.get_by_role("button", name="إنشاء الحساب").click()
    expect(page.locator("#idea-compose")).to_be_visible(timeout=15000)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--shots", default=str(ROOT / "e2e" / "screenshots"))
    parser.add_argument("--ignore-https-errors", action="store_true", help="for self-signed test certificates")
    args = parser.parse_args()

    proc = None
    base = args.base_url
    if not base:
        proc, base = start_server(args.port)
    run = Run(base, Path(args.shots))
    chromium = os.environ.get("PW_CHROMIUM", "/opt/pw-browsers/chromium-1194/chrome-linux/chrome")
    suffix = str(int(time.time()))
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=chromium if os.path.exists(chromium) else None)
            extra = {"ignore_https_errors": args.ignore_https_errors}
            ctx_a = browser.new_context(**MOBILE, **extra, color_scheme="dark", locale="ar")
            ctx_b = browser.new_context(**MOBILE, **extra, color_scheme="light", locale="ar")
            a, b = ctx_a.new_page(), ctx_b.new_page()
            run.watch(a, "A")
            run.watch(b, "B")

            print("Register")
            register(run, a, f"alice{suffix}@example.com", shot="01-auth-register")
            run.step("A registered (email + password + anti-bot)")
            run.shot(a, "02-home")
            register(run, b, f"bob{suffix}@example.com")
            run.step("B registered")

            print("Public ideas")
            idea = "أحيانًا أفضل حل هو أن تتوقف عن التفكير في رأي الآخرين."
            a.locator("#idea-compose").fill(idea)
            run.shot(a, "03-home-idea-typed")
            a.get_by_role("button", name="نشر", exact=True).click()
            mine = a.locator(".post-card").first
            expect(mine.locator(".post-card__body")).to_have_text(idea)
            expect(mine.locator(".post-card__name")).to_have_text("dzplay")
            run.step("A published an idea (shown as dzplay)")

            b.get_by_role("button", name="أفكار أخرى").click()
            card = b.locator(".post-card", has_text=idea)
            expect(card).to_be_visible(timeout=10000)
            like, dislike = card.locator(".react--like"), card.locator(".react--dislike")
            like.click()
            expect(like).to_have_attribute("aria-pressed", "true")
            expect(like).to_contain_text("1")
            dislike.click()
            expect(dislike).to_have_attribute("aria-pressed", "true")
            expect(like).to_have_attribute("aria-pressed", "false")
            expect(like).to_contain_text("0")
            expect(dislike).to_contain_text("1")
            dislike.click()
            expect(dislike).to_contain_text("0")
            like.click()
            expect(like).to_contain_text("1")
            run.step("B: like → dislike → remove → like (one reaction at a time)")

            card.get_by_role("button", name="تعليق خاص").click()
            b.locator(".sheet textarea").fill("فكرة جميلة، شكرًا لأنك شاركتها.")
            run.shot(b, "04-private-comment")
            b.locator(".sheet").get_by_role("button", name="إرسال التعليق").click()
            expect(b.get_by_text("أُرسل تعليقك إلى صاحب الفكرة فقط.")).to_be_visible()
            assert card.locator(".chip").count() == 0, "a visitor must not see comment counts"
            run.shot(b, "05-feed-visitor")
            run.step("B sent a private comment (B sees no comments or counts)")

            expect(a.locator('[data-tab="profile"] .badge')).to_have_text("1", timeout=10000)
            a.locator('[data-tab="profile"]').click()
            expect(a.locator(".stat__num").nth(0)).to_have_text("1")
            expect(a.locator(".stat__num").nth(1)).to_have_text("1")
            own = a.locator(".post-card", has_text=idea)
            expect(own.locator(".chip")).to_have_text("1 جديد")
            own.get_by_role("button", name="التعليقات على منشورك: 1").click()
            expect(a.locator(".comment__body")).to_have_text("فكرة جميلة، شكرًا لأنك شاركتها.")
            run.shot(a, "06-owner-comments")
            a.keyboard.press("Escape")
            expect(a.locator('[data-tab="profile"] .badge')).to_have_count(0)
            run.step("A (owner) sees the comment in a private sheet; badge cleared")

            card.locator(".post-card__author").click()
            expect(b.locator(".id-card__name")).to_have_text("dzplay")
            expect(b.locator(".stat__num").nth(0)).to_have_text("1")
            expect(b.locator(".stat__num").nth(1)).to_have_text("1")
            expect(b.locator(".stat__num").nth(2)).to_have_text("0")
            expect(b.locator(".post-card", has_text=idea)).to_be_visible()
            assert "فكرة جميلة" not in b.locator(".page").inner_text()
            run.shot(b, "07-public-profile")
            run.step("B opened A's public profile: dzplay + Posts/Likes/Dislikes + posts only")

            print("Anonymous message from the Messages composer")
            a.locator('[data-tab="messages"]').click()
            a.locator(".msg-dock textarea").fill("أحتاج أن أتحدث مع شخص اليوم.")
            expect(a.locator(".nav")).to_be_hidden()  # keyboard open: composer takes the nav's place
            run.shot(a, "08-messages-dock")
            a.locator(".msg-dock .send-btn").click()
            expect(a.get_by_text("وصلت رسالتك إلى شخص ما")).to_be_visible()
            expect(a.locator(".conv-item")).to_have_count(1)
            run.step("A sent an anonymous message from the Messages page")

            print("Receive (realtime)")
            b.locator('[data-tab="messages"]').click()
            item = b.locator(".conv-item").first
            expect(item).to_be_visible(timeout=10000)
            expect(item.locator(".conv-item__name")).to_have_text("dzplay")
            expect(item.locator(".unread-dot")).to_have_count(1)
            run.shot(b, "09-messages-list")
            item.click()
            expect(b.locator(".bubble-row.theirs .bubble").first).to_contain_text("أحتاج أن أتحدث مع شخص اليوم.")
            run.step("B received it from 'dzplay' and opened the conversation")

            print("Reply")
            b.locator(".chat__composer textarea").fill("أنا هنا، ماذا حدث؟")
            b.get_by_role("button", name="إرسال").click()
            expect(b.locator(".bubble-row.mine .bubble").last).to_contain_text("أنا هنا، ماذا حدث؟")
            run.shot(b, "10-chat-b")

            a.locator(".conv-item").first.click()
            expect(a.locator(".bubble-row.theirs .bubble").last).to_contain_text("أنا هنا، ماذا حدث؟", timeout=10000)
            # B's message status reaches "read" once A has the chat open.
            expect(b.locator(".bubble__status--read")).to_have_count(1, timeout=10000)
            run.step("A received the reply live; B sees it was read")

            a.locator(".chat__composer textarea").fill("شكرًا لأنك هنا. يومي كان صعبًا.")
            a.locator(".send-btn").click()
            expect(b.locator(".bubble-row.theirs .bubble").last).to_contain_text("يومي كان صعبًا", timeout=10000)
            run.shot(a, "11-chat-a")
            run.step("Conversation continues in both directions in realtime")

            print("Offline / reconnection")
            ctx_b.set_offline(True)
            b.evaluate("window.dispatchEvent(new Event('offline'))")
            expect(b.locator(".chat__composer")).to_be_visible()
            b.locator(".chat__composer textarea").fill("رسالة أثناء انقطاع الإنترنت")
            b.locator(".send-btn").click()
            expect(b.locator(".bubble__status--pending")).to_have_count(1)
            run.shot(b, "12-offline-pending")
            time.sleep(1.5)
            expect(a.get_by_text("رسالة أثناء انقطاع الإنترنت")).to_have_count(0)
            ctx_b.set_offline(False)
            b.evaluate("window.dispatchEvent(new Event('online'))")
            expect(a.locator(".bubble-row.theirs .bubble").last).to_contain_text("رسالة أثناء انقطاع الإنترنت", timeout=15000)
            expect(b.locator(".bubble__status--pending")).to_have_count(0, timeout=10000)
            run.step("Message queued offline was delivered once back online (no duplicates)")
            assert a.locator(".bubble-row.theirs .bubble", has_text="رسالة أثناء انقطاع الإنترنت").count() == 1

            print("Reload keeps history (local cache + sync)")
            a.reload()
            expect(a.locator(".bubble")).to_have_count(4, timeout=10000)
            run.step("History restored after reload")

            print("Profile")
            b.goto(base + "/#/profile")
            expect(b.locator(".id-card__name")).to_have_text("dzplay")
            expect(b.locator(".stat__num").nth(0)).to_have_text("0")  # B published no ideas
            expect(b.locator(".msg-stats")).to_contain_text("أرسلت 2 · استقبلت 2")  # private, owner-only
            run.shot(b, "13-profile")
            run.step("Profile shows only 'dzplay', idea stats and private messaging stats")

            print("Report + block")
            b.goto(base + "/#/messages")
            b.locator(".conv-item").first.click()
            b.get_by_role("button", name="معلومات وخيارات").click()
            run.shot(b, "14-chat-menu")
            b.locator(".sheet").get_by_role("button", name="إبلاغ").click()
            b.get_by_text("تحرش أو مضايقة").click()
            run.shot(b, "15-report")
            b.get_by_role("button", name="إرسال البلاغ").click()
            expect(b.locator(".conv-list, .empty")).to_be_visible(timeout=10000)
            expect(b.locator(".conv-item")).to_have_count(0)
            expect(a.locator(".closed-bar")).to_be_visible(timeout=10000)
            run.shot(a, "16-closed-for-a")
            run.step("B reported + blocked; A can no longer reply")

            b.goto(base + "/#/profile")
            b.get_by_role("button", name="المحظورون").click()
            expect(b.get_by_role("button", name="إلغاء الحظر")).to_have_count(1)
            run.shot(b, "17-blocked-list")
            run.step("Blocked list shows an anonymous entry")
            b.keyboard.press("Escape")

            print("Logout / login")
            a.goto(base + "/#/profile")
            a.get_by_role("button", name="تسجيل الخروج").click()
            a.locator(".sheet").get_by_role("button", name="تسجيل الخروج").click()
            expect(a.get_by_role("tab", name="تسجيل الدخول")).to_be_visible()
            a.locator("#email").fill(f"alice{suffix}@example.com")
            a.locator("#password").fill("wrong-password")
            a.get_by_role("button", name="دخول").click()
            expect(a.locator(".form-error")).to_contain_text("غير صحيحة", timeout=20000)
            run.shot(a, "18-login-error")
            time.sleep(2.2)  # progressive delay after a failure
            a.locator("#password").fill(PASSWORD)
            a.get_by_role("button", name="دخول").click()
            expect(a.locator("#idea-compose")).to_be_visible(timeout=20000)
            run.step("Wrong password refused, then login succeeded")

            print("Privacy checks in the DOM")
            for page, who in ((a, "A"), (b, "B")):
                html = page.content()
                assert "@example.com" not in html, f"{who} page leaks an e-mail"
            run.step("No e-mail or internal identifier in either page")

            browser.close()
    finally:
        if proc:
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
    print(f"\nE2E passed. Screenshots in {run.shots}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
