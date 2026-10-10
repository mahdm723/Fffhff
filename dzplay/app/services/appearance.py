"""V6 phase 8: appearance — light / dark / system, an accent colour, normal / large text.

The accent palette lives here and only here. `python -m app.services.appearance --write` generates
static/css/accents.css from it; a test checks the file is in sync and that every colour is readable:

* --on-accent   text/icons on the accent gradient (buttons, the send button, my chat bubbles): white or a near
                black ink, whichever contrasts more with BOTH gradient stops — always ≥ 4.5 (WCAG AA).
* --accent-text the accent used as text or icon colour (active tab, links, «liked»): the accent darkened (light
                mode) or lightened (dark mode) just enough to reach ≥ 4.5 on the page background and on cards.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

INK = "#1a0d08"
WHITE = "#ffffff"
MIN_TEXT = 4.5

# The page background and card surface of each mode (must match static/css/app.css).
SURFACES = {"dark": ("#0c0e18", "#161a29"), "light": ("#f5f2ec", "#ffffff")}

# name: (label, dark (stop 1, stop 2), light (stop 1, stop 2)). The first one is the default (the DALTA.BIT identity).
ACCENTS: dict[str, tuple[str, tuple[str, str], tuple[str, str]]] = {
    "ember": ("جمري", ("#ff8a5c", "#ff5f8a"), ("#ec6a3c", "#e5466f")),
    "rose": ("وردي", ("#ff7eb6", "#c77dff"), ("#cc2f7c", "#a23ad6")),
    "violet": ("بنفسجي", ("#a99cff", "#7b6cff"), ("#6b5cff", "#5a3fd6")),
    "ocean": ("أزرق", ("#5cc8ff", "#4f7dff"), ("#1a6dd0", "#2a56d6")),
    "mint": ("نعناعي", ("#43e0b0", "#2bc0d6"), ("#0e9f73", "#0f8a9c")),
    "sun": ("ذهبي", ("#ffd166", "#ff9f43"), ("#d99a00", "#e0661f")),
    "crimson": ("قرمزي", ("#ff6b6b", "#ff3d71"), ("#d93a3a", "#c2185b")),
}
DEFAULT_ACCENT = "ember"
MODES = ("system", "light", "dark")
FONTS = ("normal", "large")
DEFAULT = {"mode": "system", "accent": DEFAULT_ACCENT, "font": "normal"}


# ------------------------------------------------------------------ WCAG 2.x contrast

def _rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _hex(rgb: tuple[float, float, float]) -> str:
    return "#" + "".join(f"{max(0, min(255, round(c))):02x}" for c in rgb)


def luminance(hex_color: str) -> float:
    def lin(c: int) -> float:
        s = c / 255
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4

    r, g, b = _rgb(hex_color)
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def contrast(a: str, b: str) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _mix(color: str, toward: str, t: float) -> str:
    c, w = _rgb(color), _rgb(toward)
    return _hex(tuple(c[i] + (w[i] - c[i]) * t for i in range(3)))


# ------------------------------------------------------------------ derived colours

def on_accent(stops: tuple[str, str]) -> str:
    """White or ink, whichever has the higher worst-case contrast over both gradient stops."""
    def worst(fg: str) -> float:
        return min(contrast(fg, s) for s in stops)

    return WHITE if worst(WHITE) >= worst(INK) else INK


def accent_text(color: str, mode: str) -> str:
    """The accent nudged toward black (light mode) or white (dark mode) until it reads on bg and cards."""
    toward = "#000000" if mode == "light" else "#ffffff"
    bg, surface = SURFACES[mode]
    for step in range(0, 101):
        c = _mix(color, toward, step / 100)
        if min(contrast(c, bg), contrast(c, surface)) >= MIN_TEXT:
            return c
    return toward


def palette() -> dict[str, dict[str, dict[str, str]]]:
    out: dict[str, dict[str, dict[str, str]]] = {}
    for name, (_label, dark, light) in ACCENTS.items():
        out[name] = {}
        for mode, stops in (("dark", dark), ("light", light)):
            out[name][mode] = {"accent": stops[0], "accent-2": stops[1], "on-accent": on_accent(stops),
                               "accent-text": accent_text(stops[0], mode)}
    return out


def choices() -> list[dict]:
    """For the «المظهر» sheet: name, label and the two dark-mode stops (the swatch)."""
    return [{"id": n, "label": v[0], "swatch": list(v[1]), "swatch_light": list(v[2])} for n, v in ACCENTS.items()]


# ------------------------------------------------------------------ a user's choice

def clean(raw: object) -> dict:
    """A valid appearance (unknown values fall back to the defaults)."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = None
    raw = raw if isinstance(raw, dict) else {}
    return {"mode": raw.get("mode") if raw.get("mode") in MODES else DEFAULT["mode"],
            "accent": raw.get("accent") if raw.get("accent") in ACCENTS else DEFAULT["accent"],
            "font": raw.get("font") if raw.get("font") in FONTS else DEFAULT["font"]}


def save(user, raw: dict) -> dict:
    """Store a user's choice (every value must be a known one: 400 otherwise)."""
    from app.errors import AppError

    if raw.get("mode") not in MODES or raw.get("accent") not in ACCENTS or raw.get("font") not in FONTS:
        raise AppError(400, "invalid_appearance", "اختيار غير صالح.")
    value = {"mode": raw["mode"], "accent": raw["accent"], "font": raw["font"]}
    user.appearance = json.dumps(value)
    return value


# ------------------------------------------------------------------ static/css/accents.css

def _vars(v: dict[str, str]) -> str:
    return (f"--accent: {v['accent']}; --accent-2: {v['accent-2']}; --on-accent: {v['on-accent']}; "
            f"--accent-text: {v['accent-text']};")


def css() -> str:
    p = palette()
    lines = ["/* GENERATED by `python -m app.services.appearance --write` from app/services/appearance.py — do not edit. */",
             "/* Dark is the default; light applies with the system setting (unless the user chose dark) or by choice. */"]
    blocks = (  # (default selector for the first accent, selector pattern, mode, indent)
        (":root, ", ':root[data-accent="{n}"]', "dark", ""),
        (':root:not([data-theme="dark"]), ', ':root:not([data-theme="dark"])[data-accent="{n}"]', "light", "  "),
        (':root[data-theme="light"], ', ':root[data-theme="light"][data-accent="{n}"]', "light", ""),
    )
    for k, (first, pattern, mode, indent) in enumerate(blocks):
        if k == 1:
            lines.append("@media (prefers-color-scheme: light) {")
        for i, name in enumerate(ACCENTS):
            lead = first if i == 0 else ""
            lines.append(f"{indent}{lead}{pattern.format(n=name)} {{ {_vars(p[name][mode])} }}")
        if k == 1:
            lines.append("}")
    return "\n".join(lines) + "\n"


CSS_PATH = Path(__file__).resolve().parents[2] / "static" / "css" / "accents.css"


if __name__ == "__main__":
    if "--write" in sys.argv:
        CSS_PATH.write_text(css())
        print(f"wrote {CSS_PATH}")
    else:
        for name, modes in palette().items():
            for mode, v in modes.items():
                stops = (v["accent"], v["accent-2"])
                print(f"{name:8} {mode:5} on={v['on-accent']} "
                      f"min={min(contrast(v['on-accent'], s) for s in stops):.2f} text={v['accent-text']} "
                      f"bg={contrast(v['accent-text'], SURFACES[mode][0]):.2f} card={contrast(v['accent-text'], SURFACES[mode][1]):.2f}")
