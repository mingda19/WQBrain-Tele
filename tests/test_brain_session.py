"""Tests for the headless BRAIN auth state machine.

These cover the paths that cannot safely be exercised by hand: a rejected
password (which in ACE wipes a credentials file and recurses), and the biometric
flow (which needs a real prompt). ``ace.check_session_timeout`` is left
unpatched so the expiry path is tested against the real ACE function.

Run: python -m pytest tests/ -q
"""

import asyncio

import pytest

from bot import brain_session as bs
from bot.brain_session import (
    BadCredentialsError,
    BrainAuthError,
    BrainSession,
    PersonaTimeoutError,
)

from tests.fakes import AUTH_URL, PERSONA_URL, FakeResponse, FakeSession


@pytest.fixture
def config():
    class C:
        brain_credentials = ("me@example.com", "hunter2")
        warn_before_expiry_seconds = 300
        reconcile_seconds = 600

    return C()


@pytest.fixture
def guard_ace(monkeypatch):
    """Fail loudly if we ever fall into ACE's destructive login path."""

    def forbidden(*args, **kwargs):
        raise AssertionError("ACE's interactive login path must never be called")

    monkeypatch.setattr(bs.ace, "start_session", forbidden)
    monkeypatch.setattr(bs.ace, "get_credentials", forbidden)


def install(monkeypatch, session):
    monkeypatch.setattr(bs.ace, "SingleSession", lambda *a, **k: session)


async def _no_persona(url):
    raise AssertionError(f"persona callback should not fire, got {url}")


def test_successful_login_sets_expiry(monkeypatch, config, guard_ace):
    session = FakeSession([FakeResponse(201)], expiry=14395.0)
    install(monkeypatch, session)
    brain = BrainSession(config)

    remaining = asyncio.run(brain.login(_no_persona))

    assert remaining == pytest.approx(14395.0)
    assert brain.is_authenticated
    assert brain.expires_at is not None
    assert brain.scheduled_seconds_remaining() > 14000
    assert session.auth == ("me@example.com", "hunter2")
    assert session.posted == [AUTH_URL]


def test_bad_password_raises_and_does_not_recurse(monkeypatch, config, guard_ace):
    """The regression that matters: ACE would erase creds and recurse forever."""
    session = FakeSession([FakeResponse(401, headers={"WWW-Authenticate": "Basic"})])
    install(monkeypatch, session)
    brain = BrainSession(config)

    with pytest.raises(BadCredentialsError):
        asyncio.run(brain.login(_no_persona))

    assert not brain.is_authenticated
    assert len(session.posted) == 1, "must not retry or recurse"


def test_persona_flow_completes(monkeypatch, config, guard_ace):
    monkeypatch.setattr(bs, "PERSONA_POLL_SECONDS", 0.01)
    session = FakeSession(
        [
            FakeResponse(
                401,
                headers={"WWW-Authenticate": "persona", "Location": PERSONA_URL},
            ),
            FakeResponse(204),  # biometric not done yet
            FakeResponse(201),  # completed
        ]
    )
    install(monkeypatch, session)
    brain = BrainSession(config)
    seen = []

    async def on_persona(url):
        seen.append(url)

    remaining = asyncio.run(brain.login(on_persona))

    assert seen == [PERSONA_URL], "user must receive the biometric link"
    assert remaining == pytest.approx(14395.0)
    assert brain.is_authenticated
    assert session.posted.count(PERSONA_URL) == 2


def test_persona_timeout_is_reported(monkeypatch, config, guard_ace):
    monkeypatch.setattr(bs, "PERSONA_POLL_SECONDS", 0.01)
    monkeypatch.setattr(bs, "PERSONA_TIMEOUT_SECONDS", 0.05)
    session = FakeSession(
        [
            FakeResponse(
                401,
                headers={"WWW-Authenticate": "persona", "Location": PERSONA_URL},
            )
        ]
        + [FakeResponse(204)] * 50
    )
    install(monkeypatch, session)
    brain = BrainSession(config)

    with pytest.raises(PersonaTimeoutError):
        asyncio.run(brain.login(lambda url: asyncio.sleep(0)))

    assert not brain.is_authenticated


def test_persona_without_location_is_rejected(monkeypatch, config, guard_ace):
    session = FakeSession([FakeResponse(401, headers={"WWW-Authenticate": "persona"})])
    install(monkeypatch, session)
    brain = BrainSession(config)

    with pytest.raises(BrainAuthError):
        asyncio.run(brain.login(_no_persona))


def test_nudge_short_circuits_persona_poll(monkeypatch, config, guard_ace):
    """The button must not have to wait out the poll interval."""
    monkeypatch.setattr(bs, "PERSONA_POLL_SECONDS", 30.0)
    session = FakeSession(
        [
            FakeResponse(
                401,
                headers={"WWW-Authenticate": "persona", "Location": PERSONA_URL},
            ),
            FakeResponse(204),
            FakeResponse(201),
        ]
    )
    install(monkeypatch, session)
    brain = BrainSession(config)

    async def scenario():
        async def on_persona(url):
            asyncio.get_running_loop().call_later(0.01, brain.nudge_persona)

        return await asyncio.wait_for(brain.login(on_persona), timeout=5.0)

    assert asyncio.run(scenario()) == pytest.approx(14395.0)


def test_server_side_expiry_forgets_session(monkeypatch, config, guard_ace):
    session = FakeSession([FakeResponse(201)], expiry=14395.0)
    install(monkeypatch, session)
    brain = BrainSession(config)

    async def scenario():
        await brain.login(_no_persona)
        session.set_expiry(None)  # session killed elsewhere
        return await brain.refresh_expiry()

    assert asyncio.run(scenario()) == 0.0
    assert not brain.is_authenticated
    assert brain.scheduled_seconds_remaining() == 0.0


def test_expired_at_login_is_not_reported_as_success(monkeypatch, config, guard_ace):
    session = FakeSession([FakeResponse(201)], expiry=None)
    install(monkeypatch, session)
    brain = BrainSession(config)

    with pytest.raises(BrainAuthError):
        asyncio.run(brain.login(_no_persona))
    assert not brain.is_authenticated


def test_logout_clears_state(monkeypatch, config, guard_ace):
    session = FakeSession([FakeResponse(201)])
    install(monkeypatch, session)
    brain = BrainSession(config)

    async def scenario():
        await brain.login(_no_persona)
        assert brain.is_authenticated
        await brain.logout()

    asyncio.run(scenario())
    assert not brain.is_authenticated
    assert brain.expires_at is None
    with pytest.raises(BrainAuthError):
        _ = brain.session


def test_unauthenticated_session_access_raises(config):
    with pytest.raises(BrainAuthError):
        _ = BrainSession(config).session
