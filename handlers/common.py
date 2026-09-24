import asyncio
import html
import logging
import os
import json
from aiogram import Router, types, Bot, F
from aiogram.filters import CommandStart, CommandObject, Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramBadRequest
from cachetools import TTLCache
import time

from config import LOG_CHANNEL_ID, SUPPORT_GROUP_ID, BASE_DIR
from datetime import datetime
from utils import get_catalog, get_content, render_progress_bar, map_filename
from database import (
    AuthState, QuizState, SupportState, save_user_profile, get_user_subscription,
    reset_user_progress, get_skill_progress, activate_subscription,
    is_text_completed, set_user_referrer, process_referral_reward,
    get_referral_info, claim_free_subscription, get_custom_text,
    get_user_answer_stats, get_next_uncompleted_target, can_access_level,
    get_user_dashboard_stats,
    FALLBACK_TIPS
)
from keyboards import (
    get_main_menu, get_training_menu, get_services_menu,
    get_b1_skills, get_referral_menu, get_dynamic_subscribe_text,
    get_cancel_to_main_keyboard
)

common_router = Router()

# In-bot ticketing rate limiter: max 3 messages per 300s per user (TTL anti-DoS, Rule 8)
support_rate_limit: TTLCache = TTLCache(maxsize=10000, ttl=300)

_TIPS_MISSING_MARKER = "لا يوجد نص محدد لهذه الخدمة حالياً."

async def get_teil_tip(skill: str, teil: str) -> str:
    """CMS fetch for per-Teil tips (admin-editable).

    Key: tips_{skill}_{teil} (lowercased). Returns DB custom text when present,
    else the hardcoded FALLBACK_TIPS default (keeps UX identical pre-seed),
    else "" when unknown skill/teil. Never raises — callers always get a string.
    """
    try:
        skill_n = (skill or "").lower().strip()
        teil_n = (teil or "").lower().strip()
        # Normalize ASCII alias (hoeren) to canonical (hören) for CMS keys.
        if skill_n == "hoeren":
            skill_n = "hören"
        tips_key = f"tips_{skill_n}_{teil_n}"
        try:
            tips_text = await get_custom_text(tips_key)
        except Exception:
            logging.warning(f"get_custom_text failed for {tips_key}")
            tips_text = None
        if tips_text and _TIPS_MISSING_MARKER not in str(tips_text):
            txt = str(tips_text).strip()
            if txt:
                # Ensure trailing blank line when prepending to list headers.
                return txt if txt.endswith("\n\n") else txt + "\n\n"
        return FALLBACK_TIPS.get(tips_key, "")
    except Exception:
        logging.exception("get_teil_tip failed")
        return ""

async def safe_edit_message_text(callback: types.CallbackQuery, text: str, reply_markup=None, parse_mode=None, protect_content=False):
    """دالة مساعدة لمعالجة استثناء عدم تعديل محتوى الرسالة في التلغرام وبشكل مرن"""
    try:
        if protect_content:
            await callback.message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
        else:
            await callback.message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            await callback.answer("ℹ️ أنت تقف بالفعل في الصفحة المطلوب الوصول إليها.", show_alert=False)
        else:
            try:
                await callback.message.delete()
                await callback.message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)
            except TelegramBadRequest as e2:
                if "message is not modified" not in str(e2):
                    logging.error(f"safe_edit fallback TelegramBadRequest: {e2}")
            except Exception as e2:
                logging.error(f"safe_edit fallback failed: {type(e2).__name__}: {e2}")

@common_router.channel_post()
async def catch_channel_post(post: types.Message):
    logging.info(f"Channel post received — chat id: {post.chat.id}")

@common_router.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext, bot: Bot, command: CommandObject = None):
    await state.clear()
    
    # استدعاء save_user_profile والحصول على القيمة المنطقية لمعرفة هل المستخدم جديد (True) أم مسجل مسبقاً (False)
    is_new_user = await save_user_profile(message.from_user)
    
    # معالجة نظام الإحالة بغض النظر عن حالة المستخدم
    # F-05: referral handling is standalone — onboarding below runs for ALL users.
    if command and command.args:
        args = command.args
        if args.startswith("ref_"):
            ref_parts = args.split("_")
            if len(ref_parts) >= 2 and ref_parts[1].strip():
                try:
                    referrer_id = int(ref_parts[1])
                    if referrer_id != message.from_user.id:
                        await set_user_referrer(message.from_user.id, referrer_id)
                except ValueError:
                    pass

    # F-05: new-user onboarding (log + full disclaimer) runs regardless of start args.
    if is_new_user:
        # للمستخدم الجديد فقط: إرسال إشعار بقناة السجلات وإرسال الرسالة الترحيبية الشاملة
        # F-04: escape untrusted profile fields before HTML render.
        username_val = f"@{html.escape(message.from_user.username)}" if message.from_user.username else "بدون"
        _new_name = html.escape(str(message.from_user.full_name))
        log_msg = (
            f"👤 <b>مستخدم جديد دخل البوت:</b>\n"
            f"▫️ <b>الاسم:</b> {_new_name}\n"
            f"▫️ <b>اليوزر:</b> {username_val}\n"
            f"🆔 <b>الـ ID:</b> <code>{message.from_user.id}</code>"
        )
        if LOG_CHANNEL_ID:
            try:
                await bot.send_message(LOG_CHANNEL_ID, log_msg, parse_mode="HTML")
            except Exception as e:
                logging.error(f"Failed to send log to channel: {e}")

        welcome_message = (
            "⚠️ تنبيه مهم قبل البدء\n"
            "هذا البوت هو أداة مساعدة للتدريب ومراجع "
            "دورات اللغة الالمانية، وليس بديلًا عن دراسة المنهاج أو عن الدروس التعليمية، بل "
            "هو وسيلة للحفظ والمراجع فقط.\n"
            "الدورات "
            "لا تضمن النجاح لكن تساعد على تخفيف ضغط الوقت على الطالب في الامتحان، يوجد احتمال "
            "ألا يحوي الفحص على دورات لذلك عليكم التحضير بشكل جيد.\n"
            "كما أن "
            "الدورات هي عبارة عن تجميع الطلاب للأسئلة والأجوبة بعد كل امتحان لذلك حتى الدورات "
            "الموجودة بكل الملفات قد تحتوي على حلول خاطئة لذلك يرجى التركيز ضمن الفحص.\n\n"
            "📚 الدورات مترجمة الى الألمانية ترجمة "
            "تقريبية لتحاكي الفحص قدر الإمكان ومن الوارد وجود أخطاء.\n"
            "الدورات "
            "الموجودة في البوت هي نتاج تجميع للعديد من النماذج والتجارب المنتشرة.\n"
            "المحادثة والكتابة في البوت أكتفينا "
            "بكتابة تلخيص لتجربة الامتحان مع الدورات.\n\n"
            "❗ النص الأول ضمن كل جزء من "
            "الأقسام موجود بشكل مجاني لتجربة البوت قبل الاشتراك،كما يمكن تفحص عدد النصوص دون الاشتراك كما أن ملفات المحادثة "
            "والكتابة موجودة مجانا لكن بشكل مؤقت.\n\n"
            "🎯 هدف البوت: هذاالبوت هو أداة "
            "مساعدة للحفظ والمراجعة بحيث تظهر الدورات على شكل سؤال وجواب.\n\n"
            "نتمنى لكم التوفيق والنجاح إن شاء الله"
        )

        await message.answer(
            welcome_message,
            reply_markup=get_main_menu(),
            parse_mode="HTML",
            protect_content=True
        )
    else:
        # للمستخدم المسجل مسبقاً (القديم): فتح القائمة الرئيسية مباشرة وبشكل مختصر
        await message.answer(
            "أهلاً بك مجدداً! اختر من القائمة التالية للبدء:",
            reply_markup=get_main_menu(),
            protect_content=True
        )

@common_router.message(AuthState.waiting_for_key)
async def process_key(message: types.Message, state: FSMContext, bot: Bot):
    if not message.text:
        await message.answer("⚠️ يرجى إرسال كود التفعيل كنص مكتوب فقط.")
        return
    user_key = message.text.strip()
    try:
        success, sub_type = await activate_subscription(message.from_user.id, user_key)
    except Exception as e:
        logging.error(f"activate_subscription failed for user {message.from_user.id}: {type(e).__name__}: {e}")
        await message.answer("⚠️ حدث خطأ في السيرفر أثناء تفعيل الكود. يرجى إبلاغ الدعم.")
        return
    if success:
        # FSM Blackhole fix: resume an interrupted quiz instead of wiping it.
        # Pointers refactor: quiz sessions keep only skill/teil/file_name + counters.
        data = await state.get_data()
        if "file_name" in data and "current_index" in data:
            await state.set_state(QuizState.answering)
            resume_quiz = True
        else:
            await state.clear()
            resume_quiz = False

        referrer_id, new_points = await process_referral_reward(message.from_user.id)
        if referrer_id:
            try:
                msg_text = f"🎉 <b>قام أحد المستخدمين المدعوين من قبلك بتفعيل اشتراكه!</b>\nحصلت على نقطة إحالة. إجمالي نقاطك الآن: <b>{new_points}</b>"
                if new_points >= 2:
                    msg_text += "\n🎁 <b>تهانينا! لقد حصلت على تفعيل مجاني لتجمعك نقطتي إحالة!</b> يمكنك استهلاكه من قسم الإحالات."
                await bot.send_message(referrer_id, msg_text, parse_mode="HTML")
            except Exception as e:
                logging.error(f"Failed to send referral notification to {referrer_id}: {e}")

        type_names = {
            "b1": "المستوى B1",
            "b2": "المستوى B2",
            "all": "الباقة الشاملة B1+B2",
            "intensive": "المراجعة المكثفة (5 أيام)",
            "group": "باقة المجموعات"
        }
        type_str = type_names.get(sub_type, sub_type.upper())
        days_str = "5 أيام" if sub_type == "intensive" else "30 يوماً"
        
        username_val = f"@{html.escape(message.from_user.username)}" if message.from_user.username else "بدون"
        _act_name = html.escape(str(message.from_user.full_name))
        _act_key = html.escape(str(user_key))
        log_msg = (
            f"🎉 <b>تفعيل اشتراك جديد!</b>\n\n"
            f"👤 <b>المستخدم:</b> {_act_name}\n"
            f"🔗 <b>اليوزر:</b> {username_val}\n"
            f"🆔 <b>الـ ID:</b> <code>{message.from_user.id}</code>\n"
            f"📦 <b>النوع:</b> {type_str}\n"
            f"🔑 <b>الكود:</b> <code>{_act_key}</code>"
        )
        if LOG_CHANNEL_ID:
            try:
                await bot.send_message(LOG_CHANNEL_ID, log_msg, parse_mode="HTML")
            except Exception as e:
                logging.error(f"Failed to send log to channel: {e}")

        await message.answer(f"✅ تم تفعيل اشتراكك بنجاح في ({type_str}) لمدة {days_str}!", reply_markup=get_main_menu(), protect_content=True)
        if resume_quiz:
            await message.answer("✅ تم التفعيل! يمكنك الآن إكمال الاختبار من حيث توقفت.")
    else:
        await message.answer("❌ كود التفعيل غير صحيح أو مستخدم سابقاً. يرجى التأكد من الكود أو التواصل مع الدعم للتحقق.")

# --- In-Bot Ticketing System: user side ---
@common_router.callback_query(F.data == "support_contact")
async def cb_support_contact(callback: types.CallbackQuery, state: FSMContext):
    """Start a support ticket: ask the user to send their message (or /cancel)."""
    try:
        await callback.answer()
    except Exception:
        pass
    await state.set_state(SupportState.waiting_for_message)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ إلغاء / خروج للقائمة الرئيسية", callback_data="main_menu")]
    ])
    await safe_edit_message_text(
        callback,
        "💬 <b>تواصل مع الإدارة</b>\n\n"
        "يرجى إرسال رسالتك الآن (نص، صورة، ملف، أو رسالة صوتية).\n"
        "يمكنك إرسال /cancel للإلغاء.",
        reply_markup=kb,
        parse_mode="HTML",
    )


@common_router.message(Command("cancel"))
async def cmd_cancel_common(message: types.Message, state: FSMContext):
    """Allow /cancel to exit SupportState.waiting_for_message gracefully."""
    cur = await state.get_state()
    if cur == SupportState.waiting_for_message:
        await state.clear()
        await message.answer("❌ تم إلغاء إرسال الرسالة.", reply_markup=get_main_menu())
        return
    # Otherwise let other routers / handlers decide — do not clear unrelated states.
    return


@common_router.message(SupportState.waiting_for_message)
async def process_support_message(message: types.Message, state: FSMContext, bot: Bot):
    """Handle a user's support ticket message: rate-limit, copy to group, metadata."""
    # /cancel as plain text (FSM stays until cleared)
    txt = (message.text or "").strip() if message.text else ""
    if txt == "/cancel":
        await state.clear()
        await message.answer("❌ تم إلغاء إرسال الرسالة.", reply_markup=get_main_menu())
        return

    if not SUPPORT_GROUP_ID:
        await message.answer("⚠️ نظام الدعم غير مُكوّن حالياً. يرجى المحاولة لاحقاً.")
        await state.clear()
        return

    # --- Rate limit: 3 messages per 300 seconds ---
    user_id = message.from_user.id
    now = time.time()
    timestamps: list = support_rate_limit.get(user_id)  # type: ignore[assignment]
    if timestamps is None:
        timestamps = []
    else:
        # prune entries older than 300s
        timestamps = [t for t in timestamps if now - t < 300]
    if len(timestamps) >= 3:
        await message.answer("⚠️ عذراً، يرجى الانتظار 5 دقائق قبل إرسال رسالة أخرى.")
        return
    timestamps.append(now)
    support_rate_limit[user_id] = timestamps

    try:
        copied = await message.copy_to(SUPPORT_GROUP_ID)
        # Metadata reply (HTML-escaped, no blocking I/O)
        try:
            safe_name = html.escape(message.from_user.full_name or "غير معروف")
            await bot.send_message(
                SUPPORT_GROUP_ID,
                f"👤 من: {safe_name}\n🆔 ID: <code>{message.from_user.id}</code>",
                parse_mode="HTML",
                reply_to_message_id=copied.message_id,
            )
        except Exception as e:
            logging.warning(f"Failed to send support metadata: {type(e).__name__}: {e}")
        await message.answer("✅ تم إرسال رسالتك للإدارة بنجاح.")
        await state.clear()
    except Exception as e:
        logging.error(f"Failed to copy support ticket to group: {type(e).__name__}: {e}")
        await message.answer("❌ تعذر إرسال رسالتك. يرجى المحاولة لاحقاً.")
        # keep FSM so user can retry, or clear? clear to avoid stuck state
        await state.clear()


@common_router.callback_query(F.data == "user_profile")
async def cb_user_profile(callback: types.CallbackQuery, state: FSMContext, bot: Bot):
    """User Profile Dashboard (ID card): subscription, study progress, referral link/stats.

    Registered before the generic handle_callbacks so the specific filter wins.
    No premium gating here — read-only stats view for every student.
    """
    try:
        await callback.answer()
    except Exception:
        pass

    user_id = callback.from_user.id
    await state.clear()
    await save_user_profile(callback.from_user)

    sub = await get_user_subscription(user_id)
    referred_count, completed_count = await get_user_dashboard_stats(user_id)

    # Total study texts across all skills/teils (in-memory catalog cache, non-blocking).
    catalog = await get_catalog()
    total_texts = sum(len(files) for skill in catalog.values() for files in skill.values())
    percentage = (completed_count / total_texts * 100) if total_texts > 0 else 0
    bar = render_progress_bar(percentage)

    if sub["is_active"]:
        status = "✅ نشط"
        exp_raw = str(sub.get("expire_date") or "").strip()
        if "غير محدود" in exp_raw or exp_raw == "∞":
            expiry = "غير محدود ♾️"
        else:
            try:
                expiry = datetime.fromisoformat(exp_raw.replace("Z", "+00:00")).strftime("%Y-%m-%d")
            except (ValueError, TypeError):
                expiry = html.escape(exp_raw) if exp_raw else "—"
    else:
        status = "❌ غير نشط"
        expiry = "منتهي ❌"

    bot_info = await bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start=ref_{user_id}"

    # Rule 8: escape untrusted profile name before HTML render (ids/counts are ints).
    name = html.escape(callback.from_user.full_name or "غير معروف")
    text = (
        f"👤 الاسم: {name}\n"
        f"🆔 الآيدي: <code>{user_id}</code>\n"
        f"💳 حالة الاشتراك: {status}\n"
        f"⏳ تاريخ الانتهاء: {expiry}\n"
        f"📊 التقدم في الدراسة: {completed_count}/{total_texts}\n"
        f"{bar} {percentage:.1f}%\n"
        f"🔗 رابط الإحالة الخاص بك:\n"
        f"<code>{ref_link}</code>\n"
        f"👥 عدد الأشخاص المدعوين: {referred_count}"
    )
    await safe_edit_message_text(callback, text, reply_markup=get_cancel_to_main_keyboard(), parse_mode="HTML")


@common_router.callback_query()
async def handle_callbacks(callback: types.CallbackQuery, state: FSMContext, bot: Bot):
    try:
        await callback.answer()
    except Exception:
        pass

    user_id = callback.from_user.id
    await save_user_profile(callback.from_user)
    data = callback.data

    # NOTE (Strike 2 — Auth Bypass fix): Admin key generation and
    # admin user lookup MUST live only in handlers/admin.py
    # behind AdminAuthMiddleware. Removed from public common_router on purpose.

    if data == "main_menu":
        await state.clear()
        await safe_edit_message_text(callback, "اختر القائمة المطلوب الوصول إليها:", reply_markup=get_main_menu())

    elif data == "menu_training":
        await state.clear()
        await safe_edit_message_text(callback, "اختر المستوى أو الخيار المطلوب من تدريب الدورات:", reply_markup=get_training_menu())

    elif data == "menu_referral":
        await state.clear()
        ref_info = await get_referral_info(user_id)
        bot_info = await bot.get_me()
        ref_link = f"https://t.me/{bot_info.username}?start=ref_{user_id}"
        
        msg_text = (
            f"🔗 <b>نظام الإحالة والتفعيل المجاني</b>\n\n"
            f"شارك الرابط الخاص بك مع أصدقائك، وعند تفعيل أي مستخدم لااشتراكه بكود، ستحصل على نقطة!\n"
            f"عند جمع <b>2 نقاط</b> تحصل على تفعيل شهري مجاني تلقائياً.\n\n"
            f"🔗 <b>رابط إحالتك:</b>\n{ref_link}\n\n"
            f"👥 <b>عدد الدعوات الناجحة (النقاط الحالية):</b> {ref_info['points']}\n"
            f"🎁 <b>رصيد التفعيل المجاني المتاح:</b> {ref_info['free_credits']} اشتراك"
        )
        has_credit = ref_info['free_credits'] > 0
        await safe_edit_message_text(callback, msg_text, reply_markup=get_referral_menu(ref_link, has_credit), parse_mode="HTML")

    elif data == "claim_free_sub":
        success, msg = await claim_free_subscription(user_id)
        if success:
            await callback.answer("🎉 تم تفعيل الاشتراك المجاني بنجاح!", show_alert=True)
            await safe_edit_message_text(callback, f"✅ {msg}", reply_markup=get_main_menu())
        else:
            await callback.answer(msg, show_alert=True)

    elif data == "menu_buy_courses":
        default_text = "🛒 <b>قسم شراء الدورات المنسقة:</b>\n\nتواصل مع الإدارة لشراء الدورات التفاعلية والمنسقة."
        custom_text = await get_custom_text("buy_courses") or default_text
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 العودة للخدمات", callback_data="menu_services_main")]])
        await safe_edit_message_text(callback, custom_text, reply_markup=kb, parse_mode="HTML")

    elif data == "start_subscribe_flow":
        await state.set_state(AuthState.waiting_for_key)
        # Dynamic plans: CMS prices always listed clearly (never static $10/$5/$20).
        dynamic_text = await get_dynamic_subscribe_text()
        custom_text = await get_custom_text("subscribe_flow")
        if custom_text and "لا يوجد نص محدد لهذه الخدمة حالياً." not in str(custom_text):
            # Admin custom intro (if set) + live plans block so prices never go stale.
            msg_text = f"{custom_text}\n\n{dynamic_text}"
        else:
            msg_text = dynamic_text
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 رجوع", callback_data="main_menu")]
        ])
        await safe_edit_message_text(callback, msg_text, reply_markup=kb, parse_mode="HTML")

    elif data in ["user_progress", "check_subscription"]:
        lesen_p = await get_skill_progress(user_id, "lesen")
        hören_p = await get_skill_progress(user_id, "hören")
        answer_stats = await get_user_answer_stats(user_id)
        sub = await get_user_subscription(user_id)
        
        type_names = {
            "b1": "المستوى B1 فقط",
            "b2": "المستوى B2 فقط",
            "all": "الباقة الشاملة (B1 + B2)",
            "intensive": "المراجعة المكثفة (5 أيام)",
            "group": "اشتراك المجموعات"
        }

        if sub["is_active"]:
            # F-04: escape DB-derived subscription fields before HTML render.
            _sub_type = html.escape(str(type_names.get(sub['type'], str(sub['type']).upper())))
            _exp = html.escape(str(sub['expire_date']))
            _days = html.escape(str(sub['days_left']))
            sub_status_text = (
                f"💎 <b>نوع الاشتراك:</b> <code>{_sub_type}</code>\n"
                f"📅 <b>ينتهي في:</b> <code>{_exp}</code>\n"
                f"⏳ <b>المتبقي:</b> <code>{_days}</code> يومًا"
            )
        else:
            sub_status_text = "❌ <b>غير نشط / منتهي الصلاحية</b>"

        lesen_bar = render_progress_bar(lesen_p['percentage'])
        hören_bar = render_progress_bar(hören_p['percentage'])
        accuracy_bar = render_progress_bar(answer_stats['accuracy'])

        next_target = await get_next_uncompleted_target(user_id)
        skill_name_ar = "قسم القراءة (Lesen)" if next_target["skill"] == "lesen" else "قسم الاستماع (Hören)"
        button_text = f"🚀 متابعة التدريب ({skill_name_ar} - {next_target['teil'].upper()})"

        msg = (
            f"🏆 <b>لوحة التقدم والإحصائيات التفاعلية</b> 🏆\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🎗️ <b>حالة الاشتراك:</b>\n"
            f"{sub_status_text}\n\n"
            f"🎯 <b>دقة الإجابات في الاختبارات:</b>\n"
            f"{accuracy_bar} <b>{answer_stats['accuracy']}%</b>\n"
            f"▫️ إجمالي الأسئلة المُجابة: <code>{answer_stats['total']}</code>\n"
            f"▫️ الإجابات الصحيحة: <code>{answer_stats['correct']}</code>\n\n"
            f"📖 <b>قسم القراءة (Lesen):</b>\n"
            f"{lesen_bar} <b>{lesen_p['percentage']}%</b>\n"
            f"▫️ النصوص المكتملة: <code>{lesen_p['completed']}/{lesen_p['total']}</code>\n\n"
            f"🎧 <b>قسم الاستماع (Hören):</b>\n"
            f"{hören_bar} <b>{hören_p['percentage']}%</b>\n"
            f"▫️ النصوص المكتملة: <code>{hören_p['completed']}/{hören_p['total']}</code>\n\n"
            f"🔥 <i>واصل التدريب اليومي لتحقيق أفضل نتيجة في الامتحان!</i>"
        )
        
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=button_text, callback_data=next_target["callback_data"])],
            [InlineKeyboardButton(text="🔙 العودة لتدريب الدورات", callback_data="menu_training")]
        ])
        await safe_edit_message_text(callback, msg, reply_markup=kb, parse_mode="HTML")

    elif data == "confirm_reset_progress":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⚠️ نعم، تمحي علامات الإنجاز والإحصائيات", callback_data="execute_reset_progress")],
            [InlineKeyboardButton(text="❌ إلغاء", callback_data="level_b1")]
        ])
        await safe_edit_message_text(callback, "هل أنت تأكد من إزالة كل علامات (✅) وإعادة نسبة التقدم والإحصائيات إلى 0%؟", reply_markup=kb)

    elif data == "execute_reset_progress":
        await reset_user_progress(user_id)
        await callback.answer("تمت إعادة تعيين تقدمك بنجاح!", show_alert=True)
        await safe_edit_message_text(callback, "المستوى B1 - اختر المهارة:", reply_markup=get_b1_skills())

    elif data == "menu_services_main":
        await state.clear()
        await safe_edit_message_text(callback, "اختر قسم الخدمات المطلوب:", reply_markup=get_services_menu())

    elif data == "service_engineering":
        default_text = "🏗️ <b>الدورات الهندسية المتاحة:</b>\n\n1. دورة حساب الكميات الألومنيوم\n2. أوتوماتيك التصميم والرسم الهندسي"
        custom_text = await get_custom_text("service_engineering") or default_text
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 العودة للخدمات", callback_data="menu_services_main")]])
        await safe_edit_message_text(callback, custom_text, reply_markup=kb, parse_mode="HTML")

    elif data == "service_visa":
        default_text = "✈️ <b>خدمات الفيز والقبولات الجامعية:</b>\n\n▪️ القبول الجامعي والمباشر في ألمانيا."
        custom_text = await get_custom_text("service_visa") or default_text
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 العودة للخدمات", callback_data="menu_services_main")]
        ])
        await safe_edit_message_text(callback, custom_text, reply_markup=kb, parse_mode="HTML")

    elif data == "service_svu":
        default_text = "🎓 <b>خدمات الجامعة الافتراضية (SVU):</b>\n\n▪️ التسجيل وتوجيه الطلاب.\n▪️ المساعدة في حل الوظائف ومتابعة المواد."
        custom_text = await get_custom_text("service_svu") or default_text
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 العودة للخدمات", callback_data="menu_services_main")]])
        await safe_edit_message_text(callback, custom_text, reply_markup=kb, parse_mode="HTML")

    elif data in ["level_b1", "level_b2"]:
        if data == "level_b1":
            await state.clear()
            await safe_edit_message_text(callback, "المستوى B1 - اختر المهارة:", reply_markup=get_b1_skills())
        else:
            await callback.answer("🚧 قسم B2 قيد الإعداد حالياً.", show_alert=True)

    elif data.startswith("b1_skill_"):
        skill_parts = data.split("_")
        if len(skill_parts) < 3 or not skill_parts[2].strip():
            await callback.answer("⚠️ خطأ في البيانات.", show_alert=True)
            return
        skill = os.path.basename(skill_parts[2])
        # Strike 2 — Broken Access Control fix: gate premium PDFs behind subscription.
        # Preserve FSM on paywall (return before state.clear()).
        if skill in ("schreiben", "sprechen"):
            if not await can_access_level(user_id, 'b1'):
                await callback.answer("⚠️ هذا القسم مخصص للمشتركين فقط. يرجى تفعيل اشتراكك للوصول إلى الملفات.", show_alert=True)
                return
        await state.clear()
        if skill == "schreiben":
            pdf_path = os.path.join(BASE_DIR, "schreiben_guide.pdf")
            contact_username = "your_contact"
            
            try:
                await callback.message.delete()
            except Exception:
                pass

            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔙 🔙 العودة لقائمة المهارات", callback_data="level_b1")]
            ])
            
            default_caption = f"✍️ <b>قسم الكتابة (Schreiben):</b>\n\n📄 <b>ملف دليل الكتابة</b>\n\nللاستفسار يرجى مراسلة: @{contact_username}"
            caption_text = await get_custom_text("schreiben") or default_caption

            # F-01: os.path.exists is blocking — off-loop.
            if await asyncio.to_thread(os.path.exists, pdf_path):
                await bot.send_chat_action(chat_id=callback.message.chat.id, action="upload_document")
                document = FSInputFile(pdf_path)
                await bot.send_document(
                    chat_id=callback.message.chat.id,
                    document=document,
                    caption=caption_text,
                    reply_markup=kb,
                    parse_mode="HTML"
                )
            else:
                await bot.send_message(
                    chat_id=callback.message.chat.id,
                    text=f"⚠️ نعتذر، ملف دليل الكتابة غير متوفر حالياً.\n\nللاستفسار يرجى مراسلة: @{contact_username}",
                    reply_markup=kb,
                    parse_mode="HTML"
                )
        elif skill == "sprechen":
            pdf_path = os.path.join(BASE_DIR, "sprechen_guide.pdf")
            group_link = "https://t.me/+lpMfzUHpOh8wZWE0"
            
            try:
                await callback.message.delete()
            except Exception:
                pass

            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="👥 انضمام لمجموعة المحادثة", url="https://t.me/+lpMfzUHpOh8wZWE0")],
                [InlineKeyboardButton(text="🔙 🔙 العودة لقائمة المهارات", callback_data="level_b1")]
            ])
            
            default_caption = (
                "🗣️ <b>قسم المحادثة (Sprechen)</b>\n\n"
                "📄 تجدون في الملف المرفق الدليل الشامل والنصائح الخاصة بقسم المحادثة.\n\n"
                "💬 <b>مجموعة التفاعل والتدريب:</b>\n"
                "هذه المجموعة العامة مخصصة للتواصل وإيجاد زميل للتدرب على المحادثة. "
                "المجموعة ما زالت في بدايتها وتكبر بتواجدكم ومشاركتكم!"
            )
            caption_text = await get_custom_text("sprechen") or default_caption

            # F-01: os.path.exists is blocking — off-loop.
            if await asyncio.to_thread(os.path.exists, pdf_path):
                await bot.send_chat_action(chat_id=callback.message.chat.id, action="upload_document")
                document = FSInputFile(pdf_path)
                await bot.send_document(
                    chat_id=callback.message.chat.id,
                    document=document,
                    caption=caption_text,
                    reply_markup=kb,
                    parse_mode="HTML"
                )
            else:
                await bot.send_message(
                    chat_id=callback.message.chat.id,
                    text=f"⚠️ نعتذر، ملف دليل المحادثة غير متوفر حالياً.\n\n💬 يمكنك الانضمام لمجموعة المحادثة للتدريب عبر الرابط التالي:\n{group_link}",
                    reply_markup=kb,
                    parse_mode="HTML"
                )
        else:
            parts_count = 4 if skill.lower() == "hören" else 5
            kb = []
            for i in range(1, parts_count + 1):
                kb.append([InlineKeyboardButton(text=f"Teil {i}", callback_data=f"b1_parts_{skill}_teil{i}")])
            kb.append([InlineKeyboardButton(text="🔙 العودة", callback_data="level_b1")])
            await safe_edit_message_text(callback, f"قسم {skill.upper()} - اختر الجزء:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

    elif data.startswith("b1_parts_"):
        parts = data.split("_")
        if len(parts) < 4 or not parts[2].strip() or not parts[3].strip():
            await callback.answer("⚠️ خطأ في البيانات.", show_alert=True)
            return
        await state.clear()
        skill = os.path.basename(parts[2])
        teil = os.path.basename(parts[3])

        # Dynamic Tips per Teil (CMS): admin-editable via custom_texts tips_{skill}_{teil}.
        tip_text = await get_teil_tip(skill, teil)

        # F5/F6/F7: file lists come from the in-memory catalog cache (non-blocking).
        # No direct os.listdir / os.walk here — must use await get_catalog().
        catalog = await get_catalog()
        files = catalog.get(skill.lower(), {}).get(teil.lower(), [])

        total_files = len(files)
        step = 15 if (skill == "hören" and teil == "teil1") else 10

        # إذا كان إجمالي عدد الملفات يتجاوز حجم الشريحة، يتم عرض تقسيم المجموعات ديناميكياً
        if total_files > step:
            multiplier = 2 if (skill == "hören" and teil == "teil1") else 1
            files_chunks = [files[i:i + step] for i in range(0, total_files, step)]
            kb = []
            for g_idx, chunk in enumerate(files_chunks):
                g = g_idx + 1
                start_num = (g_idx * step * multiplier) + 1
                files_in_this_group = len(chunk)
                end_num = start_num + (files_in_this_group * multiplier) - 1
                kb.append([InlineKeyboardButton(
                    text=f"المجموعة {g} ({start_num} - {end_num})",
                    callback_data=f"t_group_{skill}_{teil}_{g}"
                )])
            kb.append([InlineKeyboardButton(text="🔙 العودة", callback_data=f"b1_skill_{skill}")])
            msg_content = f"{tip_text}اختر مجموعة النصوص لـ {skill.upper()} {teil.upper()}:"
            await safe_edit_message_text(callback, msg_content, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")
            return

        kb = []
        if total_files > 0:
            # F-07: fetch titles + completion concurrently instead of N+1 sequential awaits.
            async def _fetch_entry(idx_file):
                idx, file = idx_file
                file_safe = os.path.basename(file)
                try:
                    item_data = await get_content(skill, teil, file_safe)
                    if isinstance(item_data, list):
                        title = file_safe.replace('.json', '').replace('_', ' ').title()
                    else:
                        title = item_data.get('title', file_safe.replace('.json', '').replace('_', ' ').title())
                except Exception:
                    title = file_safe.replace('.json', '').replace('_', ' ').title()

                text_id = f"{skill}_{teil}_{file_safe}"
                try:
                    completed = await is_text_completed(user_id, text_id)
                except Exception:
                    logging.warning(f"is_text_completed failed for user {user_id} text {text_id}")
                    completed = False
                status_icon = "✅ " if completed else "📄 "

                tag = " (مجاني)" if (skill == "lesen" and teil == "teil1" and idx == 0) else ""
                return (idx, file_safe, title, status_icon, tag)

            entries = await asyncio.gather(*(_fetch_entry(pair) for pair in enumerate(files)))
            for idx, file_safe, title, status_icon, tag in sorted(entries, key=lambda e: e[0]):
                # F-03: hash the filename so callback_data never exceeds 64 bytes.
                kb.append([InlineKeyboardButton(text=f"{status_icon}{title}{tag}", callback_data=f"read_{skill}_{teil}_{map_filename(file_safe)}")])
        
        kb.append([InlineKeyboardButton(text="🔙 العودة", callback_data="level_b1")])
        
        msg_content = f"{tip_text}قائمة نصوص {teil.upper()}:"
        await safe_edit_message_text(callback, msg_content, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")

    elif data.startswith("t_group_"):
        parts = data.split("_")
        if len(parts) < 5 or not parts[2].strip() or not parts[3].strip() or not parts[4].strip():
            await callback.answer("⚠️ خطأ في البيانات.", show_alert=True)
            return
        try:
            group_num = int(parts[4])
        except ValueError:
            await callback.answer("⚠️ خطأ في البيانات.", show_alert=True)
            return
        await state.clear()
        skill = os.path.basename(parts[2])
        teil = os.path.basename(parts[3])
        
        step = 15 if (skill == "hören" and teil == "teil1") else 10
        start_idx = (group_num - 1) * step
        end_idx = start_idx + step
        
        # F5/F6/F7: file lists come from the in-memory catalog cache (non-blocking).
        # No direct os.listdir / os.walk here — must use await get_catalog().
        catalog = await get_catalog()
        files = catalog.get(skill.lower(), {}).get(teil.lower(), [])
        group_files = files[start_idx:end_idx]
        kb = []
        if group_files:
            # F-07: fetch titles + completion concurrently instead of N+1 sequential awaits.
            async def _fetch_group_entry(idx_file):
                idx, file = idx_file
                file_safe = os.path.basename(file)

                try:
                    item_data = await get_content(skill, teil, file_safe)
                    if isinstance(item_data, list):
                        title = file_safe.replace('.json', '').replace('_', ' ').title()
                    else:
                        title = item_data.get('title', file_safe.replace('.json', '').replace('_', ' ').title())
                except Exception:
                    title = file_safe.replace('.json', '').replace('_', ' ').title()

                text_id = f"{skill}_{teil}_{file_safe}"
                try:
                    completed = await is_text_completed(user_id, text_id)
                except Exception:
                    logging.warning(f"is_text_completed failed for user {user_id} text {text_id}")
                    completed = False
                status_icon = "✅ " if completed else "📄 "
                return (idx, file_safe, title, status_icon)

            group_entries = await asyncio.gather(*(_fetch_group_entry(pair) for pair in enumerate(group_files)))
            for idx, file_safe, title, status_icon in sorted(group_entries, key=lambda e: e[0]):
                # F-03: hash the filename so callback_data never exceeds 64 bytes.
                kb.append([InlineKeyboardButton(text=f"{status_icon}{title}", callback_data=f"read_{skill}_{teil}_{map_filename(file_safe)}")])
        
        kb.append([InlineKeyboardButton(text="🔙 العودة للمجموعات", callback_data=f"b1_parts_{skill}_{teil}")])
        group_tip = await get_teil_tip(skill, teil)
        await safe_edit_message_text(callback, f"{group_tip}المجموعة {group_num} - نصوص {teil.upper()}:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")