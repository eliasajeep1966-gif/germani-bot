import os
import pathlib
from dotenv import load_dotenv

# --- تحميل متغيرات البيئة من ملف .env ---
load_dotenv()

# --- الثوابت والإعدادات العامة ---
# قراءة التوكن ديناميكياً من متغيرات البيئة بدلاً من كتابته بشكل صريح
API_TOKEN = os.getenv('API_TOKEN')
# F18: fail fast if the bot token is missing
if not API_TOKEN:
    raise ValueError("CRITICAL: API_TOKEN is missing in .env")

def _parse_admin_ids(raw: str | None) -> list[int]:
    """Parse comma/space-separated ADMIN_IDS env var into list of ints."""
    if not raw:
        return []
    ids: list[int] = []
    for part in raw.replace(';', ',').replace(' ', ',').split(','):
        part = part.strip()
        if not part:
            continue
        try:
            ids.append(int(part))
        except ValueError:
            raise ValueError(f"CRITICAL: Invalid ADMIN_IDS format: {part}")
    return ids

# NEVER hardcode secrets — load from environment (.env)
ADMIN_IDS: list[int] = _parse_admin_ids(os.getenv('ADMIN_IDS'))

# Seller / payments contact — single source of truth for purchase buttons
# Set SELLER_ID in .env (must be numeric Telegram user ID)
_raw_seller = (os.getenv('SELLER_ID') or '').strip()
if _raw_seller and not _raw_seller.isdigit():
    raise ValueError("SELLER_ID must be a numeric Telegram user ID (digits only)")
# F-11: fail fast — paywall buttons need a valid seller contact at startup.
if not _raw_seller:
    raise ValueError("CRITICAL: SELLER_ID is required in .env")
SELLER_ID: str = _raw_seller

# Log channel ID — load from .env, parse as int
_raw_log_channel = os.getenv('LOG_CHANNEL_ID')
if _raw_log_channel:
    try:
        LOG_CHANNEL_ID: int = int(_raw_log_channel.strip())
    except ValueError:
        raise ValueError("LOG_CHANNEL_ID must be a valid integer")
else:
    LOG_CHANNEL_ID = None

# Support group ID for the in-bot ticketing system — load from .env, parse as int.
# Tickets (user messages) are copied here; admins reply by replying to the
# metadata message. 0 = not configured (ticketing disabled gracefully).
_raw_support_group = os.getenv('SUPPORT_GROUP_ID')
if _raw_support_group and _raw_support_group.strip():
    try:
        SUPPORT_GROUP_ID: int = int(_raw_support_group.strip())
    except ValueError:
        raise ValueError("SUPPORT_GROUP_ID must be a valid integer")
else:
    SUPPORT_GROUP_ID = 0

# --- تحديد المسار الرئيسي للمشروع بشكل مطلق (MED-015) ---
BASE_DIR = pathlib.Path(__file__).parent.resolve()

# F19: absolute DB path so CWD never changes where the SQLite file lives
DB_NAME = str(BASE_DIR / "bot_database.db")

# --- أسعار الاشتراكات ومددها (LOW-006: env-overridable with safe int fallbacks) ---
def _safe_int_env(name: str, default: int) -> int:
    """Read integer env var; fall back to default on missing/malformed values."""
    try:
        return int((os.getenv(name) or '').strip() or default)
    except (ValueError, TypeError, AttributeError):
        return default

SUBSCRIPTION_PRICES = {
    'monthly': _safe_int_env('PRICE_MONTHLY', 10),      # سعر الاشتراك الشهري (30 يوم)
    'intensive': _safe_int_env('PRICE_INTENSIVE', 5),   # سعر اشتراك المراجعة المكثفة (5 أيام)
    'group': _safe_int_env('PRICE_GROUP', 20)           # سعر اشتراك المجموعة (4 أفراد)
}

SUBSCRIPTION_DURATIONS = {
    'monthly': 30,      # الأيام للشهري
    'intensive': 5,     # الأيام للمراجعة المكثفة
    'group': 30         # الأيام للاشتراك الجماعي لكل عضو
}

# --- هيكل الرتب لحساب الصلاحيات ---
ROLE_MAIN_ADMIN = 'main_admin'
ROLE_SUPERVISOR = 'supervisor'

ROLES = {
    ROLE_MAIN_ADMIN: 'أدمن رئيسي',
    ROLE_SUPERVISOR: 'مشرف/موزع'
}