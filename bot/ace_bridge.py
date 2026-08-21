"""The single place that imports WorldQuant's ACE library.

Import order is load-bearing and the reason this module exists:

1. ``bot.config`` must be imported first -- it calls ``load_dotenv()``, and
   ``ace_lib`` reads ``BRAIN_API_URL`` at *import* time into a module-level global.
2. ``ACE_API/`` must be on ``sys.path`` as a directory, because ``ace_lib`` does a
   flat ``from helpful_functions import ...`` rather than a package-relative import.
3. Only then may ``ace_lib`` be imported.

ACE_API/ is vendored code -- we never edit it, so that WorldQuant's updates drop in
cleanly. Everything the bot needs beyond ``start_session`` is imported unchanged.
"""

import logging
import sys

from bot import config as _config  # noqa: F401  -- side effect: load_dotenv()

if str(_config.ACE_API_DIR) not in sys.path:
    sys.path.insert(0, str(_config.ACE_API_DIR))

import ace_lib as ace  # noqa: E402
import helpful_functions as hf  # noqa: E402

SingleSession = ace.SingleSession

# ACE attaches its own INFO-level StreamHandler to the "ace" logger at import time.
# Leave the handler in place (it also writes ace.log) but stop it duplicating routine
# chatter into the bot's stdout; warnings and errors still come through.
logging.getLogger("ace").setLevel(logging.WARNING)


def install_session_guards(credentials_provider) -> None:
    """Disarm ACE's internal session management. Called once by BrainSession.

    ``simulate_single_alpha``, ``simulate_multi_alpha`` and
    ``get_specified_alpha_stats`` all open with
    ``s = check_session_and_relogin(s)`` (ace_lib.py:718, 778, 887). That calls
    ``start_session()`` -> ``get_credentials()``, which prompts on **stdin** when
    ~/secrets/platform-brain.json is absent. A simulation begun with under 2000s
    left would therefore park a worker thread on ``input()`` forever.

    Two runtime patches, no edit to the vendored file:

    * ``check_session_and_relogin`` becomes identity. The bot already tracks
      expiry and refreshes before dispatching work, so ACE re-logging in behind
      our back is pure downside -- it is also what would silently pre-empt the
      5-minute warning.
    * ``get_credentials`` returns our config values, so nothing can reach the
      ``input()`` path or write a plaintext copy of the password to
      ~/secrets/platform-brain.json as ACE's version does.
    """
    ace.check_session_and_relogin = lambda session: session
    ace.get_credentials = credentials_provider


def api_url(path: str) -> str:
    """Join a path onto the BRAIN API base URL that ACE itself is using.

    Reads ``ace.brain_api_url`` rather than our config so the bot can never end up
    talking to a different host than the vendored library does.
    """
    return f"{ace.brain_api_url.rstrip('/')}/{path.lstrip('/')}"


__all__ = ["ace", "hf", "SingleSession", "api_url", "install_session_guards"]
