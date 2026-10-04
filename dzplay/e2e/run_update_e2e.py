"""V5 phase H: inside the Android app (user agent "DZPLAYApp/x.y.z"), an older version is told once that
the new APK is on /download, with the release notes; the current version is not bothered.

    python e2e/run_update_e2e.py [--shots DIR]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_e2e import MOBILE, Run, register, start_server  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
UA = ("Mozilla/5.0 (Linux; Android 14; Pixel 7 Build/UQ1A; wv) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Version/4.0 Chrome/130.0.0.0 Mobile Safari/537.36 DZPLAYApp/{v}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8774)
    parser.add_argument("--shots", default=str(ROOT / "e2e" / "screenshots-update"))
    args = parser.parse_args()
    meta = json.loads((ROOT / "static" / "download" / "version.json").read_text(encoding="utf-8"))
    latest = meta["version_name"]
    tmp = tempfile.mkdtemp(prefix="dz-update-e2e-")
    proc, base = start_server(args.port, {"DATABASE_URL": f"sqlite:///{tmp}/update.db"})
    run = Run(base, Path(args.shots))
    chromium = os.environ.get("PW_CHROMIUM", "/opt/pw-browsers/chromium-1194/chrome-linux/chrome")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=chromium if os.path.exists(chromium) else None)

            print(f"Old app (2.0.0) → update to {latest}")
            page = browser.new_context(**MOBILE, user_agent=UA.format(v="2.0.0"), locale="ar",
                                       color_scheme="dark").new_page()
            run.watch(page, "old")
            register(run, page, f"update{int(time.time())}@example.com")
            sheet = page.locator(".sheet", has_text="تحديث متوفر")
            expect(sheet).to_be_visible(timeout=10000)
            expect(sheet).to_contain_text(f"الإصدار {latest}")
            expect(sheet).to_contain_text("(لديك 2.0.0)")
            for note in meta["notes"][:5]:
                expect(sheet.locator(".update-notes li", has_text=note)).to_be_visible()
            assert "null" not in sheet.inner_text()
            expect(sheet.get_by_role("link", name="تحميل التحديث")).to_have_attribute("href", "/download")
            run.shot(page, "h01-update-available")
            run.step("2.0.0 sees «تحديث متوفر» with the notes and the /download link")
            sheet.get_by_role("button", name="لاحقًا").click()
            expect(sheet).to_be_hidden()
            page.reload()
            page.wait_for_timeout(2500)
            expect(page.locator(".sheet", has_text="تحديث متوفر")).to_have_count(0)
            run.step("shown once only")

            print("Current app → nothing")
            cur = browser.new_context(**MOBILE, user_agent=UA.format(v=latest), locale="ar",
                                      color_scheme="dark").new_page()
            run.watch(cur, "current")
            register(run, cur, f"current{int(time.time())}@example.com")
            cur.wait_for_timeout(2500)
            expect(cur.locator(".sheet", has_text="تحديث متوفر")).to_have_count(0)
            run.step(f"{latest} is not asked to update")

            print("/download")
            dl = browser.new_context(**MOBILE).new_page()
            dl.goto(base + "/download")
            expect(dl.get_by_text(latest).first).to_be_visible()
            r = dl.request.get(base + "/download/dzplay.apk")
            assert r.status == 200 and r.body()[:2] == b"PK", r.status
            run.shot(dl, "h02-download")
            run.step(f"/download shows {latest}; the APK is served")
            browser.close()
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    noise = ("favicon", "Failed to load resource")
    errors = [e for e in run.errors if not any(n in e for n in noise)]
    if errors:
        print("\nBrowser errors:\n  " + "\n  ".join(errors))
        return 1
    print(f"\nUPDATE E2E PASSED. Screenshots in {run.shots}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
