import asyncio
import logging
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramRetryAfter, TelegramAPIError
from aiogram.types import BotCommand, BotCommandScopeDefault, BotCommandScopeChat, ErrorEvent

from config import API_TOKEN, ADMIN_IDS
from database import init_db, close_db

# استيراد الـ Routers
from handlers.admin import admin_router
from handlers.quiz import quiz_router
from handlers.common import common_router

# استيراد طبقة تقييد الطلبات (Throttling Middleware)
from middlewares.throttling import ThrottlingMiddleware

logging.basicConfig(level=logging.INFO)


async def _startup_call(description: str, coro_factory, attempts: int = 3, fatal: bool = True) -> bool:
    """Run one Telegram startup call with retry + exponential backoff.

    P0 (report_1 §1.1): a transient Telegram outage during boot must not kill
    the bot. Retries 3x (1s/2s/4s); TelegramRetryAfter honors the server's
    retry_after; any other Exception covers TelegramAPIError (BadRequest,
    NetworkError, ...) and transport failures. On final failure logs CRITICAL
    (fatal) or WARNING (expected, e.g. admin never started the bot) and
    returns False so main() continues into polling instead of crashing.
    """
    delay = 1.0
    for attempt in range(1, attempts + 1):
        try:
            await coro_factory()
            return True
        except TelegramRetryAfter as e:
            wait = float(getattr(e, "retry_after", 0) or delay)
            logging.warning(f"{description}: FloodControl (attempt {attempt}/{attempts}), waiting {wait:.1f}s")
            await asyncio.sleep(wait)
        except Exception as e:
            logging.warning(f"{description} failed (attempt {attempt}/{attempts}): {type(e).__name__}: {e}")
            if attempt < attempts:
                await asyncio.sleep(delay)
                delay *= 2.0
    if fatal:
        logging.critical(f"{description}: giving up after {attempts} attempts — continuing without it (non-fatal degradation).")
    else:
        logging.warning(f"{description}: giving up after {attempts} attempts — skipping (non-fatal).")
    return False


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

    # P0: retried — a Telegram outage at boot must not crash main().
    await _startup_call(
        "set_my_commands (default)",
        lambda: bot.set_my_commands(user_commands, scope=BotCommandScopeDefault()),
    )
    for admin_id in ADMIN_IDS:
        # fatal=False: per-admin failure is expected when that admin never
        # started the bot (Bot blocked) — skip quietly after retries.
        await _startup_call(
            f"set_my_commands (admin {admin_id})",
            lambda admin_id=admin_id: bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=admin_id)),
            fatal=False,
        )


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

    # P0: retried — a transient outage here must not abort startup. If the
    # webhook genuinely survives, polling will surface a clear Conflict error.
    webhook_ok = await _startup_call(
        "delete_webhook",
        lambda: bot.delete_webhook(drop_pending_updates=True),
    )
    if not webhook_ok:
        logging.critical("Proceeding to polling without confirmed webhook deletion.")
    logging.info("🚀 البوت جاهز ومحدث بالكامل مع ربط جميع الـ Routers وتفعيل طبقة الحماية!")

    await setup_bot_commands(bot)

    try:
        await dp.start_polling(bot)
    finally:
        # P0: gracefully drain/close the DB pool so no connection or
        # transaction is left hanging on SIGINT/SIGTERM (Rule 4).
        await close_db()
        await bot.session.close()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("تم إيقاف البوت بنجاح.")