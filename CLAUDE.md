# DZPLAY

> الاسم مؤقت: يُغيَّر لاحقًا من إعداد واحد (`APP_NAME`).

## المبدأ
مساحة لمشاركة الأفكار والآراء ومتابعة السوق، مع حماية بيانات المستخدم: لا يُعرض بريده أو أي بيانات خاصة عنه لأي مستخدم آخر، إلا ما يختار إظهاره بنفسه (الاسم، الصورة، المنشورات).

## قواعد ثابتة
- الأسرار في .env فقط. كل الحدود والقيم في configuration، والمهمة منها قابلة للتعديل من لوحة القيادة.
- كل صلاحية تُتحقق server-side. لا ثقة في العميل.
- لا تكسر أي ميزة موجودة إلا ما طُلب حذفه صراحة.
- الملفات (صور) تمر بالفحص ثم تُخزَّن في Telegram، والخادم ممر فقط.
- كل مبلغ مالي يُدار بنظام ledger (حركات غير قابلة للتعديل، والرصيد = مجموعها). لا تعديل مباشر لأي رصيد.
- كل إجراء أدمن يُسجَّل في audit log.
- النصوص غير الأساسية (السياسات، الشروط، الشروحات) تأتي من نظام المحتوى القابل للتعديل من لوحة القيادة. أسماء الصفحات والأزرار والإعدادات تبقى في الكود.
- واجهة عربية RTL، Mobile First.

## التقنيات وأوامر التشغيل والاختبار

### البنية
| الجزء | المكان | التقنية |
|---|---|---|
| الخادم | `dzplay/app/` | Python 3.11، FastAPI، SQLAlchemy 2.1، Pydantic Settings |
| قاعدة البيانات | `dzplay/app/models.py` | PostgreSQL 16 في الإنتاج، SQLite في الاختبارات |
| Redis (اختياري) | — | hub للـWebSocket بين النسخ، rate limiting، طابور عامل الوسائط |
| الواجهة | `dzplay/static/` | PWA بـJavaScript modules بلا إطار عمل، و`sw.js` للتخزين المؤقت |
| لوحة القيادة | `dzplay/app/admin_static/` + `app/api/admin*.py` | تحت مسار سري `ADMIN_PATH`، مع كلمة مرور + TOTP |
| عامل الوسائط | `dzplay/app/media_worker.py` | حاوية معزولة (uid 10002، بلا إنترنت) تعيد ترميز الصور وتفحصها (Pillow، ffmpeg، ONNX) |
| Android | `dzplay/android/` | غلاف WebView أصلي (Java)، `applicationId io.dzplay.app` |
| النشر | `dzplay/deploy/`، `dzplay/docker-compose.yml`، `dzplay/Caddyfile` | Docker Compose: caddy، app، media-worker، db، redis (+ coturn حتى المرحلة 1 من V6) |

**داخل `dzplay/app/`:**
- `api/*.py`: الـrouters. المنطق في `services/*.py`، ولا منطق داخل الـrouter.
- `state.py`:
  - `AppState`، ويحمل الإعدادات وقاعدة البيانات وTelegram والـhub.
  - `Effects`: الإشارات والإشعارات، و`effects.later(fn, …)` لمهام الخلفية بعد الـcommit.
- `services/tunables.py`: كل حد قابل للتعديل من اللوحة دون إعادة تشغيل (يُخزَّن في `AppSetting` باسم `tun.<KEY>`).
- `services/runtime_config.py`: أسرار تُضبط من اللوحة (SMTP، بوت Telegram)، مختومة بـ`SECRET_KEY`.
- `migrations.py`: ترحيلات إضافية فقط (`add_missing_columns` + `backfill`). كل عمود جديد nullable، ولا حذف بيانات بدون نسخة احتياطية.
- `admin_cli.py`: أوامر الإدارة (`create-admin`، `admin-link`، `alert`، `set-webhook`، …).

**الواجهة:**
- CSP صارم `'self'`: لا inline scripts ولا `style=""`. العرض يُضبط بـ`el.style.width` من JS.
- `replaceChildren(...)` و`append(...)` مع `.filter(Boolean)` (لأن `null` يُطبع نصًا).
- كل تغيير في ملفات الواجهة يتطلب رفع `VERSION` في `static/sw.js`.

### التشغيل محليًا
```bash
cd dzplay
python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env          # ثم SECRET_KEY: .venv/bin/python -m app.admin_cli gen-secret
.venv/bin/uvicorn app.main:app --reload --port 8000
```

### الاختبارات
```bash
cd dzplay
.venv/bin/python -m pytest -q                                   # SQLite (أكثر من 330 اختبارًا)
DZ_TEST_DATABASE_URL=postgresql+psycopg://USER:PW@HOST:PORT/DB \
DZ_TEST_REDIS_URL=redis://HOST:PORT/0 .venv/bin/python -m pytest -q   # PostgreSQL + Redis
.venv/bin/pip-audit -r requirements.txt                         # التبعيات
```

**اختبارات المتصفح (Playwright، شاشة هاتف):**
```bash
PW_CHROMIUM=/opt/pw-browsers/chromium-1194/chrome-linux/chrome .venv/bin/python e2e/run_e2e.py
```
وكذلك باقي ملفات `e2e/run_*_e2e.py`.

- Chromium الخاص بـPlaywright لا يفك H.264، لذلك تستعمل الاختبارات مقاطع WebM.
- الخادم في الاختبار يحتاج `FFMPEG_BINARY` و`FFPROBE_BINARY` (`tests.conftest.ffmpeg_binary()` / `ffprobe_shim()`).

**مع Docker:**
- `e2e/run_backup_e2e.sh`: نسخة احتياطية ← مسح ← استعادة.
- `e2e/run_media_stack_e2e.sh`: عزل عامل الوسائط.
- `STACK_EXTRA=dir` لبناء بلا إنترنت.

### النشر على الـVPS
```bash
curl -fsSL https://raw.githubusercontent.com/mahdm723/Fffhff/claude/github-access-check-c1exvo/dzplay/deploy/install.sh | sudo bash
```
- **نفس الأمر للتحديث:** يحافظ على `.env` والبيانات، ويثبّت النسخ الاحتياطي اليومي والمراقبة.
- **الأمان:**
  - `deploy/harden.sh`: ufw وfail2ban والتحديثات التلقائية.
  - `deploy/ssh-harden.sh`: SSH بالمفاتيح فقط، ويرفض التطبيق قبل إثبات دخول بالمفتاح. **لا يُطبَّق قبل موافقة المالك.**
  - `deploy/monitor.sh`: تنبيهات Telegram كل 5 دقائق.
- **التطبيق:**
  - الكود في `/opt/dzplay/dzplay`.
  - السجلات: `docker compose logs -f app`.
  - أوامر الإدارة: `docker compose exec app python -m app.admin_cli …`.

### Android
- **البناء:** CI عند أي تغيير في `dzplay/android/**` (`.github/workflows/android-apk.yml`). الـAPK غير الموقّع يُدفع إلى فرع `android-build`.
- **التوقيع:** خارج المستودع بنفس المفتاح دائمًا (alias `dzplay`، شهادة SHA-256 تبدأ بـ`46:15:BE`). **المفتاح لا يُرفع إلى git أبدًا، ولا يتغيّر `applicationId`.**
- **النشر:**
  - الـAPK الموقّع في `dzplay/static/download/dzplay.apk`، مع `version.json` (`version_code`/`version_name`/`notes`).
  - رقم الإصدار في `dzplay/android/gradle.properties`.

### Git
- كل العمل على الفرع `claude/github-access-check-c1exvo`، بـcommits واضحة ووسم لكل مرحلة (`v6-phase-N`).
- في نهاية كل مرحلة: الاختبارات، ثم تقرير قصير، ثم التوقف حتى موافقة المالك.
- الخطة الحالية: [PLAN.md](PLAN.md).
