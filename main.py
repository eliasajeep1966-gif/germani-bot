import asyncio
import logging
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import ErrorEvent

from config import API_TOKEN
from database import init_db

# استيراد الـ Routers
from handlers.admin import admin_router
from handlers.quiz import quiz_router
from handlers.common import common_router

# استيراد طبقة تقييد الطلبات (Throttling Middleware)
from middlewares.throttling import ThrottlingMiddleware

# إعداد الـ Logging لمعرفة حالة التشغيل والتحديثات
logging.basicConfig(level=logging.INFO)

async def main():
    # تهيئة قاعدة البيانات — P2 async/aio (non-blocking)
    await init_db()

    bot = Bot(token=API_TOKEN)
    dp = Dispatcher()

    # ربط الـ Middleware
    throttling_mw = ThrottlingMiddleware(slow_mode_delay=0.5)
    dp.message.middleware(throttling_mw)
    dp.callback_query.middleware(throttling_mw)

    # ربط الـ Routers بالترتيب المعتمد
    dp.include_router(admin_router)
    dp.include_router(quiz_router)
    dp.include_router(common_router)

    # Global error handler — prevents crash, logs all exceptions
    @dp.errors()
    async def global_error_handler(event: ErrorEvent):
        exception = event.exception
        # F-10: never swallow cancellation/exit signals — let shutdown proceed.
        if isinstance(exception, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
            raise exception
        if isinstance(exception, TelegramRetryAfter):
            logging.warning(f"Telegram rate limit hit: retry_after={exception.retry_after}s | event={event}")
            return True
        logging.exception(f"Unhandled exception {type(exception).__name__}: {exception} | event={event}")
        # Returning True signals that the exception is handled and should not propagate
        return True

    # حذف الـ Webhook وتنظيف التحديثات المعلقة عند البدء
    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("🚀 البوت جاهز ومحدث بالكامل مع ربط جميع الـ Routers وتفعيل طبقة الحماية!")

    try:
        await dp.start_polling(bot)
    finally:
        # إغلاق جلسة البوت بشكل آمن لتفادي تعليق الاتصالات عند إعادة التشغيل
        await bot.session.close()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("تم إيقاف البوت بنجاح.")