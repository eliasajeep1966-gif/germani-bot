# FEATURE CHANGELOG

## 2026-09-19 — Active Keys Management Dashboard
- **Files Modified:**
  - `database.py` — added `get_active_keys(limit, offset)`, `get_active_keys_count()`, `revoke_key(key_code)`, `revoke_key_by_rowid(rowid)`
  - `keyboards.py` — `get_admin_panel()` new button `admin_manage_keys`; added `get_keys_pagination_keyboard(keys, page, total_pages)` + `_build_revoke_callback()` helper
  - `handlers/admin.py` — added `cb_admin_manage_keys`, `cb_admin_keys_page`, `cb_revoke_key_inline` + helpers `_get_keys_page_data`, `_format_keys_text`, `_extract_current_keys_page`; refactored `cmd_revoke_key` to reuse `revoke_key()`; added `logging` import
- **Feature Description:**
  - New Admin-only dashboard listing active (usable) keys (`is_used = 0 AND used_count < max_uses`, ordered by `key_code`) with 10-items-per-page pagination.
  - Each key row shows `❌ إبطال` + `<key_code> (<SUB_TYPE>)`; pagination row `⬅️ السابق / التالي ➡️` (`admin_keys_page_{page}`); back button to admin panel.
  - Inline revoke instantly removes the key from the list (Rule 19) with `callback.answer("✅ تم إبطال المفتاح", show_alert=True)`.
- **Technical Details:**
  - All DB access via `_db_connect()` (WAL + `busy_timeout=5000`, non-blocking `aiosqlite`); parameterized queries only (no f-string SQL); writes use `BEGIN IMMEDIATE` with `committed`-flag `try/except/finally` rollback + `logging.exception` on failure.
  - `callback_data` 64-byte safety (Rules 16/20): normal keys use `rev_key_{key_code}` (~37 bytes for generated keys); oversized codes fall back to `rev_keyid_{rowid}` (resolved via `revoke_key_by_rowid`); unknown `rev_keyh_*` hash buttons answer as expired.
  - AuthZ: router-level `AdminAuthMiddleware` + explicit `ADMIN_IDS` main-admin check in each new handler; `callback.data` treated as untrusted (page `int()` validated, key `strip()` + empty-check).
  - HTML injection safe (Rule 8): `html.escape()` on echoed `key_code`/`sub_type` in dashboard text.
  - Verification: `python -m py_compile` OK; temp-DB test (12 active keys, pagination 10+2, `ORDER BY key_code`, revoke + rowid revoke, keyboard byte-check, admin-panel button presence) passed; mocked-callback test confirms Rule 19 instant `edit_text` refresh and revoked key disappearance.

## 2026-09-19 — User Management UX Split (List vs Search)
- **Files Modified:**
  - `keyboards.py` — `get_admin_panel()`: replaced single `📋 قائمة المستخدمين` (`admin_list_users`) button with two distinct buttons: `👥 قائمة المستخدمين` (`admin_users_list`) and `🔍 بحث عن مستخدم` (`admin_search_user`)
  - `handlers/admin.py` — added `cb_admin_users_list` (page-1 list display); search handler `cb_admin_query_user_start` now listens to `admin_search_user` (+ legacy `admin_query_user`/`admin_list_users` aliases for stale buttons); extracted shared `_build_users_page_text_and_kb()` builder reused by `_send_users_page`, `cb_admin_users_list`, and `cb_admin_users_page`
- **Feature Description:**
  - Admin panel now separates concerns: `👥 قائمة المستخدمين` opens the paginated users list directly (page 1, `LIMIT 10`, prev/next via existing `admin_users_page_{n}`), while `🔍 بحث عن مستخدم` enters search mode (`AdminSupervisorState.waiting_for_user_query`, prompts for ID/Username).
  - Previously `admin_list_users` forced search mode instead of showing the list.
- **Technical Details:**
  - AuthZ unchanged: both new handlers gated by router-level `AdminAuthMiddleware` + explicit `ADMIN_IDS` main-admin check; list rendering keeps `html.escape()` on profile fields and `get_users_count`/`get_users_page` SQL pagination (no N+1).
  - Backward compat: legacy callbacks (`admin_list_users`, `admin_query_user`) still resolve to search mode so old panel messages don't break.
  - Verification: `python -m py_compile` OK; temp-DB test (panel callbacks present, shared builder single/multi-page output, mocked `cb_admin_users_list` edits message with page-1 rows, mocked search handler sets FSM state) passed.

## 2026-09-19 — In-Bot Ticketing System (Support)
- **Files Modified:**
  - `config.py` — added `SUPPORT_GROUP_ID: int` (validated via `int()`, default `0` when unset)
  - `.env` — added `SUPPORT_GROUP_ID=-1001234567890` (placeholder; replace with the real supergroup ID)
  - `database.py` — added `SupportState(StatesGroup)` FSM class with `waiting_for_message`
  - `keyboards.py` — `get_main_menu()` adds `💬 تواصل مع الإدارة` (`support_contact`); added `PAYWALL_TEXT` constant + `get_paywall_keyboard()`; removed all `url=f"tg://user?id={SELLER_ID}"` buttons in favor of in-bot support
  - `handlers/quiz.py` — removed all 7 `tg://user` paywall URL buttons, replaced with `get_paywall_keyboard()` and unified `PAYWALL_TEXT` (single source of truth, HTML-escaped)
  - `handlers/common.py` — added `support_rate_limit = TTLCache(maxsize=10000, ttl=300)` (max 3 messages per 300s per user); added `SupportState` import; added `cb_support_contact` callback (sets `SupportState.waiting_for_message`); added `process_support_message` handler (rate-limit check, `message.copy_to(SUPPORT_GROUP_ID)`, sends metadata reply `👤 من: {name}\n🆔 ID: <code>{id}</code>`); added `cmd_cancel_common` for graceful exit
  - `handlers/admin.py` — imported `SUPPORT_GROUP_ID` + `re`; added `cb_admin_reply_to_ticket` message handler (`F.chat.id == SUPPORT_GROUP_ID`, `F.reply_to_message`, `F.text`) that extracts `user_id` via `re.search(r"🆔 ID:\s*<code>(\d+)</code>", ...)`, copies the admin reply to the user via `message.copy_to(extracted_user_id)`, replies `✅ تم إرسال الرد للمستخدم.`, and catches `Exception` with `❌ تعذر إرسال الرد. قد يكون المستخدم قام بحظر البوت.` + `logging.exception`
  - `FEATURE_CHANGELOG.md` — this entry
- **Feature Description:**
  - Replaced all direct `tg://user?id={SELLER_ID}` deep-link admin buttons with an in-bot ticketing system: users tap `💬 تواصل مع الإدارة`, type their message, and it is copied to the support group with metadata; admins reply by replying to the metadata message and the reply is forwarded back to the user.
  - Rate limiting (3 messages / 5 minutes per user) prevents abuse.
  - HTML escaping applied to all user-facing names; no blocking I/O in handlers.
- **Technical Details:**
  - `SupportState.waiting_for_message` FSM state managed in `database.py` and imported by `handlers/common.py`.
  - `SUPPORT_GROUP_ID` defaults to `0` (disabled) when unset; `process_support_message` returns early if `SUPPORT_GROUP_ID <= 0`.
  - Rate limiter uses `cachetools.TTLCache(maxsize=10000, ttl=300)` with per-user timestamp lists and pruning of entries older than 300s.
  - Backward compat: removed `from config import SELLER_ID` from `handlers/quiz.py`; all paywall paths use shared `PAYWALL_TEXT` + `get_paywall_keyboard()`.
  - Verification: `python -m py_compile` OK; `tg://user` count == 0 in `handlers/quiz.py`; `SELLER_ID` removed from quiz imports; `support_contact`, `SupportState`, `support_rate_limit`, `cb_admin_reply_to_ticket` all present.
