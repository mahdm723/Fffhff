"""V6 browser checks on a phone-sized screen, one section per phase.

* phase 3 — identity: a name is required at registration, 4-tab navigation, «المستخدمون» (list + search,
  tap → public profile → «مراسلة»), no way back to "dzplay". (Profile pictures: e2e/run_media_e2e.py.)
* phase 2 — market: «السوق | الأفكار» switch on Home (ideas by default, last pane remembered), gainers/losers with
  sparklines from a local fake Bybit, details sheet, stale notice when the source goes away, disclaimer.

    python e2e/run_v6_e2e.py [--shots DIR]
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_bybit import FakeBybit  # noqa: E402
from run_e2e import MOBILE, Run, register, start_server  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def pane_on_screen(page: Page) -> str:
    return page.evaluate("""() => {
      const w = innerWidth;
      for (const p of document.querySelectorAll('.home-pane')) {
        const r = p.getBoundingClientRect();
        if (Math.abs(r.left) < 2 && Math.abs(r.right - w) < 2) return p.id;
      }
      return '';
    }""")


def phase2_market(run: Run, page: Page, fake: FakeBybit) -> None:
    base = run.base
    page.goto(base + "/#/home")
    expect(page.locator("#idea-compose")).to_be_visible(timeout=10000)
    page.wait_for_timeout(400)
    assert pane_on_screen(page) == "pane-ideas", "ideas is the default pane"
    expect(page.get_by_role("tab", name="الأفكار")).to_have_attribute("aria-selected", "true")
    run.step("Home opens on «الأفكار» with the «السوق | الأفكار» switch at the top")

    page.get_by_role("tab", name="السوق").click()
    page.wait_for_timeout(700)
    assert pane_on_screen(page) == "pane-market"
    gainers = page.locator(".mk-section", has_text="الأعلى ربحًا").locator(".mk-row")
    losers = page.locator(".mk-section", has_text="الأعلى خسارة").locator(".mk-row")
    expect(gainers.first).to_be_visible(timeout=15000)
    assert gainers.count() == 6 and losers.count() == 6, (gainers.count(), losers.count())
    first = gainers.first.inner_text()
    assert "PEPE" in first and "+25.00%" in first, first
    assert "-12.00%" in losers.first.inner_text()
    names = page.locator(".mk-row__name b").all_inner_texts()
    assert "USDC" not in names and "BTC3L" not in names, names  # stablecoin + leveraged token filtered on the server
    assert page.locator(".mk-row .spark path").count() == 12
    expect(page.locator(".mk-disclaimer")).to_contain_text("ليست نصيحة استثمارية")
    expect(page.locator(".mk-updated")).to_contain_text("آخر تحديث")
    run.shot(page, "v6-market")
    run.step("market: 6 gainers + 6 losers with sparklines, stablecoins/leveraged tokens hidden, disclaimer shown")

    gainers.first.click()
    sheet = page.locator(".sheet")
    expect(sheet).to_contain_text("أعلى سعر 24 س")
    expect(sheet).to_contain_text("PEPEUSDT")
    run.shot(page, "v6-market-details")
    sheet.get_by_role("button", name="إغلاق").click()
    run.step("market: a coin opens its 24 h details")

    page.reload()
    expect(page.locator(".mk-row").first).to_be_visible(timeout=15000)
    page.wait_for_timeout(400)
    assert pane_on_screen(page) == "pane-market", "the last pane is remembered"
    # swipe back to ideas (RTL: ideas sits to the left of the market)
    box = page.locator(".home-pager").bounding_box()
    y = box["y"] + box["height"] / 2
    page.mouse.move(box["x"] + 60, y)
    page.evaluate("() => { const p = document.getElementById('pane-ideas'); p.scrollIntoView({ inline: 'start' }); }")
    page.wait_for_timeout(700)
    assert pane_on_screen(page) == "pane-ideas"
    expect(page.get_by_role("tab", name="الأفكار")).to_have_attribute("aria-selected", "true")
    run.step("market: last pane remembered after reload; swiping back selects «الأفكار»")

    # the source goes away: the last list stays, with a notice once it is older than 3 refresh periods
    page.get_by_role("tab", name="السوق").click()
    fake.stop()
    deadline = time.time() + 60
    while time.time() < deadline and not page.locator(".mk-banner:not([hidden])").count():
        page.get_by_role("button", name="تحديث الأسعار").click()
        page.wait_for_timeout(3000)
    expect(page.locator(".mk-banner")).to_contain_text("لم تُحدَّث")
    assert gainers.count() == 6
    run.shot(page, "v6-market-stale")
    run.step("market: Bybit unreachable → last list kept with a «not updated» notice")


def phase3_identity(run: Run, a: Page, b: Page, suffix) -> None:
    base = run.base
    b.goto(base + "/")
    b.get_by_role("tab", name="حساب جديد").click()
    b.locator("#email").fill(f"v6b{suffix}@example.com")
    b.locator("#password").fill("Str0ng-Pass!e2e")
    b.locator("#password_confirm").fill("Str0ng-Pass!e2e")
    b.locator(".gender-pick__opt", has_text="أنثى").click()
    b.locator("#age_confirmed").check()
    b.get_by_role("button", name="إنشاء الحساب").click()
    expect(b.locator(".form-error")).to_contain_text("اسم")
    b.locator("#display-name").fill("dzplay")
    b.locator(".antibot").click()
    expect(b.locator(".antibot")).to_have_attribute("data-state", "done", timeout=20000)
    b.get_by_role("button", name="إنشاء الحساب").click()
    expect(b.locator(".form-error")).to_contain_text("محجوز", timeout=10000)
    b.locator("#display-name").fill("Nour Trader")
    b.locator(".antibot").click()
    expect(b.locator(".antibot")).to_have_attribute("data-state", "done", timeout=20000)
    b.get_by_role("button", name="إنشاء الحساب").click()
    expect(b.locator("#idea-compose")).to_be_visible(timeout=15000)
    run.step("registration refuses an empty or reserved name, then accepts «Nour Trader»")

    tabs = [t.strip() for t in b.locator(".nav__btn").all_inner_texts()]
    assert tabs == ["الرئيسية", "المستخدمون", "الرسائل", "حسابي"], tabs
    run.step("bottom navigation: الرئيسية · المستخدمون · الرسائل · حسابي")

    b_id = b.evaluate("fetch('/api/me').then((r) => r.json()).then((j) => j.public_id)")
    a.locator('[data-tab="users"]').click()
    row = a.locator(".user-row", has_text=b_id)
    expect(row).to_be_visible(timeout=10000)
    expect(row).to_contain_text("Nour Trader")
    assert "@example.com" not in a.locator(".page").inner_text()
    run.shot(a, "v6-users")
    a.locator(".people-search input").fill("nour")
    a.locator(".people-search input").press("Enter")
    expect(a.locator(".person .person__id")).to_have_text(b_id, timeout=10000)
    row.click()
    expect(a.locator(".id-card__name")).to_contain_text("Nour Trader", timeout=10000)
    expect(a.get_by_role("button", name="مراسلة")).to_be_visible()
    expect(a.locator('[data-tab="users"]')).to_have_attribute("aria-current", "page")
    run.shot(a, "v6-user-profile")
    run.step("«المستخدمون»: listed by recent activity, search by name, tap → public profile with «مراسلة»")

    b.goto(base + "/#/profile")
    b.get_by_role("button", name="تعديل الملف").click()
    expect(b.get_by_role("button", name="العودة إلى الاسم dzplay")).to_have_count(0)
    b.locator("#display-name").fill("")
    b.locator(".sheet").get_by_role("button", name="حفظ").click()
    expect(b.locator(".sheet .form-error")).to_contain_text("اسم", timeout=10000)
    b.keyboard.press("Escape")
    run.step("the name cannot be emptied (no way back to dzplay)")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8773)
    parser.add_argument("--shots", default=str(ROOT / "e2e" / "screenshots-v6"))
    args = parser.parse_args()
    fake = FakeBybit(args.port + 100)
    proc, base = start_server(args.port, {"MARKET_BASE_URL": fake.url, "MARKET_REFRESH_SECONDS": "10",
                                          "MARKET_MIN_TURNOVER_24H": "1000"})
    run = Run(base, Path(args.shots))
    suffix = os.getpid()
    try:
        chromium = os.environ.get("PW_CHROMIUM", "")
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=chromium if os.path.exists(chromium) else None)
            ctx = browser.new_context(**MOBILE, locale="ar", color_scheme="dark")
            a = ctx.new_page()
            run.watch(a, "A")
            register(run, a, f"v6a{suffix}@example.com")
            b = browser.new_context(**MOBILE, locale="ar", color_scheme="light").new_page()
            run.watch(b, "B")
            phase3_identity(run, a, b, suffix)
            phase2_market(run, a, fake)
            browser.close()
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    noise = ("favicon", "Failed to load resource")
    errors = [e for e in run.errors if not any(n in e for n in noise)]
    if errors:
        print("\nBrowser errors:\n  " + "\n  ".join(errors))
        return 1
    print(f"\nV6 E2E PASSED. Screenshots in {run.shots}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
