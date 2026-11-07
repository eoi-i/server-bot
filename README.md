# APK Analyzer & Recovery

خدمة Flask عربية لتحليل ملفات APK باستخدام JADX وApktool، حفظ نتائج جزئية، تجميع مشروع استعادة قابل للتحرير، وضغطه كـ ZIP. تستخدم PostgreSQL الحالية عبر ترحيلات SQL إضافية لا تحذف الجداول أو السجلات.

## التشغيل

يتطلب Python 3.12 وJava 21 وJADX 1.5.6 وApktool، بالإضافة إلى PostgreSQL.

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
export DATABASE_URL='postgresql://user:password@localhost:5432/apk_analyzer'
export SECRET_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')"
export DATA_DIR="$PWD/data"
python app.py
```

أو: `docker build -t apk-analyzer .` ثم `docker run --rm -p 8080:8080 -e DATABASE_URL=... -e SECRET_KEY=... -v apk-data:/data apk-analyzer`.

## إعداد Stripe

ضبط `STRIPE_PUBLISHABLE_KEY`, `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET` في الأسرار، جميعها Test Mode أولاً. أنشئ Webhook يشير إلى `https://YOUR_HOST/api/stripe/webhook` واشترك بأحداث `payment_intent.succeeded`, `payment_intent.payment_failed`, و`payment_intent.processing`. التبرع ثابت 10 GBP. لا تحفظ الخدمة بيانات البطاقة؛ Stripe.js وPayment Element يتعاملان معها. حالة النجاح في المتصفح ليست إثباتاً؛ تعرض الحالة قاعدة البيانات التي تحدثها Webhook فقط.

## متغيرات البيئة

`SECRET_KEY`, `DATABASE_URL`, `BASE_URL`, `STRIPE_PUBLISHABLE_KEY`, `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `DATA_DIR`, `MAX_UPLOAD_BYTES` (افتراضي 100 MiB), `MAX_ANALYSIS_SECONDS` (1800), `MAX_JADX_DEX` (حد تنبيه 12), `MAX_WORKERS` (2). لا تضع أسرار Stripe في ملفات المستودع.

## Fly.io

تحقق من اسم التطبيق وقاعدة البيانات قبل النشر، وأرفق PostgreSQL الحالية دون إعادة إنشائها أو حذفها. أنشئ volume دائم باسم `apk_data` بالحجم المناسب. ثم:

```sh
fly secrets set DATABASE_URL='...' SECRET_KEY='...' BASE_URL='https://YOUR_HOST' STRIPE_PUBLISHABLE_KEY='pk_test_...' STRIPE_SECRET_KEY='sk_test_...' STRIPE_WEBHOOK_SECRET='whsec_...'
fly deploy
fly status
fly logs
```

أزل مفاتيح test واستبدلها بمفاتيح live فقط بعد الدفع التجريبي وفحص Webhook. الملفات المحلية لكل آلة ليست تخزيناً موزعاً؛ شغل نسخة تطبيق واحدة حالياً. هذا المستودع يستخدم executor داخل العملية، لذا نشر عدة آلات يحتاج طابوراً موزعاً (مثل Redis/RQ) قبل التوسع الأفقي. افحص قوانين وإذن تحليل التطبيقات قبل رفعها.

## الأمان وحدود التشغيل

CSRF على طلبات التغيير باستثناء Webhook الذي يتحقق من التوقيع، cookies آمنة، حد رفع، اسم ملف آمن، فحص بنية ZIP، حدود زمنية، منع مسار التنزيل خارج DATA_DIR، ورسائل خطأ عربية. لا تُشغّل APK أو محتواه. يجب وضع WAF/حماية إضافية للتطبيق العام؛ محدد المعدل الحالي process-local وليس بديلاً عن rate limiter موزع.

## الاختبارات

`pytest -q` يشغّل اختبارات وظائف مساعدة وواجهات باستخدام قاعدة بيانات Postgres تجريبية مطلوبة؛ لا تشغّلها على قاعدة الإنتاج. اختبارات تحليل حقيقية تتطلب أدوات jadx/apktool وAPK fixtures مرخصة. لم يتم تضمين بيانات اعتماد أو APK خاص.
