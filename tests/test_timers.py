"""Tests for session-expiry timer arming.

The warning arithmetic is what the whole feature rests on, and it is awkward to
verify by hand -- observing it live means waiting out a ~4 hour session. These
drive the logic directly with a recording job queue.

Run: python -m pytest tests/ -q
"""

import pytest

from bot import handlers as h

from tests.fakes import FakeContext

CHAT_ID = 4242


@pytest.fixture
def config():
    class C:
        warn_before_expiry_seconds = 300
        reconcile_seconds = 600

    return C()


@pytest.fixture
def context(config):
    return FakeContext(config)


def test_warning_lands_five_minutes_before_expiry(context):
    h._arm_session_timers(context, CHAT_ID, 14395.0, already_warned=False)

    warning = context.job_queue.live(h.WARNING_JOB)
    expiry = context.job_queue.live(h.EXPIRED_JOB)

    assert len(warning) == 1 and len(expiry) == 1
    assert warning[0].when == pytest.approx(14095.0)
    assert expiry[0].when == pytest.approx(14395.0)
    assert expiry[0].when - warning[0].when == pytest.approx(300.0)


def test_already_warned_does_not_re_warn(context):
    h._arm_session_timers(context, CHAT_ID, 14395.0, already_warned=True)

    assert context.job_queue.live(h.WARNING_JOB) == ()
    assert len(context.job_queue.live(h.EXPIRED_JOB)) == 1


def test_login_inside_the_warning_window_warns_immediately(context):
    """A session with 100s left is already past the 300s mark -- warn now."""
    h._arm_session_timers(context, CHAT_ID, 100.0, already_warned=False)

    warning = context.job_queue.live(h.WARNING_JOB)
    assert len(warning) == 1
    assert warning[0].when == pytest.approx(1.0)


def test_rearming_replaces_rather_than_duplicates(context):
    h._arm_session_timers(context, CHAT_ID, 14395.0, already_warned=False)
    h._arm_session_timers(context, CHAT_ID, 9000.0, already_warned=False)
    h._arm_session_timers(context, CHAT_ID, 3600.0, already_warned=False)

    warning = context.job_queue.live(h.WARNING_JOB)
    expiry = context.job_queue.live(h.EXPIRED_JOB)

    assert len(warning) == 1, "stale warning timers must be cancelled"
    assert len(expiry) == 1
    assert warning[0].when == pytest.approx(3300.0)
    assert len(context.job_queue.jobs) == 6, "prior jobs are removed, not dropped"


def test_expiry_timer_never_schedules_in_the_past(context):
    h._arm_session_timers(context, CHAT_ID, 0.0, already_warned=False)

    for name in (h.WARNING_JOB, h.EXPIRED_JOB):
        jobs = context.job_queue.live(name)
        assert jobs and jobs[0].when >= 1.0


def test_warn_interval_is_configurable(config, context):
    """The 14000 trick from the runbook: warn almost immediately after login."""
    config.warn_before_expiry_seconds = 14000
    h._arm_session_timers(context, CHAT_ID, 14395.0, already_warned=False)

    assert context.job_queue.live(h.WARNING_JOB)[0].when == pytest.approx(395.0)


def test_reconciler_starts_once(context):
    h._start_reconciler(context, CHAT_ID)
    h._start_reconciler(context, CHAT_ID)
    h._start_reconciler(context, CHAT_ID)

    jobs = context.job_queue.live(h.RECONCILE_JOB)
    assert len(jobs) == 1
    assert jobs[0].interval == 600


def test_cancel_jobs_clears_everything(context):
    h._arm_session_timers(context, CHAT_ID, 14395.0, already_warned=False)
    h._start_reconciler(context, CHAT_ID)

    h._cancel_jobs(context, h.WARNING_JOB, h.EXPIRED_JOB, h.RECONCILE_JOB)

    for name in (h.WARNING_JOB, h.EXPIRED_JOB, h.RECONCILE_JOB):
        assert context.job_queue.live(name) == ()
