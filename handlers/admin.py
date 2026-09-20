import aiosqlite
import asyncio
import html
import logging
import time
from datetime import datetime, timezone
from typing import Callable, Dict, Any, Awaitable
from aiogram import Router, types, Bot, F, BaseMiddleware
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramRetryAfter, TelegramAPIError, TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from cachetools import TTLCache

import re
from config import ADMIN_IDS, DB_NAME, SUPPORT_GROUP_ID

# CVE-2026-008: Rate limiting for key generation (max 10 per 60 seconds per user)
genkey_ratelimit = TTLCache(maxsize=100, ttl=60)
from database import (
    generate_new_key, revoke_user, revoke_key, revoke_key_by_rowid,
    get_user_subscription,
    BroadcastState, set_custom_text, get_custom_text, get_current_prices,
    get_user_full_details, get_users_page, get_users_count,
    get_active_keys, get_active_keys_count,
    VALID_TIPS_KEYS, VALID_PRICE_TYPES,
    _db_connect
)
from keyboards import (
    get_admin_panel, get_supervisor_panel,
    get_supervisors_management_menu, get_financial_settlement_menu,
    get_edit_texts_menu, get_edit_tips_menu, get_edit_prices_menu,
    get_keys_pagination_keyboard
)

admin_router = Router()

# CVE-2026-008: Rate limit helper for key generation
async def check_genkey_ratelimit(user_id: int) -> bool:
    """Check if user has exceeded key generation rate limit (10 per 60s). Returns True if allowed."""
    current = genkey_ratelimit.get(user_id, 0)
    if current >= 10:
        return False
    genkey_ratelimit[user_id] = current + 1
    return True

class AdminSupervisorState(StatesGroup):
    waiting_for_supervisor_info = State()
    waiting_for_user_query = State()  # حالة انتظار إدخال ID أو username الاستعلام

class AdminEditState(StatesGroup):
    waiting_for_text = State()
    waiting_for_price = State()

async def is_supervisor(user_id: int) -> bool:
    """معرفة ما إذا كان المستخدم مشرفاً نشطاً — P2 async/aio."""
    async with _db_connect() as conn:
        async with conn.execute('SELECT is_active FROM supervisors WHERE supervisor_id = ?', (user_id,)) as cursor:
            row = await cursor.fetchone()
            return row is not None and row[0] == 1

async def safe_edit_message_text(callback: types.CallbackQuery, text: str, reply_markup=None, parse_mode="HTML"):
    """دالة مساعدة للتعديل الآمن للرسائل وتفادي أخطاء محتوى الرسالة المتطابق"""
    try:
        await callback.message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            await callback.answer("ℹ️ أنت تقف بالفعل في الصفحة المطلوب الوصول إليها.", show_alert=False)
        else:
            raise e

# --- أداة فحص الصلاحيات المركزية (Authorization Middleware) ---

class AdminAuthMiddleware(BaseMiddleware):
    """
    ميدلوير مركزي يتحقق من أن المستخدم إما أدمن رئيسي أو مشرف نشط
    قبل السماح للطلب بالوصول إلى أي معالج داخل admin_router.
    P2: awaits is_supervisor (now async/aio, non-blocking).
    """
    async def __call__(
        self,
        handler: Callable[[types.TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: types.TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        user: types.User = data.get("event_from_user")
        
        if not user:
            return

        user_id = user.id

        # السماح فقط للأدمن أو المشرف النشط بالمرور
        if user_id in ADMIN_IDS or await is_supervisor(user_id):
            return await handler(event, data)

        # في حال عدم التمتع بالصلاحية: التعامل بحسب نوع الحدث مع إرسال استجابة واضحة (HIGH-008)
        if isinstance(event, types.Message):
            await event.answer("⚠️ عذراً، هذا الأمر مخصص للإدارة فقط.")
        elif isinstance(event, types.CallbackQuery):
            await event.answer("⚠️ غير مصرح.", show_alert=True)
        
        return

# تسجيل الميدلوير على مستوى الـ Router للرسائل والكولباك
admin_router.message.middleware(AdminAuthMiddleware())
admin_router.callback_query.middleware(AdminAuthMiddleware())


# --- أمر الإلغاء العام لحالات FSM ---

@admin_router.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    """معالج أمر /cancel للخروج من أي حالة FSM نشطة بشكل آمن"""
    current_state = await state.get_state()
    if current_state is None:
        await message.answer("ℹ️ لا توجد عملية معلقة لإلغائها.")
        return

    await state.clear()
    await message.answer("❌ تم إلغاء العملية الحالية والعودة للوضع الطبيعي.")


# --- أوامر الأدمن والمشرف الرئيسية ---

@admin_router.message(Command("admin"))
async def cmd_admin(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    if user_id in ADMIN_IDS:
        await message.answer("👑 <b>لوحة تحكم الأدمن الرئيسي:</b>", reply_markup=get_admin_panel(), parse_mode="HTML")
    elif await is_supervisor(user_id):
        await message.answer("🛠️ <b>لوحة تحكم المشرف / الموزع:</b>", reply_markup=get_supervisor_panel(), parse_mode="HTML")

@admin_router.message(Command("health"))
async def cmd_health(message: types.Message):
    """MED-014: Liveness probe — protected by AdminAuthMiddleware (admin_router)."""
    start = time.perf_counter()
    try:
        async with _db_connect() as conn:
            async with conn.execute('SELECT 1') as cursor:
                await cursor.fetchone()
    except Exception as e:
        await message.answer(f"❌ Database check failed: {type(e).__name__}: {e}")
        return
    latency_ms = (time.perf_counter() - start) * 1000
    now_iso = datetime.now(timezone.utc).isoformat()
    await message.answer(
        f"✅ Bot and Database are healthy.\n"
        f"⏱ DB latency: <b>{latency_ms:.1f} ms</b>\n"
        f"🕒 <code>{now_iso}</code>",
        parse_mode="HTML",
    )

@admin_router.callback_query(F.data == "admin_panel_back")
async def cb_admin_panel_back(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    user_id = callback.from_user.id
    if user_id in ADMIN_IDS:
        await safe_edit_message_text(callback, "👑 <b>لوحة تحكم الأدمن الرئيسي:</b>", reply_markup=get_admin_panel(), parse_mode="HTML")
    elif await is_supervisor(user_id):
        await safe_edit_message_text(callback, "🛠️ <b>لوحة تحكم المشرف / الموزع:</b>", reply_markup=get_supervisor_panel(), parse_mode="HTML")
    await callback.answer()

# --- قسم الاستعلام الشامل عن المستخدمين (الأدمن الرئيسي فقط) ---

@admin_router.callback_query(F.data.in_({"admin_search_user", "admin_query_user", "admin_list_users"}))
async def cb_admin_query_user_start(callback: types.CallbackQuery, state: FSMContext):
    """بدء عملية الاستعلام عن مستخدم معين (زر 🔍 بحث عن مستخدم).

    يستمع إلى admin_search_user الجديد مع إبقاء الأسماء القديمة
    (admin_query_user/admin_list_users) كأسماء بديلة لتوافق الأزرار
    القديمة في الرسائل السابقة قبل إعادة التشغيل.
    """
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⚠️ هذا الخيار مقتصر على الأدمن الرئيسي فقط.", show_alert=True)
        return

    await state.set_state(AdminSupervisorState.waiting_for_user_query)
    
    back_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 العودة للوحة الأدمن", callback_data="admin_panel_back")]
    ])
    
    await safe_edit_message_text(
        callback,
        "🔍 <b>الاستعلام عن مستخدم / معرفة الإحالات:</b>\n\n"
        "يرجى إرسال الـ ID الرقمي للمستخدم أو اسم المستخدم الخاص به (مع أو بدون @):\n\n"
        "<i>(يمكنك إرسال /cancel للإلغاء في أي وقت)</i>",
        reply_markup=back_kb,
        parse_mode="HTML"
    )
    await callback.answer()

@admin_router.message(AdminSupervisorState.waiting_for_user_query)
async def process_admin_query_user(message: types.Message, state: FSMContext):
    """معالج استقبال المدخلات واسترجاع بيانات المستخدم للأدمن الرئيسي"""
    if message.from_user.id not in ADMIN_IDS:
        return

    text = message.text.strip() if message.text else ""

    # التحقق مما إذا كان المدخل أمراً أرسله الأدمن (يبدأ بـ /)
    if text.startswith("/"):
        await state.clear()
        if text.startswith("/cancel"):
            await message.answer("❌ تم إلغاء عملية البحث.")
        else:
            # F-04: escape echoed admin input before HTML render.
            await message.answer(f"⚠️ تم إلغاء حالة البحث. أعد إرسال الأمر <code>{html.escape(text)}</code> لتنفيذه.", parse_mode="HTML")
        return

    # 1. إلغاء الحالة (Clear State) فور استلام القيمة وقبل أي عملية أخرى
    await state.clear()

    # 2. استخراج النص وتجريده من المسافات الزائدة
    raw_input = text
    if not raw_input:
        await message.answer("⚠️ يرجى إدخال معرف رقمي أو اسم مستخدم صحيح.")
        return

    # 3. معالجة القيمة والتمييز بين المعرف الرقمي (User ID) واستخدام اسم المستخدم (Username)
    if raw_input.isdigit():
        query_input = int(raw_input)
    else:
        # إزالة رمز @ إن وجد من بداية اسم المستخدم
        query_input = raw_input.lstrip("@")

    # 4. البحث في قاعدة البيانات باستعمال القيمة المعالجة
    user_data = await get_user_full_details(query_input)

    back_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 العودة للوحة الأدمن", callback_data="admin_panel_back")]
    ])

    if not user_data:
        # F-04: escape echoed query input before HTML render.
        await message.answer(
            f"❌ <b>عذراً، لم يتم العثور على أي مستخدم يطابق المدخل:</b> <code>{html.escape(raw_input)}</code>",
            reply_markup=back_kb,
            parse_mode="HTML"
        )
        return

    # تنسيق حالة الاشتراك
    sub_info = user_data.get("subscription", {})
    if sub_info.get("is_active"):
        sub_status = f"✅ <b>مفعّل</b> (النوع: {sub_info.get('type', 'غير معروف').upper()})"
    else:
        sub_status = "❌ <b>غير مفعّل</b>"

    # تنسيق بيانات الداعي (من قام بدعوته) — F-04: escape untrusted names.
    referrer_info = user_data.get("referrer")
    if referrer_info:
        _ref_name = html.escape(str(referrer_info.get('full_name', 'غير معروف')))
        _ref_user = html.escape(str(referrer_info.get('username')))
        ref_text = (
            f"👤 <b>اسم الداعي:</b> {_ref_name}\n"
            f"🆔 <b>ID الداعي:</b> <code>{referrer_info.get('user_id')}</code>\n"
            f"🔗 <b>يوزر الداعي:</b> @{_ref_user}" if referrer_info.get('username') != 'بدون يوزر' else f"👤 <b>اسم الداعي:</b> {_ref_name}\n🆔 <b>ID الداعي:</b> <code>{referrer_info.get('user_id')}</code>"
        )
    else:
        ref_text = "<i>انضم بشكل مباشر (بدون رابط إحالة)</i>"

    # صياغة وتنسيق الرسالة النهائية — F-04: escape untrusted user content.
    _u_full = html.escape(str(user_data.get('full_name', 'غير معروف')))
    _u_user = html.escape(str(user_data.get('username')))
    _u_key = html.escape(str(user_data.get('activated_via', 'لا يوجد')))
    details_text = (
        f"📊 <b>تقرير البيانات الشامل للمستخدم:</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 <b>معرف المستخدم (ID):</b> <code>{user_data.get('user_id')}</code>\n"
        f"👤 <b>الاسم الكامل:</b> {_u_full}\n"
        f"🌐 <b>اسم المستخدم:</b> @{_u_user}\n"
        f"📅 <b>تاريخ الانضمام:</b> {user_data.get('joined_at', 'غير معروف')}\n"
        f"💳 <b>كود التفعيل المستعمل:</b> <code>{_u_key}</code>\n\n"
        f"🎗️ <b>حالة الاشتراك:</b> {sub_status}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📈 <b>إحصائيات نقاط ورابط الإحالة:</b>\n"
        f"▫️ <b>إجمالي النقاط:</b> {user_data.get('referral_points', 0)} نقطة\n"
        f"▫️ <b>عدد الذين دعاهم:</b> {user_data.get('referred_count', 0)} مستخدم\n\n"
        f"👥 <b>معلومات من قام بدعوته:</b>\n"
        f"{ref_text}\n"
        f"━━━━━━━━━━━━━━━━━━━━━"
    )

    await message.answer(details_text, reply_markup=back_kb, parse_mode="HTML")

# --- إدارة وتعديل النصوص الديناميكية ---

@admin_router.callback_query(F.data == "admin_edit_texts")
async def cb_admin_edit_texts_menu(callback: types.CallbackQuery):
    """عرض قائمة اختيار الأقسام المتاحة للتعديل عند الضغط على 'تغيير النصوص'"""
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⚠️ مقتصر على الأدمن الرئيسي.", show_alert=True)
        return

    await safe_edit_message_text(
        callback,
        "✏️ <b>إدارة وتعديل النصوص:</b>\nاختر القسم الذي تريد تعديل رسالته الترحيبية/التعريفية:",
        reply_markup=get_edit_texts_menu(),
        parse_mode="HTML"
    )
    await callback.answer()


@admin_router.callback_query(F.data == "admin_edit_tips_menu")
async def cb_admin_edit_tips_menu(callback: types.CallbackQuery):
    """Tips per Teil sub-menu (CMS): pick which tips_{skill}_{teil} to edit."""
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⚠️ مقتصر على الأدمن الرئيسي.", show_alert=True)
        return

    await safe_edit_message_text(
        callback,
        "💡 <b>نصائح الأقسام (Tips per Teil):</b>\nاختر القسم الذي تريد تعديل نصيحته:",
        reply_markup=get_edit_tips_menu(),
        parse_mode="HTML"
    )
    await callback.answer()

@admin_router.callback_query(F.data.startswith("admin_edit_text_"))
async def cb_admin_start_edit_text(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⚠️ مقتصر على الأدمن الرئيسي.", show_alert=True)
        return

    key = callback.data.replace("admin_edit_text_", "")
    # Treat callback_data as UNTRUSTED (Rule 2): allow only known service keys + tips CMS keys.
    _allowed_service_keys = {
        "service_visa", "service_svu", "service_engineering", "free_services",
        "buy_courses", "subscribe_flow", "schreiben", "sprechen",
    }
    if not key or (key not in _allowed_service_keys and key not in VALID_TIPS_KEYS):
        await callback.answer("⚠️ مفتاح نص غير معروف.", show_alert=True)
        return
    await state.set_state(AdminEditState.waiting_for_text)
    await state.update_data(editing_key=key)

    current_val = await get_custom_text(key) or "لا يوجد نص مخصص حالياً (يتم استخدام النص الافتراضي)."
    # F-04: escape key + current text preview before HTML render.
    _key_esc = html.escape(str(key))
    _cur_esc = html.escape(str(current_val))
    await safe_edit_message_text(
        callback,
        f"📝 <b>تغيير النص الخاص بـ (<code>{_key_esc}</code>):</b>\n\n"
        f"<b>النص الحالي:</b>\n{_cur_esc}\n\n"
        f"✏️ <b>يرجى إرسال النص الجديد الآن (أو /cancel للإلغاء):</b>",
        parse_mode="HTML"
    )
    await callback.answer()

@admin_router.message(AdminEditState.waiting_for_text)
async def process_admin_save_text(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return

    text = message.text.strip() if message.text else ""
    if text.startswith("/"):
        await state.clear()
        if text.startswith("/cancel"):
            await message.answer("❌ تم إلغاء عملية تعديل النص.")
        else:
            # F-04: escape echoed admin input before HTML render.
            await message.answer(f"⚠️ تم إلغاء تعديل النص. أعد إرسال الأمر <code>{html.escape(text)}</code> لتنفيذه.", parse_mode="HTML")
        return

    data = await state.get_data()
    key = data.get("editing_key")

    if key and text:
        # Rule 16: keep prepended tips + list headers under Telegram 4096 cap.
        if len(text) > 3500:
            await message.answer("⚠️ النص طويل جداً (الحد 3500 حرف للنصائح/النصوص لضمان عدم تجاوز حد التلغرام 4096). يرجى اختصاره.")
            return
        await set_custom_text(key, text)
        await state.clear()
        # F-04: escape echoed key before HTML render.
        await message.answer(f"✅ تم تحديث ونشر النص الخاص بـ <code>{html.escape(str(key))}</code> بنجاح!", parse_mode="HTML")
    else:
        await message.answer("⚠️ حدث خطأ أثناء حفظ النص، يرجى المحاولة مجدداً.")

# --- إدارة أسعار الاشتراكات (Dynamic Prices CMS) ---

_PRICE_TYPE_NAMES = {
    "monthly": "💵 شهري (30 يوم)",
    "intensive": "⚡ مكثف (5 أيام)",
    "group": "👥 مجموعات (4 مستخدمين)",
}


@admin_router.callback_query(F.data == "admin_prices_menu")
async def cb_admin_prices_menu(callback: types.CallbackQuery):
    """Show current CMS prices + edit menu (main admin only)."""
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⚠️ مقتصر على الأدمن الرئيسي.", show_alert=True)
        return

    prices = await get_current_prices()
    await safe_edit_message_text(
        callback,
        "💰 <b>أسعار الاشتراكات الحالية:</b>\n"
        f"💵 شهري (30 يوم): <b>${prices.get('monthly', 10)}</b>\n"
        f"⚡ مكثف (5 أيام): <b>${prices.get('intensive', 5)}</b>\n"
        f"👥 مجموعات (4 مستخدمين): <b>${prices.get('group', 20)}</b>\n\n"
        "اختر السعر الذي تريد تعديله:",
        reply_markup=get_edit_prices_menu(),
        parse_mode="HTML",
    )
    await callback.answer()


@admin_router.callback_query(F.data.startswith("admin_edit_price_"))
async def cb_admin_start_edit_price(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⚠️ مقتصر على الأدمن الرئيسي.", show_alert=True)
        return

    # Treat callback_data as UNTRUSTED (Rule 2): strict allowlist.
    price_type = (callback.data.replace("admin_edit_price_", "") or "").strip().lower()
    if price_type not in VALID_PRICE_TYPES:
        await callback.answer("⚠️ نوع سعر غير معروف.", show_alert=True)
        return

    await state.set_state(AdminEditState.waiting_for_price)
    await state.update_data(price_type=price_type)

    prices = await get_current_prices()
    current = prices.get(price_type)
    type_name = _PRICE_TYPE_NAMES.get(price_type, price_type)
    await safe_edit_message_text(
        callback,
        f"💰 <b>تعديل سعر {html.escape(str(type_name))}:</b>\n"
        f"السعر الحالي: <b>${current}</b>\n\n"
        "✏️ <b>يرجى إرسال السعر الجديد كرقم صحيح فقط (مثال: 10).</b>\n"
        "(أو /cancel للإلغاء)",
        parse_mode="HTML",
    )
    await callback.answer()


@admin_router.message(AdminEditState.waiting_for_price)
async def process_admin_save_price(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return

    text = message.text.strip() if message.text else ""
    if text.startswith("/"):
        await state.clear()
        if text.startswith("/cancel"):
            await message.answer("❌ تم إلغاء عملية تعديل السعر.")
        else:
            # F-04: escape echoed admin input before HTML render.
            await message.answer(f"⚠️ تم إلغاء تعديل السعر. أعد إرسال الأمر <code>{html.escape(text)}</code> لتنفيذه.", parse_mode="HTML")
        return

    data = await state.get_data()
    price_type = (data.get("price_type") or "").strip().lower()
    if price_type not in VALID_PRICE_TYPES:
        await state.clear()
        await message.answer("⚠️ حدث خطأ أثناء حفظ السعر (نوع غير معروف)، يرجى المحاولة مجدداً.")
        return

    if not text.isdigit():
        await message.answer("⚠️ يرجى إدخال السعر كرقم صحيح فقط (مثال: 10).")
        return

    value = int(text)
    if value <= 0 or value > 10000:
        await message.answer("⚠️ السعر يجب أن يكون بين 1 و 10000.")
        return

    await set_custom_text(f"price_{price_type}", str(value))
    await state.clear()
    type_name = _PRICE_TYPE_NAMES.get(price_type, price_type)
    await message.answer(f"✅ تم تحديث سعر {html.escape(str(type_name))} إلى <b>${value}</b> بنجاح!", parse_mode="HTML")

# --- إمكانية إنشاء مفاتيح للأدمن ---

@admin_router.message(Command("genkey"))
async def cmd_genkey(message: types.Message):
    if message.from_user.id in ADMIN_IDS:
        if not await check_genkey_ratelimit(message.from_user.id):
            await message.answer("⚠️ تم تجاوز الحد الأقصى لتوليد المفاتيح. انتظر دقيقة.")
            return
        args = message.text.split()
        sub_type = "b1"
        max_uses = 1
        if len(args) > 1 and args[1].lower() in ["b1", "b2", "all", "intensive", "group"]:
            sub_type = args[1].lower()
            
        if sub_type == "b2":
            await message.answer("⚠️ <b>مستوى B2 غير متاح حالياً.</b> لا يمكن إنشاء أكواد تفعيل لهذا المستوى.", parse_mode="HTML")
            return

        if sub_type == "group":
            max_uses = 4

        new_key = await generate_new_key(sub_type, created_by=message.from_user.id, max_uses=max_uses)
        uses_info = f" (يسمح لـ {max_uses} مستخدمين)" if max_uses > 1 else ""
        await message.answer(f"🔑 <b>كود تفعيل جديد ({sub_type.upper()}):</b>\n<code>{new_key}</code>{uses_info}", parse_mode="HTML")

@admin_router.callback_query(F.data.startswith("admin_gen_"))
async def cb_admin_gen_key(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⚠️ مقتصر على الأدمن الرئيسي.", show_alert=True)
        return
    
    if not await check_genkey_ratelimit(callback.from_user.id):
        await callback.answer("⚠️ تم تجاوز الحد الأقصى لتوليد المفاتيح. انتظر دقيقة.", show_alert=True)
        return
        
    sub_type = callback.data.replace("admin_gen_", "")
    if sub_type == "b2":
        await callback.answer("⚠️ مستوى B2 غير متاح حالياً.", show_alert=True)
        return
    max_uses = 4 if sub_type == "group" else 1
    new_key = await generate_new_key(sub_type, created_by=callback.from_user.id, max_uses=max_uses)
    uses_info = f"\n• الأقصى للاستخدام: <b>{max_uses} مستخدمين</b>" if max_uses > 1 else ""
    await callback.message.answer(f"🔑 <b>كود تفعيل جديد ({sub_type.upper()}):</b>\n<code>{new_key}</code>{uses_info}", parse_mode="HTML")
    await callback.answer()

# --- لوحة ووظائف المشرفين ---

@admin_router.callback_query(F.data.startswith("sup_gen_"))
async def cb_sup_gen_key(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    if not (await is_supervisor(user_id) or user_id in ADMIN_IDS):
        await callback.answer("⚠️ لا تملك صلاحيات المشرف.", show_alert=True)
        return
    
    if not await check_genkey_ratelimit(user_id):
        await callback.answer("⚠️ تم تجاوز الحد الأقصى لتوليد المفاتيح. انتظر دقيقة.", show_alert=True)
        return

    action = callback.data.replace("sup_gen_", "")
    max_uses = 1
    prices = await get_current_prices()
    if action == "monthly_b1":
        sub_type = "b1"
        price = prices.get('monthly', 10)
    elif action == "monthly_b2":
        await callback.answer("⚠️ مستوى B2 غير متاح حالياً.", show_alert=True)
        return
    elif action == "intensive":
        sub_type = "intensive"
        price = prices.get('intensive', 5)
    elif action == "group":
        sub_type = "group"
        price = prices.get('group', 20)
        max_uses = 4
    else:
        sub_type = "b1"
        price = prices.get('monthly', 10)

    new_key = await generate_new_key(sub_type, created_by=user_id, max_uses=max_uses)
    uses_text = f"\n• عدد المستخدمين المسموح: <b>{max_uses}</b>" if max_uses > 1 else ""
    await callback.message.answer(
        f"🔑 <b>كود تفعيل جديد من المشرف:</b>\n"
        f"• الكود: <code>{new_key}</code>\n"
        f"• النوع: <b>{sub_type.upper()}</b>\n"
        f"• السعر المحسوب: <b>${price}</b>{uses_text}",
        parse_mode="HTML"
    )
    await callback.answer()

@admin_router.callback_query(F.data == "sup_stats")
async def cb_sup_stats(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    if not (await is_supervisor(user_id) or user_id in ADMIN_IDS):
        await callback.answer("⚠️ لا تملك صلاحية الوصول.", show_alert=True)
        return

    async with _db_connect() as conn:
        async with conn.execute('SELECT COUNT(*), SUM(used_count) FROM keys WHERE created_by = ?', (user_id,)) as cursor:
            row = await cursor.fetchone()
            total_created = row[0] or 0
            total_used = row[1] or 0

        async with conn.execute('''
            SELECT u.user_id, p.full_name, u.sub_type, u.activated_via
            FROM users u
            LEFT JOIN user_profiles p ON u.user_id = p.user_id
            JOIN keys k ON u.activated_via = k.key_code
            WHERE k.created_by = ?
        ''', (user_id,)) as cursor:
            activated_users = await cursor.fetchall()

    text = f"📊 <b>إحصائيات الأكواد الخاصة بك:</b>\n"
    text += f"▫️ إجمالي الأكواد المنشأة: <b>{total_created}</b>\n"
    text += f"▫️ إجمالي عدد الاستخدامات: <b>{total_used}</b>\n\n"
    text += "👥 <b>قائمة العملاء الذين فعّلوا عبر أكوادك:</b>\n"
    
    if activated_users:
        for uid, name, s_type, k_code in activated_users:
            # F-04: escape untrusted client names; tolerate legacy NULL sub_type.
            _cname = html.escape(str(name or 'غير معروف'))
            text += f"• <b>{_cname}</b> (<code>{uid}</code>) - [{(s_type or 'b1').upper()}]\n"
    else:
        text += "لا يوجد عملاء قاموا بالتفعيل بعد.\n"

    # F-08: Telegram 4096-char cap — chunk aggregated stats like finish_teil3.
    MAX_CHUNK = 4000
    if len(text) > MAX_CHUNK:
        chunks = [text[i:i + MAX_CHUNK] for i in range(0, len(text), MAX_CHUNK)]
        for chunk in chunks[:-1]:
            await callback.message.answer(chunk, parse_mode="HTML")
        await callback.message.answer(chunks[-1], parse_mode="HTML")
    else:
        await callback.message.answer(text, parse_mode="HTML")
    await callback.answer()

@admin_router.callback_query(F.data == "sup_finance")
async def cb_sup_finance(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    if not (await is_supervisor(user_id) or user_id in ADMIN_IDS):
        await callback.answer("⚠️ لا تملك صلاحية الوصول.", show_alert=True)
        return

    async with _db_connect() as conn:
        async with conn.execute('''
            SELECT sub_type, is_settled, COALESCE(SUM(used_count), 0)
            FROM keys
            WHERE created_by = ?
            GROUP BY sub_type, is_settled
        ''', (user_id,)) as cursor:
            rows = await cursor.fetchall()

    unsettled_amount = 0
    settled_amount = 0

    prices = await get_current_prices()
    for sub_type, is_settled, sold in rows:
        if sub_type == 'intensive':
            price = prices.get('intensive', 5)
        elif sub_type == 'group':
            price = prices.get('group', 20)
        else:
            price = prices.get('monthly', 10)

        total = price * (sold or 0)
        if is_settled == 1:
            settled_amount += total
        else:
            unsettled_amount += total

    text = f"💳 <b>حسابك المالي والتسويات:</b>\n\n"
    text += f"🔴 المستحقات المعلقة (غير التسوية): <b>${unsettled_amount}</b>\n"
    text += f"🟢 المستحقات المسواة والمعلّمة كمدفوعة: <b>${settled_amount}</b>\n\n"
    text += "ℹ️ <i>ملاحظة: يتم تسديد المستحقات المعلقة للادمن الرئيسي مباشرة لتصفير الرصيد.</i>"

    await callback.message.answer(text, parse_mode="HTML")
    await callback.answer()

# --- إدارة المشرفين للأدمن الرئيسي ---

@admin_router.callback_query(F.data == "admin_manage_supervisors")
async def cb_manage_supervisors(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⚠️ مقتصر على الأدمن الرئيسي.", show_alert=True)
        return

    async with _db_connect() as conn:
        async with conn.execute('''
            SELECT s.supervisor_id, s.supervisor_name, p.full_name, p.username, s.is_active
            FROM supervisors s
            LEFT JOIN user_profiles p ON s.supervisor_id = p.user_id
        ''') as cursor:
            supervisors = await cursor.fetchall()

    kb = get_supervisors_management_menu(supervisors)
    await safe_edit_message_text(callback, "⚙️ <b>إدارة المشرفين والموزعين:</b>\nيمكنك تفعيل/تعطيل أو حذف المشرفين هنا.", reply_markup=kb, parse_mode="HTML")
    await callback.answer()

@admin_router.callback_query(F.data == "sup_add_new")
async def cb_sup_add_new(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⚠️ مقتصر على الأدمن الرئيسي.", show_alert=True)
        return

    await state.set_state(AdminSupervisorState.waiting_for_supervisor_info)
    await callback.message.answer(
        "➕ <b>إضافة مشرف جديد:</b>\n\n"
        "أرسل الـ ID الخاص بالمشرف والاسم المخصص له بالشكل التالي:\n"
        "<code>ID الاسم المخصص</code>\n\n"
        "<i>مثال:</i>\n"
        "<code>123456789 المشرف أحمد</code>\n\n"
        "أو أرسل الـ ID فقط بشكل عادي دون اسم.\n"
        "(أو /cancel للإلغاء)",
        parse_mode="HTML"
    )
    await callback.answer()

@admin_router.message(AdminSupervisorState.waiting_for_supervisor_info)
async def process_add_supervisor(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return

    text = message.text.strip() if message.text else ""
    if text.startswith("/"):
        await state.clear()
        if text.startswith("/cancel"):
            await message.answer("❌ تم إلغاء إضافة المشرف.")
        else:
            # F-04: escape echoed admin input before HTML render.
            await message.answer(f"⚠️ تم إلغاء الإضافة. أعد إرسال الأمر <code>{html.escape(text)}</code> لتنفيذه.", parse_mode="HTML")
        return

    text_parts = text.split(maxsplit=1) if text else []
    if not text_parts:
        await message.answer("⚠️ يرجى إرسال بيانات المشرف.")
        return

    try:
        sup_id = int(text_parts[0])
        sup_name = text_parts[1].strip() if len(text_parts) > 1 else None
        now_str = datetime.now(timezone.utc).isoformat()
        
        async with _db_connect() as conn:
            await conn.execute('''
                INSERT INTO supervisors (supervisor_id, supervisor_name, role, is_active, created_at)
                VALUES (?, ?, 'supervisor', 1, ?)
                ON CONFLICT(supervisor_id) DO UPDATE SET 
                    is_active = 1,
                    supervisor_name = COALESCE(excluded.supervisor_name, supervisors.supervisor_name)
            ''', (sup_id, sup_name, now_str))
            await conn.commit()

        await state.clear()
        # F-04: escape admin-typed display name before HTML render.
        name_display = f" ({html.escape(sup_name)})" if sup_name else ""
        await message.answer(f"✅ تم إضافة المستخدم <code>{sup_id}</code>{name_display} كمشرف بنجاح!", parse_mode="HTML")
    except ValueError:
        await message.answer("⚠️ يرجى التأكد من كتابة الـ ID كأرقام صحيحة أولاً.\nمثال: <code>123456789 المشرف أحمد</code>", parse_mode="HTML")

@admin_router.callback_query(F.data.startswith("sup_toggle_"))
async def cb_sup_toggle(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⚠️ غير مصرح.", show_alert=True)
        return

    try:
        sup_id = int(callback.data.replace("sup_toggle_", ""))
    except ValueError:
        await callback.answer("⚠️ خطأ في البيانات.", show_alert=True)
        return
    async with _db_connect() as conn:
        await conn.execute('UPDATE supervisors SET is_active = CASE WHEN is_active = 1 THEN 0 ELSE 1 END WHERE supervisor_id = ?', (sup_id,))
        await conn.commit()
    
    await cb_manage_supervisors(callback)

@admin_router.callback_query(F.data.startswith("sup_delete_"))
async def cb_sup_delete(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⚠️ غير مصرح.", show_alert=True)
        return

    try:
        sup_id = int(callback.data.replace("sup_delete_", ""))
    except ValueError:
        await callback.answer("⚠️ خطأ في البيانات.", show_alert=True)
        return
    async with _db_connect() as conn:
        await conn.execute('DELETE FROM supervisors WHERE supervisor_id = ?', (sup_id,))
        await conn.commit()
    
    await callback.answer("🗑️ تم حذف المشرف بنجاح.")
    await cb_manage_supervisors(callback)

# --- لوحة التسوية المالية للأدمن الرئيسي ---

@admin_router.callback_query(F.data == "admin_financial_settlement")
async def cb_financial_settlement(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⚠️ مقتصر على الأدمن الرئيسي.", show_alert=True)
        return

    # F13: single query with LEFT JOIN + GROUP BY — no N+1 per-supervisor lookups.
    async with _db_connect() as conn:
        async with conn.execute('''
            SELECT s.supervisor_id, s.supervisor_name, p.full_name,
                   k.sub_type, COALESCE(SUM(k.used_count), 0) AS sold
            FROM supervisors s
            LEFT JOIN user_profiles p ON p.user_id = s.supervisor_id
            LEFT JOIN keys k ON k.created_by = s.supervisor_id AND k.is_settled = 0
            GROUP BY s.supervisor_id, k.sub_type
        ''') as cursor:
            rows = await cursor.fetchall()

    grouped = {}
    prices = await get_current_prices()
    for sup_id, sup_name, profile_name, sub_type, sold in rows:
        if sup_id not in grouped:
            display_name = sup_name or profile_name or f"ID: {sup_id}"
            grouped[sup_id] = {"display_name": display_name, "count": 0, "total": 0}
        sold = sold or 0
        if sub_type is None:
            continue
        grouped[sup_id]["count"] += sold
        if sub_type == 'intensive':
            price = prices.get('intensive', 5)
        elif sub_type == 'group':
            price = prices.get('group', 20)
        else:
            price = prices.get('monthly', 10)
        grouped[sup_id]["total"] += price * sold

    settlements = [(s_id, v["display_name"], v["count"], v["total"]) for s_id, v in grouped.items()]

    kb = get_financial_settlement_menu(settlements)
    await safe_edit_message_text(callback, "💰 <b>التسوية المالية للمشرفين:</b>\nفيما يلي المبالغ والأكواد المتبقية غير المسواة:", reply_markup=kb, parse_mode="HTML")
    await callback.answer()

@admin_router.callback_query(F.data == "noop")
async def cb_noop(callback: types.CallbackQuery):
    """LOW-011: placeholder buttons must answer to prevent client hang."""
    await callback.answer("⏳ قريباً...", show_alert=True)

@admin_router.callback_query(F.data.startswith("settle_sup_"))
async def cb_settle_sup(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⚠️ غير مصرح.", show_alert=True)
        return

    try:
        sup_id = int(callback.data.replace("settle_sup_", ""))
    except ValueError:
        await callback.answer("⚠️ خطأ في البيانات.", show_alert=True)
        return
    async with _db_connect() as conn:
        await conn.execute('UPDATE keys SET is_settled = 1 WHERE created_by = ? AND is_settled = 0', (sup_id,))
        await conn.commit()

    await callback.answer("✅ تمت تسوية مستحقات المشرف بنجاح!", show_alert=True)
    await cb_financial_settlement(callback)

# --- الأوامر القديمة الحالية مع الحفاظ عليها ---

@admin_router.message(Command("revoke"))
async def cmd_revoke(message: types.Message):
    if message.from_user.id in ADMIN_IDS:
        try:
            target_id = int(message.text.split()[1])
            ok = await revoke_user(target_id)
            if not ok:
                await message.answer("❌ لم يتم العثور على المستخدم.", parse_mode="HTML")
                return
            await message.answer(f"🚫 تم إلغاء تفعيل المستخدم <code>{target_id}</code> بنجاح.", parse_mode="HTML")
        except (IndexError, ValueError):
            await message.answer("⚠️ الاستخدام: <code>/revoke 12345678</code>", parse_mode="HTML")

@admin_router.message(Command("revoke_key"))
async def cmd_revoke_key(message: types.Message):
    """Revoke a generated key — protected by AdminAuthMiddleware (admin_router)."""
    # Defense in depth — middleware already gates admin_router, keep explicit check
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer("⚠️ الاستخدام: <code>/revoke_key KEY-XXX-...</code>", parse_mode="HTML")
        return
    key_code = parts[1].strip()
    # F-04: escape echoed key material before HTML render.
    _key_esc = html.escape(key_code)
    ok = await revoke_key(key_code)
    if not ok:
        await message.answer(f"❌ لم يتم العثور على المفتاح <code>{_key_esc}</code>.", parse_mode="HTML")
    else:
        await message.answer(f"✅ تم إبطال المفتاح <code>{_key_esc}</code> بنجاح — أصبح غير صالح للاستخدام.", parse_mode="HTML")

# --- Active Keys Management Dashboard (paginated, Rule 15/19/20) ---

KEYS_PAGE_LIMIT = 10


async def _get_keys_page_data(page: int):
    """Fetch one page of active keys + pagination metadata (single source of truth)."""
    try:
        page = int(page)
    except (TypeError, ValueError):
        page = 1
    page = max(1, page)
    total = await get_active_keys_count()
    total_pages = max(1, (total + KEYS_PAGE_LIMIT - 1) // KEYS_PAGE_LIMIT)
    page = min(page, total_pages)
    offset = (page - 1) * KEYS_PAGE_LIMIT
    rows = await get_active_keys(KEYS_PAGE_LIMIT, offset)
    return rows, page, total_pages, total


def _format_keys_text(rows: list, page: int, total_pages: int, total: int) -> str:
    """Build the dashboard message text (HTML-escaped, Rule 8)."""
    text = f"🔑 <b>الأكواد الفعالة (صفحة {page}/{total_pages})</b>\n"
    text += f"📊 الإجمالي: <b>{total}</b>\n\n"
    if not rows:
        text += "ℹ️ لا توجد أكواد فعالة حالياً.\n"
        return text
    for item in rows:
        # get_active_keys returns (rowid, key_code, sub_type, used_count, max_uses)
        if isinstance(item, dict):
            key_code = str(item.get("key_code", ""))
            sub_type = str(item.get("sub_type") or "b1")
            used_count = item.get("used_count", 0)
            max_uses = item.get("max_uses", 1)
        elif isinstance(item, (list, tuple)) and len(item) >= 3:
            key_code = str(item[1])
            sub_type = str(item[2] or "b1")
            used_count = item[3] if len(item) > 3 else 0
            max_uses = item[4] if len(item) > 4 else 1
        else:
            continue
        _kc = html.escape(key_code)
        _st = html.escape(str(sub_type).upper())
        text += f"• <code>{_kc}</code> [{_st}] ({used_count}/{max_uses})\n"
    return text


def _extract_current_keys_page(callback: types.CallbackQuery) -> int:
    """Infer the current keys-dashboard page from the message being acted on.

    Rule 19 requires re-fetching the *current* page after revoke. The revoke
    callback itself only carries the key, so recover the page from the
    pagination buttons (admin_keys_page_N) or the message text (صفحة N).
    Defaults to 1 when nothing parseable is found. Never raises.
    """
    try:
        markup = getattr(getattr(callback, "message", None), "reply_markup", None)
        if markup and getattr(markup, "inline_keyboard", None):
            for row in markup.inline_keyboard:
                for btn in row:
                    data = getattr(btn, "callback_data", "") or ""
                    if data.startswith("admin_keys_page_"):
                        try:
                            return max(1, int(data.replace("admin_keys_page_", "")))
                        except ValueError:
                            continue
        text = getattr(getattr(callback, "message", None), "text", "") or ""
        import re
        m = re.search(r"صفحة\s+(\d+)", text)
        if m:
            return max(1, int(m.group(1)))
    except Exception:
        pass
    return 1


@admin_router.callback_query(F.data == "admin_manage_keys")
async def cb_admin_manage_keys(callback: types.CallbackQuery):
    """Open the Active Keys dashboard at page 1 (main admin only)."""
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⚠️ مقتصر على الأدمن الرئيسي.", show_alert=True)
        return
    rows, page, total_pages, total = await _get_keys_page_data(1)
    text = _format_keys_text(rows, page, total_pages, total)
    kb = get_keys_pagination_keyboard(rows, page, total_pages)
    await safe_edit_message_text(callback, text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@admin_router.callback_query(F.data.startswith("admin_keys_page_"))
async def cb_admin_keys_page(callback: types.CallbackQuery):
    """Paginate the Active Keys dashboard (callback data is UNTRUSTED — validate)."""
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⚠️ مقتصر على الأدمن الرئيسي.", show_alert=True)
        return
    try:
        page = int(callback.data.replace("admin_keys_page_", ""))
    except ValueError:
        await callback.answer("⚠️ رقم صفحة غير صالح.", show_alert=True)
        return
    if page < 1:
        await callback.answer("⚠️ رقم صفحة غير صالح.", show_alert=True)
        return
    rows, page, total_pages, total = await _get_keys_page_data(page)
    if not rows and total > 0:
        # Page beyond range after concurrent revokes — clamp to last page.
        rows, page, total_pages, total = await _get_keys_page_data(total_pages)
    text = _format_keys_text(rows, page, total_pages, total)
    kb = get_keys_pagination_keyboard(rows, page, total_pages)
    await safe_edit_message_text(callback, text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@admin_router.callback_query(F.data.startswith("rev_key"))
async def cb_revoke_key_inline(callback: types.CallbackQuery):
    """Inline revoke from the Active Keys dashboard (Rule 19: instant UI refresh).

    Supports rev_key_<key_code> and the short fallback rev_keyid_<rowid>
    (used when key_code would exceed the 64-byte callback limit).
    Stale buttons clicked after a restart raise TelegramBadRequest
    ("query is too old") — each answer is guarded with
    try...except TelegramBadRequest + logging.debug.
    """
    if callback.from_user.id not in ADMIN_IDS:
        try:
            await callback.answer("⚠️ مقتصر على الأدمن الرئيسي.", show_alert=True)
        except TelegramBadRequest:
            logging.debug("Ignored old callback query")
        return
    data = callback.data or ""
    revoked_code = None
    found = False
    try:
        if data.startswith("rev_keyid_"):
            # Short-callback path: resolve via DB rowid (no client-trusted key material).
            try:
                rowid = int(data.replace("rev_keyid_", ""))
            except ValueError:
                try:
                    await callback.answer("⚠️ بيانات غير صالحة.", show_alert=True)
                except TelegramBadRequest:
                    logging.debug("Ignored old callback query")
                return
            revoked_code = await revoke_key_by_rowid(rowid)
            found = revoked_code is not None
        elif data.startswith("rev_keyh_"):
            try:
                await callback.answer("⚠️ انتهت صلاحية هذا الزر. أعد فتح إدارة الأكواد.", show_alert=True)
            except TelegramBadRequest:
                logging.debug("Ignored old callback query")
            return
        elif data.startswith("rev_key_"):
            key_code = data[len("rev_key_"):].strip()
            if not key_code:
                try:
                    await callback.answer("⚠️ بيانات غير صالحة.", show_alert=True)
                except TelegramBadRequest:
                    logging.debug("Ignored old callback query")
                return
            # Treat callback_data as UNTRUSTED: parameterized revoke, no string SQL.
            found = await revoke_key(key_code)
            revoked_code = key_code if found else None
        else:
            try:
                await callback.answer("⚠️ بيانات غير صالحة.", show_alert=True)
            except TelegramBadRequest:
                logging.debug("Ignored old callback query")
            return
    except Exception:
        logging.exception("Error in cb_revoke_key_inline")
        try:
            await callback.answer("❌ حدث خطأ أثناء الإبطال.", show_alert=True)
        except TelegramBadRequest:
            logging.debug("Ignored old callback query")
        return

    if not found:
        try:
            await callback.answer("❌ المفتاح غير موجود أو تم إبطاله مسبقاً.", show_alert=True)
        except TelegramBadRequest:
            logging.debug("Ignored old callback query")
        return

    # Rule 19: tactile feedback + instant removal from the list.
    try:
        await callback.answer("✅ تم إبطال المفتاح", show_alert=True)
    except TelegramBadRequest:
        logging.debug("Ignored old callback query")
    current_page = _extract_current_keys_page(callback)
    rows, page, total_pages, total = await _get_keys_page_data(current_page)
    if not rows and total > 0 and page > 1:
        # Revoked the last item on the last page — step back one page.
        rows, page, total_pages, total = await _get_keys_page_data(page - 1)
    text = _format_keys_text(rows, page, total_pages, total)
    kb = get_keys_pagination_keyboard(rows, page, total_pages)
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise

@admin_router.message(Command("users"))
async def cmd_users(message: types.Message):
    """معالج أمر النص /users لعرض المستخدمين مع ترقيم الصفحات (MED-001)."""
    if message.from_user.id in ADMIN_IDS:
        await _send_users_page(message, page=1)


def _build_users_page_text_and_kb(page: int, rows: list, total_users: int):
    """Shared builder for the paginated users list (text + keyboard).

    Single source of truth reused by _send_users_page (/users command),
    cb_admin_users_list (👥 panel button), and cb_admin_users_page
    (pagination). F12: no N+1 — rows already carry sub_type/expire_date.
    """
    from datetime import datetime, timezone
    limit = 10
    text = f"📋 <b>قائمة المستخدمين (صفحة {page}):</b>\n\n"
    for uid, full_name, username, sub_type, expire_date in rows:
        is_active = False
        if expire_date and str(expire_date).strip():
            try:
                exp = datetime.fromisoformat(str(expire_date).replace('Z', '+00:00'))
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                is_active = datetime.now(timezone.utc) < exp
            except (ValueError, TypeError):
                is_active = False
        sub_status = f"✅ مفعّل ({(sub_type or 'b1').upper()})" if is_active else "❌ غير مفعّل"
        # F-04: escape untrusted profile fields.
        _fname = html.escape(str(full_name or 'غير معروف'))
        _uname = html.escape(str(username or 'بدون'))
        text += f"👤 <b>{_fname}</b> (@{_uname})\n🆔 <code>{uid}</code> | {sub_status}\n"

    # Pagination keyboard
    total_pages = (total_users + limit - 1) // limit
    kb_buttons = []
    if page > 1:
        kb_buttons.append(InlineKeyboardButton(text="⬅️ السابق", callback_data=f"admin_users_page_{page - 1}"))
    if page < total_pages:
        kb_buttons.append(InlineKeyboardButton(text="التالي ➡️", callback_data=f"admin_users_page_{page + 1}"))

    kb = InlineKeyboardMarkup(inline_keyboard=[kb_buttons]) if kb_buttons else None
    return text, kb


async def _send_users_page(message: types.Message, page: int):
    """Helper to fetch and send a specific page of users (reuses shared builder)."""
    limit = 10
    offset = (page - 1) * limit

    total_users = await get_users_count()
    if total_users == 0:
        await message.answer("لا يوجد مستخدمون مسجلون بعد.")
        return

    rows = await get_users_page(limit, offset)
    if not rows:
        await message.answer("لا يوجد مستخدمون في هذه الصفحة.")
        return

    text, kb = _build_users_page_text_and_kb(page, rows, total_users)
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@admin_router.callback_query(F.data == "admin_users_list")
async def cb_admin_users_list(callback: types.CallbackQuery):
    """عرض الصفحة الأولى من قائمة المستخدمين (زر 👥 قائمة المستخدمين).

    Reuses the same fetch + render logic as cmd_users/_send_users_page
    via the shared _build_users_page_text_and_kb() builder.
    """
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⚠️ مقتصر على الأدمن الرئيسي.", show_alert=True)
        return

    limit = 10
    page = 1
    total_users = await get_users_count()
    if total_users == 0:
        await safe_edit_message_text(callback, "لا يوجد مستخدمون مسجلون بعد.", parse_mode="HTML")
        await callback.answer()
        return

    rows = await get_users_page(limit, 0)
    if not rows:
        await safe_edit_message_text(callback, "لا يوجد مستخدمون في هذه الصفحة.", parse_mode="HTML")
        await callback.answer()
        return

    text, kb = _build_users_page_text_and_kb(page, rows, total_users)
    await safe_edit_message_text(callback, text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@admin_router.callback_query(F.data.startswith("admin_users_page_"))
async def cb_admin_users_page(callback: types.CallbackQuery):
    """Handle pagination clicks for users list."""
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⚠️ مقتصر على الأدمن الرئيسي.", show_alert=True)
        return

    try:
        page = max(1, int(callback.data.replace("admin_users_page_", "")))
    except ValueError:
        await callback.answer("⚠️ رقم صفحة غير صالح.", show_alert=True)
        return

    limit = 10
    offset = (page - 1) * limit

    total_users = await get_users_count()
    rows = await get_users_page(limit, offset)

    if not rows:
        await callback.answer("لا يوجد مستخدمون في هذه الصفحة.", show_alert=True)
        return

    text, kb = _build_users_page_text_and_kb(page, rows, total_users)
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()

@admin_router.message(Command("broadcast"))
async def cmd_broadcast(message: types.Message, state: FSMContext):
    if message.from_user.id in ADMIN_IDS:
        await state.set_state(BroadcastState.waiting_for_message)
        await message.answer("📢 أرسل النص أو الرسالة التي تريد بثها لجميع المستخدمين (أو /cancel للإلغاء):")

async def _run_broadcast_task(bot: Bot, admin_chat_id: int, message_id: int, users: list):
    """دالة خلفية لعملية البث لتجنب تجميد البوت مع التقييد الديناميكي لأخطاء 429.
    HIGH-002 (concurrency model): up to 30 concurrent sends via semaphore;
    per-recipient failures never abort the broadcast."""
    semaphore = asyncio.Semaphore(30)

    async def _send_one(user_id: int) -> bool:
        async with semaphore:
            try:
                await bot.copy_message(chat_id=user_id, from_chat_id=admin_chat_id, message_id=message_id)
                return True
            except TelegramRetryAfter as e:
                # الانتظار الديناميكي المطلوب من التلغرام عند تجاوَز الحدود
                await asyncio.sleep(e.retry_after)
                # Retry once after waiting
                try:
                    await bot.copy_message(chat_id=user_id, from_chat_id=admin_chat_id, message_id=message_id)
                    return True
                except Exception:
                    return False
            except Exception:
                return False

    results = []
    for i in range(0, len(users), 500):
        chunk = users[i:i + 500]
        tasks = [_send_one(u[0]) for u in chunk]
        chunk_results = await asyncio.gather(*tasks)
        results.extend(chunk_results)
    success = sum(1 for ok in results if ok)
    failed = len(results) - success

    try:
        await bot.send_message(
            chat_id=admin_chat_id,
            text=f"✅ <b>تم الانتهاء من عملية البث الجماعي!</b>\n\n"
                 f"▫️ نجح الإرسال: <b>{success}</b>\n"
                 f"▫️ فشل الإرسال (حظر/حساب مغلق): <b>{failed}</b>",
            parse_mode="HTML"
        )
    except Exception:
        pass

@admin_router.message(BroadcastState.waiting_for_message)
async def process_broadcast(message: types.Message, state: FSMContext, bot: Bot):
    text = message.text.strip() if message.text else ""
    if text.startswith("/cancel"):
        await state.clear()
        await message.answer("❌ تم إلغاء عملية البث الجماعي.")
        return

    # P1 Strike 3 (Admin UX Hazard): 2-step verification — do NOT broadcast immediately.
    # Store the pending message reference, send a preview, require explicit confirm.
    await state.update_data(
        broadcast_message_id=message.message_id,
        broadcast_from_chat_id=message.chat.id,
    )

    # 1. Preview: echo the exact pending content back to the admin
    try:
        await bot.copy_message(
            chat_id=message.chat.id,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
        )
    except Exception:
        pass

    async with _db_connect() as conn:
        async with conn.execute('SELECT COUNT(*) FROM user_profiles') as cursor:
            row = await cursor.fetchone()
            total_users = row[0] if row and row[0] else 0

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Confirm Broadcast", callback_data="admin_broadcast_confirm"),
        InlineKeyboardButton(text="❌ Cancel", callback_data="admin_broadcast_cancel"),
    ]])
    await message.answer(
        f"📢 <b>Preview above — please confirm the broadcast.</b>\n\n"
        f"👥 <b>Recipients:</b> <b>{total_users}</b> users\n"
        f"Press ✅ Confirm Broadcast to send, or ❌ Cancel to abort. (Or /cancel)",
        reply_markup=kb,
        parse_mode="HTML",
    )


@admin_router.callback_query(F.data == "admin_broadcast_confirm")
async def cb_broadcast_confirm(callback: types.CallbackQuery, state: FSMContext, bot: Bot):
    # Protected by AdminAuthMiddleware (router-level) + explicit check for defense in depth.
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⚠️ مقتصر على الأدمن الرئيسي.", show_alert=True)
        return

    data = await state.get_data()
    message_id = data.get("broadcast_message_id")
    from_chat_id = data.get("broadcast_from_chat_id")
    if not message_id or not from_chat_id:
        await callback.answer("⚠️ لا توجد رسالة بث معلقة. أرسل /broadcast أولاً.", show_alert=True)
        return

    await state.clear()

    async with _db_connect() as conn:
        async with conn.execute('SELECT user_id FROM user_profiles') as cursor:
            users = await cursor.fetchall()

    await callback.message.answer("🚀 <b>بدأت عملية البث الجماعي في الخلفية.</b>\nيمكنك استخدام البوت ولوحة التحكم بشكل طبيعي، وستتلقى تقريراً فور الانتهاء.", parse_mode="HTML")
    await callback.answer()

    # تشغيل مهمة الإرسال في الخلفية لعدم تجميد الأحداث الأخرى
    # source = stored from_chat_id (original admin message); report goes to admin chat.
    asyncio.create_task(
        _run_broadcast_task(
            bot=bot,
            admin_chat_id=from_chat_id,
            message_id=message_id,
            users=users
        )
    )


@admin_router.callback_query(F.data == "admin_broadcast_cancel")
async def cb_broadcast_cancel(callback: types.CallbackQuery, state: FSMContext):
    # Protected by AdminAuthMiddleware (router-level) + explicit check for defense in depth.
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⚠️ مقتصر على الأدمن الرئيسي.", show_alert=True)
        return

    await state.clear()
    await callback.answer("❌ تم إلغاء عملية البث الجماعي.", show_alert=False)
    try:
        await callback.message.edit_text("❌ <b>تم إلغاء عملية البث الجماعي. لم يتم إرسال أي رسالة.</b>", parse_mode="HTML")
    except Exception:
        try:
            await callback.message.answer("❌ <b>تم إلغاء عملية البث الجماعي. لم يتم إرسال أي رسالة.</b>", parse_mode="HTML")
        except Exception:
            pass


# --- Admin-side ticket reply (replies to users via metadata in support group) ---

@admin_router.message(F.chat.id == SUPPORT_GROUP_ID, F.reply_to_message, F.text)
async def cb_admin_reply_to_ticket(message: types.Message):
    """Reply to a user's support ticket.

    Listens ONLY in the support group (F.chat.id == SUPPORT_GROUP_ID)
    and ONLY on replies (F.reply_to_message) with text content.
    Extracts the user_id from the replied-to metadata message
    (🔍 the '🆔 ID: <code>...</code>' line), then copies the admin's
    reply to that user.
    """
    if SUPPORT_GROUP_ID == 0:
        await message.reply("⚠️ نظام الدعم غير مُكوّن.")
        return

    replied_msg = message.reply_to_message
    if replied_msg is None:
        return

    # Parse user_id from the metadata message text.
    # Format sent by user side: 👤 من: {name}\n🆔 ID: <code>{user_id}</code>
    user_id: int | None = None
    text = replied_msg.text or ""
    m = re.search(r"🆔 ID:\s*<code>(\d+)</code>", text)
    if not m:
        # Fallback: try plain integer near ID
        m2 = re.search(r"🆔 ID:\s*(\d+)", text)
        if m2:
            user_id = int(m2.group(1))
    else:
        user_id = int(m.group(1))

    if user_id is None:
        logging.warning(f"Admin reply in support group could not extract user_id from: {text!r}")
        await message.reply("⚠️ تعذر استخراج معرف المستخدم من الرسالة المُشار إليها.")
        return

    try:
        await message.copy_to(user_id)
        await message.reply("✅ تم إرسال الرد للمستخدم.")
    except Exception as e:
        logging.exception("Failed to send admin reply to ticket user")
        await message.reply("❌ تعذر إرسال الرد. قد يكون المستخدم قام بحظر البوت.")
