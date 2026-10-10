"""The new logo, stage A (concepts): read-only data for the panel's «الهوية» tab — the three concept SVGs from
brand/concepts/, the brand colours with their measured contrast, and a proposal (not applied) to align the app's
default accent with the brand red. Super admin only; nothing here changes the app."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request

from app.api.admin import SUPER_ADMIN, AdminContext
from app.services import appearance as ap

router = APIRouter(prefix="/api/admin", tags=["admin-brand"])

BRAND_DIR = Path(__file__).resolve().parents[2] / "brand"

# The brand: black, white and one red (the dot of DALTA.BIT). The red reads on both (≥ 4:1).
COLORS = {"black": "#0B0B0D", "white": "#FFFFFF", "red": "#E5232D"}

CONCEPTS = (
    {
        "id": "delta",
        "title": "Δ مع نقطة حمراء",
        "idea": "Δ (دلتا) رمز التغيّر وحركة السعر، ويُنطق مثل DALTA. مثلث مفرَّغ بزوايا مستديرة، والنقطة الحمراء في مركزه كأنها السعر الآن.",
        "notes": [
            "واضح حتى 16px. النقطة تصغر تحت 24px لكنها تبقى ظاهرة.",
            "مثلث مفرَّغ وليس مصمتًا، فلا يشبه شعار Vercel، وليس أحمر كله فلا يشبه Delta Air Lines.",
            "رموز «دلتا» شائعة في المال والكريبتو، مثل تطبيق Delta لمتابعة المحافظ وDelta Exchange. يُنصح ببحث في العلامات التجارية قبل الاعتماد.",
        ],
    },
    {
        "id": "d-dot",
        "title": "D مع نقطة حمراء (D.)",
        "idea": "حرف D هندسي ثقيل تتبعه النقطة الحمراء، كما في DALTA.BIT. أبسط المفاهيم وأقربها إلى الاسم المكتوب.",
        "notes": [
            "الأوضح في الأحجام الصغيرة (16 و24px) وفي أيقونة الإشعارات.",
            "يتحول مباشرة إلى wordmark، فالنقطة نفسها في الرمز والاسم.",
            "حرف مع نقطة شكل عام، لكنه لا يطابق شعار منصة معروفة. Dash مثلًا يستعمل D مائلًا بخطوط سرعة زرقاء.",
        ],
    },
    {
        "id": "candles",
        "title": "Δ من شموع يابانية",
        "idea": "خمس شموع على قاعدة واحدة، قممها تتدرّج على ضلعي Δ. النقطة الحمراء قمة الشمعة الوسطى: نقطة السعر. الأقرب إلى عالم التداول.",
        "notes": [
            "جميل في 96px فما فوق.",
            "الأضعف تحت 48px: تندمج الشموع ويقترب الشكل من أعمدة مخطط. أنسب للأحجام الكبيرة منه لأيقونة الإشعارات.",
            "أصلي: لا يطابق رمزًا رسميًا لأي منصة.",
        ],
    },
)

# The proposal (not applied): replace the coral default accent by a red one built on the brand red.
PROPOSED_ACCENT = {"label": "أحمر العلامة", "dark": ("#E5232D", "#B5161F"), "light": ("#D7262E", "#A8141C")}


def _accent(stops: tuple[str, str], mode: str) -> dict:
    return {"accent": stops[0], "accent-2": stops[1], "on-accent": ap.on_accent(stops),
            "accent-text": ap.accent_text(stops[0], mode),
            "on_contrast": round(min(ap.contrast(ap.on_accent(stops), s) for s in stops), 2)}


def preview_data() -> dict:
    concepts = []
    for c in CONCEPTS:
        svg = (BRAND_DIR / "concepts" / f"{c['id']}.svg").read_text(encoding="utf-8")
        concepts.append({**c, "svg": svg})
    red, black, white = COLORS["red"], COLORS["black"], COLORS["white"]
    surfaces = {mode: {"bg": bg, "card": card} for mode, (bg, card) in ap.SURFACES.items()}
    current = ap.ACCENTS[ap.DEFAULT_ACCENT]
    return {
        "concepts": concepts,
        "colors": COLORS,
        "contrast": {
            "red_on_black": round(ap.contrast(red, black), 2), "red_on_white": round(ap.contrast(red, white), 2),
            "red_on_dark_app": round(ap.contrast(red, ap.SURFACES["dark"][0]), 2),
            "red_on_light_app": round(ap.contrast(red, ap.SURFACES["light"][0]), 2),
        },
        "surfaces": surfaces,
        "accent_now": {"label": current[0], "dark": _accent(current[1], "dark"), "light": _accent(current[2], "light")},
        "accent_proposed": {"label": PROPOSED_ACCENT["label"], "dark": _accent(PROPOSED_ACCENT["dark"], "dark"),
                            "light": _accent(PROPOSED_ACCENT["light"], "light")},
    }


@router.get("/brand")
def brand_preview(request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    return preview_data()
