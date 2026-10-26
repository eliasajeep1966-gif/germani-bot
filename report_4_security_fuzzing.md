# Report 4 — Security Fuzzing & RBAC Pentest

**Date (UTC):** 2026-10-11
**Harness:** `pentest_fuzzing.py` (repo root) — real `AdminAuthMiddleware`, real quiz handlers, real `ThrottlingMiddleware`, real `Message`/`CallbackQuery` objects for RBAC (API surface stubbed via `patch.object`, zero network), duck-typed fakes for FSM paths, isolated temp DB. Exit code 0.
**Result: 11/11 assertions PASS. No bot-code bugs found (two harness-only artifacts fixed in the script itself).**

## RBAC & privilege escalation (R1–R5) — all blocked, positive control passes

| # | Payload (non-admin `id=950001`) | Result |
|---|---|---|
| R1 | `/users` as `Message` | ✅ Blocked — handler calls 0, denial `answer` sent |
| R2 | `admin_gen_group` as `CallbackQuery` | ✅ Blocked — handler never reached |
| R3 | `admin_financial_settlement` as `CallbackQuery` | ✅ Blocked — handler never reached |
| R4 | `/broadcast` as `Message` | ✅ Blocked — router-level middleware is command-agnostic |
| R5 | `admin_gen_group` as real admin (`ADMIN_IDS[0]`) | ✅ Passes — `reached == ['admin_gen_group']` |

Privilege escalation is impossible through the dispatcher: every admin/supervisor entry sits behind `AdminAuthMiddleware` (agent.md Rule 2), and the middleware denies before any handler runs.

## FSM state confusion fuzzing (F1–F5) — graceful on all inputs

| # | Hostile input | Result |
|---|---|---|
| F1 | `ans_A` with zero state | ✅ Session-expired fallback message, no `KeyError` |
| F2 | `current_index=9999` on free file | ✅ Bounds fallback (`send_quiz_question` guards), no crash |
| F3 | `current_index=9999` on paid file | ✅ Paywall rendered, FSM pointers preserved (Rule 5/13), no crash |
| F4 | Forged `t3_q_Q99` with zero matching state | ✅ Fail-closed paywall, forged ID ignored via server-side membership check, no crash |
| F5 | `current_index=-5` on free file | ✅ "Cannot load" fallback via `_get_ordered_question` guard, no crash |

## Throttling anti-spam (T1) — 50 concurrent same-user events

✅ Exactly **1 proceeds, 49 dropped** (`slow_mode_delay=0.5`). The check-then-set contains no `await`, so concurrent bursts serialize deterministically on the loop — the Telegram API is shielded from 429 floods by construction, and `TTLCache(10000, 2.0)` bounds the tracker (agent.md Rule 8).

## Code fixes applied

**None to bot code.** Two failures during the run were harness artifacts in `pentest_fuzzing.py` itself (cp1256 console encoding of Arabic detail text; a wrong-branch assertion for the duck-typed Teil3 fake), both corrected in the script. Bot behavior was correct in all 11 cases.

## Sign-off

RBAC holds against forged admin payloads, the FSM cannot be crashed or confused into serving premium content, and throttling provably absorbs a 50× burst down to 1 handler call. Fuzzing sign-off granted.

*End of report_4 — security fuzzing sign-off.*
