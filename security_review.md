# Architecture & Security Review — Telegram B1 Bot
**Verdict: Prototype-grade. Ships with auth bypass paths, payment-logic bugs, and no persistence guarantees. Do not scale or take real money on this codebase without a rewrite of auth, state, and billing.**

## 1. Inferred Dependencies & Frameworks

No `requirements.txt` / `pyproject.toml` / lockfile found. Reconstructed from imports:

| Library | Evidence | Risk |
|---|---|---|
| `aiogram 3.x` | `Bot, Dispatcher, Router, F, BaseMiddleware, FSMContext, StatesGroup, FSInputFile, Command/CommandStart` | Unpinned version. Breaking changes will silently break you. |
| `python-dotenv` | `config.py:2 load_dotenv()` | Only used for `API_TOKEN`. Everything else hardcoded. |
| `sqlite3` (stdlib) | `database.py, admin.py` | No ORM, no pool, no WAL, no FK enforcement, blocking I/O in async loop. |
| stdlib: `asyncio, secrets, sqlite3, datetime, os, json, math, random, time` | throughout | `random.shuffle` for exam order, `time.time()` for throttle, naive `datetime.now()` for money-relevant expiry. |

Missing and needed: version pinning, `aiosqlite` or real DB driver, Redis for FSM/Throttle, structured logging, validation (`pydantic`), tests, linter, migration tool (`alembic`).

## 2. Architecture & Entry Points

**Update reception: Long-polling only.**
`main.py:39-43`: `await bot.delete_webhook(drop_pending_updates=True)` + `await dp.start_polling(bot)`. No webhook, no `allowed_updates` filter, no secret-token, no healthcheck, no graceful shutdown except `KeyboardInterrupt`.

**Router wiring (`main.py:33-36`) — order is load-bearing and fragile:**
1. `admin_router` (`handlers/admin.py`) — `/admin, /genkey, /revoke, /users, /broadcast` + `admin_*`, `sup_*`, `settle_*` callbacks. Guarded by `AdminAuthMiddleware`.
2. `texts_router` (`handlers/texts.py`) — **empty, dead code.** 4 lines, just `Router()`. Confuses dispatch and must be deleted.
3. `quiz_router` (`handlers/quiz.py`) — `read_*`, `ans_*`, `skip_question`, `prev_question`, `view_full_text`, `t3_q_/t3_a_` + Teil3 engine.
4. `common_router` (`handlers/common.py`) — `/start`, `AuthState.waiting_for_key`, and **catch-all** `@callback_query()` with no filter (line 170). Handles `main_menu, menu_training, menu_referral, b1_skill_*, b1_parts_*, t_group_*, user_progress`, services, PDFs, **plus duplicated** `admin_gen_*` and `admin_list_users`.

Problems:
- Catch-all at the end means any typo/new callback silently falls into `handle_callbacks` with `data` unhandled = silent drop. No logging, no fallback error.
- Admin logic lives in **two routers**. `common.py:183-201` re-implements `admin_gen_*` with ad-hoc `if user_id in ADMIN_IDS`. Drift = future bypass. Admin must live in one place.
- No global error handler (`dp.errors`). One `TelegramBadRequest` in quiz kills the coroutine. `safe_edit_message_text*` helpers swallow and `delete()+send()` causing message spam and FSM desync.
- Blocking calls in async handlers: `open().json.load()`, `os.listdir/walk`, `sqlite3.connect()` on every request. Under load this blocks the event loop. No caching.
- `Dispatcher()` uses default in-memory FSM storage. Restart = all quiz/auth states wiped. No timeout.

## 3. Data Flow & State Management

**DB: single-file SQLite `bot_database.db`, raw SQL strings, no models.**

Tables: `users(user_id, expire_date TEXT, sub_type, activated_via, is_group, is_intensive, total_answers, correct_answers)`, `keys(key_code, sub_type, is_used, created_by, is_settled, max_uses, used_count)`, `completed_texts(user_id,text_id)`, `user_profiles`, `supervisors`, `referrals`, `free_credits`, `custom_texts`.

Critical flaws:
- **No FKs, no indexes beyond PKs, no WAL mode.** `revoke_user()` deletes from `users` only — leaves `completed_texts, referrals, free_credits` orphans.
- **Fake migrations:** `ALLOWED_MIGRATION_COLUMNS` + `try: ALTER TABLE / except OperationalError: pass`. Silent schema drift across deploys. Use real migrations.
- **Time is broken:** `datetime.now()` naive local time stored as `'%Y-%m-%d %H:%M:%S'`. Parser in `get_user_subscription()` tries 3 formats. DST/timezone move = free days or early expiry. Money logic must use UTC ISO8601 + explicit tz.
- **Overwrite, not extend:** `activate_subscription()` does `ON CONFLICT(user_id) DO UPDATE SET expire_date=excluded.expire_date`. Existing paid days are **destroyed**. User renewing early loses money. Must be `max(now, current_expire)+days`.
- **Memory FSM bloat:** `QuizState` stores full `questions[]` + `text_body` + `keywords` per user in RAM. No expiry, no cap. `Teil3State` same. Attacker opening many texts = OOM.
- **Non-deterministic content:** `random.shuffle(questions_list)` except `hören/teil1` (`quiz.py:162`). No seed, no stable order. Breaks reproducibility, debugging, answer-stats comparability, and “intensive review” claims. Randomness here is a bug, not a feature.
- **Unbounded growth:** `ThrottlingMiddleware.user_timestamps: Dict[int,float]` never evicted. `user_profiles` grows forever. `broadcast` loads all `user_ids` into RAM.
- **Path security is the one decent part:** `find_directory_case_insensitive()` + `basename()` + `commonpath` check. Keep it, but add caching and stop doing `os.listdir` per callback.

**Content flow:** `b1_parts_{skill}_{teil}` -> list JSON files -> `read_{skill}_{teil}_{file}` loads JSON (`{title,summary,fixed_question,text/body,options,questions[]|audios[].questions[]|pairs[]}`) -> FSM -> `send_quiz_question()` -> `ans_*`. 106 JSON files under `data/b1/lesen|hören/teil*`. No schema validation; one malformed JSON = `تعذر قراءة` dead-end.

## 4. Authentication & Authorization — BROKEN BY DESIGN

**How roles are checked today:**
- `config.ADMIN_IDS = [2137767635, 720656784]` **hardcoded and committed**. `LOG_CHANNEL_ID = -1001234567890` placeholder. Token in `.env` (correctly gitignored, but already shared in workspace).
- `AdminAuthMiddleware` (`admin.py:52-82`) on `admin_router` only: `if uid in ADMIN_IDS or is_supervisor(uid): pass else swallow`. `is_supervisor()` = `SELECT is_active FROM supervisors WHERE id=?`.
- Everywhere else: ad-hoc `if uid in ADMIN_IDS` / `if is_supervisor() or uid in ADMIN_IDS`. No central `AdminOnly` filter, no decorator, no role enum enforcement (`ROLE_MAIN_ADMIN/ROLE_SUPERVISOR` defined in config but **never used**).
- Subscription bypass: `get_user_subscription()` returns `type=all, ∞` for admins/active supervisors without DB row. `can_access_level(uid,'b1')` = `is_active and (type==all or type==req)`.

**Missing centralized checks / bypasses:**
1. **Split-brain admin:** `common.handle_callbacks` handles `admin_gen_*` outside middleware. Today it re-checks, tomorrow someone forgets. Delete all admin branches from `common.py`.
2. **Free premium PDFs:** `common.py:356-431` `b1_skill_schreiben/sprechen` sends `schreiben_guide.pdf / sprechen_guide.pdf` with **zero** `can_access_level` check. Any unauthenticated user gets paid content. Same for `level_b1` menu itself — enumeration of titles/counts without sub.
3. **Client-controlled callbacks:** `read_`, `b1_parts_`, `t_group_`, `ans_*`, `prev/skip` trust `callback.data`. No HMAC/signature, no ownership check. `prev_question` has no paywall re-check at all (only `read/ans/skip` do). Step back into free zone, step forward — logic assumes linear flow.
4. **Throttle is not auth:** `ThrottlingMiddleware(0.5s)` silently `return`s (no `answer()`). Causes lost answers on fast tapping, retry storms, and per-process-only protection. No cleanup = memory leak. Does not stop Sybil/farm.
5. **PII leak:** `/users` dumps 20 users + sub status. `admin_list_users` + `get_user_full_details(id|@username)` has no audit log, no rate limit. Username lookup via `LOWER(username)=LOWER(?)` — spoofable, no pagination.
6. **Hardcoded seller:** purchase button `tg://user?id=8837732291` is **not** in `ADMIN_IDS`. Impersonate that ID = steal payments. Must be config-driven + verified channel.
7. **Supervisor TOCTOU:** `is_supervisor()` queried per-request. Deactivate mid-flow still leaves active FSM + `get_user_subscription` cache-less but inconsistent across handlers.

## 5. Payment & Business Logic — MANUAL KEYS, REAL BUGS

There is **no gateway**. Flow is prepaid activation codes sold out-of-band:

`SUBSCRIPTION_PRICES={monthly:10, intensive:5, group:20}`, `DURATIONS={monthly:30, intensive:5, group:30}`.

1. Admin/Supervisor: `admin_gen_{b1|b2|all|intensive|group}` / `/genkey <type>` / `sup_gen_{monthly_b1|monthly_b2|intensive|group}` -> `generate_new_key()` = `f"KEY-{TYPE}-{secrets.token_hex(3).upper()}"` (24-bit entropy, guessable prefix) with `max_uses=4` iff `group`, `created_by=uid`, `is_settled=0`.
2. User: `⭐ الاشتراك` (`start_subscribe_flow`) -> `AuthState.waiting_for_key` -> sends key -> `process_key()` -> `activate_subscription(uid,key)` under `BEGIN IMMEDIATE`: validate `exists and used_count<max_uses`, `days=5 if intensive else 30`, `UPDATE keys used_count/is_used`, `INSERT users expire=now+days ON CONFLICT overwrite`, `_process_referral_reward()`, commit.
3. Mid-quiz paywall: `is_free_content()` (only `teil1` first sorted file; `lesen`=all free, `hören`=first 6 idx) else require `can_access_level(uid,'b1')`. On fail, FSM is **overwritten** to `waiting_for_key` — quiz progress in RAM is lost.
4. Settlement: `sup_finance` / `admin_financial_settlement` sum `price*COUNT(keys)` where `created_by=sup AND is_settled=0`, then `UPDATE keys SET is_settled=1`.

**Exploitable / wrong:**
- **`intensive`/`group` keys don’t grant access.** `can_access_level(..., 'b1')` requires `sub_type==b1 or all`. `intensive`/`group` != `b1` => buyer pays $5/$20 and is still locked. Either deny sale or fix to capability set. This alone is payment fraud by bug.
- **`b2` keys sold for no content.** `level_b2` just alerts `قيد الإعداد`. Selling `b2/monthly_b2` today = charging for empty menu.
- **Finance counts created, not sold.** `sup_finance` groups `COUNT(*) FROM keys WHERE created_by` — unsold/unused keys create debt. Supervisor can be framed by key spam; admin books phantom revenue. Must sum `used_count` / joined activations.
- **No idempotency/audit:** no `key_redemptions(uid,key,at)` table. `activated_via` on user overwritten each time; history lost. Double-submit race mitigated by `BEGIN IMMEDIATE` but no request ID.
- **Weak keyspace:** 16M combos per type, no expiry, no revocation UI (`/revoke` deletes user, not key). Enumerable via `process_key` oracle (no rate limit beyond 0.5s throttle). Add 128-bit entropy, expiry, single-flight + captcha on failures.
- **Referral = free-money printer:** 2 counted activations -> 1x `free_credits` -> `claim_free_sub` grants `sub_type=all 30d`. Attacker mints 2 fake Telegram accounts, activates both with one $20 group key (4 uses), main account gets free $10 sub. Repeat infinitely. No referrer-exists check, no device/IP/graph analysis, `==2` exact-match (race to 3 skips reward, or double-grant under concurrency).
- **Free-account chain:** `claim_free_sub` doesn’t call referral reward, good, but free `all` account can still *be* a referrer for others? Yes — no distinction, amplifies farm.
- **UX destroys evidence:** paywall mid-quiz wipes `QuizState`. User can’t resume after paying. Must preserve state across `AuthState`.
- **Broadcast with no confirm:** one `/broadcast` typo spams entire base via `copy_message` loop. No preview/confirm/cancel, no batching.

## 6. Harsh Recommendations — FIX OR SHUT DOWN BILLING

**AuthZ (P0):**
- Delete `texts_router`. Move every `admin_*` branch out of `common.py` into `admin_router`. Single `AdminOnly / SupervisorOnly` filter (`aiogram.filters`) + unit tests that non-admin callback/message gets rejected. Fail closed with logging, not silent `return`.
- Load `ADMIN_IDS`, seller contact, log channel from env, not hardcoded. Rotate `API_TOKEN` now that it lived in workspace. Enforce webhook secret if you ever leave polling.
- Gate **everything** including `level_b1` listing, `schreiben/sprechen` PDFs, `user_progress` counts behind `can_access_level` or explicit free-tier flag. Current PDF bypass is indefensible.
- Sign callback data (HMAC `skill|teil|file|exp`) or store server-side session IDs. Never trust `read_*/ans_*` params blindly. Re-check paywall on `prev_question` too.

**Payments (P0):**
- Stop selling `b2`, `intensive`, `group` until `can_access_level` supports capabilities (`set{sub_types}` or `all`). Today they are broken SKUs.
- Change renewal to `expire = max(now_utc, current_expire_utc) + days`. Add `key_redemptions` ledger, key expiry, revocation, and 128-bit keys (`secrets.token_urlsafe(16)`).
- Settle on `SUM(used_count*price)` for activated users, not created keys. Require receipt/transfer ID on `settle_sup_*`, audit-log who settled when.
- Throttle + lock `process_key` (e.g. 5 tries / 10 min / user + global), return generic error to stop enumeration.
- Kill referral farming: require referrer exists + joined >N days, one reward per `referred_id` (already UNIQUE, keep), anti-Sybil (phone/age/activity gate), `>=` threshold with idempotent credit grant via unique constraint, abuse dashboard.

**Architecture (P1):**
- Pin deps + lockfile. Replace `sqlite3` blocking calls with `aiosqlite` + WAL + FKs + real migrations. Move FSM + throttle to Redis with TTL. Add `dp.errors` handler + structured logs (user, callback, latency, paywall decision).
- Cache JSON catalog at startup with schema validation (`pydantic`), preload titles/counts. Remove per-request `listdir/open`. Remove `random.shuffle` or make it opt-in `🔀 shuffle` with seeded session so stats are comparable.
- Preserve quiz FSM across auth: stash `pending_quiz` before switching to `waiting_for_key`, restore after success. Fix `menu_referral` bug (`reply_markup=bool` at `common.py:226` — must be `get_referral_menu(link, has_credit)`).
- Fix time: UTC everywhere (`datetime.now(timezone.utc)`), store ISO8601, test expiry boundaries. Add cron to warn `days_left<=3`.
- Replace naive throttle with sliding-window + burst (e.g. 5/2s quiz, 3/min key tries), with `callback.answer()` feedback and metrics. Evict old entries.
- Add `/broadcast` preview + confirm + dry-run count + cancel. Paginate `/users` and redacts PII. Audit-log admin actions.

Bottom line: the path-traversal guard is fine; almost everything around money, roles, and state is not. Freeze new SKUs, fix `intensive/group/b2` access, centralize auth, and put billing on a ledger before the next paid user.
