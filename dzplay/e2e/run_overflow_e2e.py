"""V6 phase 10b: every screen fits a phone (360 and 390 px wide).

On every page of the app, its main sheets, the policy / download pages and every tab of the panel (with
their detail views):

1. the page is never wider than the screen (no horizontal scroll);
2. nothing sticks out past the screen edge (except inside a deliberately horizontal scroller);
3. no field or button is hidden: scrolled to the middle of the screen, it is the element actually there
   (not covered by the bottom bar, the status-bar strip, the «السوق | الأفكار» band or a stray sheet);
4. no clipped text (only deliberate "…" truncation) and no text box too short for its line / placeholder;
5. the status-bar strip (24 px, injected as the Android app does) is opaque and on top; headers start below
   it; on Home the band under it is opaque and what scrolls goes under it;
6. the blue star: 18×18 next to every name (profile, posts, comments, likers, users, search, conversations,
   chat), centred on the name, its seal and check exactly centred, in --star of the theme — dark and light,
   every accent.

All problems are listed at the end (one run shows everything); any problem fails the test.

    python e2e/run_overflow_e2e.py [--shots DIR]
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
import run_admin_e2e as ae  # noqa: E402  (server with ADMIN_PATH, seeded panel data, admin accounts)
from run_e2e import Run  # noqa: E402

from app.services.appearance import ACCENTS, STAR  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
WIDTHS = (360, 390)
SAFE_TOP = 24
LONG_WORD = "BTCUSDT" * 20  # one unbroken 140-character word
NAME_A = "عبد الرحمن المصطفاوي"  # 20 characters: the longest name allowed
NAME_B = "Maximilian Alexander"
PROBLEMS: list[str] = []

INJECT_SAFE_TOP = f"document.documentElement.style.setProperty('--dz-safe-top', '{SAFE_TOP}px')"

CHECK_JS = r"""(opts) => {
  const W = document.documentElement.clientWidth;
  const problems = [];
  const scope = opts.scope ? document.querySelector(opts.scope) : document.body;
  if (!scope) return { problems: [`${opts.scope} is not on the page`], stars: 0 };
  const desc = (el) => {
    const cls = typeof el.className === 'string' && el.className.trim() ? '.' + el.className.trim().split(/\s+/).slice(0, 3).join('.') : '';
    const label = (el.getAttribute && (el.getAttribute('aria-label') || el.getAttribute('placeholder'))) || (el.textContent || '').trim().slice(0, 24);
    return `${el.tagName.toLowerCase()}${el.id ? '#' + el.id : ''}${cls}${label ? ` «${label}»` : ''}`;
  };
  const shown = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return false;
    // inside a closed <details>, or hidden by an ancestor: not on screen by design
    if (el.checkVisibility && !el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true, contentVisibilityAuto: true })) return false;
    const cs = getComputedStyle(el);
    return cs.visibility !== 'hidden' && Number(cs.opacity) > 0.05;
  };
  const inScroller = (el) => {
    for (let p = el.parentElement; p && p !== document.body && p !== document.documentElement; p = p.parentElement) {
      if (getComputedStyle(p).overflowX !== 'visible') return true;
    }
    return false;
  };
  const offPane = (el) => { const pane = el.closest('.home-pane'); return !!pane && Math.abs(pane.getBoundingClientRect().left) > 2; };
  const toasts = document.querySelector('.toasts');
  if (toasts) toasts.style.display = 'none';

  // 1. the page width
  const sw = document.documentElement.scrollWidth;
  if (sw > W + 0.5) problems.push(`the page is ${sw}px wide on a ${W}px screen`);

  // 2. past the edge, 4. clipped text / text boxes too short
  for (const el of scope.querySelectorAll('*')) {
    if (el instanceof SVGElement && el.tagName.toLowerCase() !== 'svg') continue;
    if (offPane(el) || el.closest('.sr-only, #star-sizes') || !shown(el)) continue;
    const r = el.getBoundingClientRect();
    if ((r.right > W + 1 || r.left < -1) && !inScroller(el)) problems.push(`${desc(el)} goes past the screen edge [${Math.round(r.left)} … ${Math.round(r.right)}]`);
    const cs = getComputedStyle(el);
    const ownText = [...el.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim());
    if (ownText && (cs.overflowX === 'hidden' || cs.overflowX === 'clip') && el.scrollWidth > el.clientWidth + 1 && cs.textOverflow !== 'ellipsis') {
      problems.push(`${desc(el)}: text cut off (${el.scrollWidth}px in ${el.clientWidth}px)`);
    }
    if (el.matches('textarea, input:not([type=checkbox]):not([type=radio]):not([type=range]):not([type=file]):not([type=hidden]):not(.hp)')) {
      const lh = parseFloat(cs.lineHeight) || parseFloat(cs.fontSize) * 1.25;
      const need = lh + parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom);
      if (el.clientHeight + 1 < need) problems.push(`${desc(el)} is ${el.clientHeight}px tall: its line / placeholder needs ${Math.round(need)}px`);
      if (el.tagName === 'TEXTAREA' && cs.resize !== 'none' && !opts.admin) problems.push(`${desc(el)} shows a resize handle`);
    }
  }

  // 3. every field / button is reachable: scrolled to the middle, it is the element actually there
  const lifted = [];
  for (const el of document.querySelectorAll('body *')) {  // overlays that let clicks through still hide things
    if (getComputedStyle(el).pointerEvents === 'none') { lifted.push([el, el.style.pointerEvents]); el.style.pointerEvents = 'auto'; }
  }
  const controls = [...scope.querySelectorAll('button, input, textarea, select, a.btn, [role=button], [role=tab]')]
    .filter((el) => !el.closest('.hp, .sr-only') && !offPane(el) && shown(el) && el.getBoundingClientRect().width >= 4);
  for (const el of controls) {
    el.scrollIntoView({ block: 'center', inline: 'nearest' });
    const r = el.getBoundingClientRect();
    const x = Math.min(Math.max(r.left + r.width / 2, 1), W - 1);
    const y = r.top + Math.min(r.height / 2, 18);
    const hit = document.elementFromPoint(x, y);
    if (!hit || !(hit === el || el.contains(hit) || hit.contains(el))) {
      problems.push(`${desc(el)} is hidden under ${hit ? desc(hit) : 'nothing (off screen)'}`);
    }
  }
  for (const [el, v] of lifted) el.style.pointerEvents = v;
  window.scrollTo(0, 0);
  document.querySelectorAll('.home-pane, .chat__body, .sheet').forEach((p) => { if (!p.classList.contains('chat__body')) p.scrollTop = 0; });

  // 6. the blue star
  const probe = document.createElement('span');
  probe.style.color = 'var(--star)';
  document.body.append(probe);
  const starColour = getComputedStyle(probe).color;
  probe.remove();
  const stars = [...scope.querySelectorAll('.star-mark')].filter((s) => !s.closest('#star-sizes') && shown(s));
  for (const s of stars) {
    const where = desc(s.closest('.name-line') ? s.closest('.name-line').parentElement : s.parentElement);
    const r = s.getBoundingClientRect();
    const large = s.classList.contains('star-mark--lg');
    if (!large && (Math.abs(r.width - 18) > 0.5 || Math.abs(r.height - 18) > 0.5)) problems.push(`star ${r.width.toFixed(1)}×${r.height.toFixed(1)} (not 18×18) in ${where}`);
    const text = s.parentElement.querySelector('.name-line__text');
    if (!large && text) {
      const t = text.getBoundingClientRect();
      const d = (r.top + r.height / 2) - (t.top + t.height / 2);
      if (Math.abs(d) > 1) problems.push(`star ${d.toFixed(1)}px off the centre of the name in ${where}`);
    }
    for (const [part, sel] of [['seal', '.star-mark__seal'], ['check', '.star-mark__check']]) {
      const b = s.querySelector(sel).getBBox();
      const cx = b.x + b.width / 2, cy = b.y + b.height / 2;
      if (Math.abs(cx - 12) > 0.01 || Math.abs(cy - 12) > 0.01) problems.push(`star ${part} centred at (${cx.toFixed(3)}, ${cy.toFixed(3)}), not (12, 12)`);
    }
    const fill = getComputedStyle(s.querySelector('.star-mark__seal')).fill;
    if (fill !== starColour) problems.push(`star colour ${fill} instead of --star ${starColour} in ${where}`);
  }
  if (toasts) toasts.style.display = '';
  return { problems, stars: stars.length };
}"""

TOP_JS = r"""() => {
  const alpha = (c) => { const m = c.match(/\/\s*([\d.]+)\s*\)$/) || c.match(/rgba\([^)]*,\s*([\d.]+)\)$/); return m ? Number(m[1]) : (c === 'transparent' ? 0 : 1); };
  const s = document.querySelector('.status-scrim');
  const scrim = s ? { top: s.getBoundingClientRect().top, height: s.getBoundingClientRect().height, z: Number(getComputedStyle(s).zIndex),
                      alpha: alpha(getComputedStyle(s).backgroundColor), fixed: getComputedStyle(s).position === 'fixed' } : null;
  const head = document.querySelector('.home-switch__inner, .page .topbar, .chat__head > *, .admin-top');
  return { scrim, headTop: head ? head.getBoundingClientRect().top : null, head: head ? head.className : null };
}"""

HOME_BAND_JS = r"""() => {
  const alpha = (c) => { const m = c.match(/\/\s*([\d.]+)\s*\)$/) || c.match(/rgba\([^)]*,\s*([\d.]+)\)$/); return m ? Number(m[1]) : (c === 'transparent' ? 0 : 1); };
  const band = document.querySelector('.home-switch');
  const pane = document.querySelector('.home-pane--ideas');
  const composer = pane.querySelector('.idea-composer');
  const top = pane.querySelector('.topbar');
  const r = band.getBoundingClientRect();
  const out = { bandTop: r.top, bandBottom: r.bottom, alpha: alpha(getComputedStyle(band).backgroundColor),
                restTop: top.getBoundingClientRect().top, composerTop: composer.getBoundingClientRect().top };
  pane.scrollTop = 260;
  const before = band.style.pointerEvents;
  band.style.pointerEvents = 'auto';
  const hit = document.elementFromPoint(document.documentElement.clientWidth - 12, r.bottom - 4);
  out.coveredByBand = !!hit && band.contains(hit);
  out.composerUnder = composer.getBoundingClientRect().top < r.bottom;
  band.style.pointerEvents = before;
  pane.scrollTop = 0;
  return out;
}"""


def check(page: Page, where: str, scope: str | None = None, admin: bool = False) -> int:
    res = page.evaluate(CHECK_JS, {"scope": scope, "admin": admin})
    PROBLEMS.extend(f"{where}: {p}" for p in res["problems"])
    return res["stars"]


def problem_if(cond: bool, text: str) -> None:
    if cond:
        PROBLEMS.append(text)


def check_top(page: Page, where: str, min_head: float = SAFE_TOP) -> None:
    t = page.evaluate(TOP_JS)
    s = t["scrim"]
    problem_if(not s or not s["fixed"] or s["top"] != 0 or abs(s["height"] - SAFE_TOP) > 0.5 or s["z"] < 40 or s["alpha"] < 0.9,
               f"{where}: status-bar strip wrong: {s}")
    problem_if(t["headTop"] is not None and t["headTop"] < min_head - 0.5,
               f"{where}: the header ({t['head']}) starts at {t['headTop']:.0f}px, under the status bar")


# ----------------------------------------------------------------------------- data


def make_user(base: str, email: str, name: str):
    c = ae.api_client(base)
    r = c.post("/api/auth/register", json={"email": email, "password": ae.PASSWORD, "password_confirm": ae.PASSWORD,
                                           "gender": "male", "age_confirmed": True, "display_name": name,
                                           "antibot": ae.challenge(c, "register")})
    assert r.status_code == 201, r.text
    return c


def seed_long(base: str, db_url: str) -> dict:
    a = make_user(base, "fit-a@example.com", NAME_A)
    b = make_user(base, "fit-b@example.com", NAME_B)
    pid = a.post("/api/posts", json={"content": "تحليل اليوم للسوق مع تفاصيل كثيرة " * 12 + LONG_WORD}).json()["id"]
    assert b.put(f"/api/posts/{pid}/reaction", json={"reaction": "like"}).status_code == 200
    assert b.post(f"/api/posts/{pid}/comments", json={"content": "تعليق بكلمة طويلة " + LONG_WORD}).status_code == 201
    a_pub, b_pub = a.get("/api/me").json()["public_id"], b.get("/api/me").json()["public_id"]
    conv = b.post(f"/api/people/{a_pub}/messages", json={"content": "رسالة " + LONG_WORD}).json()["conversation"]["id"]
    assert a.post(f"/api/conversations/{conv}/messages", json={"content": "رد " + LONG_WORD}).status_code == 201
    long_subject = "عنوان طويل جدا لتجربة عرض صفحة الدعم على شاشة هاتف صغيرة بعرض ثلاثمئة وستين"
    assert a.post("/api/support/tickets", json={"category": "verification", "subject": long_subject, "body": "تفاصيل المشكلة " * 10}).status_code in (200, 201)
    assert a.post("/api/support/tickets", json={"category": "other", "body": LONG_WORD + " " + "شرح " * 30}).status_code in (200, 201)
    tid = a.get("/api/support/tickets").json()["tickets"][-1]["id"]

    from sqlalchemy import create_engine, update
    from sqlalchemy.orm import Session

    from app import clock
    from app.models import User

    with Session(create_engine(db_url)) as db:  # both are members: the blue star shows next to their names
        db.execute(update(User).where(User.email.in_(["fit-a@example.com", "fit-b@example.com"]))
                   .values(verified_at=clock.utcnow(), member_since=clock.utcnow()))
        db.commit()
    return {"a": a, "pid": pid, "conv": conv, "tid": tid, "b_pub": b_pub}


# ----------------------------------------------------------------------------- app pages


def user_pages(run: Run, browser, base: str, data: dict, width: int) -> None:
    ctx = browser.new_context(viewport={"width": width, "height": 780}, device_scale_factor=2, is_mobile=True,
                              has_touch=True, locale="ar", color_scheme="dark")
    ctx.add_cookies([{"name": c.name, "value": c.value, "domain": "127.0.0.1", "path": "/"} for c in data["a"].cookies.jar])
    page = ctx.new_page()
    page.set_default_timeout(15000)
    run.watch(page, f"app-{width}")
    stars: dict[str, int] = {}

    def visit(route: str, ready: str) -> None:
        page.goto(f"{base}/{route}")
        page.evaluate(INJECT_SAFE_TOP)
        expect(page.locator(ready).first).to_be_visible()
        page.wait_for_timeout(500)

    pid, conv, tid = data["pid"], data["conv"], data["tid"]
    pages = [
        ("#/messages", ".conv-item", "conversations"), (f"#/chat/{conv}", ".chat__composer textarea", "chat"),
        ("#/users", ".user-row", "users"), ("#/profile", ".id-card__name", "profile"),
        (f"#/id/{data['b_pub']}", ".id-card__name", None),
        ("#/notifications", ".page", None), ("#/support", ".ticket-item", None), (f"#/support/{tid}", ".ticket-msg", None),
        ("#/contact", ".page", None), ("#/membership", ".page", None), ("#/earnings", ".page", None),
        ("#/referrals", ".page", None), ("#/giveaway", ".page", None),
    ]
    # Home: both panes, the band under the status bar
    visit("#/home", "#idea-compose")
    band = page.evaluate(HOME_BAND_JS)
    problem_if(band["bandTop"] != 0 or band["alpha"] < 0.9, f"{width} home: the band is not opaque at the top: {band}")
    problem_if(band["restTop"] < band["bandBottom"] - 0.5 or band["composerTop"] < band["bandBottom"],
               f"{width} home: at rest the page starts under the band: {band}")
    problem_if(not band["coveredByBand"] or not band["composerUnder"], f"{width} home: scrolled text shows through the band: {band}")
    check_top(page, f"{width} home")
    check(page, f"{width} home (ideas)")
    page.locator(".home-switch__tab[data-pane=market]").click()
    page.wait_for_timeout(700)
    check(page, f"{width} home (market)")
    page.locator(".home-switch__tab[data-pane=ideas]").click()
    page.wait_for_timeout(500)
    run.shot(page, f"{width}-home")

    for route, ready, place in pages:
        visit(route, ready)
        check_top(page, f"{width} {route}")
        n = check(page, f"{width} {route}")
        if place:
            stars[place] = stars.get(place, 0) + n
        name = route.strip("#/").split("/")
        run.shot(page, f"{width}-{name[0]}{'-item' if len(name) > 1 else ''}")

    # one idea (from a notification): its comments open in a sheet; then the page itself; the likers
    visit(f"#/post/{pid}", ".comments-sheet")
    page.wait_for_timeout(600)
    stars["comments"] = check(page, f"{width} comments sheet", ".sheet")
    run.shot(page, f"{width}-sheet-comments")
    page.keyboard.press("Escape")
    expect(page.locator(".sheet-backdrop")).to_have_count(0)
    check_top(page, f"{width} #/post")
    stars["post"] = check(page, f"{width} #/post")
    run.shot(page, f"{width}-post")
    page.locator(".post-card__likers").first.click()
    expect(page.locator(".sheet .likers .user-row .star-mark").first).to_be_visible()
    page.wait_for_timeout(400)  # the sheet's entry animation
    stars["likers"] = check(page, f"{width} likers sheet", ".sheet")
    page.keyboard.press("Escape")

    visit("#/users", ".user-row")
    page.locator(".people-search input").fill("Maximilian")
    page.locator(".people-search input").press("Enter")
    expect(page.locator(".people .star-mark").first).to_be_visible()
    stars["search"] = check(page, f"{width} people search", ".people")
    run.shot(page, f"{width}-search")

    visit("#/profile", ".id-card__name")
    page.get_by_role("button", name="تعديل الملف").click()
    expect(page.locator(".sheet")).to_be_visible()
    check(page, f"{width} edit-profile sheet", ".sheet")
    page.keyboard.press("Escape")
    page.get_by_role("button", name="المظهر").click()
    expect(page.locator(".sheet")).to_be_visible()
    check(page, f"{width} appearance sheet", ".sheet")
    page.keyboard.press("Escape")

    # leaving a page closes its sheet (it used to stay over the next page)
    visit(f"#/post/{pid}", ".comments-sheet")
    page.evaluate("() => { location.hash = '#/support'; }")
    expect(page.locator(".ticket-item").first).to_be_visible()
    problem_if(page.locator(".sheet-backdrop").count() != 0, f"{width}: the comments sheet stayed open over «الدعم»")

    for place in ("profile", "post", "comments", "likers", "users", "search", "conversations", "chat"):
        problem_if(not stars.get(place), f"{width}: no blue star shown in {place}")
    if width == WIDTHS[0]:
        star_themes(run, page)
    ctx.close()
    run.step(f"{width}px: home (both panes), 14 pages and 5 sheets fit; stars in {', '.join(sorted(stars))}")


def star_themes(run: Run, page: Page) -> None:
    """The star in both themes and every accent (the colour is --star of the theme, never the accent)."""
    page.goto(page.url.split("#")[0] + "#/users")
    expect(page.locator(".user-row .star-mark").first).to_be_visible()
    hexes = {mode: tuple(int(STAR[mode][i:i + 2], 16) for i in (1, 3, 5)) for mode in STAR}
    for mode in ("dark", "light"):
        for accent in ACCENTS:
            page.evaluate("([m, a]) => { document.documentElement.dataset.theme = m; document.documentElement.dataset.accent = a; }", [mode, accent])
            check(page, f"star {mode}/{accent}", ".page")
            fill = page.evaluate("() => getComputedStyle(document.querySelector('.user-row .star-mark__seal')).fill")
            want = "rgb({}, {}, {})".format(*hexes[mode])
            problem_if(fill != want, f"star {mode}/{accent}: {fill} instead of {want}")
        # a close-up of the star at 16 / 18 / 24 / 56 px for the eye
        page.evaluate("""() => {
          const old = document.getElementById('star-sizes'); if (old) old.remove();
          const box = document.createElement('div'); box.id = 'star-sizes';
          Object.assign(box.style, { display: 'flex', gap: '18px', alignItems: 'center', padding: '16px', background: 'var(--surface)' });
          for (const px of [16, 18, 24, 56]) {
            const s = document.querySelector('.user-row .star-mark').cloneNode(true);
            s.style.width = px + 'px'; s.style.height = px + 'px';
            box.append(s);
          }
          document.querySelector('.page').prepend(box);
        }""")
        page.wait_for_timeout(700)  # the page's fade-in animation
        page.locator("#star-sizes").screenshot(path=str(run.shots / f"star-sizes-{mode}.png"))
        page.evaluate("() => document.getElementById('star-sizes').remove()")
        page.locator(".user-row").first.screenshot(path=str(run.shots / f"star-row-{mode}.png"))
    page.evaluate("() => { delete document.documentElement.dataset.theme; document.documentElement.dataset.accent = 'ember'; }")
    run.step(f"blue star: same colour in all {len(ACCENTS)} accents, dark {STAR['dark']} / light {STAR['light']}")


def public_pages(run: Run, browser, base: str, width: int) -> None:
    ctx = browser.new_context(viewport={"width": width, "height": 780}, device_scale_factor=2, is_mobile=True,
                              has_touch=True, locale="ar", color_scheme="light")
    page = ctx.new_page()
    run.watch(page, f"public-{width}")
    page.goto(base + "/")
    expect(page.locator(".auth")).to_be_visible(timeout=15000)
    check(page, f"{width} sign-in")
    page.get_by_role("tab", name="حساب جديد").click()
    check(page, f"{width} sign-up")
    run.shot(page, f"{width}-signup")
    for path in ("/policies/privacy", "/policies/terms", "/policies/membership_terms", "/download"):
        page.goto(base + path)
        page.wait_for_timeout(300)
        check(page, f"{width} {path}")
    ctx.close()


# ----------------------------------------------------------------------------- panel

PANEL_TABS = ("overview", "users", "content", "engage", "reports", "media", "membership", "rewards", "giveaway",
              "texts", "support", "settings", "system", "logs")
DETAIL = {"users": ".admin-card--tap", "content": ".admin-card--tap", "support": ".admin-line--btn", "texts": ".admin-line--btn"}


def panel_pages(run: Run, browser, base: str, width: int, admin: str) -> None:
    from app.security import totp

    ctx = browser.new_context(viewport={"width": width, "height": 780}, device_scale_factor=2, is_mobile=True,
                              has_touch=True, locale="ar", color_scheme="dark")
    page = ctx.new_page()
    page.set_default_timeout(15000)
    run.watch(page, f"panel-{width}")
    page.goto(base + ae.ADMIN_PATH)
    page.evaluate(INJECT_SAFE_TOP)
    check(page, f"{width} panel sign-in", admin=True)
    page.fill("#admin-user", admin)
    page.fill("#admin-pass", ae.ADMIN_PASSWORD)
    page.fill("#admin-code", totp.code_at(ae.ADMIN_TOTP[admin], totp.current_step(time.time())))
    page.get_by_role("button", name="دخول").click()
    expect(page.locator(".admin-tile__num").first).to_be_visible()
    for tab in PANEL_TABS:
        page.locator(f"#tab-{tab}").click()
        page.wait_for_timeout(900)
        check(page, f"{width} panel «{tab}»", admin=True)
        sel = DETAIL.get(tab)
        if sel and page.locator(sel).count():
            page.locator(sel).first.click()
            page.wait_for_timeout(900)
            check(page, f"{width} panel «{tab}» detail", ".sheet" if page.locator(".sheet").count() else None, admin=True)
            run.shot(page, f"{width}-panel-{tab}-detail")
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
    ctx.close()
    run.step(f"{width}px: panel sign-in, {len(PANEL_TABS)} tabs and {len(DETAIL)} detail views fit")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8774)
    parser.add_argument("--shots", default=str(ROOT / "e2e" / "screenshots-overflow"))
    args = parser.parse_args()
    proc, base, db_url = ae.start_server(args.port)
    run = Run(base, Path(args.shots))
    try:
        ae.seed(base, db_url)  # panel data + admin accounts
        data = seed_long(base, db_url)
        chromium = os.environ.get("PW_CHROMIUM", "")
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=chromium if os.path.exists(chromium) else None)
            for width, admin in zip(WIDTHS, ("owner", "light")):
                print(f"Width {width}px")
                user_pages(run, browser, base, data, width)
                public_pages(run, browser, base, width)
                panel_pages(run, browser, base, width, admin)
            browser.close()
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    noise = ("favicon", "Failed to load resource")
    errors = [e for e in run.errors if not any(n in e for n in noise)]
    if PROBLEMS or errors:
        print(f"\n{len(PROBLEMS)} layout problem(s):\n  " + "\n  ".join(PROBLEMS))
        if errors:
            print("\nBrowser errors:\n  " + "\n  ".join(errors))
        return 1
    print(f"\nOVERFLOW E2E PASSED. Screenshots in {run.shots}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
