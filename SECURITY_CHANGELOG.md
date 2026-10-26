# SECURITY CHANGELOG

## 2026-10-11 — P0 Launch-Blockers (report_1.md §2.2, §3.1)
- **Files Modified:**
  - `handlers/quiz.py` — `handle_view_full_text`: free-check now uses the actual `current_index` from FSM data instead of hardcoded `question_index=0`.
  - `database.py` — per-call `aiosqlite.connect()` replaced with a bounded persistent connection pool (`_POOL_SIZE = 10`) + serialized write checkout (`_db_write()` via `_WRITE_LOCK`); `PRAGMA journal_mode=WAL` moved to one-time pool creation; added `close_db()`; all 8 `BEGIN IMMEDIATE` writers switched to `_db_write()`; `get_user_details` restructured to avoid nested checkout; `_db_connect()` API unchanged (pool-backed, with rollback hygiene + broken-connection replacement).
  - `main.py` — imports `close_db()` and calls it in the shutdown `finally` block before `bot.session.close()`.
  - `SECURITY_CHANGELOG.md` — this entry.
- **Fix Description:**
  - Premium Text Leak (HIGH): users standing on paid `hören` questions (index 6+) passed the `question_index=0` free test and received the full premium passage via `view_full_text`. The gate now uses the same `current_index` semantics as `handle_answers` / `handle_skip_question` / `handle_prev_question`.
  - Database Lockup / Thread Explosion (CRITICAL): every DB helper opened its own `aiosqlite.connect()` (one OS thread each) with zero serialization, guaranteeing thread explosion and mass `SQLITE_BUSY` under load. A single shared connection was deliberately rejected (multi-statement `BEGIN IMMEDIATE...COMMIT` transactions with interleaved awaits would corrupt the atomic key/referral claims). The pool bounds threads to 10 while the write lock queues writers in asyncio instead of failing.
- **Technical Details:**
  - `handle_view_full_text` coerces `current_index` defensively (`int(...) or 0`, `TypeError/ValueError` → 0, clamp negatives) so stale/tampered FSM data can never bypass the gate or crash it.
  - Pool design: `asyncio.Queue` of 10 persistent connections; `_ensure_pool()` double-checked lazy init (safe for tests/offline scripts); `_db_connect()` rolls back leaked transactions on check-in and replaces connections that raised `aiosqlite.Error`; `_db_write()` = `_WRITE_LOCK` + `_db_connect()`; explicit `BEGIN IMMEDIATE` statements kept as defense-in-depth.
  - `get_user_details` no longer holds a checkout while calling `get_user_subscription` / `get_referral_info` (hold-and-wait on a bounded pool would deadlock under fan-out); admin.py's raw-SQL blocks were audited for the same hazard (none found).
  - Verification (isolated temp DB, real `data/b1` catalog): `init_db` builds pool=10; `is_free_content(...,0)=True` vs `(...,6)=False` on first `hören` file; key generate→activate→double-spend-rejected; 300-task mixed read/write stress with 0 errors / 0 `SQLITE_BUSY` and threads 12→12 (bounded); pool fully checked back in; `close_db` + lazy re-init OK; `py_compile` OK on all touched files.

## 2026-10-11 — P0 Hotfixes batch 2 (report_1.md §1.1, §1.4/§2.1, §2.4)
- **Files Modified:**
  - `main.py` — added `_startup_call()` retry-with-backoff helper (3 attempts, 1s/2s/4s); `set_my_commands` (default scope) and `delete_webhook` now retried instead of crashing `main()`; per-admin command failures stay non-fatal (`fatal=False`, expected when an admin never started the bot).
  - `handlers/common.py` — added `_stash_pending_quiz()` / `_clear_pending_quiz()` (pending_* is now the SOLE resume source); `process_key` honors `/cancel` (+`/cancel@bot` variants); success path restores the exact stashed text (standard quiz → `QuizState.answering` + re-rendered question; Teil3 → `Teil3State.matching` + re-rendered board) with catalog validation and single-use stash consumption; `start_subscribe_flow` snapshots live quiz pointers (or drops stale stash); referrer congrats now fires on even totals only (matches recurring economy).
  - `handlers/quiz.py` — `handle_read_text` paywall stashes the clicked text (skill/teil/file @0) BEFORE `state.clear()` + `AuthState` entry; top-level import of the stash helper (no cycle: common never imports quiz at module level; common→quiz is function-local).
  - `database.py` — `_process_referral_reward_conn`: peek + self/existence validation moved BEFORE the `UPDATE...RETURNING` flip (invalid referrers no longer burn the row; unclaimed rows stay retryable); reward changed from one-time `counted_refs == 2` to recurring `counted_refs > 0 and counted_refs % 2 == 0` (credit on 2nd, 4th, 6th, ...); atomic flip kept as the concurrency guard (racers serialize on `_db_write` + `BEGIN IMMEDIATE`, exactly one wins).
  - `SECURITY_CHANGELOG.md` — this entry.
- **Fix Description:**
  - Startup Crash (HIGH): any Telegram outage during `set_my_commands`/`delete_webhook` aborted boot before polling. Retried with backoff; persistent failure degrades (logs CRITICAL, continues to polling) instead of crashing.
  - FSM Prison (HIGH): `/cancel` typed in `AuthState` was consumed as a key attempt forever; paywall entry wiped progress with no resume; resume could land on the wrong text. Cancel now exits; every AuthState entry stashes context; activation restores and re-renders the exact position (incl. full Teil3 matching progress); any navigation (`main_menu`, lists) clears the single-use stash so nothing phantom-resumes.
  - Referral Economy Flaw (MEDIUM→business-critical): one-time bonus contradicted the "every 2 invites" promise, and ghost referrers burned rows. Recurring threshold + validate-before-flip.
- **Technical Details:**
  - `TelegramRetryAfter` honors the server's `retry_after`; all other failures (covers `TelegramAPIError`/network) use exponential backoff; lambdas capture per-admin IDs correctly (`lambda admin_id=admin_id:`).
  - Stash keys: `pending_skill/_teil/_file/_index/_correct/_wrong/_order/_teil3/_t3`; Teil3 snapshot copies the 11 matching keys; resume validates via `get_content` probe (deleted file → clean slate + notice, activation still succeeds); `selected_*` reset on restore for a clean board.
  - Referral exactly-once preserved: second `process_referral_reward` call post-activation (common.py) now peeks `is_counted=1` → `(None, 0)`; 5-way concurrent claim test yields exactly one winner.
  - Verification (isolated temp DBs, real catalog): retry helper recovers after 2 transient fails and returns False (never raises) on persistent outage; 4 invites → 2 credits; ghost-referrer row stays `is_counted=0` then claims once the referrer appears; `/cancel` + `/cancel@bot` clear AuthState; quiz resume restores file/index/counters + re-renders; Teil3 resume restores board; fresh activation has no phantom resume; wrong key keeps AuthState; entry paywall wipes live-A and stashes B@0; `py_compile` + import-cycle check OK.

## 2026-10-11 — P1 Performance & Anti-DoS (report_1.md §3.1–§3.4)
- **Files Modified:**
  - `database.py` — `get_next_uncompleted_target`: single `SELECT text_id ... WHERE user_id=?` into a Python `set`, then in-memory set-difference scan (was: one pooled-checkout round-trip per file, up to ~159 sequential DB hits per `user_progress` render).
  - `utils.py` — `_CONTENT_CACHE` + `FILENAME_MAP` changed from unbounded dicts to `TTLCache(maxsize=1000, ttl=3600)` (cachetools, already a dependency); mtime wipe uses `.clear()` (no rebinding); `Path.resolve()` pair moved into `asyncio.to_thread`; fixed `_ALLOWED_DATA_DIR` resolved once at import.
  - `handlers/common.py` — new `_load_guide_pdf()` (m-time-validated, 2-fixed-keys-bounded bytes cache; disk read in worker thread; returns `BufferedInputFile`); Schreiben/Sprechen now upload from cached bytes while `send_document` stays on-loop (network I/O is async and must never move to a thread); `FSInputFile` import removed (zero remaining refs repo-wide).
  - `handlers/admin.py` — broadcast rewrite: `_run_broadcast_task(bot, admin_chat_id, message_id)` streams recipients via keyset pagination (`WHERE user_id > ? ORDER BY user_id LIMIT 500`, one short checkout per chunk, never held across the broadcast); semaphore acquired ONLY around each `copy_message` (released before 429-sleep, re-acquired for the single retry); `_BROADCAST_RUNNING` single-flight flag (atomic check-then-set on the loop, always reset in `finally`); `cb_broadcast_confirm` no longer `fetchall()`s and refuses a second concurrent broadcast with a notice.
  - `SECURITY_CHANGELOG.md` — this entry.
- **Fix Description:**
  - N+1 Query (HIGH): one progress render cost up to ~159 sequential DB round-trips; now exactly 1.
  - Blocking I/O (MEDIUM): `Path.resolve()` stat syscalls and ~0.5 MB PDF disk reads no longer block the event loop; uploads remain async on-loop by design.
  - Unbounded Caches (MEDIUM): content/file-hash maps now TTL-bounded (1000 entries / 1h) instead of growing forever.
  - Broadcast RAM & Stall (MEDIUM): RAM flat at ~500 ids regardless of user count (was: full `fetchall()`); 429 waves no longer freeze all 30 send slots; parallel confirms can no longer stack duplicate broadcasts.
- **Technical Details:**
  - TTLCache check-then-set is atomic on the single-threaded loop where no `await` intervenes (`map_filename`/`resolve_filename`); `get_content`'s check-then-set spans awaits, so concurrent misses may parse twice (benign duplicate work, last-write-wins — documented, no singleflight added by design).
  - Keyset pagination is stable under concurrent signups (unlike OFFSET); per-chunk checkouts keep pool pressure to 1 slot at a time.
  - Verification (isolated temp DB, real catalog): caches are TTLCache(1000/3600) with map/resolve roundtrip; `get_content` works through the new path; N+1 rewrite matches old behavior on empty/partial/full completion (incl. all-159 fallback); PDF loader returns None on missing and cached `BufferedInputFile` otherwise; broadcast task lazy-copies 7/7 seeded users, sends the report, resets the flag; second confirm refused while running; real-DB `init_db`/`close_db` OK; `py_compile` clean on all touched files.
