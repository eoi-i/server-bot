# بوت تلجرام ولوحة تحكم ويب — Fly.io

بوت عربي مع لوحة تحكم ويب متجاوبة باللغة العربية، تسجيل دخول بكلمة مرور، إحصائيات المستخدمين، وضع الصيانة، وتعديل رسالة الترحيب. الإعدادات تُحفظ في SQLite على وحدة تخزين Fly.io الدائمة.

## أسرار البيئة المطلوبة

- `BOT_TOKEN`: توكن Telegram من [@BotFather](https://t.me/BotFather).
- `DASHBOARD_PASSWORD`: كلمة مرور جديدة وقوية للوحة الويب.
- `DASHBOARD_SECRET`: مفتاح عشوائي طويل لتوقيع جلسات الدخول.
- `TELEGRAM_ADMIN_IDS`: (اختياري) أرقام حسابات Telegram المسموح لها بأوامر `/admin` و`/setwelcome`، مفصولة بفواصل.

لا تضع أي سر داخل `bot.py` أو GitHub. كلمة المرور التي أُرسلت في المحادثة لم تُضمّن في الكود؛ لأنها أصبحت مكشوفة في سجل المحادثة، أنشئ كلمة مرور جديدة بدلًا منها. استخدم HTTPS في الإنتاج.

## لوحة التحكم

بعد النشر افتح `https://اسم-التطبيق.fly.dev/` أو `/dashboard`، ثم سجّل الدخول بكلمة المرور التي عيّنتها. تتيح اللوحة عرض عدد المستخدمين، تشغيل/إيقاف وضع الصيانة، وتحديث رسالة الترحيب. تستخدم جلسة موقعة، وملفات تعريف ارتباط `HttpOnly` و`SameSite=Strict`، وحماية CSRF، كما لا تُخزّن كلمة المرور في قاعدة البيانات.

- `/health` — فحص حالة الخدمة.
- `/login` — تسجيل الدخول للوحة.
- `/admin` داخل Telegram — لوحة الإدارة النصية للمديرين المحددين.

## النشر على Fly.io

1. ثبّت `flyctl` وسجّل الدخول: `fly auth login`.
2. تأكد أن قيمة `app` في `fly.toml` فريدة وأن `primary_region` هي المنطقة التي ستستخدمها.
3. أنشئ التطبيق دون نشر إذا لم يكن موجودًا: `fly launch --no-deploy`.
4. أنشئ وحدة تخزين دائمة في منطقة التطبيق (المثال يستخدم `iad`): `fly volumes create bot_data --region iad --size 1`.
5. أنشئ كلمة مرور جديدة، ثم أضف الأسرار. أنشئ سر الجلسات العشوائي في الطرفية ولا تحفظه في المستودع:

   ```bash
   fly secrets set BOT_TOKEN="توكن_جديد_أو_حالي" DASHBOARD_PASSWORD="كلمة_مرور_جديدة_وقوية" DASHBOARD_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')" TELEGRAM_ADMIN_IDS="123456789"
   ```

   احذف `TELEGRAM_ADMIN_IDS` من الأمر إذا لم تكن بحاجة إلى أوامر الإدارة داخل Telegram. استبدل القيم النموذجية بقيمك محليًا؛ لا ترسلها في المحادثة.

6. انشر: `fly deploy`، ثم افتح `https://اسم-التطبيق.fly.dev/`.
7. للتشخيص: `fly logs` و`https://اسم-التطبيق.fly.dev/health`.

التطبيق يستخدم long polling وقاعدة SQLite؛ شغّل نسخة واحدة من البوت. عند مشاركة توكن البوت أو كلمة مرور لوحة التحكم علنًا، أبطلهما/غيّرهما فورًا.

## التشغيل المحلي

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export BOT_TOKEN="توكن_البوت"
export DASHBOARD_PASSWORD="كلمة_مرور_جديدة_وقوية"
export DASHBOARD_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
python bot.py
```

ثم افتح `http://localhost:8080/`. في التشغيل المحلي عبر HTTP سيستخدم التطبيق ملف تعريف ارتباط غير Secure تلقائيًا؛ لا تغيّر ذلك في الإنتاج، واستخدم HTTPS.
