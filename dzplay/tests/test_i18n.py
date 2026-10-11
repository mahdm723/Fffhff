"""V6 phase 12: three languages — the catalog is complete, a missing translation shows the Arabic (never a key),
errors / pages / notifications / e-mails / pushes follow the reader's language, and the panel stays in Arabic."""

from __future__ import annotations

import json
import re

import pytest
from sqlalchemy import select

from app import i18n
from app.config import Settings
from app.i18n import content, extract, store
from app.models import User
from app.services import cms, mail_templates, notify
from tests.smtp_sink import SmtpSink


def _user(hx, c) -> User:
    with hx.db() as db:
        return db.scalar(select(User).where(User.email == c.email))


# ------------------------------------------------------------------ catalog


def test_every_text_has_a_french_and_an_english_translation():
    """app/i18n/msgids.json is current and fr.json / en.json cover every text, with the same {variables}."""
    assert extract.main(["--check"]) == 0
    var = re.compile(r"\{(\w+)\}")
    for lang in ("fr", "en"):
        table = i18n.defaults(lang)
        for entry in store.catalog():
            value = table[entry["id"]]
            forms = value.values() if isinstance(value, dict) else [value]
            for form in forms:
                assert set(var.findall(form)) == set(var.findall(entry["id"])), (lang, entry["id"])
                assert not re.search(r"[؀-ۿ]", form), (lang, entry["id"])  # no Arabic left inside


def test_the_content_system_ships_french_and_english_with_the_same_variables():
    s = Settings(SECRET_KEY="x" * 40)
    v2, v1 = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}"), re.compile(r"(?<!\{)\{([a-z_]+)\}(?!\})")
    for key, sp in cms.registry().items():
        ar = sp.default(s) if callable(sp.default) else sp.default
        for lang in ("fr", "en"):
            title, body = content.default(key, lang)
            if sp.kind == "email":
                assert set(v1.findall(body)) | set(v1.findall(title or "")) == \
                    set(v1.findall(ar)) | set(v1.findall(mail_templates.TEMPLATES[key][1])), (lang, key)
            else:
                assert set(v2.findall(body)) == set(v2.findall(ar)), (lang, key)


def test_a_missing_translation_shows_the_arabic_never_a_key(monkeypatch):
    assert i18n.tr("غير موجود.", "fr") == "Introuvable."
    assert i18n.tr("غير موجود.", "en") == "Not found."
    monkeypatch.setitem(i18n.OVERRIDES, "fr", {})
    i18n.defaults.cache_clear()
    monkeypatch.setattr(i18n, "defaults", lambda lang: {})  # no translation shipped at all
    assert i18n.tr("غير موجود.", "fr") == "غير موجود."
    assert i18n.tr("نص جديد لم يُترجم بعد {n}", "en", n=3) == "نص جديد لم يُترجم بعد 3"


def test_plural_forms_and_number_formats():
    assert i18n.tr("{n} يوم", "fr", n=1) == "1 jour" and i18n.tr("{n} يوم", "fr", n=7) == "7 jours"
    assert i18n.tr("{n} يوم", "en", n=1) == "1 day" and i18n.tr("{n} يوم", "en", n=7) == "7 days"
    assert i18n.tr("{n} يوم", "ar", n=7) == "7 أيام" and i18n.tr("{n} يوم", "ar", n=30) == "30 يومًا"
    assert i18n.tr("أعجب {n} أشخاص", "en", n=2) == "Liked by 2 people"
    assert i18n.number("12.50", "fr") == "12,50" and i18n.number("12.50", "en") == "12.50" and i18n.number(3, "fr") == "3"
    assert i18n.tr_params("مكافأة: {amount} USDT — {note}", "fr", {"amount": "2.50", "note": "x"}) == "Récompense : 2,50 USDT — x"


# ------------------------------------------------------------------ requests


def test_errors_follow_the_request_language(hx):
    c = hx.client()
    assert c.get("/api/content/nope").json()["error"]["message"] == "غير موجود."
    assert c.get("/api/content/nope", headers={"X-DZ-Lang": "fr"}).json()["error"]["message"] == "Introuvable."
    assert c.get("/api/content/nope", headers={"Accept-Language": "en-US,en;q=0.9"}).json()["error"]["message"] == "Not found."
    assert c.get("/api/content/nope?lang=en").json()["error"]["message"] == "Not found."
    assert c.get("/api/content/nope", headers={"X-DZ-Lang": "de"}).json()["error"]["message"] == "غير موجود."


def test_the_dictionary_of_each_language(hx):
    c = hx.client()
    r = c.get("/api/i18n/fr")
    assert r.status_code == 200 and r.headers["etag"]
    data = r.json()
    assert data["messages"]["حفظ"] == "Enregistrer" and data["version"]
    assert c.get("/api/i18n/en").json()["messages"]["حفظ"] == "Save"
    assert c.get("/api/i18n/de").status_code == 404
    server_only = next(e["id"] for e in store.catalog() if not e["client"])
    assert server_only not in data["messages"]  # the app gets only its own texts


def test_the_language_is_saved_on_the_account(hx):
    c = hx.user()
    assert c.get("/api/me").json().get("language") is None
    r = c.put("/api/me/language", json={"lang": "fr"})
    assert r.status_code == 200 and r.json() == {"language": "fr"}
    assert r.cookies.get("dz_lang") == "fr"
    assert c.get("/api/me").json()["language"] == "fr"
    assert _user(hx, c).language == "fr"
    assert c.put("/api/me/language", json={"lang": "xx"}).status_code == 400
    assert c.put("/api/me/language", json={"lang": "en", "extra": 1}).status_code in (400, 422)
    assert hx.client().put("/api/me/language", json={"lang": "fr"}).status_code == 401
    cfg = hx.client().get("/api/config").json()
    assert [(x["code"], x["dir"]) for x in cfg["languages"]] == [("ar", "rtl"), ("fr", "ltr"), ("en", "ltr")]


def test_pages_without_javascript_in_three_languages(hx):
    c = hx.client()
    page = c.get("/policies/privacy?lang=fr")
    assert '<html lang="fr" dir="ltr">' in page.text and "Politique de confidentialité" in page.text
    assert page.cookies.get("dz_lang") == "fr"  # remembered for the next pages
    nxt = c.get("/policies/terms")
    assert '<html lang="fr" dir="ltr">' in nxt.text and "Conditions d’utilisation" in nxt.text
    en = hx.client().get("/policies/terms", headers={"Accept-Language": "en"})
    assert '<html lang="en" dir="ltr">' in en.text and "Not investment advice" in en.text
    ar = hx.client().get("/policies/terms")
    assert '<html lang="ar" dir="rtl">' in ar.text and "ليست نصيحة استثمارية" in ar.text
    dl = hx.client().get("/download", headers={"Accept-Language": "fr-FR"})
    assert '<html lang="fr" dir="ltr">' in dl.text and "Télécharger" in dl.text and 'content="fr_FR"' in dl.text
    home = hx.client().get("/", headers={"Accept-Language": "en"})
    assert '<html lang="en" dir="ltr"' in home.text and "{{" not in home.text
    man = hx.client().get("/manifest.webmanifest", headers={"Accept-Language": "fr"}).json()
    assert man["lang"] == "fr" and man["dir"] == "ltr" and "traders" in man["description"]


def test_short_texts_follow_the_language_and_keep_the_settings_variables(hx):
    c = hx.client()
    fr = c.get("/api/content/membership_intro", headers={"X-DZ-Lang": "fr"}).json()
    assert fr["lang"] == "fr" and fr["dir"] == "ltr" and "adhésion" in fr["text"] and hx.settings.APP_NAME in fr["text"]
    terms = c.get("/api/content/terms", headers={"X-DZ-Lang": "en"}).json()
    assert f"{hx.settings.MEMBERSHIP_PRICE:g} USDT" in terms["text"]  # {{membership_price}} still filled


def test_the_panel_stays_in_arabic(hx):
    hx.create_admin()
    adm = hx.admin()
    r = adm.get("/api/admin/content/nope", headers={"X-DZ-Lang": "fr", "Accept-Language": "fr"})
    assert r.status_code == 404 and r.json()["error"]["message"] == "غير موجود."
    items = adm.get("/api/admin/content", headers={"X-DZ-Lang": "en"}).json()["items"]
    assert next(i for i in items if i["key"] == "privacy")["name"] == "سياسة الخصوصية"


# ------------------------------------------------------------------ the content system by language


def test_a_french_edit_wins_then_back_to_the_shipped_translation(hx, monkeypatch):
    s = hx.settings
    with hx.db() as db:
        cms.save(db, s, "about", body="# Bienvenue\nTexte modifié.", title="À propos (modifié)", actor="test", lang="fr")
        db.commit()
    with hx.db() as db:
        out = cms.public(db, s, "about", "fr")
        assert out["title"] == "À propos (modifié)" and "Texte modifié." in out["text"] and out["lang"] == "fr"
        assert "عن" in cms.public(db, s, "about", "ar")["title"]  # the Arabic is untouched
        assert [r["lang"] for r in cms.revisions(db, "about", lang="fr")] == ["fr"]
        assert cms.revisions(db, "about") == []  # the Arabic history is separate
        langs = next(i for i in cms.listing(db, s) if i["key"] == "about")["langs"]
        assert langs == {"ar": "default", "fr": "edited", "en": "default"}
        cms.reset(db, "about", "test", "fr")
        db.commit()
    with hx.db() as db:
        assert cms.public(db, s, "about", "fr")["title"] == "À propos de l’application"
        rev = cms.revisions(db, "about", lang="fr")
        cms.restore(db, s, "about", rev[-1]["id"], "test")  # the edit again, from its backup
        db.commit()
    with hx.db() as db:
        assert "Texte modifié." in cms.public(db, s, "about", "fr")["text"]
    # nothing in a language at all: the Arabic text is shown, and the panel marks it missing
    monkeypatch.setattr(content, "default", lambda key, lang: None)
    with hx.db() as db:
        cur = cms.current(db, s, "faq", "en")
        assert cur["missing"] and "العضوية" in cur["body"]
        assert cms.public(db, s, "faq", "en")["lang"] == "ar"
        assert next(i for i in cms.listing(db, s) if i["key"] == "faq")["langs"]["en"] == "missing"


def test_an_edited_setting_text_is_not_replaced_by_the_shipped_translation(make_harness):
    hx = make_harness(MARKET_DISCLAIMER="نص التنبيه الذي كتبه المالك")
    c = hx.client()
    assert c.get("/api/content/market_disclaimer", headers={"X-DZ-Lang": "en"}).json()["text"] == "نص التنبيه الذي كتبه المالك"
    hx2 = make_harness()
    assert "investment advice" in hx2.client().get("/api/content/market_disclaimer", headers={"X-DZ-Lang": "en"}).json()["text"]


# ------------------------------------------------------------------ notifications, e-mails, pushes


def test_notifications_are_shown_in_the_readers_language(hx):
    a = hx.user()
    uid = _user(hx, a).id
    with hx.db() as db:
        notify.create(db, uid, "reward", None, data=notify.msg("مكافأة: {amount} USDT — {note}", amount="2.50", note="شكرًا"))
        notify.create(db, uid, "refund_rejected", None, data={"text": "نص قديم كما حُفظ"})  # a row from before phase 12
        db.commit()
    fr = a.get("/api/notifications", headers={"X-DZ-Lang": "fr"}).json()["notifications"]
    assert fr[1]["data"] == {"text": "Récompense : 2,50 USDT — شكرًا"}  # the admin's own note stays as written
    assert fr[0]["data"] == {"text": "نص قديم كما حُفظ"}
    ar = a.get("/api/notifications").json()["notifications"]
    assert ar[1]["data"]["text"] == "مكافأة: 2.50 USDT — شكرًا"


@pytest.fixture
def smtp():
    with SmtpSink() as sink:
        yield sink


def test_emails_arrive_in_the_users_language(make_harness, smtp):
    from app.services import email_codes

    hx = make_harness(SMTP_HOST="127.0.0.1", SMTP_PORT=smtp.port, SMTP_SECURITY="none", SMTP_FROM="App <no-reply@dzplay.test>")
    a = hx.user()
    a.put("/api/me/language", json={"lang": "fr"})
    with hx.db() as db:
        email_codes.send(db, hx.settings, hx.state.limiter, _user(hx, a), "withdraw")
        db.commit()
    msg = smtp.messages[-1]["msg"]
    text = msg.get_body(("plain",)).get_content()
    assert "code de confirmation" in msg["Subject"] and "Retrait des récompenses" in text
    assert "Ceci est un message automatique" in text
    html_part = msg.get_body(("html",)).get_content()
    assert '<html lang="fr" dir="ltr">' in html_part
    b = hx.user()
    with hx.db() as db:
        email_codes.send(db, hx.settings, hx.state.limiter, _user(hx, b), "withdraw")
        db.commit()
    assert "رمز التأكيد" in smtp.messages[-1]["msg"]["Subject"]  # no language chosen: Arabic


def test_web_push_text_follows_the_users_language():
    from app.services import push

    s = Settings(SECRET_KEY="x" * 40)
    assert push.push_payload(s, "en")["body"] == f"You have a new message on {s.APP_NAME}"
    assert push.push_payload(s, "ar")["body"] == f"لديك رسالة جديدة على {s.APP_NAME}"
    assert json.dumps(push.push_payload(s, "fr"))  # still only title, body and url
