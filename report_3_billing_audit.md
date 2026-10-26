# Report 3 — Billing Pentest Sign-Off (Payment / Subscription / Referral)

**Date (UTC):** 2026-10-11
**Harness:** `pentest_billing.py` (repo root) — async script on an isolated temp DB (`init_db` schema, real `data/b1` catalog, production code paths, zero mocks on the billing logic). Exit code 0.
**Result: 12/12 assertions PASS. No code changes required.**

## Attack results

| # | Attack | Method | Result |
|---|---|---|---|
| T1 | Subscription Stacking (time manipulation) | Activate two 30-day keys back-to-back for one user | ✅ PASS — expiry stacks to exactly +60.0000d (`max(current_utc, current_expire) + timedelta`, agent.md Rule 1). Overwrite impossible. |
| T2 | Referral double-spend race | 10 concurrent `process_referral_reward` for one referred user via `asyncio.gather` | ✅ PASS — exactly 1 winner, referrer = 1 point, 1 counted row. `_db_write` serialization + `BEGIN IMMEDIATE` + atomic `UPDATE...RETURNING` hold. |
| T3 | Ghost & self-referral bypass | `set_user_referrer(X, X)`; reward for nonexistent referrer `919999888` | ✅ PASS — both yield `(None, 0)`; ghost row stays `is_counted=0` (NOT burned); the same row claims exactly once after the referrer appears. Validation-before-flip holds. |
| T4 | Free-tier AuthZ boundary | `is_free_content` on real first hören file at indexes 5 / 6 / unknown file | ✅ PASS — idx5 free, idx6 paid, unknown file never free. |

## Noted deviation (faithful to intent)

The brief names `'file.json'` for T4, but no such file exists in the real catalog — and `is_free_content` correctly returns `False` for unknown files (fail-closed, asserted as T4c). The 5-free/6-paid boundary was therefore proven against the real first file of hören/teil1, which is the exact gate the `view_full_text` fix relies on.

## Code fixes applied

**None.** Every assertion passed on the first logic run (the single re-run fixed only a cp1256 console-encoding artifact in the test script's own `print`, not billing logic).

## Sign-off

Payment, Subscription, and Referral systems are **logically and concurrency-proof against all four attack vectors**: no time-overwrite, no double-spend, no ghost/self farming, no free-tier bypass. Architecture complies with agent.md Rules 1 (ISO-8601 UTC, stacking expiry), 3 (BEGIN IMMEDIATE atomicity, parameterized queries, idempotent verified rewards) and 14 (explicit SKU mapping in `can_access_level`).

*End of report_3 — billing audit sign-off.*
