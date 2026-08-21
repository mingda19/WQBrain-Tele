"""Entrypoint: ``python -m bot``."""

import logging
import sys

import requests
from telegram.error import InvalidToken, NetworkError
from telegram.ext import Application

from bot.config import Config, ConfigError


def _preflight(token: str) -> tuple[str | None, str | None]:
    """Resolve the bot's username, or explain why we cannot.

    Done before ``run_polling`` so a bad token produces one clear line rather
    than PTB's stack trace -- which also embeds the full token in the log.
    Returns ``(username, error)``; exactly one is set.
    """
    try:
        response = requests.get(
            f"https://api.telegram.org/bot{token}/getMe", timeout=15
        )
    except requests.RequestException as exc:
        return None, f"Could not reach Telegram: {exc}"

    if response.status_code == 401:
        return None, (
            "Telegram rejected TELEGRAM_BOT_TOKEN. Check it against the value "
            "@BotFather gave you."
        )
    if response.status_code != 200:
        return None, f"Telegram returned {response.status_code} for getMe."

    return response.json().get("result", {}).get("username", "unknown"), None


def _setup_logging() -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        level=logging.INFO,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def main() -> int:
    _setup_logging()
    log = logging.getLogger("bot")

    try:
        config = Config()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    username, error = _preflight(config.telegram_bot_token)
    if error:
        print(error, file=sys.stderr)
        return 1

    # Imported after Config() so .env is loaded before ace_lib reads BRAIN_API_URL.
    from bot import handlers
    from bot.brain_session import BrainSession

    if not config.allowed_chat_ids:
        log.warning(
            "TELEGRAM_ALLOWED_CHAT_IDS is empty -- every command except /start is "
            "locked. Send /start to the bot to find your chat ID."
        )

    # concurrent_updates keeps /status answerable while a login sits waiting on a
    # biometric prompt (up to 10 minutes). BrainSession's lock guards the session
    # itself, so overlapping commands are safe.
    app = (
        Application.builder()
        .token(config.telegram_bot_token)
        .concurrent_updates(True)
        .build()
    )
    app.bot_data["config"] = config
    app.bot_data["brain"] = BrainSession(config)
    app.bot_data["warned"] = False

    handlers.register(app, config)

    log.info(
        "Starting as @%s | BRAIN API: %s | %d authorised chat(s)",
        username,
        config.brain_api_url,
        len(config.allowed_chat_ids),
    )
    try:
        app.run_polling()
    except InvalidToken:
        # PTB's own message embeds the full token; don't echo it into logs.
        print(
            "Telegram rejected TELEGRAM_BOT_TOKEN. Check it against the value "
            "@BotFather gave you.",
            file=sys.stderr,
        )
        return 1
    except NetworkError as exc:
        print(f"Could not reach Telegram: {exc}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, SystemExit):
        log.info("Bot stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
