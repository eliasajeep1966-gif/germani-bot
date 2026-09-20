# Bot Architecture & Security Constitution (Senior DevSecOps Level)
You are a Senior DevSecOps and Backend Architect. Whenever you edit or generate code for this project, you MUST strictly adhere to the following rules. Any violation is considered a critical security failure.

## 1. Time & Dates (CRITICAL)
- NEVER use naive `datetime.now()` or `strptime` with multiple fragile formats.
- ALWAYS store dates as ISO 8601 UTC strings (`datetime.now(timezone.utc).isoformat()`).
- ALWAYS parse dates using `datetime.fromisoformat()`.
- When calculating subscription expiry, NEVER overwrite the existing date. ALWAYS use: `max(current_utc, current_expire) + timedelta(days)`.


## 2. Authorization & Access Control (AuthZ)
- NEVER place Admin or Supervisor logic inside `common.py` or any public router.
- ALL Admin/Supervisor commands MUST be in `admin.py` and protected by `AdminAuthMiddleware`.
- NEVER serve premium content (PDFs, Quiz parts > 1) without explicitly calling `can_access_level()`.
- Treat `callback_query.data` as UNTRUSTED user input. Validate all IDs and parameters before querying the database.

## 3. Database, SQL & Business Logic (CRITICAL)
- ALL financial transactions (key activation, referral rewards, settlement) MUST be wrapped in `BEGIN IMMEDIATE` SQLite transactions to prevent Race Conditions.
- Settlement (`sup_finance`) MUST be calculated based on `used_count` (sold keys), NOT just created keys.
- Prevent infinite loops/farming in referrals: verify referrer exists, enforce strict constraints, and grant rewards idempotently.
- NEVER use "fake migrations" (try/except ALTER TABLE). Schema changes must be explicit and safe.
- **SQL Injection Prevention:** NEVER use f-strings or string formatting for SQL queries. ALWAYS use parameterized queries (`?`). If dynamic names (like PRAGMA table_info) are unavoidable, validate them strictly against a hardcoded allowlist BEFORE execution.
- **Concurrency & TOCTOU:** ALL financial transactions (key activation, referral rewards) MUST be atomic. Do NOT use check-then-act `SELECT` followed by `UPDATE`. Use `UPDATE ... RETURNING` (SQLite 3.35+) or wrap everything in a single `BEGIN IMMEDIATE` transaction using the SAME connection.
- **Data Retention:** NEVER use hard deletes (`DELETE FROM users`). ALWAYS implement soft deletes (e.g., `deleted_at` column) to prevent orphaned records and data loss.
- **Idempotency:** Prevent infinite loops in referrals. Verify referrer exists, enforce strict constraints, and grant rewards idempotently in the same transaction.


## 4. Error Handling & Graceful Shutdown
- NEVER use silent `return` or `pass` in exception blocks. Do not swallow errors.
- If a user is unauthorized, explicitly answer the callback with an alert.
- Implement Graceful Shutdown: Ensure database connections are safely closed on `SIGINT`/`SIGTERM` to prevent corruption.

## 5. State Management & Pagination
- Do not wipe the FSM state (`state.clear()`) if a user hits a paywall mid-quiz; preserve their progress.
- Move towards persistent state (e.g., Redis) instead of RAM-only FSM to prevent data loss on restarts.
- NEVER dump large datasets (like `/users`) in a single message. ALWAYS use Pagination to prevent PII leaks and UI breakage.

## 6. Resource Exhaustion (Anti-DoS) & Async Performance
- NEVER use unbounded in-memory data structures (like dictionaries) for state or throttling. Use TTL/Eviction.
- NEVER use blocking I/O operations (heavy `sqlite3` queries or reading large JSON files) that block the main event loop. 
- Cache frequently accessed JSON files in memory instead of reading them from disk on every callback.

## 7. Secrets & Configuration
- NEVER hardcode sensitive data (API Tokens, Admin IDs, Payment IDs) directly in the code.
- ALWAYS read them from environment variables (`.env`) or a dedicated config file.

## 8. Middleware & Memory Management (Anti-Leak)
- NEVER use standard Python `dict` for throttling or caching user requests. 
- ALWAYS use `cachetools.TTLCache` or implement an explicit eviction mechanism (TTL) to prevent Out-Of-Memory (OOM) crashes.

## 9. State Transitions & Client Trust
- NEVER assume a user follows a linear flow in the FSM (e.g., Quiz). 
- Users can click old inline buttons (like `prev_question`). ALWAYS re-verify `can_access_level` on ANY state transition, forward or backward.

## 10. Admin UX & Mass Actions (Broadcast)
- NEVER execute mass actions (like `/broadcast` to all users) immediately.
- ALWAYS implement a 2-step verification: 1. Preview the message -> 2. Require explicit InlineButton confirmation (Send / Cancel).
## 11. Audit & Changelog (MANDATORY)
## 11. Audit & Changelog (MANDATORY)
- For security fixes and refactoring, log details in `SECURITY_CHANGELOG.md`.
- For new features and UI enhancements, log details in `FEATURE_CHANGELOG.md`.
- Format: Date, Files Modified, Feature/Fix Description, Technical Details.


## 12. Input Validation & Path Security
- **Path Traversal Prevention:** NEVER rely on `os.path.basename` or `os.path.commonpath` for security boundaries (they are bypassable). ALWAYS use `pathlib.Path.resolve().is_relative_to(base_path)` for strict directory containment.
- Validate all IDs (like `SELLER_ID` or `LOG_CHANNEL_ID`) as integers at startup. Fail fast if invalid.
## 13. State Management & Anti-DoS
- **FSM State Integrity:** NEVER leave stale state in the FSM. If a user hits a paywall, explicitly clear the state or stash it. Do not allow them to remain in `QuizState.answering`.
- **Stateless Callbacks:** NEVER trust client-side callback data for navigation (e.g., `prev_question`). Always validate the *target* index against the user's subscription, not just the current index.
- **Resource Exhaustion:** NEVER use unbounded `asyncio.create_task()` for bulk operations like broadcasting. ALWAYS use `asyncio.Semaphore` to limit concurrency and respect Telegram's rate limits (e.g., 30 msg/s).

## 14.Determinism
- **Strict SKU Mapping:** Subscription checks (`can_access_level`) MUST explicitly map SKUs to capabilities. Do not blindly group SKUs (like `intensive` or `group`) without verifying their actual target level (B1 vs B2).
- **State Determinism:** NEVER mutate shared state or cached lists in-place (e.g., `random.shuffle(cached_list)`). ALWAYS copy data before mutating to ensure deterministic behavior for the user's session.
## 15. Performance & Async Safety
- **No Blocking I/O:** NEVER use `os.walk`, `os.listdir`, or read files from disk inside request handlers. File structures and static JSON content MUST be cached in memory at startup.
- **N+1 Query Problem:** NEVER execute database queries inside a loop (e.g., fetching user details one by one). ALWAYS use SQL `JOIN` or batch fetching (`WHERE id IN (...)`).
- **Async-Safe Structures:** NEVER use thread-safe-only caches (like `cachetools.TTLCache`) in async contexts. Use async-safe alternatives or Redis to prevent state corruption.
- **Pagination:** NEVER fetch or display unbounded lists of database records (e.g., listing all users). ALWAYS implement SQL-level pagination (`LIMIT`, `OFFSET`) and UI pagination (Next/Prev buttons) to prevent Telegram message length errors and memory bloat.
- **Async Performance:** NEVER use blocking I/O operations (e.g., `sqlite3.connect()`) that block the main event loop. ALWAYS use non-blocking `aiosqlite` or `asyncio.create_task()` for database operations.

## 16. Telegram API Limits
- `callback_data` MUST NEVER exceed 64 bytes. Never embed raw filenames in callbacks. Use short hashes or IDs mapped to a server-side dictionary.
- Messages MUST NEVER exceed 4096 characters. Always chunk long outputs (like reports or quiz results) using a `MAX_CHUNK = 4000` loop.

## 8. HTML Injection Prevention
- ANY untrusted user input (`full_name`, `username`, `title`) injected into a `parse_mode="HTML"` message MUST be escaped using `html.escape()`.

## 17. Strict Non-Blocking I/O
- NEVER use synchronous filesystem calls (`os.listdir`, `os.path.exists`, `open()`) directly in the async event loop. Wrap them in `await asyncio.to_thread(...)`.

## 18. SQLite Concurrency
- The database MUST be initialized with `PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000;` to prevent `database is locked` errors under concurrent load.
## 19. Destructive Inline Actions (UX & Safety)
- ANY inline button that performs a state-changing or destructive action (e.g., Revoke, Delete, Ban) MUST immediately update the message UI upon success (e.g., remove the button, strike-through the text, or update the status) to prevent double-clicks and user confusion.
- ALWAYS use `await callback.answer("Success message")` to provide immediate tactile feedback.

## 20. Keyboard Pagination Limits
- Inline keyboards displaying dynamic lists (like users or keys) MUST be strictly paginated to a maximum of 10 items per page. This ensures clean UX on mobile devices and prevents hitting Telegram's maximum button limits.
- Callback data for these items must be carefully constructed to stay under the 64-byte limit (e.g., `rev_key_<short_id>`).
