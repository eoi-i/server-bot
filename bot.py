import asyncio
import hashlib
import hmac
import html
import logging
import os
import secrets
import sqlite3
import time
from contextlib import suppress
from pathlib import Path

from aiohttp import web
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
)
logger = logging.getLogger(__name__)

DB_PATH = Path(os.getenv("DATABASE_PATH", "/data/bot.db"))
ADMIN_IDS = {
    int(value.strip())
    for value in os.getenv("TELEGRAM_ADMIN_IDS", "").split(",")
    if value.strip().isdigit()
}
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")
DASHBOARD_SECRET = os.getenv("DASHBOARD_SECRET", "")
SESSION_COOKIE = "server_bot_session"
SESSION_TTL = 8 * 60 * 60
DEFAULT_WELCOME = (
    "أهلًا بك! 👋\n\n"
    "أنا بوتك التجريبي، جاهز لمساعدتك. استخدم الأزرار بالأسفل للتنقل."
)


def db_connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_database() -> None:
    with db_connect() as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS users ("
            "user_id INTEGER PRIMARY KEY, first_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "last_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('maintenance', '0')")
        db.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('welcome', ?)",
            (DEFAULT_WELCOME,),
        )


def is_admin(user_id: int | None) -> bool:
    return user_id is not None and user_id in ADMIN_IDS


def get_setting(key: str, default: str = "") -> str:
    with db_connect() as db:
        row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with db_connect() as db:
        db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def record_user(user_id: int) -> None:
    with db_connect() as db:
        db.execute(
            "INSERT INTO users (user_id) VALUES (?) "
            "ON CONFLICT(user_id) DO UPDATE SET last_seen = CURRENT_TIMESTAMP",
            (user_id,),
        )


def total_users() -> int:
    with db_connect() as db:
        row = db.execute("SELECT COUNT(*) AS count FROM users").fetchone()
    return int(row["count"])


def user_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✨ عن البوت", callback_data="about")],
            [InlineKeyboardButton("📖 المساعدة", callback_data="help")],
        ]
    )


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📊 الإحصائيات", callback_data="admin:stats")],
            [InlineKeyboardButton("🚧 تبديل وضع الصيانة", callback_data="admin:maintenance")],
            [InlineKeyboardButton("📝 تعديل الترحيب", callback_data="admin:welcome")],
            [InlineKeyboardButton("🔄 تحديث اللوحة", callback_data="admin:refresh")],
        ]
    )


async def maintenance_message(update: Update) -> bool:
    user = update.effective_user
    if get_setting("maintenance", "0") != "1" or is_admin(user.id if user else None):
        return False
    if update.effective_message:
        await update.effective_message.reply_text("🛠️ البوت تحت الصيانة حاليًا. يرجى المحاولة لاحقًا.")
    return True


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user:
        record_user(user.id)
    if await maintenance_message(update):
        return
    if update.effective_message:
        await update.effective_message.reply_text(
            get_setting("welcome", DEFAULT_WELCOME), reply_markup=user_keyboard()
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await maintenance_message(update):
        return
    text = "📖 المساعدة\n\n/start — بدء البوت\n/help — عرض المساعدة\n/about — معلومات عن البوت"
    if update.effective_message:
        await update.effective_message.reply_text(text, reply_markup=user_keyboard())


async def about_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await maintenance_message(update):
        return
    if update.effective_message:
        await update.effective_message.reply_text(
            "✨ بوت تجريبي عربي بواجهة تفاعلية ولوحة إدارة خاصة.",
            reply_markup=user_keyboard(),
        )


def admin_dashboard_text() -> str:
    maintenance = "مفعّل 🔴" if get_setting("maintenance", "0") == "1" else "متوقف 🟢"
    return f"🛡️ لوحة تحكم البوت\n\n👥 المستخدمون المسجلون: {total_users()}\n🚧 وضع الصيانة: {maintenance}"


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_admin(user.id if user else None):
        if update.effective_message:
            await update.effective_message.reply_text("⛔ هذا الأمر مخصص لمدير البوت فقط.")
        return
    if update.effective_message:
        await update.effective_message.reply_text(
            admin_dashboard_text(), reply_markup=admin_keyboard()
        )


async def set_welcome_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_admin(user.id if user else None):
        if update.effective_message:
            await update.effective_message.reply_text("⛔ هذا الأمر مخصص لمدير البوت فقط.")
        return
    text = " ".join(context.args).strip()
    if not text:
        if update.effective_message:
            await update.effective_message.reply_text("استخدم: /setwelcome نص الترحيب الجديد")
        return
    set_setting("welcome", text)
    if update.effective_message:
        await update.effective_message.reply_text("✅ تم تحديث الترحيب.", reply_markup=admin_keyboard())


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    user_id = query.from_user.id
    data = query.data or ""

    if data.startswith("admin:") and not is_admin(user_id):
        await query.answer("هذا الخيار مخصص للمدير فقط.", show_alert=True)
        return
    if data.startswith("admin:"):
        if data == "admin:stats":
            text = f"📊 الإحصائيات\n\n👥 المستخدمون: {total_users()}\n🚧 الصيانة: {'مفعلة' if get_setting('maintenance', '0') == '1' else 'متوقفة'}"
        elif data == "admin:maintenance":
            set_setting("maintenance", "0" if get_setting("maintenance", "0") == "1" else "1")
            text = admin_dashboard_text()
        elif data == "admin:welcome":
            text = "📝 لتغيير رسالة الترحيب أرسل:\n/setwelcome النص الجديد"
        else:
            text = admin_dashboard_text()
        markup = admin_keyboard()
    else:
        if get_setting("maintenance", "0") == "1" and not is_admin(user_id):
            await query.answer("البوت تحت الصيانة حاليًا.", show_alert=True)
            return
        messages = {
            "about": "✨ بوت تجريبي عربي بواجهة تفاعلية ولوحة إدارة خاصة.",
            "help": "📖 الأوامر المتاحة:\n/start — بدء البوت\n/help — عرض المساعدة\n/about — معلومات عن البوت",
        }
        text = get_setting("welcome", DEFAULT_WELCOME) if data == "home" else messages.get(data, DEFAULT_WELCOME)
        markup = user_keyboard()
    try:
        await query.edit_message_text(text, reply_markup=markup)
    except BadRequest as error:
        if "Message is not modified" not in str(error):
            raise


def _sign_session(expiry: int, csrf: str) -> str:
    payload = f"{expiry}.{csrf}"
    signature = hmac.new(DASHBOARD_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def _read_session(request: web.Request) -> tuple[bool, str | None]:
    if not DASHBOARD_SECRET:
        return False, None
    value = request.cookies.get(SESSION_COOKIE, "")
    try:
        expiry_text, csrf, signature = value.split(".", 2)
        expiry = int(expiry_text)
    except (ValueError, TypeError):
        return False, None
    payload = f"{expiry}.{csrf}"
    expected = hmac.new(DASHBOARD_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if expiry < int(time.time()) or not hmac.compare_digest(signature, expected):
        return False, None
    return True, csrf


def _secure_cookie(request: web.Request) -> bool:
    override = os.getenv("DASHBOARD_COOKIE_SECURE")
    if override is not None:
        return override.strip().lower() in {"1", "true", "yes"}
    return bool(os.getenv("FLY_APP_NAME")) or request.headers.get("X-Forwarded-Proto") == "https"


def _page(title: str, body: str) -> web.Response:
    document = f"""<!doctype html>
<html lang="ar" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#0b1020"><title>{html.escape(title)} | Server Bot</title>
<style>
:root{{--bg:#090e1b;--panel:#111a2d;--line:#24314b;--text:#edf3ff;--muted:#9aabc6;--accent:#7c9cff;--green:#41d6a3;--red:#ff7185}}
*{{box-sizing:border-box}}body{{margin:0;background:radial-gradient(ellipse at 85% -15%,#263d73 0,transparent 42%),var(--bg);color:var(--text);font-family:system-ui,-apple-system,"Segoe UI",sans-serif;min-height:100vh}}
.wrap{{max-width:1080px;margin:auto;padding:28px 20px 60px}}header{{display:flex;justify-content:space-between;align-items:center;gap:16px;margin-bottom:28px}}.brand{{display:flex;align-items:center;gap:13px}}.logo{{width:48px;height:48px;border-radius:16px;display:grid;place-items:center;background:linear-gradient(135deg,#607fff,#9b75ff);font-size:24px;box-shadow:0 10px 32px #526fff44}}h1{{font-size:22px;margin:0}}.sub,.muted{{color:var(--muted);font-size:14px;margin-top:4px}}.card{{background:linear-gradient(150deg,#141f35f5,#0e1728f5);border:1px solid var(--line);border-radius:20px;padding:22px;box-shadow:0 18px 55px #0003}}.grid{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px;margin-bottom:18px}}.metric{{min-height:125px}}.label{{color:var(--muted);font-size:14px}}.value{{font-size:28px;font-weight:750;margin-top:12px}}.status{{display:inline-flex;align-items:center;gap:8px;color:var(--green);font-size:15px;margin-top:14px}}.dot{{width:9px;height:9px;border-radius:50%;background:currentColor;box-shadow:0 0 14px currentColor}}.status.off{{color:#ffcb70}}.section-title{{font-size:18px;margin:0 0 6px}}textarea,input[type=password]{{width:100%;border-radius:12px;border:1px solid var(--line);background:#091222;color:var(--text);padding:13px 15px;font:inherit;outline:none}}textarea{{resize:vertical;min-height:130px;margin:14px 0}}textarea:focus,input:focus{{border-color:var(--accent);box-shadow:0 0 0 3px #7c9cff20}}.actions{{display:flex;flex-wrap:wrap;gap:10px}}button,.button{{border:0;border-radius:11px;padding:11px 17px;font:inherit;font-weight:700;color:white;background:linear-gradient(135deg,#547cff,#8a68ee);cursor:pointer;text-decoration:none}}button.secondary,.button.secondary{{background:#202d47;border:1px solid #33425f}}button.danger{{background:linear-gradient(135deg,#d94e70,#a93659)}}.notice{{padding:13px 15px;border-radius:12px;background:#102d2b;border:1px solid #23554c;color:#a6f1d7;margin-bottom:16px}}.error{{background:#351b28;border-color:#673047;color:#ffc0ce}}.login{{max-width:460px;margin:10vh auto}}.login form>*{{margin-top:14px}}footer{{text-align:center;color:var(--muted);font-size:12px;margin-top:24px}}.wide{{margin-bottom:18px}}
@media(max-width:720px){{.grid{{grid-template-columns:1fr}}header{{align-items:flex-start}}.wrap{{padding:20px 14px 40px}}.card{{padding:18px}}}}
</style></head><body>{body}</body></html>"""
    response = web.Response(text=document, content_type="text/html", charset="utf-8")
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    response.headers["Cache-Control"] = "no-store"
    return response


def _login_markup(csrf: str, error: bool = False) -> web.Response:
    alert = '<div class="notice error">بيانات الدخول غير صحيحة. حاول مرة أخرى.</div>' if error else ""
    body = f"""<main class="wrap"><section class="card login"><header><div class="brand"><div class="logo">🤖</div><div><h1>لوحة إدارة البوت</h1><div class="sub">تسجيل دخول آمن للمدير</div></div></div></header>{alert}<form method="post" action="/login" autocomplete="on"><input type="hidden" name="csrf" value="{html.escape(csrf, quote=True)}"><label class="label" for="password">كلمة مرور اللوحة</label><input id="password" name="password" type="password" autocomplete="current-password" required><button type="submit" style="width:100%">دخول إلى لوحة التحكم</button></form><footer>اتصال محمي • Server Bot</footer></section></main>"""
    return _page("تسجيل الدخول", body)


async def login_get(request: web.Request) -> web.Response:
    if not DASHBOARD_PASSWORD or not DASHBOARD_SECRET:
        return web.Response(text="Dashboard credentials are not configured.", status=503)
    valid, _ = _read_session(request)
    if valid:
        raise web.HTTPFound("/dashboard")
    csrf = secrets.token_urlsafe(24)
    response = _login_markup(csrf)
    response.set_cookie(
        "dashboard_login_csrf", csrf, httponly=True, secure=_secure_cookie(request),
        samesite="Strict", max_age=600, path="/",
    )
    return response


async def login_post(request: web.Request) -> web.Response:
    if not DASHBOARD_PASSWORD or not DASHBOARD_SECRET:
        return web.Response(text="Dashboard credentials are not configured.", status=503)
    form = await request.post()
    csrf_cookie = request.cookies.get("dashboard_login_csrf", "")
    csrf_form = str(form.get("csrf", ""))
    password = str(form.get("password", ""))
    if not csrf_cookie or not hmac.compare_digest(csrf_cookie, csrf_form):
        raise web.HTTPForbidden(text="Invalid request token.")
    if not hmac.compare_digest(password.encode(), DASHBOARD_PASSWORD.encode()):
        response = _login_markup(secrets.token_urlsafe(24), error=True)
        csrf = secrets.token_urlsafe(24)
        response = _login_markup(csrf, error=True)
        response.set_cookie(
            "dashboard_login_csrf", csrf, httponly=True, secure=_secure_cookie(request),
            samesite="Strict", max_age=600, path="/",
        )
        return response
    csrf = secrets.token_urlsafe(24)
    expiry = int(time.time()) + SESSION_TTL
    response = web.HTTPFound("/dashboard")
    response.set_cookie(
        SESSION_COOKIE, _sign_session(expiry, csrf), httponly=True,
        secure=_secure_cookie(request), samesite="Strict", max_age=SESSION_TTL, path="/",
    )
    response.del_cookie("dashboard_login_csrf", path="/")
    return response


def _dashboard_markup(request: web.Request, message: str = "") -> web.Response:
    _, csrf = _read_session(request)
    if not csrf:
        raise web.HTTPFound("/login")
    maintenance = get_setting("maintenance", "0") == "1"
    status_text = "تحت الصيانة" if maintenance else "يعمل بشكل طبيعي"
    status_class = "off" if maintenance else ""
    notice = f'<div class="notice">{html.escape(message)}</div>' if message else ""
    welcome = html.escape(get_setting("welcome", DEFAULT_WELCOME))
    body = f"""<main class="wrap"><header><div class="brand"><div class="logo">🤖</div><div><h1>لوحة إدارة البوت</h1><div class="sub">مركز التحكم التشغيلي • Server Bot</div></div></div><form method="post" action="/logout"><input type="hidden" name="csrf" value="{html.escape(csrf, quote=True)}"><button class="secondary" type="submit">تسجيل الخروج</button></form></header>{notice}
<section class="grid"><article class="card metric"><div class="label">إجمالي المستخدمين</div><div class="value">{total_users():,}</div><div class="muted">حسابات بدأت استخدام البوت</div></article><article class="card metric"><div class="label">حالة الخدمة</div><div class="status"><span class="dot"></span>الخدمة متصلة</div><div class="muted">واجهة الصحة: /health</div></article><article class="card metric"><div class="label">حالة البوت</div><div class="status {status_class}"><span class="dot"></span>{status_text}</div><div class="muted">المدير يستطيع استخدامه أثناء الصيانة</div></article></section>
<section class="card wide"><h2 class="section-title">إعدادات التشغيل</h2><p class="muted">تغيير فوري محفوظ في قاعدة البيانات الدائمة.</p><form method="post" action="/dashboard/action"><input type="hidden" name="csrf" value="{html.escape(csrf, quote=True)}"><input type="hidden" name="action" value="maintenance"><button class="{'secondary' if maintenance else 'danger'}" type="submit">{'إيقاف وضع الصيانة' if maintenance else 'تفعيل وضع الصيانة'}</button></form></section>
<section class="card wide"><h2 class="section-title">رسالة الترحيب</h2><p class="muted">تظهر للمستخدم عند إرسال /start.</p><form method="post" action="/dashboard/action"><input type="hidden" name="csrf" value="{html.escape(csrf, quote=True)}"><input type="hidden" name="action" value="welcome"><textarea name="welcome" maxlength="3500" required>{welcome}</textarea><div class="actions"><button type="submit">حفظ رسالة الترحيب</button></div></form></section>
<footer>لوحة خاصة • لا تشارك رابطها أو كلمة مرورها • يتم حفظ الإعدادات محليًا في قاعدة بيانات التطبيق</footer></main>"""
    return _page("لوحة التحكم", body)


async def dashboard_get(request: web.Request) -> web.Response:
    valid, _ = _read_session(request)
    if not valid:
        raise web.HTTPFound("/login")
    return _dashboard_markup(request, request.query.get("saved", ""))


async def dashboard_action(request: web.Request) -> web.Response:
    valid, csrf = _read_session(request)
    if not valid or not csrf:
        raise web.HTTPFound("/login")
    form = await request.post()
    if not hmac.compare_digest(str(form.get("csrf", "")), csrf):
        raise web.HTTPForbidden(text="Invalid request token.")
    action = str(form.get("action", ""))
    if action == "maintenance":
        new_state = "0" if get_setting("maintenance", "0") == "1" else "1"
        set_setting("maintenance", new_state)
        message = "تم تحديث وضع الصيانة."
    elif action == "welcome":
        text = str(form.get("welcome", "")).strip()
        if not text or len(text) > 3500:
            return _dashboard_markup(request, "الترحيب مطلوب ويجب ألا يتجاوز 3500 حرف.")
        set_setting("welcome", text)
        message = "تم حفظ رسالة الترحيب بنجاح."
    else:
        raise web.HTTPBadRequest(text="Unknown action.")
    return _dashboard_markup(request, message)


async def logout_post(request: web.Request) -> web.Response:
    valid, csrf = _read_session(request)
    if not valid or not csrf:
        raise web.HTTPFound("/login")
    form = await request.post()
    if not hmac.compare_digest(str(form.get("csrf", "")), csrf):
        raise web.HTTPForbidden(text="Invalid request token.")
    response = web.HTTPFound("/login")
    response.del_cookie(SESSION_COOKIE, path="/")
    return response


async def health_check(_: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "service": "telegram-bot"})


async def root_redirect(_: web.Request) -> web.Response:
    raise web.HTTPFound("/dashboard")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled Telegram update error", exc_info=context.error)


async def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN is not set. Add it as a Fly.io secret.")
    if not ADMIN_IDS:
        logger.warning("TELEGRAM_ADMIN_IDS is empty; Telegram admin commands are unavailable.")
    if not DASHBOARD_PASSWORD or not DASHBOARD_SECRET:
        logger.warning("Dashboard is disabled until DASHBOARD_PASSWORD and DASHBOARD_SECRET are configured.")

    initialize_database()
    port = int(os.getenv("PORT", "8080"))
    application = Application.builder().token(token).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("about", about_command))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("setwelcome", set_welcome_command))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_error_handler(error_handler)

    web_app = web.Application(client_max_size=16 * 1024)
    web_app.router.add_get("/", root_redirect)
    web_app.router.add_get("/health", health_check)
    web_app.router.add_get("/login", login_get)
    web_app.router.add_post("/login", login_post)
    web_app.router.add_get("/dashboard", dashboard_get)
    web_app.router.add_post("/dashboard/action", dashboard_action)
    web_app.router.add_post("/logout", logout_post)
    runner = web.AppRunner(web_app)

    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    await application.initialize()
    await application.start()
    if application.updater is None:
        raise RuntimeError("Telegram updater is unavailable.")
    try:
        await application.updater.start_polling()
        logger.info("Bot and web dashboard started on port %s", port)
        await asyncio.Event().wait()
    finally:
        if application.updater.running:
            await application.updater.stop()
        await application.stop()
        await application.shutdown()
        with suppress(Exception):
            await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped")
