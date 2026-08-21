"""Environment-backed configuration, validated once at startup.

Importing this module loads ``.env``. It must be imported before ``ace_lib``,
because ACE reads ``BRAIN_API_URL`` at import time -- see ace_bridge.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
ACE_API_DIR = REPO_ROOT / "ACE_API"
DATA_DIR = REPO_ROOT / "data"

load_dotenv(REPO_ROOT / ".env")

DEFAULT_BRAIN_API_URL = "https://api.worldquantbrain.com"
DEFAULT_BRAIN_URL = "https://platform.worldquantbrain.com"


class ConfigError(RuntimeError):
    """Raised at startup when the environment is not usable."""


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(
            f"{name} is not set. Copy .env.example to .env and fill it in."
        )
    return value


def _positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a whole number of seconds, got {raw!r}.")
    if value <= 0:
        raise ConfigError(f"{name} must be greater than 0, got {value}.")
    return value


def _chat_ids(name: str) -> frozenset[int]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return frozenset()
    ids = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            raise ConfigError(
                f"{name} must be comma-separated numeric chat IDs, got {part!r}. "
                "Send /start to the bot to find yours."
            )
    return frozenset(ids)


class Config:
    """Validated settings. Constructing this is the startup check."""

    def __init__(self) -> None:
        self.telegram_bot_token = _require("TELEGRAM_BOT_TOKEN")
        self.allowed_chat_ids = _chat_ids("TELEGRAM_ALLOWED_CHAT_IDS")
        self.brain_email = _require("BRAIN_CREDENTIAL_EMAIL")
        self.brain_password = _require("BRAIN_CREDENTIAL_PASSWORD")
        self.warn_before_expiry_seconds = _positive_int("WARN_BEFORE_EXPIRY_SECONDS", 300)
        self.reconcile_seconds = _positive_int("SESSION_RECONCILE_SECONDS", 600)
        self.max_concurrent_sims = _positive_int("BRAIN_MAX_CONCURRENT_SIMS", 3)
        self.db_path = DATA_DIR / "alphas.db"
        self.export_path = DATA_DIR / "alphas.csv"
        self.brain_api_url = (
            os.environ.get("BRAIN_API_URL", "").strip() or DEFAULT_BRAIN_API_URL
        ).rstrip("/")
        self.brain_url = (
            os.environ.get("BRAIN_URL", "").strip() or DEFAULT_BRAIN_URL
        ).rstrip("/")

    def is_allowed(self, chat_id: int) -> bool:
        """An empty allowlist authorises nobody -- misconfiguration fails closed."""
        return chat_id in self.allowed_chat_ids

    @property
    def brain_credentials(self) -> tuple[str, str]:
        return (self.brain_email, self.brain_password)
