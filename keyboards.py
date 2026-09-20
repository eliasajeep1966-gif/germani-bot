from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

def get_main_menu():
    buttons = [
        [InlineKeyboardButton(text="✨ خدمات البوت الأخرى", callback_data="menu_services_main")],
        [InlineKeyboardButton(text="📚 تدريب دورات", callback_data="menu_training")],
        [InlineKeyboardButton(text="⭐ الاشتراك بالبوت", callback_data="start_subscribe_flow")],
        [InlineKeyboardButton(text="🔗 رابط إحالتي / تفعيل مجاني", callback_data="menu_referral")],
        [InlineKeyboardButton(text="💬 تواصل مع الإدارة", callback_data="support_contact")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# Unified paywall message + keyboard for the in-bot ticketing system.
# Replaces all direct tg://user admin URL buttons (no client-side user IDs).
PAYWALL_TEXT = (
    "⚠️ هذا المحتوى مخصص للمشتركين فقط.\n"
    "للحصول على كود التفعيل أو للاستفسار، اضغط على زر التواصل بالأسفل."
)

def get_paywall_keyboard():
    """Paywall keyboard: in-bot support contact + cancel (no external URLs)."""
    buttons = [
        [InlineKeyboardButton(text="💬 تواصل مع الإدارة", callback_data="support_contact")],
        [InlineKeyboardButton(text="❌ إلغاء / العودة للقائمة الرئيسية", callback_data="main_menu")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_cancel_to_main_keyboard():
    """
    زر إلغاء وخروج صريح موحد لإلغاء أي عملية وإعادة المستخدم بنقرة واحدة للقائمة الرئيسية.
    """
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ إلغاء / خروج للقائمة الرئيسية", callback_data="main_menu")]
    ])

def get_referral_menu(referral_link: str = None, has_free_credit: bool = False):
    buttons = []
    
    if referral_link:
        buttons.append([InlineKeyboardButton(text="🔗 مشاركة رابط الإحالة", url=referral_link)])
        
    if has_free_credit:
        buttons.append([InlineKeyboardButton(text="⚡ استهلاك تفعيل مجاني", callback_data="claim_free_sub")])
        
    buttons.append([InlineKeyboardButton(text="🔙 القائمة الرئيسية", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_training_menu():
    buttons = [
        [InlineKeyboardButton(text="📘 المستوى B1", callback_data="level_b1")],
        [InlineKeyboardButton(text="📙 المستوى B2", callback_data="level_b2")],
        [InlineKeyboardButton(text="📊 نسب التقدم وحالة الاشتراك", callback_data="user_progress")],
        [InlineKeyboardButton(text="🔙 القائمة الرئيسية", callback_data="main_menu")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_admin_panel():
    buttons = [
        [InlineKeyboardButton(text="🔑 إنشاء كود B1", callback_data="admin_gen_b1")],
        [InlineKeyboardButton(text="🌟 إنشاء كود شامل (B1+B2)", callback_data="admin_gen_all")],
        [InlineKeyboardButton(text="⚡ كود مراجعة (5 أيام)", callback_data="admin_gen_intensive"),
          InlineKeyboardButton(text="👥 كود مجموعات (4 مستخدمين)", callback_data="admin_gen_group")],
        [InlineKeyboardButton(text="🔑 إدارة الأكواد الفعالة", callback_data="admin_manage_keys")],
        [InlineKeyboardButton(text="✏️ تغيير النصوص", callback_data="admin_edit_texts")],
        [InlineKeyboardButton(text="👥 إدارة المشرفين", callback_data="admin_manage_supervisors")],
        [InlineKeyboardButton(text="💰 التسوية المالية للمشرفين", callback_data="admin_financial_settlement")],
        [InlineKeyboardButton(text="👥 قائمة المستخدمين", callback_data="admin_users_list")],
        [InlineKeyboardButton(text="🔍 بحث عن مستخدم", callback_data="admin_search_user")],
        [InlineKeyboardButton(text="🔙 القائمة الرئيسية", callback_data="main_menu")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _build_revoke_callback(key_code: str, rowid=None) -> str:
    """Build a <=64-byte revoke callback for a key.

    Rule 16/20: callback_data MUST NEVER exceed 64 bytes.
    Normal keys (KEY-XXX- + urlsafe(16) ≈ 30 chars) fit as rev_key_<key_code>.
    If key_code is too long, fall back to the short DB rowid: rev_keyid_<rowid>.
    """
    key_code = str(key_code or "")
    candidate = f"rev_key_{key_code}"
    if len(candidate.encode("utf-8")) <= 64:
        return candidate
    if rowid is not None:
        return f"rev_keyid_{rowid}"
    # Last resort (no rowid available): short hash — caller must resolve
    # via DB lookup is impossible, so keep prefix + truncated hash marker.
    # Handlers treat unknown hashes as expired with a clear alert.
    import hashlib
    short = hashlib.md5(key_code.encode()).hexdigest()[:12]
    return f"rev_keyh_{short}"


def get_keys_pagination_keyboard(keys: list, page: int, total_pages: int):
    """Paginated active-keys keyboard (Rule 20: max 10 items per page).

    Each key row: [ ❌ إبطال (rev_key_<key_code> | rev_keyid_<rowid> fallback) | <key_code> (<sub_type>) (noop) ].
    Bottom row: ⬅️ السابق / التالي ➡️ (admin_keys_page_<page>) + back button.
    Accepts rows as (rowid, key_code, sub_type[, ...]) tuples, (key_code, sub_type)
    tuples, or dicts with key_code/sub_type/rowid keys.
    """
    try:
        page = int(page)
    except (TypeError, ValueError):
        page = 1
    try:
        total_pages = int(total_pages)
    except (TypeError, ValueError):
        total_pages = 1
    page = max(1, page)
    total_pages = max(1, total_pages)

    buttons = []
    # Defensive cap: never render more than 10 key rows (Telegram limits + mobile UX).
    for item in list(keys or [])[:10]:
        rowid = None
        key_code = ""
        sub_type = "b1"
        if isinstance(item, dict):
            rowid = item.get("rowid")
            key_code = str(item.get("key_code", ""))
            sub_type = str(item.get("sub_type") or "b1")
        elif isinstance(item, (list, tuple)):
            if len(item) >= 3:
                rowid, key_code, sub_type = item[0], str(item[1]), str(item[2] or "b1")
            elif len(item) == 2:
                key_code, sub_type = str(item[0]), str(item[1] or "b1")
            elif len(item) == 1:
                key_code = str(item[0])
        else:
            key_code = str(item)

        revoke_cb = _build_revoke_callback(key_code, rowid=rowid)
        # Button text: keep readable on mobile; truncate very long codes.
        display_code = key_code if len(key_code) <= 32 else key_code[:29] + "..."
        info_text = f"{display_code} ({(sub_type or 'b1').upper()})"
        buttons.append([
            InlineKeyboardButton(text="❌ إبطال", callback_data=revoke_cb),
            InlineKeyboardButton(text=info_text, callback_data="noop"),
        ])

    nav_row = []
    if page > 1:
        nav_row.append(InlineKeyboardButton(text="⬅️ السابق", callback_data=f"admin_keys_page_{page - 1}"))
    if page < total_pages:
        nav_row.append(InlineKeyboardButton(text="التالي ➡️", callback_data=f"admin_keys_page_{page + 1}"))
    if nav_row:
        buttons.append(nav_row)
    buttons.append([InlineKeyboardButton(text="🔙 لوحة الأدمن", callback_data="admin_panel_back")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_edit_texts_menu():
    """
    تعديل المفاتيح التفاعلية لتطابق مفاتيح common.py تماماً.
    """
    buttons = [
        [InlineKeyboardButton(text="🎓 الفيز والقبولات الجامعية", callback_data="admin_edit_text_service_visa")],
        [InlineKeyboardButton(text="🏫 خدمات الجامعة الافتراضية", callback_data="admin_edit_text_service_svu")],
        [InlineKeyboardButton(text="🏗️ دورات الهندسة", callback_data="admin_edit_text_service_engineering")],
        [InlineKeyboardButton(text="🆓 الخدمات المجانية", callback_data="admin_edit_text_free_services")],
        [InlineKeyboardButton(text="🛒 شراء الدورات", callback_data="admin_edit_text_buy_courses")],
        [InlineKeyboardButton(text="⭐ الاشتراك بالبوت", callback_data="admin_edit_text_subscribe_flow")],
        [InlineKeyboardButton(text="✍️ قسم الكتابة Schreiben", callback_data="admin_edit_text_schreiben")],
        [InlineKeyboardButton(text="🗣️ قسم المحادثة Sprechen", callback_data="admin_edit_text_sprechen")],
        [InlineKeyboardButton(text="💡 نصائح الأقسام (Tips per Teil)", callback_data="admin_edit_tips_menu")],
        [InlineKeyboardButton(text="🔙 لوحة الأدمن", callback_data="admin_panel_back")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_edit_tips_menu():
    """Admin sub-menu for editing per-Teil tips (CMS keys tips_{skill}_{teil}).

    Each button reuses the generic admin_edit_text_ flow so
    process_admin_save_text handles persistence without changes.
    All callback_data kept <= 64 bytes (Rule 16).
    """
    buttons = [
        [InlineKeyboardButton(text="📖 Lesen Teil 1", callback_data="admin_edit_text_tips_lesen_teil1")],
        [InlineKeyboardButton(text="📖 Lesen Teil 2", callback_data="admin_edit_text_tips_lesen_teil2")],
        [InlineKeyboardButton(text="📖 Lesen Teil 3", callback_data="admin_edit_text_tips_lesen_teil3")],
        [InlineKeyboardButton(text="📖 Lesen Teil 4", callback_data="admin_edit_text_tips_lesen_teil4")],
        [InlineKeyboardButton(text="📖 Lesen Teil 5", callback_data="admin_edit_text_tips_lesen_teil5")],
        [InlineKeyboardButton(text="🎧 Hören Teil 1", callback_data="admin_edit_text_tips_hören_teil1")],
        [InlineKeyboardButton(text="🎧 Hören Teil 2", callback_data="admin_edit_text_tips_hören_teil2")],
        [InlineKeyboardButton(text="🎧 Hören Teil 3", callback_data="admin_edit_text_tips_hören_teil3")],
        [InlineKeyboardButton(text="🎧 Hören Teil 4", callback_data="admin_edit_text_tips_hören_teil4")],
        [InlineKeyboardButton(text="🔙 النصوص", callback_data="admin_edit_texts")],
        [InlineKeyboardButton(text="🔙 لوحة الأدمن", callback_data="admin_panel_back")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_supervisor_panel():
    buttons = [
        [InlineKeyboardButton(text="🔑 إنشاء كود شهري (B1)", callback_data="sup_gen_monthly_b1")],
        [InlineKeyboardButton(text="⚡ إنشاء كود مراجعة (5 أيام)", callback_data="sup_gen_intensive"),
          InlineKeyboardButton(text="👥 إنشاء كود مجموعات", callback_data="sup_gen_group")],
        [InlineKeyboardButton(text="📊 إحصائيات الأكواد والعملاء", callback_data="sup_stats")],
        [InlineKeyboardButton(text="💳 الحساب والتحصيل المالي", callback_data="sup_finance")],
        [InlineKeyboardButton(text="🔙 القائمة الرئيسية", callback_data="main_menu")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_supervisors_management_menu(supervisors_list):
    buttons = []
    for item in supervisors_list:
        if len(item) == 5:
            sup_id, sup_name, full_name, username, is_active = item
        else:
            sup_id, full_name, username, is_active = item
            sup_name = None

        status = "🟢 نشط" if is_active else "🔴 معطل"
        name_str = sup_name or full_name or f"ID: {sup_id}"
        
        buttons.append([
            InlineKeyboardButton(text=f"{name_str} ({status})", callback_data=f"sup_toggle_{sup_id}"),
            InlineKeyboardButton(text="❌ حذف", callback_data=f"sup_delete_{sup_id}")
        ])
    buttons.append([InlineKeyboardButton(text="➕ إضافة مشرف جديد", callback_data="sup_add_new")])
    buttons.append([InlineKeyboardButton(text="❌ إلغاء والعودة للوحة الأدمن", callback_data="admin_panel_back")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_financial_settlement_menu(settlements):
    buttons = []
    for item in settlements:
        sup_id, display_name, unsettled_count, total_amount = item
        name_str = display_name or f"ID: {sup_id}"
        if unsettled_count > 0:
            btn_text = f"✅ تسوية {name_str} (${total_amount})"
            buttons.append([InlineKeyboardButton(text=btn_text, callback_data=f"settle_sup_{sup_id}")])
        else:
            buttons.append([InlineKeyboardButton(text=f"⚪ {name_str}: لا يوجد مستحقات", callback_data="noop")])
    buttons.append([InlineKeyboardButton(text="🔙 لوحة الأدمن", callback_data="admin_panel_back")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_services_menu():
    buttons = [
        [InlineKeyboardButton(text="🏗️ الدورات الهندسية", callback_data="service_engineering")],
        [InlineKeyboardButton(text="✈️ الفيز والقبولات الجامعية", callback_data="service_visa")],
        [InlineKeyboardButton(text="🎓 خدمات الجامعة الافتراضية SVU", callback_data="service_svu")],
        [InlineKeyboardButton(text="🛒 ملفات الدورات", callback_data="menu_buy_courses")],
        [InlineKeyboardButton(text="🔙 القائمة الرئيسية", callback_data="main_menu")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_b1_skills():
    buttons = [
        [InlineKeyboardButton(text="📖 Lesen", callback_data="b1_skill_lesen"),
         InlineKeyboardButton(text="🎧 hören", callback_data="b1_skill_hören")],
        [InlineKeyboardButton(text="✍️ Schreiben", callback_data="b1_skill_schreiben"),
         InlineKeyboardButton(text="🗣️ Sprechen", callback_data="b1_skill_sprechen")],
        [InlineKeyboardButton(text="🔄 إعادة تعيين التقدم", callback_data="confirm_reset_progress")],
        [InlineKeyboardButton(text="🔙 العودة لتدريب الدورات", callback_data="menu_training")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_back_to_b1_skills():
    buttons = [
        [InlineKeyboardButton(text="🔙 العودة لمهارات B1", callback_data="level_b1")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_quiz_navigation_keyboard(skill: str, teil: str, current_index: int, total_questions: int, options: list = None, teil4: bool = False):
    """
    مولد أزرار التصفح والإجابة الموحد للاستبيانات لمنع التعليق.
    يضمن دائماً وجود أزرار التحكم (السؤال السابق، التجاوز، إنهاء الاختبار، القائمة الرئيسية).
    """
    buttons = []
    
    # 1. إضافة أزرار الخيارات والخيارات المتعددة / صح وخطأ
    if teil4:
        buttons.append([
            InlineKeyboardButton(text="✅ Ja (نعم)", callback_data="ans_J"),
            InlineKeyboardButton(text="❌ Nein (لا)", callback_data="ans_N")
        ])
    elif options:
        row_btns = []
        if isinstance(options, list):
            for idx_opt, opt_item in enumerate(options):
                if isinstance(opt_item, dict):
                    opt_key = str(opt_item.get('key', chr(97 + idx_opt))).upper()
                    row_btns.append(InlineKeyboardButton(text=opt_key, callback_data=f"ans_{opt_key}"))
                else:
                    opt_letter = chr(97 + idx_opt).upper()
                    row_btns.append(InlineKeyboardButton(text=opt_letter, callback_data=f"ans_{opt_letter}"))
            buttons.append(row_btns)
        elif isinstance(options, dict):
            for opt_key in options.keys():
                opt_str = str(opt_key).upper()
                row_btns.append(InlineKeyboardButton(text=opt_str, callback_data=f"ans_{opt_str}"))
            buttons.append(row_btns)
    else:
        buttons.append([
            InlineKeyboardButton(text="✅ Richtig (صح)", callback_data="ans_a"),
            InlineKeyboardButton(text="❌ Falsch (خطأ)", callback_data="ans_b")
        ])

    # 2. أزرار التحكم بالتصفح (السؤال السابق + السؤال التالي/تجاوز)
    nav_row = []
    if current_index > 0:
        nav_row.append(InlineKeyboardButton(text="⬅️ السؤال السابق", callback_data="prev_question"))

    if current_index + 1 < total_questions:
        nav_row.append(InlineKeyboardButton(text="⏭️ السؤال التالي (تجاوز)", callback_data="skip_question"))
    
    if nav_row:
        buttons.append(nav_row)

    # 3. أزرار الخروج والإنهاء (إنهاء الاختبار + القائمة الرئيسية)
    buttons.append([
        InlineKeyboardButton(text="🔙 إنهاء الاختبار", callback_data=f"b1_parts_{skill}_{teil}"),
        InlineKeyboardButton(text="🏠 القائمة الرئيسية", callback_data="main_menu")
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)