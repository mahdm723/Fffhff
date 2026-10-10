"""The new logo, stage A: three concepts (SVG) previewed in the panel's «الهوية» tab — super admin only, plain
shapes only (no script, no text, no external reference), the brand colours only, and a red that reads on both
black and white."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

from app.api import admin_brand
from app.services import appearance as ap

CONCEPTS = Path(__file__).resolve().parent.parent / "brand" / "concepts"
SVG = "{http://www.w3.org/2000/svg}"


def test_three_concepts_are_plain_safe_shapes_in_the_brand_colours():
    ids = [c["id"] for c in admin_brand.CONCEPTS]
    assert ids == ["delta", "d-dot", "candles"]
    for cid in ids:
        text = (CONCEPTS / f"{cid}.svg").read_text()
        root = ET.fromstring(text)
        assert root.tag == SVG + "svg" and root.get("viewBox") == "0 0 100 100", cid
        for el in root.iter():
            assert el.tag.removeprefix(SVG) in {"svg", "g", "path", "circle", "rect"}, (cid, el.tag)  # no text, image, script
            assert not any(k.startswith("on") or "href" in k for k in el.attrib), (cid, el.attrib)
        colours = set(re.findall(r'(?:fill|stroke)="([^"]+)"', text))
        assert colours <= {"currentColor", "none", admin_brand.COLORS["red"]}, (cid, colours)
        dots = [el for el in root.iter(SVG + "circle") if el.get("class") == "dot"]
        assert len(dots) == 1 and dots[0].get("fill") == admin_brand.COLORS["red"], cid  # one red dot, the same everywhere


def test_the_brand_red_reads_on_black_and_white_and_the_proposal_is_readable():
    c = admin_brand.COLORS
    assert ap.contrast(c["red"], c["black"]) >= 4 and ap.contrast(c["red"], c["white"]) >= 4
    data = admin_brand.preview_data()
    for mode in ("dark", "light"):
        v = data["accent_proposed"][mode]
        assert v["on_contrast"] >= 4.5, mode  # the button text on the proposed red
        bg, card = ap.SURFACES[mode]
        assert min(ap.contrast(v["accent-text"], bg), ap.contrast(v["accent-text"], card)) >= 4.5, mode


def test_the_preview_is_for_the_super_admin_only(make_harness):
    hx = make_harness()
    user = hx.user()
    assert user.get("/api/admin/brand").status_code == 404  # outside the secret panel path
    from tests.conftest import ADMIN_PATH
    assert user.get(ADMIN_PATH + "/api/admin/brand").status_code in (401, 403)
    adm = hx.admin()
    r = adm.get("/api/admin/brand")
    assert r.status_code == 200, r.text
    d = r.json()
    assert [c["id"] for c in d["concepts"]] == ["delta", "d-dot", "candles"] and all("<svg" in c["svg"] for c in d["concepts"])
    assert d["colors"]["red"] == "#E5232D"


def test_the_chosen_logo_has_a_large_and_a_small_version():
    """Concept 3 chosen: large (≥ 48 px: five candles on a base, wicks) and small (16–24 px and the notification icon:
    three thick staggered candles, no base, no wick, a bigger red dot)."""
    brand = CONCEPTS.parent
    shapes = {}
    for name in ("logo-large", "logo-small"):
        text = (brand / f"{name}.svg").read_text()
        root = ET.fromstring(text)
        assert root.get("viewBox") == "0 0 100 100", name
        for el in root.iter():
            assert el.tag.removeprefix(SVG) in {"svg", "g", "path", "circle", "rect"}, (name, el.tag)
        assert set(re.findall(r'(?:fill|stroke)="([^"]+)"', text)) <= {"currentColor", admin_brand.COLORS["red"]}, name
        shapes[name] = ([(float(r.get("x")), float(r.get("width")), float(r.get("height")), float(r.get("y"))) for r in root.iter(SVG + "rect")],
                        float(root.find(SVG + "circle").get("r")))
    large, large_dot = shapes["logo-large"]
    small, small_dot = shapes["logo-small"]
    assert len(small) == 3 and min(w for _x, w, _h, _y in small) >= 20  # three thick candles, nothing thin
    assert small_dot > large_dot  # a bigger red dot at small sizes
    bodies = sorted((x, w) for x, w, _h, _y in large if w == 11)
    assert len(bodies) == 5 and all(b[0] - (a[0] + a[1]) >= 6 for a, b in zip(bodies, bodies[1:]))  # clear gaps
    # the small one is not mirrored (reads as a candle chart, not a triangle)
    tops = {x: y for x, _w, _h, y in small}
    assert tops[min(tops)] != tops[max(tops)]  # the side candles sit at different levels
    d = admin_brand.preview_data()
    assert d["chosen"]["large"]["svg"].startswith("<svg") and d["chosen"]["small"]["svg"].startswith("<svg")
