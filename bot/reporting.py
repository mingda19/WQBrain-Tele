"""How the queue worker talks back to the chat.

Forty finished simulations must not become forty messages. So a batch reports as
a running tally, full detail is reserved for alphas that pass every check -- the
only ones worth acting on immediately -- and everything else stays in the store
behind /alphas and /export.
"""

import logging
from types import SimpleNamespace

from telegram.constants import ParseMode

from bot.formatting import bold, code, esc
from bot.results import format_outcome

log = logging.getLogger(__name__)


class QueueReporter:
    """Posts batch progress. Never raises -- a reporting failure must not kill
    the worker or lose a result that is already safely in the store."""

    def __init__(self, bot, application, queue) -> None:
        self._bot = bot
        self._application = application
        self._queue = queue

    def persona_callback(self, chat_id: int):
        """Biometric prompt for a mid-queue re-login, reusing the /login flow.

        ``persona_prompt`` only ever touches ``context.bot``, so a namespace
        carrying the bot is all it needs -- the worker has no Update to build a
        real context from.
        """
        from bot.handlers import persona_prompt

        return persona_prompt(SimpleNamespace(bot=self._bot), chat_id)

    async def _send(self, chat_id: int, text: str) -> None:
        try:
            await self._bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Exception:  # noqa: BLE001
            log.exception("Could not post to chat %s", chat_id)

    async def session_lost(self, chat_id: int) -> None:
        await self._send(
            chat_id,
            f"{bold('Queue paused.')} The BRAIN session could not be refreshed, so "
            "the current bundle was not simulated. Send /login, then /queue retry.",
        )

    async def bundle_done(self, chat_id: int, batch_id: str, outcomes: list) -> None:
        """Post the winners in full, then a one-line tally for the batch."""
        import asyncio

        for outcome in outcomes:
            if outcome.ok and outcome.all_passed:
                await self._send(chat_id, format_outcome(outcome))

        progress = await asyncio.to_thread(self._queue.batch_progress, batch_id)
        total = sum(progress.values())
        done = progress.get("done", 0)
        failed = progress.get("failed", 0)
        settled = done + failed
        passed = sum(1 for o in outcomes if o.ok and o.all_passed)

        line = (
            f"Batch {code(batch_id)} · {settled}/{total} done"
            f" · {done} simulated · {failed} failed"
        )
        if passed:
            line += f" · {bold(f'{passed} passed all checks')}"

        if settled >= total:
            line = f"{bold('Batch complete')}\n{line}\n{esc('/alphas to review, /export for CSV.')}"

        await self._send(chat_id, line)
