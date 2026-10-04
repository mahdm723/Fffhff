"""/download: the public page to install the Android app (and add DZPLAY to an iPhone home
screen), plus /api/app/version for the in-app "update available" check."""

from __future__ import annotations

import html

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.api.deps import get_state
from app.services import app_release

router = APIRouter(tags=["download"])


@router.get("/api/app/version")
def app_version() -> dict:
    data = app_release.info()
    return {"available": data is not None, **(data or {})}


def _qr_svg(text: str) -> str:
    import qrcode
    import qrcode.image.svg

    q = qrcode.QRCode(border=2, image_factory=qrcode.image.svg.SvgPathImage,
                      error_correction=qrcode.constants.ERROR_CORRECT_M)
    q.add_data(text)
    q.make(fit=True)
    svg = q.make_image().to_string(encoding="unicode")
    return svg.replace("<svg ", '<svg role="img" aria-label="رمز QR لصفحة التحميل" ', 1)


@router.get("/download", include_in_schema=False)
def download_page(request: Request) -> HTMLResponse:
    st = get_state(request)
    base = (st.settings.PUBLIC_URL or str(request.base_url)).rstrip("/")
    page_url = f"{base}/download"
    rel = app_release.info()
    e = html.escape
    if rel:
        ver = f"الإصدار {e(rel['version_name'])}" if rel["version_name"] else "أحدث إصدار"
        app_block = f"""
      <a class="dl-btn" href="{e(rel['url'])}" download="DZPLAY.apk">تحميل تطبيق أندرويد (APK)</a>
      <p class="dl-meta">{ver} · {rel['size_mb']} MB · أندرويد {e(str(rel['min_android']))} أو أحدث</p>
      <details class="dl-hash"><summary>بصمة الملف SHA-256 (للتحقق)</summary><code dir="ltr">{e(rel['sha256'])}</code></details>"""
    else:
        app_block = '<p class="dl-meta">تطبيق أندرويد غير متاح حاليًا. يمكنك استخدام الموقع مباشرة.</p>'
    body = f"""<!doctype html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <title>تحميل DZPLAY</title>
  <meta name="description" content="حمّل تطبيق DZPLAY لأندرويد أو أضفه إلى شاشة iPhone الرئيسية.">
  <meta name="referrer" content="no-referrer">
  <link rel="icon" href="/icons/icon-192.png">
  <link rel="stylesheet" href="/css/download.css">
</head>
<body>
  <main class="dl">
    <header class="dl-head">
      <img class="dl-icon" src="/icons/icon-192.png" width="88" height="88" alt="">
      <h1>DZPLAY</h1>
      <p class="dl-tag">شارك مشاعرك مع شخص آخر — رسائل ومكالمات بدون أن ينكشف رقمك.</p>
    </header>

    <section class="dl-card">
      <h2>أندرويد</h2>{app_block}
      <ol class="dl-steps">
        <li>اضغط «تحميل» وانتظر انتهاء التحميل.</li>
        <li>افتح الملف <b dir="ltr">DZPLAY.apk</b> من الإشعارات أو من «التنزيلات».</li>
        <li>إن ظهرت رسالة «لأمانك، لا يُسمح بتثبيت تطبيقات من هذا المصدر»: اضغط «الإعدادات» ثم فعّل «السماح من هذا المصدر» وارجع.</li>
        <li>اضغط «تثبيت». قد يعرض Play Protect تنبيهًا لأن التطبيق ليس من المتجر: اختر «التثبيت على أي حال».</li>
        <li>عند أول مكالمة سيطلب التطبيق إذن الميكروفون والكاميرا، وفي أندرويد 13+ إذن الإشعارات.</li>
      </ol>
    </section>

    <section class="dl-card">
      <h2>iPhone</h2>
      <ol class="dl-steps">
        <li>افتح <b dir="ltr">{e(base)}</b> في Safari.</li>
        <li>اضغط زر المشاركة <span aria-hidden="true">⬆️</span> ثم «إضافة إلى الشاشة الرئيسية».</li>
        <li>افتح DZPLAY من الأيقونة الجديدة وسجّل الدخول.</li>
      </ol>
    </section>

    <section class="dl-card dl-qr">
      <h2>على جهاز آخر؟</h2>
      <p>امسح الرمز بكاميرا هاتفك لفتح هذه الصفحة:</p>
      <div class="dl-qr__code">{_qr_svg(page_url)}</div>
      <p class="dl-url" dir="ltr">{e(page_url)}</p>
    </section>

    <p class="dl-foot"><a href="/">العودة إلى DZPLAY</a> · <a href="/policies/privacy">سياسة الخصوصية</a> · <a href="/policies/terms">شروط الاستخدام</a> · <a href="/policies/guidelines">إرشادات المجتمع</a></p>
  </main>
</body>
</html>"""
    return HTMLResponse(body, headers={"Cache-Control": "no-cache"})
