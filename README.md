# APK Analyzer & Recovery

خدمة عربية لتحليل ملفات APK باستخدام JADX وApktool، مع إدارة مهام بالخلفية، نتائج ZIP، وصفحة تبرع Stripe بقيمة 10 GBP. تُطبق تغييرات PostgreSQL بشكل إضافي ولا تحذف السجلات الحالية.

## التشغيل المحلي

يتطلب Python 3.12 وPostgreSQL وJava 21 وJADX 1.5.6 وApktool.

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# عدّل DATABASE_URL وSECRET_KEY، ثم حمّل متغيرات .env إلى shell
python app.py
```

أو باستخدام Docker:

```sh
docker build -t apk-analyzer .
docker run --rm -p 8080:8080 -e DATABASE_URL="$DATABASE_URL" -e SECRET_KEY="$SECRET_KEY" -e BASE_URL=http://localhost:8080 -v apk-data:/data apk-analyzer
```

افتح `http://localhost:8080`. يجب تثبيت قاعدة PostgreSQL قبل التشغيل.

## البيئة

`SECRET_KEY`, `DATABASE_URL`, `BASE_URL`, `STRIPE_PUBLISHABLE_KEY`, `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `DATA_DIR`, `MAX_UPLOAD_BYTES` (100 MiB افتراضياً), `MAX_ANALYSIS_SECONDS` (1800), `MAX_JADX_DEX` (حد تنبيه 12), `MAX_WORKERS` (2). لا تضع مفاتيح Stripe السرية في Git.

## Stripe Test Mode

استخدم مفاتيح Stripe الاختبارية أولاً. أنشئ Webhook على `https://YOUR_HOST/api/stripe/webhook` للأحداث `payment_intent.succeeded`, `payment_intent.payment_failed`, و`payment_intent.processing`. يستخدم Payment Element ولا يتم حفظ رقم البطاقة. واجهة العودة تعرض الحالة المسجلة في الخادم فقط؛ تحديث الحالة لا يتم إلا عبر توقيع Webhook صحيح.

## Fly.io

تحقق من اسم التطبيق وقاعدة البيانات الحالية قبل النشر، وخذ نسخة احتياطية معتادة من PostgreSQL. لا ينفذ التطبيق DROP أو DELETE على جداول الإنتاج. أنشئ Fly volume اسمه `apk_data` بالحجم المناسب، ثم:

```sh
fly secrets set DATABASE_URL='...' SECRET_KEY='...' BASE_URL='https://YOUR_HOST' STRIPE_PUBLISHABLE_KEY='pk_test_...' STRIPE_SECRET_KEY='sk_test_...' STRIPE_WEBHOOK_SECRET='whsec_...'
fly deploy
fly status
fly logs
```

يستخدم العامل تخزيناً محلياً دائماً عبر Fly volume وطابور PostgreSQL مع `FOR UPDATE SKIP LOCKED`. ابدأ بآلة تطبيق واحدة لأن حجم الملفات محلي للآلة. لا ترفع عدد النسخ دون تخطيط مشاركة الملفات، كما أن عدة عمليات Gunicorn لا تشارك حدود العامل ولا يُوصى بها؛ Docker يشغل عاملاً واحداً.

## الاختبارات

```sh
pytest -q
```

الاختبارات الحالية لا تتصل بقاعدة الإنتاج ولا تشغل JADX أو Apktool. اختبار تحليل APK حقيقي وStripe Test Mode يحتاجان APK مرخصاً وبيئة اختبار خارجية. لا تعتبر البناء أو النشر ناجحاً دون تشغيلهما في بيئتك.

## أمان

الملف المرفوع لا يُنفذ؛ يتم فحص حاوية ZIP ووجود AndroidManifest.xml ثم تمريره لأدوات فك التحليل فقط. يطبق CSRF، حدود رفع، تنزيل محصور داخل DATA_DIR، ترويسات أمان، ورسائل أخطاء عربية. نظام الحد من المعدل داخل الذاكرة لكل عملية؛ للنشر العام أضف WAF أو rate limiter موزعاً.
