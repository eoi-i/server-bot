import asyncio
import logging
import os
from contextlib import suppress

from aiohttp import web
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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

WELCOME_TEXT = (
    "أهلًا بك! 👋\n\n"
    "أنا بوت تجريبي جاهز للعمل على Fly.io. استخدم الأزرار بالأسفل للتنقل، "
    "أو أرسل /help لعرض المساعدة."
)


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✨ عن البوت", callback_data="about")],
            [InlineKeyboardButton("📖 المساعدة", callback_data="help")],
            [InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="home")],
        ]
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(WELCOME_TEXT, reply_markup=main_keyboard())


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = "📖 *المساعدة*\n\n/start — بدء البوت\n/help — عرض هذه المساعدة\n/about — معلومات عن البوت"
    if update.message:
        await update.message.reply_text(
            text, parse_mode="Markdown", reply_markup=main_keyboard()
        )


async def about_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(
            "✨ هذا بوت تجريبي بسيط بواجهة عربية وأزرار تفاعلية، مُعدّ للاستضافة على Fly.io.",
            reply_markup=main_keyboard(),
        )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()

    messages = {
        "about": "✨ هذا بوت تجريبي بسيط بواجهة عربية وأزرار تفاعلية، مُعدّ للاستضافة على Fly.io.",
        "help": "📖 الأوامر المتاحة:\n/start — بدء البوت\n/help — عرض المساعدة\n/about — معلومات عن البوت",
        "home": WELCOME_TEXT,
    }
    await query.edit_message_text(
        messages.get(query.data, WELCOME_TEXT), reply_markup=main_keyboard()
    )


async def health_check(_: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "service": "telegram-bot"})


async def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN is not set. Add it as a Fly.io secret.")

    port = int(os.getenv("PORT", "8080"))
    application = Application.builder().token(token).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("about", about_command))
    application.add_handler(CallbackQueryHandler(button_handler))

    web_app = web.Application()
    web_app.router.add_get("/", health_check)
    web_app.router.add_get("/health", health_check)
    runner = web.AppRunner(web_app)

    await application.initialize()
    await application.start()
    if application.updater is None:
        raise RuntimeError("Telegram updater is unavailable.")
    await application.updater.start_polling()
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    logger.info("Bot is running; health endpoint listens on port %s", port)

    try:
        await asyncio.Event().wait()
    finally:
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
