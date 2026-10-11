"""V6 phase 8: the content system — defaults that follow the settings, editing with history and restore, safe
Markdown (no XSS), «major change» re-consent with a fresh 2FA code, e-mail texts, and permissions."""

from __future__ import annotations

import pytest

from app import clock
from app.models import User
from app.security import totp
from app.services import cms, markdown, tunables


@pytest.fixture
def cx(make_harness):
    hx = make_harness(ADMIN_SESSION_IDLE=30 * 86400, ADMIN_SESSION_TTL=30 * 86400)
    hx.adm = hx.admin()
    return hx


def _step(hx) -> str:
    clock.advance(totp.STEP)
    return totp.code_at(hx.admin_secrets["owner"], totp.current_step(clock.timestamp()))


def test_every_page_renders_with_live_values_and_no_owner_details(cx):
    c = cx.client()
    for key, sp in cms.registry().items():
        if sp.kind != "page":
            continue
        r = c.get(f"/policies/{key}")
        assert r.status_code == 200, key
        assert cms.principle(cx.settings) in r.text and "<script" not in r.text and "{{" not in r.text, key
    terms = c.get("/policies/terms").text
    assert f"{cx.settings.MEMBERSHIP_PRICE:g} USDT" in terms and "ليست نصيحة استثمارية" in terms
    assert 'href="/policies/rewards_terms"' in terms  # site links work
    w = c.get("/policies/withdraw_terms").text
    assert f"{cx.settings.WITHDRAW_MIN:g} USDT" in w and "TRC20" in w and "مسؤوليتك" in w
    with cx.db() as db:
        tunables.save(db, cx.settings, {"WITHDRAW_MIN": 25}, "owner")
    cx.state.reload_tunables()
    assert "25 USDT" in c.get("/policies/withdraw_terms").text
    for key in ("about", "faq", "contact", "membership_terms", "rewards_terms", "giveaway_terms"):
        assert c.get(f"/policies/{key}").status_code == 200
    assert c.get("/policies/email.code").status_code == 404  # e-mail texts are not pages


def test_edit_preview_history_restore_and_reset(cx):
    adm, c = cx.adm, cx.client()
    items = {i["key"]: i for i in adm.get("/api/admin/content").json()["items"]}
    assert items["about"]["edited"] is False and items["email.code"]["kind"] == "email"
    r = adm.put("/api/admin/content/about", json={"body": "# من نحن\nفريق **صغير**.", "title": "عنّا", "note": "أول نسخة"})
    assert r.status_code == 200, r.text
    assert r.json()["edited"] is True and len(r.json()["revisions"]) == 1
    page = c.get("/policies/about").text
    assert "<h2>من نحن</h2>" in page and "<strong>صغير</strong>" in page and "عنّا" in page
    assert adm.put("/api/admin/content/about", json={"body": "نسخة ثانية"}).status_code == 200
    assert "نسخة ثانية" in c.get("/policies/about").text
    revs = adm.get("/api/admin/content/about").json()["revisions"]
    first = revs[-1]["id"]
    r = adm.post("/api/admin/content/about/restore", json={"revision_id": first})
    assert r.status_code == 200 and "صغير" in c.get("/policies/about").text
    assert adm.post("/api/admin/content/about/reset").json()["edited"] is False
    assert "عن DALTA.BIT" in c.get("/policies/about").text or f"عن {cx.settings.APP_NAME}" in c.get("/policies/about").text
    assert len(adm.get("/api/admin/content/about").json()["revisions"]) == 4  # history kept
    assert adm.put("/api/admin/content/privacy", json={"body": "   "}).status_code == 400  # a page cannot be empty
    assert adm.put("/api/admin/content/nope", json={"body": "x"}).status_code == 404
    audit = [e["action"] for e in adm.get("/api/admin/audit").json()["entries"]]
    assert {"content_update", "content_restore", "content_reset"} <= set(audit)


def _real_markup(page: str) -> list[tuple[str, list[tuple[str, str | None]]]]:
    """The elements and attributes a browser would actually create (escaped text is not markup)."""
    from html.parser import HTMLParser

    found: list = []

    class P(HTMLParser):
        def handle_starttag(self, tag, attrs):
            found.append((tag, attrs))

    P().feed(page)
    return found


ALLOWED = {"h2", "h3", "h4", "p", "ul", "ol", "li", "strong", "em", "a", "br"}


def _check_safe(fragment: str) -> None:
    for tag, attrs in _real_markup(fragment):
        assert tag in ALLOWED, (tag, fragment)
        for name, value in attrs:
            assert tag == "a" and name in ("href", "target", "rel"), (tag, name, fragment)
            if name == "href":
                assert value.startswith("https://") or (value.startswith("/") and not value.startswith(("//", "/\\"))), value


@pytest.mark.parametrize("evil", [
    "<script>alert(1)</script>",
    "<img src=x onerror=alert(1)>",
    "[x](javascript:alert(1))",
    "[x](data:text/html;base64,PHNjcmlwdD4=)",
    "[x](//evil.example/a)",
    '[x](https://ok.example/"onmouseover="alert(1))',
    "<a href='https://x.example' onclick='alert(1)'>x</a>",
    "**<svg/onload=alert(1)>**",
])
def test_markdown_never_lets_html_or_bad_links_through(cx, evil):
    _check_safe(markdown.render(evil))
    r = cx.adm.put("/api/admin/content/faq", json={"body": evil})
    assert r.status_code == 200
    page = cx.client().get("/policies/faq").text
    body = page[page.index('<article class="dl-card policy-body">'):page.index("</article>")]
    _check_safe(body.replace('<article class="dl-card policy-body">', ""))
    _check_safe(cx.client().get("/api/content/faq").json()["html"])


def test_texts_api_is_public_but_never_serves_email_texts(cx):
    c = cx.client()
    r = c.get("/api/content/membership_intro")
    assert r.status_code == 200 and "ميزات" in r.json()["text"] and "<p>" in r.json()["html"]
    assert c.get("/api/content/email.code").status_code == 404
    assert c.get("/api/content/../policies").status_code == 404
    assert c.get("/api/content/announcement").json()["text"] == ""
    cx.adm.put("/api/admin/content/announcement", json={"body": "صيانة قصيرة الليلة **2:00**"})
    assert c.get("/api/content/announcement").json()["text"] == "صيانة قصيرة الليلة 2:00"
    # the market disclaimer comes from the content system (default: the old setting)
    assert c.get("/api/content/market_disclaimer").json()["text"] == cx.settings.MARKET_DISCLAIMER


def test_major_change_needs_2fa_and_asks_everyone_again(cx):
    a = cx.user()
    assert a.get("/api/me").json()["privacy_notice"] is False
    before = cx.adm.get("/api/admin/content").json()["ack_version"]
    body = {"body": cms.PRIVACY + "\n\n# 11. جديد\nبند جديد.", "major": True}
    assert cx.adm.put("/api/admin/content/privacy", json={**body, "code": "000000"}).status_code == 403
    assert a.get("/api/me").json()["privacy_notice"] is False  # nothing changed
    r = cx.adm.put("/api/admin/content/privacy", json={**body, "code": _step(cx)})
    assert r.status_code == 200 and r.json()["ack_version"] == before + 1
    assert a.get("/api/me").json()["privacy_notice"] is True
    assert a.post("/api/me/privacy-ack").status_code == 200
    assert a.get("/api/me").json()["privacy_notice"] is False
    b = cx.user()  # a new account accepts the current version at sign-up
    assert b.get("/api/me").json()["privacy_notice"] is False
    # a normal edit (or a major edit of a non-consent page) asks nothing
    cx.adm.put("/api/admin/content/privacy", json={"body": "# 1\nتعديل بسيط"})
    cx.adm.put("/api/admin/content/faq", json={"body": "س", "major": True, "code": _step(cx)})
    assert a.get("/api/me").json()["privacy_notice"] is False
    assert "content_major" in [e["action"] for e in cx.adm.get("/api/admin/audit").json()["entries"]]


def test_email_texts_are_editable_with_safe_variables(cx):
    from app.services import mail_templates

    cx.adm.put("/api/admin/content/email.code", json={"title": "{app}: {code} رمزك", "body": "رمزك {code} {0.__class__} {settings}"})
    with cx.db() as db:
        subject, body = mail_templates.render(db, cx.settings, "email.code", {"code": "424242", "purpose": "x", "minutes": 5})
    assert subject == f"{cx.settings.APP_NAME}: 424242 رمزك"
    assert body == "رمزك 424242 {0.__class__} {settings}"
    prev = cx.adm.post("/api/admin/content/email.code/preview", json={"body": "{code} {nope}"}).json()
    assert prev == {"text": "123456 {nope}"}


def test_content_routes_need_super_admin(cx):
    from tests.conftest import ADMIN_PATH

    a = cx.user()
    for method, path in (("get", "/api/admin/content"), ("put", "/api/admin/content/about"),
                         ("post", "/api/admin/content/about/reset"), ("post", "/api/admin/content/about/restore")):
        r = getattr(a, method)(ADMIN_PATH + path, **({} if method == "get" else {"json": {"body": "x", "revision_id": 1}}))
        assert r.status_code in (401, 403, 404, 405), (path, r.status_code)
    with cx.db() as db:
        assert db.query(User).count() >= 1
