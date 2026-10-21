"""V6 phase 11c (browser): the logo and the name on every screen, at a phone's width, in light and dark.

    PW_CHROMIUM=… python e2e/run_brand_e2e.py [--shots DIR]

Checks, in each colour scheme:
- the boot screen (before JavaScript), sign-in, sign-up, the home header, the panel's sign-in and header,
  /download and a policy page all show the logo and the name;
- the name's dot is the logo's red (229, 35, 45) whatever the accent;
- the 24px header logo is the small drawing (three bodies, one wick), the large ones have a wick per candle;
- no copy is mirrored in the RTL pages (the tallest on the left, then shorter, then shorter; the dot above it);
- the glow pulses, and stops with «تقليل الحركة» (prefers-reduced-motion);
- the favicon, the iPhone icon and the link-preview image are served;
- no horizontal scroll;
- V6 phase 19 «الهوية» → «الشارات»: the blue star and the gold badge at 14–56 px on the four backgrounds (the real
  size, flat under 28 px and metallic from 28, the theme's colour, centred on the name line like the star), the tap
  sheet «الحساب الرسمي لـ …»; screenshots badges-<scheme>.png (phone) and badges-<scheme>-wide.png.
All problems are listed at the end; any problem fails the run.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_admin_e2e as ae  # noqa: E402
from run_e2e import register  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PROBLEMS: list[str] = []
RED = "rgb(229, 35, 45)"

LOGOS_JS = r"""(sel) => [...document.querySelectorAll(sel)].map((svg) => {
  const box = svg.getBoundingClientRect();
  const rects = [...svg.querySelectorAll('.ink rect')].map((r) => r.getBoundingClientRect());
  const base = rects.reduce((a, b) => (b.width > a.width ? b : a));
  const bodyW = Math.max(...rects.filter((b) => b !== base).map((b) => b.width));
  const bodies = rects.filter((b) => b !== base && b.width > bodyW * 0.9).sort((a, b) => a.left - b.left);
  const wicks = rects.filter((b) => b !== base && b.width < bodyW / 2).length;
  const h = bodies.map((b) => base.bottom - b.top);
  const dot = svg.querySelector('.dot').getBoundingClientRect();
  const dx = dot.left + dot.width / 2;
  const dotOver = bodies.map((b) => Math.abs(b.left + b.width / 2 - dx)).reduce((m, v, i, a) => (v < a[m] ? i : m), 0);
  const glow = svg.querySelector('.glow');
  const anims = svg.getAnimations ? svg.getAnimations({ subtree: true }).filter((a) => a.playState === 'running').length : 0;
  return { w: Math.round(box.width), visible: box.width > 0 && box.height > 0, bodies: bodies.length, wicks,
           order: h[0] > h[1] && h[1] > h[2], dotOver, above: dot.bottom <= bodies[0].top + 0.5,
           glow: !!glow && getComputedStyle(glow).display !== 'none', anims };
})"""


def problem_if(cond: bool, text: str) -> None:
    if cond:
        PROBLEMS.append(text)
        print(f"  ✗ {text}", flush=True)


def logos(page: Page, where: str, sel: str, *, size: int | None = None, small: bool | None = None,
          pulse: bool | None = None) -> None:
    found = page.evaluate(LOGOS_JS, sel)
    problem_if(not found, f"{where}: no logo ({sel})")
    for c in found:
        problem_if(not c["visible"], f"{where}: the logo is not visible: {c}")
        problem_if(c["bodies"] != 3 or not c["order"] or c["dotOver"] != 0 or not c["above"],
                   f"{where}: the logo is mirrored or out of order: {c}")
        problem_if(size is not None and abs(c["w"] - size) > 1, f"{where}: the logo is {c['w']}px, not {size}px")
        if small is not None:
            problem_if(c["wicks"] != (1 if small else 3), f"{where}: {'small' if small else 'large'} drawing expected: {c}")
        if pulse is not None:
            problem_if((c["anims"] > 0) != pulse, f"{where}: pulse {'expected' if pulse else 'must stop'}: {c}")
        problem_if(not c["glow"], f"{where}: the dot has lost its glow: {c}")


def red_dot(page: Page, where: str, sel: str) -> None:
    dots = page.locator(sel)
    problem_if(dots.count() == 0, f"{where}: no name with its dot ({sel})")
    for i in range(dots.count()):
        bg = dots.nth(i).evaluate("(e) => getComputedStyle(e).backgroundColor")
        problem_if(bg != RED, f"{where}: the name's dot is {bg}, not the logo's red")


BADGES_JS = r"""() => [...document.querySelectorAll('#brand-badges .brand-badges__panel')].map((p) => {
  const mid = (e) => { const b = e.getBoundingClientRect(); return b.top + b.height / 2; };
  const off = (sel) => { const m = p.querySelector(`.brand-badges__names ${sel}`); const tx = m && m.parentElement.querySelector('.name-line__text');
    return m && tx ? Math.abs(mid(m) - mid(tx)) : null; };
  const flat = p.querySelector('.gold-mark:not(.gold-mark--metal) .gold-mark__seal');
  return { tone: p.dataset.tone,
    rows: Object.fromEntries([...p.querySelectorAll('.brand-badges__row')].map((r) => [r.dataset.badge,
      [...r.querySelectorAll('.star-mark, .gold-mark')].map((m) => { const b = m.getBoundingClientRect();
        return [Math.round(b.width), Math.round(b.height), m.classList.contains('gold-mark--metal')]; })])),
    gold: off('.gold-mark'), star: off('.star-mark'), fill: flat ? getComputedStyle(flat).fill : null };
})"""
BADGE_SIZES = [14, 16, 18, 20, 24, 34, 56]
GOLD = {"dark": "rgb(192, 133, 16)", "light": "rgb(176, 122, 14)"}  # appearance.GOLD


def badges(page: Page, base: str, tag: str, shots: Path) -> None:
    """«الهوية» → «الشارات» (the panel is open)."""
    page.get_by_role("tab", name="الهوية").click()
    expect(page.locator("#brand-badges")).to_be_visible(timeout=15000)
    found = page.evaluate(BADGES_JS)
    problem_if([p["tone"] for p in found] != ["dark", "light", "wall-dark", "wall-light"], f"{tag} badges: panels {found}")
    for p in found:
        where = f"{tag} badges {p['tone']}"
        problem_if(p["rows"].get("star") != [[s, s, False] for s in BADGE_SIZES], f"{where}: star sizes {p['rows'].get('star')}")
        problem_if(p["rows"].get("gold") != [[s, s, s >= 28] for s in BADGE_SIZES], f"{where}: gold sizes {p['rows'].get('gold')}")
        problem_if(p["gold"] is None or p["gold"] > 1 or p["star"] is None or p["star"] > 1,
                   f"{where}: the badges are not centred on the name line (gold {p['gold']}, star {p['star']})")
        mode = "dark" if p["tone"].endswith("dark") else "light"
        problem_if(p["fill"] != GOLD[mode], f"{where}: the flat gold is {p['fill']}, not {GOLD[mode]}")
    no_scroll(page, f"{tag} badges")
    page.locator("#brand-badges").screenshot(path=str(shots / f"badges-{tag}.png"))
    # the tap: what a user sees
    page.locator("#brand-badges .brand-badges__names .gold-mark").first.click()
    sheet = page.locator(".official-sheet")
    expect(sheet).to_be_visible()
    problem_if("الحساب الرسمي لـ DALTA.BIT" not in sheet.inner_text(), f"{tag} badges: the tap sheet says {sheet.inner_text()!r}")
    problem_if(sheet.locator(".gold-mark--metal").count() != 1, f"{tag} badges: no large badge in the tap sheet")
    page.screenshot(path=str(shots / f"badges-{tag}-sheet.png"))
    sheet.get_by_role("button", name="إغلاق").click()
    expect(sheet).to_have_count(0)
    page.set_viewport_size({"width": 1180, "height": 1500})  # tall enough that the sticky tabs never cover it
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(200)
    page.locator("#brand-badges").screenshot(path=str(shots / f"badges-{tag}-wide.png"))
    page.set_viewport_size(ae.MOBILE["viewport"])


def no_scroll(page: Page, where: str) -> None:
    sw, w = page.evaluate("[document.documentElement.scrollWidth, window.innerWidth]")
    problem_if(sw > w, f"{where}: the page is {sw}px wide on a {w}px screen")


def scheme(browser, base: str, scheme_name: str, admin: str, shots: Path) -> None:
    tag = scheme_name
    # the boot screen: what shows before JavaScript (and if it never loads)
    ctx = browser.new_context(**ae.MOBILE, color_scheme=scheme_name, locale="ar", java_script_enabled=False)
    page = ctx.new_page()
    page.goto(base + "/")
    expect(page.locator(".boot .logo-mark")).to_be_visible()
    logos(page, f"{tag} boot", ".boot .logo-mark", size=96, small=False)
    red_dot(page, f"{tag} boot", ".boot .wordmark__dot")
    no_scroll(page, f"{tag} boot")
    page.screenshot(path=str(shots / f"{tag}-boot.png"))
    ctx.close()

    ctx = browser.new_context(**ae.MOBILE, color_scheme=scheme_name, locale="ar-DZ", timezone_id="Africa/Algiers")
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(base + "/")
    expect(page.locator(".auth__brand .logo-mark")).to_be_visible(timeout=15000)
    page.wait_for_timeout(300)
    logos(page, f"{tag} sign-in", ".auth__brand .logo-mark", size=88, small=False, pulse=True)
    red_dot(page, f"{tag} sign-in", ".auth__brand .wordmark__dot")
    no_scroll(page, f"{tag} sign-in")
    page.screenshot(path=str(shots / f"{tag}-sign-in.png"))
    page.get_by_role("tab", name="حساب جديد").click()
    logos(page, f"{tag} sign-up", ".auth__brand .logo-mark", size=88, small=False, pulse=True)
    no_scroll(page, f"{tag} sign-up")

    class R:  # what run_e2e.register needs
        pass

    r = R()
    r.base, r.shots = base, shots
    register(r, page, f"brand.{tag}.{int(time.time())}@example.com")
    expect(page.locator(".topbar .logo-mark")).to_be_visible()
    logos(page, f"{tag} home header", ".topbar .logo-mark", size=24, small=True, pulse=True)
    red_dot(page, f"{tag} home header", ".topbar .wordmark__dot")
    no_scroll(page, f"{tag} home")
    page.screenshot(path=str(shots / f"{tag}-home.png"))
    for path, kind in (("/icons/favicon.ico", "image/"), ("/icons/icon.svg", "image/svg"),
                       ("/icons/apple-touch-icon.png", "image/png"), ("/icons/og-image.png", "image/png"),
                       ("/icons/badge-96.png", "image/png")):
        resp = page.request.get(base + path)
        problem_if(resp.status != 200 or kind not in resp.headers.get("content-type", ""), f"{path}: {resp.status} {resp.headers.get('content-type')}")
    og = page.locator('meta[property="og:image"]').get_attribute("content") or ""
    problem_if(not og.endswith("/icons/og-image.png") or not og.startswith("http"), f"og:image is not an absolute URL: {og!r}")

    page.goto(base + "/download")
    red_dot(page, f"{tag} /download", ".dl-name .wordmark__dot")
    problem_if(page.evaluate("document.querySelector('.dl-icon').naturalWidth") != 192, f"{tag} /download: the icon did not load")
    no_scroll(page, f"{tag} /download")
    page.screenshot(path=str(shots / f"{tag}-download.png"))
    page.goto(base + "/policies/privacy")
    problem_if(page.evaluate("document.querySelector('.dl-icon').naturalWidth") != 192, f"{tag} policies: the icon did not load")
    no_scroll(page, f"{tag} policies")
    problem_if(bool(errors), f"{tag}: page errors {errors}")
    ctx.close()

    # the panel: its sign-in screen and header
    ctx = browser.new_context(**ae.MOBILE, color_scheme=scheme_name, locale="ar-DZ", timezone_id="Africa/Algiers")
    page = ctx.new_page()
    page.goto(base + ae.ADMIN_PATH)
    expect(page.locator(".admin-login .logo-mark")).to_be_visible(timeout=15000)
    logos(page, f"{tag} panel sign-in", ".admin-login .logo-mark", size=72, small=False, pulse=True)
    red_dot(page, f"{tag} panel sign-in", ".admin-login .wordmark__dot")
    no_scroll(page, f"{tag} panel sign-in")
    from app.security import totp

    page.fill("#admin-user", admin)
    page.fill("#admin-pass", ae.ADMIN_PASSWORD)
    page.fill("#admin-code", totp.code_at(ae.ADMIN_TOTP[admin], totp.current_step(time.time())))
    page.get_by_role("button", name="دخول").click()
    expect(page.locator(".admin-top__brand .logo-mark")).to_be_visible(timeout=15000)
    logos(page, f"{tag} panel header", ".admin-top__brand .logo-mark", size=24, small=True)
    red_dot(page, f"{tag} panel header", ".admin-top__brand .wordmark__dot")
    no_scroll(page, f"{tag} panel")
    page.screenshot(path=str(shots / f"{tag}-panel.png"))
    badges(page, base, tag, shots)
    ctx.close()


def reduced_motion(browser, base: str) -> None:
    ctx = browser.new_context(**ae.MOBILE, color_scheme="dark", locale="ar", reduced_motion="reduce")
    page = ctx.new_page()
    page.goto(base + "/")
    expect(page.locator(".auth__brand .logo-mark")).to_be_visible(timeout=15000)
    page.wait_for_timeout(300)
    logos(page, "reduced motion sign-in", ".auth__brand .logo-mark", pulse=False)
    ctx.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8791)
    ap.add_argument("--shots", default=str(ROOT / "e2e" / "screenshots-brand"))
    args = ap.parse_args()
    shots = Path(args.shots)
    shots.mkdir(parents=True, exist_ok=True)
    proc, base, db_url = ae.start_server(args.port)
    try:
        ae.seed(base, db_url)
        chromium = os.environ.get("PW_CHROMIUM", "/opt/pw-browsers/chromium-1194/chrome-linux/chrome")
        with sync_playwright() as p:
            browser = p.chromium.launch(**({"executable_path": chromium} if Path(chromium).exists() else {}))
            for name, admin in (("dark", "owner"), ("light", "light")):
                print(f"Scheme {name}", flush=True)
                scheme(browser, base, name, admin, shots)
            print("Reduced motion", flush=True)
            reduced_motion(browser, base)
            browser.close()
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    if PROBLEMS:
        print(f"\nBRAND E2E FAILED: {len(PROBLEMS)} problem(s)")
        for p_ in PROBLEMS:
            print("  -", p_)
        return 1
    print(f"\nBRAND E2E PASSED. Screenshots in {shots}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
