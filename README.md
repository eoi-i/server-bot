# APK Analyzer — Flask + PostgreSQL

واجهة عربية لرفع ملفات APK وتحليلها بواسطة JADX وApktool، ثم تنزيل حزمة ZIP تحتوي على مشروع Android أولي والشفرة المفككة والموارد.

## المتطلبات
- Python 3.12 للتشغيل المحلي، أو Docker.
- Java 21 وApktool وJADX إذا شغّلت التطبيق دون Docker.
- قاعدة بيانات PostgreSQL.
- متغير البيئة `DATABASE_URL`.

## التشغيل محلياً

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
```

اضبط المتغيرات في الطرفية (لا تضع كلمة المرور في ملفات عامة):

```bash
export DATABASE_URL='postgresql://USER:PASSWORD@HOST:5432/DATABASE?sslmode=require'
export SECRET_KEY='ضع-قيمة-عشوائية-طويلة-هنا'
python app.py
```

افتح `http://127.0.0.1:8080`.

## النشر على Fly.io

1. تأكد أن قاعدة PostgreSQL متاحة من Fly.io وأن عنوان الاتصال صحيح.
2. سجّل الدخول إلى Fly CLI.
3. من مجلد المشروع نفّذ:

```bash
flyctl secrets set DATABASE_URL='postgresql://USER:PASSWORD@HOST:5432/DATABASE?sslmode=require' -a server-bot-w3jeua
flyctl secrets set SECRET_KEY='قيمة-عشوائية-طويلة' -a server-bot-w3jeua
flyctl deploy -a server-bot-w3jeua
```

إذا كان اسم تطبيق Fly مختلفاً، عدّل `app` في `fly.toml` أو استخدم اسم تطبيقك في الأوامر. لا يحتاج هذا الإعداد إلى `[mounts]` لأنه لا يربط Volume.

## ما الذي تحفظه قاعدة البيانات؟
تحفظ PostgreSQL سجل التحليل وحالته واسم الملف ومسار النتيجة. ملفات APK وZIP تبقى في مساحة ملفات محلية مؤقتة داخل الحاوية (`/tmp/apk-analyzer-data`). لذلك قد تختفي الملفات عند استبدال الآلة أو إعادة تشغيلها. إذا أردت حفظ النتائج بشكل دائم، أضف لاحقاً تخزيناً خارجياً مثل S3-compatible / Cloudflare R2، وخزّن رابط الملف في PostgreSQL.

## حدود الاستعادة
- JADX يستخرج شفرة قريبة من Java؛ لا يعيد بالضرورة ملفات Kotlin الأصلية أو أسماء المتغيرات والتعليقات.
- Apktool يستخرج الموارد وManifest وSmali.
- مجلد Android الناتج هيكل أولي، وليس ضماناً أن التطبيق سيُبنى مباشرة. قد تحتاج إلى إعادة التبعيات وإصلاح الموارد والشفرة يدوياً.
- استخدم الأداة فقط على التطبيقات التي تملكها أو لديك إذن واضح بتحليلها.

## API
- `GET /health` فحص الخدمة.
- `POST /api/analyze` رفع ملف باسم الحقل `apk`.
- `GET /api/status/<id>` حالة عملية.
- `GET /api/history` آخر العمليات.
- `GET /download/<id>` تنزيل ZIP عند اكتمال التحليل.
