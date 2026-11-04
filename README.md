# بوت تلجرام عربي مع لوحة تحكم — Fly.io

بوت عربي باستخدام أزرار Telegram التفاعلية (Inline Keyboard)، ولوحة إدارة خاصة، وإحصاء المستخدمين، ووضع صيانة، وتعديل رسالة الترحيب. توجد نقطة فحص صحة على `/health`.

> أزرار Telegram التفاعلية لا تتيح للمطور فرض لون أو شفافية؛ شكلها يحدده تطبيق Telegram. هذا المشروع يستخدم أزرار Inline الأصلية المناسبة داخل المحادثة.

## المتغيرات البيئية

- `BOT_TOKEN`: توكن البوت من [@BotFather](https://t.me/BotFather). هذا هو اسم المتغير المطلوب داخل الكود.
- `TELEGRAM_ADMIN_IDS`: أرقام حسابات Telegram المسموح لها بلوحة التحكم، مفصولة بفواصل عند وجود أكثر من مدير.
- `PORT`: منفذ فحص الصحة، الافتراضي `8080`.
- `DATABASE_PATH`: مسار قاعدة البيانات، الافتراضي `/data/bot.db`.

لا تضع التوكن داخل `bot.py` أو GitHub أو أي رسالة عامة. الكود يقرأه بأمان من `BOT_TOKEN` في البيئة.

## الأوامر

- `/start` — بدء البوت.
- `/help` — المساعدة.
- `/about` — معلومات عن البوت.
- `/admin` — لوحة التحكم (للمديرين فقط).
- `/setwelcome نص الترحيب` — تغيير رسالة الترحيب (للمديرين فقط).

من لوحة التحكم يمكن عرض عدد المستخدمين وتفعيل/إيقاف وضع الصيانة. يُسمح للمدير باستخدام البوت حتى أثناء الصيانة.

## التشغيل محليًا

يتطلب Python 3.12 أو أحدث:

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
export BOT_TOKEN="توكن_البوت"  # PowerShell: $env:BOT_TOKEN="توكن_البوت"
export TELEGRAM_ADMIN_IDS="123456789"  # ضع رقم حسابك في Telegram
python bot.py
```

تحقق من نقطة الصحة عبر `http://localhost:8080/health`.

## النشر على Fly.io

1. ثبّت `flyctl` وسجّل الدخول: `fly auth login`.
2. عدّل اسم `app` في `fly.toml` إلى اسم تطبيق Fly.io متاح وفريد، واختر المنطقة المناسبة.
3. أنشئ التطبيق دون نشر: `fly launch --no-deploy`.
4. أنشئ وحدة تخزين دائمة في منطقة التطبيق (المنطقة الافتراضية في الملف `iad`):

   ```bash
   fly volumes create bot_data --region iad --size 1
   ```

5. أضف الأسرار الآمنة (استبدل الرقم بمعرّف Telegram الرقمي الخاص بك):

   ```bash
   fly secrets set BOT_TOKEN="توكن_البوت" TELEGRAM_ADMIN_IDS="123456789"
   ```

6. انشر التطبيق: `fly deploy`.
7. افحص السجلات والحالة: `fly logs` و`https://اسم-التطبيق.fly.dev/health`.

التطبيق يستخدم long polling وقاعدة SQLite؛ شغّل نسخة واحدة لتجنب تكرار التحديثات. إذا سبق أن شاركت التوكن علنًا، أبطله عبر BotFather وأنشئ توكنًا جديدًا.
