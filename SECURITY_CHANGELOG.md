# SECURITY CHANGELOG

## Review 4 - Final Polish
Final remediation phase for review_3.md findings F-01 to F-12.

- [F-01] `utils.py` (`get_content`, `get_catalog`): all synchronous filesystem calls (`_data_max_mtime`, `find_directory_case_insensitive`, `file_path.is_file`, `json.load`) run via `await asyncio.to_thread(...)`. `handlers/common.py`: both `os.path.exists(pdf_path)` checks (schreiben/sprechen) wrapped in `await asyncio.to_thread(os.path.exists, pdf_path)`. Sync helpers (`_data_max_mtime`, `_build_catalog_sync`, `find_directory_case_insensitive`) never run on the event loop directly.
- [F-02] `database.py`: added `_db_connect()` async context manager that executes `PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000; PRAGMA synchronous=NORMAL;` on every connection. `init_db()` uses `_db_connect()` plus explicit all-three PRAGMAs. All `aiosqlite.connect(DB_NAME)` call sites in `database.py` migrated to `_db_connect()`. `handlers/admin.py` direct DB reads also migrated to `_db_connect()` (imported from `database`).
- [F-03] `utils.py`: global `FILENAME_MAP` dict + `get_short_hash()` (`hashlib.md5(filename.encode()).hexdigest()[:10]`) + `map_filename()` / `resolve_filename()`. `handlers/common.py`: file-list and group keyboards use `callback_data=f"read_{skill}_{teil}_{map_filename(file_safe)}"` (short hash, always <= 64 bytes). `handlers/quiz.py::handle_read_text`: resolves `file_token` via `resolve_filename()`, replies with expiry notice if unknown, then `os.path.basename()`-sanitizes before use.
- [F-04] `import html` in `handlers/common.py`, `handlers/quiz.py`, `handlers/admin.py`. `html.escape()` applied to all untrusted interpolations into `parse_mode="HTML"` strings: `full_name`/`username` in `/start` log and activation log (`common.py`); subscription-type fallback in `user_progress`; `title`/`summary`/question text/`fixed_question`/option keys+texts/keywords/feedback/`raw_pairs` Q-A-KW in `quiz.py` (`send_quiz_question`, `handle_answers`, `render_teil3_view`, `finish_teil3_matching`, skip-finish); user report fields, echoed inputs/keys, client names, users-page names in `admin.py` (`sup_stats`, `get_user_full_details` report, edit-text preview, revoke-key echo).
- [F-05] `handlers/common.py::cmd_start`: referral handling isolated in standalone `if command and command.args:` block; onboarding branched independently on `if is_new_user:` (log-channel record + full disclaimer + main menu) vs `else:` (short welcome + menu). New users via plain `/start` now always get disclaimer + logging regardless of `command.args`.
- [F-06] `database.py`: `logging.exception(...)` added to every swallowed-DB-error path (`activate_subscription`, `generate_new_key`, `set_user_referrer`, `process_referral_reward`, `claim_free_subscription`, `update_custom_text`); `IntegrityError` duplicate-referral kept as idempotent but logged via `logging.debug`.
- [F-07] `handlers/common.py` (`b1_parts_` and `t_group_` branches): per-file `get_content()` + `is_text_completed()` fetched concurrently via `asyncio.gather(*(_fetch_entry...))` instead of sequential awaits.
- [F-08] `MAX_CHUNK = 4000` chunking applied to `handlers/quiz.py::send_quiz_question` (splits `final_text`, sends head chunks via `answer`, tail via `safe_edit_message_text_or_send` with keyboard) and `handlers/admin.py::cb_sup_stats` (splits aggregated client list), mirroring `finish_teil3_matching`.
- [F-09] `database.py`: all `BEGIN IMMEDIATE` blocks (`activate_subscription`, `generate_new_key`, `set_user_referrer`, `process_referral_reward`, `claim_free_subscription`, `update_custom_text`) use strict `try...except...finally` with `committed` flag guaranteeing `await conn.rollback()` on any failure path; `activate_subscription` still re-raises after logging.
- [F-10] `main.py::global_error_handler`: explicitly re-raises `asyncio.CancelledError`, `KeyboardInterrupt`, `SystemExit` before any `return True`; only `TelegramRetryAfter` and app errors are swallowed/logged.
- [F-11] `config.py`: raises `ValueError("CRITICAL: SELLER_ID is required in .env")` immediately if `SELLER_ID` is empty; non-numeric values also rejected. Prevents silent invalid `tg://user?id=` paywall buttons.
- [F-12] `utils.py`: `_CONTENT_CACHE` key is composite `f"{skill.lower().strip()}_{teil.lower().strip()}_{safe_name}"` (i.e. `skill_teil_basename`), preventing cross-skill/teil basename collisions; invalidation still on tree mtime.

Verification: `python -m py_compile` passes on all modules.

## Runtime Crash Fixes — 2026-09-19
- **Files Modified:** `main.py`, `handlers/admin.py`, `SECURITY_CHANGELOG.md`
- **Fix 1 — Aiogram 3.x Error Handler Signature Mismatch (`main.py`):**
  - Imported `ErrorEvent` from `aiogram.types`.
  - Changed `global_error_handler(event, exception)` → `global_error_handler(event: ErrorEvent)` with `exception = event.exception` extracted inside.
  - Keeps existing logic: re-raise `CancelledError`/`KeyboardInterrupt`/`SystemExit`, `TelegramRetryAfter` warning + `return True`, otherwise `logging.exception` + `return True`.
  - Root cause: aiogram 3.x dispatches a single `ErrorEvent` object to `@dp.errors()` handlers; the old 2-arg signature raised `TypeError` at runtime.
- **Fix 2 — Unhandled 'Query is too old' (`handlers/admin.py::cb_revoke_key_inline`):**
  - `TelegramBadRequest` was already imported from `aiogram.exceptions`; wrapped every `await callback.answer(...)` in `cb_revoke_key_inline` with `try...except TelegramBadRequest: logging.debug("Ignored old callback query")`.
  - Covers stale inline buttons clicked after a bot restart (expired `callback_query.id`); uses `logging.debug` (not silent `pass`) to keep observability without cluttering error logs.
  - Rule 19 instant-refresh `edit_text` path already guarded against `message is not modified`; unchanged.
- **Verification:** `python -m py_compile` passes; mocked stale-query test (answer raising `TelegramBadRequest`) confirms no propagation.
