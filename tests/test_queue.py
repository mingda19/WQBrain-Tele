"""Tests for the persistent queue, the worker loop, and multi-simulation.

The queue's whole reason to exist is surviving things that are awkward to
reproduce by hand -- a process dying mid-batch, a session expiring halfway
through forty alphas -- so those are the cases driven hardest here.

Run: python -m pytest tests/ -q
"""

import asyncio

import pandas as pd
import pytest

from bot import simulation
from bot.alpha_spec import AlphaSpec
from bot.simqueue import (
    CANCELLED,
    DONE,
    FAILED,
    INTERRUPTED,
    QUEUED,
    RUNNING,
    QueueWorker,
    SimQueue,
    new_batch_id,
    row_to_spec,
)
from bot.simulation import SimOutcome, SimulationRunner, run_batch
from bot.store import AlphaStore

CHAT = 4242


@pytest.fixture
def store(tmp_path):
    return AlphaStore(tmp_path / "alphas.db")


@pytest.fixture
def queue(store):
    return SimQueue(store)


def specs(n, **kwargs):
    return [AlphaSpec(expression=f"expr_{i}", **kwargs) for i in range(n)]


# ------------------------------------------------------------------ the queue


def test_enqueue_then_claim_marks_running(queue):
    queue.enqueue(specs(3), CHAT, "b1")
    assert queue.counts() == {QUEUED: 3}

    claimed = queue.claim_next_bundle(10)

    assert len(claimed) == 3
    assert queue.counts() == {RUNNING: 3}
    assert [r["expression"] for r in claimed] == ["expr_0", "expr_1", "expr_2"]


def test_claim_respects_the_bundle_size(queue):
    queue.enqueue(specs(25), CHAT, "b1")
    assert len(queue.claim_next_bundle(10)) == 10
    assert queue.counts() == {QUEUED: 15, RUNNING: 10}


def test_claim_never_mixes_region_or_delay(queue):
    """A bundle that mixes markets would be rejected by BRAIN."""
    queue.enqueue(specs(2, region="USA", delay=1), CHAT, "b1")
    queue.enqueue(specs(3, region="CHN", delay=1), CHAT, "b1")

    first = queue.claim_next_bundle(10)
    second = queue.claim_next_bundle(10)

    assert {r["region"] for r in first} == {"USA"}
    assert {r["region"] for r in second} == {"CHN"}
    assert len(first) == 2 and len(second) == 3


def test_claim_is_fifo(queue):
    queue.enqueue(specs(2), CHAT, "b1")
    queue.enqueue([AlphaSpec(expression="later")], CHAT, "b2")

    assert [r["expression"] for r in queue.claim_next_bundle(2)] == ["expr_0", "expr_1"]
    assert [r["expression"] for r in queue.claim_next_bundle(2)] == ["later"]


def test_claim_on_empty_queue_returns_nothing(queue):
    assert queue.claim_next_bundle(10) == []


def test_finish_records_success_and_failure(queue):
    queue.enqueue(specs(2), CHAT, "b1")
    rows = queue.claim_next_bundle(10)

    queue.finish(rows[0]["id"], alpha_id="AAA")
    queue.finish(rows[1]["id"], error="bad expression")

    assert queue.counts() == {DONE: 1, FAILED: 1}


def test_restart_parks_running_rows_as_interrupted(queue):
    """The process died mid-flight; the simulation may have completed anyway."""
    queue.enqueue(specs(5), CHAT, "b1")
    queue.claim_next_bundle(3)

    assert queue.reset_interrupted() == 3
    assert queue.counts() == {QUEUED: 2, INTERRUPTED: 3}


def test_restart_leaves_settled_rows_alone(queue):
    queue.enqueue(specs(2), CHAT, "b1")
    rows = queue.claim_next_bundle(10)
    queue.finish(rows[0]["id"], alpha_id="AAA")

    queue.reset_interrupted()

    assert queue.counts() == {DONE: 1, INTERRUPTED: 1}


def test_cancel_only_touches_queued_rows(queue):
    queue.enqueue(specs(5), CHAT, "b1")
    rows = queue.claim_next_bundle(2)
    queue.finish(rows[0]["id"], alpha_id="AAA")

    assert queue.cancel_pending() == 3
    counts = queue.counts()
    assert counts[CANCELLED] == 3
    assert counts[DONE] == 1
    assert counts[RUNNING] == 1, "a running simulation must not be cancelled"


def test_retry_requeues_interrupted_and_failed_only(queue):
    queue.enqueue(specs(4), CHAT, "b1")
    rows = queue.claim_next_bundle(4)
    queue.finish(rows[0]["id"], alpha_id="AAA")
    queue.finish(rows[1]["id"], error="boom")
    queue.reset_interrupted()  # rows 2 and 3
    queue.cancel_pending()

    assert queue.retry() == 3  # 1 failed + 2 interrupted
    counts = queue.counts()
    assert counts[QUEUED] == 3
    assert counts[DONE] == 1


def test_retry_clears_the_previous_error(queue):
    queue.enqueue(specs(1), CHAT, "b1")
    row = queue.claim_next_bundle(1)[0]
    queue.finish(row["id"], error="boom")
    queue.retry()

    claimed = queue.claim_next_bundle(1)[0]
    assert claimed["error"] is None
    assert claimed["finished_at"] is None


def test_batch_progress_and_active_batches(queue):
    queue.enqueue(specs(3), CHAT, "aaa")
    queue.enqueue(specs(2), CHAT, "bbb")
    rows = queue.claim_next_bundle(10)
    queue.finish(rows[0]["id"], alpha_id="A")

    assert queue.active_batches() == ["aaa", "bbb"]
    assert queue.batch_progress("aaa")[DONE] == 1


def test_row_round_trips_to_a_spec(queue):
    original = AlphaSpec(
        expression="ts_sum(vec_avg(x),120)", region="CHN", universe="TOP2000U",
        delay=0, decay=6, neutralization="MARKET", truncation=0.05, test_period="P2Y",
    )
    queue.enqueue([original], CHAT, "b1")
    assert row_to_spec(queue.claim_next_bundle(1)[0]) == original


def test_batch_ids_are_distinct():
    assert len({new_batch_id() for _ in range(50)}) == 50


# ------------------------------------------------------------------ run_batch


@pytest.fixture
def is_stats():
    return pd.DataFrame([{"sharpe": 1.9, "fitness": 1.2, "turnover": 0.2,
                          "drawdown": 0.05, "margin": 0.001, "returns": 0.1,
                          "pnl": 100, "longCount": 10, "shortCount": 10}])


@pytest.fixture
def is_tests():
    return pd.DataFrame([{"name": "LOW_SHARPE", "result": "PASS", "limit": 1.58, "value": 1.9}])


class FakeBrain:
    def __init__(self, submitted=None, stats=None, multi_error=None):
        self.submitted = submitted
        self.stats = stats
        self.multi_error = multi_error
        self.calls = []

    async def run_ace(self, func, *args, **kwargs):
        name = getattr(func, "__name__", "lambda")
        self.calls.append(name)
        if name == "simulate_multi_alpha":
            if self.multi_error:
                raise self.multi_error
            return self.submitted
        if name == "simulate_single_alpha":
            return {"alpha_id": f"single_{len(self.calls)}", "simulate_data": {}}
        return self.stats


def test_run_batch_returns_one_outcome_per_spec(is_stats, is_tests):
    batch = specs(3)
    brain = FakeBrain(
        submitted=[{"alpha_id": f"A{i}", "simulate_data": {}} for i in range(3)],
        stats={"is_stats": is_stats, "is_tests": is_tests},
    )

    outcomes = asyncio.run(run_batch(brain, batch))

    assert [o.alpha_id for o in outcomes] == ["A0", "A1", "A2"]
    assert all(o.ok for o in outcomes)
    assert [o.spec.expression for o in outcomes] == ["expr_0", "expr_1", "expr_2"]
    assert brain.calls.count("simulate_multi_alpha") == 1


def test_run_batch_marks_only_the_rejected_alpha(is_stats, is_tests):
    batch = specs(3)
    brain = FakeBrain(
        submitted=[
            {"alpha_id": "A0", "simulate_data": {}},
            {"alpha_id": None, "simulate_data": {}},
            {"alpha_id": "A2", "simulate_data": {}},
        ],
        stats={"is_stats": is_stats, "is_tests": is_tests},
    )

    outcomes = asyncio.run(run_batch(brain, batch))

    assert [o.ok for o in outcomes] == [True, False, True]
    assert "rejected" in outcomes[1].error


def test_run_batch_falls_back_when_ace_raises_type_error(is_stats, is_tests):
    """ace_lib.py:494 does len() on an int when a response has no children."""
    batch = specs(3)
    brain = FakeBrain(multi_error=TypeError("object of type 'int' has no len()"),
                      stats={"is_stats": is_stats, "is_tests": is_tests})

    outcomes = asyncio.run(run_batch(brain, batch))

    assert len(outcomes) == 3
    assert all(o.ok for o in outcomes)
    assert brain.calls.count("simulate_single_alpha") == 3, "must not lose the bundle"


def test_run_batch_falls_back_on_a_length_mismatch(is_stats, is_tests):
    brain = FakeBrain(
        submitted=[{"alpha_id": "A0", "simulate_data": {}}],  # 1 result for 3 alphas
        stats={"is_stats": is_stats, "is_tests": is_tests},
    )
    outcomes = asyncio.run(run_batch(brain, specs(3)))

    assert len(outcomes) == 3
    assert brain.calls.count("simulate_single_alpha") == 3


def test_run_batch_reports_a_submission_failure_per_spec():
    brain = FakeBrain(multi_error=RuntimeError("network down"))
    outcomes = asyncio.run(run_batch(brain, specs(3)))

    assert len(outcomes) == 3
    assert all(not o.ok and "network down" in o.error for o in outcomes)


def test_run_batch_of_one_uses_the_single_path(is_stats, is_tests):
    brain = FakeBrain(stats={"is_stats": is_stats, "is_tests": is_tests})
    outcomes = asyncio.run(run_batch(brain, specs(1)))

    assert len(outcomes) == 1
    assert "simulate_multi_alpha" not in brain.calls


def test_run_batch_of_nothing_is_empty():
    assert asyncio.run(run_batch(FakeBrain(), [])) == []


# --------------------------------------------------------------- the worker


class FakeReporter:
    def __init__(self):
        self.bundles = []
        self.session_lost_calls = 0

    def persona_callback(self, chat_id):
        async def _cb(url):
            return None

        return _cb

    async def bundle_done(self, chat_id, batch_id, outcomes):
        self.bundles.append((batch_id, outcomes))

    async def session_lost(self, chat_id):
        self.session_lost_calls += 1


class FakeSessionBrain:
    def __init__(self, fresh=True):
        self.fresh = fresh

    async def ensure_fresh(self, min_seconds, on_persona):
        return self.fresh

    async def run_ace(self, func, *args, **kwargs):
        raise AssertionError("run_batch is patched in these tests")


def drain(worker, queue, *, limit=10):
    """Run the worker until the queue stops producing bundles."""

    async def scenario():
        for _ in range(limit):
            bundle = queue.claim_next_bundle(worker._bundle_size)
            if not bundle:
                return
            await worker._run_bundle(bundle)

    asyncio.run(scenario())


def build_worker(queue, store, brain, reporter, monkeypatch, outcomes_for):
    async def fake_run_batch(_brain, batch_specs):
        return outcomes_for(batch_specs)

    monkeypatch.setattr(simulation, "run_batch", fake_run_batch)
    return QueueWorker(
        queue, SimulationRunner(brain, 2), brain, store, reporter, bundle_size=10
    )


def test_worker_drains_the_queue_and_records_results(queue, store, monkeypatch):
    queue.enqueue(specs(3), CHAT, "b1")
    reporter = FakeReporter()

    def outcomes_for(batch):
        return [
            SimOutcome(spec=s, ok=True, alpha_id=f"A{i}", metrics={"sharpe": 1.0},
                       tests=[{"name": "T", "result": "PASS", "limit": None, "value": None}])
            for i, s in enumerate(batch)
        ]

    worker = build_worker(queue, store, FakeSessionBrain(), reporter, monkeypatch, outcomes_for)
    drain(worker, queue)

    assert queue.counts() == {DONE: 3}
    assert store.count() == 3
    assert len(reporter.bundles) == 1


def test_worker_marks_rejected_alphas_failed_without_storing(queue, store, monkeypatch):
    queue.enqueue(specs(2), CHAT, "b1")
    reporter = FakeReporter()

    def outcomes_for(batch):
        return [
            SimOutcome(spec=batch[0], ok=True, alpha_id="A0", metrics={"sharpe": 1.0},
                       tests=[{"name": "T", "result": "PASS", "limit": None, "value": None}]),
            SimOutcome(spec=batch[1], ok=False, error="syntax error"),
        ]

    worker = build_worker(queue, store, FakeSessionBrain(), reporter, monkeypatch, outcomes_for)
    drain(worker, queue)

    assert queue.counts() == {DONE: 1, FAILED: 1}
    assert store.count() == 1


def test_worker_stops_the_bundle_when_the_session_cannot_refresh(queue, store, monkeypatch):
    """A long queue outlives a 4h session; failing loudly beats silent nonsense."""
    queue.enqueue(specs(3), CHAT, "b1")
    reporter = FakeReporter()

    worker = build_worker(
        queue, store, FakeSessionBrain(fresh=False), reporter, monkeypatch,
        lambda batch: [],
    )
    drain(worker, queue)

    assert queue.counts() == {FAILED: 3}
    assert store.count() == 0
    assert reporter.session_lost_calls == 1


def test_worker_holds_one_concurrency_slot_per_bundle(queue, store, monkeypatch):
    """/sim and the queue share one semaphore, so the account limit holds."""
    queue.enqueue(specs(2), CHAT, "b1")
    reporter = FakeReporter()
    brain = FakeSessionBrain()
    seen = []

    runner = SimulationRunner(brain, 2)

    async def fake_run_batch(_brain, batch_specs):
        seen.append(runner.in_flight)
        return [
            SimOutcome(spec=s, ok=True, alpha_id=f"A{i}", metrics={},
                       tests=[{"name": "T", "result": "PASS", "limit": None, "value": None}])
            for i, s in enumerate(batch_specs)
        ]

    monkeypatch.setattr(simulation, "run_batch", fake_run_batch)
    worker = QueueWorker(queue, runner, brain, store, reporter, bundle_size=10)
    drain(worker, queue)

    assert seen == [1]
    assert runner.in_flight == 0
