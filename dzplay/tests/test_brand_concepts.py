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


def _logo(name: str):
    root = ET.fromstring((CONCEPTS.parent / f"{name}.svg").read_text())
    rects = [{k: float(r.get(k)) for k in ("x", "y", "width", "height")} for r in root.iter(SVG + "rect")]
    circles = {c.get("class"): {k: float(c.get(k)) for k in ("cx", "cy", "r")} for c in root.iter(SVG + "circle")}
    return root, rects, circles


DOT_STOPS = {"#FF8A80", "#FF3B3B", "#C8141C", admin_brand.COLORS["red"]}  # the brand red, lighter at the centre


def test_the_logo_is_three_candles_with_a_glowing_dot_on_the_wick():
    """The adopted logo (concept 3, version A) — three candles on a base: the tallest on the LEFT with a clear thin
    wick and the red dot on the wick like a flame (not stuck to the body), then shorter, then shorter. The dot glows
    (a radial gradient + a soft halo). Large (≥ 48 px): a wick on every candle. Small (16–24 px): the same heights,
    thicker, only the tallest keeps its wick, a bigger dot. Fixed coordinates: nothing can mirror it."""
    found = {}
    for size in ("large", "small"):
        name = f"logo-{size}"
        text = (CONCEPTS.parent / f"{name}.svg").read_text()
        root, rects, circles = _logo(name)
        assert root.get("viewBox") == "0 0 100 100", name
        ids = set()
        for el in root.iter():
            assert el.tag.removeprefix(SVG) in {"svg", "g", "defs", "radialGradient", "stop", "circle", "rect"}, (name, el.tag)
            assert not any(k.startswith("on") or "href" in k or k == "transform" for k in el.attrib), (name, el.attrib)
            if el.get("id"):
                ids.add(el.get("id"))
        paints = set(re.findall(r'(?:fill|stroke)="([^"]+)"', text))
        refs = {p[5:-1] for p in paints if p.startswith("url(#")}
        assert paints - {f"url(#{r})" for r in refs} <= {"currentColor"} and refs <= ids, (name, paints)
        assert set(re.findall(r'stop-color="([^"]+)"', text)) <= DOT_STOPS, name
        dot, glow = circles["dot"], circles["glow"]
        assert dot["cx"] == glow["cx"] and dot["cy"] == glow["cy"] and glow["r"] > dot["r"], name  # a halo around it

        base = max(rects, key=lambda r: r["width"])
        body_w = max(r["width"] for r in rects if r is not base)
        bodies = sorted((r for r in rects if r["width"] == body_w), key=lambda r: r["x"])  # left to right
        wicks = [r for r in rects if r["width"] < body_w / 2]
        assert len(bodies) == 3 and len(bodies) + len(wicks) + 1 == len(rects), name
        h = [round(base["y"] + base["height"] - b["y"], 3) for b in bodies]  # height above the bottom of the base
        assert h[0] > h[1] > h[2], (name, h)  # the tallest on the left, then shorter, then shorter
        tall = bodies[0]
        axis = tall["x"] + tall["width"] / 2
        wick = [w for w in wicks if abs(w["x"] + w["width"] / 2 - axis) < 0.01]
        assert len(wick) == 1, name
        wick = wick[0]
        assert wick["y"] < tall["y"] <= wick["y"] + wick["height"], name  # a thin wick standing on the body
        assert abs(dot["cx"] - axis) < 0.01, name  # the dot on the tallest candle's axis
        assert dot["cy"] < wick["y"] <= dot["cy"] + dot["r"], name  # resting on the top of the wick
        gaps = [b["x"] - (a["x"] + a["width"]) for a, b in zip(bodies, bodies[1:])]
        found[size] = {"body": body_w, "dot": dot["r"], "gap": min(gaps), "wicks": len(wicks),
                       "dot_gap": tall["y"] - (dot["cy"] + dot["r"])}  # clear of the body
    large, small = found["large"], found["small"]
    assert large["wicks"] == 3 and small["wicks"] == 1  # wicks at large sizes; only the tallest keeps one when small
    assert large["gap"] >= 6 and small["gap"] >= 6 and large["dot_gap"] >= 8 and small["dot_gap"] >= 8
    assert large["dot"] > 9  # a little bigger than the earlier revisions
    assert small["body"] > large["body"] and small["dot"] > large["dot"]  # thicker, a bigger dot
    d = admin_brand.preview_data()
    assert [v["id"] for v in d["chosen"]["variants"]] == ["a"]
    v = d["chosen"]["variants"][0]
    assert v["large"]["svg"] == (CONCEPTS.parent / "logo-large.svg").read_text()
    assert v["small"]["svg"] == (CONCEPTS.parent / "logo-small.svg").read_text()


def test_the_glow_pulse_respects_reduced_motion_and_the_flat_versions_have_no_glow():
    css = (Path(__file__).resolve().parent.parent / "app" / "admin_static" / "admin.css").read_text()
    assert re.search(r"\.brand-mark--mono \.glow\s*\{\s*display:\s*none", css)  # notification, Android 13 themed
    reduced = css[css.index("prefers-reduced-motion: reduce"):]
    assert re.search(r"\.brand-anim \.glow, \.brand-anim \.dot\s*\{\s*animation:\s*none", reduced)
