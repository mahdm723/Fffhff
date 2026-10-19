# الانتقال إلى نطاق خاص خلف Cloudflare (المرحلة 20)

الكود جاهز، والتنفيذ يبدأ عندما يصبح النطاق عندك. نفّذ الخطوات بالترتيب. كل خطوة تحافظ على البيانات، و`rollback` يعيدك إلى العنوان القديم في أي وقت.

**الفكرة:**
- الزوار يصلون إلى Cloudflare، وCloudflare وحده يتصل بالخادم بشهادة «Origin» وفي وضع **Full (strict)**.
- يرى التطبيق عنوان الزائر الحقيقي من `CF-Connecting-IP`، ولا يقبله إلا إذا جاء الاتصال من نطاقات Cloudflare المنشورة.
- في الخطوة الأخيرة لا يقبل الخادم على المنفذين 80 و443 إلا Cloudflare.

---

## 0. ما تحتاجه
- نطاق، مثلًا `example.com`.
- حساب Cloudflare (الخطة المجانية تكفي).
- مزوّد بريد: **Brevo** أو **Resend** أو **Zoho** (الجدول في الخطوة 2).
- دخول SSH إلى الخادم.

## 1. Cloudflare
1. **Add a site** ← اكتب النطاق ← الخطة **Free** ← غيّر خوادم الأسماء (Nameservers) عند مسجّل النطاق إلى الاثنين اللذين يعطيك إياهما Cloudflare. انتظر حتى تصير حالة الموقع **Active**.
2. **DNS ← Records:**
   - `A`، الاسم `@`، القيمة: عنوان الخادم، **Proxied** (السحابة البرتقالية).
   - اختياري: `CNAME`، الاسم `www`، القيمة `@`، Proxied.
   - **لا تنشئ أي سجل آخر يشير إلى عنوان الخادم** (مثل `mail` أو `ftp` أو `direct`)، ولا سجلًا رماديًا (DNS only) له: هذا يكشف العنوان.
   - إن كان للخادم IPv6، أضف `AAAA` بالاسم `@` وProxied أيضًا. وإلا فلا تضف شيئًا.
3. **SSL/TLS ← Overview:** اختر **Full (strict)**.
4. **SSL/TLS ← Edge Certificates:** فعّل **Always Use HTTPS**، و**Minimum TLS Version** على 1.2.
5. **SSL/TLS ← Origin Server ← Create Certificate:**
   - المفتاح RSA (2048)، والأسماء `example.com` و`*.example.com`، والمدة 15 سنة.
   - انسخ **Origin Certificate** واحفظه للخطوة 3 باسم `cert.pem`.
   - انسخ **Private Key** باسم `key.pem`. **يظهر مرة واحدة فقط**، فلا ترسله لأحد ولا تضعه في git.
6. **Network:** تأكد أن **WebSockets** مفعّلة (الرسائل الفورية تحتاجها).
7. **Security:**
   - اترك **Bot Fight Mode** موقوفًا: قد يحجب طلبات تطبيق أندرويد وTelegram.
   - إن فعّلت أي حماية للبوتات، فأضف في **WAF ← Custom rules** قاعدة **Skip** للمسار `/api/telegram/webhook`.

## 2. البريد: `noreply@` و`support@`
- يعمل التطبيق مع أي مزوّد SMTP. الإعداد من لوحة القيادة: «الأمان والنظام ← البريد»، بملفين:
  - **بريد النظام** (`noreply@example.com`): الرموز والإشعارات.
  - **بريد الدعم** (`support@example.com`): التذاكر.
- سجلات البريد كلها في Cloudflare ← DNS، وتبقى **DNS only** (رمادية). قيم DKIM خاصة بحسابك: **انسخها كما تظهر في لوحة المزوّد**. الجدول يبيّن شكلها.

| | Brevo | Resend | Zoho Mail |
|---|---|---|---|
| **مناسب لـ** | الإرسال (رموز ورسائل آلية). خطة مجانية يومية. | الإرسال فقط. | صندوق حقيقي لـ`support@` (استقبال وإرسال). |
| **التحقق من النطاق** | `TXT` `@` ← `brevo-code:…` | — (يكفي DKIM) | `TXT` `@` ← `zoho-verification=…` |
| **SPF** (`TXT` على `@`، سجل واحد فقط) | `v=spf1 include:spf.brevo.com ~all` | على `send`: `v=spf1 include:amazonses.com ~all` | `v=spf1 include:zohomail.com ~all` |
| **DKIM** | `CNAME` `brevo1._domainkey` و`brevo2._domainkey` ← القيم من Brevo | `TXT` `resend._domainkey` ← `p=…` من Resend | `TXT` `<selector>._domainkey` ← من Zoho |
| **MX** | — | `send` ← `feedback-smtp.<region>.amazonses.com` (10) | `@` ← `mx.zoho.com` (10)، `mx2.zoho.com` (20)، `mx3.zoho.com` (50) |
| **DMARC** (`TXT` على `_dmarc`) | `v=DMARC1; p=none; rua=mailto:rua@dmarc.brevo.com` | `v=DMARC1; p=none;` | `v=DMARC1; p=none; rua=mailto:support@example.com` |
| **SMTP في اللوحة** | `smtp-relay.brevo.com`، المنفذ 587، STARTTLS، المستخدم: SMTP login من Brevo، كلمة المرور: SMTP key | `smtp.resend.com`، المنفذ 465 (SSL) أو 587، المستخدم `resend`، كلمة المرور: API key | `smtppro.zoho.com` (حساب نطاق)، المنفذ 465 (SSL) أو 587، المستخدم: العنوان كاملًا، كلمة المرور: App Password |

- **مزوّدان معًا** (مثلًا Brevo للنظام وZoho للدعم): سجل SPF واحد يجمعهما، مثل `v=spf1 include:spf.brevo.com include:zohomail.com ~all`.
- **DMARC:** ابدأ بـ`p=none`. بعد أسبوعين أو ثلاثة من النجاح انتقل إلى `p=quarantine`.
- **استقبال `support@` بلا Zoho:** يمكن تفعيل **Email Routing** في Cloudflare (مجاني) لتحويله إلى بريدك. اكتب العنوان المُستقبِل في «بريد الدعم».
- **التجربة:**
  - اضغط «إرسال تجربة» لكل ملف من اللوحة.
  - في Gmail افتح الرسالة ← «إظهار الرسالة الأصلية». يجب أن يظهر `SPF: PASS` و`DKIM: PASS` و`DMARC: PASS`.
  - انظر أيضًا في أسطر `Received:`. إن ظهر فيها عنوان الخادم، فهذا من المزوّد (بعضهم يسجّل عنوان من أرسل إليه). الحل حينها مزوّد لا يضيفه، أو إرسال عبر API (أخبرني وأجهّزه).

## 3. على الخادم
1. ضع الشهادة والمفتاح:
   ```bash
   sudo mkdir -p /opt/dzplay/dzplay/deploy/origin
   sudo nano /opt/dzplay/dzplay/deploy/origin/cert.pem   # الصق Origin Certificate
   sudo nano /opt/dzplay/dzplay/deploy/origin/key.pem    # الصق Private Key
   ```
2. انقل التطبيق:
   ```bash
   sudo /opt/dzplay/dzplay/deploy/domain-switch.sh example.com
   ```
   - يتحقق السكربت قبل أي تغيير من أن الشهادة تغطي النطاق، وأن المفتاح مفتاحها، وأنها غير منتهية.
   - يحفظ `.env` باسم `.env.before-domain`، ثم يكتب `APP_DOMAIN` و`DOMAIN` و`PUBLIC_URL` و`TLS_MODE=cloudflare` و`LEGACY_HOSTS` (العنوان القديم).
   - يكتب نطاقات Cloudflare لـCaddy، ويعيد تشغيل Caddy والتطبيق، ويضبط webhook البوت، ويثبّت مؤقتًا يحدّث النطاقات يوميًا وعند الإقلاع.
   - في النهاية يشغّل الفحوص:
     - شهادة Origin على الخادم، وعدم إرسال ترويسة `Server` أو `Via`.
     - تجاهل `CF-Connecting-IP` المزوّر من خارج Cloudflare.
     - المرور عبر Cloudflare: النطاق يجيب، ويرى التطبيق عنوانك الحقيقي، وWebSocket يصل.
     - العنوان القديم: 301 إلى النطاق، و`/download` ما زالت تعمل.
     - webhook البوت.
   - لإعادة الفحوص في أي وقت: `sudo deploy/domain-switch.sh check`.
3. جرّب من الهاتف:
   - افتح `https://example.com` وسجّل الدخول، ثم أرسل رسالة: يجب أن تصل فورًا.
   - افتح لوحة القيادة على `https://example.com/<ADMIN_PATH>`.
   - أرسل `/stats` للبوت.
   - أرسل بريد تجربة (الخطوة 2).

**أثناء الانتقال:**
- التطبيق المثبّت (3.2.0) يبقى يعمل على العنوان القديم، ومنه يصله التحديث.
- المتصفح على العنوان القديم يُحوَّل إلى النطاق (301 مع المسار).
- **مستخدمو iPhone:** يفتحون العنوان الجديد ويضيفونه إلى الشاشة الرئيسية من جديد.
- الجميع يسجّل الدخول مرة واحدة على العنوان الجديد، لأن الجلسات مرتبطة بالعنوان.

## 4. تطبيق أندرويد 3.3.0 (تحديث إلزامي)
- أجهّزه أنا حين تعطيني النطاق:
  - `appHost` = النطاق، بنفس المفتاح ونفس `applicationId`.
  - أبنيه وأوقّعه، وأنشره في `static/download/` مع `version.json` و`min_version_code` الجديد.
- بعد النشر يرى كل هاتف عليه 3.2.0 شاشة «حدّث التطبيق»، وينزّل 3.3.0 من العنوان القديم الذي ما زال يعمل.
- 3.3.0 يفتح النطاق مباشرة.

## 5. فترة الانتظار
- تابع في لوحة القيادة نسخ التطبيق على الأجهزة.
- حين تصير كلها (أو شبه كلها) على 3.3.0، انتقل إلى الخطوة 6. عادةً يكفي أسبوع إلى أسبوعين.

## 6. الإنهاء: إيقاف العنوان القديم، وCloudflare فقط
```bash
sudo /opt/dzplay/dzplay/deploy/domain-switch.sh finalize
```
- يتوقف العنوان القديم.
- جدار الحماية يصبح: **المنفذان 80 و443 لنطاقات Cloudflare فقط**.
  - القواعد في سلسلة `DOCKER-USER`، لأن منافذ Docker تتجاوز ufw.
  - تُحدَّث النطاقات يوميًا.
  - لا يمسّ SSH أبدًا.
- **تحقق من الخارج:**
  - من الهاتف على بيانات الجوال، لا على شبكة الخادم: `curl -m 8 -k https://<عنوان الخادم>/healthz`. يجب أن ينتهي بمهلة (time-out).
  - و`https://example.com` يعمل كالمعتاد.
- **للطوارئ** (يفتح 80/443 للجميع فورًا): `sudo deploy/cloudflare-ips.sh firewall-off`، ثم `CF_FIREWALL=off` في `.env`.

## 7. فحص أخير لتسرّب العنوان
- `dig +short example.com`: عناوين Cloudflare فقط. وفي Cloudflare ← DNS لا يوجد أي سجل رمادي يشير إلى الخادم.
- لا ترويسة `Server` أو `Via` من الخادم، وصفحات الأخطاء JSON فقط.
- البريد: اسم EHLO و`Message-ID` هما النطاق، لا اسم الجهاز. راجع أيضًا أسطر `Received:` (الخطوة 2).
- **ما لا يمكن إخفاؤه بالانتقال وحده:**
  - العنوان القديم معروف أصلًا: كان جزءًا من اسم sslip، وموجود في سجلات شفافية الشهادات، والتطبيقات القديمة، وتاريخ git.
  - الخدمات التي يتصل بها الخادم بنفسه ترى عنوانه، وهذا طبيعي: Telegram، وBybit، وTronGrid/BSC، ومزوّد البريد.
  - الإخفاء الكامل يحتاج **عنوان خادم جديدًا** بعد الانتقال: نسخة احتياطية ← خادم جديد ← استعادة ← تغيير سجل `A` في Cloudflare. اختياري، وأجهّزه عند الطلب.

## 8. الرجوع
```bash
sudo /opt/dzplay/dzplay/deploy/domain-switch.sh rollback
```
- يعيد `.env` من `.env.before-domain`، ويوقف جدار Cloudflare، ويعيد Caddy إلى Let's Encrypt على العنوان القديم، ويعيد webhook البوت.

## الإعدادات (يكتبها السكربت في `.env`)
| المتغير | المعنى |
|---|---|
| `APP_DOMAIN` | النطاق. منه `PUBLIC_URL` (الروابط، وwebhook البوت، والبريد) |
| `DOMAIN` | الموقع الذي يخدمه Caddy |
| `TLS_MODE` | `auto` (Let's Encrypt، الافتراضي) أو `cloudflare` (شهادة Origin + `CF-Connecting-IP` من Cloudflare فقط) |
| `LEGACY_HOSTS` | العنوان القديم أثناء الانتقال (301، ما عدا ما يحتاجه التطبيق المثبّت). يُفرَّغ عند `finalize` |
| `CF_FIREWALL` | `on` بعد `finalize`: المنفذان 80/443 لـCloudflare فقط |
