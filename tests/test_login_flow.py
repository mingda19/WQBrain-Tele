"""End-to-end tests through the handler layer.

These join BrainSession to the handlers with a fake bot, so they catch wiring
mistakes the unit tests cannot: whether a successful login actually arms the
timers, whether the warning fires, and whether a rejected password reaches the
user as a readable message instead of a traceback.

Run: python -m pytest tests/ -q
"""

import asyncio

import pytest

from bot import brain_session as bs
from bot import handlers as h
from bot.brain_session import BrainSession
from tests.fakes import PERSONA_URL, FakeContext, FakeResponse, FakeSession

CHAT_ID = 4242


@pytest.fixture
def config():
    class C:
        brain_credentials = ("me@example.com", "hunter2")
        warn_before_expiry_seconds = 300
        reconcile_seconds = 600

        def is_allowed(self, chat_id):
            return chat_id == CHAT_ID

    return C()


def build(monkeypatch, config, session):
    monkeypatch.setattr(bs.ace, "SingleSession", lambda *a, **k: session)
    brain = BrainSession(config)
    context = FakeContext(config, brain)
    return brain, context


def test_login_reports_success_and_arms_both_timers(monkeypatch, config):
    session = FakeSession([FakeResponse(201)], expiry=14395.0)
    brain, context = build(monkeypatch, config, session)

    asyncio.run(h._authenticate(context, CHAT_ID))

    assert brain.is_authenticated
    text = context.bot.texts()[-1]
    assert "Logged in to BRAIN" in text
    assert "3h 59m" in text

    warning = context.job_queue.live(h.WARNING_JOB)
    expiry = context.job_queue.live(h.EXPIRED_JOB)
    reconciler = context.job_queue.live(h.RECONCILE_JOB)
    assert len(warning) == 1 and len(expiry) == 1 and len(reconciler) == 1
    assert expiry[0].when - warning[0].when == pytest.approx(300.0)
    assert context.application.bot_data["warned"] is False


def test_bad_password_surfaces_a_readable_message(monkeypatch, config):
    session = FakeSession([FakeResponse(401, headers={"WWW-Authenticate": "Basic"})])
    brain, context = build(monkeypatch, config, session)

    asyncio.run(h._authenticate(context, CHAT_ID))

    assert not brain.is_authenticated
    text = context.bot.texts()[-1]
    assert "Login failed" in text
    assert "BRAIN_CREDENTIAL_PASSWORD" in text
    assert "Traceback" not in text
    assert context.job_queue.jobs == [], "no timers armed on a failed login"


def test_persona_link_reaches_the_user(monkeypatch, config):
    monkeypatch.setattr(bs, "PERSONA_POLL_SECONDS", 0.01)
    session = FakeSession(
        [
            FakeResponse(
                401, headers={"WWW-Authenticate": "persona", "Location": PERSONA_URL}
            ),
            FakeResponse(204),
            FakeResponse(201),
        ]
    )
    brain, context = build(monkeypatch, config, session)

    asyncio.run(h._authenticate(context, CHAT_ID))

    prompts = [t for t in context.bot.texts() if "Biometric" in t]
    assert len(prompts) == 1
    # Escaped for HTML parse mode; Telegram renders the & back and auto-links it.
    assert "persona?id=abc" in prompts[0]
    assert brain.is_authenticated


def test_warning_fires_with_relogin_and_snooze_buttons(monkeypatch, config):
    session = FakeSession([FakeResponse(201)], expiry=14395.0)
    brain, context = build(monkeypatch, config, session)

    async def scenario():
        await h._authenticate(context, CHAT_ID)
        job = context.job_queue.live(h.WARNING_JOB)[0]
        context.job = job
        await job.callback(context)

    asyncio.run(scenario())

    warning = context.bot.messages[-1]
    assert "expiring" in warning["text"]
    assert context.application.bot_data["warned"] is True
    labels = [b.text for row in warning["reply_markup"].inline_keyboard for b in row]
    assert labels == ["Re-login now", "Snooze 2m"]


def test_reconciler_announces_a_session_killed_elsewhere(monkeypatch, config):
    session = FakeSession([FakeResponse(201)], expiry=14395.0)
    brain, context = build(monkeypatch, config, session)

    async def scenario():
        await h._authenticate(context, CHAT_ID)
        session.set_expiry(None)  # logged out from the platform in a browser
        context.job = context.job_queue.live(h.RECONCILE_JOB)[0]
        await h._reconcile_session(context)

    asyncio.run(scenario())

    assert not brain.is_authenticated
    assert "session is gone" in context.bot.texts()[-1]
    assert context.job_queue.live(h.WARNING_JOB) == ()
    assert context.job_queue.live(h.EXPIRED_JOB) == ()


def test_reconciler_rearms_after_a_host_sleep(monkeypatch, config):
    """Wall-clock timers can be dropped over a sleep; the reconciler restores them."""
    session = FakeSession([FakeResponse(201)], expiry=14395.0)
    brain, context = build(monkeypatch, config, session)

    async def scenario():
        await h._authenticate(context, CHAT_ID)
        for job in context.job_queue.jobs:  # simulate the scheduler losing them
            if job.name in (h.WARNING_JOB, h.EXPIRED_JOB):
                job.schedule_removal()
        session.set_expiry(3600.0)
        context.job = context.job_queue.live(h.RECONCILE_JOB)[0]
        await h._reconcile_session(context)

    asyncio.run(scenario())

    warning = context.job_queue.live(h.WARNING_JOB)
    assert len(warning) == 1
    assert warning[0].when == pytest.approx(3300.0), "re-anchored to the real expiry"
    assert brain.is_authenticated


def test_reconciler_does_not_re_warn_once_warned(monkeypatch, config):
    session = FakeSession([FakeResponse(201)], expiry=14395.0)
    brain, context = build(monkeypatch, config, session)

    async def scenario():
        await h._authenticate(context, CHAT_ID)
        context.application.bot_data["warned"] = True
        session.set_expiry(200.0)
        context.job = context.job_queue.live(h.RECONCILE_JOB)[0]
        await h._reconcile_session(context)

    asyncio.run(scenario())

    assert context.job_queue.live(h.WARNING_JOB) == ()
    assert len(context.job_queue.live(h.EXPIRED_JOB)) == 1


def test_expiry_job_does_not_cry_wolf_if_session_was_refreshed(monkeypatch, config):
    session = FakeSession([FakeResponse(201)], expiry=14395.0)
    brain, context = build(monkeypatch, config, session)

    async def scenario():
        await h._authenticate(context, CHAT_ID)
        before = len(context.bot.messages)
        context.job = context.job_queue.live(h.EXPIRED_JOB)[0]
        await h._session_expired(context)
        return before

    before = asyncio.run(scenario())

    assert len(context.bot.messages) == before, "session is alive; stay quiet"
    assert brain.is_authenticated
    assert len(context.job_queue.live(h.EXPIRED_JOB)) == 1


def test_logout_clears_every_timer(monkeypatch, config):
    session = FakeSession([FakeResponse(201)], expiry=14395.0)
    brain, context = build(monkeypatch, config, session)

    async def scenario():
        await h._authenticate(context, CHAT_ID)
        h._cancel_jobs(context, h.WARNING_JOB, h.EXPIRED_JOB, h.RECONCILE_JOB)
        await brain.logout()

    asyncio.run(scenario())

    assert not brain.is_authenticated
    for name in (h.WARNING_JOB, h.EXPIRED_JOB, h.RECONCILE_JOB):
        assert context.job_queue.live(name) == ()
