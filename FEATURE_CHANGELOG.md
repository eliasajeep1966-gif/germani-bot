# FEATURE CHANGELOG

## 2026-09-24 — Group Button Question Ranges (dynamic UX)
- **Files Modified:**
  - `handlers/common.py` — `b1_parts_` group keyboard: replaced file-count ranges with dynamic question ranges; `multiplier = 2` for `hören/teil1` (1 file = 2 questions) else `1`; chunks built via `files[i:i+step]`; per group `start_num = (g_idx*step*multiplier)+1`, `end_num = start_num + len(chunk)*multiplier - 1`; text `المجموعة {g} ({start_num} - {end_num})`; removed now-unused `import math`
- **Feature Description:**
  - All group buttons (including the last partial group, e.g. `Menge 3`) now show a consistent question range (`Menge 3 (61-72)` instead of bare `Menge 3` / file range).
- **Technical Details:**
  - Old math `start=(g-1)*step+1, end=min(g*step,total)` counted files, undercounting `hören/teil1` by 2x. New logic counts questions and sizes the tail chunk by its actual length.
  - Verification: `py_compile` OK; checks `36 files hören/teil1 step15 → 1-30/31-60/61-72`, `36 files step10 → 1-10/11-20/21-30/31-36`, exact-multiple and single-partial tails OK.

## 2026-09-24 — Quiz False-Negative Fix (numeric correct_answer index)
- **Files Modified:**
  - `handlers/quiz.py` — `handle_answers`: `correct_answer` now normalized with `.strip().upper()` + numeric-index mapping (`isdigit()` → `chr(65 + int(...))`: 0→A, 1→B, 2→C) placed BEFORE `norm_map` logic; fixes `"A" == "0"` false-negative and wrong-answer feedback showing raw index
- **Feature Description:**
  - Users selecting the correct option (e.g. 'A' when JSON stores `0`) are now marked correct; feedback `full_text_ans` lookup also resolves to the right option letter.
- **Technical Details:**
  - Root cause: JSON `correct_answer` sometimes numeric index (int `0` or str `"0"`); string compare vs letter choice always failed. Fix is type-agnostic via `str(...).strip().upper()` + `isdigit` guard, idempotent for existing letter answers.
  - Current `data/b1/**` scan (106 files): no digit-only answers in tree today — fix is defensive for future/imported content.
  - Verification: `py_compile` OK; mapping checks `0→A, 1→B, 2→C, "0"→A`, letter answers unchanged, `Richtig vs A` still correctly False.

## 2026-09-20 — Referral Native Share Sheet (t.me/share/url)
- **Files Modified:**
  - `keyboards.py` — imported `urllib.parse.quote`; `get_referral_menu()` share button now `📤 مشاركة الرابط` with `url=https://t.me/share/url?url={quoted_ref}&text={quoted_text}` (no `callback_data`); free-credit claim + main-menu buttons untouched
- **Feature Description:**
  - Tapping share opens Telegram's native share picker (WhatsApp, chats, etc.) prefilled with the Arabic promo text + personal referral link, instead of a plain link button.
- **Technical Details:**
  - Both `url` and `text` params percent-encoded via `quote` (stdlib, no new deps); share URL verified to carry the encoded `ref_` link and promo text; `None`-link / no-credit variants still render 2 / 1 rows correctly.
  - Verification: `py_compile` OK; mocked-menu assertions (button text, `callback_data is None`, share-URL prefix + encoded payload, intact `claim_free_sub`/`main_menu` rows) pass.

## 2026-09-20 — User Profile Dashboard (ID Card)
- **Files Modified:**
  - `database.py` — added `get_user_dashboard_stats(user_id) -> tuple[int, int]` (single query, two scalar subqueries on `referrals`/`completed_texts` via `_db_connect()`; `(0, 0)` fallback, never raises)
  - `keyboards.py` — `get_main_menu()` new prominent first-row `👤 حسابي` (`user_profile`) button
  - `handlers/common.py` — added `cb_user_profile` (`F.data == "user_profile"`, registered before generic `handle_callbacks`); imports `get_user_dashboard_stats`, `get_cancel_to_main_keyboard`, `datetime` (`render_progress_bar`/`get_catalog` were already imported)
- **Feature Description:**
  - Students tap `👤 حسابي` for an ID-card view: escaped name, ID, subscription status, formatted expiry (`YYYY-MM-DD`, `غير محدود ♾️` for staff, `منتهي ❌` when inactive), study progress `{completed}/{total}` + bar + 1-decimal %, referral link (`?start=ref_{id}`) and invited count; back button to main menu.
- **Technical Details:**
  - No N+1: one dashboard query (counts) + cached `get_catalog()` total (106 texts) + existing `get_user_subscription`; no premium gating (read-only stats, consistent with `menu_referral` precedent).
  - Rule 8: `html.escape()` on `full_name`; ids/counts/percentages are ints/floats, referral link is bot-generated (no user input in HTML).
  - `safe_edit_message_text` (common.py's local helper) used for the edit — `safe_edit_message_text_or_send` lives in `quiz.py` and is not cross-imported (layering).
  - Verification: `py_compile` OK; live-DB stats `(0, 18)` + unknown-user `(0, 0)`; mocked handler confirms escaping (`&lt;b&gt;`), admin-unlimited expiry, inactive (`غير نشط`/`منتهي ❌`) and zero-progress paths, `parse_mode="HTML"` + `main_menu` back button; mock-created `user_profiles` rows removed afterward (pre-existing legacy `users` test row left untouched).

## 2026-09-20 — Dynamic Subscription Prices (Admin CMS) & Dynamic Paywall UX
- **Files Modified:**
  - `database.py` — added `price_monthly/price_intensive/price_group` defaults ("10"/"5"/"20") to `DEFAULT_CUSTOM_TEXTS` (seeded via `init_db`); added `VALID_PRICE_TYPES` allowlist + `get_current_prices() -> dict` (missing/placeholder/non-digit/non-positive fall back to 10/5/20, always ints, never raises)
  - `keyboards.py` — removed static `PAYWALL_TEXT`; added `get_dynamic_paywall_text()` + `get_dynamic_subscribe_text()` (CMS prices, 10/5/20 fallback); `get_admin_panel()` new `💰 تعديل الأسعار` (`admin_prices_menu`) button; added `get_edit_prices_menu()` (monthly/intensive/group + back)
  - `handlers/admin.py` — `AdminEditState` new `waiting_for_price` state (lives here, not `database.py`); removed `SUBSCRIPTION_PRICES` import (zero static uses left); added `cb_admin_prices_menu`, `cb_admin_start_edit_price` (allowlist-validated), `process_admin_save_price` (`isdigit` + 1–10000 range, retry-preserving, `/cancel` aware, persists via `set_custom_text(price_*)`); `cb_sup_gen_key`, `cb_sup_finance`, `cb_financial_settlement` now use `await get_current_prices()`
  - `handlers/common.py` — `start_subscribe_flow` uses `get_dynamic_subscribe_text()` (live plans) + appends admin `subscribe_flow` custom intro when set (never stale prices in default path)
  - `handlers/quiz.py` — all 7 paywall triggers now `await get_dynamic_paywall_text()` (import updated, no static string left)
- **Feature Description:**
  - Admins edit prices from panel → Prices menu → per-plan prompt; users always see live prices in subscribe flow and every paywall block (monthly 30d / intensive 5d / group 4-users line items with $ values).
  - Finance/settlement math (supervisor + admin views) and supervisor key-gen display price follow the same CMS source.
- **Technical Details:**
  - AuthZ (Rule 2): price UI stays in `admin.py`+`keyboards.py` behind `AdminAuthMiddleware` + explicit `ADMIN_IDS` main-admin checks; `callback.data` + FSM `price_type` allowlist-validated (`VALID_PRICE_TYPES`); price input strictly `isdigit` + range-guarded.
  - SQL: parameterized `set_custom_text`/`get_custom_text` only; seeding via existing `INSERT OR IGNORE` loop (no fake migrations); `config.SUBSCRIPTION_PRICES` left untouched as env-level source (DB overrides at runtime).
  - HTML/limits (Rules 8/16): prices are ints (no escaping needed); texts stay well under 4096.
  - Verification: `py_compile` OK; `get_current_prices` defaults/override/invalid-fallback OK; dynamic paywall/subscribe render OK; live price override reflected in paywall OK; `AdminEditState.waiting_for_price` + 3 price callbacks + panel button present; `SUBSCRIPTION_PRICES` count 0 in `admin.py`, `PAYWALL_TEXT` constant removed.

## 2026-09-20 — Bot Commands Menu (Role-Based Scopes)
- **Files Modified:**
  - `main.py` — imported `BotCommand`, `BotCommandScopeDefault`, `BotCommandScopeChat` (+ `ADMIN_IDS`); added `setup_bot_commands(bot)`; called it in `main()` before `dp.start_polling(bot)`
- **Feature Description:**
  - Global (default scope) user commands: `/start` (القائمة الرئيسية 🏠), `/cancel` (إلغاء العملية الحالية ❌).
  - Per-admin chat scope (`BotCommandScopeChat(chat_id)`) commands: user commands plus `/admin` ⚙️, `/genkey` 🔑, `/revoke_key` ❌, `/users` 👥, `/broadcast` 📢, `/health` 🩺.
  - Per-admin failures logged + ignored (admin hasn't started the bot yet).
- **Technical Details:**
  - No handler logic changed; commands map 1:1 to existing `Command(...)` handlers (`/start`, `/cancel`, `/admin`, `/genkey`, `/revoke_key`, `/users`, `/broadcast`, `/health` — all already registered in routers).
  - AuthZ unchanged: menus are UX-only; enforcement stays in `AdminAuthMiddleware` + explicit `ADMIN_IDS` checks.
  - Verification: `py_compile` OK; mocked-`Bot` test confirms 1 default-scope call (2 cmds) + N admin-scope calls (8 cmds each, `BotCommandScopeChat`), `setup_bot_commands` awaited before `start_polling`.

## 2026-09-20 — Pair-Shuffling (Hören Teil 1) & Dynamic Tips per Teil (CMS)
- **Files Modified:**
  - `handlers/quiz.py` — added `_build_question_order()`, `_resolve_question_order()`, `_get_ordered_question()`; `handle_read_text` builds + stores `question_order` in FSM; `send_quiz_question`, `handle_answers`, `handle_skip_question`, `handle_prev_question` resolve order with identity fallback and access via mapping (no cache mutation)
  - `database.py` — extended `DEFAULT_CUSTOM_TEXTS` with 9 `tips_{skill}_{teil}` defaults (lesen teil1-5, hören teil1-4); added `VALID_TIPS_KEYS` allowlist + `FALLBACK_TIPS` hardcoded fallback (seeded via `init_db` `INSERT OR IGNORE`)
  - `handlers/common.py` — added `get_teil_tip()` CMS helper (hoeren→hören normalized, missing-marker filtered, never raises); `b1_parts_` handler replaced hardcoded if/else with `await get_teil_tip()`; `t_group_` handler now prepends same tip
  - `keyboards.py` — `get_edit_texts_menu()` new `💡 نصائح الأقسام` (`admin_edit_tips_menu`) button; added `get_edit_tips_menu()` with 9 `admin_edit_text_tips_{skill}_{teil}` buttons (all ≤64 bytes) + back buttons
  - `handlers/admin.py` — imported `get_edit_tips_menu` + `VALID_TIPS_KEYS`; added `cb_admin_edit_tips_menu`; hardened `cb_admin_start_edit_text` with service+tips allowlist (rejects unknown keys); `process_admin_save_text` now enforces 3500-char cap (Rule 16) and already handles tips keys via generic `set_custom_text`
- **Feature Description:**
  - Task 1: Hören Teil 1 quizzes pair-shuffle (pairs `[0,1],[2,3],...` stay adjacent in order, pair order randomized); all other standard sections fully shuffle; Lesen Teil 3 matching keeps its custom logic (early return, untouched); per-session `question_order` stored in FSM, all consumers map `questions[order[idx]]` with legacy fallback.
  - Task 2: Per-Teil tips are now admin-editable CMS entries (`tips_lesen_teil1…teil5`, `tips_hören_teil1…teil4`); user-facing group/file lists prepend the tip; admin edits via Texts menu → Tips sub-menu, persisted in `custom_texts`.
- **Technical Details:**
  - Rule 14: never `random.shuffle(cached_list)` — only fresh index lists shuffled; `_extract_quiz_content` already copies; validators check length/range/uniqueness before trusting FSM order.
  - AuthZ (Rule 2): admin UI stays in `admin.py`+`keyboards.py` behind `AdminAuthMiddleware` + explicit `ADMIN_IDS` checks; `callback.data` treated as untrusted (allowlist validation).
  - SQL: parameterized `set_custom_text` only; tips seeding via existing `INSERT OR IGNORE` loop (no fake migrations).
  - HTML: tips contain trusted admin HTML (`parse_mode="HTML"` intentional, consistent with service texts); user content elsewhere still `html.escape()`d.
  - Limits (Rules 16/20): tips callbacks ~33 bytes; save capped at 3500 chars so prepended list messages stay under 4096.
  - Verification: `py_compile` OK; pair-shuffle keeps pairs adjacent/ordered (N=1,2,6,7,10); normal-shuffle permutation OK; resolve-fallback + no-mutate OK; keyboard 64-byte check OK; temp-DB `init_db` seeds all 9 tips, `get_teil_tip` alias/case/unknown OK, custom override roundtrip OK.

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
