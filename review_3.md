# review_3 — ZERO-TRUST Final Audit (Lead Security / Performance / QA)

**Scope:** ENTIRE codebase (`main.py`, `config.py`, `database.py`, `utils.py`, `keyboards.py`, `handlers/common.py`, `handlers/quiz.py`, `handlers/admin.py`, `middlewares/throttling.py`, `requirements.txt`, `data/b1/**`).
**Method:** line-by-line read of every file above + compile check (`py_compile` OK) + static scans (await usage, blocking-call scan, callback-length measurement, HTML-escape grep, branch-indent analysis) + sample JSON inspection.
**Explicitly IGNORED (per instructions, not re-reported):** untracked `asyncio` tasks, TTLCache async safety, hardcoded texts/prices, missing tests, missing type hints, inconsistent language, `.env`/gitignore check.

# VERDICT: FAIL

Real, non-accepted defects were found (blocking I/O, crash/DoS paths, silent error swallowing, HTML injection, broken onboarding branch, callback overflow, missing guards). Details below with exact file + line + fix. The code is **not** bulletproof.

---

## F-01 — FAIL — Blocking I/O on the async event loop (`utils.get_content`)
- **File/line:** `utils.py:125` (`max_mtime = _data_max_mtime()` inside `async def get_content`), `utils.py:132-134` (sync `find_directory_case_insensitive(...)` call), `utils.py:189` (`os.listdir` inside that helper), `utils.py:22,26,31` (`os.path.getmtime`/`iterdir` in `_data_max_mtime`).
- **What:** `get_catalog()` correctly offloads all `os.listdir`/`getmtime` via `asyncio.to_thread`, but `get_content()` — called on **every** question render/answer/skip/prev (`handlers/quiz.py`, `handlers/common.py`) — runs `_data_max_mtime()` (dir `exists` + `getmtime` + `iterdir` + per-subdir `getmtime`) **synchronously** on the loop, then runs `find_directory_case_insensitive()` (sync `os.listdir`, line 189) synchronously. Only the final `json.load` is in `to_thread` (line 151). Under concurrent quiz traffic this blocks the loop on filesystem syscalls.
- **Also blocking:** `handlers/common.py:386,426` — `os.path.exists(pdf_path)` synchronously inside async callback before `send_document`.
- **Fix:**
  ```python
  # utils.py — get_content
  max_mtime = await asyncio.to_thread(_data_max_mtime)
  ...
  target_dir = await asyncio.to_thread(find_directory_case_insensitive, f"data/b1/{skill.lower().strip()}", teil.lower().strip())
  ```
  and in `handlers/common.py` replace `os.path.exists` with `await asyncio.to_thread(os.path.exists, pdf_path)` (or pre-resolve at startup).

## F-02 — FAIL — SQLite will lock under concurrent load (no WAL / busy_timeout); will NOT survive 1000 concurrent users
- **File/line:** `database.py:88-92` (`init_db()` — no `PRAGMA`), every writer (`record_answer_stat:289`, `activate_subscription:487`, `claim_free_subscription:664`, `mark_text_completed:421`, etc. open a **new** `aiosqlite.connect(DB_NAME)` per call).
- **What:** No `PRAGMA journal_mode=WAL`, no `PRAGMA busy_timeout`, no retry. Default rollback-journal + `BEGIN IMMEDIATE` writers (`activate_subscription`, `claim_free_subscription`, `generate_new_key`, `set_user_referrer`) contend with per-answer writers (`record_answer_stat` fires on **every** quiz answer). Concurrent `BEGIN IMMEDIATE` + writes → `sqlite3.OperationalError: database is locked` once the default timeout is exceeded. Readers block behind writers without WAL. This is a load-bearing scalability defect.
- **Fix:**
  ```python
  async with aiosqlite.connect(DB_NAME) as conn:
      await conn.execute("PRAGMA journal_mode=WAL;")
      await conn.execute("PRAGMA busy_timeout=5000;")
      await conn.execute("PRAGMA synchronous=NORMAL;")
  ```
  at startup (and/or per connection), plus retry-with-backoff on `OperationalError: database is locked` for write transactions, or a single write-queue.

## F-03 — FAIL — Telegram 64-byte `callback_data` overflow breaks a real file list (fatal UX/DoS)
- **File/line:** buttons built at `handlers/common.py:523` (`callback_data=f"read_{skill}_{teil}_{file_safe}"`) and `:570` (same in `t_group_` branch).
- **What:** Measured all 106 JSON files: 1 file exceeds the Bot API 64-byte `callback_data` limit — `read_lesen_teil2_text34_<arabic>.json` = **67 bytes UTF-8**. Telegram rejects the **entire** message (`BUTTON_DATA_INVALID`), and `safe_edit_message_text`'s fallback re-sends the same invalid keyboard → also fails → user sees nothing and that teil/group becomes unbrowsable. Other Arabic filenames are close to the limit, so any rename/addition re-triggers it.
- **Fix:** stop embedding raw filenames in `callback_data`. Use a short index/token, e.g. `read_{skill}_{teil}_{idx}` with server-side lookup in the catalog, or `hashlib.sha1(file.encode()).hexdigest()[:16]` + map. Keep raw filename only in FSM state (already `basename`-sanitized).

## F-04 — FAIL — HTML injection / parse-failure DoS (zero `html.escape` in the repo)
- **File/line:** no `html.escape`/`quote_html` anywhere (grep over `handlers/common.py`, `handlers/quiz.py`, `handlers/admin.py` = 0 hits). Untrusted `full_name`/`username` interpolated into `parse_mode="HTML"` messages at e.g. `handlers/common.py:74-79` (new-user log), `:168-176` (activation log), `handlers/admin.py:249-264` (user report), `:440-447` (`sup_stats` client list), `:749,801` (users pages); JSON `title`/custom texts also interpolated (`handlers/quiz.py:831-834`, `handlers/common.py:383-384,424`).
- **What:** A user whose Telegram name contains `<`, `&`, or tags (e.g. `<b>`, `<code>`) breaks HTML parsing → `TelegramBadRequest: can't parse entities` → log/report sends fail; an admin opening that user's report or users-page gets an error instead of data (selective admin-view DoS + log loss).
- **Fix:** `import html` and wrap every untrusted interpolation: `html.escape(str(full_name or ""))`, same for username/title/custom text where parsed as HTML. Or send with `parse_mode=None`.

## F-05 — FAIL — `/start` onboarding branch broken: new users without args miss disclaimer + logging
- **File/line:** `handlers/common.py:59-123`. `if command and command.args:` (line 59) wraps everything through line 116; `else:` (line 117) is the no-args path.
- **What:** The new-user log block (`71-84`) **and** the full disclaimer + main menu (`86-116`) only execute when start-args exist. A new user joining via plain `/start` falls into `else` and gets the short "أهلاً بك مجدداً!" with **no** disclaimer and **no** `LOG_CHANNEL_ID` record. Organic joins are invisible to admins and miss the legal/disclaimer text. (Returning users with args do get the welcome; the defect is the args-gating itself.)
- **Fix:** dedent: handle referral inside `if args:`, then branch on `is_new_user` independently:
  ```python
  if command and command.args:
      ... referral only ...
  if is_new_user:
      ... log + disclaimer + menu ...
  else:
      ... short welcome + menu ...
  ```

## F-06 — FAIL — Critical DB errors swallowed silently (no logging, caller can't tell)
- **File/line:** `database.py:579-588` (`set_user_referrer` — `except IntegrityError: rollback/pass`, `except Exception: rollback/pass`, returns `None`), `:644-649` (`process_referral_reward` — `except Exception: rollback/pass; return None,0`), `:701-706` (`claim_free_subscription` — `except Exception: rollback/pass; return False,"..."`), `:731-736` (`update_custom_text` — `except Exception: rollback/pass; return False`).
- **What:** Disk-full, corruption, `database is locked`, permission errors are all rolled back and **never logged**. Callers (`cmd_start`, admin edit flow) see only "no referrer" / generic failure. Root cause is invisible in logs — violates "never swallow critical errors silently."
- **Fix:** add `logging.exception(...)` (with user/key context, never secrets) in each `except Exception` before returning the safe fallback. Keep `IntegrityError`-duplicate as expected-idempotent but still `logging.debug`.

## F-07 — FAIL — N+1 sequential I/O per file-list open (latency multiplier under load)
- **File/line:** `handlers/common.py:507-524` (`b1_parts_` list) and `:555-570` (`t_group_` list): per file → `await get_content(...)` + `await is_text_completed(...)` **sequentially** (up to 15 files ⇒ ~30 sequential awaits per menu open).
- **What:** Prior reviews fixed admin-side N+1 (F12/F13) but the hot user-facing path still fans out sequentially. Under load this multiplies DB + FS pressure per click.
- **Fix:** batch completion with one query (`SELECT text_id FROM completed_texts WHERE user_id=? AND text_id IN (...)`) and load titles with `asyncio.gather(*[get_content(...)])` (bounded semaphore), or precompute titles in the catalog.

## F-08 — FAIL — Missing Telegram 4096-char guard everywhere except Teil3 finish
- **File/line:** `handlers/quiz.py:739-836` (`send_quiz_question` — concatenates feedback + progress + title + summary + question + options + full option texts, single `edit_text`/`answer`, no split); `handlers/admin.py:440-452` (`sup_stats` loops unbounded `activated_users` into one message).
- **What:** Only `finish_teil3_matching` chunks (F16, `quiz.py:731-737`). A long question/options set or a supervisor with many clients exceeds `MESSAGE_TOO_LONG` → `TelegramBadRequest` → quiz stuck with no question, stats command dies. Long-data JSON makes this deterministic.
- **Fix:** apply the same `MAX_CHUNK = 4000` splitter used in `finish_teil3_matching` to `send_quiz_question` output and to `sup_stats` (or paginate client list, 20/page).

## F-09 — FAIL — `activate_subscription` has `BEGIN IMMEDIATE` with no `try/rollback` (lock amplifier)
- **File/line:** `database.py:487-543` (compare `generate_new_key:547-561` which **does** `try/commit/except/rollback/raise`).
- **What:** Any exception after line 488 (SELECT fail, `_parse_iso...`, INSERT conflict, `_process_referral_reward_conn` error) propagates with the `RESERVED` lock held until connection close. Under concurrency this widens the lock window and turns single failures into `database is locked` cascades. The F17 comment ("let it raise") is fine for visibility, but it must still `rollback` first.
- **Fix:** mirror `generate_new_key`:
  ```python
  try:
      await conn.execute('BEGIN IMMEDIATE')
      ... claim + insert + referral ...
      await conn.commit()
  except Exception:
      try: await conn.rollback()
      except Exception: pass
      raise
  ```

## F-10 — FAIL — Global error handler suppresses cancellation (shutdown-hang risk)
- **File/line:** `main.py:38-45` (`@dp.errors() ... return True` for **every** exception type).
- **What:** Returning `True` for `asyncio.CancelledError` / `KeyboardInterrupt` / `SystemExit` tells aiogram the error is "handled," suppressing task cancellation during shutdown/restart. Correct for `TelegramRetryAfter`/app errors; wrong for cancellation.
- **Fix:**
  ```python
  import asyncio
  if isinstance(exception, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
      raise exception
  ```

## F-11 — FAIL — Empty `SELLER_ID` silently builds invalid paywall buttons
- **File/line:** `config.py:35-38` (empty string allowed when env missing; only validates non-empty), consumed at `handlers/quiz.py:162,240,376,428,511,630` (`url=f"tg://user?id={SELLER_ID}"`).
- **What:** With `SELLER_ID` unset, every paywall message carries `url="tg://user?id="` → `BUTTON_URL_INVALID` on send → users hitting the paywall see nothing and can't contact the seller. Fail-silent misconfiguration on the money path.
- **Fix:** fail fast in `config.py` (`if not _raw_seller: raise ValueError(...)`) or, if anonymous purchase is intended, omit the URL button when empty and show a text fallback.

## F-12 — FAIL (latent) — Content cache keyed by basename only + unbounded
- **File/line:** `utils.py:130-154` (`safe_name = os.path.basename(file_name)`; `_CONTENT_CACHE[safe_name] = parsed`; invalidation only on tree mtime).
- **What:** Two different `skill/teil` files sharing a basename would collide and serve each other's content (wrong quiz). Cache also has no size/TTL eviction — grows with every distinct file ever opened. Currently 106 files / 106 unique basenames (measured — 0 active collisions), so latent, not yet firing.
- **Fix:** key by `(skill.lower(), teil.lower(), safe_name)` and bound with `LRUCache(maxsize=...)` or TTL.

---

## What was checked and PASSES (no new finding beyond accepted risks)
- **AuthZ central gate:** `AdminAuthMiddleware` (`handlers/admin.py:66-99`) + per-handler `ADMIN_IDS` defense-in-depth; supervisor-vs-admin separation on gen/stats/finance/toggle/delete/settle all re-checked — no bypass found. Public `common_router` carries no admin keygen/lookup (note at `common.py:200-202` holds).
- **SQLi:** all user-influenced SQL is parameterized (`?`); only f-string is `PRAGMA table_info({table_name})` over hardcoded allowlist keys (`database.py:180-184`) — not injectable. `LIKE ?` pattern (`get_skill_progress:450`) is parameterized.
- **Path traversal:** `basename` + `resolve` + `is_relative_to(BASE_DIR/data)` in `utils.py:130-143,175-196`, plus `basename` on all `skill/teil/file` from `callback.data`/FSM in `common.py:362,457-458,542-543` and `quiz.py:147-149,331-333,436-438,559-561,697-699` — traversal neutralized.
- **Referral/key atomicity:** key claim is single `UPDATE...RETURNING` (`database.py:491-500`), referral claim is single `UPDATE...RETURNING` (`:596-601`), both inside `BEGIN IMMEDIATE`; self-referral rejected twice (`:567-568`, `:610-611`); duplicate `referred_id` is `UNIQUE` + idempotent swallow — races addressed.
- **Missing `await` / bad imports / undefined vars:** full await-scan found zero missing awaits on DB/content/ratelimit paths; `py_compile` passes on all modules; no unresolved imports or undefined names on the exercised paths.
- **Per-recipient broadcast isolation + 429 retry + semaphore-30** (`admin.py:820-861`) and 2-step broadcast confirm (`:863-938`) hold; group-key `max_uses=4` accounting and `is_settled` math verified.

## Re-test checklist (for the fixer)
1. `py_compile` all modules; boot with `SELLER_ID` empty → must fail fast (F-11).
2. Plain `/start` as new user → disclaimer + log-channel record (F-05); with `ref_` link as returning user → menu appears.
3. User with name `<b>x & y</b>` → logs/reports/users-pages still send (F-04).
4. Open `lesen/teil2` file list → all groups render (F-03); answer/skip/prev under load shows no loop-blocking (F-01).
5. Kill `-INT` during polling → clean shutdown, no hang (F-10); `database is locked` never surfaces at 50+ concurrent answer writers (F-02/F-09).
6. Force DB error (e.g. read-only DB file) on referral/claim/edit-text → error **logged** (F-06).
7. Supervisor with 200+ clients → `/admin` stats paginated/chunked, no `MESSAGE_TOO_LONG` (F-08).
