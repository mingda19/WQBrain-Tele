"""Tests for how a batch reports back.

The rule being enforced: forty finished simulations must not become forty
messages. Full detail is reserved for alphas that pass every check.

Run: python -m pytest tests/ -q
"""

import asyncio

import pytest

from bot.alpha_spec import AlphaSpec
from bot.reporting import QueueReporter
from bot.simqueue import SimQueue
from bot.simulation import SimOutcome
from bot.store import AlphaStore
from tests.fakes import FakeBot

CHAT = 4242

PASS = [{"name": "LOW_SHARPE", "result": "PASS", "limit": 1.58, "value": 1.9}]
FAIL = [{"name": "LOW_SHARPE", "result": "FAIL", "limit": 1.58, "value": 0.4}]


@pytest.fixture
def queue(tmp_path):
    return SimQueue(AlphaStore(tmp_path / "alphas.db"))


@pytest.fixture
def bot():
    return FakeBot()


def outcome(expression, tests, alpha_id="A1"):
    return SimOutcome(
        spec=AlphaSpec(expression=expression),
        ok=True,
        alpha_id=alpha_id,
        metrics={"sharpe": 1.9, "fitness": 1.2},
        tests=tests,
    )


def seed(queue, n, batch_id="b1"):
    queue.enqueue([AlphaSpec(expression=f"e{i}") for i in range(n)], CHAT, batch_id)


def test_only_passing_alphas_get_a_full_message(queue, bot):
    seed(queue, 3)
    rows = queue.claim_next_bundle(10)
    for row in rows:
        queue.finish(row["id"], alpha_id="A")

    reporter = QueueReporter(bot, None, queue)
    outcomes = [
        outcome("winner", PASS, "GOOD"),
        outcome("loser", FAIL, "BAD1"),
        outcome("loser2", FAIL, "BAD2"),
    ]
    asyncio.run(reporter.bundle_done(CHAT, "b1", outcomes))

    texts = bot.texts()
    detailed = [t for t in texts if "Simulation complete" in t]
    assert len(detailed) == 1, "one full report, not three"
    assert "GOOD" in detailed[0]
    assert not any("BAD1" in t for t in detailed)


def test_tally_counts_the_whole_batch(queue, bot):
    seed(queue, 5)
    rows = queue.claim_next_bundle(2)
    queue.finish(rows[0]["id"], alpha_id="A")
    queue.finish(rows[1]["id"], error="nope")

    reporter = QueueReporter(bot, None, queue)
    asyncio.run(
        reporter.bundle_done(CHAT, "b1", [outcome("x", FAIL), outcome("y", FAIL)])
    )

    tally = bot.texts()[-1]
    assert "2/5 done" in tally
    assert "1 simulated" in tally and "1 failed" in tally
    assert "Batch complete" not in tally


def test_final_bundle_announces_completion(queue, bot):
    seed(queue, 2)
    rows = queue.claim_next_bundle(10)
    for row in rows:
        queue.finish(row["id"], alpha_id="A")

    reporter = QueueReporter(bot, None, queue)
    asyncio.run(
        reporter.bundle_done(CHAT, "b1", [outcome("x", FAIL), outcome("y", FAIL)])
    )

    final = bot.texts()[-1]
    assert "Batch complete" in final
    assert "/alphas" in final


def test_passing_count_is_highlighted(queue, bot):
    seed(queue, 2)
    rows = queue.claim_next_bundle(10)
    for row in rows:
        queue.finish(row["id"], alpha_id="A")

    reporter = QueueReporter(bot, None, queue)
    asyncio.run(
        reporter.bundle_done(CHAT, "b1", [outcome("x", PASS), outcome("y", PASS)])
    )

    assert "2 passed all checks" in bot.texts()[-1]


def test_session_lost_tells_you_how_to_recover(queue, bot):
    reporter = QueueReporter(bot, None, queue)
    asyncio.run(reporter.session_lost(CHAT))

    text = bot.texts()[-1]
    assert "/login" in text and "/queue retry" in text


def test_reporting_failure_never_propagates(queue):
    """A dead chat must not kill the worker or lose a stored result."""

    class BrokenBot:
        async def send_message(self, **kwargs):
            raise RuntimeError("chat not found")

    reporter = QueueReporter(BrokenBot(), None, queue)
    asyncio.run(reporter.session_lost(CHAT))  # must not raise


def test_persona_callback_only_needs_a_bot(queue, bot):
    """The worker has no Update, so the callback gets a bot-only namespace."""
    reporter = QueueReporter(bot, None, queue)
    callback = reporter.persona_callback(CHAT)

    asyncio.run(callback("https://example.com/persona?id=abc"))

    assert "Biometric" in bot.texts()[-1]
    assert "persona?id=abc" in bot.texts()[-1]
