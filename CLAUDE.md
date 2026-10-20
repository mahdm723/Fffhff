# DALTA.BIT (سابقًا DZPLAY)

> الاسم من إعداد واحد (`APP_NAME`): الواجهة، واللوحة، والسياسات، والبريد، والبوت، وAndroid. المعرّفات الداخلية (`io.dzplay.app`، `/opt/dzplay`، الكوكيز، `DZPLAYAndroid`) لا تتغير.

## المبدأ
مجتمع للمتداولين ولمشاركة أخبار العملات الرقمية والتحليلات والأفكار، مع حماية بيانات المستخدم: لا يُعرض بريده أو أي بيانات خاصة عنه إلا ما يختار إظهاره بنفسه (الاسم، الصورة، المنشورات).

- **العضوية** تمنح ميزات داخل المنصة فقط، ولا علاقة لها بالتداول أو بأي عائد مالي.
- **المكافآت** (الإحالة، المسابقات، المكافآت الجماعية) ترويجية ومنفصلة تمامًا عن العضوية.

## قواعد ثابتة
- الأسرار في .env فقط. كل الحدود والقيم في configuration، والمهمة منها قابلة للتعديل من لوحة القيادة.
- كل صلاحية تُتحقق server-side. لا ثقة في العميل.
- لا تكسر أي ميزة موجودة إلا ما طُلب حذفه صراحة.
- الملفات (صور) تمر بالفحص ثم تُخزَّن في Telegram، والخادم ممر فقط.
- كل مبلغ مالي يُدار بنظام ledger (حركات غير قابلة للتعديل، والرصيد = مجموعها). لا تعديل مباشر لأي رصيد.
  - حسابان منفصلان: `membership` (دفع العضوية واسترجاعها) و`rewards` (المكافآت والسحب).
  - كل كتابة تمر بخدمة واحدة، مع قيد فريد للـidempotency وقفل على مستوى المستخدم.
- لا يُعرض أي ربح أو عائد في شاشة العضوية، ولا تدخل العضوية أو الدفع في معايير أي مكافأة.
- لا حسابات مُدارة متخفية ولا تعزيز تفاعل بأي شكل: الأعداد الظاهرة حقيقية فقط.
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
| عامل الوسائط | `dzplay/app/media_worker.py` | حاوية معزولة (uid 10002، بلا إنترنت) تعيد ترميز الصور وتفحصها (Pillow، ONNX) |
| Android | `dzplay/android/` | غلاف WebView أصلي (Java)، `applicationId io.dzplay.app` |
| النشر | `dzplay/deploy/`، `dzplay/docker-compose.yml`، `dzplay/Caddyfile` | Docker Compose: caddy، app، media-worker، db، redis |

**داخل `dzplay/app/`:**
- `api/*.py`: الـrouters. المنطق في `services/*.py`، ولا منطق داخل الـrouter.
- `state.py`:
  - `AppState`، ويحمل الإعدادات وقاعدة البيانات وTelegram والـhub.
  - `Effects`: الإشارات والإشعارات، و`effects.later(fn, …)` لمهام الخلفية بعد الـcommit.
- `services/tunables.py`: كل حد قابل للتعديل من اللوحة دون إعادة تشغيل (يُخزَّن في `AppSetting` باسم `tun.<KEY>`).
- `services/runtime_config.py`: أسرار تُضبط من اللوحة (SMTP، بوت Telegram)، مختومة بـ`SECRET_KEY`.
- `migrations.py`: ترحيلات إضافية فقط (`add_missing_columns` + `backfill`). كل عمود جديد nullable، ولا حذف بيانات بدون نسخة احتياطية.
- `admin_cli.py`: أوامر الإدارة (`create-admin`، `admin-link`، `alert`، `set-webhook`، `market-check`، `stars-count`، `legacy-status`، `export-legacy`، `drop-legacy`، …).
- `services/legacy_v6.py`: تصدير بيانات الميزات المحذوفة في V6 ثم حذفها (SQL خام، بلا models)؛ يشغّله `deploy/v6-cleanup.sh`.
- **V6 (المراحل 2–5ج):**
  - `services/market.py`: السوق من Bybit. الخادم وحده يتصل، و`market_snapshots` مشترك بين النسخ.
  - `services/notify.py`: الإشعارات (`notifications`)، تُكتب في نفس المعاملة وإشارة `notify` بعدها.
  - `services/ledger.py`: **الكاتب الوحيد للمبالغ**.
    - حسابا `membership` و`rewards`؛
    - قيد `ux_ledger_source` للـidempotency، و`lock_user` قبل أي فحص رصيد؛
    - أنواع الحركات لا تستعمل أبدًا أنواع V5 المحذوفة (`earning|payout|adjustment|reversal`).
  - `services/email_codes.py`: رموز البريد (`refund`، `withdraw`، `giveaway`).
  - `services/mail.py`: البريد الصادر بملفين، `system` (رسائل آلية بتذييل «لا ترد») و`support` (التذاكر). النصوص من `mail_templates.py` (`email.*`)، والمتغيرات بـregex لا بـ`str.format`.
  - `services/membership.py`: العضوية، والاسترجاع، وأزرار Telegram `ms:`.
  - `services/rewards.py` و`withdrawals.py`: «أرباحي» والدعوات والسحب.
  - `services/giveaway.py`: الظرف الأحمر.
  - `services/cms.py` + `markdown.py`: نظام المحتوى (`content_blocks`/`content_revisions`). النصوص الافتراضية في الكود، و`{{متغيرات}}` من الإعدادات، والمحوّل يهرّب كل شيء. إعادة الموافقة عبر `cms.ack_version`.
  - `services/appearance.py`: الألوان، ومصدرها الوحيد. `python -m app.services.appearance --write` يولّد `static/css/accents.css`، واختبار يتحقق من التباين ≥ 4.5.
  - `services/brand.py` (الخادم) و`static/js/brand.js` (الواجهة): اسم التطبيق. لا تكتب الاسم في الكود.
  - **الشعار (11c):**
    - المصدر رسمتان فقط: `brand/logo-large.svg` (48px فما فوق) و`brand/logo-small.svg` (16–24px). كل الأيقونات (Android، وPWA، وfavicon، وog:image، وصورة البوت، و`static/js/logo-shapes.js`) تُولَّد منهما بـ`python brand/build.py`، ولا تُعدَّل يدويًا. `--check` تستعمله الاختبارات.
    - القواعد في `brand/BRAND.md`: لا يُعكس الشعار أبدًا، والنقطة متوهجة إلا في أيقونة الإشعارات وAndroid 13 (مسطحتان).
    - في الواجهة: `logoMark()` / `logoLockup()` في `ui.js`. النبض بـ`.logo-mark--pulse`، ويتوقف مع `prefers-reduced-motion`.
  - اللوحة: `admin_static/admin-money.js` + `api/admin_money.py`.
  - **فحص الدفع على البلوكشين (المرحلة 13):**
    - `services/chain.py`: قراءة فقط. TronGrid لـTRC20، وJSON-RPC لـBEP20، وعقدا USDT الرسميان ثابتان.
    - `services/payment_check.py`: لكل TXID حالة في `membership_txs`، ونتيجة الطلب في `membership_requests.check_*`.
    - **يعرض النتيجة فقط ولا يغيّر `status` أبدًا**، إلا عبر `services/membership_auto.py` حين يكون `AUTO_APPROVE_MEMBERSHIP` مفعّلًا (معطّل افتراضيًا). القرار للمالك (قبول/رفض/حظر)، والاختبارات تفرض ذلك.
    - **المنح التلقائي (المرحلة 16):** `membership_auto.try_approve` بعد `aggregate`. الشروط: كل TXID `ok`، والنتيجة `match`/`over` (الزائد يُعلَّم للمراجعة)، والحساب نشط وغير مشبوه، والعدد دون `AUTO_APPROVE_DAILY_MAX`. حجز ذري (`auto_state`)، ثم `membership.decide` نفسها. «سحب العضوية» (`ms:revoke` واللوحة) = `membership.end` + عكس مكافأة الدعوة، بلا استرجاع تلقائي.
    - `services/suspicion.py`: علامة «مشبوه» من اللوحة على الحساب أو الجهاز، وجهاز مشترك مع حساب محظور/موقوف، وبلاغ أو رصد مفتوح، وقفل إقلاع مفتوح. **مشاركة الشبكة أو IP ليست إشارة أبدًا.**
    - الفحص المتكرر في `main._chain_loop` (حجز ذري لكل صف). الشبكة تُقرأ خارج أي معاملة.
    - في الاختبارات: `chain_transport` (MockTransport)، و`CHAIN_CHECK_ENABLED=False` افتراضيًا، و`e2e/fake_chain.py`.
  - **الوصول للتطبيق (المرحلة 14):**
    - `services/access.py`: من يفتح التطبيق. أندرويد من الـAPK فقط، وiPhone/iPad من المتصفح (iPad يُعرف باللمس عبر `js/gate.js` وكوكي `dz_touch`)، والكمبيوتر حسب `WEB_DESKTOP_ACCESS`. اللوحة و`/download` والسياسات و`/api/app/*` مفتوحة دائمًا.
    - البوابة في `security_middleware` و`/api/ws`: الصفحات ← صفحة وصول من `download.gate_page`، والـAPI ← 403 `app_required`.
    - إثبات الـAPK: `security/attestation.py` (Key Attestation حتى جذور Google في `attestation_roots.py`، والحزمة وبصمة شهادة التوقيع) و`api/app_access.py` ← توكن موقّع في كوكي `dz_app`. الـUser-Agent وحده لا يكفي أبدًا.
    - ربط الجهاز: `app_devices`، و`user_devices`، و`users.registered_device_id`، والحد `MAX_ACCOUNTS_PER_DEVICE`.
    - صمام الأمان `ANDROID_APP_GATE` من اللوحة. كل تحديث للتطبيق إلزامي (`version.json` → `min_version_code`).
    - في الاختبارات: `WEB_DESKTOP_ACCESS=True` (المتصفحات فيها حواسيب)، وسلاسل شهادات مصطنعة من جذر اختباري (`tests/test_access.py`).
  - **تأكيد البريد عند التسجيل (المرحلة 15):**
    - `services/signup.py`: مع `REGISTRATION_EMAIL_VERIFICATION` لا يُنشأ الحساب إلا بعد رمز من 6 أرقام يصل إلى البريد. قبله صف في `signup_pending` (بصمات فقط)، ثم `auth.create_user` (نفس مسار `auth.validate_registration`).
    - **المرحلة 18:** الرموز تخرج من طابور في الخلفية (`services/mail_queue.py`) لا من الطلب.
      - الجواب واحد لبريد جديد ولبريد مسجّل (`validate_registration(allow_taken=True)`): البريد المسجّل يصله «لديك حساب بالفعل» ولا رمز.
      - `REGISTRATION_CODE_AUTO` (مفعّل): الرمز بالبريد مع ملاحظة للمالك. موقوف: الرمز للمالك في Telegram (`sg:sent|cancel`).
      - فشل البريد ← المسار اليدوي في Telegram + تنبيه. 503 «التسجيل غير متاح مؤقتًا» فقط بلا بريد ولا بوت.
      - الاستعادة: `RESET_CODE_AUTO` (موقوف افتراضيًا) ← `password_reset.auto_send` من الطابور، وفشله يعيد الطلب إلى أزرار `rgen`/`rdeny`.
    - `users.email_canonical`: صندوق واحد = حساب واحد (Gmail بلا نقاط، وبلا `+tag`). `users.signup_verified_via`: `email` / `admin_code` / `google`.
    - رمز إنشاء الحساب الإداري: في `app_settings` (`signup.admin_code`، argon2)، يُضبط من اللوحة بـ2FA، وكل استعمال في سجل التدقيق وTelegram.
    - في الاختبارات: `REGISTRATION_EMAIL_VERIFICATION=False` افتراضيًا، و`tests/test_signup_verify.py` و`e2e/run_signup_e2e.py` يفعّلانه.
- **الصور والعضوية (المرحلة 17):**
  - نشر الصور مع الأفكار إذن لكل حساب (`users.can_post_images`) يمنحه المدير فقط (`services/image_permission.py`، فردي أو جماعي، مع التدقيق). لا علاقة له بالعضوية.
  - ميزات العضوية في `membership.FEATURES`، ولكل ميزة مفتاح `MEMBER_FEATURE_*` من اللوحة. الإيقاف يزيلها عن الجميع فورًا.
  - النجمة تُحسب بـ`badges.star(user)` في كل payload، ولا تُقرأ `verified_at` مباشرة. نجمة العضوية تتبع ميزة `star`، والنجمة اليدوية تبقى.
  - صور المحادثات متوقفة نهائيًا: 410 `chat_images_disabled` في Caddy و`security_middleware` قبل قراءة الجسم.
  - `admin_cli purge-chat-images` (عبر `deploy/chat-images-purge.sh` بعد نسخة احتياطية) يحذف القديمة من الخادم وقناة التخزين، إلا المبلَّغ عنها (`legal_hold`).
- **الحساب الرسمي DALTA.BIT (المرحلة 19):**
  - صف واحد `is_official`، يُنشأ ويُعاد تسميته عند الإقلاع (`admin_content.ensure_official`). اسمه دائمًا `APP_NAME` (`names.shown_name`). بلا كلمة مرور ولا دخول.
  - ينشر من اللوحة فقط (`api/admin_official.py`، برمز 2FA عبر `admin_auth.verify_step_up`) أو من البوت (`/post` أو صورة بوصف ← `official_drafts` ← أزرار `op:yes|no`). المنطق في `services/official.py`، وكل إجراء في التدقيق.
  - صوره تمر بنفس الفحص والتخزين (`media_items.begin_official_upload`)، بلا حد يومي (`OFFICIAL_POSTS_PER_DAY`، 0 = بلا حد).
  - التثبيت: `posts.pinned_until`، منشور واحد، أول الصفحة الأولى من الأفكار، وينتهي وحده.
  - يظهر أولًا في «المستخدمون»، وملفه يُفتح، والرسائل إليه 403 `official_no_dm` (ورقة بزر «الدعم والمساعدة»).
  - خارج المال: `ledger.post` يرفضه (`official_account`)، و`verification.grant` كذلك.
  - الاسم محجوز بكل أشكاله (`names.brand_like`: داخل الاسم، بعد `skeleton`)، ورموز الشارات (نجوم، علامات صح، أوسمة، تيجان) مرفوضة في الأسماء (`badge_symbol_name`).
  - `badges.official(user)` في كل payload بجانب `verified`.
- **السعة واختبار الحمل (المرحلة 21):**
  - `services/metrics.py`:
    - أزمنة كل نقطة (قالب المسار، لا رابط ولا عنوان) بهستوغرام، تُجمع في Redis كل `METRICS_FLUSH_SECONDS` من كل العمال؛
    - عدد WebSocket المفتوحة؛
    - تُعرض في «صحة النظام» (`services/health.py`، `GET /api/admin/health`)؛
    - تنبيه `admin_cli health-check` ← `monitor.sh` (`HEALTH_P95_MS`/`HEALTH_ERROR_PCT` من اللوحة).
  - **العمال (`WEB_CONCURRENCY`):**
    - كل حلقة خلفية تأخذ `leader.take` (قفل `pg_try_advisory_xact_lock`) فتعمل في عامل واحد؛
    - الإقلاع (الجداول والحساب الرسمي) تحت `database.startup_lock()`؛
    - السوق للبقية من `market.load_shared`.
  - **قاعدة البيانات:** `DB_POOL_SIZE`/`DB_MAX_OVERFLOW`/`THREADPOOL_SIZE`، وتحذير عند الإقلاع إن لم تتسع لها `max_connections`.
  - **كلمات المرور (`security/passwords.py`):**
    - كل عملية Argon2 تحجز 64 MiB (معاملات RFC 9106 كما هي). بلا حد: 40 في وقت واحد = 2.5 GB، فأسقطت موجة الدخول التطبيق (OOM).
    - لذلك بوابة لكل عامل: `PASSWORD_HASH_CONCURRENCY` في وقت واحد (0 = الأنوية ÷ العمال)، و`PASSWORD_HASH_QUEUE` ينتظر حتى `PASSWORD_HASH_WAIT_SECONDS`.
    - ما زاد: 503 `busy` فورًا مع `Retry-After`، بلا خيط ولا ذاكرة.
    - التشفير نفسه في خيوط خاصة بأولوية منخفضة (`HASH_NICE`، وخيوط Argon2 ترثها): التصفح والرسائل أولًا، والتشفير يأخذ ما يبقى من المعالج.
    - `AUTH_MAX_IN_FLIGHT` (0 = ربع `THREADPOOL_SIZE`): طلبات `POST /api/auth/*` (عدا الخروج) في وقت واحد لكل عامل، يُعدّها `security_middleware` (`state.auth_in_flight`). الزائد 503 `busy` قبل أي خيط أو قاعدة بيانات.
  - **المنشور المثبّت:** `official.pinned_post` من ذاكرة كل عامل (60 ث، 10 ث بلا منشور)، و`official.changed_pin` بعد التثبيت أو الحذف يبثّ `{type:"pinned"}` عبر الـhub فيُنسى فورًا في كل العمال.
  - **WebSocket:**
    - الجلسة تُراجع كل `WS_SESSION_RECHECK_SECONDS`؛
    - وفورًا عند حدث `session` في الـhub: `sessions.revoke_*` تسجّل في `db.info`، ثم `Database.on_sessions_ended` بعد الـcommit (الخروج، والحظر، وتغيير كلمة المرور).
    - التطبيق يرفع حدّ الملفات المفتوحة إلى الحد الأعلى عند الإقلاع.
    - سطرا uvicorn لكل اتصال (`[accepted]`، `connection open`) محذوفان بفلتر `main._QuietSockets`، فلا يدفعان الأخطاء الحقيقية خارج السجل المحدود الحجم.
  - **الصور (`services/media.py`، 55 % من الطلبات):**
    - `path_for` بلا `resolve()` (المعرّف 32 حرفًا ست عشريًا)؛
    - `touch` يكتب `last_access` مرة كل دقيقتين لكل صورة في كل عامل، لا عند كل عرض.
    - الصور المرسلة إلى المتصفح تُحسب مرة لكل هاتف (ذاكرته المؤقتة)، وk6 يحاكي ذلك.
  - **الإيقاف:** `CMD` في `Dockerfile` بـ`exec uvicorn`، فيستلم uvicorn إشارة الإيقاف (PID 1) ويغلق الاتصالات بهدوء.
  - **التنظيف:** دفعات `cleanup.BATCH` في كل دورة (لا معاملة طويلة تحجز الصفوف).
  - **فهارس:** `ix_posts_pinned`، و`ix_notifications_created_at`، و`ix_users_name_trgm` (pg_trgm، `migrations.search_index`).
  - **وضع الاختبار:** `LOADTEST_MODE` (`security/loadtest.py`، `api/loadtest.py`) مستحيل خارج `ENV=staging`، ويحتاج رمزًا من 32+ حرفًا. لا يدخل اللوحة ولا `.env.example`.
  - **الأدوات في `loadtest/`** (README بالعربية):
    - staging منفصل على 8443 بخدمات مزيفة؛
    - زرع 50 ألفًا؛
    - k6 أ–ز؛
    - wsflood؛
    - المراقبة؛
    - التقرير؛
    - `phase2/` للخادم.
    - **لا يلمس الإنتاج أبدًا، والمرحلة 2 بعد موافقة المالك فقط.**
- **اللغات (V6 المرحلة 12):** العربية (الأصل والاحتياط) والفرنسية والإنجليزية.
  - **النص العربي هو المفتاح:** `t('…')` في الواجهة (`static/js/i18n.js`)، و`tr('…')`/`N_('…')` في الخادم (`app/i18n/`). رسائل `AppError` تُكتب بالعربية وقيمها في `params` (`{name}`)، ولا تُدمج في النص.
  - الترجمات المرفقة في `app/i18n/fr.json` و`en.json` (وصيغ الجمع العربية في `ar.json`)، ونصوص نظام المحتوى في `app/i18n/content_fr.py` و`content_en.py`.
  - بعد إضافة أي نص: `python -m app.i18n.extract`، ثم ترجمته. يفشل الاختبار إن نقصت ترجمة.
  - تعديلات اللوحة («اللغات والنصوص») في `i18n_texts`، مع نسخة احتياطية لكل تعديل في `i18n_revisions`. تُبثّ عبر الـhub (`{type:"i18n"}`) فتظهر في التطبيق فورًا.
  - لغة المستخدم في `users.language`. البريد والإشعارات والدفع تصله بلغته (`i18n.user_lang`)، واللوحة تبقى بالعربية دائمًا.
- **الرسائل:** المباشرة وطلبات المراسلة فقط. الرسائل المجهولة العشوائية (matching، كشف الهوية) حُذفت في المرحلة 1ب. المحادثات المجهولة القديمة (`kind IS NULL`) للقراءة فقط (`messaging.require_open`)، ثم تُصدَّر مشفّرة وتُحذف بعد `LEGACY_ANON_RETENTION_DAYS`.

**الواجهة:**
- CSP صارم `'self'`: لا inline scripts ولا `style=""`. العرض يُضبط بـ`el.style.width` من JS.
- `replaceChildren(...)` و`append(...)` مع `.filter(Boolean)` (لأن `null` يُطبع نصًا).
- **اللغات:**
  - كل نص يمر بـ`t()`، والمتغيرات تُكتب `{name}` ولا تُستعمل `${}` داخل `t()`. العدد الذي يحدد صيغة الجمع اسمه `n`.
  - لا يُترجَم نص عند تحميل الوحدة: الثوابت تصبح دوال أو getters.
  - الاتجاه من `<html dir>`. الأيقونات الاتجاهية (`back`/`chev`) تُعكس في LTR، والشعار لا يُعكس أبدًا.
  - الأرقام والتواريخ والمبالغ عبر `fmtNumber`/`fmtDate`/`fmtMoney`.
  - `e2e/run_overflow_e2e.py --lang fr|en` يفحص كل الصفحات في LTR.
- كل تغيير في ملفات الواجهة يتطلب رفع `VERSION` في `static/sw.js`.
- نصوص من نظام المحتوى تُدرج عبر `content.js` (`safeHtml` يعيد فحص الوسوم)، ولا تُدرج بـ`innerHTML` مباشرة.
- **عرض الهاتف (360px):**
  - أي قائمة `display:grid` تأخذ `grid-template-columns: minmax(0, 1fr)`، وإلا وسّع نص `nowrap` الصفحة كلها.
  - كل صفحة أو نافذة جديدة تُضاف إلى `e2e/run_overflow_e2e.py`. يفحص عند 360 و390: لا تمرير أفقي، ولا حقل مخفي، ولا نص مقطوع، وشريط الحالة، والنجمة.
- **الشعار:** `logoMark({ size, pulse, mono })` في `ui.js` فقط (الصغيرة عند ≤24px). نقطة الاسم بلون `--brand-red` في كل ألوان التمييز.
- **النجمة الزرقاء:** `starMark()` في `ui.js` فقط، بشكل محسوب في `icons.js` ولون `--star` (`appearance.STAR`). حجمها 18px بجانب كل اسم عبر `nameLine`.
- **الشارة الذهبية (الحساب الرسمي):** `goldMark({ size })` في `ui.js` فقط، عبر `nameLine(…, official)` بدل النجمة.
  - نفس الختم (`GOLD_SEAL` في `icons.js`). من 28px: تدرّج معدني `#FFE08A → #F5B83D → #C98A14` مع لمعة. أصغر: لون واحد `--gold` (`appearance.GOLD`) على شبكة بكسل لكل حجم (`GOLD_FLAT`).
  - الضغط عليها يفتح `officialSheet()`. معاينتها في اللوحة: «الهوية ← الشارات» (`run_brand_e2e`).
- **خانات الكتابة:** `autoGrow` بلا مقبض تغيير حجم.
- **النوافذ (sheets):** تُغلق عند تغيّر الصفحة، إلا الإلزامية منها.

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
.venv/bin/python -m pytest -q                                   # SQLite (أكثر من 290 اختبارًا)
DZ_TEST_DATABASE_URL=postgresql+psycopg://USER:PW@HOST:PORT/DB \
DZ_TEST_REDIS_URL=redis://HOST:PORT/0 .venv/bin/python -m pytest -q   # PostgreSQL + Redis
.venv/bin/pip-audit -r requirements.txt                         # التبعيات
```

**اختبارات المتصفح (Playwright، شاشة هاتف):**
```bash
PW_CHROMIUM=/opt/pw-browsers/chromium-1194/chrome-linux/chrome .venv/bin/python e2e/run_e2e.py
```
وكذلك باقي ملفات `e2e/run_*_e2e.py`.

- V6: الرفع صور فقط (Pillow)، ولا حاجة إلى ffmpeg في الاختبارات.

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
  - `deploy/chat-images-purge.sh`: نسخة كاملة ← حذف صور المحادثات القديمة من كل مكان إلا المبلَّغ عنها (مرة واحدة، آمن للتكرار؛ يشغّله `install.sh`).
  - `deploy/v6-cleanup.sh [anon]`: نسخة كاملة ← تصدير مشفّر للبيانات القديمة ← تحقق ← حذف (مرة واحدة، آمن للتكرار).
    - `anon`: المحادثات المجهولة القديمة بعد `LEGACY_ANON_RETENTION_DAYS`.
    - cron يومي `/etc/cron.d/dzplay-v6-anon` يثبّته `install.sh`.
- **النطاق خلف Cloudflare (المرحلة 20، جاهز وغير مطبَّق حتى يعطي المالك النطاق):** الخطوات في `docs/DOMAIN_MIGRATION.md`.
  - `APP_DOMAIN` إعداد واحد، و`PUBLIC_URL` يتبعه (الروابط، وwebhook، واسم EHLO و`Message-ID` عبر `settings.mail_hostname`).
  - `LEGACY_HOSTS`: العنوان القديم ← 301 (308 لغير GET)، ما عدا `_LEGACY_SERVED` في `main.py` وطلبات `DZPLAYApp/`.
  - Caddy بوضعين `TLS_MODE=auto|cloudflare`، والأجزاء في `deploy/caddy/` (مركّبة في `/etc/caddy/conf.d`):
    - `cloudflare`: شهادة Origin من `deploy/origin/` (خارج git)، و`trusted_proxies` من `cloudflare-ips.caddy` (مولَّد، خارج git) مع `client_ip_headers CF-Connecting-IP`.
    - `X-Forwarded-For` = `{client_ip}` دائمًا، وبلا ترويسة `Server`/`Via` (وuvicorn بـ`--no-server-header`).
  - `deploy/cloudflare-ips.sh`: يجلب النطاقات ويتحقق منها (القائمة السيئة لا تُستعمل أبدًا)، ومؤقت systemd يومي وعند الإقلاع. مع `CF_FIREWALL=on`: 80/443 لـCloudflare فقط في سلسلة `DOCKER-USER` (منافذ Docker تتجاوز ufw)، ولا يمسّ SSH.
  - `deploy/domain-switch.sh <domain>|check|finalize|rollback`.
  - `GET /healthz/ip` يعيد عنوان المتصل نفسه فقط (لفحص السلسلة).
  - التنبيهات بلا اسم الجهاز. الاختبارات: `tests/test_domain_v20.py`، و`e2e/run_cloudflare_e2e.sh` (Docker).
- **صفحة الصيانة واختبار الحمل على الخادم (المرحلة 21):**
  - `loadtest/phase2/maintenance.sh on|off`: «صيانة مؤقتة» من Caddy عبر `deploy/caddy/maintenance-<TLS_MODE>.caddy`، والمراقبة تكتفي بالقرص.
  - `night.sh`: النافذة الليلية بالترتيب.
  - `abort.sh`: إيقاف طارئ يعيد الإنتاج.
- **التطبيق:**
  - الكود في `/opt/dzplay/dzplay`.
  - السجلات: `docker compose logs -f app`.
  - أوامر الإدارة: `docker compose exec app python -m app.admin_cli …`.

### Android
- **البناء:** CI عند أي تغيير في `dzplay/android/**` (`.github/workflows/android-apk.yml`). الـAPK غير الموقّع يُدفع إلى فرع `android-build`.
- **الحد الأدنى:** Android 7.0 (minSdk 24) منذ 3.2.0، لأن Key Attestation يحتاجه.
- **التوقيع:** خارج المستودع بنفس المفتاح دائمًا (alias `dzplay`، شهادة SHA-256 تبدأ بـ`46:15:BE`). **المفتاح لا يُرفع إلى git أبدًا، ولا يتغيّر `applicationId`.**
- **النشر:**
  - الـAPK الموقّع في `dzplay/static/download/dzplay.apk`، مع `version.json` (`version_code`/`version_name`/`notes`).
  - رقم الإصدار في `dzplay/android/gradle.properties`.

### Git
- كل العمل على الفرع `claude/github-access-check-c1exvo`، بـcommits واضحة تبدأ بـ`V6 phase N:`.
- في نهاية كل مرحلة: الاختبارات، ثم تقرير قصير، ثم التوقف حتى موافقة المالك.
- الخطة الحالية: [PLAN.md](PLAN.md).
