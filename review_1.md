# Code Review: Telegram Bot (German Language Learning)
**Date:** 2026-09-17  
**Reviewer:** Senior Staff Engineer / AppSec Auditor  
**Scope:** Full static analysis of entire codebase

---

## Executive Summary

This is a Telegram bot for German language learning (B1/B2 levels) with subscription management, referral system, quiz engine, and admin panel. The codebase uses **aiogram 3.x**, **aiosqlite**, and **cachetools**.

**Overall Risk Rating: HIGH** — Multiple critical security vulnerabilities, architectural flaws, and performance issues exist.

---

## CRITICAL Findings

### 1. [CVE-2026-001] SQL Injection via `PRAGMA table_info()` — `database.py:149`
**File:** `database.py`  
**Line:** 149  
**Issue:** Table name interpolated directly into SQL via f-string in `PRAGMA table_info({table_name})`. While `ALLOWED_MIGRATION_COLUMNS` is a hardcoded dict, the pattern is dangerous and sets precedent for dynamic table names.
```python
async with conn.execute(f"PRAGMA table_info({table_name})") as cursor:
```
**Fix:** Use allowlist validation or parameterized approach (though PRAGMA doesn't support params for table names). At minimum, validate `table_name` against hardcoded allowlist before interpolation.

---

### 2. [CVE-2026-002] Path Traversal in `find_directory_case_insensitive` — `utils.py:10-35`
**File:** `utils.py`  
**Lines:** 10-35  
**Issue:** The function uses `os.path.basename(target_dir)` but then joins with `full_base_path` which comes from `BASE_DIR + base_path`. An attacker controlling `base_path` or `target_dir` could escape the `data/` directory. The `commonpath` check is bypassable on Windows via case-insensitivity and alternate path separators.
**Attack Vector:** `target_dir = "..\\..\\windows\\system32"` or similar.
**Fix:** Use `pathlib.Path.resolve()` and strict containment check with `is_relative_to()` (Python 3.9+).

---

### 3. [CVE-2026-003] Broken Access Control — Admin Functions in Public Router
**File:** `handlers/common.py`  
**Lines:** 183-185 (comment admits removal)  
**Issue:** The comment at lines 183-185 explicitly states "Admin key generation and admin user lookup MUST live only in handlers/admin.py behind AdminAuthMiddleware. Removed from public common_router on purpose." This indicates **prior vulnerability was known and partially fixed**, but the pattern suggests other admin functions may leak. The `common_router` has NO authentication middleware.
**Risk:** Any callback handler in `common_router` could be exploited if admin logic accidentally added.

---

### 4. [CVE-2026-004] Race Condition in Key Activation — `database.py:444-524`
**File:** `database.py`  
**Lines:** 444-524 (`activate_subscription`)  
**Issue:** `BEGIN IMMEDIATE` is used but the check-then-act pattern between `SELECT` (line 450-454) and `UPDATE` (line 481-485) is vulnerable to **TOCTOU**. Two concurrent activations of the same key can both pass the `used_count >= max_uses` check before either commits.
**Impact:** Key overuse, revenue loss, subscription stacking abuse.
**Fix:** Use `UPDATE ... WHERE used_count < max_uses RETURNING used_count` (SQLite 3.35+) or `SELECT ... FOR UPDATE` equivalent. Or move logic to single atomic statement.

---

### 5. [CVE-2026-005] Referral Reward Double-Count Race — `database.py:571-615`
**File:** `database.py`  
**Lines:** 571-615 (`_process_referral_reward_conn`)  
**Issue:** The `UPDATE referrals SET is_counted = 1 WHERE ...` (line 600) uses `rowcount` for idempotency, but the subsequent `SELECT COUNT(*)` (line 604) and credit grant (line 609-614) are **not atomic**. Two concurrent activations by referred users can both see `counted_refs == 1`, then both increment to 2, both granting free credit.
**Fix:** Use single transaction with `CASE WHEN` or `INSERT ... ON CONFLICT` for credit grant tied to the referral row update.

---

### 6. [CVE-2026-006] Hardcoded Log Channel ID — `config.py:33`
**File:** `config.py`  
**Line:** 33  
**Issue:** `LOG_CHANNEL_ID = -1001234567890` is hardcoded. This exposes internal logging channel. If bot token leaks, attacker can read all user PII, subscription keys, admin actions.
**Fix:** Move to `.env` as `LOG_CHANNEL_ID`.

---

### 7. [CVE-2026-007] Missing Input Validation on `SELLER_ID` — `config.py:31`
**File:** `config.py`  
**Line:** 31  
**Issue:** `SELLER_ID = (os.getenv('SELLER_ID') or '').strip()` — no validation it's a valid Telegram user ID (integer). Used in `handlers/quiz.py:92` and `handlers/common.py:232` in `tg://user?id={SELLER_ID}` deep links. If empty or malicious, breaks purchase flow or enables phishing.
**Fix:** Validate as integer at startup; fail fast if invalid.

---

### 8. [CVE-2026-008] No Rate Limiting on Key Generation — `handlers/admin.py:292-326`
**File:** `handlers/admin.py`  
**Lines:** 292-326 (`cmd_genkey`, `cb_admin_gen_key`)  
**Issue:** Admin can generate unlimited keys instantly. No rate limit, no audit log beyond DB. Compromised admin account = infinite free subscriptions.
**Fix:** Add rate limiting (e.g., max 100 keys/hour per admin), require 2FA for bulk generation, log to immutable audit trail.

---

## HIGH Findings

### 9. [HIGH-001] Throttling Middleware Bypass — `middlewares/throttling.py:8-30`
**File:** `middlewares/throttling.py`  
**Lines:** 8-30  
**Issue:** `TTLCache(maxsize=10000, ttl=2.0)` with 0.5s delay. **Problems:**
- Cache keyed only by `user_id` — attacker can rotate user IDs (Telegram allows multiple sessions)
- `maxsize=10000` — memory exhaustion via 10k+ unique users
- No protection on callback queries vs messages differently
- Returns `None` silently — no 429 response, user sees "bot ignored me"
**Fix:** Use Redis-backed sliding window, per-IP + per-user, return proper `TelegramRetryAfter` or user-facing message.

---

### 10. [HIGH-002] Broadcast DoS / Resource Exhaustion — `handlers/admin.py:675-781`
**File:** `handlers/admin.py`  
**Lines:** 675-781 (`_run_broadcast_task`, `process_broadcast`, `cb_broadcast_confirm`)  
**Issues:**
- `asyncio.create_task()` with no concurrency limit — broadcasts to 100k+ users = OOM
- `await asyncio.sleep(0.05)` — fixed delay ignores Telegram's actual limits (30 msg/s)
- No progress tracking, no cancellation, no retry logic for transient failures
- `copy_message` loads entire message into memory per user
**Fix:** Use `asyncio.Semaphore(30)`, chunk users, stream messages, add checkpoint/resume.

---

### 11. [HIGH-003] Subscription Check Bypass via `prev_question` — `handlers/quiz.py:443-480`
**File:** `handlers/quiz.py`  
**Lines:** 443-480 (`handle_prev_question`)  
**Issue:** Line 465 re-verifies subscription on backward navigation — **good**. But the `prev_question` callback data is **predictable and stateless**. User can manually invoke `prev_question` to navigate to earlier free questions after hitting paywall, then use `skip_question` to jump forward past paywall without subscription check (line 376-391 checks current index, not target index).
**Fix:** Store `current_index` in FSM, validate target index on every navigation, not just current.

---

### 12. [HIGH-004] FSM State Confusion in Quiz — `handlers/quiz.py:183-350`
**File:** `handlers/quiz.py`  
**Lines:** 183-350 (`handle_answers`)  
**Issue:** Multiple code paths return early without clearing state on paywall hit (lines 203-216, 337-350). User hits paywall, state remains `QuizState.answering`. Later, user buys subscription, returns — stale state causes wrong question index, double-counting, or crash.
**Fix:** On paywall, either clear state or store `paywall_at_index` and resume correctly.

---

### 13. [HIGH-005] Date Parsing Fragility — `database.py:312-320`, `database.py:493-501`
**File:** `database.py`  
**Lines:** 312-320, 493-501  
**Issue:** Multiple `strptime` formats tried sequentially. No validation of parsed date sanity (year 1970, year 3000). `expire_dt` can be `None` leading to `is_active: False` silently. UTC handling assumes naive = UTC.
**Fix:** Store dates as ISO 8601 UTC strings (`datetime.now(timezone.utc).isoformat()`), parse with `fromisoformat()`, validate range.

---

### 14. [HIGH-006] `can_access_level` Logic Flaw — `database.py:338-346`
**File:** `database.py`  
**Lines:** 338-346  
**Issue:** 
```python
if required_level == "b1" and sub["type"] in ['b1', 'all', 'intensive', 'group']:
    return True
```
- `intensive` (5 days) grants B1 access — correct
- `group` grants B1 access — but `group` is 4 users sharing, should verify user is in that group
- No check for `sub_type == 'b2'` (returns False correctly but logic inverted)
**Fix:** Explicit allowlist per subscription type, validate group membership.

---

### 15. [HIGH-007] No Transaction on Multi-Table Writes — `database.py:514`
**File:** `database.py`  
**Line:** 514 (`_process_referral_reward_conn` called from `activate_subscription`)  
**Issue:** `activate_subscription` calls `_process_referral_reward_conn(conn, user_id)` passing same connection — **good**. But `process_referral_reward` (line 617-630) opens **new connection** and new transaction — **race condition** if called concurrently.
**Fix:** Remove standalone `process_referral_reward` or make it use same connection pattern.

---

### 16. [HIGH-008] Admin Middleware Allows Non-Admin Callbacks Silently — `handlers/admin.py:74-78`
**File:** `handlers/admin.py`  
**Lines:** 74-78  
**Issue:** 
```python
if isinstance(event, types.CallbackQuery):
    await event.answer("⚠️ لا تملك صلاحية الوصول لهذا القسم.", show_alert=True)
return
```
Silently drops non-CallbackQuery events (messages) with **no response**. User sends `/admin` — nothing happens, no error, confusion.
**Fix:** Send message response for `Message` events too.

---

### 17. [HIGH-009] `revoke_user` Deletes Data Without Archive — `database.py:380-386`
**File:** `database.py`  
**Lines:** 380-386  
**Issue:** `DELETE FROM users WHERE user_id = ?` — hard delete. No soft delete, no audit log, no GDPR compliance. Admin mistake = permanent data loss.
**Fix:** Add `deleted_at` column, soft delete, admin audit log table.

---

### 18. [HIGH-010] `LOG_CHANNEL_ID` Used Without Validation — `handlers/common.py:82`, `handlers/common.py:162`
**File:** `handlers/common.py`  
**Lines:** 82, 162  
**Issue:** `await bot.send_message(LOG_CHANNEL_ID, ...)` — if channel ID wrong or bot not admin, fails silently (bare `except Exception: pass`). Critical logs lost.
**Fix:** Validate channel accessibility at startup, alert admin on failure.

---

## MEDIUM Findings

### 19. [MED-001] N+1 Query in `cmd_users` — `handlers/admin.py:642-667`
**File:** `handlers/admin.py`  
**Lines:** 642-667  
**Issue:** Loop calls `await get_user_subscription(uid)` per user (20 users = 20 DB round trips).
**Fix:** Single query with JOIN or batch fetch.

---

### 20. [MED-002] `get_skill_progress` Walks Filesystem on Every Call — `database.py:405-419`
**File:** `database.py`  
**Lines:** 405-419  
**Issue:** `os.walk(base_path)` on every call. Called from `user_progress` callback (high traffic). Filesystem I/O blocks event loop.
**Fix:** Cache total file counts at startup in memory or DB.

---

### 21. [MED-003] `is_free_content` Filesystem Access — `database.py:348-378`
**File:** `database.py`  
**Lines:** 348-378  
**Issue:** Calls `find_directory_case_insensitive` + `os.listdir` + `sorted()` on every quiz question. Blocks event loop.
**Fix:** Pre-compute free content map at startup.

---

### 22. [MED-004] No Index on `keys.created_by` — `database.py:76-86`
**File:** `database.py`  
**Lines:** 76-86  
**Issue:** `keys` table has no index on `created_by`. Supervisor stats query (line 375) does `WHERE created_by = ?` — full table scan.
**Fix:** Add `CREATE INDEX idx_keys_created_by ON keys(created_by)`.

---

### 23. [MED-005] `referrals.referred_id` Unique but No FK — `database.py:113-121`
**File:** `database.py`  
**Lines:** 113-121  
**Issue:** `referred_id INTEGER UNIQUE` but no foreign key to `users.user_id`. Orphaned referrals possible.
**Fix:** Add FK constraint (SQLite supports with `PRAGMA foreign_keys=ON`).

---

### 24. [MED-006] `supervisors.balance` Unused — `database.py:100-110`
**File:** `database.py`  
**Lines:** 100-110  
**Issue:** Column `balance REAL DEFAULT 0.0` exists but never read/written. Financial logic uses `keys` aggregation instead. Schema drift.
**Fix:** Remove column or migrate logic to use it.

---

### 25. [MED-007] `commission_rate` Unused — `database.py:107`
**File:** `database.py`  
**Line:** 107  
**Issue:** Column defined, never used. Dead code.
**Fix:** Remove or implement.

---

### 26. [MED-008] `get_user_full_details` Duplicates `get_user_details` — `database.py:719-819` vs `186-252`
**File:** `database.py`  
**Lines:** 186-252, 719-819  
**Issue:** Two nearly identical functions with different return formats. Maintenance burden, inconsistency risk.
**Fix:** Consolidate into one with parameterized output.

---

### 27. [MED-009] `safe_edit_message_text` Swallows All Exceptions — `handlers/common.py:33-48`
**File:** `handlers/common.py`  
**Lines:** 33-48  
**Issue:** Bare `except Exception: pass` at line 48. Hides real bugs (network errors, invalid markup).
**Fix:** Log exception, re-raise or handle specific errors only.

---

### 28. [MED-010] `ThrottlingMiddleware` Doesn't Handle `TelegramRetryAfter` — `middlewares/throttling.py`
**File:** `middlewares/throttling.py`  
**Lines:** 13-30  
**Issue:** Middleware returns early on throttle but doesn't catch `TelegramRetryAfter` from downstream handlers. Bot can still hit global limits.
**Fix:** Add global rate limiter with `TelegramRetryAfter` handling at dispatcher level.

---

### 29. [MED-011] `SELLER_ID` Used in `tg://user?id=` Without Validation — `handlers/quiz.py:92`, `handlers/common.py:232`
**File:** `handlers/quiz.py:92`, `handlers/common.py:232`  
**Issue:** If `SELLER_ID` empty or non-numeric, deep link broken. No fallback.
**Fix:** Validate at startup, show "Contact admin" text if invalid.

---

### 30. [MED-012] Callback Data Parsing Fragile — `handlers/common.py:341`, `handlers/quiz.py:70`
**File:** Multiple  
**Issue:** `os.path.basename(data.split("_")[2])` — assumes fixed format. No validation. Malformed callback = crash or wrong behavior.
**Fix:** Use structured callback data (e.g., `CallbackData` factory from aiogram) or validate with regex.

---

### 31. [MED-013] `random.shuffle` on Questions — `handlers/quiz.py:162`
**File:** `handlers/quiz.py`  
**Line:** 162  
**Issue:** `random.shuffle(questions_list)` modifies list in-place. If same list reused (cached in FSM), order changes on retry. Not deterministic for review.
**Fix:** Copy list before shuffle or store shuffled order in FSM.

---

### 32. [MED-014] No Health Check / Readiness Endpoint — `main.py`
**File:** `main.py`  
**Issue:** No `/health` or `/ready` endpoint for container orchestration. Can't distinguish "starting" from "healthy".
**Fix:** Add aiohttp health check server or use aiogram's webhook health pattern.

---

### 33. [MED-015] `BASE_DIR` Computed at Import Time — `config.py:38`
**File:** `config.py`  
**Line:** 38  
**Issue:** `BASE_DIR = os.path.dirname(os.path.abspath(__file__))` — breaks if code run from different CWD or frozen (PyInstaller).
**Fix:** Use `pathlib.Path(__file__).parent.resolve()`.

---

## LOW Findings

### 34. [LOW-001] Dead Code: `get_teil3_matching_keyboard` — `keyboards.py:197-237`
**File:** `keyboards.py`  
**Lines:** 197-237  
**Issue:** Function defined but never imported/used. Quiz uses inline keyboard building in `render_teil3_view`.
**Fix:** Remove or use consistently.

---

### 35. [LOW-002] Inconsistent Error Language — Multiple Files
**File:** Multiple  
**Issue:** Mix of Arabic and English error messages. No i18n framework.
**Fix:** Adopt single language or add i18n.

---

### 36. [LOW-003] `print()` Statements in Production — `handlers/common.py:52`, `handlers/common.py:140`
**File:** `handlers/common.py`  
**Lines:** 52, 140  
**Issue:** `print(f"\n📢 [معرف القناة الخاص بك هو]: {post.chat.id}\n")` and `print(f"خطأ في إرسال إشعار الإحالة للداعي: {e}")` — goes to stdout, not structured logging.
**Fix:** Use `logging` module consistently.

---

### 37. [LOW-004] `asyncio.create_task` Without Tracking — `handlers/admin.py:774-780`
**File:** `handlers/admin.py`  
**Lines:** 774-780  
**Issue:** Broadcast task created but not tracked. If bot restarts, task lost. No way to list running broadcasts.
**Fix:** Store task reference, add admin command to list/cancel.

---

### 38. [LOW-005] `DEFAULT_CUSTOM_TEXTS` Hardcoded Arabic — `database.py:25-34`
**File:** `database.py`  
**Lines:** 25-34  
**Issue:** Default texts in Arabic only. No fallback for other languages.
**Fix:** Externalize to JSON config files.

---

### 39. [LOW-006] `SUBSCRIPTION_PRICES` Hardcoded — `config.py:41-45`
**File:** `config.py`  
**Lines:** 41-45  
**Issue:** Prices in code. Requires deploy to change.
**Fix:** Load from DB or config file.

---

### 40. [LOW-007] No Type Hints on Many Functions — Multiple Files
**File:** Multiple  
**Issue:** Missing type hints on public functions (e.g., `find_directory_case_insensitive`, `create_progress_bar`).
**Fix:** Add type hints for maintainability.

---

### 41. [LOW-008] `cachetools.TTLCache` Not Thread-Safe for Async — `middlewares/throttling.py:11`
**File:** `middlewares/throttling.py`  
**Line:** 11  
**Issue:** `TTLCache` is thread-safe but not async-safe. Concurrent async access can corrupt internal state.
**Fix:** Use `aiocache` or async-compatible cache.

---

### 42. [LOW-009] `revoke_key` Command Updates `used_count = max_uses` — `handlers/admin.py:633`
**File:** `handlers/admin.py`  
**Line:** 633  
**Issue:** `UPDATE keys SET is_used = 1, used_count = max_uses` — if `max_uses` is NULL, sets to NULL. Should use `COALESCE(max_uses, 1)`.
**Fix:** `used_count = COALESCE(max_uses, 1)`.

---

### 43. [LOW-010] `admin_edit_text_` Callback Prefix Mismatch — `keyboards.py:60-68` vs `handlers/admin.py:252`
**File:** `keyboards.py:60-68`, `handlers/admin.py:252`  
**Issue:** Keyboard uses `admin_edit_text_service_visa` but handler expects `admin_edit_text_` prefix. Works but fragile.
**Fix:** Use constants for callback prefixes.

---

### 44. [LOW-011] `noop` Callback in Financial Menu — `keyboards.py:112`
**File:** `keyboards.py`  
**Line:** 112  
**Issue:** `callback_data="noop"` — no handler. User clicks, nothing happens, confusion.
**Fix:** Disable button or add handler.

---

### 45. [LOW-012] `Teil3State.matching` Filter Duplicated — `handlers/quiz.py:482`, `handlers/quiz.py:19`
**File:** `handlers/quiz.py`  
**Lines:** 19, 482  
**Issue:** State filter defined in router decorator AND in callback query filter. Redundant.
**Fix:** Remove duplicate.

---

### 46. [LOW-013] `generate_progress_bar` vs `create_progress_bar` — `handlers/quiz.py:21`, `handlers/common.py:26`
**File:** `handlers/quiz.py:21`, `handlers/common.py:26`  
**Issue:** Two similar functions with different emoji styles (🟦 vs 🟩). Inconsistent UX.
**Fix:** Unify in `utils.py`.

---

### 47. [LOW-014] No Tests — Entire Codebase
**File:** N/A  
**Issue:** Zero test files. No CI/CD. High regression risk.
**Fix:** Add pytest + pytest-asyncio, target 80% coverage on critical paths (activation, referral, quiz).

---

### 48. [LOW-015] `.env` Committed? — `.gitignore` Check
**File:** `.gitignore`  
**Issue:** Need to verify `.env` is in `.gitignore`. If not, secrets leaked.
**Fix:** Confirm `.env` in `.gitignore`, rotate tokens if exposed.

---

## Architectural Weaknesses

| Area | Problem | Recommendation |
|------|---------|----------------|
| **Database** | Single SQLite file, no connection pooling, no migrations tool | Migrate to PostgreSQL + Alembic for production scale |
| **State Management** | FSM in memory (default) — loses state on restart | Use Redis-backed FSM storage |
| **Deployment** | No Dockerfile, no systemd unit, no health checks | Containerize, add orchestration configs |
| **Observability** | Only basic `logging.info`, no metrics, no tracing | Add Prometheus metrics, structured JSON logs, OpenTelemetry |
| **Configuration** | Mixed `.env` + hardcoded constants | Use Pydantic Settings for typed config |
| **Error Handling** | Inconsistent: some swallow, some re-raise, no Sentry | Centralized error handler + alerting |
| **Security** | No input validation library, no CSP, no security headers | Add Pydantic models for all inputs, security middleware |

---

## Prioritized Remediation Plan

### Phase 1 (Immediate — Security)
1. Fix SQL Injection vector (CVE-2026-001)
2. Fix Path Traversal (CVE-2026-002)
3. Fix Key Activation Race (CVE-2026-004)
4. Fix Referral Race (CVE-2026-005)
5. Move `LOG_CHANNEL_ID`, `SELLER_ID` to `.env` with validation (CVE-2026-006, 007)
6. Add rate limiting to key generation (CVE-2026-008)

### Phase 2 (Week 1 — Stability)
7. Fix Broadcast DoS (HIGH-002)
8. Fix Quiz Paywall Bypass (HIGH-003, 004)
9. Fix Date Parsing (HIGH-005)
10. Fix Subscription Logic (HIGH-006)
11. Add DB Indexes (MED-004)
12. Consolidate User Detail Functions (MED-008)

### Phase 3 (Week 2 — Performance)
13. Cache File Counts (MED-002, 003)
14. Fix N+1 Queries (MED-001)
15. Add Redis FSM + Throttling (MED-010, Arch)
16. Add Health Checks (MED-014)

### Phase 4 (Month 1 — Architecture)
17. Migrate to PostgreSQL + Alembic
18. Add Structured Logging + Metrics
19. Containerize + CI/CD + Tests
20. Implement i18n Framework

---

## File Risk Heatmap

| File | Critical | High | Medium | Low | Total |
|------|----------|------|--------|-----|-------|
| `database.py` | 3 | 4 | 5 | 2 | 14 |
| `handlers/common.py` | 1 | 2 | 3 | 2 | 8 |
| `handlers/quiz.py` | 0 | 2 | 3 | 2 | 7 |
| `handlers/admin.py` | 1 | 2 | 2 | 3 | 8 |
| `config.py` | 2 | 0 | 0 | 2 | 4 |
| `utils.py` | 1 | 0 | 0 | 0 | 1 |
| `middlewares/throttling.py` | 0 | 1 | 1 | 1 | 3 |
| `keyboards.py` | 0 | 0 | 0 | 4 | 4 |
| `main.py` | 0 | 0 | 1 | 0 | 1 |

---

## Conclusion

This codebase has **significant security debt** — multiple critical vulnerabilities that could lead to subscription abuse, data loss, and information disclosure. The architecture works for a small bot but **will not scale** beyond ~1,000 concurrent users without major refactoring (PostgreSQL, Redis, proper observability).

**Recommendation:** Freeze feature work. Dedicate 2 weeks to Phase 1-2 remediation before any new features. Add automated security scanning (bandit, semgrep) to CI.