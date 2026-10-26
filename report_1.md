# Report 1 — Pre-Flight & Stress Audit (Highly Critical)

**Date (UTC):** 2026-10-11
**Scope:** Entire bot codebase (`main.py`, `config.py`, `database.py`, `utils.py`, `keyboards.py`, `handlers/admin.py`, `handlers/quiz.py`, `handlers/common.py`, `middlewares/throttling.py`, `check_json.py`)
**Method:** Full file reads + targeted `grep` verification (`BadRequest|RetryAfter|TelegramAPI`, `os.listdir|os.walk|json.load|create_task|Semaphore|TTLCache|BEGIN IMMEDIATE`, `view_full_text|question_index=0`). No code changed.
**Corpus measured:** 159 JSON files / 290,918 bytes under `data/b1`; `handlers/admin.py` 1383 lines (~64 KB), `handlers/quiz.py` 891 lines, `handlers/common.py` 771 lines, `database.py` 1107+ lines, `keyboards.py` 392 lines, `utils.py` 225 lines, `middlewares/throttling.py` 47 lines.
**Focus (per brief):** NOT basic syntax. Only (1) Operational Readiness, (2) Business Logic & Edge Cases (المنطقية), (3) High-Load Concurrency & Anti-DoS.

Severity scale: 🔴 CRITICAL (launch-blocker / data-loss / full-outage) · 🟠 HIGH (degraded UX, silent failures, exploitable edge) · 🟡 MEDIUM (latent, scales badly) · 🟢 LOW (hygiene).

---

## Executive Summary — Can this bot survive production?

| Pillar | Verdict |
|---|---|
| 1. Operational Readiness | 🟠 **NOT launch-ready.** Two startup calls (`set_my_commands` default scope, `delete_webhook`) are unguarded — a Telegram outage at boot kills the bot. ~150 Bot-API calls rely solely on the global error handler, which logs-and-swallows (user sees nothing). One real FSM trap: `AuthState.waiting_for_key` has no `/cancel` exit. |
| 2. Business Logic & Edge Cases | 🟠 **One HIGH paywall hole + one business-rule flag.** `handle_view_full_text` (`quiz.py:158`) checks freeness at `question_index=0` then serves the full passage — a paid-only `hören` user (Q6+) passes the free test and reads premium text. Referral reward fires once-ever (`counted_refs == 2`, `database.py:711`) while user-facing copy promises "every 2 points" — either the code or the copy is wrong. SQLite money paths are otherwise correctly atomic (`UPDATE...RETURNING` inside `BEGIN IMMEDIATE`). |
| 3. High-Load Concurrency & Anti-DoS | 🔴 **Will NOT survive 5000 concurrent users.** Per-call `aiosqlite.connect()` (one thread per connection) × 5–6 connections per quiz click × 15-way `gather` fan-out per list render, against a single-writer SQLite file with a 5 s `busy_timeout`, no write queue, no pooling, unbounded `_CONTENT_CACHE`, throttle cache capped at 10k entries, and broadcast holding a semaphore across `sleep(retry_after)`. Expected outcome under stress: thread explosion → `SQLITE_BUSY` mass failures → silently swallowed by the global handler → stats silently lost. |

**Top 5 launch-blockers (fix before any traffic spike):**
1. 🔴 DB single-writer + per-call connections with no serialization → mass `database is locked` under load (§3.1).
2. 🟠 `view_full_text` free-check bypass (`quiz.py:158`) → premium text leak (§2.2).
3. 🟠 Unguarded startup (`main.py:42` `set_my_commands`, `main.py:76` `delete_webhook`) → boot crash on Telegram outage (§1.1).
4. 🟠 `AuthState.waiting_for_key` has no `/cancel` path → users stuck typing `/cancel` as a "key" (§1.4/§2.1).
5. 🟡 Broadcast: `asyncio.create_task` fire-and-forget + full `fetchall()` + semaphore held across 429-sleep → parallel broadcasts, RAM growth, 30-slot stall (§3.3).

---

## Pillar 1 — Operational Readiness

### 1.1 Telegram errors: what is caught locally vs global-only

Baseline — `main.py:65-74` global handler catches `TelegramRetryAfter` (warn) and **everything else** (log + `return True`). It never retries, never notifies the user. Cancellation is correctly re-raised (prior F-10 fix verified intact).

**Locally guarded (the only places):**
- `TelegramBadRequest` → 3 `safe_edit*` helpers only: `admin.py:63-71`, `common.py:68-86`, `quiz.py:136-148`. All three special-case only `"message is not modified"`; any other `BadRequest` (`message too long`, `can't parse entities`, `message to edit not found`, `BUTTON_DATA_INVALID`) is re-raised to global → user sees nothing.
- `TelegramRetryAfter` → exactly 1 place: broadcast `_send_one` (`admin.py:1212`), single retry only; a second 429 falls into `except Exception: return False`.
- Bare `except Exception` around ~6 sends: broadcast report/preview (`admin.py:1233-1242`, `1260-1267`, `1331-1337`), ticket copy (`common.py:300-319`), referral/log sends (`common.py:126-129`, `198-201`, `225-228`), throttled-callback answer (`throttling.py:39-42`).
- `TelegramAPIError` / `TelegramForbiddenError` are **never caught locally anywhere** (verified: only import at `admin.py:12`, zero `except` uses). Blocked-bot / deleted-account errors are lumped into generic `False`/swallow.

**Global-only (representative; pattern holds for ~150 calls):**
- 🟠 `main.py:42` `await bot.set_my_commands(user_commands, scope=BotCommandScopeDefault())` — **unguarded**. The per-admin loop (`main.py:44-47`) is guarded, the default is not. Telegram outage at startup → `main()` crashes before polling.
- 🟠 `main.py:76` `await bot.delete_webhook(drop_pending_updates=True)` — **unguarded**. Network failure aborts startup identically.
- 🟡 All quiz/common/admin content sends (`quiz.py:191,219,222,232,260,274,293,298,303,448,462,472,532,769-775,884-888`; `common.py:156,164,173,180,230,232,234,264,277,281,295,313,317,361,576-584,619-625`; `admin.py:118,122,132,134,145,149,164,177,194,521,559,608-611,653,683,733,1069,1126-1135,1353-1380`) — any `BadRequest`/`Forbidden`/`RetryAfter` escapes to global: logged, swallowed, user stuck with dead UI.
- 🟡 Chunk loops with no per-chunk try: `admin.py:605-611` (sup-stats chunks), `quiz.py:769-775` (`finish_teil3_matching` chunks), `quiz.py:884-888` (`send_quiz_question` head chunks). One `BadRequest` aborts remaining chunks.
- 🟢 Broadcast `_run_broadcast_task` (`admin.py:1201-1242`) is the **only** send-loop done right: `Semaphore(30)`, 500/chunk `gather`, per-recipient try + 429 sleep + single retry. Gaps: no `Forbidden`/`BadRequest` discrimination (all `False`), no backoff, and `asyncio.create_task(...)` at `admin.py:1312` is fire-and-forget (no handle, no dedup, no cancel — parallel admin confirms spawn parallel broadcasts; restart kills silently).

> **Severe warning:** the global handler's `return True`-always policy converts every unguarded failure (including mass `SQLITE_BUSY` under load, §3.1) into **silent** UX death. At minimum, user-facing callbacks should get a fallback notice; startup calls must be retried, not crashed on.

### 1.2 Missing imports, dead code, use-before-assignment

**Unused imports (🟢 LOW, all verified by grep):**
- `handlers/admin.py:1` `import aiosqlite` — never used (DB via `database._db_connect`).
- `handlers/admin.py:12` `TelegramAPIError` — imported, never referenced.
- `handlers/admin.py:17` `DB_NAME` — imported, never used in file.
- `handlers/common.py:5` `import json` — no `json.` usage (only `'.json'` string literals).
- `handlers/quiz.py:4` `import asyncio` — no `asyncio.` usage.
- `middlewares/throttling.py:2` `import logging` — never used.
- `handlers/admin.py:16` + `:940` duplicate `import re` (top-level + function-local in `_extract_current_keys_page`).

**Dead / misleading code:**
- 🟢 `keyboards.py:334-338` `get_back_to_b1_skills()` — defined, zero call sites.
- 🟡 `common.py:68-74` dead branch: `if protect_content:` / `else:` contain **identical** `edit_text` calls; the flag is never forwarded (`edit_text(..., protect_content=...)` never passed). Activation sends use `protect_content=True` (`common.py:160,167,230`) but every edit loses it — inconsistent content-protection posture.
- 🟢 `admin.py:1244-1250` partially unreachable: state handler checks `text.startswith("/cancel")`, but exact `/cancel` is intercepted first by `cmd_cancel` (`admin.py:113`, registered first, no state filter). The branch only fires for `/cancelXYZ` variants — and also **matches** them as cancel (over-broad).
- 🟡 `common.py:266-267` misleading comment: `# Otherwise let other routers / handlers decide — do not clear` + `return`. In aiogram v3 a matched `Command("cancel")` handler **consumes** the event; `return` does not propagate. `/cancel` in `AuthState`/`QuizState` is swallowed here with no action (see §1.4).

**Use-before-assignment:** no classic `UnboundLocalError` found (all `try` blocks assign before use). Operational equivalents instead:
- 🟠 State-stuck on DB throw (no `finally`/`clear`): `process_add_supervisor` (`admin.py:715-733`), `process_admin_save_text` (`:377`), `process_admin_save_price` (`:474`) — `state.clear()` only on success/slash paths. If the DB raises, the exception goes global and the FSM stays in `waiting_for_*` with no guidance.
- 🟠 Bare FSM-dict access in quiz: `data["questions"]`/`data["answers"]` (`quiz.py:613-616`), `data["correct_matches"]` (`:690`), `data['skill']/['teil']/['file_name']` (`:734-736`) with no `KeyError` guard. Expired/evicted FSM → `KeyError` → global → user sees nothing, view never renders.
- 🟡 `cb_user_profile` / `menu_referral` (`common.py:338-342,361,406`): `get_catalog()` + `bot.get_me()` + DB stats all unguarded — any failure kills the profile button silently.

### 1.3 Missing / corrupted JSON handling — will the loop crash?

`utils.py:137-183` `get_content()` raises `FileNotFoundError` (unknown dir, path escape, missing file) and `ValueError` (`json.load` → `JSONDecodeError ⊂ ValueError`, bad shape). Runtime `json.load` exists **only** at `utils.py:176` (the `check_json.py:15` copy is an offline validator, never imported).

**Caller coverage — all PASS (no loop crash):**

| Call site | Handling |
|---|---|
| `quiz.py:165-170` `handle_view_full_text` | `except Exception` → empty title/body → user popup notice |
| `quiz.py:216-223` `handle_read_text` | `FileNotFoundError` → "file missing" notice; `Exception` → "could not read" notice |
| `quiz.py:271-275` `handle_answers`, `:459-463` `handle_skip` | `except Exception` → "session expired" notice |
| `quiz.py:542-546` `handle_prev` probe + `send_quiz_question:787-791` real load | probe `except: pass`; real load → safe-edit fallback message |
| `common.py:683-700,743-760` list builders | per-file `try/except` → filename fallback title; list still renders via `gather` |
| `common.py:39-66` `get_teil_tip` | nested `try` → `""` fallback, never raises |

Residual escape lands in `main.py:65-74` (`return True`) — **the event loop survives**. Operational cost is silent UX degradation (generic popup + a fallback edit that can itself raise `BadRequest` to global). No action required beyond noting: JSON corruption is handled; UX messaging is thin.

### 1.4 `/cancel` coverage per FSM state (lockup matrix)

| State | Cancel exit? | Evidence / gap |
|---|---|---|
| `AdminSupervisorState.*`, `AdminEditState.*` | ✅ YES | generic `cmd_cancel` (`admin.py:113-122`) + slash-clear in each saver |
| `BroadcastState.waiting_for_message` | ✅ YES (2 paths, 1 hazard) | `/cancel` (`admin.py:113` + `1247-1250`), cancel button (`:1322-1337`); hazard: only `/cancel` prefix intercepted — `/admin` typed mid-broadcast is stored as broadcast content; `/genkey` etc. don't `clear` (only `cmd_admin:129` does) → next message consumed as broadcast |
| `SupportState.waiting_for_message` | ✅ YES (narrow) | `common.py:258-267,270-278`; exact `== "/cancel"` only — `/cancel@BotName` via typed message relies on the `Command` filter path |
| `AuthState.waiting_for_key` | 🔴 **NO** | `process_key` (`common.py:170-234`) has no slash check; `cmd_cancel_common` explicitly no-ops non-support states; admin cancel requires admin rights. Typing `/cancel` → consumed as key text → `activate_subscription("/cancel")` → `False` → "wrong code" reply, state NOT cleared — **stuck** (only `/start` exits) |
| `QuizState.answering` | 🟡 **NO message-cancel** | no `message(QuizState)` handler; `/cancel` typed mid-quiz ignored. Button exits (`main_menu`, `b1_parts_*`) all `state.clear()` — tap-to-exit works, type-to-exit doesn't |
| `Teil3State.matching` | 🟡 **NO message-cancel** | same; plus stale `t3_q_/t3_a_` taps after `clear()` match no handler filter (`quiz.py:565`) → silent spinner until client timeout |

Cross-cutting 🟡: `AdminAuthMiddleware` (`admin.py:99-104`) answers non-admin `/cancel` with a denial **before** `common_router` sees it, so a normal user in `SupportState` gets a double reply (denial + real cancel); non-support `/cancel` from normal users gets only the denial + swallow.

---

## Pillar 2 — Business Logic & Edge Cases (المنطقية)

### 2.1 Remaining state lockups

- 🔴 **`AuthState.waiting_for_key`** (entry `quiz.py:207-208`): no `/cancel`, and entry **wipes** quiz pointers (`state.clear()` then set key-state) while mid-quiz paywall paths **preserve** them (`quiz.py:284-289,433-438,477-482,517-522`) — inconsistent. Worse, the success resume (`common.py:186-190`) expects `file_name+current_index` that entry just wiped, and a paywall on text B preserves text A's pointers → resume lands on the **wrong text**. Fix: stash `{skill,teil,file_name,current_index}` on paywall entry; clear on success; honor `/cancel`.
- 🟡 **`QuizState.answering`** (entry `quiz.py:240-241`): no type-to-cancel; stale `ans_/skip/prev` after clear dead-end safely (`quiz.py:259-261,447-449,531-533`), stale `read_` re-enters (re-gated — safe). Not a lockup for button users; keyboard-only users must know `/start`.
- 🟡 **`Teil3State.matching`** (entry `quiz.py:588`, exit `:767`): in-view exit buttons exist (`quiz.py:652-655`), but no `/cancel` and no stale-button fallback — add a catch-all `Teil3State.matching` callback handler answering "session ended".
- 🟢 **`BroadcastState`**: covered (§1.4) except command-interleaving hazard above.
- ✅ **`SupportState`**: correct — clear on `/cancel`, success, hard-fail, and `main_menu`; rate-limit path intentionally retains FSM for retry (documented, acceptable).

### 2.2 `can_access_level` bypass analysis

Definition `database.py:431-439` (explicit SKU map `b1/all/intensive/group` — Rule 14 compliant); free rule `database.py:441-462` (first file per skill/teil free; `hören` only `question_index < 6`).

| Handler | Entry check | Navigation/target check | Verdict |
|---|---|---|---|
| `handle_view_full_text` (`quiz.py:150`) | `is_free(...,question_index=0)` + `can_access(b1)` | **none** | 🔴 **HIGH — bypass.** Paid `hören` Q6+ user passes the `index=0` free test, receives the full passage (`:176-181`, up to 1900 chars). Fix: check with the **actual** `current_index` from FSM, not `0` |
| `handle_read_text` (`quiz.py:183`) | `is_free(idx=0)` + `can_access` | n/a (entry) | 🟢 OK but fragile — same `0`-index pattern; entry-only is correct here since reading starts at 0 |
| `handle_answers` (`quiz.py:253`) | current-index gate (`:282-284`) | target gate on `next_index` (`:433`) | ✅ OK — both ends gated |
| `handle_skip_question` (`quiz.py:442`) | current (`:475-477`) | `next_index` (`:517`) | ✅ OK |
| `handle_prev_question` (`quiz.py:526`) | — | `prev_index` before `update_data` (`:555-562`, HIGH-003 fix) | ✅ OK — stateless `prev_question` can only step −1, no arbitrary jump |
| `handle_teil3_click` (`quiz.py:660`) | `is_free(idx=0)` + `can_access` **per click** (`:666-667`) | n/a (whole-file model) | ✅ OK (prior review claiming a missing check is stale) |
| `send_quiz_question` (`quiz.py:777`) | **none** (`:777-801` presence + `get_content` only) | none | 🟡 defense-in-depth gap — relies entirely on callers; any future direct caller serves premium freely. Add an internal gate |
| `b1_parts_/t_group_/b1_skill_` listings (`common.py:551,641,716`) | **none** — titles/completion/hashes exposed pre-paywall | n/a | 🟡 enumeration only — entry still gated, not a full bypass |
| `b1_skill_schreiben/sprechen` PDFs (`common.py:558-632`) | **none** | n/a | 🟡 flag-if-paid / ✅ if intentional — `FEATURE_CHANGELOG:29` says the gate was deliberately removed (free users reach PDFs). No `can_access_level` call remains in `common.py` despite the import (`common.py:22`). Confirm this is the intended business rule |

**Forged `callback_data`:** the old `quiz_<skill>_<teil>_<hash>_<idx>` pattern **does not exist** — only `read_/ans_/skip/prev/view_full_text/t3_` prefixes. `ans_` carries only the choice (index from FSM), `skip/prev` are fixed ±1: no arbitrary-index jump. `read_` parsing (`quiz.py:194-202`) takes `skill/teil` from the client but `file_name` from server-side `FILENAME_MAP` hash + `basename` + `is_relative_to(data/)` + `is_file` (`utils.py:152-172`) — mismatched triples dead-end via `FileNotFoundError`, not bypass. Residual availability note: `FILENAME_MAP` is in-memory, so restart invalidates all old buttons (`resolve_filename → None` → "button expired").

### 2.3 SQLite transaction atomicity (per function)

All connections set `WAL + busy_timeout=5000` (`database.py:111-113`), so `BEGIN IMMEDIATE` serializes writers correctly where used.

| Function (`database.py`) | Atomic? | Notes |
|---|---|---|
| `activate_subscription` (`:538-614`) | ✅ GOOD | `BEGIN IMMEDIATE` (`:548`) same-conn; atomic key claim `UPDATE...RETURNING` (`:551-559`); miss → rollback+return (`:562-565`); user upsert + referral reward in same txn (`:581-601`); `commit` (`:603`); `except: raise` + `finally: rollback if not committed` (`:606-614`). No TOCTOU |
| `generate_new_key` (`:616-639`) | ✅ GOOD | `BEGIN IMMEDIATE` + INSERT + commit; log+raise + finally rollback |
| `set_user_referrer` (`:643-671`) | 🟡 MED (swallowed) | txn discipline correct incl. `IntegrityError` idempotent-duplicate path (`:660-662`); but generic `except Exception: logging.exception` (`:663-665`) with no raise/flag — caller (`common.py:109`) assumes success on real DB errors |
| `_process_referral_reward_conn` (`:673-717`) + `process_referral_reward` (`:719-739`) | 🟡 MED (late validation) + 🟢 LOW (swallow) | atomic flip `UPDATE...RETURNING` (`:679-684`) = exact-once, good; **but** self-check (`:693-694`) and referrer-existence check (`:697-703`) run **after** the flip — an invalid referrer burns the row to `1`, blocking later correction. `counted_refs == 2` check-then-act is safe only via `BEGIN IMMEDIATE` serialization. Outer swallow (`:730-733` → `None,0`) hides real errors from standalone callers |
| `claim_free_subscription` (`:754-805`) | 🟡 MED (TOCTOU-prone by shape) | `BEGIN IMMEDIATE` + empty-credit rollback path good; but `UPDATE free_credits SET credits_count = credits_count - 1 WHERE user_id = ?` (`:769`) has no `AND credits_count > 0` / `RETURNING` guard — safe today only via serialization; can go negative if timeout semantics ever change. Swallow preserves `(bool,str)` contract (`:795-799`) — acceptable but must stay logged |
| `update_custom_text` / `set_custom_text` (`:818-846`) | 🟢 LOW (non-financial) | correct `BEGIN IMMEDIATE` + upsert + commit + logged `False` + finally rollback |
| `revoke_key` (`:1038-1067`) | ✅ GOOD | `BEGIN IMMEDIATE` + parameterized UPDATE + commit; raise + finally rollback; idempotent |
| `revoke_key_by_rowid` (`:1069-1107`) | 🟢 LOW | SELECT-then-UPDATE lets two concurrent revokes both report success — no financial impact |
| `revoke_user` (`:464-474`) | 🟢 LOW (no txn discipline) | single-statement autocommit is atomic, but no `BEGIN IMMEDIATE` / try-finally rollback — inconsistent with the strict pattern elsewhere |

Double-reward note: `activate_subscription:601` runs the referral reward **inside** the activation txn, and `common.py:193` calls `process_referral_reward` again post-commit. Safe only via `RETURNING` idempotency (2nd sees `is_counted=1` → `(None,0)`). Redundant — leave a comment or remove the second call.

### 2.4 Referral farming

Schema (`database.py:208-216`): `referred_id UNIQUE`, `is_counted DEFAULT 0`. Verdict: **economically sound, one business-rule contradiction.**
- ✅ Self-referral blocked triple (`common.py:108`, `database.py:645-646`, `:693-694`) — last one post-flip (wastes row; reorder before the flip).
- ✅ Duplicates idempotent (`database.py:660-662` first referrer wins; replay ignored).
- ✅ Points count only on **activation**, not `/start` join — each fake account must burn a valid key (real economic cost; no device/IP throttling, but acceptable).
- ✅ `is_counted` flip exact-once via single `UPDATE...RETURNING`; concurrent activations serialize via `BEGIN IMMEDIATE` + 5 s timeout.
- 🔴 **Business-logic flag:** `counted_refs == 2` (`database.py:711-716`) fires **once ever**, not per-2-points. 4 referrals → still 1 credit, while `get_referral_info:744` points keep growing and user copy (`common.py:411-414` "عند جمع 2 نقاط…", `:197-198` congrats on every later activation) promises recurring rewards. **Decide: one-time bonus (fix copy) or recurring (use `% 2 == 0` + per-threshold ledger).**
- 🟢 LOW inconsistency: `get_user_full_details:935` counts **all** referrals (incl. unactivated) vs `get_referral_info:744` counting `is_counted=1` — admin report overstates vs user dashboard.

---

## Pillar 3 — High-Load Concurrency & Anti-DoS

### 3.1 Database — 🔴 CRITICAL: per-call connections, single writer, no queue

**F-DB1 — one thread per connection, no pool/semaphore/lock.**
`database.py:102-116` `_db_connect()` opens a fresh `aiosqlite.connect()` (→ one dedicated thread) + 3 PRAGMA round-trips per call; 28 call sites (`database.py:150,270,293,357,377,395,467,477,482,488,504,542,618,648,721,743,756,811,820,850,911,954,966,984,1001,1027,1048,1079`); zero `Semaphore`/`Lock`/pool in `database.py` (the repo's only `Semaphore` is broadcast's at `admin.py:1205`).
Quantified at 5000 concurrent users: 5000 × 1 connection ≈ **5000 threads** (~GBs virtual stack + GIL/context-switch storm); fan-out handlers multiply it (one `b1_parts` render = up to 15 `get_content` probes + 15 `is_text_completed` connections ≈ 30 concurrent connects per click → worst case ~50k simultaneous threads/connections). Each also holds the DB fd + WAL/SHM handles (Windows `LockFileEx` pressure) and pays 3 PRAGMAs before the real query.

**F-DB2 — single-writer serialization, 5 s timeout.**
8 writers take `BEGIN IMMEDIATE` (exactly 1 writer at a time; readers proceed in WAL): `activate_subscription:548` (longest — ~6 statements incl. referral sub-flow), `generate_new_key:622`, `set_user_referrer:652`, `process_referral_reward:725`, `claim_free_subscription:760`, `update_custom_text:824`, `revoke_key:1051`, `revoke_key_by_rowid:1082`. A second `BEGIN IMMEDIATE` under contention gets `SQLITE_BUSY` retried internally until `busy_timeout=5000`, then raises `OperationalError: database is locked`. Writer hold ≈ 5–50 ms → ceiling ≈ 20–200 writers/s. But **every quiz answer fires `record_answer_stat`** (`database.py:355-373`: `INSERT OR IGNORE` + `UPDATE` + commit): 5000 users answering ≈ queue depth 5000 × 20 ms ≈ 100 s of serialization ≫ 5 s timeout → **mass `SQLITE_BUSY`**. The global handler swallows them — stats silently lost; activation path surfaces only a generic error (`common.py:178-181`).

**F-DB3 — `PRAGMA journal_mode=WAL` on every connection** (`database.py:111`, redundantly `:153` in `init_db`). `journal_mode` is persistent (set once); per-request re-set is a metadata write needing brief exclusive access — a pointless global serialization point on all 28 call sites. (`busy_timeout`/`synchronous` are correctly per-connection.)

**F-DB4 — read/write amplification per click (no batching).**
One quiz answer = `get_content` probe + `is_free_content→get_catalog` + `can_access_level→get_user_subscription` (up to 2 queries) + `record_answer_stat` write; expired users add paywall text → `get_current_prices` = **3 sequential connections** (`database.py:863-890`) → ~5–6 connections + 2 mtime probes per click. `get_next_uncompleted_target` (`database.py:513-536`) loops skills×teils×files with **sequential** `is_text_completed` (`:530`) — up to ~159 round-trips for one `user_progress` render (`common.py:480`). At 0.5 s throttle ≈ 2 clicks/s/user, 5000 quiz users ≈ 1.5–10k req/s × ~4 conns ≈ **6–40k conn-opens/s × 3 PRAGMAs ≈ 20–120k statements/s** against one SQLite file. Not survivable.

### 3.2 Blocking I/O in async paths — mostly wrapped, 2 misses + 1 amplifier

| Site | Call | Wrapped? | Verdict |
|---|---|---|---|
| `utils.py:43,47,52` mtime probe | via `to_thread` at `:125`,`:147` | ✅ | OK — but runs on **every** `get_catalog` **and every** `get_content` (amplification, F-IO3) |
| `utils.py:72-105` catalog rebuild (~13 dirs + 159 names, ~2–10 ms SSD) | via `to_thread` at `:130` | ✅ | OK in isolation; no TTL — rebuild gated only by mtime compare |
| `utils.py:157-160` `find_directory_case_insensitive` (`os.listdir` at `:216`) | via `to_thread` | ✅ | OK at the only live call site. Dead import `database.py:11` — any future direct call blocks the loop |
| `utils.py:171` `is_file()` stat | via `to_thread` | ✅ | OK |
| `utils.py:175-178` `open()` + `json.load` | via `to_thread` at `:178` | ✅ | OK |
| `utils.py:163-164` `Path.resolve()` ×2 inside `async get_content` | **direct on loop** | ❌ | 🟡 **F-IO1** — `resolve()` = stat/symlink syscalls (~0.05–0.2 ms each Windows) ×2 per `get_content` × 10–15-way gather = 0.5–6 ms pure loop block per list render |
| `common.py:575,616` `os.path.exists` (PDFs) | via `to_thread` | ✅ | OK (`os.path.join`/`basename` elsewhere are pure string ops — explicit non-findings) |
| `common.py:577-578,618-619` `FSInputFile` + `send_document` (~0.5 MB PDFs) | **not offloaded** | ❌ | 🟡 **F-IO2** — file read + TLS send on the loop thread (~1–10 ms each; concurrent clicks serialize the loop) |
| `quiz.py` all `get_content` calls (`:166,217,272,460,543,788`) | inherits wrapping | ✅ | Clean — no direct `listdir/walk/open` in quiz handlers |
| `check_json.py:9,14-15` `os.walk/open/json.load` | offline script, never imported | — | Out of scope |

🟠 **F-IO3 — `to_thread` + `gather` fan-out into a fixed pool.** `common.py:683-706` / `:742-764`: one list render = ~1 catalog probe + tips query + up to **15 content probes + 15 concurrent DB connections** through the default `to_thread` pool (≈ 8–32 threads). 5000 concurrent renders ≈ 75k tasks + 75k connects queued behind ~32 threads → p99 ms→s. `user_progress → get_next_uncompleted_target` is worse (sequential 159×, F-DB4).
🟡 **F-IO4 — cache-invalidation thundering herd, no singleflight.** Any mtime touch wipes **all** `_CONTENT_CACHE` (`utils.py:147-150`); next N concurrent requests all miss and re-parse simultaneously. Same for catalog rebuild (`:127-133`).

### 3.3 Memory — unbounded structures

| Cache | Bound/TTL? | Growth | Severity |
|---|---|---|---|
| `utils.py:9` `FILENAME_MAP` (`map_filename:17-21`, no delete/TTL/cap; minted per file button `common.py:709,767`) | ❌ none | ≈159 today (server-controlled names); stale hashes live forever; restart empties map → old buttons 404 | 🟢 LOW today → 🟡 if corpus churns |
| `utils.py:33` `_CONTENT_CACHE` (`:181`, composite key — collision-safe) | ❌ none | one parsed JSON per file forever (~291 KB disk → ~0.5–1.5 MB live today; linear with corpus) | 🟡 MEDIUM (🔴 if data grows to thousands) |
| `utils.py:29-30` `_CATALOG_CACHE` | single-entry + mtime gate | ~159 strings | ✅ OK |
| `throttling.py:12` `TTLCache(10000, 2.0)` — 2 keys/user → **fills at 5000 users**; overflow evicts live users → throttle bypass exactly when most needed | ✅ TTL+LRU | capped, wrong ceiling | 🟡 LOW-MEDIUM |
| `admin.py:20` `genkey_ratelimit TTLCache(100, 60)` | ✅ | admin-only | 🟢 LOW |
| `common.py:35` `support_rate_limit TTLCache(10000, 300)` | ✅ | 10k ticket-senders; same LRU-bypass shape | 🟢 LOW-MEDIUM |

`TTLCache` async-safety: single-op GIL-atomic; every check-then-set here has **no `await` between** (`throttling.py:33→45`, `admin.py:42-45`, `common.py:288-298`) → safe on a single-threaded loop **today**. Breaks under multi-process workers (per-process caches, limits don't aggregate) and any future intervening `await`. LOW hardening note, not a live race.
🟡 **F-MEM1 — broadcast RAM + stall:** `SELECT user_id FROM user_profiles` + `fetchall()` (`admin.py:1303-1305`) materializes all recipients (~6 MB/100k, ~60 MB/1M, no streaming); `_send_one` holds `Semaphore(30)` **across `sleep(retry_after)`** (`:1208,1214`) — a 429 wave parks all 30 slots (30 × retry_after stall); no pacing (30 concurrent vs ~30 msg/s limit ⇒ 429s routine, single retry); bare `create_task` (`:1312`) — no dedup/cancel/done-callback. 100k users at 30/s ≈ 55 min holding the task.

### 3.4 Throttling middleware — correct policy, narrow coverage

Exact policy (`throttling.py:9,12,31-45`): 1 message/0.5 s **and** 1 callback/0.5 s independently (≈ 4 events/s/user; burst 2). Dropped messages silent, callbacks alerted (`:38-42`, itself guarded). Ordering correct: dispatcher-level throttle runs before router auth, so floods never touch the DB.
🟠 **F-TH1 — registered on `message` + `callback_query` only** (`main.py:57-59`). aiogram stacks are per-update-type: `channel_post` (has a live handler `common.py:88-90`), `inline_query`, `my_chat_member`, errors (`dp.errors` runs outside the chain — mass-`SQLITE_BUSY` log storms unthrottled) all run with zero protection. The `else` branch (`throttling.py:29-30`) is dead code until registered wider. No per-chat/global/burst-bucket limits; rotating-ID floods get fresh budgets each.

---

## Architectural Recommendations for Scaling (exact)

**P0 — do before any load (each is a numbered work item):**
1. **Serialize SQLite writes.** Add one module-level `asyncio.Lock` (e.g. `_WRITE_LOCK` in `database.py`) acquired around every `BEGIN IMMEDIATE` section, or better a dedicated single-writer task + `asyncio.Queue`. Keep `busy_timeout=5000` as backstop, not primary. Expected effect: converts `SQLITE_BUSY` failures into bounded queue latency; measure p95 write-hold time to size it.
2. **Move `PRAGMA journal_mode=WAL` to `init_db` only.** Keep `busy_timeout` + `synchronous=NORMAL` per-connection. Removes a per-request exclusive-access serialization point.
3. **Fix `view_full_text` gate** (`quiz.py:158`): pass the FSM's actual `current_index` instead of hardcoded `0`. One-line change, closes the premium-text leak.
4. **Guard startup** (`main.py:42,76`): wrap `set_my_commands` (default scope) and `delete_webhook` in retry-with-backoff (3 attempts) instead of crashing `main()`.
5. **Give `AuthState` an exit:** accept `/cancel` (and `main_menu` button) in `process_key`; stash `{skill,teil,file_name,current_index}` on paywall entry and resume-or-clear deterministically (fixes wrong-text resume too).
6. **Decide the referral economy:** one-time bonus (rewrite `common.py:197-198,411-414` copy) or recurring (replace `== 2` with threshold ledger). Also move the self/existence checks before the `is_counted` flip and add `AND credits_count > 0 ... RETURNING` to the credit decrement.

**P1 — needed for 1k+ concurrent users:**
7. **Kill connection fan-out.** Batch `b1_parts` completion checks into one connection (`WHERE text_id IN (...)` + single `COUNT`), cache `get_current_prices` briefly (TTL ~60 s — currently 3 sequential conns per paywall render), rewrite `get_next_uncompleted_target` to a single `SELECT text_id ... WHERE user_id=?` + in-memory set difference (replaces ~159 sequential round-trips).
8. **Bound the caches.** Replace `_CONTENT_CACHE`/`FILENAME_MAP` plain dicts with `TTLCache`/`LRUCache` (e.g. content 500 entries / 10 min; filename map keyed with expiry), or precompute the `{skill/teil/file: title}` index at startup with mtime invalidation. Add singleflight (per-key `asyncio.Lock` or `Future` dedup) on catalog/content rebuild.
9. **Fix the event-loop misses.** Move `Path.resolve()` (`utils.py:163-164`) into the worker thread; offload `FSInputFile`/PDF sends (`common.py:577-578,618-619`) via `to_thread` or pre-read bytes at startup (PDFs ~0.5 MB each — cache in RAM).
10. **Harden broadcast.** Stream recipients (cursor, not `fetchall`); pace to ~25/s + jitter; release the semaphore **before** `sleep(retry_after)`; single-flight guard + stored task handle with done-callback + admin cancel; track `success/failed/blocked` separately (`Forbidden` vs `RetryAfter` vs `BadRequest`).

**P2 — needed for 5k+ / multi-instance:**
11. **Throttle everywhere.** Register the middleware on `dp.update` (or every stack), raise `maxsize` (≥ 25k) or shard per-type caches, add a global-RPS guard + burst bucket. If scaling past one process, move throttle + FSM to Redis (per `agent.md` Rule 5 direction) — per-process `TTLCache` limits don't aggregate.
12. **Split the DB.** At minimum: separate read/write connections with WAL; realistically migrate financial/write-heavy tables to Postgres (connection pool, e.g. `asyncpg` pool 10–20) while SQLite keeps serving read-mostly quiz content — or go full Postgres. SQLite's single-writer ceiling (~20–200 writers/s here) is a hard wall; `record_answer_stat`-per-answer guarantees hitting it.
13. **Make failures visible.** Replace global-handle-and-swallow for user-facing paths with a fallback notice ("⚠️ حدث خطأ، حاول مجدداً"); add counters (success/fail per handler, `SQLITE_BUSY` count, 429 count) + alert on thresholds. Today's silence will hide the first overload until users leave.

**Explicit non-findings (checked, clean — do not "fix"):** `os.path.join`/`basename` are pure string ops (not blocking); quiz handlers contain no direct `listdir/walk/open`; `check_json.py` is offline-only; money SQL is fully parameterized with the single dynamic-SQL site allowlisted; key/referral claims are atomic `UPDATE...RETURNING`; path traversal is contained via `basename` + `is_relative_to(data/)` + `is_file`.

---

*End of report_1.md — Pre-Flight & Stress Audit. Three pillars covered; severities assigned with file:line evidence; P0/P1/P2 scaling roadmap above.*
