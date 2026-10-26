"""Ultimate load test: 5000 concurrent heavy-mix users, direct logic hammer.

Each worker mirrors the real `user_progress` + quiz-answer hot path:
  get_next_uncompleted_target -> record_answer_stat (write) ->
  get_skill_progress x2 -> get_user_answer_stats -> get_user_subscription.

NOTE: no `get_user_progress` exists in database.py; the mix above is exactly
what the `user_progress` handler calls, plus the per-answer write.

Isolated temp DB. Asserts 5000/5000 success, 0 SQLITE_BUSY/OperationalError.
Exit code 0 = pass.
"""
import asyncio
import os
import sys
import tempfile
import threading
import time

import aiosqlite

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import database as db

TMP = os.path.join(tempfile.gettempdir(), "stress5000.db")
for suffix in ("", "-wal", "-shm", "-journal"):
    try:
        os.remove(TMP + suffix)
    except OSError:
        pass
db.DB_NAME = TMP  # isolate BEFORE pool creation

N = 5000
BASE_UID = 800000
ok = 0
busy = 0
other_errors = []
lock = threading.Lock()


async def worker(i):
    global ok, busy
    uid = BASE_UID + (i % 1000)  # 1000 distinct users, 5x overlap each
    try:
        await db.get_next_uncompleted_target(uid)
        await db.record_answer_stat(uid, i % 2 == 0)
        await db.get_skill_progress(uid, "lesen")
        await db.get_skill_progress(uid, "hören")
        await db.get_user_answer_stats(uid)
        await db.get_user_subscription(uid)
        with lock:
            ok += 1
    except aiosqlite.OperationalError as e:
        with lock:
            busy += 1
            if len(other_errors) < 5:
                other_errors.append(f"OperationalError: {e}")
    except Exception as e:  # noqa: BLE001
        with lock:
            if len(other_errors) < 5:
                other_errors.append(f"{type(e).__name__}: {e}")


async def main():
    global ok
    await db.init_db()
    threads_before = threading.active_count()
    t0 = time.perf_counter()
    await asyncio.gather(*[worker(i) for i in range(N)])
    dt = time.perf_counter() - t0
    threads_after = threading.active_count()
    pool_ok = db._DB_POOL is not None and db._DB_POOL.qsize() == db._POOL_SIZE
    failed = N - ok
    print(f"workers={N} success={ok} failed={failed} "
          f"sqlite_busy={busy} time={dt:.1f}s "
          f"threads={threads_before}->{threads_after} pool_intact={pool_ok}")
    for e in other_errors:
        print(f"  sample error: {e}")
    await db.close_db()
    assert ok == N, f"only {ok}/{N} succeeded"
    assert busy == 0, f"{busy} SQLITE_BUSY failures"
    assert failed == 0, f"{failed} failed"
    assert pool_ok, "pool leaked connections"
    print(f"STRESS CLEAN -- {N}/{N} ops, 0 busy, {dt:.1f}s "
          f"({N / dt:.0f} users/s, ~{6 * N / dt:.0f} db-ops/s)")


try:
    asyncio.run(main())
finally:
    try:
        asyncio.run(db.close_db())
    except Exception:
        pass
