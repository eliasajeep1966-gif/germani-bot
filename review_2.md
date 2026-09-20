# Final Zero-Trust Audit — review_2 (2026-09-17)

**Scope:** Entire codebase (`config.py`, `database.py`, `utils.py`, `main.py`, `keyboards.py`, `handlers/admin.py`, `handlers/common.py`, `handlers/quiz.py`, `middlewares/throttling.py`, `data/b1/*` layout).
**Method:** Full static read of every shipped `.py` file + targeted runtime probes (AST parse, blocking-I/O scan, cache-key/collision analysis over 106 JSON files, import/await cross-check).
**Excluded (per brief, NOT re-reported):** untracked `asyncio` tasks, `TTLCache` async-safety, hardcoded texts/prices, missing tests, missing type hints, inconsistent language, `.env`/`.gitignore` check.

## Verdict: FAIL — 27 findings outside the accepted risks

The prior remediation (atomic `UPDATE...RETURNING`, PDF gate, broadcast confirm, aiosqlite, catalog cache, pagination shell) is real and verified. The code is **not** bulletproof. The items below are all outside the 7 accepted risks, each with exact file + line + fix. No `PASS` is warranted.

**Load verdict:** the bot will **not** cleanly survive 1000 concurrent users: blocking filesystem I/O remains on the hottest paths (F5/F6/F7), per-user/per-supervisor N+1 query fans persist (F12/F13), and broadcast fans out an unbounded in-RAM `gather` (F14).

---

## A. Error Handling & Resilience — FAIL

### F1 — FAIL — `process_key` crashes on non-text message (Fatal Runtime Bug)
**File:** `handlers/common.py:127`
```python
user_key = message.text.strip()
```
`process_key` is the `AuthState.waiting_for_key` message handler. Any non-text update while in this state (photo, sticker, voice, contact) has `message.text is None` → `AttributeError: 'NoneType' object has no attribute 'strip'`. The global `dp.errors` handler keeps the process alive, but this handler dies and the user is stranded with no guidance. Every sibling FSM text handler guards correctly (`process_admin_query_user:185`, `process_add_supervisor:536`, `process_admin_save_text:308` all use `message.text ... if message.text else ""`) — this one was missed.
**Fix:**
```python
user_key = (message.text or "").strip()
if not user_key:
    await message.answer("يرجى إرسال كود التفعيل كنص (أو /cancel للإلغاء).")
    return
```

### F15 — FAIL — `send_quiz_question` fails silently (swallowed error, stranded user)
**File:** `handlers/quiz.py:710-715`
```python
if not skill or not teil or not file_name:
    return
try:
    content = await get_content(skill, teil, file_name)
except Exception:
    return
```
Both early exits return with zero user feedback. Callers (`handle_answers`, `handle_skip_question`, `handle_prev_question`) already called `callback.answer()`, so the user sees their tap acknowledged and then nothing — a dead quiz with intact FSM.
**Fix:** notify before returning, e.g.:
```python
if not skill or not teil or not file_name:
    await safe_edit_message_text_or_send(callback_or_message, "⚠️ انتهت الجلسة الحالية، يرجى إعادة اختيار النص.", reply_markup=get_cancel_to_main_keyboard(), parse_mode="HTML")
    return
try:
    content = await get_content(skill, teil, file_name)
except Exception:
    logging.exception("get_content failed in send_quiz_question")
    await safe_edit_message_text_or_send(callback_or_message, "⚠️ تعذر تحميل الأسئلة، يرجى إعادة اختيار النص.", reply_markup=get_cancel_to_main_keyboard(), parse_mode="HTML")
    return
```
(add `import logging` to `handlers/quiz.py`).

### F16 — FAIL — `finish_teil3_matching` can exceed Telegram's 4096-char limit (unhandled `TelegramBadRequest`)
**File:** `handlers/quiz.py:665-701`
The finish text concatenates **every** pair + answer + keywords into one `edit_text`/`answer` call. Large Teil3 sets exceed 4096 chars → `TelegramBadRequest: message is too long` → escapes `safe_edit_message_text_or_send` (which only special-cases "message is not modified") → global handler, user sees nothing. `cmd_users` already chunks at 4000 chars; this path was missed.
**Fix:** chunk exactly like `cmd_users` (`MAX_CHUNK = 4000` loop over `finish_text`), or cap pairs shown + link back to list.

### F17 — FAIL — `activate_subscription` swallows DB errors as "invalid key"
**File:** `database.py:542-547`
```python
except Exception as e:
    try:
        await conn.rollback()
    except Exception:
        pass
    return False, ""
```
`e` is never logged. The caller (`process_key:179`) renders **any** `(False, "")` as "❌ كود التفعيل غير صحيح أو مستخدم سابقاً". A real DB outage/corruption therefore masquerades as user error and leaves zero trace in logs.
**Fix:** add `import logging` to `database.py` and log before returning:
```python
except Exception:
    logging.exception(f"activate_subscription failed for user_id={user_id}")
    try:
        await conn.rollback()
    except Exception:
        pass
    return False, ""
```
(Apply the same `logging.exception` to the sibling silent `except Exception: rollback; return` blocks in `set_user_referrer`, `process_referral_reward`, `claim_free_subscription`, `update_custom_text` — all currently log nothing.)

### F24 — FAIL — `/revoke` reports success for nonexistent users
**File:** `handlers/admin.py:675-683` (+ `database.py:409-418` `revoke_user`)
`revoke_user` is `UPDATE users SET ... WHERE user_id = ?` with no rowcount check, returning `None`. `cmd_revoke` unconditionally replies "🚫 تم إلغاء تفعيل المستخدم ... بنجاح" even when 0 rows matched.
**Fix:** return affected rows:
```python
# database.py
async def revoke_user(user_id: int) -> bool:
    ...
    async with aiosqlite.connect(DB_NAME) as conn:
        cursor = await conn.execute('UPDATE users SET sub_type = ... WHERE user_id = ?', (revoked_date, user_id))
        await conn.commit()
        return cursor.rowcount > 0
# handlers/admin.py
ok = await revoke_user(target_id)
await message.answer("🚫 تم ..." if ok else "❌ لا يوجد مستخدم بهذا الـ ID.", parse_mode="HTML")
```

---

## B. Performance & Load — FAIL (will not survive 1000 concurrent users)

### F5 — FAIL — Blocking filesystem I/O + per-file JSON parse in the hottest async handler
**File:** `handlers/common.py:476-479`, `503-521`, `552-577` (`b1_parts_`, `t_group_` branches of `handle_callbacks`)
Each listing does `os.listdir` + `os.path.exists` + **synchronous `open().json.load()` per file** (up to 10–15 files) inside an `async` callback to extract titles. With 106 JSON files on disk and concurrent users, this blocks the event loop per callback. The catalog/content cache built for exactly this problem is bypassed here.
**Fix:** resolve the file list from `await get_catalog()` and titles from `await get_content(...)` (which already uses `asyncio.to_thread`), or precompute a `{skill/teil/file: title}` index at startup with mtime invalidation. Never `open()` synchronously in a handler.

### F6 — FAIL — `get_next_uncompleted_target`: blocking I/O + up-to-106 sequential DB round-trips
**File:** `database.py:457-478`
```python
target_path = find_directory_case_insensitive(f"data/b1/{skill}", teil)  # os.listdir, blocking
if target_path and os.path.exists(target_path):                          # blocking
    files = sorted([f for f in os.listdir(target_path) ...])             # blocking
    for file_name in files:
        if not await is_text_completed(user_id, text_id):                # 1 connection + query EACH
```
Called from `user_progress` (high traffic). Cost per call: ~9 blocking dir scans + up to 106 sequential `aiosqlite.connect()` + `SELECT` round-trips.
**Fix:**
```python
from utils import get_catalog
catalog = await get_catalog()
async with aiosqlite.connect(DB_NAME) as conn:
    async with conn.execute('SELECT text_id FROM completed_texts WHERE user_id = ?', (user_id,)) as cur:
        done = {r[0] for r in await cur.fetchall()}
for skill in ("lesen", "hören"):
    for teil, files in catalog.get(skill, {}).items():
        for fn in files:
            if f"{skill}_{teil}_{fn}" not in done:
                return {"skill": skill, "teil": teil, "callback_data": f"b1_parts_{skill}_{teil}"}
```

### F7 — FAIL — `get_catalog` is `async` but does only blocking I/O
**File:** `utils.py:41-74` (+ `_data_max_mtime:16-38`)
`async def get_catalog()` and `_data_max_mtime()` call `os.path.getmtime`, `Path.iterdir()`, `is_dir()` directly — all blocking syscalls on the event loop, on **every** paywall/progress/quiz step (5+ call sites in `quiz.py`, plus `database.py`). `get_content` correctly offloads the file read with `asyncio.to_thread`, but the probe + catalog rebuild were left blocking.
**Fix:** offload the probe/rebuild (`await asyncio.to_thread(_build_catalog_sync)`) or make catalog build synchronous-once + mtime-probe in a thread. Do not add new deps.

### F12 — FAIL — Users-list N+1 survives the "MED-001 fix"
**File:** `handlers/admin.py:729-733` (`_send_users_page`), `770-774` (`cb_admin_users_page`)
`get_users_page` already `JOIN`s `users + user_profiles` and returns `(user_id, full_name, username, sub_type, expire_date)` — then the code **ignores** `sub_type/expire_date` and calls `await get_user_subscription(uid)` per row (10 users = 10 extra connections, ~20 extra queries per page, sequential). MED-001 added pagination but did not remove the loop query.
**Fix:** derive the status line from the already-fetched `sub_type/expire_date` columns (reuse the `_parse_iso_or_legacy` + UTC comparison locally) instead of calling `get_user_subscription` in a loop.

### F13 — FAIL — Financial-settlement N+1 per supervisor
**File:** `handlers/admin.py:616-630` (`cb_financial_settlement`)
```python
for s_id, s_custom_name in sups:
    async with conn.execute('SELECT full_name ... WHERE user_id = ?', (s_id,)) ...
    async with conn.execute('SELECT sub_type, COALESCE(SUM(used_count),0) ... WHERE created_by = ? ...', (s_id,)) ...
```
2 queries × N supervisors, sequential, on one admin tap.
**Fix:** single query with `LEFT JOIN` + `GROUP BY`, e.g.:
```sql
SELECT s.supervisor_id, COALESCE(s.supervisor_name, p.full_name, 'ID:'||s.supervisor_id),
       COALESCE(SUM(CASE WHEN k.is_settled=0 THEN k.used_count ELSE 0 END),0),
       COALESCE(SUM(CASE WHEN k.is_settled=0 THEN k.used_count * CASE k.sub_type WHEN 'intensive' THEN 5 WHEN 'group' THEN 20 ELSE 10 END ELSE 0 END),0)
FROM supervisors s LEFT JOIN user_profiles p ON p.user_id=s.supervisor_id
LEFT JOIN keys k ON k.created_by=s.supervisor_id GROUP BY s.supervisor_id
```

### F14 — FAIL — Broadcast fans out an unbounded in-RAM `gather` (OOM at scale)
**File:** `handlers/admin.py:890-906` (fetch-all + `create_task`) and `793-816` (`_run_broadcast_task:816`)
```python
async with conn.execute('SELECT user_id FROM user_profiles') as cursor:
    users = await cursor.fetchall()                       # ALL ids in RAM
results = await asyncio.gather(*(_send_one(u[0]) for u in users))  # N coroutine objects at once
```
The `Semaphore(30)` caps *concurrency* but not *creation*: 100k users = 100k coroutine objects materialized in one `gather` call + full id list in RAM. This is the classic broadcast OOM.
**Fix:** page the recipients (`LIMIT/OFFSET` batches of e.g. 200) and `gather` per batch sequentially (keep the existing `_send_one`/semaphore/retry logic unchanged).

---

## C. Security (AppSec) — FAIL

### F8 — FAIL — Listing guard uses the forbidden `os.path.commonpath` primitive (constitution §12 violation)
**File:** `handlers/common.py:507`, `563`
```python
file_path = os.path.abspath(os.path.join(target_path, file_safe))
if os.path.commonpath([allowed_data_dir, file_path]) != allowed_data_dir:
```
`agent.md §12` explicitly forbids `basename`/`commonpath` for security boundaries (bypassable; `abspath` does not resolve symlinks; case/sep behavior differs on Windows). `utils.py` already implements the correct `Path.resolve().is_relative_to()` guard — these two branches did not adopt it.
**Fix:** replace both blocks with the `utils.py` pattern:
```python
allowed = Path(BASE_DIR, "data").resolve()
fp = (Path(target_path) / file_safe).resolve()
try:
    if not fp.is_relative_to(allowed) or not fp.is_file():
        continue
except (ValueError, AttributeError):
    continue
```

### F9 — FAIL — AuthZ bypass: `view_full_text` serves paid text with zero subscription check (IDOR-class)
**File:** `handlers/quiz.py:103-129` (`handle_view_full_text`)
The handler reads `skill/teil/file_name` from FSM and returns the full `text_body` (up to ~1900 chars) with **no** `can_access_level` / `is_free_content` gate. Mid-quiz paywalls intentionally *preserve* FSM (`quiz.py:226,362,411,458`), so a paywalled user (e.g. stuck at hören/teil1 Q6) still holds valid `skill/teil/file_name` pointers and can tap "📄 عرض النص الأساسي" to exfiltrate the entire paid passage.
**Fix:** gate like every other quiz transition:
```python
from database import can_access_level, is_free_content
# inside handle_view_full_text after loading skill/teil/file_name:
if not await is_free_content(skill, teil, file_name, question_index=data.get('current_index', 0)) and not await can_access_level(callback.from_user.id, "b1"):
    await callback.answer("⚠️ هذا النص مخصص للمشتركين فقط.", show_alert=True)
    return
```

### F10 — FAIL — Teil3 click trusts unvalidated `t3_q_*`/`t3_a_*` IDs (untrusted callback input → stat poisoning)
**File:** `handlers/quiz.py:609-621` (`handle_teil3_click`)
```python
if click_data.startswith("t3_q_"):
    q_id = click_data.replace("t3_q_", "")
    selected_q = q_id if selected_q != q_id else None
```
No membership check against `remaining_q_ids` / `remaining_a_ids`. A forged `t3_q_Q999` is stored, then paired: `correct_matches.get("Q999")` → `None` ≠ selected → marked wrong → `record_answer_stat(..., False)` inflates failure stats and pollutes `selected_*` state. Violates `agent.md §2` ("Treat `callback_query.data` as UNTRUSTED").
**Fix:**
```python
if click_data.startswith("t3_q_"):
    q_id = click_data.replace("t3_q_", "")
    if q_id not in data.get("remaining_q_ids", []):
        await callback.answer("⚠️ اختيار غير صالح.", show_alert=True)
        return
    ...
# mirror for t3_a_ against remaining_a_ids
```

### F11 — FAIL — Teil3 engine never re-verifies subscription after entry
**File:** `handlers/quiz.py:514-516`, `609-663`
Entry (`handle_read_text:148`) gates Teil3, but `handle_teil3_events`/`handle_teil3_click` perform unlimited subsequent steps with no `can_access_level` re-check. A user revoked/expired mid-matching continues answering (and writing `record_answer_stat`) for free. Every other quiz path (`ans_*`, `skip_*`, `prev_*`) re-checks; Teil3 does not. Violates `agent.md §9` (re-verify on ANY transition).
**Fix:** at the top of `handle_teil3_click`, add the standard gate on the session pointers and `return` before mutating state on denial.

**Explicit non-findings (checked, clean):** all money SQL uses parameterized `?` placeholders; the single dynamic-SQL site (`database.py:184` `PRAGMA table_info`) is preceded by an allowlist membership test (`181-182`, raises `ValueError`); key activation and referral claim are single-statement atomic `UPDATE...RETURNING` inside `BEGIN IMMEDIATE` on one connection; `find_directory_case_insensitive` + `get_content` enforce `resolve().is_relative_to(data/)` + basename lock. No additional SQLi / traversal / classic IDOR beyond F8–F11 was found.

---

## D. Business Logic & State — FAIL

### F2 — FAIL — `claim_free_subscription` overwrites paid expiry and leaks stale group flags
**File:** `database.py:682-690`
```python
expire_date = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
... ON CONFLICT(user_id) DO UPDATE SET expire_date = excluded.expire_date, sub_type='all', activated_via='FREE_REFERRAL'
```
Unlike `activate_subscription:525` (`max(current_utc, current_expire) + days`), the free claim **destroys** remaining paid days (user with 25 paid days left who redeems a referral reward drops to 30 instead of 55). The `UPDATE` also leaves stale `is_group/is_intensive` flags (a prior group buyer keeps `is_group=1` while `sub_type='all'`).
**Fix:** mirror the stacking logic:
```python
# read current expire_date first, parse with _parse_iso_or_legacy, then:
expire_date = (max(datetime.now(timezone.utc), current_expire) + timedelta(days=30)).isoformat()
... ON CONFLICT(user_id) DO UPDATE SET expire_date=excluded.expire_date, sub_type='all', activated_via='FREE_REFERRAL', is_group=0, is_intensive=0
```

### F26 — FAIL — Paywall-branch resume targets the wrong quiz (FSM loophole)
**File:** `handlers/quiz.py:148-160` + `handlers/common.py:130-138`
Entry paywall does `set_state(AuthState.waiting_for_key)` **without clearing data**, preserving the *old* `file_name/current_index`. Scenario: user mid-quiz A clicks locked text B → paywalled on B (state still points at A) → pays → `process_key` sees `file_name+current_index` (A) and resumes A; B is never opened and there is no pending-target record.
**Fix:** stash the requested target on paywall and prefer it on resume:
```python
# quiz.py paywall branch:
await state.update_data(pending_skill=skill, pending_teil=teil, pending_file=file_name)
await state.set_state(AuthState.waiting_for_key)
# common.py process_key success branch:
data = await state.get_data()
if data.get("pending_file"):
    await state.update_data(skill=data.pop("pending_skill"), teil=data.pop("pending_teil"), file_name=data.pop("pending_file"), current_index=0, correct_count=0, wrong_count=0)
    await state.set_state(QuizState.answering)
elif "file_name" in data and "current_index" in data:
    await state.set_state(QuizState.answering)
else:
    await state.clear()
```

### F3 — FAIL — Content cache keyed by basename only (cross-quiz poisoning, latent)
**File:** `utils.py:91-92`, `115`, `117`
```python
safe_name = os.path.basename(file_name)
if safe_name not in _CONTENT_CACHE: ...
_CONTENT_CACHE[safe_name] = parsed
return _CONTENT_CACHE[safe_name]
```
The key discards `skill/teil`. All 106 current basenames happen to be unique (verified by scan), so this is latent — but the first future same-name file in two parts (e.g. `text1.json` under both `lesen/teil1` and `hören/teil1`) silently serves the wrong quiz.
**Fix:** `cache_key = f"{skill.lower().strip()}/{teil.lower().strip()}/{safe_name}"` for store/lookup/return.

### F4 — FAIL — Content cache never invalidates on in-place file edits (stale quiz forever)
**File:** `utils.py:16-38` (`_data_max_mtime`), `86-89`
Invalidation probes **directory** mtimes only (`data/b1`, skill, teil dirs). On all major filesystems, editing a file's bytes updates the *file's* mtime, not its parent directory's. Correcting a wrong answer key in place therefore never bumps the probe → stale cached quiz served indefinitely (until a file is added/removed/renamed).
**Fix:** include file mtimes in the probe (e.g. max over `data/b1/**/*.json` mtimes, or track per-file mtime/size in the cache entry and re-stat on hit).

### F27 — FAIL — `protect_content` flag is dead (forward-protection never applied on edits)
**File:** `handlers/common.py:27-33`
```python
if protect_content:
    await callback.message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
else:
    await callback.message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
```
Both branches are byte-identical; `protect_content` is never forwarded. Quiz/content edits therefore lack the forwarding restriction the welcome/activation sends (`protect_content=True`) intended.
**Fix:** `await callback.message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode, protect_content=protect_content)` (aiogram `edit_text` supports it; otherwise route through `answer` with the flag).

---

## E. Fatal Runtime / Startup / Config — FAIL

### F18 — FAIL — Missing `API_TOKEN` crashes startup with a cryptic error (no fail-fast)
**File:** `config.py:10`
```python
API_TOKEN = os.getenv('API_TOKEN')
```
No validation. Without `.env`, `main.py:24` `Bot(token=None)` raises an opaque `TokenValidationError` deep in aiogram instead of a clear startup message.
**Fix:**
```python
API_TOKEN = os.getenv('API_TOKEN')
if not API_TOKEN:
    raise RuntimeError("API_TOKEN is missing: set it in .env (see .env.example).")
```

### F19 — FAIL — `DB_NAME` is CWD-relative (split-brain databases)
**File:** `config.py:47` (affects all 30+ `aiosqlite.connect(DB_NAME)` sites)
```python
DB_NAME = 'bot_database.db'
```
Relative to process CWD, not the project. Running under a different CWD (systemd, Docker, cron, tests) creates a second empty DB: subscriptions/keys/referrals diverge, users appear "unsubscribed".
**Fix:**
```python
DB_NAME = str(BASE_DIR / DB_NAME) if not os.path.isabs(DB_NAME) else DB_NAME
# i.e. DB_NAME = str(pathlib.Path(__file__).parent.resolve() / 'bot_database.db')
```
(Note `BASE_DIR` is defined below `DB_NAME` today — reorder so `BASE_DIR` comes first.)

### F20 — FAIL — Malformed `ADMIN_IDS` entries silently discarded (silent lockout)
**File:** `config.py:20-24`
```python
try:
    ids.append(int(part))
except ValueError:
    continue
```
A typo (`ADMIN_IDS=21377abc,720656784`) silently drops the admin with no log; the operator discovers the lockout only when `/admin` goes dead.
**Fix:** `import logging; logging.warning(f"Ignoring malformed ADMIN_IDS entry: {part!r}")` in the `except` branch.

### F21 — FAIL — Supervisor `created_at` stored in legacy naive format (violates ISO-8601 standard)
**File:** `handlers/admin.py:553`
```python
now_str = message.date.strftime('%Y-%m-%d %H:%M')
```
Drops timezone + seconds after HIGH-005 standardized everything else on `datetime.now(timezone.utc).isoformat()`. Inconsistent with `_parse_iso_or_legacy` expectations and the constitution.
**Fix:** `now_str = message.date.isoformat() if getattr(message.date, 'tzinfo', None) else datetime.now(timezone.utc).isoformat()`.

### F22 — FAIL — `admin_panel_back` strands supervisors
**File:** `handlers/admin.py:147-152`
```python
await state.clear()
if callback.from_user.id in ADMIN_IDS:
    await safe_edit_message_text(...)
await callback.answer()
```
Supervisors (legitimate `admin_router` users via `is_supervisor`) tapping "🔙 لوحة الأدمن" get a bare `answer()` and no panel — dead end.
**Fix:**
```python
if callback.from_user.id in ADMIN_IDS:
    ... get_admin_panel() ...
elif await is_supervisor(callback.from_user.id):
    ... get_supervisor_panel() ...
```

### F23 — FAIL — Silent drops on `sup_toggle_*` / `sup_delete_*` / `settle_sup_*` for non-admins (client spinner hangs)
**File:** `handlers/admin.py:573-574`, `589-590`, `658-659`
```python
if callback.from_user.id not in ADMIN_IDS:
    return
```
No `callback.answer(...)`, so a forged/expired tap leaves the Telegram loading spinner forever. The middleware-level HIGH-008 fix answered; these handler-level guards did not.
**Fix:** `await callback.answer("⚠️ مقتصر على الأدمن الرئيسي.", show_alert=True); return`.

### F25 — FAIL — Users pagination accepts `page <= 0` (negative `OFFSET`)
**File:** `handlers/admin.py:747-761` (`cb_admin_users_page:754-761`)
`page = int(...)` is used unchecked: `offset = (page - 1) * limit`. `page=0/-N` (crafted `admin_users_page_-5`) yields a negative `OFFSET` passed to SQLite.
**Fix:** after parsing, `if page < 1: page = 1`.

---

## F. Concurrency & Data-Integrity notes (checked)

- Key activation (`database.py:488-496`) and referral claim (`599-605`) are atomic single-statement `UPDATE...RETURNING` inside `BEGIN IMMEDIATE` on one connection — TOCTOU fixes verified intact.
- `check_genkey_ratelimit` (`handlers/admin.py:32-38`) contains no `await` between read and write, so the check-then-act is atomic on the single-threaded event loop — not flagged.
- `claim_free_subscription` correctly serializes on `BEGIN IMMEDIATE`; the flaw is stacking semantics (F2), not atomicity.
- SQLite concurrency under 1000 users remains a structural ceiling (single-writer + per-request `connect()` fan in F12/F6); connection pooling is impossible with the current file-DB design — noted as architecture, not a new FAIL beyond the items above.

---

## Remediation priority

1. **Today (correctness/security):** F9, F10, F11, F8, F1, F2, F26.
2. **This week (load/stability):** F5, F6, F7, F12, F14, F15, F16, F4, F3.
3. **Hardening (startup/UX):** F18, F19, F20, F21, F22, F23, F24, F25, F17, F27.

*End of review_2. Verdict: FAIL (27 FAIL items, all outside the 7 accepted risks).*
