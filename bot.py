import asyncio
import logging
import os
import sqlite3
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
        db.execute(
            "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        db.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('maintenance', '0')"
        )
        db.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('welcome', ?) ",
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
            [InlineKeyboardButton("📊 إحصائيات البوت", callback_data="admin:stats")],
            [InlineKeyboardButton("🚧 تبديل وضع الصيانة", callback_data="admin:maintenance")],
            [InlineKeyboardButton("📝 طريقة تعديل الترحيب", callback_data="admin:welcome")],
            [InlineKeyboardButton("🔄 تحديث اللوحة", callback_data="admin:refresh")],
        ]
    )


async def maintenance_message(update: Update) -> bool:
    user = update.effective_user
    if get_setting("maintenance", "0") != "1" or is_admin(user.id if user else None):
        return False
    if update.effective_message:
        await update.effective_message.reply_text(
            "🛠️ البوت تحت الصيانة حاليًا. يرجى المحاولة لاحقًا."
        )
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
            "✨ بوت تجريبي عربي بواجهة تفاعلية ولوحة تحكم خاصة بالمدير.",
            reply_markup=user_keyboard(),
        )


def admin_dashboard_text() -> str:
    maintenance = "مفعّل 🔴" if get_setting("maintenance", "0") == "1" else "متوقف 🟢"
    return (
        "🛡️ لوحة تحكم البوت\n\n"
        f"👥 المستخدمون المسجلون: {total_users()}\n"
        f"🚧 وضع الصيانة: {maintenance}\n\n"
        "اختر أحد الخيارات لإدارة البوت."
    )


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
            await update.effective_message.reply_text(
                "استخدم الأمر هكذا:\n/setwelcome أهلًا بك في بوتنا 👋"
            )
        return
    set_setting("welcome", text)
    if update.effective_message:
        await update.effective_message.reply_text(
            "✅ تم تحديث رسالة الترحيب بنجاح.",
            reply_markup=admin_keyboard(),
        )


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
            text = (
                "📊 إحصائيات البوت\n\n"
                f"👥 إجمالي المستخدمين المسجلين: {total_users()}\n"
                f"🚧 الصيانة: {'مفعّلة' if get_setting('maintenance', '0') == '1' else 'متوقفة'}"
            )
        elif data == "admin:maintenance":
            enabled = get_setting("maintenance", "0") != "1"
            set_setting("maintenance", "1" if enabled else "0")
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
            "about": "✨ بوت تجريبي عربي بواجهة تفاعلية ولوحة تحكم خاصة بالمدير.",
            "help": "📖 الأوامر المتاحة:\n/start — بدء البوت\n/help — عرض المساعدة\n/about — معلومات عن البوت",
        }
        text = get_setting("welcome", DEFAULT_WELCOME) if data == "home" else messages.get(data, DEFAULT_WELCOME)
        markup = user_keyboard()

    try:
        await query.edit_message_text(text, reply_markup=markup)
    except BadRequest as error:
        if "Message is not modified" not in str(error):
            raise


async def health_check(_: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "service": "telegram-bot"})


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled Telegram update error", exc_info=context.error)


async def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN is not set. Add it with: fly secrets set BOT_TOKEN=...")
    if not ADMIN_IDS:
        logger.warning("TELEGRAM_ADMIN_IDS is empty; the admin panel will be unavailable.")

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

    web_app = web.Application()
    web_app.router.add_get("/", health_check)
    web_app.router.add_get("/health", health_check)
    runner = web.AppRunner(web_app)

    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    await application.initialize()
    await application.start()
    if application.updater is None:
        raise RuntimeError("Telegram updater is unavailable.")

    try:
        await application.updater.start_polling()
        logger.info("Bot started; health endpoint listens on port %s", port)
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
