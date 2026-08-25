"""SQLite-backed simulation queue and the worker that drains it.

A batch of forty alphas outlives a 4-hour BRAIN session and, on a laptop, very
likely outlives the process too. So the queue lives on disk beside the results
rather than in memory, and the worker re-checks the session before every bundle.

State machine:

    queued ──► running ──► done
                   │  └──► failed
                   └─────► interrupted   (process died mid-flight)
    queued ──► cancelled

``interrupted`` exists because a row left ``running`` at startup means the
process died after submitting. The simulation may well have completed on BRAIN's
side, so retrying automatically would burn quota on work already done -- it takes
an explicit /queue retry.
"""

import asyncio
import logging
import sqlite3
import uuid
from typing import Optional

from bot.alpha_spec import AlphaSpec
from bot.formatting import utc_now

log = logging.getLogger(__name__)

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
INTERRUPTED = "interrupted"
CANCELLED = "cancelled"

PENDING_STATES = (QUEUED,)
RETRYABLE_STATES = (INTERRUPTED, FAILED)


def new_batch_id() -> str:
    return uuid.uuid4().hex[:8]


class SimQueue:
    """Persistence for queued alphas. All methods are synchronous and cheap."""

    def __init__(self, store) -> None:
        self._store = store

    def _connect(self) -> sqlite3.Connection:
        return self._store._connect()

    # ------------------------------------------------------------------ writes

    def enqueue(self, specs: list[AlphaSpec], chat_id: int, batch_id: str) -> int:
        now = utc_now().isoformat(timespec="seconds")
        rows = [
            {
                "batch_id": batch_id,
                "chat_id": chat_id,
                "expression": s.expression,
                "region": s.region,
                "universe": s.universe,
                "delay": s.delay,
                "decay": s.decay,
                "neutralization": s.neutralization,
                "truncation": s.truncation,
                "test_period": s.test_period,
                "state": QUEUED,
                "queued_at": now,
            }
            for s in specs
        ]
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO sim_queue
                    (batch_id, chat_id, expression, region, universe, delay,
                     decay, neutralization, truncation, test_period, state, queued_at)
                VALUES
                    (:batch_id, :chat_id, :expression, :region, :universe, :delay,
                     :decay, :neutralization, :truncation, :test_period, :state, :queued_at)
                """,
                rows,
            )
        return len(rows)

    def reset_interrupted(self) -> int:
        """Called once at startup. Rows stuck in `running` had their process die."""
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE sim_queue SET state = ?, finished_at = ? WHERE state = ?",
                (INTERRUPTED, utc_now().isoformat(timespec="seconds"), RUNNING),
            )
        if cursor.rowcount:
            log.warning("%d queued simulations were interrupted", cursor.rowcount)
        return cursor.rowcount

    def claim_next_bundle(self, max_size: int) -> list[sqlite3.Row]:
        """Take the oldest queued rows that can share a multi-simulation.

        The bundle key (region, delay, universe) is taken from the oldest queued
        row, then matching rows are pulled in FIFO order. Claim and mark happen
        in one transaction so two workers cannot take the same rows.
        """
        with self._connect() as conn:
            head = conn.execute(
                "SELECT region, delay, universe FROM sim_queue "
                "WHERE state = ? ORDER BY id LIMIT 1",
                (QUEUED,),
            ).fetchone()
            if head is None:
                return []

            rows = conn.execute(
                """
                SELECT * FROM sim_queue
                WHERE state = ? AND region IS ? AND delay IS ? AND universe IS ?
                ORDER BY id LIMIT ?
                """,
                (QUEUED, head["region"], head["delay"], head["universe"], max_size),
            ).fetchall()

            conn.executemany(
                "UPDATE sim_queue SET state = ?, started_at = ? WHERE id = ?",
                [
                    (RUNNING, utc_now().isoformat(timespec="seconds"), r["id"])
                    for r in rows
                ],
            )
        return rows

    def finish(self, row_id: int, *, alpha_id=None, error=None) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE sim_queue SET state = ?, finished_at = ?, alpha_id = ?, "
                "error = ? WHERE id = ?",
                (
                    FAILED if error else DONE,
                    utc_now().isoformat(timespec="seconds"),
                    alpha_id,
                    error,
                    row_id,
                ),
            )

    def cancel_pending(self) -> int:
        """Cancel everything still queued. Running and finished rows are untouched."""
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE sim_queue SET state = ?, finished_at = ? WHERE state = ?",
                (CANCELLED, utc_now().isoformat(timespec="seconds"), QUEUED),
            )
        return cursor.rowcount

    def retry(self) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                f"UPDATE sim_queue SET state = ?, started_at = NULL, "
                f"finished_at = NULL, error = NULL "
                f"WHERE state IN ({','.join('?' * len(RETRYABLE_STATES))})",
                (QUEUED, *RETRYABLE_STATES),
            )
        return cursor.rowcount

    # ------------------------------------------------------------------- reads

    def counts(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT state, COUNT(*) AS n FROM sim_queue GROUP BY state"
            ).fetchall()
        return {r["state"]: r["n"] for r in rows}

    def pending_count(self) -> int:
        return self.counts().get(QUEUED, 0)

    def batch_progress(self, batch_id: str) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT state, COUNT(*) AS n FROM sim_queue "
                "WHERE batch_id = ? GROUP BY state",
                (batch_id,),
            ).fetchall()
        return {r["state"]: r["n"] for r in rows}

    def active_batches(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT batch_id FROM sim_queue WHERE state IN (?, ?) "
                "ORDER BY batch_id",
                (QUEUED, RUNNING),
            ).fetchall()
        return [r["batch_id"] for r in rows]


def row_to_spec(row: sqlite3.Row) -> AlphaSpec:
    return AlphaSpec(
        expression=row["expression"],
        region=row["region"],
        universe=row["universe"],
        delay=row["delay"],
        decay=row["decay"],
        neutralization=row["neutralization"],
        truncation=row["truncation"],
        test_period=row["test_period"],
    )


class QueueWorker:
    """Drains the queue one bundle at a time.

    Deliberately a single task: BRAIN's concurrency ceiling is enforced by
    ``SimulationRunner``'s semaphore, which /sim shares, so a second worker would
    only contend for the same slots while making progress reporting incoherent.
    """

    def __init__(self, queue: SimQueue, runner, brain, store, reporter, *, bundle_size: int):
        self._queue = queue
        self._runner = runner
        self._brain = brain
        self._store = store
        self._reporter = reporter
        self._bundle_size = bundle_size
        self._task: Optional[asyncio.Task] = None
        self._wake = asyncio.Event()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.running:
            return
        self._task = asyncio.create_task(self._loop(), name="queue-worker")
        log.info("Queue worker started")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        log.info("Queue worker stopped")

    def nudge(self) -> None:
        """Wake the worker immediately -- called after enqueueing."""
        self._wake.set()

    async def _loop(self) -> None:
        while True:
            bundle = None
            try:
                bundle = await asyncio.to_thread(
                    self._queue.claim_next_bundle, self._bundle_size
                )
                if not bundle:
                    self._wake.clear()
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=30)
                    except asyncio.TimeoutError:
                        pass
                    continue
                await self._run_bundle(bundle)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 -- the worker must never die
                if bundle:
                    for row in bundle:
                        await asyncio.to_thread(
                            self._queue.finish, row["id"], error="worker error"
                        )
                log.exception("Queue worker iteration failed")
                await asyncio.sleep(5)

    async def _run_bundle(self, rows: list[sqlite3.Row]) -> None:
        from bot.simulation import MIN_SESSION_SECONDS, run_batch

        chat_id = rows[0]["chat_id"]
        batch_id = rows[0]["batch_id"]
        specs = [row_to_spec(r) for r in rows]

        # Before every bundle, not once per batch: a long queue outlives a
        # session, and ACE's own refresh is disarmed by install_session_guards.
        fresh = await self._brain.ensure_fresh(
            MIN_SESSION_SECONDS, self._reporter.persona_callback(chat_id)
        )
        if not fresh:
            for row in rows:
                await asyncio.to_thread(
                    self._queue.finish, row["id"], error="session could not be refreshed"
                )
            await self._reporter.session_lost(chat_id)
            return

        async with self._runner.slot():
            outcomes = await run_batch(self._brain, specs)

        for row, outcome in zip(rows, outcomes):
            if outcome.ok and outcome.alpha_id:
                await asyncio.to_thread(self._store.record, outcome)
                await asyncio.to_thread(
                    self._queue.finish, row["id"], alpha_id=outcome.alpha_id
                )
            else:
                await asyncio.to_thread(
                    self._queue.finish, row["id"], error=outcome.error or "unknown"
                )

        await self._reporter.bundle_done(chat_id, batch_id, outcomes)
