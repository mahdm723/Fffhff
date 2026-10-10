"""V6 phase 8: appearance — every accent colour readable in both modes (WCAG ≥ 4.5), the generated CSS in sync
with its source, the choice saved on the account (validated, synced), and the app's name shown everywhere."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.services import appearance as ap

STATIC = Path(__file__).resolve().parent.parent / "static"


def test_contrast_formula_matches_wcag_reference_values():
    assert round(ap.contrast("#ffffff", "#000000"), 2) == 21.0
    assert round(ap.contrast("#777777", "#ffffff"), 2) == 4.48  # the classic "just fails" grey
    assert ap.contrast("#123456", "#123456") == 1.0


@pytest.mark.parametrize("name", list(ap.ACCENTS))
@pytest.mark.parametrize("mode", ["dark", "light"])
def test_every_accent_is_readable(name, mode):
    v = ap.palette()[name][mode]
    bg, card = ap.SURFACES[mode]
    for stop in (v["accent"], v["accent-2"]):
        assert ap.contrast(v["on-accent"], stop) >= 4.5, (name, mode, "button text", stop)
    assert ap.contrast(v["accent-text"], bg) >= 4.5, (name, mode, "accent text on the page")
    assert ap.contrast(v["accent-text"], card) >= 4.5, (name, mode, "accent text on cards")


def test_generated_css_is_in_sync_and_surfaces_match_the_stylesheet():
    assert (STATIC / "css" / "accents.css").read_text() == ap.css(), "run: python -m app.services.appearance --write"
    app_css = (STATIC / "css" / "app.css").read_text()
    for mode, (bg, card) in ap.SURFACES.items():
        assert f"--bg: {bg};" in app_css and f"--surface: {card};" in app_css, mode
    index = (STATIC / "index.html").read_text()
    assert '<script src="/js/theme-boot.js"></script>' in index and "/css/accents.css" in index
    # every colour that sits on the accent gradient uses --on-accent (never a fixed white)
    for rule in re.findall(r"[^{}]*\{[^{}]*background:\s*var\(--grad\)[^{}]*\}", app_css):
        assert "color: #fff" not in rule, rule


def test_appearance_is_saved_validated_and_returned(make_harness):
    hx = make_harness()
    a = hx.user()
    me = a.get("/api/me").json()
    assert me["appearance"] == ap.DEFAULT and {c["id"] for c in me["appearance_choices"]} == set(ap.ACCENTS)
    r = a.put("/api/me/appearance", json={"mode": "light", "accent": "ocean", "font": "large"})
    assert r.status_code == 200 and r.json()["appearance"] == {"mode": "light", "accent": "ocean", "font": "large"}
    assert a.get("/api/me").json()["appearance"]["accent"] == "ocean"  # synced through the account
    for bad in ({"mode": "neon"}, {"accent": "</style>"}, {"font": "huge"}, {"mode": "light", "extra": 1}):
        assert a.put("/api/me/appearance", json={**{"mode": "light", "accent": "ocean", "font": "large"}, **bad}).status_code in (400, 422)
    assert hx.client().put("/api/me/appearance", json={"mode": "dark", "accent": "ember", "font": "normal"}).status_code in (401, 403)


def test_the_app_name_comes_from_one_setting(make_harness):
    hx = make_harness(APP_NAME="Brand.New")
    c = hx.client()
    assert c.get("/api/config").json()["app_name"] == "Brand.New"
    page = c.get("/policies/privacy").text
    assert "Brand.New" in page and "DZPLAY" not in page and "DALTA" not in page
    # no hard-coded name in the app code (comments and the Android bridge identifiers DZPLAYAndroid / DZPLAYApp aside)
    for f in [*(STATIC / "js").rglob("*.js"), *(STATIC.parent / "app" / "admin_static").glob("*.js")]:
        code = re.sub(r"//[^\n]*|/\*.*?\*/", "", f.read_text(), flags=re.S)
        code = re.sub(r"DZPLAY(Android|App)", "", code)
        assert "DZPLAY" not in code, f
        assert "DALTA.BIT" not in code.replace("|| 'DALTA.BIT'", ""), f  # only brand.js' fallback
