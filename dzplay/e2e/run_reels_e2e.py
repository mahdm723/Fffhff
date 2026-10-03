"""Browser test of Reels on a phone viewport (touch), with real processed media.

    python e2e/run_reels_e2e.py [--shots DIR]

The app runs in-process with a local fake of the Telegram Bot API (Telegram is
not reachable from CI); reels are uploaded through the real webhook and
prepared by the real ffmpeg/Pillow pipeline. Then Chromium checks: autoplay,
vertical snap, photo carousel vs the Reels⇄Ideas swipe, caption more/less,
reactions, public comments with the keyboard, prefetch timing, network
priority for messages, and the Save-Data mode.

Codec note: Playwright's open-source Chromium has no H.264 decoder (phones,
Chrome, Safari and Android WebView do). The server still prepares H.264 MP4;
only in the playback part of this test the browser receives a VP9 copy of the
same clip (page.route, service worker off). Prefetch is measured on the real
MP4 bytes with the service worker on.
"""

from __future__ import annotations

import argparse
import io
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import httpx
import uvicorn
from playwright.sync_api import Page, expect, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.security.pow import solve  # noqa: E402
from tests import fake_telegram as tg  # noqa: E402
from tests.conftest import ffmpeg_binary, ffprobe_shim  # noqa: E402

PASSWORD = "Str0ng-Pass!"
SECRET = "e2e-hook-secret"
MOBILE = {"viewport": {"width": 390, "height": 844}, "device_scale_factor": 2, "is_mobile": True, "has_touch": True}
LONG_CAPTION = ("هذا وصف طويل لمقطع على DZPLAY يشرح الفكرة بالتفصيل. " * 6).strip()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def to_webm(src: bytes, tmp: Path, name: str) -> bytes:
    a, b = tmp / f"{name}.src.mp4", tmp / f"{name}.webm"
    a.write_bytes(src)
    subprocess.run([ffmpeg_binary(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(a), "-c:v", "libvpx-vp9",
                    "-b:v", "600k", "-deadline", "realtime", "-cpu-used", "8", "-c:a", "libopus", str(b)], check=True, timeout=300)
    return b.read_bytes()


def make_media(tmp: Path) -> dict[str, bytes]:
    ff = ffmpeg_binary()
    out = {}
    for name, src in (("v1", "testsrc2=size=480x854:rate=25"), ("v2", "mandelbrot=size=480x854:rate=25"),
                      ("v3", "smptebars=size=480x854:rate=25"), ("v4", "rgbtestsrc=size=480x854:rate=25")):
        path = tmp / f"{name}.mp4"
        subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", src, "-f", "lavfi",
                        "-i", "sine=frequency=500", "-t", "6", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-b:v", "900k",
                        "-c:a", "aac", "-shortest", str(path)], check=True, timeout=180)
        out[name] = path.read_bytes()
    from PIL import Image, ImageDraw

    for i, color in enumerate(((220, 70, 60), (60, 160, 90), (70, 90, 210))):
        im = Image.new("RGB", (900, 1400), color)
        ImageDraw.Draw(im).text((60, 60), f"{i + 1}/3", fill=(255, 255, 255))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=90)
        out[f"img{i}"] = buf.getvalue()
    return out


class Server:
    def __init__(self, tmp: Path):
        from tests.smtp_sink import SmtpSink

        self.smtp = SmtpSink().__enter__()
        self.fake = tg.FakeTelegram()
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.settings = Settings(
            ENV="development", SECRET_KEY="e2e-secret", DATABASE_URL=f"sqlite:///{tmp}/reels.db", REDIS_URL="",
            CLEANUP_INTERVAL=0, LOG_LEVEL="WARNING", GOOGLE_CLIENT_ID="", MAX_ACCOUNTS_PER_IP=20,
            TELEGRAM_BOT_TOKEN=tg.TOKEN, TELEGRAM_ADMIN_CHAT_ID=str(tg.ADMIN_ID), TELEGRAM_WEBHOOK_SECRET=SECRET,
            TELEGRAM_ALBUM_SETTLE_SECONDS=0.4, MEDIA_CACHE_DIR=str(tmp / "media"), FFMPEG_BINARY=ffmpeg_binary(),
            SMTP_HOST="127.0.0.1", SMTP_PORT=self.smtp.port, SMTP_SECURITY="none", SMTP_FROM="DZPLAY <no-reply@dzplay.test>",
            FFPROBE_BINARY=ffprobe_shim(), PREFETCH_COUNT=2, PREFETCH_AHEAD=2,
        )
        self.app = create_app(self.settings, telegram_transport=self.fake.transport)
        self.server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self) -> None:
        self.thread.start()
        for _ in range(100):
            try:
                urllib.request.urlopen(self.base + "/healthz", timeout=1)
                return
            except Exception:  # noqa: BLE001
                time.sleep(0.1)
        raise RuntimeError("server did not start")

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(5)
        self.smtp.__exit__(None, None, None)

    def hook(self, update: dict) -> None:
        r = httpx.post(self.base + "/api/telegram/webhook", json=update, headers={"X-Telegram-Bot-Api-Secret-Token": SECRET})
        assert r.status_code == 200, r.text

    def idle(self) -> None:
        self.app.state.dz.bot.wait_idle(120)


def api_user(base: str, email: str) -> httpx.Client:
    c = httpx.Client(base_url=base, headers={"X-DZ-Requested": "1"}, timeout=30)
    ch = c.post("/api/auth/challenge", json={"purpose": "register"}).json()
    ch["number"] = solve(ch)
    r = c.post("/api/auth/register", json={"email": email, "password": PASSWORD, "password_confirm": PASSWORD, "antibot": ch,
                                           "gender": "unspecified", "age_confirmed": True})
    assert r.status_code == 201, r.text
    return c


class Run:
    def __init__(self, shots: Path):
        self.shots = shots
        self.errors: list[str] = []
        self.metrics: dict[str, float] = {}
        shots.mkdir(parents=True, exist_ok=True)

    def watch(self, page: Page, who: str, expected: tuple[str, ...] = ()) -> None:
        ok = ("status of 401",) + expected  # 401: /api/me before sign-in

        def on_console(m):
            if m.type == "error" and not any(x in m.text for x in ok):
                self.errors.append(f"{who} console: {m.text}")

        page.on("console", on_console)
        page.on("pageerror", lambda e: self.errors.append(f"{who} pageerror: {e}"))

    def shot(self, page: Page, name: str) -> None:
        page.wait_for_timeout(350)
        page.screenshot(path=str(self.shots / f"{name}.png"))

    @staticmethod
    def step(text: str) -> None:
        print(f"  ✓ {text}", flush=True)


def register_ui(page: Page, base: str, email: str) -> None:
    page.goto(base + "/")
    page.get_by_role("tab", name="حساب جديد").click()
    page.locator("#email").fill(email)
    page.locator("#password").fill(PASSWORD)
    page.locator("#password_confirm").fill(PASSWORD)
    page.locator(".gender-pick__opt", has_text="أفضّل عدم الذكر").click()
    page.locator("#age_confirmed").check()
    page.locator(".antibot").click()
    expect(page.locator(".antibot")).to_have_attribute("data-state", "done", timeout=30000)
    page.get_by_role("button", name="إنشاء الحساب").click()
    expect(page.locator(".home-switch")).to_be_visible(timeout=15000)


def swipe(page: Page, x: float, y: float, dx: float, dy: float, steps: int = 14) -> None:
    """A real finger drag (touchstart → touchmoves → touchend through Chromium's input pipeline:
    scroll latching, chaining and snapping behave as on a phone). dx > 0 = finger moves right."""
    cdp = page.context.new_cdp_session(page)
    cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]})
    for i in range(1, steps + 1):
        cdp.send("Input.dispatchTouchEvent", {"type": "touchMove",
                                              "touchPoints": [{"x": x + dx * i / steps, "y": y + dy * i / steps}]})
        time.sleep(0.016)
    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    cdp.detach()
    page.wait_for_timeout(900)


ACTIVE_JS = """() => { const r = [...document.querySelectorAll('.reel')].map(el => el.getBoundingClientRect());
  return r.findIndex(b => Math.abs(b.top) < 40); }"""
PANE_JS = "() => document.querySelector('.home-switch').dataset.pane"


def playing(page: Page, index: int, timeout: float = 15.0) -> float:
    """Seconds until reel #index's video is actually playing."""
    t0 = time.monotonic()
    page.wait_for_function("""(i) => { const v = document.querySelectorAll('.reel')[i]?.querySelector('video');
        return v && !v.paused && v.currentTime > 0.05 && v.readyState >= 3; }""", arg=index, timeout=timeout * 1000)
    return time.monotonic() - t0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shots", default=str(ROOT / "e2e" / "shots-reels"))
    args = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="dz-reels-e2e-"))
    media = make_media(tmp)
    srv = Server(tmp)
    srv.start()
    run = Run(Path(args.shots))
    try:
        print("Upload through the Telegram webhook")
        for name, cap in (("v1", "المقطع الأول ✨"), ("v2", LONG_CAPTION), ("v3", "مقطع ثالث")):
            fid = srv.fake.add_file(media[name], name=name)
            srv.hook(tg.video(fid, len(media[name]), cap, width=480, height=854))
        ids = [srv.fake.add_file(media[f"img{i}"], name=f"img{i}") for i in range(3)]
        for i, fid in enumerate(ids):
            srv.hook(tg.photo(fid, len(media[f"img{i}"]), caption="ألبوم من ثلاث صور" if i == 0 else None,
                              group="album-1", message_id=9000 + i))
        srv.idle()
        assert sum("نُشر Reel" in t for t in srv.fake.texts()) == 4, srv.fake.texts()
        run.step("3 videos + 1 three-photo album published via the bot")
        # A newer reel pinned to the top, so the first screen is deterministic.
        fid = srv.fake.add_file(media["v4"], name="v4")
        srv.hook(tg.video(fid, len(media["v4"]), "مقطع مثبّت", width=480, height=854))
        srv.idle()
        pinned = srv.fake.last_text().split("Reel ")[1].split(" ")[0]
        srv.hook(tg.text(f"/pin {pinned}"))
        srv.idle()

        # VP9 copies for the codec-less test browser (see module docstring), keyed by asset id.
        from sqlalchemy import select

        from app.models import ReelAsset

        with srv.app.state.dz.database.session() as db:
            assets = {a: f.split("-")[0] for a, f in db.execute(select(ReelAsset.id, ReelAsset.tg_file_id)).all()}
        webm = {aid: to_webm(media[name], tmp, name) for aid, name in assets.items() if name.startswith("v")}

        def serve_webm(route):
            aid = route.request.url.split("/media/")[1].split("/")[0]
            route.fulfill(status=200, body=webm[aid], content_type="video/webm") if aid in webm else route.continue_()

        exe = os.environ.get("PW_CHROMIUM")
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()

            # ---------------------------------------------------------------- main flow (dark)
            ctx = browser.new_context(**MOBILE, color_scheme="dark", locale="ar-DZ", service_workers="block")
            ctx.route("**/media/*/mp4*", serve_webm)
            page = ctx.new_page()
            run.watch(page, "A")
            register_ui(page, srv.base, f"reels-a{int(time.time())}@example.com")
            assert page.evaluate(PANE_JS) == "reels"
            expect(page.locator(".reel").first).to_be_visible()
            run.metrics["first_play_s"] = playing(page, 0)
            assert page.locator(".reel").first.locator(".reel__caption").inner_text().startswith("مقطع مثبّت")
            run.step(f"Home opens on Reels; pinned reel autoplays (muted) in {run.metrics['first_play_s']:.2f}s")
            run.shot(page, "01-reel-video-dark")

            # tap = pause / play, mute toggle
            page.locator(".reel").first.locator(".reel__stage").click()
            page.wait_for_function("() => document.querySelector('.reel video').paused")
            page.locator(".reel").first.locator(".reel__stage").click()
            playing(page, 0)
            mute = page.locator(".reel").first.get_by_role("button", name="تشغيل الصوت")
            mute.click()
            expect(page.locator(".reel").first.get_by_role("button", name="كتم الصوت")).to_be_visible()
            assert page.evaluate("() => document.querySelector('.reel video').muted") is False
            run.step("tap pauses/resumes, mute button toggles sound")

            # vertical snap to the next reel; the previous one pauses
            swipe(page, 195, 700, 0, -520)
            page.wait_for_function(f"() => ({ACTIVE_JS})() === 1")
            run.metrics["next_play_s"] = playing(page, 1, timeout=30)
            assert page.evaluate("() => document.querySelectorAll('.reel video')[0].paused")
            run.step(f"swipe up snaps to the next reel; it plays in {run.metrics['next_play_s']:.2f}s, previous paused")

            # find the album and the long caption
            reels_n = page.locator(".reel").count()
            album_i = page.evaluate("() => [...document.querySelectorAll('.reel')].findIndex(r => r.classList.contains('reel--images'))")
            long_i = page.evaluate("(t) => [...document.querySelectorAll('.reel__text')].findIndex(p => p.textContent.startsWith(t))",
                                   LONG_CAPTION[:20])
            assert album_i >= 0 and reels_n == 5

            # caption: two lines, "المزيد" then "أقل"
            for _ in range(10):
                if page.evaluate(ACTIVE_JS) == page.evaluate(
                        "(t) => [...document.querySelectorAll('.reel')].findIndex(r => r.querySelector('.reel__text')?.textContent.startsWith(t))",
                        LONG_CAPTION[:20]):
                    break
                swipe(page, 195, 700, 0, -520)
            cap = page.locator(".reel__caption").filter(has_text=LONG_CAPTION[:20])
            more = cap.get_by_role("button", name="المزيد")
            expect(more).to_be_visible()
            h_closed = cap.bounding_box()["height"]
            run.shot(page, "02-caption-two-lines")
            more.click()
            expect(cap.get_by_role("button", name="أقل")).to_be_visible()
            assert cap.bounding_box()["height"] > h_closed * 2
            run.shot(page, "03-caption-open")
            cap.get_by_role("button", name="أقل").click()
            expect(cap.get_by_role("button", name="المزيد")).to_be_visible()
            run.step("long caption: 2 lines → المزيد shows all → أقل collapses")

            # like / dislike
            like = page.locator(".reel").nth(page.evaluate(ACTIVE_JS)).get_by_role("button", name="أعجبني")
            like.click()
            expect(like).to_have_attribute("aria-pressed", "true")
            expect(like.locator(".reel__n")).to_have_text("1")
            dislike = page.locator(".reel").nth(page.evaluate(ACTIVE_JS)).get_by_role("button", name="لم يعجبني")
            dislike.click()
            expect(like).to_have_attribute("aria-pressed", "false")
            expect(dislike.locator(".reel__n")).to_have_text("1")
            dislike.click()
            expect(dislike.locator(".reel__n")).to_have_text("0")
            run.step("like → switch to dislike → remove; counts follow")

            # public comments with the keyboard
            page.locator(".reel").nth(page.evaluate(ACTIVE_JS)).get_by_role("button", name="التعليقات").click()
            sheet = page.locator(".reel-comments")
            expect(sheet.locator(".rc-empty")).to_be_visible()
            box = sheet.locator("textarea")
            box.click()
            box.fill("تعليق عام من المستخدم الأول")
            run.shot(page, "04-comments-typing")
            sheet.get_by_role("button", name="إرسال").click()
            expect(sheet.locator(".rc-text").first).to_have_text("تعليق عام من المستخدم الأول")
            expect(sheet.locator(".rc-name").first).to_have_text("dzplay")
            run.shot(page, "05-comments-sent")
            page.keyboard.press("Escape")
            expect(page.locator(".reel").nth(page.evaluate(ACTIVE_JS)).get_by_role("button", name="التعليقات").locator(".reel__n")).to_have_text("1")
            reel_id = page.evaluate(f"() => {{ const i = ({ACTIVE_JS})(); return null; }}")
            b = api_user(srv.base, f"reels-b{int(time.time())}@example.com")
            feed = b.get("/api/reels/feed", params={"limit": 20}).json()["reels"]
            commented = next(r for r in feed if r["comments"] == 1)
            seen_by_b = b.get(f"/api/reels/{commented['id']}/comments").json()["comments"]
            assert seen_by_b[0]["content"] == "تعليق عام من المستخدم الأول" and seen_by_b[0]["mine"] is False
            run.step("comment sheet: typed + sent; another user reads it (comments are public)")
            del reel_id

            # photo carousel vs the Reels ⇄ Ideas pager
            target = page.evaluate("() => [...document.querySelectorAll('.reel')].findIndex(r => r.classList.contains('reel--images'))")
            page.locator(".reel").nth(target).scroll_into_view_if_needed()
            page.wait_for_timeout(600)
            page.wait_for_function(f"(t) => ({ACTIVE_JS})() === t", arg=target)
            dots = page.locator(".reel").nth(target).locator(".reel__dots span")
            expect(dots).to_have_count(3)
            page.wait_for_function("(t) => document.querySelectorAll('.reel')[t].querySelectorAll('img[src]').length === 3", arg=target)
            run.shot(page, "06-album-photo1")
            # RTL: the next photo is to the left, so the finger moves right (positive xDistance).
            swipe(page, 120, 420, 260, 0)
            expect(dots.nth(1)).to_have_class("is-on")
            assert page.evaluate(PANE_JS) == "reels", "first swipe must turn the photo, not change page"
            swipe(page, 120, 420, 260, 0)
            expect(dots.nth(2)).to_have_class("is-on")
            assert page.evaluate(PANE_JS) == "reels"
            run.shot(page, "07-album-photo3")
            swipe(page, 120, 420, 260, 0)  # already on the last photo → the pager moves to Ideas
            page.wait_for_function(f"() => ({PANE_JS})() === 'ideas'", timeout=5000)
            expect(page.locator("#idea-compose")).to_be_in_viewport()
            run.shot(page, "08-ideas-after-swipe")
            assert page.evaluate("() => [...document.querySelectorAll('.reel video')].every(v => v.paused)")
            swipe(page, 300, 420, -260, 0)  # back to Reels
            page.wait_for_function(f"() => ({PANE_JS})() === 'reels'", timeout=5000)
            run.step("album: swipes turn photos 1→2→3; at the last photo the next swipe opens Ideas; swipe back to Reels")

            page.get_by_role("tab", name="الأفكار").click()
            page.wait_for_function(f"() => ({PANE_JS})() === 'ideas'")
            page.get_by_role("tab", name="Reels").click()
            page.wait_for_function(f"() => ({PANE_JS})() === 'reels'")
            run.step("top indicator Reels | الأفكار switches panes")
            ctx.close()

            # ---------------------------------------------------------------- prefetch (good network)
            ctx = browser.new_context(**MOBILE, color_scheme="light", locale="ar-DZ")
            page = ctx.new_page()
            run.watch(page, "P")
            register_ui(page, srv.base, f"reels-p{int(time.time())}@example.com")
            page.goto(srv.base + "/#/messages")  # user is NOT on Reels
            page.wait_for_function("""async () => { const c = await caches.open('dz-media-v1');
                const keys = (await c.keys()).map(r => new URL(r.url).pathname);
                return keys.filter(k => k.endsWith('/mp4')).length >= 2 && keys.filter(k => k.endsWith('/poster')).length >= 2; }""",
                                   timeout=30000)
            run.step("prefetch while on Messages: posters + first 2 videos cached on the device")
            # Messages API stays fast while a prefetch is running (throttled network).
            cdp = ctx.new_cdp_session(page)
            cdp.send("Network.enable")
            cdp.send("Network.emulateNetworkConditions", {"offline": False, "latency": 60,
                                                          "downloadThroughput": 400 * 1024 / 8 * 8, "uploadThroughput": 200 * 1024})
            page.evaluate("async () => { await caches.delete('dz-media-v1'); localStorage.removeItem('dz:media-index'); }")
            page.evaluate("() => import('/js/reels-prefetch.js').then(m => { m.resetFeed(); m.startPrefetch(); })")
            page.wait_for_timeout(300)
            t = page.evaluate("""async () => { const t0 = performance.now();
                await fetch('/api/conversations', { headers: { 'X-DZ-Requested': '1' } }); return performance.now() - t0; }""")
            run.metrics["messages_ms_during_prefetch"] = t
            assert t < 1500, f"messages request took {t:.0f}ms during prefetch"
            run.step(f"messages request during prefetch on a slow link: {t:.0f} ms")
            page.wait_for_function("""async () => (await (await caches.open('dz-media-v1')).keys()).some(r => r.url.endsWith('/mp4'))""", timeout=120000)
            # Still on the slow link: compare the first video from the device cache vs the network.
            page.get_by_role("button", name="الرئيسية").click()
            expect(page.locator(".reel").first).to_be_visible()
            src = page.evaluate("() => import('/js/reels-prefetch.js').then(m => m.feedState().reels[0].media[0].src)")
            cached = page.evaluate("""async (src) => { const t0 = performance.now();
                const r = await fetch(src, { headers: { Range: 'bytes=0-262143' } }); await r.arrayBuffer();
                return [performance.now() - t0, r.status]; }""", src)
            cdp.send("Network.emulateNetworkConditions", {"offline": False, "latency": 0, "downloadThroughput": -1, "uploadThroughput": -1})
            run.metrics["first_256k_from_device_cache_ms"] = cached[0]
            assert cached[1] == 206 and cached[0] < 300, cached
            run.step(f"open Reels after prefetch: first 256 KB of the video served from the device cache in {cached[0]:.0f} ms (206 Range)")
            run.shot(page, "09-reel-light")
            ctx.close()

            # ---------------------------------------------------------------- Save-Data: posters only
            ctx = browser.new_context(**MOBILE, color_scheme="dark", locale="ar-DZ")
            ctx.add_init_script("Object.defineProperty(navigator, 'connection', { value: { saveData: true, effectiveType: '3g' } });")
            page = ctx.new_page()
            run.watch(page, "S")
            register_ui(page, srv.base, f"reels-s{int(time.time())}@example.com")
            page.goto(srv.base + "/#/messages")
            page.wait_for_function("""async () => (await (await caches.open('dz-media-v1')).keys()).some(r => r.url.endsWith('/poster'))""",
                                   timeout=30000)
            page.wait_for_timeout(1500)
            keys = page.evaluate("async () => (await (await caches.open('dz-media-v1')).keys()).map(r => new URL(r.url).pathname)")
            assert keys and not any(k.endswith("/mp4") for k in keys), keys
            run.step(f"Save-Data / 3G: only posters prefetched ({len(keys)} files, no video)")
            ctx.close()
            # ---------------------------------------------------------------- forgot password
            email = f"forgot{int(time.time())}@example.com"
            old_session = api_user(srv.base, email)
            ctx = browser.new_context(**MOBILE, color_scheme="dark", locale="ar-DZ")
            page = ctx.new_page()
            run.watch(page, "R", expected=("status of 400",))  # the deliberately wrong code
            page.goto(srv.base + "/")
            page.get_by_role("button", name="نسيت كلمة السر؟").click()
            page.locator("#reset-email").fill(email)
            page.locator(".antibot:visible").click()
            expect(page.locator(".antibot:visible")).to_have_attribute("data-state", "done", timeout=30000)
            run.shot(page, "10-reset-step1")
            page.get_by_role("button", name="إرسال الطلب").click()
            expect(page.locator(".reset__sent")).to_contain_text("إن كان هذا البريد مسجّلًا")
            expect(page.locator("#reset-code")).to_be_visible()
            run.shot(page, "11-reset-step2")
            srv.idle()
            import re as _re

            rid = _re.search(r"رقم الطلب: ([A-Z0-9]+)", next(t for t in reversed(srv.fake.texts()) if "طلب استعادة" in t)).group(1)
            srv.hook(tg.callback(f"rgen:{rid}"))
            srv.idle()
            assert "أُرسل رمز الاستعادة" in srv.fake.last_text()
            code = _re.search(r"^ {4}([A-Z0-9]{4,12})\s*$", srv.smtp.last_text(), _re.M).group(1)
            page.locator("#reset-code").fill("000000" if code != "000000" else "111111")
            page.get_by_role("button", name="تحقق من الرمز").click()
            expect(page.get_by_role("alert").filter(has_text="الرمز غير صحيح")).to_be_visible()
            page.locator("#reset-code").fill(code)
            page.get_by_role("button", name="تحقق من الرمز").click()
            expect(page.locator("#reset-password")).to_be_visible()
            page.locator("#reset-password").fill("Brand-New-Pass-42")
            page.locator("#reset-password2").fill("Brand-New-Pass-42")
            page.locator(".antibot:visible").click()
            expect(page.locator(".antibot:visible")).to_have_attribute("data-state", "done", timeout=30000)
            run.shot(page, "12-reset-step3")
            page.get_by_role("button", name="تغيير كلمة المرور والدخول").click()
            expect(page.locator(".home-switch")).to_be_visible(timeout=15000)
            assert old_session.get("/api/me").status_code == 401
            run.step("forgot password: e-mail → admin taps «توليد رمز» in Telegram → code e-mailed → wrong code refused → "
                     "right code → new password → signed in; the old session was signed out")
            ctx.close()
            browser.close()

        if run.errors:
            print("\n".join(run.errors))
            return 1
        print("metrics:", {k: round(v, 3) for k, v in run.metrics.items()})
        print("REELS E2E PASSED")
        return 0
    finally:
        srv.stop()


if __name__ == "__main__":
    sys.exit(main())
