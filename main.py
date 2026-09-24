import asyncio
import logging
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import BotCommand, BotCommandScopeDefault, BotCommandScopeChat, ErrorEvent

from config import API_TOKEN, ADMIN_IDS
from database import init_db

# استيراد الـ Routers
from handlers.admin import admin_router
from handlers.quiz import quiz_router
from handlers.common import common_router

# استيراد طبقة تقييد الطلبات (Throttling Middleware)
from middlewares.throttling import ThrottlingMiddleware

logging.basicConfig(level=logging.INFO)


async def setup_bot_commands(bot: Bot):
    """Role-based slash-commands menu (Rule 10 admin UX: explicit, no mass-action).

    - Default scope: user commands for everyone.
    - Per-admin chat scope: user commands + admin commands.
    - Per-chat set failures are logged and ignored (admin hasn't started bot yet).
    """
    user_commands = [
        BotCommand(command="start", description="القائمة الرئيسية 🏠"),
        BotCommand(command="cancel", description="إلغاء العملية الحالية ❌"),
    ]
    admin_commands = [
        *user_commands,
        BotCommand(command="admin", description="لوحة الإدارة ⚙️"),
        BotCommand(command="genkey", description="توليد كود جديد 🔑"),
        BotCommand(command="revoke_key", description="إبطال كود ❌"),
        BotCommand(command="users", description="قائمة المستخدمين 👥"),
        BotCommand(command="broadcast", description="إرسال إذاعة 📢"),
        BotCommand(command="health", description="حالة السيرفر 🩺"),
    ]

    await bot.set_my_commands(user_commands, scope=BotCommandScopeDefault())
    for admin_id in ADMIN_IDS:
        try:
            await bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=admin_id))
        except Exception as e:
            logging.warning(f"Failed to set admin commands for {admin_id}: {type(e).__name__}: {e}")


async def main():
    await init_db()

    bot = Bot(token=API_TOKEN)

    dp = Dispatcher()

    throttling_mw = ThrottlingMiddleware(slow_mode_delay=0.5)
    dp.message.middleware(throttling_mw)
    dp.callback_query.middleware(throttling_mw)

    dp.include_router(admin_router)
    dp.include_router(quiz_router)
    dp.include_router(common_router)

    @dp.errors()
    async def global_error_handler(event: ErrorEvent):
        exception = event.exception
        if isinstance(exception, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
            raise exception
        if isinstance(exception, TelegramRetryAfter):
            logging.warning(f"Telegram rate limit hit: retry_after={exception.retry_after}s | event={event}")
            return True
        logging.exception(f"Unhandled exception {type(exception).__name__}: {exception} | event={event}")
        return True

    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("🚀 البوت جاهز ومحدث بالكامل مع ربط جميع الـ Routers وتفعيل طبقة الحماية!")

    await setup_bot_commands(bot)

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("تم إيقاف البوت بنجاح.")