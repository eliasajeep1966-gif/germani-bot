import time
import logging
from typing import Any, Awaitable, Callable, Dict
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, User, CallbackQuery, Message
from cachetools import TTLCache

class ThrottlingMiddleware(BaseMiddleware):
    def __init__(self, slow_mode_delay: float = 0.5):
        super().__init__()
        self.delay = slow_mode_delay
        self.user_timestamps: TTLCache[str, float] = TTLCache(maxsize=10000, ttl=2.0)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        user: User = data.get("event_from_user")
        if user:
            user_id = user.id
            # HIGH-001 correction: composite key per action type so a message
            # cannot consume/refresh the callback budget and vice versa.
            if isinstance(event, CallbackQuery):
                event_type = "callback"
            elif isinstance(event, Message):
                event_type = "message"
            else:
                event_type = type(event).__name__.lower()
            throttle_key = f"{user_id}:{event_type}"
            current_time = time.time()
            last_time = self.user_timestamps.get(throttle_key, 0.0)

            # إذا كان الوقت المنقضي أقل من المهلة المحددة، يتم تجاهل الطلب لحماية الخادم
            if current_time - last_time < self.delay:
                # HIGH-001: Provide feedback for throttled CallbackQuery
                if isinstance(event, CallbackQuery):
                    try:
                        await event.answer("⚠️ الرجاء عدم الضغط بسرعة.", show_alert=True)
                    except Exception:
                        pass
                return

            self.user_timestamps[throttle_key] = current_time

        return await handler(event, data)