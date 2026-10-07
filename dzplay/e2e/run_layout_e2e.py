"""V6: layout checks on a phone-sized browser.

* the status-bar strip (`.status-scrim`) stays on top of every screen while content scrolls under it
  (the Android app reports the status bar height as --dz-safe-top; here it is injected as 24px);
* every chevron in the «حسابي» menu sits at the same place (the «الدعم والمساعدة» row used to be off).

    python e2e/run_layout_e2e.py [--shots DIR]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_e2e import MOBILE, Run, register, start_server  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SAFE_TOP = 24
INJECT = f"document.documentElement.style.setProperty('--dz-safe-top', '{SAFE_TOP}px')"


def scrim_ok(page) -> dict:
    return page.evaluate("""() => {
      const s = document.querySelector('.status-scrim');
      if (!s) return { ok: false };
      const r = s.getBoundingClientRect(), cs = getComputedStyle(s);
      return { ok: true, top: r.top, height: r.height, position: cs.position, z: Number(cs.zIndex),
               bg: cs.backgroundColor };
    }""")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8772)
    parser.add_argument("--shots", default=str(ROOT / "e2e" / "screenshots-layout"))
    args = parser.parse_args()
    proc, base = start_server(args.port)
    run = Run(base, Path(args.shots))
    suffix = os.getpid()
    try:
        chromium = os.environ.get("PW_CHROMIUM", "")
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=chromium if os.path.exists(chromium) else None)
            ctx = browser.new_context(**MOBILE, locale="ar", color_scheme="dark")
            a = ctx.new_page()
            run.watch(a, "A")
            register(run, a, f"layout{suffix}@example.com")

            for i in range(6):  # enough ideas to scroll
                a.locator("#idea-compose").fill(f"فكرة رقم {i} لتجربة التمرير تحت شريط الحالة")
                a.get_by_role("button", name="نشر", exact=True).click()
                a.wait_for_timeout(300)

            for route in ("#/home", "#/messages", "#/profile"):
                a.goto(base + "/" + route)
                a.evaluate(INJECT)  # what the Android app does with the real status bar height
                a.wait_for_timeout(600)
                a.mouse.wheel(0, 1500)
                a.evaluate("() => { window.scrollTo(0, 99999); document.querySelectorAll('.home-pane').forEach((p) => p.scrollTo(0, 99999)); }")
                a.wait_for_timeout(300)
                s = scrim_ok(a)
                assert s["ok"] and s["position"] == "fixed" and s["top"] == 0, (route, s)
                assert abs(s["height"] - SAFE_TOP) < 0.5 and s["z"] >= 40, (route, s)
                assert s["bg"] not in ("rgba(0, 0, 0, 0)", "transparent"), (route, s)
                run.shot(a, f"layout-scrolled-{route.strip('#/')}")
            run.step(f"status-bar strip ({SAFE_TOP}px, opaque, fixed) stays on top while home / messages / profile scroll")

            a.goto(base + "/#/profile")
            expect(a.locator(".menu")).to_be_visible(timeout=10000)
            centers = a.evaluate("""() => [...document.querySelectorAll('.menu__item')]
              .map((b) => ({ label: b.textContent.trim(), chev: b.querySelector('svg.chev') }))
              .filter((x) => x.chev)
              .map((x) => { const r = x.chev.getBoundingClientRect(); return { label: x.label, x: r.left + r.width / 2, w: r.width }; })
              .filter((c) => c.w > 0)""")  # hidden rows (e.g. 18+ already confirmed) have no box
            assert len(centers) >= 4, centers
            support = [c for c in centers if "الدعم" in c["label"]]
            assert support, centers
            xs = {round(c["x"]) for c in centers}
            assert max(xs) - min(xs) <= 1, centers
            run.shot(a, "layout-menu")
            run.step("every chevron in the «حسابي» menu is aligned (support row included)")
            browser.close()
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    noise = ("favicon", "Failed to load resource")
    errors = [e for e in run.errors if not any(n in e for n in noise)]
    if errors:
        print("\nBrowser errors:\n  " + "\n  ".join(errors))
        return 1
    print(f"\nLAYOUT E2E PASSED. Screenshots in {run.shots}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
