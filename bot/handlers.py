"""Telegram command handlers and session-expiry alerting."""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    filters,
)

from bot.brain_session import BrainAuthError, BrainSession
from bot.config import Config
from bot.formatting import bold, code, esc, human_duration, local_time, pre

log = logging.getLogger(__name__)

WARNING_JOB = "session-warning"
EXPIRED_JOB = "session-expired"
RECONCILE_JOB = "session-reconcile"

CB_PERSONA_DONE = "persona:done"
CB_RELOGIN = "session:relogin"
CB_SNOOZE = "session:snooze"

SNOOZE_SECONDS = 120


# --------------------------------------------------------------------- helpers


def _brain(context: ContextTypes.DEFAULT_TYPE) -> BrainSession:
    return context.application.bot_data["brain"]


def _config(context: ContextTypes.DEFAULT_TYPE) -> Config:
    return context.application.bot_data["config"]


async def _reply(update: Update, text: str, **kwargs) -> None:
    await update.effective_message.reply_text(
        text, parse_mode=ParseMode.HTML, **kwargs
    )


# ------------------------------------------------------------- expiry timers


def _cancel_jobs(context: ContextTypes.DEFAULT_TYPE, *names: str) -> None:
    for name in names:
        for job in context.job_queue.get_jobs_by_name(name):
            job.schedule_removal()


def _arm_session_timers(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    remaining: float,
    *,
    already_warned: bool,
) -> None:
    """(Re)arm the warning and expiry one-shots. Idempotent by design.

    Two ``run_once`` timers beat polling every minute: exact, no drift, and one
    timer each. The reconciler calls this again with a freshly fetched
    ``remaining``, so a timer lost to a host sleep re-arms itself.
    """
    cfg = _config(context)
    _cancel_jobs(context, WARNING_JOB, EXPIRED_JOB)

    if not already_warned:
        warn_in = max(1.0, remaining - cfg.warn_before_expiry_seconds)
        context.job_queue.run_once(
            _warn_expiry, when=warn_in, chat_id=chat_id, name=WARNING_JOB
        )

    context.job_queue.run_once(
        _session_expired, when=max(1.0, remaining), chat_id=chat_id, name=EXPIRED_JOB
    )


def _start_reconciler(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    if context.job_queue.get_jobs_by_name(RECONCILE_JOB):
        return
    interval = _config(context).reconcile_seconds
    context.job_queue.run_repeating(
        _reconcile_session,
        interval=interval,
        first=interval,
        chat_id=chat_id,
        name=RECONCILE_JOB,
    )


async def _warn_expiry(context: ContextTypes.DEFAULT_TYPE) -> None:
    brain = _brain(context)
    if not brain.is_authenticated:
        return
    context.application.bot_data["warned"] = True
    remaining = brain.scheduled_seconds_remaining()
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Re-login now", callback_data=CB_RELOGIN),
                InlineKeyboardButton("Snooze 2m", callback_data=CB_SNOOZE),
            ]
        ]
    )
    await context.bot.send_message(
        chat_id=context.job.chat_id,
        text=(
            f"{bold('BRAIN session expiring')}\n"
            f"About {esc(human_duration(remaining))} left."
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


async def _session_expired(context: ContextTypes.DEFAULT_TYPE) -> None:
    brain = _brain(context)
    if not brain.is_authenticated:
        return
    if await brain.refresh_expiry() > 0:
        # Something refreshed it underneath us; re-arm rather than cry wolf.
        _arm_session_timers(
            context,
            context.job.chat_id,
            brain.scheduled_seconds_remaining(),
            already_warned=context.application.bot_data.get("warned", False),
        )
        return
    await context.bot.send_message(
        chat_id=context.job.chat_id,
        text=f"{bold('BRAIN session expired.')} Send /login to start a new one.",
        parse_mode=ParseMode.HTML,
    )


async def _reconcile_session(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Re-anchor timers against the real expiry.

    Covers the two failure modes a one-shot timer cannot see: the host sleeping
    (APScheduler works in wall-clock, so a pending job fires late or is dropped)
    and the session being killed server-side, which shows up as a 0 from
    ``check_session_timeout``.
    """
    brain = _brain(context)
    if not brain.is_authenticated or brain.login_in_progress:
        return

    remaining = await brain.refresh_expiry()
    if remaining <= 0:
        _cancel_jobs(context, WARNING_JOB, EXPIRED_JOB)
        await context.bot.send_message(
            chat_id=context.job.chat_id,
            text=(
                f"{bold('BRAIN session is gone.')} It was ended from elsewhere or "
                "timed out. Send /login to start a new one."
            ),
            parse_mode=ParseMode.HTML,
        )
        return

    _arm_session_timers(
        context,
        context.job.chat_id,
        remaining,
        already_warned=context.application.bot_data.get("warned", False),
    )


# ------------------------------------------------------------------- commands


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Always answers, even unauthorised -- it is how you learn your chat ID."""
    chat_id = update.effective_chat.id
    cfg = _config(context)

    if cfg.is_allowed(chat_id):
        await _reply(
            update,
            f"{bold('WQBrain bot ready.')}\n"
            f"This chat ({code(chat_id)}) is authorised.\n\n"
            "/login — authenticate with BRAIN\n"
            "/status — session time remaining\n"
            "/whoami — verify the session works\n"
            "/relogin — force a fresh session\n"
            "/logout — drop the session",
        )
        return

    await _reply(
        update,
        f"{bold('Not authorised.')}\n"
        f"This chat's ID is {code(chat_id)}.\n\n"
        "To grant access, add it to TELEGRAM_ALLOWED_CHAT_IDS in .env and "
        "restart the bot.",
    )


async def login(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    brain = _brain(context)
    chat_id = update.effective_chat.id

    if brain.login_in_progress:
        await _reply(update, "A login is already in progress.")
        return
    if brain.is_authenticated:
        await _reply(
            update,
            "Already logged in — "
            f"{esc(human_duration(brain.scheduled_seconds_remaining()))} left. "
            "Use /relogin to force a fresh session.",
        )
        return

    await _authenticate(context, chat_id)


async def relogin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    brain = _brain(context)
    if brain.login_in_progress:
        await _reply(update, "A login is already in progress.")
        return
    await brain.logout()
    await _authenticate(context, update.effective_chat.id)


def persona_prompt(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """Build the callback BrainSession uses to hand over a biometric link.

    Shared with the /sim flow, which may need to top the session up before
    dispatching a simulation.
    """

    async def on_persona(url: str) -> None:
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"{bold('Biometric authentication required.')}\n"
                "Open this link, complete it, then tap the button below.\n\n"
                f"{esc(url)}"
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("I've completed it", callback_data=CB_PERSONA_DONE)]]
            ),
        )

    return on_persona


async def _authenticate(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    """Shared by /login, /relogin and the warning's Re-login button."""
    brain = _brain(context)
    on_persona = persona_prompt(context, chat_id)

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    try:
        remaining = await brain.login(on_persona)
    except BrainAuthError as exc:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"{bold('Login failed.')}\n{esc(exc)}",
            parse_mode=ParseMode.HTML,
        )
        return
    except Exception as exc:  # noqa: BLE001 -- surface anything, never die silently
        log.exception("Unexpected error during login")
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"{bold('Login failed unexpectedly.')}\n{pre(repr(exc))}",
            parse_mode=ParseMode.HTML,
        )
        return

    context.application.bot_data["warned"] = False
    _arm_session_timers(context, chat_id, remaining, already_warned=False)
    _start_reconciler(context, chat_id)

    cfg = _config(context)
    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"{bold('Logged in to BRAIN.')}\n"
            f"Expires in {esc(human_duration(remaining))} "
            f"(at {esc(local_time(brain.expires_at))}).\n"
            f"You'll get a warning "
            f"{esc(human_duration(cfg.warn_before_expiry_seconds))} before that."
        ),
        parse_mode=ParseMode.HTML,
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    brain = _brain(context)
    if not brain.is_authenticated:
        await _reply(update, "Not logged in. Send /login.")
        return

    remaining = await brain.refresh_expiry()
    if remaining <= 0:
        _cancel_jobs(context, WARNING_JOB, EXPIRED_JOB)
        await _reply(update, "Session has expired. Send /login.")
        return

    await _reply(
        update,
        f"{bold('BRAIN session active')}\n"
        f"Remaining: {esc(human_duration(remaining))}\n"
        f"Expires:   {esc(local_time(brain.expires_at))}\n"
        f"Logged in: {esc(local_time(brain.logged_in_at))}",
    )


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    brain = _brain(context)
    if not brain.is_authenticated:
        await _reply(update, "Not logged in. Send /login.")
        return
    try:
        data = await brain.whoami()
    except Exception as exc:  # noqa: BLE001
        log.exception("whoami failed")
        await _reply(update, f"{bold('Probe failed.')}\n{pre(repr(exc))}")
        return

    user_id = data.get("id")
    if user_id is None:
        # Fallback path returned the token payload rather than a user record.
        await _reply(update, f"{bold('Session responds, no user record:')}\n{pre(data)}")
        return

    lines = [f"{bold('BRAIN account')}", f"id: {esc(user_id)}"]
    if isinstance(data.get("email"), str):
        lines.append(f"email: {esc(data['email'])}")
    await _reply(update, "\n".join(lines))


async def logout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    brain = _brain(context)
    await brain.logout()
    _cancel_jobs(context, WARNING_JOB, EXPIRED_JOB, RECONCILE_JOB)
    context.application.bot_data["warned"] = False
    await _reply(update, "Session dropped and timers cleared.")


# -------------------------------------------------------------------- buttons


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat_id = update.effective_chat.id

    if not _config(context).is_allowed(chat_id):
        await query.answer("Not authorised.", show_alert=True)
        return

    if query.data == CB_PERSONA_DONE:
        _brain(context).nudge_persona()
        await query.answer("Checking…")
        return

    if query.data == CB_RELOGIN:
        await query.answer("Re-logging in…")
        brain = _brain(context)
        if brain.login_in_progress:
            return
        await brain.logout()
        await _authenticate(context, chat_id)
        return

    if query.data == CB_SNOOZE:
        brain = _brain(context)
        remaining = brain.scheduled_seconds_remaining()
        if remaining <= 5:
            await query.answer("Too late to snooze — the session is about to expire.")
            return
        _cancel_jobs(context, WARNING_JOB)
        context.job_queue.run_once(
            _warn_expiry,
            when=min(SNOOZE_SECONDS, max(1.0, remaining - 5)),
            chat_id=chat_id,
            name=WARNING_JOB,
        )
        context.application.bot_data["warned"] = False
        await query.answer("Snoozed for 2 minutes.")
        return

    await query.answer()


# ---------------------------------------------------------------- registration


def register(app: Application, config: Config) -> None:
    """Wire handlers up.

    The allowlist is enforced twice: ``filters.Chat`` here, and ``is_allowed``
    inside the button handler. An empty allowlist matches nobody, so a
    misconfigured .env locks the bot down rather than opening it up. /start is
    deliberately unfiltered -- it is how a new chat discovers its own ID.
    """
    allowed = filters.Chat(chat_id=set(config.allowed_chat_ids))

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("login", login, filters=allowed))
    app.add_handler(CommandHandler("relogin", relogin, filters=allowed))
    app.add_handler(CommandHandler("status", status, filters=allowed))
    app.add_handler(CommandHandler("whoami", whoami, filters=allowed))
    app.add_handler(CommandHandler("logout", logout, filters=allowed))
    # Scoped by pattern so it does not swallow the /sim conversation's buttons,
    # which use the "sim:" prefix and are owned by its ConversationHandler.
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^(persona|session):"))
