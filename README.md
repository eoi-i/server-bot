# بوت تلجرام تجريبي — Fly.io

بوت تلجرام عربي تجريبي بأزرار تفاعلية (Inline Keyboard)، وأوامر `/start` و`/help` و`/about`. يتضمن نقطة فحص صحة على `/health`.

## التشغيل محليًا

يتطلب Python 3.12 أو أحدث. أنشئ بوتًا عبر [@BotFather](https://t.me/BotFather)، ثم خزّن التوكن في متغير بيئة — لا تضعه داخل الملفات أو Git:

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
export BOT_TOKEN="ضع_التوكن_هنا"  # PowerShell: $env:BOT_TOKEN="ضع_التوكن_هنا"
python bot.py
```

تحقق من نقطة الصحة محليًا بزيارة `http://localhost:8080/health`.

## النشر على Fly.io

1. ثبّت `flyctl` وسجّل الدخول: `fly auth login`.
2. عدّل قيمة `app` في `fly.toml` إلى اسم تطبيق Fly.io متاح وفريد.
3. من مجلد المشروع أنشئ التطبيق: `fly launch --no-deploy`، واختر الاسم والمنطقة المناسبة إذا طُلب ذلك.
4. أضف التوكن كسرّ آمن، وليس في ملف أو رسالة عامة:

   ```bash
   fly secrets set BOT_TOKEN="ضع_التوكن_هنا"
   ```

5. انشر التطبيق: `fly deploy`.
6. راقب السجلات: `fly logs`، وافحص الحالة عبر `https://اسم-التطبيق.fly.dev/health`.

التطبيق يستخدم long polling، لذا شغّل نسخة واحدة من البوت لتجنب تكرار استلام التحديثات. إذا سبق نشر التوكن في مكان عام، أبطله فورًا من BotFather وأنشئ توكنًا جديدًا.
