"""V6 phase 13: the automatic payment check, in a phone browser (a fake TronGrid answers).

1. A user pays 40 USDT instead of 50: the panel shows «⚠️ ناقص 10.00 USDT» next to the request (never decided).
2. The owner presses «طلب استكمال الفارق»: the user (app in French) sees the exact missing amount, the same address
   and network, and adds the TXID of the difference.
3. The second transfer is checked and summed: the panel shows «✅ مطابق — وصل 50.00 USDT». Still pending until the
   owner presses «قبول»; then the user is a member.
Every screen fits a 390 px phone (the overflow checks of run_overflow_e2e).

    PW_CHROMIUM=… python e2e/run_payment_e2e.py
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import run_admin_e2e as ae  # noqa: E402
import run_overflow_e2e as ro  # noqa: E402
from fake_chain import FakeTronGrid  # noqa: E402
from run_e2e import Run  # noqa: E402

from app.services import chain  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PHONE = {"viewport": {"width": 390, "height": 844}, "device_scale_factor": 2, "is_mobile": True, "has_touch": True}
OURS_HEX = "1f" * 20
WALLET = chain.tron_from_hex20(OURS_HEX)
TX1, TX2 = "ab" * 32, "cd" * 32


def wait_for(fn, what: str, seconds: float = 20) -> None:
    end = time.time() + seconds
    while time.time() < end:
        if fn():
            return
        time.sleep(0.25)
    raise AssertionError(f"timed out waiting for {what}")


def check_status(db_url: str) -> str | None:
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session

    from app.models import MembershipRequest

    with Session(create_engine(db_url)) as db:
        return db.scalar(select(MembershipRequest.check_status).limit(1))


def panel_login(page: Page, base: str, name: str) -> None:
    from app.security import totp

    page.goto(base + ae.ADMIN_PATH)
    page.fill("#admin-user", name)
    page.fill("#admin-pass", ae.ADMIN_PASSWORD)
    page.fill("#admin-code", totp.code_at(ae.ADMIN_TOTP[name], totp.current_step(time.time())))
    page.get_by_role("button", name="دخول").click()
    expect(page.locator(".admin-tile__num").first).to_be_visible()


def main() -> int:
    fake = FakeTronGrid(8778)
    proc, base, db_url = ae.start_server(8777, CHAIN_CHECK_ENABLED="true", TRONGRID_URL=fake.url, CHAIN_TICK_SECONDS="0")
    run = Run(base, ROOT / "e2e" / "screenshots-payment")
    problems: list[str] = []
    try:
        ae.seed(base, db_url)
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session

        from app import clock
        from app.models import AppSetting

        with Session(create_engine(db_url)) as db:  # the payment wallet set in the panel
            for k, v in (("pay.currency", "USDT"), ("pay.network", "TRC20"), ("pay.wallet", WALLET)):
                db.add(AppSetting(key=k, value=v, updated_at=clock.utcnow()))
            db.commit()
        c = ae.register(base, "payer@example.com")
        assert c.put("/api/me/language", json={"lang": "fr"}).status_code == 200
        fake.add(TX1, 40, OURS_HEX)
        r = c.post("/api/membership/requests", json={"txid": TX1})
        assert r.status_code == 201, r.text
        rid = r.json()["request"]["id"]
        wait_for(lambda: check_status(db_url) == "under", "the first check")

        chromium = os.environ.get("PW_CHROMIUM", "")
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=chromium if os.path.exists(chromium) else None)
            actx = browser.new_context(**PHONE, locale="ar", color_scheme="dark")
            ap = actx.new_page()
            ap.set_default_timeout(15000)
            run.watch(ap, "panel")
            panel_login(ap, base, "owner")
            ap.locator("#tab-membership").click()
            card = ap.locator(".admin-card", has=ap.locator(".admin-paycheck"))
            expect(card.locator(".admin-paycheck__head")).to_contain_text("⚠️ ناقص 10.00 USDT — وصل 40.00 من 50.00")
            expect(card).to_contain_text("قيد المراجعة")  # shown, never decided
            run.shot(ap, "1-panel-under")
            card.screenshot(path=str(run.shots / "1b-card-under.png"))
            ro.check(ap, "panel membership (under)", admin=True)
            run.step("40 USDT of 50: the panel shows «⚠️ ناقص 10.00 USDT» next to the request, still pending")

            card.get_by_role("button", name="💬 طلب استكمال الفارق").click()
            ap.locator(".sheet").last.get_by_role("button", name="إرسال").click()
            expect(card).to_contain_text("طُلب الفارق من المستخدم")
            run.step("the owner asks for the difference from the panel")

            uctx = browser.new_context(**PHONE, locale="fr-FR", color_scheme="dark")
            uctx.add_cookies([{"name": k.name, "value": k.value, "domain": "127.0.0.1", "path": "/"} for k in c.cookies.jar])
            up = uctx.new_page()
            up.set_default_timeout(15000)
            run.watch(up, "user")
            up.goto(base + "/#/membership")
            box = up.locator(".mem-topup")
            expect(box).to_contain_text("Complétez le paiement de l’adhésion")
            expect(box).to_contain_text("10,00 USDT")
            expect(box.locator(".pay__wallet code")).to_have_text(WALLET)
            run.shot(up, "2-user-topup-fr")
            box.screenshot(path=str(run.shots / "2b-topup-box-fr.png"))
            ro.check(up, "membership top-up (fr)")
            fake.add(TX2, 10, OURS_HEX)
            box.locator("#mem-topup-txid").fill(TX2)
            box.get_by_role("button", name="Ajouter le TXID de la différence").click()
            expect(up.locator(".mem-txids li")).to_have_count(2)
            run.step("the user (French) sees 10,00 USDT, the same address and network, and adds the second TXID")

            wait_for(lambda: check_status(db_url) == "match", "the second check")
            ap.reload()
            ap.locator("#tab-membership").click()
            card = ap.locator(".admin-card", has=ap.locator(".admin-paycheck"))
            expect(card.locator(".admin-paycheck__head")).to_contain_text("✅ مطابق — وصل 50.00 USDT")
            expect(card.locator(".admin-paycheck__tx")).to_have_count(2)
            expect(card).to_contain_text("قيد المراجعة")
            run.shot(ap, "3-panel-match")
            card.screenshot(path=str(run.shots / "3b-card-match.png"))
            run.step("both transfers summed: «✅ مطابق — وصل 50.00 USDT», still waiting for the owner")

            card.get_by_role("button", name="قبول").click()
            ap.locator(".sheet").last.get_by_role("button", name="قبول").click()
            wait_for(lambda: c.get("/api/me").json().get("member") is True, "the membership")
            run.step("the owner accepts: the user is a member (the only way a request is decided)")
            browser.close()
        if ro.PROBLEMS:
            problems.extend(ro.PROBLEMS)
        assert rid
    except AssertionError as exc:
        problems.append(str(exc))
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        fake.stop()
    noise = ("favicon", "Failed to load resource")
    errors = [e for e in run.errors if not any(n in e for n in noise)]
    if problems or errors:
        print("\nPAYMENT E2E FAILED:\n  " + "\n  ".join(problems + errors))
        return 1
    print(f"\nPAYMENT E2E PASSED. Screenshots in {run.shots}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
