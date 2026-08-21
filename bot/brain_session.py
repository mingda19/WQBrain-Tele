"""Headless BRAIN authentication and session lifecycle.

This is the one piece of ACE the bot cannot reuse. ``ace.start_session()``
(ACE_API/ace_lib.py:151) blocks on ``input()`` / ``getpass.getpass()``, and its
biometric branch waits on a terminal keypress. Worse, a rejected password makes it
erase ~/secrets/platform-brain.json and then *recurse* -- an unkillable hang in a
long-running process.

So we reimplement the handshake, and only the handshake, against the same
``SingleSession`` singleton every other ACE function uses. Two rules this module
holds to that ACE does not:

* Credentials come from our config, never ``ace.get_credentials()`` -- which
  silently prefers ~/secrets/platform-brain.json over the environment, so a stale
  file from a notebook run would shadow .env forever.
* A failed login reports and stops. It never deletes a file and never recurses.

The bot also owns expiry from here on. ``ace.check_session_and_relogin()`` refreshes
whenever under 2000s remain, which would pre-empt a 300s warning entirely once
simulations start calling it; ``run_ace()`` exists so later phases can invoke ACE
functions without tripping that path.
"""

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Optional, TypeVar
from urllib.parse import urljoin

import requests

from bot.ace_bridge import ace, api_url, install_session_guards
from bot.config import Config
from bot.formatting import utc_now

log = logging.getLogger(__name__)

T = TypeVar("T")

PERSONA_TIMEOUT_SECONDS = 600
PERSONA_POLL_SECONDS = 5
HTTP_CREATED = 201
HTTP_UNAUTHORIZED = 401

PersonaCallback = Callable[[str], Awaitable[None]]


class BrainAuthError(RuntimeError):
    """Login failed. The message is safe to show the user."""


class BadCredentialsError(BrainAuthError):
    pass


class PersonaTimeoutError(BrainAuthError):
    pass


class BrainSession:
    """Owns the authenticated BRAIN session and knows when it dies."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._session: Optional[ace.SingleSession] = None
        self._lock = asyncio.Lock()
        self._persona_nudge = asyncio.Event()
        self.expires_at: Optional[datetime] = None
        self.logged_in_at: Optional[datetime] = None
        install_session_guards(lambda: config.brain_credentials)

    # ------------------------------------------------------------------ state

    @property
    def is_authenticated(self) -> bool:
        return self._session is not None

    @property
    def login_in_progress(self) -> bool:
        """True while a login holds the lock -- a persona wait can last minutes."""
        return self._lock.locked()

    @property
    def session(self) -> ace.SingleSession:
        """The raw session, for handing to ACE functions in later phases."""
        if self._session is None:
            raise BrainAuthError("Not logged in. Send /login first.")
        return self._session

    def scheduled_seconds_remaining(self) -> float:
        """Seconds left according to our own clock -- no network call.

        May drift if the host slept; ``refresh_expiry()`` is the authority.
        """
        if self.expires_at is None:
            return 0.0
        return max(0.0, (self.expires_at - utc_now()).total_seconds())

    async def refresh_expiry(self) -> float:
        """Ask BRAIN how long is really left, and re-anchor our clock to it.

        Returns 0.0 if the session is gone -- including when it was killed
        server-side, which is the case our timers cannot see on their own.
        """
        if self._session is None:
            return 0.0
        remaining = await asyncio.to_thread(ace.check_session_timeout, self._session)
        remaining = float(remaining or 0.0)
        if remaining <= 0:
            self._forget()
            return 0.0
        self.expires_at = utc_now() + timedelta(seconds=remaining)
        return remaining

    # ------------------------------------------------------------------ login

    async def login(self, on_persona: PersonaCallback) -> float:
        """Authenticate headlessly. Returns seconds remaining on the new session.

        ``on_persona`` is awaited with a one-time biometric URL if BRAIN asks for
        one; the user completes it in a browser and we poll until it lands.
        """
        async with self._lock:
            session = ace.SingleSession()
            session.auth = self._config.brain_credentials
            self._persona_nudge.clear()

            response = await asyncio.to_thread(
                session.post, api_url("/authentication")
            )

            if response.status_code == HTTP_UNAUTHORIZED:
                if response.headers.get("WWW-Authenticate") == "persona":
                    location = response.headers.get("Location")
                    if not location:
                        raise BrainAuthError(
                            "BRAIN asked for biometric authentication but sent no "
                            "link. Try /login again, or sign in on the platform."
                        )
                    await self._complete_persona(
                        session, urljoin(response.url, location), on_persona
                    )
                else:
                    raise BadCredentialsError(
                        "BRAIN rejected the email or password in .env. "
                        "Check BRAIN_CREDENTIAL_EMAIL and BRAIN_CREDENTIAL_PASSWORD, "
                        "then /login again."
                    )
            elif response.status_code // 100 != 2:
                raise BrainAuthError(
                    f"BRAIN returned {response.status_code} during authentication."
                )

            self._session = session
            self.logged_in_at = utc_now()

            remaining = await self.refresh_expiry()
            if remaining <= 0:
                self._forget()
                raise BrainAuthError(
                    "Authentication looked like it succeeded but the session was "
                    "already expired. Try /login again."
                )
            log.info("BRAIN session established, %.0fs remaining", remaining)
            return remaining

    async def _complete_persona(
        self,
        session: ace.SingleSession,
        location_url: str,
        on_persona: PersonaCallback,
    ) -> None:
        """Poll the biometric endpoint until it returns 201, or give up.

        Mirrors ACE_API/ace_lib.py:170-185 with the ``input()`` calls replaced by
        a Telegram prompt plus a poll. ``nudge_persona()`` short-circuits the wait
        when the user taps the button, so the common case is near-instant.
        """
        await on_persona(location_url)
        deadline = time.monotonic() + PERSONA_TIMEOUT_SECONDS

        while time.monotonic() < deadline:
            check = await asyncio.to_thread(session.post, location_url)
            if check.status_code == HTTP_CREATED:
                log.info("Biometric authentication completed")
                return
            try:
                await asyncio.wait_for(
                    self._persona_nudge.wait(), timeout=PERSONA_POLL_SECONDS
                )
                self._persona_nudge.clear()
            except asyncio.TimeoutError:
                pass

        raise PersonaTimeoutError(
            "Biometric authentication was not completed in "
            f"{PERSONA_TIMEOUT_SECONDS // 60} minutes. Send /login to start over."
        )

    def nudge_persona(self) -> None:
        """Called when the user taps 'I've completed it' -- polls immediately."""
        self._persona_nudge.set()

    # ----------------------------------------------------------------- logout

    async def logout(self) -> None:
        """Drop the session locally. Cookies are cleared so the singleton is clean."""
        async with self._lock:
            if self._session is not None:
                await asyncio.to_thread(self._session.cookies.clear)
            self._forget()

    def _forget(self) -> None:
        self._session = None
        self.expires_at = None
        self.logged_in_at = None

    # ------------------------------------------------------------------ calls

    async def run_ace(self, func: Callable[..., T], *args, **kwargs) -> T:
        """Run a blocking ACE function off the event loop, session injected.

        The seam Phase 2's simulation worker builds on: it keeps every ACE call in
        one place, so session handling stays here rather than spreading into
        handlers, and it keeps blocking ``time.sleep`` loops off the event loop.
        """
        return await asyncio.to_thread(func, self.session, *args, **kwargs)

    async def whoami(self) -> dict:
        """End-to-end auth probe: prove the session can actually read account data.

        A 201 from /authentication only says the handshake worked. Falls back to
        the token endpoint if this deployment has no /users/self.
        """
        response: requests.Response = await asyncio.to_thread(
            self.session.get, api_url("/users/self")
        )
        if response.status_code // 100 == 2:
            return response.json()
        log.warning(
            "/users/self returned %s; falling back to /authentication",
            response.status_code,
        )
        fallback: requests.Response = await asyncio.to_thread(
            self.session.get, api_url("/authentication")
        )
        fallback.raise_for_status()
        return fallback.json()
