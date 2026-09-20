import logging
import os
import aiosqlite
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from aiogram.fsm.state import State, StatesGroup

# استيراد الثوابت والدوال المساعدة
from config import ADMIN_IDS, DB_NAME, ROLE_MAIN_ADMIN, ROLE_SUPERVISOR
from utils import find_directory_case_insensitive

# --- فئات الـ FSM (States) ---
class AuthState(StatesGroup):
    waiting_for_key = State()

class QuizState(StatesGroup):
    answering = State()

class Teil3State(StatesGroup):
    matching = State()

class BroadcastState(StatesGroup):
    waiting_for_message = State()

class SupportState(StatesGroup):
    waiting_for_message = State()

# النصوص الافتراضية للخدمات/الأقسام المخصصة (تم استبعاد welcome_message لتجريد صلاحيات الإدارة منها)
DEFAULT_CUSTOM_TEXTS = {
    "service_visa": "خدمات الفيزا والقبولات: نقدم استشارات ودعم أكاديمي شامل للتخصصات العلمية والقبولات الجامعية.",
    "service_svu": "خدمات الجامعة الافتراضية: مساعدة وتوجيه في التسجيل وإدارة المواد في الجامعة الافتراضية.",
    "service_engineering": "الدورات الهندسية: دورات متخصصة في التحضير الهندسي والمفاهيم الأساسية للجامعات.",
    "free_services": "الخدمات المجانية: قائمة الخدمات المجانية الشاملة المتاحة للطلاب.",
    "buy_courses": "شراء الدورات: يمكنك الحصول على وصول كامل لكافة المواد والدورات التعليمية المتاحة.",
    "subscribe_flow": "الاشتراك بالبوت: تفعيل كود الاشتراك يمنحك صلاحية الوصول الكاملة لااختبارات وتمارين البوت.",
    "schreiben": "قسم الكتابة: تمارين وااختبارات تفاعلية لتطوير مهارات الكتابة والصياغة.",
    "sprechen": "قسم المحادثة: جلسات وتطبيقات عملية لتحسين مهارات المحادثة والتواصل.",
    # Dynamic Tips per Teil (CMS) — defaults mirror the former hardcoded tips in common.py.
    # Admin-editable via admin_edit_text_tips_{skill}_{teil} (custom_texts table).
    "tips_lesen_teil1": "💡 <b>ملاحظة ونصيحة للحل (Teil 1):</b>\nهذا الجزء يمكن حفظ نصوصه كقصة، وقد تم وضع ملخصات بسيطة جداً يمكن قراءتها قبل البدء بالحل لتذكر النص.",
    "tips_lesen_teil2": "💡 <b>ملاحظة ونصيحة للحل (Teil 2):</b>\nهذا الجزء في الامتحان يأتي بنظام الاختيار من متعدد.",
    "tips_lesen_teil3": "💡 <b>ملاحظة ونصيحة للحل (Teil 3):</b>\nقم بصل كل موقف بالسؤال أو الإجابة المناسبة له بالضغط عليهما بالتوالي.",
    "tips_lesen_teil4": "💡 <b>ملاحظة ونصيحة للحل (Teil 4):</b>\nاختر خيار الجواب المناسب (Ja أو Nein) لكل نص بناءً على موقف وتوجه الكاتب حول السؤال المطروح.",
    "tips_lesen_teil5": "💡 <b>ملاحظة ونصيحة للحل (Teil 5):</b>\nاقرأ القواعد أو اللوائح جيداً ثم اختر الإجابة الصحيحة من بين الخيارات الثلاثة لكل سؤال.",
    "tips_hören_teil1": "💡 <b>ملاحظة ونصيحة للحل (Hören Teil 1):</b>\nهنا يمكنك كتابة الملاحظات والنصائح الخاصة بالجزء الأول للاستماع ويمكن تعديلها لاحقاً حسب الحاجة.",
    "tips_hören_teil2": "💡 <b>ملاحظة ونصيحة للحل (Hören Teil 2):</b>\nهذا الجزء يحتوي على أسئلة اختيار من متعدد مع ثلاثة خيارات لكل سؤال، ويمكن إضافة الملاحظات هنا لاحقاً.",
    "tips_hören_teil3": "💡 <b>ملاحظة ونصيحة للحل (Hören Teil 3):</b>\nاستمع إلى المحادثات القصيرة ثم أجب على الأسئلة بتحديد صح (Richtig) أو خطأ (Falsch).",
    "tips_hören_teil4": "💡 <b>ملاحظة ونصيحة للحل (Hören Teil 4):</b>\nاستمع إلى الحوار أو المقابلة بعناية، ثم أجب على الأسئلة باختيار الإجابة الصحيحة من بين الخيارات الثلاثة (a, b, c).",
}

# Valid Teil-tips keys (CMS allowlist — prevents arbitrary custom_texts key injection).
VALID_TIPS_KEYS = frozenset([
    "tips_lesen_teil1", "tips_lesen_teil2", "tips_lesen_teil3",
    "tips_lesen_teil4", "tips_lesen_teil5",
    "tips_hören_teil1", "tips_hören_teil2",
    "tips_hören_teil3", "tips_hören_teil4",
])

# Hardcoded fallback tips (used when DB has no row yet — e.g. existing DB
# before migration — keeps UX identical until init_db seeds defaults).
FALLBACK_TIPS = {
    "tips_lesen_teil1": "💡 <b>ملاحظة ونصيحة للحل (Teil 1):</b>\nهذا الجزء يمكن حفظ نصوصه كقصة، وقد تم وضع ملخصات بسيطة جداً يمكن قراءتها قبل البدء بالحل لتذكر النص.\n\n",
    "tips_lesen_teil2": "💡 <b>ملاحظة ونصيحة للحل (Teil 2):</b>\nهذا الجزء في الامتحان يأتي بنظام الاختيار من متعدد.\n\n",
    "tips_lesen_teil3": "💡 <b>ملاحظة ونصيحة للحل (Teil 3):</b>\nقم بصل كل موقف بالسؤال أو الإجابة المناسبة له بالضغط عليهما بالتوالي.\n\n",
    "tips_lesen_teil4": "💡 <b>ملاحظة ونصيحة للحل (Teil 4):</b>\nاختر خيار الجواب المناسب (Ja أو Nein) لكل نص بناءً على موقف وتوجه الكاتب حول السؤال المطروح.\n\n",
    "tips_lesen_teil5": "💡 <b>ملاحظة ونصيحة للحل (Teil 5):</b>\nاقرأ القواعد أو اللوائح جيداً ثم اختر الإجابة الصحيحة من بين الخيارات الثلاثة لكل سؤال.\n\n",
    "tips_hören_teil1": "💡 <b>ملاحظة ونصيحة للحل (Hören Teil 1):</b>\nهنا يمكنك كتابة الملاحظات والنصائح الخاصة بالجزء الأول للاستماع ويمكن تعديلها لاحقاً حسب الحاجة.\n\n",
    "tips_hören_teil2": "💡 <b>ملاحظة ونصيحة للحل (Hören Teil 2):</b>\nهذا الجزء يحتوي على أسئلة اختيار من متعدد مع ثلاثة خيارات لكل سؤال، ويمكن إضافة الملاحظات هنا لاحقاً.\n\n",
    "tips_hören_teil3": "💡 <b>ملاحظة ونصيحة للحل (Hören Teil 3):</b>\nاستمع إلى المحادثات القصيرة ثم أجب على الأسئلة بتحديد صح (Richtig) أو خطأ (Falsch).\n\n",
    "tips_hören_teil4": "💡 <b>ملاحظة ونصيحة للحل (Hören Teil 4):</b>\nاستمع إلى الحوار أو المقابلة بعناية، ثم أجب على الأسئلة باختيار الإجابة الصحيحة من بين الخيارات الثلاثة (a, b, c).\n\n",
}

# قائمة الحقول الآمنة المسجلة لغرض التوسيع الآمن للمخطط (Schema Migration White-list)
ALLOWED_MIGRATION_COLUMNS = {
    "users": {
        "sub_type TEXT DEFAULT 'b1'": "ALTER TABLE users ADD COLUMN sub_type TEXT DEFAULT 'b1'",
        "activated_via TEXT": "ALTER TABLE users ADD COLUMN activated_via TEXT",
        "is_group INTEGER DEFAULT 0": "ALTER TABLE users ADD COLUMN is_group INTEGER DEFAULT 0",
        "is_intensive INTEGER DEFAULT 0": "ALTER TABLE users ADD COLUMN is_intensive INTEGER DEFAULT 0",
        "total_answers INTEGER DEFAULT 0": "ALTER TABLE users ADD COLUMN total_answers INTEGER DEFAULT 0",
        "correct_answers INTEGER DEFAULT 0": "ALTER TABLE users ADD COLUMN correct_answers INTEGER DEFAULT 0"
    },
    "keys": {
        "sub_type TEXT DEFAULT 'b1'": "ALTER TABLE keys ADD COLUMN sub_type TEXT DEFAULT 'b1'",
        "created_by INTEGER": "ALTER TABLE keys ADD COLUMN created_by INTEGER",
        "is_settled INTEGER DEFAULT 0": "ALTER TABLE keys ADD COLUMN is_settled INTEGER DEFAULT 0",
        "max_uses INTEGER DEFAULT 1": "ALTER TABLE keys ADD COLUMN max_uses INTEGER DEFAULT 1",
        "used_count INTEGER DEFAULT 0": "ALTER TABLE keys ADD COLUMN used_count INTEGER DEFAULT 0"
    },
    "supervisors": {
        "supervisor_name TEXT": "ALTER TABLE supervisors ADD COLUMN supervisor_name TEXT"
    }
}

# --- F-02: per-connection SQLite tuning (WAL + busy timeout + sync mode) ---
@asynccontextmanager
async def _db_connect():
    """Open a configured connection: WAL mode, 5s busy timeout, NORMAL sync.

    WAL persists in the DB file after first set, but busy_timeout and
    synchronous are per-connection, so every connection must set all three.
    """
    conn = await aiosqlite.connect(DB_NAME)
    try:
        await conn.execute("PRAGMA journal_mode=WAL;")
        await conn.execute("PRAGMA busy_timeout=5000;")
        await conn.execute("PRAGMA synchronous=NORMAL;")
        yield conn
    finally:
        await conn.close()


# --- Date utilities (HIGH-005): ISO 8601 standard ---
def _now_iso() -> str:
    """Return current UTC time as ISO 8601 string with timezone."""
    return datetime.now(timezone.utc).isoformat()

def _parse_iso_or_legacy(date_str: str) -> datetime | None:
    """Parse ISO 8601 string; fallback to legacy formats; always return timezone-aware UTC datetime."""
    if not date_str or not str(date_str).strip():
        return None
    s = str(date_str).strip()
    # Try ISO 8601 first (Python 3.11+ supports Z suffix)
    try:
        dt = datetime.fromisoformat(s.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        pass
    # Legacy fallbacks
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d'):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            continue
    return None

# --- دوال قاعدة البيانات (P2: migrated to aiosqlite — all blocking sqlite3 I/O now non-blocking) ---
async def init_db():
    async with _db_connect() as conn:
        # F-02: WAL mode + busy timeout + NORMAL sync so concurrent readers/writers
        # don't fail with "database is locked" under load.
        await conn.execute("PRAGMA journal_mode=WAL;")
        await conn.execute("PRAGMA busy_timeout=5000;")
        await conn.execute("PRAGMA synchronous=NORMAL;")
        # جدول users (تم إضافة حقول الأجوبة الإجمالية والصحيحة)
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY, 
                expire_date TEXT,
                sub_type TEXT DEFAULT 'b1',
                activated_via TEXT,
                is_group INTEGER DEFAULT 0,
                is_intensive INTEGER DEFAULT 0,
                total_answers INTEGER DEFAULT 0,
                correct_answers INTEGER DEFAULT 0
            )
        ''')
        
        # جدول keys
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS keys (
                key_code TEXT PRIMARY KEY, 
                sub_type TEXT DEFAULT 'b1',
                is_used INTEGER DEFAULT 0,
                created_by INTEGER,
                is_settled INTEGER DEFAULT 0,
                max_uses INTEGER DEFAULT 1,
                used_count INTEGER DEFAULT 0
            )
        ''')
        
        await conn.execute('CREATE TABLE IF NOT EXISTS completed_texts (user_id INTEGER, text_id TEXT, PRIMARY KEY (user_id, text_id))')
        
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS user_profiles (
                user_id INTEGER PRIMARY KEY,
                full_name TEXT,
                username TEXT,
                joined_at TEXT
            )
        ''')
        
        # جدول supervisors
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS supervisors (
                supervisor_id INTEGER PRIMARY KEY,
                supervisor_name TEXT,
                role TEXT DEFAULT 'supervisor',
                is_active INTEGER DEFAULT 1,
                balance REAL DEFAULT 0.0,
                commission_rate REAL DEFAULT 0.0,
                created_at TEXT
            )
        ''')

        # جدول referrals
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS referrals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id INTEGER,
                referred_id INTEGER UNIQUE,
                is_counted INTEGER DEFAULT 0,
                created_at TEXT
            )
        ''')

        # جدول free_credits
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS free_credits (
                user_id INTEGER PRIMARY KEY,
                credits_count INTEGER DEFAULT 0
            )
        ''')

        # جدول النصوص المخصصة (custom_texts)
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS custom_texts (
                service_key TEXT PRIMARY KEY,
                content TEXT NOT NULL
            )
        ''')

        # إدراج السجلات الافتراضية في جدول custom_texts إن لم تكن موجودة
        for key, default_text in DEFAULT_CUSTOM_TEXTS.items():
            await conn.execute('''
                INSERT OR IGNORE INTO custom_texts (service_key, content)
                VALUES (?, ?)
            ''', (key, default_text))
        
        # MED-004: Add index for supervisor finance queries
        await conn.execute('CREATE INDEX IF NOT EXISTS idx_keys_created_by ON keys(created_by)')
        
        # التعامل مع حقول الجداول التي أضيفت لاحقاً بأمان (Schema Migration) باستخدام القائمة البيضاء + PRAGMA
        allowed_tables = set(ALLOWED_MIGRATION_COLUMNS.keys())
        for table_name, columns in ALLOWED_MIGRATION_COLUMNS.items():
            if table_name not in allowed_tables:
                raise ValueError(f"Table name '{table_name}' not in migration allowlist")
            # Get existing columns for this table
            async with conn.execute(f"PRAGMA table_info({table_name})") as cursor:
                existing_columns = {row[1] for row in await cursor.fetchall()}
            
            for col_def, query in columns.items():
                # Extract column name from col_def (e.g., "sub_type TEXT DEFAULT 'b1'" -> "sub_type")
                col_name = col_def.split()[0]
                if col_name not in existing_columns:
                    await conn.execute(query)
        await conn.commit()

async def save_user_profile(user) -> bool:
    """
    تحديث/حفظ بروفايل المستخدم.
    ترجع True إذا كان المستخدم جديداً (أُضيف لأول مرة)، و False إذا كان موجوداً مسبقاً.
    P2: async + aiosqlite (non-blocking). HIGH-005: ISO 8601 datetime.
    """
    username = f"@{user.username}" if user.username else "بدون يوزر"
    full_name = user.full_name or "غير معروف"
    now_iso = _now_iso()
    
    async with _db_connect() as conn:
        # التحقق أولاً من وجود المستخدم سابقاً
        async with conn.execute('SELECT 1 FROM user_profiles WHERE user_id = ?', (user.id,)) as cursor:
            row = await cursor.fetchone()
            is_existing = row is not None
        
        await conn.execute('''
            INSERT INTO user_profiles (user_id, full_name, username, joined_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                full_name=excluded.full_name,
                username=excluded.username
        ''', (user.id, full_name, username, now_iso))
        await conn.commit()
        
        return not is_existing

async def get_user_details(user_identifier) -> dict:
    """
    دالة مساعدة تستقبل معرف المستخدم (user_id كـ int/str أو username كـ str)
    وترجع تفاصيله الكاملة شاملة البروفايل، الاشتراك الحالي، الإحالات والرصيد المجاني، والداعي إن وجد.
    P2: async + aiosqlite.
    """
    async with _db_connect() as conn:
        # 1. البحث عن البروفايل بحسب نوع المعرف (user_id أم username)
        target_id = None
        if isinstance(user_identifier, int):
            target_id = user_identifier
        elif isinstance(user_identifier, str):
            clean_input = user_identifier.strip()
            if clean_input.startswith('@'):
                clean_input = clean_input[1:]
            if clean_input.lstrip('-').isdigit():
                target_id = int(clean_input)
            else:
                target_id = clean_input

        if isinstance(target_id, int):
            async with conn.execute('SELECT user_id, full_name, username, joined_at FROM user_profiles WHERE user_id = ?', (target_id,)) as cursor:
                profile_row = await cursor.fetchone()
        else:
            username_str = f"@{target_id}"
            async with conn.execute('SELECT user_id, full_name, username, joined_at FROM user_profiles WHERE LOWER(username) = LOWER(?)', (username_str,)) as cursor:
                profile_row = await cursor.fetchone()
        
        if not profile_row:
            return None
            
        user_id, full_name, username, joined_at = profile_row

        # 2. معلومات الاشتراك الحالي
        subscription_info = await get_user_subscription(user_id)

        # 3. معلومات الإحالات والرصيد المجاني
        referral_info = await get_referral_info(user_id)

        # 4. البحث عن الشخص الداعي (الذي قام بدعوته)
        async with conn.execute('''
            SELECT r.referrer_id, p.full_name, p.username 
            FROM referrals r
            LEFT JOIN user_profiles p ON r.referrer_id = p.user_id
            WHERE r.referred_id = ?
        ''', (user_id,)) as cursor:
            referrer_row = await cursor.fetchone()
        
        referrer_info = None
        if referrer_row and referrer_row[0]:
            referrer_info = {
                "user_id": referrer_row[0],
                "full_name": referrer_row[1] or "غير معروف",
                "username": referrer_row[2] or "بدون يوزر"
            }

        return {
            "profile": {
                "user_id": user_id,
                "full_name": full_name,
                "username": username,
                "joined_at": joined_at
            },
            "subscription": subscription_info,
            "referrals": referral_info,
            "referrer": referrer_info
        }

async def record_answer_stat(user_id: int, is_correct: bool):
    """تحديث إحصائيات الإجابات للمستخدم (إجمالي الأسئلة والإجابات الصحيحة) — async/aio."""
    async with _db_connect() as conn:
        await conn.execute('INSERT OR IGNORE INTO users (user_id) VALUES (?)', (user_id,))
        
        if is_correct:
            await conn.execute('''
                UPDATE users 
                SET total_answers = COALESCE(total_answers, 0) + 1,
                    correct_answers = COALESCE(correct_answers, 0) + 1
                WHERE user_id = ?
            ''', (user_id,))
        else:
            await conn.execute('''
                UPDATE users 
                SET total_answers = COALESCE(total_answers, 0) + 1
                WHERE user_id = ?
            ''', (user_id,))
        await conn.commit()

async def get_user_answer_stats(user_id: int) -> dict:
    """جلب إحصائيات الإجابات وحساب النسبة المئوية — async/aio."""
    async with _db_connect() as conn:
        async with conn.execute('SELECT total_answers, correct_answers FROM users WHERE user_id = ?', (user_id,)) as cursor:
            row = await cursor.fetchone()
        
    if not row or not row[0]:
        return {"total": 0, "correct": 0, "accuracy": 0.0}
        
    total = row[0] or 0
    correct = row[1] or 0
    accuracy = round((correct / total) * 100, 1) if total > 0 else 0.0
    return {"total": total, "correct": correct, "accuracy": accuracy}

async def get_user_subscription(user_id: int):
    # 1. التحقق إن كان المستخدم أدمن رئيسي
    if user_id in ADMIN_IDS:
        return {"is_active": True, "type": "all", "expire_date": "غير محدود (أدمن)", "days_left": "∞"}
    
    # 2. التحقق إن كان المستخدم مشرف نشط
    async with _db_connect() as conn:
        async with conn.execute('SELECT is_active FROM supervisors WHERE supervisor_id = ?', (user_id,)) as cursor:
            sup_row = await cursor.fetchone()
            if sup_row and sup_row[0] == 1:
                return {"is_active": True, "type": "all", "expire_date": "غير محدود (مشرف)", "days_left": "∞"}

        async with conn.execute('SELECT expire_date, sub_type FROM users WHERE user_id = ?', (user_id,)) as cursor:
            row = await cursor.fetchone()

    if not row:
        return {"is_active": False, "type": None, "expire_date": None, "days_left": 0}

    date_str = row[0]

    # فحص شرطي لضمان أن قيمة تاريخ الانتهاء موجودة وليست None أو نصاً فارغاً
    if not date_str or not str(date_str).strip():
        return {"is_active": False, "type": row[1], "expire_date": None, "days_left": 0}

    expire_dt = _parse_iso_or_legacy(date_str)

    if not expire_dt:
        return {"is_active": False, "type": row[1], "expire_date": row[0], "days_left": 0}

    now = datetime.now(timezone.utc)

    if now < expire_dt:
        days_left = (expire_dt - now).days
        return {
            "is_active": True,
            "type": row[1],
            "expire_date": expire_dt.strftime('%Y-%m-%d'),
            "days_left": days_left if days_left > 0 else 1
        }
    else:
        return {"is_active": False, "type": row[1], "expire_date": row[0], "days_left": 0}

async def can_access_level(user_id: int, required_level: str) -> bool:
    sub = await get_user_subscription(user_id)
    if not sub["is_active"]:
        return False
    if sub["type"] == "all":
        return True
    if required_level == "b1" and sub["type"] in ['b1', 'all', 'intensive', 'group']:
        return True
    return sub["type"] == required_level

async def is_free_content(skill: str, teil: str, file_name: str, question_index: int = 0) -> bool:
    """
    دالة فحص مجانية النص أو الأسئلة بناءً على الشروط:
    1. القراءة (Lesen): النص الأول في المجلد/المجموعة الأولى (teil1) مجاني بالكامل.
    2. الاستماع (Hören): النص الأول في المجلد/المجموعة الأولى (teil1) مجاني للأسئلة الستة الأولى فقط (فهرس 0 حتى 5).
    MED-003: Uses catalog cache instead of os.listdir.
    """
    skill_clean = skill.lower().strip()
    teil_clean = teil.lower().strip()

    # التقييد بالمجلد/المجموعة الأولى فقط
    if teil_clean != "teil1":
        return False

    from utils import get_catalog
    catalog = await get_catalog()
    
    if skill_clean not in catalog or teil_clean not in catalog[skill_clean]:
        return False
    
    files = catalog[skill_clean][teil_clean]
    if not files or files[0] != file_name:
        return False

    # مهارة القراءة (Lesen): النص الأول مجاني بكامل أسئلته
    if skill_clean == "lesen":
        return True

    # مهارة الاستماع (Hören): النص الأول مجاني حتى السؤال السادس فقط (الفهرس من 0 إلى 5)
    if skill_clean in ["hören", "hoeren"]:
        return question_index < 6

    return False

async def revoke_user(user_id: int) -> bool:
    """Soft delete: archive user instead of destroying records (HIGH-009)."""
    revoked_date = "1970-01-01T00:00:00+00:00"
    async with _db_connect() as conn:
        cursor = await conn.execute('''
            UPDATE users 
            SET sub_type = 'revoked', expire_date = ? 
            WHERE user_id = ?
        ''', (revoked_date, user_id))
        await conn.commit()
        return cursor.rowcount > 0

async def mark_text_completed(user_id: int, text_id: str):
    async with _db_connect() as conn:
        await conn.execute('INSERT OR IGNORE INTO completed_texts (user_id, text_id) VALUES (?, ?)', (user_id, text_id))
        await conn.commit()

async def is_text_completed(user_id: int, text_id: str) -> bool:
    async with _db_connect() as conn:
        async with conn.execute('SELECT 1 FROM completed_texts WHERE user_id = ? AND text_id = ?', (user_id, text_id)) as cursor:
            row = await cursor.fetchone()
    return row is not None

async def reset_user_progress(user_id: int):
    async with _db_connect() as conn:
        await conn.execute('DELETE FROM completed_texts WHERE user_id = ?', (user_id,))
        await conn.execute('UPDATE users SET total_answers = 0, correct_answers = 0 WHERE user_id = ?', (user_id,))
        await conn.commit()

async def get_skill_progress(user_id: int, skill_name: str) -> dict:
    # MED-002: Use catalog cache instead of os.walk
    from utils import get_catalog
    catalog = await get_catalog()
    
    skill_key = skill_name.lower()
    total_files = 0
    if skill_key in catalog:
        for teil_files in catalog[skill_key].values():
            total_files += len(teil_files)
            
    async with _db_connect() as conn:
        search_pattern = f"{skill_name}_%"
        async with conn.execute('SELECT COUNT(*) FROM completed_texts WHERE user_id = ? AND text_id LIKE ?', (user_id, search_pattern)) as cursor:
            row = await cursor.fetchone()
            completed_count = row[0] if row else 0

    percentage = round((completed_count / total_files * 100), 1) if total_files > 0 else 0
    return {"completed": completed_count, "total": total_files, "percentage": percentage}

async def get_next_uncompleted_target(user_id: int) -> dict:
    """
    تحديد أولوية وأول جزء أو مهارة لم تكتمل بعد لإنشاء زر المتابعة التفاعلي
    P2: async — awaits is_text_completed (DB).
    F5/F6/F7: file lists come from the in-memory catalog cache (non-blocking).
    No direct os.listdir / os.walk here — must use await get_catalog().
    """
    from utils import get_catalog
    catalog = await get_catalog()
    skills = ["lesen", "hören"]
    for skill in skills:
        parts_count = 4 if skill == "hören" else 5
        for i in range(1, parts_count + 1):
            teil = f"teil{i}"
            files = catalog.get(skill, {}).get(teil, [])
            for file_name in files:
                text_id = f"{skill}_{teil}_{file_name}"
                if not await is_text_completed(user_id, text_id):
                    return {
                        "skill": skill,
                        "teil": teil,
                        "callback_data": f"b1_parts_{skill}_{teil}"
                    }
    return {"skill": "lesen", "teil": "teil1", "callback_data": "level_b1"}

async def activate_subscription(user_id: int, key: str) -> tuple[bool, str]:
    # P2: async + aiosqlite. Atomic key activation to prevent TOCTOU (CVE-2026-004).
    # Uses single UPDATE ... RETURNING to atomically claim key and get sub_type/max_uses.
    # F17: aiosqlite.Error is NOT swallowed — it raises naturally for the global error handler.
    async with _db_connect() as conn:
        # F-09: any failure after BEGIN IMMEDIATE must roll back before
        # propagating, otherwise the RESERVED lock is held until close.
        # F-06/F-09: strict try...except...finally guarantees rollback on failure.
        committed = False
        try:
            await conn.execute('BEGIN IMMEDIATE')

            # Atomic claim: increment used_count, set is_used if exhausted, return sub_type and max_uses
            async with conn.execute('''
                UPDATE keys 
                SET used_count = used_count + 1,
                    is_used = CASE WHEN used_count + 1 >= COALESCE(max_uses, 1) THEN 1 ELSE 0 END
                WHERE key_code = ? 
                  AND is_used = 0 
                  AND used_count < COALESCE(max_uses, 1)
                RETURNING sub_type, COALESCE(max_uses, 1)
            ''', (key,)) as cursor:
                row = await cursor.fetchone()

            if not row:
                await conn.rollback()
                committed = True
                return False, ""

            sub_type, max_uses = row
            max_uses = max_uses or 1

            days = 30
            is_intensive = 0
            is_group = 0

            if sub_type == 'intensive':
                days = 5
                is_intensive = 1
            elif max_uses > 1:
                is_group = 1

            current_utc = datetime.now(timezone.utc)
            async with conn.execute('SELECT expire_date FROM users WHERE user_id = ?', (user_id,)) as cursor:
                existing_row = await cursor.fetchone()
            current_expire = current_utc
            if existing_row and existing_row[0] and str(existing_row[0]).strip():
                existing_str = str(existing_row[0]).strip()
                parsed = _parse_iso_or_legacy(existing_str)
                if parsed:
                    current_expire = parsed
            expire_date = (max(current_utc, current_expire) + timedelta(days=days)).isoformat()
            await conn.execute('''
                INSERT INTO users (user_id, expire_date, sub_type, activated_via, is_group, is_intensive)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    expire_date = excluded.expire_date,
                    sub_type = excluded.sub_type,
                    activated_via = excluded.activated_via,
                    is_group = excluded.is_group,
                    is_intensive = excluded.is_intensive
            ''', (user_id, expire_date, sub_type, key, is_group, is_intensive))

            await _process_referral_reward_conn(conn, user_id)

            await conn.commit()
            committed = True
            return True, sub_type
        except Exception:
            logging.exception("Error in activate_subscription")
            raise
        finally:
            if not committed:
                try:
                    await conn.rollback()
                except Exception:
                    pass

async def generate_new_key(sub_type: str = "b1", created_by: int = None, max_uses: int = 1) -> str:
    key = f"KEY-{sub_type.upper()}-" + secrets.token_urlsafe(16)
    async with _db_connect() as conn:
        # F-06/F-09: strict try...except...finally guarantees rollback on failure.
        committed = False
        try:
            await conn.execute('BEGIN IMMEDIATE')
            await conn.execute('''
                INSERT INTO keys (key_code, sub_type, is_used, created_by, max_uses, used_count, is_settled) 
                VALUES (?, ?, 0, ?, ?, 0, 0)
            ''', (key, sub_type, created_by, max_uses))
            await conn.commit()
            committed = True
        except Exception:
            # F-06: never swallow — log before rollback + re-raise.
            logging.exception("Error in generate_new_key")
            raise
        finally:
            if not committed:
                try:
                    await conn.rollback()
                except Exception:
                    pass
    return key

# --- دوال نظام الإحالات والتفعيل المجاني ---

async def set_user_referrer(referred_id: int, referrer_id: int):
    """تسجيل الشخص الموصي (الإحالة) للمستخدم الجديد — async/aio. HIGH-005: ISO 8601 datetime."""
    if referred_id == referrer_id:
        return

    async with _db_connect() as conn:
        # F-06/F-09: strict try...except...finally guarantees rollback on failure.
        committed = False
        try:
            await conn.execute('BEGIN IMMEDIATE')
            await conn.execute('''
                INSERT INTO referrals (referrer_id, referred_id, is_counted, created_at)
                VALUES (?, ?, 0, ?)
            ''', (referrer_id, referred_id, _now_iso())
            )
            await conn.commit()
            committed = True
        except aiosqlite.IntegrityError:
            # Duplicate referral (UNIQUE referred_id) — expected idempotent path.
            logging.debug("Duplicate referral ignored for referred_id=%s", referred_id)
        except Exception:
            # F-06: never swallow critical DB errors silently.
            logging.exception("Error in set_user_referrer")
        finally:
            if not committed:
                try:
                    await conn.rollback()
                except Exception:
                    pass

async def _process_referral_reward_conn(conn: aiosqlite.Connection, user_id: int) -> tuple[int, int]:
    """دالة مساعدة تنفيذية تخدم عملية الإحالات داخل معاملة مسبقة القفل — async/aio.
    CVE-2026-005 fix: Atomic claim using UPDATE ... RETURNING to prevent double-count race.
    conn is an open aiosqlite connection already in BEGIN IMMEDIATE.
    """
    # Atomic claim: flip is_counted 0->1 and return referrer_id in single statement
    async with conn.execute('''
        UPDATE referrals 
        SET is_counted = 1 
        WHERE referred_id = ? AND is_counted = 0
        RETURNING referrer_id
    ''', (user_id,)) as cursor:
        row = await cursor.fetchone()
    
    if not row:
        return None, 0
    
    referrer_id = row[0]

    # Prevent self-referral farming
    if not referrer_id or referrer_id == user_id:
        return None, 0

    # Verify referrer actually exists before granting any reward
    async with conn.execute('SELECT 1 FROM user_profiles WHERE user_id = ?', (referrer_id,)) as cursor:
        referrer_exists = await cursor.fetchone() is not None
    if not referrer_exists:
        async with conn.execute('SELECT 1 FROM users WHERE user_id = ?', (referrer_id,)) as cursor:
            referrer_exists = await cursor.fetchone() is not None
    if not referrer_exists:
        return None, 0

    # Count total successful referrals for this referrer (including this one)
    async with conn.execute('SELECT COUNT(*) FROM referrals WHERE referrer_id = ? AND is_counted = 1', (referrer_id,)) as cursor:
        row = await cursor.fetchone()
        counted_refs = row[0] if row else 0

    # شرط محدد بدقة: إضافة الرصيد المجاني لمرة واحدة فقط عند الوصول لـ 2 أو أكثر بدلاً من استخدام المودولو المفتوح
    if counted_refs == 2:
        await conn.execute('''
            INSERT INTO free_credits (user_id, credits_count)
            VALUES (?, 1)
            ON CONFLICT(user_id) DO UPDATE SET credits_count = credits_count + 1
        ''', (referrer_id,))
    return referrer_id, counted_refs

async def process_referral_reward(user_id: int) -> tuple[int, int]:
    """احتساب نقاط الإحالة والمكافأة للداعي عند تفعيل المدعو لااشتراكه — async/aio."""
    async with _db_connect() as conn:
        # F-06/F-09: strict try...except...finally guarantees rollback on failure.
        committed = False
        try:
            await conn.execute('BEGIN IMMEDIATE')
            res = await _process_referral_reward_conn(conn, user_id)
            await conn.commit()
            committed = True
            return res
        except Exception:
            # F-06: never swallow critical DB errors silently.
            logging.exception("Error in process_referral_reward")
            return None, 0
        finally:
            if not committed:
                try:
                    await conn.rollback()
                except Exception:
                    pass

async def get_referral_info(user_id: int) -> dict:
    """جلب معلومات النقاط والرصيد المجاني للمستخدم — async/aio."""
    async with _db_connect() as conn:
        async with conn.execute('SELECT COUNT(*) FROM referrals WHERE referrer_id = ? AND is_counted = 1', (user_id,)) as cursor:
            row = await cursor.fetchone()
            points = row[0] if row else 0

        async with conn.execute('SELECT credits_count FROM free_credits WHERE user_id = ?', (user_id,)) as cursor:
            row = await cursor.fetchone()
            free_credits = row[0] if row else 0

    return {"points": points, "free_credits": free_credits}

async def claim_free_subscription(user_id: int) -> tuple[bool, str]:
    """تطبيق تفعيل اشتراك مجاني مقابل رصيد الإحالة — async/aio (keeps BEGIN IMMEDIATE). HIGH-005: ISO 8601 datetime."""
    async with _db_connect() as conn:
        # F-06/F-09: strict try...except...finally guarantees rollback on failure.
        committed = False
        try:
            await conn.execute('BEGIN IMMEDIATE')
            async with conn.execute('SELECT credits_count FROM free_credits WHERE user_id = ?', (user_id,)) as cursor:
                row = await cursor.fetchone()
            
            if not row or row[0] <= 0:
                await conn.rollback()
                committed = True
                return False, "ليس لديك رصيد مجاني متاح للاستهلاك."

            await conn.execute('UPDATE free_credits SET credits_count = credits_count - 1 WHERE user_id = ?', (user_id,))
            
            current_utc = datetime.now(timezone.utc)
            async with conn.execute('SELECT expire_date FROM users WHERE user_id = ?', (user_id,)) as cursor:
                existing_row = await cursor.fetchone()
            current_expire = current_utc
            if existing_row and existing_row[0] and str(existing_row[0]).strip():
                existing_str = str(existing_row[0]).strip()
                parsed = _parse_iso_or_legacy(existing_str)
                if parsed:
                    current_expire = parsed
            expire_date = (max(current_utc, current_expire) + timedelta(days=30)).isoformat()
            await conn.execute('''
                INSERT INTO users (user_id, expire_date, sub_type, activated_via, is_group, is_intensive)
                VALUES (?, ?, 'all', 'FREE_REFERRAL', 0, 0)
                ON CONFLICT(user_id) DO UPDATE SET
                    expire_date = excluded.expire_date,
                    sub_type = 'all',
                    activated_via = 'FREE_REFERRAL',
                    is_group = 0,
                    is_intensive = 0
            ''', (user_id, expire_date))
            await conn.commit()
            committed = True

            return True, "تم تفعيل الاشتراك الشامل المجاني لمدة 30 يوماً بنجاح!"
        except Exception:
            # F-06/F-09: roll back the BEGIN IMMEDIATE txn and log;
            # keep the (False, msg) contract — the caller has no try/except.
            logging.exception("Error in claim_free_subscription")
            return False, "حدث خطأ أثناء معالجة التفعيل المجاني."
        finally:
            if not committed:
                try:
                    await conn.rollback()
                except Exception:
                    pass

# --- دوال إدارة النصوص المخصصة (Custom Texts) ---

async def get_custom_text(service_key: str) -> str:
    """جلب النص المخصص لخدمة أو قسم معين، ويعود بالنص الافتراضي إذا لم يتم تعيينه — async/aio."""
    async with _db_connect() as conn:
        async with conn.execute('SELECT content FROM custom_texts WHERE service_key = ?', (service_key,)) as cursor:
            row = await cursor.fetchone()
            if row and row[0]:
                return row[0]
    return DEFAULT_CUSTOM_TEXTS.get(service_key, "لا يوجد نص محدد لهذه الخدمة حالياً.")

async def update_custom_text(service_key: str, new_content: str) -> bool:
    """تحديث أو إدراج نص جديد لخدمة معينة من قبل الأدمن والتأكيد بـ commit — async/aio."""
    async with _db_connect() as conn:
        # F-06/F-09: strict try...except...finally guarantees rollback on failure.
        committed = False
        try:
            await conn.execute('BEGIN IMMEDIATE')
            await conn.execute('''
                INSERT INTO custom_texts (service_key, content)
                VALUES (?, ?)
                ON CONFLICT(service_key) DO UPDATE SET content = excluded.content
            ''', (service_key, new_content))
            await conn.commit()
            committed = True
            return True
        except Exception:
            # F-06: never swallow critical DB errors silently.
            logging.exception("Error in update_custom_text")
            return False
        finally:
            if not committed:
                try:
                    await conn.rollback()
                except Exception:
                    pass

async def set_custom_text(service_key: str, new_content: str) -> bool:
    """دالة مطابقة لاستيراد handlers.admin لتحديث النصوص المخصصة — async wrapper."""
    return await update_custom_text(service_key, new_content)

async def get_all_custom_texts() -> dict:
    """جلب جميع النصوص المخصصة المسجلة في قاعدة البيانات — async/aio."""
    async with _db_connect() as conn:
        async with conn.execute('SELECT service_key, content FROM custom_texts') as cursor:
            rows = await cursor.fetchall()
            return {row[0]: row[1] for row in rows}

async def get_user_full_details(query_input) -> dict | None:
    """
    البحث عن مستخدم باستخدام ID (كعدد صحيح) أو Username (كنص) بشكل مستقل
    شاملة التفاصيل والاستعلام عن الاشتراك والإحالات — async/aio.
    MED-008: reuses get_user_details for base profile/subscription/referrer
    to avoid duplicating profile + referrer SQL.
    """
    # 1. Base profile data via shared helper (profile lookup + subscription + referrer)
    base = await get_user_details(query_input)
    if not base:
        return None

    profile = base["profile"]
    user_id = profile["user_id"]
    full_name = profile["full_name"]
    username = profile["username"]
    joined_at = profile["joined_at"]
    referrer_info = base.get("referrer")

    async with _db_connect() as conn:
        # 2. Extra subscription fields unique to the admin report (activated_via)
        async with conn.execute('SELECT expire_date, sub_type, activated_via FROM users WHERE user_id = ?', (user_id,)) as cursor:
            sub_row = await cursor.fetchone()

        sub_info = {"is_active": False, "type": "لا يوجد", "expire_date": None}
        activated_via = None

        if sub_row:
            exp_date_str, sub_type, activated_via = sub_row
            if exp_date_str:
                exp_date = _parse_iso_or_legacy(exp_date_str)
                if exp_date is not None and exp_date > datetime.now(timezone.utc):
                    sub_info = {
                        "is_active": True,
                        "type": sub_type or "b1",
                        "expire_date": exp_date_str
                    }

        # 3. Referral stats unique to the admin report
        async with conn.execute('SELECT credits_count FROM free_credits WHERE user_id = ?', (user_id,)) as cursor:
            pts_row = await cursor.fetchone()
            referral_points = pts_row[0] if pts_row else 0

        async with conn.execute('SELECT COUNT(*) FROM referrals WHERE referrer_id = ?', (user_id,)) as cursor:
            row = await cursor.fetchone()
            referred_count = row[0] if row else 0

        return {
            "user_id": user_id,
            "full_name": full_name or "غير معروف",
            "username": username or "بدون يوزر",
            "joined_at": joined_at or "غير معروف",
            "subscription": sub_info,
            "activated_via": activated_via or "لا يوجد",
            "referrer": referrer_info,
            "referral_points": referral_points,
            "referred_count": referred_count
        }

# MED-001: Pagination support for users list
async def get_users_count() -> int:
    """Return total number of users for pagination."""
    async with _db_connect() as conn:
        async with conn.execute('SELECT COUNT(*) FROM users') as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0

async def get_users_page(limit: int, offset: int) -> list[tuple]:
    """Fetch a page of users with profile info using JOIN.
    Returns list of (user_id, full_name, username, sub_type, expire_date).
    """
    async with _db_connect() as conn:
        async with conn.execute('''
            SELECT u.user_id, p.full_name, p.username, u.sub_type, u.expire_date
            FROM users u
            LEFT JOIN user_profiles p ON u.user_id = p.user_id
            ORDER BY u.user_id DESC
            LIMIT ? OFFSET ?
        ''', (limit, offset)) as cursor:
            rows = await cursor.fetchall()
            return rows

async def get_active_keys_count() -> int:
    """Return total count of active (usable) keys for pagination.

    Active = is_used = 0 AND used_count < COALESCE(max_uses, 1).
    P2: async + aiosqlite (non-blocking). Parameterized, no dynamic SQL.
    """
    async with _db_connect() as conn:
        async with conn.execute('''
            SELECT COUNT(*) FROM keys
            WHERE is_used = 0 AND used_count < COALESCE(max_uses, 1)
        ''') as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0

async def get_active_keys(limit: int, offset: int) -> list[tuple]:
    """Fetch a page of active keys ordered by key_code (Rule 15/20: SQL-level pagination).

    Returns list of (rowid, key_code, sub_type, used_count, max_uses).
    rowid is included so keyboards can build short revoke callbacks
    (rev_keyid_<rowid>) when key_code would exceed the 64-byte Telegram limit.
    """
    # Clamp to sane bounds (Rule 20: max 10 per page in UI; DB layer stays safe anyway).
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 10
    try:
        offset = int(offset)
    except (TypeError, ValueError):
        offset = 0
    limit = max(1, min(limit, 10))
    offset = max(0, offset)
    async with _db_connect() as conn:
        async with conn.execute('''
            SELECT rowid, key_code, sub_type, used_count, COALESCE(max_uses, 1)
            FROM keys
            WHERE is_used = 0 AND used_count < COALESCE(max_uses, 1)
            ORDER BY key_code
            LIMIT ? OFFSET ?
        ''', (limit, offset)) as cursor:
            rows = await cursor.fetchall()
            return rows

async def revoke_key(key_code: str) -> bool:
    """Revoke (invalidate) a key so it can no longer be used for activation.

    Sets is_used = 1 and used_count = COALESCE(max_uses, 1).
    Returns True if a row was updated, False if the key was not found.
    Uses BEGIN IMMEDIATE + parameterized query (Rules 3, 22).
    """
    if not key_code or not str(key_code).strip():
        return False
    key_code = str(key_code).strip()
    async with _db_connect() as conn:
        committed = False
        try:
            await conn.execute('BEGIN IMMEDIATE')
            cursor = await conn.execute(
                "UPDATE keys SET is_used = 1, used_count = COALESCE(max_uses, 1) WHERE key_code = ?",
                (key_code,),
            )
            await conn.commit()
            committed = True
            return cursor.rowcount > 0
        except Exception:
            logging.exception("Error in revoke_key")
            raise
        finally:
            if not committed:
                try:
                    await conn.rollback()
                except Exception:
                    pass

async def revoke_key_by_rowid(rowid: int) -> str | None:
    """Revoke a key by its SQLite rowid (short-callback fallback).

    Returns the key_code that was revoked, or None if not found.
    Used when key_code is too long to fit in a 64-byte callback_data.
    """
    try:
        rowid = int(rowid)
    except (TypeError, ValueError):
        return None
    async with _db_connect() as conn:
        committed = False
        try:
            await conn.execute('BEGIN IMMEDIATE')
            async with conn.execute(
                "SELECT key_code FROM keys WHERE rowid = ?", (rowid,)
            ) as cursor:
                row = await cursor.fetchone()
            if not row:
                await conn.rollback()
                committed = True
                return None
            key_code = row[0]
            await conn.execute(
                "UPDATE keys SET is_used = 1, used_count = COALESCE(max_uses, 1) WHERE rowid = ?",
                (rowid,),
            )
            await conn.commit()
            committed = True
            return key_code
        except Exception:
            logging.exception("Error in revoke_key_by_rowid")
            raise
        finally:
            if not committed:
                try:
                    await conn.rollback()
                except Exception:
                    pass
