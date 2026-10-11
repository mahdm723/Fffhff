"""/download: the public page to install the Android app (and add it to an iPhone home
screen), plus /api/app/version for the in-app "update available" check."""

from __future__ import annotations

import html
import re

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app import i18n
from app.i18n import N_
from app.api.deps import get_state
from app.api.policies import lang_switch, respond
from app.services import app_release, brand

router = APIRouter(tags=["download"])


@router.get("/api/app/version")
def app_version() -> dict:
    data = app_release.info()
    return {"available": data is not None, **(data or {})}


def _qr_svg(text: str, lang: str) -> str:
    import qrcode
    import qrcode.image.svg

    q = qrcode.QRCode(border=2, image_factory=qrcode.image.svg.SvgPathImage,
                      error_correction=qrcode.constants.ERROR_CORRECT_M)
    q.add_data(text)
    q.make(fit=True)
    svg = q.make_image().to_string(encoding="unicode")
    label = html.escape(i18n.tr("رمز QR لصفحة التحميل", lang))
    return svg.replace("<svg ", f'<svg role="img" aria-label="{label}" ', 1)


def page(request: Request, lang: str, *, notice: str = "", show_ios: bool = True, show_qr: bool = True,
         gate: bool = False) -> str:
    """The download page in lang. `notice`: an escaped HTML card shown first; `gate`: an access page of phase 14
    (no link back to the app it replaces, js/gate.js for the iPad check and the old offline cache)."""
    st = get_state(request)
    tr = i18n.tr
    base = (st.settings.PUBLIC_URL or str(request.base_url)).rstrip("/")
    page_url = f"{base}/download"
    rel = app_release.info()
    e = html.escape
    app_name = st.settings.APP_NAME
    name = e(app_name)
    apk_name = e((re.sub(r"[^A-Za-z0-9._-]", "", app_name) or "app") + ".apk")
    if rel:
        ver = e(tr("الإصدار {version}", lang, version=rel["version_name"])) if rel["version_name"] else e(tr("أحدث إصدار", lang))
        android = e(tr("أندرويد {version} أو أحدث", lang, version=str(rel["min_android"])))
        app_block = f"""
      <a class="dl-btn" href="{e(rel['url'])}" download="{apk_name}">{e(tr("تحميل تطبيق أندرويد (APK)", lang))}</a>
      <p class="dl-meta">{ver} · {e(i18n.number(rel['size_mb'], lang))} MB · {android}</p>
      <details class="dl-hash"><summary>{e(tr("بصمة الملف SHA-256 (للتحقق)", lang))}</summary><code dir="ltr">{e(rel['sha256'])}</code></details>"""
    else:
        app_block = f'<p class="dl-meta">{e(tr("تطبيق أندرويد غير متاح حاليًا. يمكنك استخدام الموقع مباشرة.", lang))}</p>'
    title = tr("تحميل {app}", lang, app=app_name)
    description = tr("حمّل تطبيق {app} لأندرويد أو أضفه إلى شاشة iPhone الرئيسية.", lang, app=app_name)
    file_step = e(tr("افتح الملف {file} من الإشعارات أو من «التنزيلات».", lang, file="\x00")).replace(
        "\x00", f'<b dir="ltr">{apk_name}</b>')
    site_step = e(tr("افتح {site} في Safari.", lang, site="\x00")).replace("\x00", f'<b dir="ltr">{e(base)}</b>')
    share_step = e(tr("اضغط زر المشاركة {icon} ثم «إضافة إلى الشاشة الرئيسية».", lang, icon="\x00")).replace(
        "\x00", '<span aria-hidden="true">⬆️</span>')
    ios = f"""
    <section class="dl-card">
      <h2>iPhone · iPad</h2>
      <ol class="dl-steps">
        <li>{site_step}</li>
        <li>{share_step}</li>
        <li>{e(tr("افتح {app} من الأيقونة الجديدة وسجّل الدخول.", lang, app=app_name))}</li>
      </ol>
    </section>""" if show_ios else ""
    qr = f"""
    <section class="dl-card dl-qr">
      <h2>{e(tr("على جهاز آخر؟", lang))}</h2>
      <p>{e(tr("امسح الرمز بكاميرا هاتفك لفتح هذه الصفحة:", lang))}</p>
      <div class="dl-qr__code">{_qr_svg(page_url, lang)}</div>
      <p class="dl-url" dir="ltr">{e(page_url)}</p>
    </section>""" if show_qr else ""
    steps = (tr("اضغط «تحميل» وانتظر انتهاء التحميل.", lang), None,
             tr("إن ظهرت رسالة «لأمانك، لا يُسمح بتثبيت تطبيقات من هذا المصدر»: اضغط «الإعدادات» ثم فعّل «السماح من هذا المصدر» وارجع.", lang),
             tr("اضغط «تثبيت». قد يعرض Play Protect تنبيهًا لأن التطبيق ليس من المتجر: اختر «التثبيت على أي حال».", lang),
             tr("إن طلب التطبيق إذن الإشعارات فاسمح به لتصلك رسائلك الجديدة. لا يطلب كاميرا ولا ميكروفون.", lang))
    items = "\n        ".join(f"<li>{file_step if s is None else e(s)}</li>" for s in steps)
    return f"""<!doctype html>
<html lang="{lang}" dir="{i18n.direction(lang)}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <title>{e(title)}</title>
  <meta name="description" content="{e(description)}">
  <meta name="referrer" content="no-referrer">
  {brand.HEAD_ICONS}
  {brand.og_tags(st.settings, base, title, description, lang)}
  <link rel="stylesheet" href="/css/download.css">
</head>
<body>
  <main class="dl">
    <header class="dl-head">
      <img class="dl-icon" src="/icons/icon-192.png" width="88" height="88" alt="">
      <h1 class="dl-name" dir="ltr" aria-label="{name}">{brand.wordmark_html(app_name)}</h1>
      <p class="dl-tag">{e(tr("مجتمع للمتداولين: أخبار العملات الرقمية والتحليلات والأفكار، وبياناتك محمية.", lang))}</p>
    </header>
{notice}
    <section class="dl-card">
      <h2>{e(tr("أندرويد", lang))}</h2>{app_block}
      <ol class="dl-steps">
        {items}
      </ol>
    </section>
{ios}{qr}

    <p class="dl-foot">{"" if gate else f'<a href="/">{e(tr("العودة إلى {app}", lang, app=app_name))}</a> · '}<a href="/policies/privacy">{e(tr("سياسة الخصوصية", lang))}</a> · <a href="/policies/terms">{e(tr("شروط الاستخدام", lang))}</a> · <a href="/policies/guidelines">{e(tr("إرشادات المجتمع", lang))}</a></p>
    <p class="dl-foot dl-langs">{lang_switch("/" if gate else "/download", lang)}</p>
  </main>{'<script src="/js/gate.js"></script>' if gate else ""}
</body>
</html>"""


@router.get("/download", include_in_schema=False)
def download_page(request: Request) -> HTMLResponse:
    return respond(request, page(request, i18n.current()))


# ---------------------------------------------------------------------------------------------- V6 phase 14: access


GATES = {  # decision → (title, text, show the iPhone steps, show the QR code)
    "android": (N_("حمّل التطبيق"),
                N_("على أندرويد يعمل {app} من التطبيق فقط. حمّله من الزر أدناه وثبّته، ثم افتحه وسجّل الدخول بحسابك نفسه."),
                False, False),
    "update": (N_("حدّث التطبيق"),
               N_("يتوفر إصدار جديد من {app} وهو إلزامي. حمّله وثبّته فوق التطبيق الحالي: يبقى حسابك وبياناتك كما هي."),
               False, False),
    "unverified": (N_("تعذّر التحقق من التطبيق"),
                   N_("لم نتمكن من التحقق من هذا التطبيق على جهازك. تأكد من اتصالك بالإنترنت وأعد المحاولة. إن تكرر ذلك فحمّل التطبيق من جديد من هذه الصفحة."),
                   False, False),
    "desktop": (N_("{app} للهواتف"),
                N_("{app} مصمم للهواتف. على أندرويد: حمّل التطبيق. على iPhone أو iPad: افتح الموقع في Safari. امسح الرمز أدناه بكاميرا هاتفك."),
                True, True),
}


def gate_page(request: Request, verdict: str, lang: str) -> str:
    title, text, show_ios, show_qr = GATES[verdict]
    app_name = get_state(request).settings.APP_NAME
    e = html.escape
    retry = (f'\n      <button type="button" class="dl-btn dl-btn--ghost" id="gate-retry" hidden>'
             f'{e(i18n.tr("إعادة المحاولة", lang))}</button>') if verdict == "unverified" else ""
    notice = f"""    <section class="dl-card dl-gate" data-gate="{verdict}">
      <h2>{e(i18n.tr(title, lang, app=app_name))}</h2>
      <p>{e(i18n.tr(text, lang, app=app_name))}</p>{retry}
    </section>"""
    return page(request, lang, notice=notice, show_ios=show_ios, show_qr=show_qr, gate=True)


def gate_response(request: Request, verdict: str) -> HTMLResponse:
    out = respond(request, gate_page(request, verdict, i18n.current()))
    out.headers["Cache-Control"] = "no-store"
    out.headers["Vary"] = "Cookie, User-Agent, Accept-Language"
    return out
