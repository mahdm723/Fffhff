"""V6 browser checks on a phone-sized screen, one section per phase.

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
