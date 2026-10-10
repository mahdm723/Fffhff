"""V6 browser checks on a phone-sized screen, one section per phase.

* phase 3 — identity: a name is required at registration, 4-tab navigation, «المستخدمون» (list + search,
  tap → public profile → «مراسلة»), no way back to "dzplay". (Profile pictures: e2e/run_media_e2e.py.)
* phase 4 — public comments + replies, notifications (bell), likers. (Members-only pictures: run_media_e2e.py.)
* phase 5b — invitation link → member → reward on hold; «أرباحي»; withdrawal with fee, e-mail code, password.
* phase 5c — red envelope: enter with a confirmed e-mail, draw, prize code by e-mail only.
* phase 2 — market: «السوق | الأفكار» switch on Home (ideas by default, last pane remembered), gainers/losers with
  sparklines from a local fake Bybit, details sheet, stale notice when the source goes away, disclaimer.

    python e2e/run_v6_e2e.py [--shots DIR]
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_bybit import FakeBybit  # noqa: E402
from run_account_e2e import ADMIN_PATH, admin_client  # noqa: E402
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


def phase4_comments(run: Run, a: Page, b: Page) -> None:
    """A posts; B likes + comments; A gets a notification, replies; B gets a reply notification; likers list."""
    base = run.base
    a.goto(base + "/#/home")
    a.get_by_role("tab", name="الأفكار").click()
    expect(a.locator("#idea-compose")).to_be_visible(timeout=10000)
    idea = "BTC يختبر مقاومة 70 ألف، ما رأيكم؟"
    a.locator("#idea-compose").fill(idea)
    a.get_by_role("button", name="نشر", exact=True).click()
    expect(a.locator(".post-card", has_text=idea)).to_be_visible(timeout=10000)

    b.goto(base + "/#/home")
    b.get_by_role("tab", name="الأفكار").click()
    b.get_by_role("button", name="أفكار أخرى").click()
    card = b.locator(".post-card", has_text=idea)
    expect(card).to_be_visible(timeout=10000)
    card.locator(".react--like").click()
    expect(card.locator(".post-card__likers")).to_have_text("أعجب شخصًا واحدًا")
    card.get_by_role("button", name="التعليقات").click()
    b.locator(".comments-sheet textarea").fill("أتوقع اختراقًا قريبًا")
    b.locator(".comments-sheet").get_by_role("button", name="إرسال التعليق").click()
    expect(b.locator(".comments-sheet .comment__body")).to_have_text("أتوقع اختراقًا قريبًا", timeout=10000)
    b.keyboard.press("Escape")
    run.step("B liked and commented publicly")

    expect(a.locator(".bell-btn .badge")).to_have_text("1", timeout=15000)
    a.locator(".bell-btn").click()
    expect(a.locator(".notif").first).to_contain_text("Nour Trader علّق على فكرتك")
    run.shot(a, "v6-notifications")
    a.locator(".notif").first.click()
    expect(a.locator(".comments-sheet .comment.is-focus")).to_be_visible(timeout=10000)
    a.locator(".comments-sheet").get_by_role("button", name="رد").click()
    a.locator(".comments-sheet textarea").fill("ممكن، لكن الحجم ضعيف")
    a.locator(".comments-sheet").get_by_role("button", name="إرسال التعليق").click()
    expect(a.locator(".comment-replies .comment__body")).to_contain_text("ممكن، لكن الحجم ضعيف")
    expect(a.locator(".comment-replies .comment__at")).to_contain_text("@Nour Trader")
    run.shot(a, "v6-comments-thread")
    a.keyboard.press("Escape")
    run.step("A: bell → notification → the comment highlighted → reply in the thread (@name)")

    expect(b.locator(".bell-btn .badge")).to_have_text("1", timeout=15000)
    b.locator(".bell-btn").click()
    expect(b.locator(".notif").first).to_contain_text("ردّ على تعليقك")
    b.goto(base + "/#/home")
    b.get_by_role("tab", name="الأفكار").click()
    b.get_by_role("button", name="أفكار أخرى").click()  # fresh counts (the feed is kept for a few minutes)
    card = b.locator(".post-card", has_text=idea)
    expect(card.locator(".react--comment")).to_contain_text("تعليقات (2)", timeout=10000)
    card.locator(".post-card__likers").click()
    expect(b.locator(".sheet .user-row")).to_have_count(1)
    expect(b.locator(".sheet .user-row")).to_contain_text("Nour Trader")
    run.shot(b, "v6-likers")
    b.keyboard.press("Escape")
    run.step("B: reply notification; public count (2); likers list (dislikers never listed)")


def phase5b_rewards(run: Run, browser, a: Page, adm, smtp, suffix) -> None:
    """Invitation link → the invitee becomes a member → the inviter's reward is on hold; a contest reward;
    a withdrawal with fee summary, e-mail code and password; the admin records the transfer."""
    import re as _re

    from run_e2e import PASSWORD

    base = run.base
    a.goto(base + "/#/referrals")
    link = a.locator(".ref-link").inner_text().strip()
    assert "/r/" in link, link
    run.shot(a, "v6-referrals")
    c = browser.new_context(**MOBILE, locale="ar").new_page()
    run.watch(c, "C")
    c.goto(link)
    register(run, c, f"v6c{suffix}@example.com", name="Invited Friend")
    assert adm.put("/api/admin/payment-settings", json={"currency": "USDT", "network": "TRC20",
                                                        "wallet": "TQ5pZ9aBcDeFgHiJkLmNoPqRsTuVwXyZ12"}).status_code == 200
    c.goto(base + "/#/membership")
    c.locator("#mem-txid").fill("e" * 64)
    c.get_by_role("button", name="إرسال رقم العملية").click()
    expect(c.get_by_text("طلبك: قيد المراجعة")).to_be_visible(timeout=10000)
    rid = adm.get("/api/admin/membership?status=pending").json()["requests"][0]["id"]
    assert adm.post(f"/api/admin/membership/requests/{rid}/decide", json={"action": "accept"}).status_code == 200
    run.step("an invited friend registered through the link and became a member")
    lst = adm.get("/api/admin/rewards").json()  # same network as the inviter here (127.0.0.1): admin review
    ref = lst["referrals"][0]
    assert ref["status"] == "review" and "same_network" in ref["flags"], ref
    assert adm.post(f"/api/admin/referrals/{ref['id']}/review", json={"action": "approve"}).status_code == 200
    uid = ref["referrer"]["ref"]
    run.step("a suspicious invitation (same network) waits for the admin, who approves it")

    a.goto(base + "/#/earnings")
    expect(a.locator(".earn-tile").nth(1)).to_contain_text("5.00", timeout=10000)  # on hold
    expect(a.locator(".earn-item").first).to_contain_text("مكافأة دعوة")
    assert adm.post(f"/api/admin/users/{uid}/rewards", json={"kind": "contest", "amount": 20, "reason": "فائز مسابقة الأسبوع"}).status_code == 200
    a.reload()
    expect(a.locator(".earn-tile--main")).to_contain_text("20.00", timeout=10000)
    run.shot(a, "v6-earnings")
    run.step("«أرباحي»: invitation reward on hold (5.00) + contest reward available (20.00)")

    a.get_by_role("button", name="سحب (الحد الأدنى 10.00 USDT)").click()
    a.locator("#wd-address").fill("TNPeeaaFB7K9cmo4uQpcU32zGK8G8NHxXz")
    a.locator("#wd-amount").fill("12")
    expect(a.locator(".wd-summary")).to_contain_text("11.00")
    a.get_by_role("button", name="أرسل الرمز").click()
    def code_mail():
        for m in reversed(smtp.messages):
            body = m["msg"].get_body(preferencelist=("plain",)).get_content()
            if "رمز التأكيد" in body:
                return body
        return None

    deadline = time.time() + 15
    while time.time() < deadline and not code_mail():
        time.sleep(0.3)
    text = code_mail()
    a.locator("#wd-code").fill(_re.search(r"\b(\d{6})\b", text).group(1))
    a.locator("#wd-password").fill(PASSWORD)
    run.shot(a, "v6-withdraw")
    a.get_by_role("button", name="تأكيد السحب").click()
    expect(a.get_by_text("طلب سحب قيد التنفيذ")).to_be_visible(timeout=10000)
    expect(a.locator(".earn-tile--main")).to_contain_text("8.00")
    wid = adm.get("/api/admin/withdrawals").json()["withdrawals"][0]["id"]
    assert adm.post(f"/api/admin/withdrawals/{wid}/decide", json={"action": "done", "txid": "f" * 64}).status_code == 200
    a.reload()
    expect(a.get_by_text("تم الإرسال")).to_be_visible(timeout=10000)
    run.step("withdrawal: fee shown before confirming, e-mail code + password, admin records the transfer")


def phase5c_giveaway(run: Run, a: Page, adm, smtp) -> None:
    """A round → A enters with the account e-mail (code) → the round ends → draw (fresh 2FA) → prize code by
    e-mail only, «ربحت» in the app, winners' names shown."""
    import re as _re
    from datetime import datetime, timedelta, timezone

    sys.path.insert(0, str(ROOT))
    from app.security import totp

    base = run.base
    ends = (datetime.now(timezone.utc) + timedelta(seconds=25)).isoformat()
    r = adm.post("/api/admin/giveaway", json={"title": "ظرف الأسبوع", "winners_count": 1, "ends_at": ends, "show_winners": True})
    assert r.status_code == 200, r.text
    rid = r.json()["id"]
    a.goto(base + "/#/giveaway")
    expect(a.get_by_text("ظرف الأسبوع")).to_be_visible(timeout=10000)
    n = len(smtp.messages)
    a.get_by_role("button", name="🧧 شارك الآن").click()
    deadline = time.time() + 15
    while time.time() < deadline and len(smtp.messages) <= n:
        time.sleep(0.3)
    body = smtp.messages[-1]["msg"].get_body(preferencelist=("plain",)).get_content()
    a.locator("#gw-code").fill(_re.search(r"\b(\d{6})\b", body).group(1))
    a.get_by_role("button", name="تأكيد البريد").click()
    expect(a.get_by_text("أنت مشارك ببريد")).to_be_visible(timeout=10000)
    run.shot(a, "v6-giveaway-entered")
    run.step("red envelope: entered with the account e-mail confirmed by code")

    time.sleep(max(0.0, (datetime.fromisoformat(ends) - datetime.now(timezone.utc)).total_seconds() + 1))
    step = totp.current_step(time.time())
    while totp.current_step(time.time()) == step:  # step-up codes are single-use: wait for a new 30 s step
        time.sleep(0.5)
    code = totp.code_at(adm.totp_secret, totp.current_step(time.time()))
    d = adm.post(f"/api/admin/giveaway/{rid}/draw", json={"code": code})
    assert d.status_code == 200, d.text
    assert d.json()["draw_log"]["entrants"] >= 1 and len(d.json()["winners"]) == 1
    assert adm.post(f"/api/admin/giveaway/{rid}/codes", json={"codes": ["RED-ENVELOPE-2026"]}).status_code == 200
    assert adm.post(f"/api/admin/giveaway/{rid}/send").status_code == 200
    deadline = time.time() + 15
    while time.time() < deadline and not any("RED-ENVELOPE-2026" in m["msg"].get_body(preferencelist=("plain",)).get_content() for m in smtp.messages):
        time.sleep(0.3)
    assert any("RED-ENVELOPE-2026" in m["msg"].get_body(preferencelist=("plain",)).get_content() for m in smtp.messages)
    a.reload()
    expect(a.get_by_text("ربحت! أُرسل رمز الجائزة إلى بريدك.")).to_be_visible(timeout=10000)
    assert "RED-ENVELOPE-2026" not in a.content()  # the prize code is never in the app
    run.shot(a, "v6-giveaway-won")
    run.step("draw after the end (fresh 2FA) → prize code by e-mail only; «ربحت» + winners' names in the app")


def phase7_contact(run: Run, a: Page, adm, smtp) -> None:
    """The support mailbox is set from the panel (fresh 2FA) → «تواصل معنا» shows its address → a new ticket is
    e-mailed from it to the support inbox with Reply-To = the user."""
    sys.path.insert(0, str(ROOT))
    from app.security import totp

    a.goto(run.base + "/#/profile")
    a.get_by_role("button", name="تواصل معنا").click()
    expect(a.get_by_role("button", name="فتح تذكرة دعم")).to_be_visible(timeout=10000)
    expect(a.locator(".contact__addr")).to_have_count(0)  # no official address yet
    step = totp.current_step(time.time())
    while totp.current_step(time.time()) == step:  # step-up codes are single-use: wait for a new 30 s step
        time.sleep(0.5)
    r = adm.put("/api/admin/smtp/support", json={
        "host": "127.0.0.1", "port": smtp.port, "security": "none", "username": "", "password": "e2e-app-pass",
        "sender": "DALTA.BIT — الدعم <support@dzplay.test>", "test_to": "",
        "code": totp.code_at(adm.totp_secret, totp.current_step(time.time()))})
    assert r.status_code == 200, r.text
    a.reload()
    expect(a.locator(".contact__addr")).to_have_text("support@dzplay.test", timeout=10000)
    run.shot(a, "v6-contact")
    n = len(smtp.messages)
    status = a.evaluate("""fetch('/api/support/tickets', {method: 'POST', headers: {'Content-Type': 'application/json',
      'X-DZ-Requested': '1'}, body: JSON.stringify({category: 'other', body: 'سؤال من صفحة تواصل معنا'})}).then((r) => r.status)""")
    assert status == 201, status
    deadline = time.time() + 15
    while time.time() < deadline and len(smtp.messages) <= n:
        time.sleep(0.3)
    m = smtp.messages[-1]
    assert m["to"] == ["support@dzplay.test"] and "support@dzplay.test" in m["msg"]["From"], (m["to"], m["msg"]["From"])
    assert m["msg"]["Reply-To"] == f"v6a{os.getpid()}@example.com", m["msg"]["Reply-To"]  # A's own address
    run.step("support mailbox set from the panel → «تواصل معنا» shows it → tickets are e-mailed from it (Reply-To = user)")


def phase8_content_appearance(run: Run, a: Page, adm) -> None:
    """The name comes from APP_NAME; texts edited in the panel show in the app; «المظهر» changes mode, accent and
    text size, saved on the account and applied before the first paint after a reload."""
    base = run.base
    a.goto(base + "/#/home")
    expect(a.locator(".topbar .wordmark")).to_contain_text("DALTA", timeout=10000)
    assert "DALTA.BIT" in a.title(), a.title()
    run.step("the app is called DALTA.BIT (wordmark DALTA●BIT, page title) — from APP_NAME")

    assert adm.put("/api/admin/content/announcement", json={"body": "صيانة قصيرة الليلة **2:00**"}).status_code == 200
    assert adm.put("/api/admin/content/membership_intro", json={"body": "**ميزات فقط** داخل التطبيق، بلا أي عائد."}).status_code == 200
    a.reload()
    banner = a.locator(".announce")
    expect(banner).to_contain_text("صيانة قصيرة الليلة 2:00", timeout=10000)
    run.shot(a, "v8-announcement")
    banner.get_by_role("button", name="إخفاء الإعلان").click()
    a.reload()
    expect(a.locator(".topbar .wordmark")).to_be_visible(timeout=10000)
    expect(a.locator(".announce")).to_have_count(0)
    expect(a.locator("#idea-compose")).to_be_visible(timeout=10000)
    assert "null" not in a.locator("#app").inner_text(), "a missing block was printed as «null»"
    a.goto(base + "/#/membership")
    expect(a.locator(".mem-intro strong")).to_have_text("ميزات فقط", timeout=10000)
    run.step("texts edited in «النصوص» show in the app (announcement once, membership intro)")

    def choose(mode: str, accent: str, font: str) -> None:
        a.goto(base + "/#/profile")
        a.get_by_role("button", name="المظهر").click()
        sheet = a.locator(".sheet")
        sheet.get_by_role("radio", name=mode).click()
        sheet.get_by_role("radio", name=accent).click()
        sheet.get_by_role("radio", name=font).click()
        sheet.get_by_role("button", name="حفظ").click()
        expect(a.locator(".sheet")).to_have_count(0, timeout=10000)

    choose("فاتح", "أزرق", "كبير")
    root = a.evaluate("() => ({...document.documentElement.dataset})")
    assert root.get("theme") == "light" and root.get("accent") == "ocean" and root.get("font") == "large", root
    a.reload()
    expect(a.locator(".id-card")).to_be_visible(timeout=10000)
    root = a.evaluate("() => ({...document.documentElement.dataset, accent_css: getComputedStyle(document.documentElement).getPropertyValue('--accent').trim()})")
    assert root.get("theme") == "light" and root.get("accent_css") == "#1a6dd0", root
    run.shot(a, "v8-light-ocean-large")
    a.goto(base + "/#/home")
    expect(a.locator("#idea-compose")).to_be_visible(timeout=10000)
    run.shot(a, "v8-home-light-ocean-large")
    choose("داكن", "نعناعي", "عادي")
    a.goto(base + "/#/home")
    expect(a.locator("#idea-compose")).to_be_visible(timeout=10000)
    run.shot(a, "v8-home-dark-mint")
    choose("حسب الجهاز", "جمري", "عادي")
    run.step("«المظهر»: light / dark, accent and large text — saved on the account, applied before paint after reload")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8773)
    parser.add_argument("--shots", default=str(ROOT / "e2e" / "screenshots-v6"))
    args = parser.parse_args()
    fake = FakeBybit(args.port + 100)
    sys.path.insert(0, str(ROOT))
    from tests.smtp_sink import SmtpSink

    smtp = SmtpSink().__enter__()
    db_file = Path(tempfile.mkdtemp(prefix="dz-v6-e2e-")) / "v6.db"
    db_url = f"sqlite:///{db_file}"
    proc, base = start_server(args.port, {"MARKET_BASE_URL": fake.url, "MARKET_REFRESH_SECONDS": "10",
                                          "MARKET_MIN_TURNOVER_24H": "1000", "DATABASE_URL": db_url, "ADMIN_PATH": ADMIN_PATH,
                                          "SMTP_HOST": "127.0.0.1", "SMTP_PORT": str(smtp.port), "SMTP_SECURITY": "none",
                                          "SMTP_FROM": "no-reply@dzplay.test", "NO_PROXY": "127.0.0.1,localhost",
                                          "no_proxy": "127.0.0.1,localhost"})
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
            phase4_comments(run, a, b)
            adm = admin_client(base, db_url)
            phase5b_rewards(run, browser, a, adm, smtp, suffix)
            phase5c_giveaway(run, a, adm, smtp)
            phase7_contact(run, a, adm, smtp)
            phase8_content_appearance(run, a, adm)
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
