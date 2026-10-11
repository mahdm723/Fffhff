"""V6 phase 11c: the logo everywhere. Every icon is generated from the two masters (brand/build.py) at its exact
size; the two system icons that must be flat are flat (one colour, no glow); the Android icon survives every
launcher mask; the pages, the emails and the deploy scripts show the logo and the name from APP_NAME."""

from __future__ import annotations

import importlib.util
import json
import math
import re
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
RES = ROOT / "android" / "app" / "src" / "main" / "res"
ICONS = ROOT / "static" / "icons"
DENSITIES = ("mdpi", "hdpi", "xhdpi", "xxhdpi", "xxxhdpi")


def _build():
    spec = importlib.util.spec_from_file_location("brand_build", ROOT / "brand" / "build.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _pixels(im: Image.Image):
    return im.get_flattened_data() if hasattr(im, "get_flattened_data") else im.getdata()


def test_every_generated_file_matches_the_masters():
    assert _build().check() == []  # text outputs byte for byte, images present (python brand/build.py)


def test_every_image_has_its_exact_size_and_transparency():
    b = _build()
    for path, w, h, _html, transparent in b.rasters(b.APP_NAME):
        im = Image.open(path)
        assert im.size == (w, h), path
        if transparent:
            assert im.mode == "RGBA" and im.getchannel("A").getextrema()[0] == 0, path  # see-through around the mark
        else:
            assert im.mode == "RGB", path  # opaque: iOS, maskable, og:image, Telegram
    assert Image.open(ICONS / "favicon.ico").ico.sizes() == {(16, 16), (32, 32), (48, 48)}
    assert Image.open(ICONS / "apple-touch-icon.png").size == (180, 180)
    assert Image.open(ICONS / "og-image.png").size == (1200, 630)
    assert Image.open(ROOT / "brand" / "export" / "telegram-bot-512.png").size == (512, 512)
    manifest = json.loads((ROOT / "static" / "manifest.webmanifest").read_text(encoding="utf-8"))
    assert any(i.get("purpose") == "maskable" for i in manifest["icons"])
    for icon in manifest["icons"]:
        w, h = map(int, icon["sizes"].split("x"))
        assert Image.open(ROOT / "static" / icon["src"].lstrip("/")).size == (w, h), icon


def test_the_notification_and_android_13_icons_are_flat():
    xml = (RES / "drawable" / "ic_notification.xml").read_text()
    assert set(re.findall(r'fillColor="([^"]+)"', xml)) == {"#FFFFFFFF"} and "gradient" not in xml and "aapt" not in xml
    flat = [RES / f"mipmap-{d}" / "ic_launcher_monochrome.png" for d in DENSITIES] + [ICONS / "badge-96.png"]
    for path in flat:
        im = Image.open(path).convert("RGBA")
        assert {px[:3] for px in _pixels(im) if px[3] > 0} == {(255, 255, 255)}, path  # one colour, no glow


def test_the_android_icon_survives_every_launcher_mask():
    """Adaptive icon: 108 units, the launcher shows the 72-unit centre through its mask and guarantees the 66-unit
    circle. Every drawn pixel (alpha ≥ 10 %) of both layers must be inside that circle and inside every mask."""
    b = _build()
    for name in ("ic_launcher_foreground", "ic_launcher_monochrome"):
        im = Image.open(RES / "mipmap-xxxhdpi" / f"{name}.png").convert("RGBA")
        w = im.width
        unit, c = w / 108, w / 2
        alpha = im.getchannel("A")
        drawn = [(i % w, i // w) for i, a in enumerate(_pixels(alpha)) if a >= 26]
        assert drawn, name
        assert max(math.hypot(x + 0.5 - c, y + 0.5 - c) for x, y in drawn) <= 33 * unit, name
        lo = round(18 * unit)
        for shape in b.MASK_SHAPES:
            m = b.mask(shape, w - 2 * lo)
            assert all(lo <= x < w - lo and lo <= y < w - lo and m.getpixel((x - lo, y - lo)) for x, y in drawn), (name, shape)


def test_pages_show_the_logo_the_red_dot_and_link_previews(hx):
    c = hx.client()
    home = c.get("/").text
    assert 'class="logo-mark logo-mark--pulse"' in home and 'class="wordmark__dot"' in home  # the boot screen
    assert "/icons/og-image.png" in home and 'property="og:image"' in home and "summary_large_image" in home
    assert "/icons/favicon.ico" in home and "/icons/apple-touch-icon.png" in home and "{{" not in home
    download = c.get("/download").text
    assert 'class="wordmark__dot"' in download and 'property="og:image"' in download and "style=" not in download
    assert "/icons/favicon.ico" in c.get("/policies/privacy").text
    for path, kind in (("/icons/favicon.ico", "image/"), ("/icons/og-image.png", "image/png"),
                       ("/icons/apple-touch-icon.png", "image/png"), ("/icons/badge-96.png", "image/png"),
                       ("/js/logo-shapes.js", "javascript")):
        r = c.get(path)
        assert r.status_code == 200 and kind in r.headers["content-type"], path


def test_the_name_with_its_red_dot_in_emails_and_scripts():
    from app.services import mail, mailer

    _text, body = mailer._reset_bodies("Brand.New", "123456", 1)
    assert 'Brand<span style="color:#E5232D">.</span>New' in body and "dzplay" not in body.lower()
    assert 'Brand<span style="color:#E5232D">.</span>New' in mail._html("Brand.New", "رمزك 1234", "1234")
    # messages the deploy scripts print or send: the name from APP_NAME, never the old one
    for script in (ROOT / "deploy").glob("*.sh"):
        for line in script.read_text().splitlines():
            if not line.lstrip().startswith("#") and re.search(r"\b(echo|say|ok|warn|die|printf)\b|text=", line):
                assert "DZPLAY" not in line, (script.name, line)
