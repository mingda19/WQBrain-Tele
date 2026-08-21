"""The /sim conversation: expression -> settings -> confirm -> run -> report.

A ConversationHandler rather than an argument-parsing one-liner, because the
settings are interdependent (universe and neutralization depend on region and
delay) and getting one wrong means a simulation that fails only after it has
been submitted. Menus are built from the live catalog, so only combinations
BRAIN accepts can be chosen.

The simulation itself runs as a tracked application task: it blocks for minutes
inside ACE's polling loop, so the conversation ends at confirmation and the
result arrives as its own message.
"""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from bot.alpha_spec import (
    TEST_PERIOD_CHOICES,
    AlphaSpec,
    SettingsCatalog,
    parse_decay,
    parse_truncation,
)
from bot.config import Config
from bot.formatting import bold, code, esc, pre
from bot.handlers import persona_prompt
from bot.results import format_outcome, format_spec_card
from bot.simulation import MIN_SESSION_SECONDS

log = logging.getLogger(__name__)

ASK_EXPRESSION, SETTINGS, ASK_VALUE, CONFIRM = range(4)

P = "sim"  # callback prefix; handlers.py owns "persona:" and "session:"

# field -> (button label, catalog lookup)
CHOICE_FIELDS = {
    "region": "Region",
    "universe": "Universe",
    "delay": "Delay",
    "neutralization": "Neutral",
    "test_period": "Period",
}
TEXT_FIELDS = {"decay": "Decay", "truncation": "Trunc"}


def _brain(context):
    return context.application.bot_data["brain"]


def _catalog(context) -> SettingsCatalog:
    return context.application.bot_data["catalog"]


def _spec(context) -> AlphaSpec:
    return context.user_data["spec"]


def _choices(context, field: str) -> list:
    catalog = _catalog(context)
    spec = _spec(context)
    if field == "region":
        return catalog.regions()
    if field == "delay":
        return catalog.delays(spec.region)
    if field == "universe":
        return catalog.universes(spec.region, spec.delay)
    if field == "neutralization":
        return catalog.neutralizations(spec.region, spec.delay)
    if field == "test_period":
        return list(TEST_PERIOD_CHOICES)
    return []


def _settings_keyboard(spec: AlphaSpec) -> InlineKeyboardMarkup:
    """Each button shows the value it currently holds."""
    rows = [
        [
            InlineKeyboardButton(f"Region: {spec.region}", callback_data=f"{P}:pick:region"),
            InlineKeyboardButton(
                f"Universe: {spec.universe}", callback_data=f"{P}:pick:universe"
            ),
        ],
        [
            InlineKeyboardButton(f"Delay: {spec.delay}", callback_data=f"{P}:pick:delay"),
            InlineKeyboardButton(
                f"Neutral: {spec.neutralization}",
                callback_data=f"{P}:pick:neutralization",
            ),
        ],
        [
            InlineKeyboardButton(f"Decay: {spec.decay}", callback_data=f"{P}:edit:decay"),
            InlineKeyboardButton(
                f"Trunc: {spec.truncation:g}", callback_data=f"{P}:edit:truncation"
            ),
        ],
        [
            InlineKeyboardButton(
                f"Test period: {spec.test_period}", callback_data=f"{P}:pick:test_period"
            )
        ],
        [
            InlineKeyboardButton("Run simulation", callback_data=f"{P}:run"),
            InlineKeyboardButton("Cancel", callback_data=f"{P}:cancel"),
        ],
    ]
    return InlineKeyboardMarkup(rows)


def _choice_keyboard(field: str, options: list, current) -> InlineKeyboardMarkup:
    buttons = []
    for option in options:
        mark = "• " if str(option) == str(current) else ""
        buttons.append(
            InlineKeyboardButton(
                f"{mark}{option}", callback_data=f"{P}:set:{field}:{option}"
            )
        )
    rows = [buttons[i : i + 3] for i in range(0, len(buttons), 3)]
    rows.append([InlineKeyboardButton("Back", callback_data=f"{P}:menu")])
    return InlineKeyboardMarkup(rows)


async def _show_settings(update: Update, context, *, edit: bool) -> int:
    spec = _spec(context)
    text = format_spec_card(spec, title="Alpha settings")
    markup = _settings_keyboard(spec)
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=markup
        )
    else:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
        )
    return SETTINGS


# ------------------------------------------------------------------ entry


async def sim_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    brain = _brain(context)
    if not brain.is_authenticated:
        await update.effective_message.reply_text(
            "Not logged in. Send /login first.", parse_mode=ParseMode.HTML
        )
        return ConversationHandler.END

    context.user_data["spec"] = AlphaSpec()

    catalog = _catalog(context)
    if not catalog.loaded:
        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id, action=ChatAction.TYPING
        )
        if not await catalog.load(brain):
            await update.effective_message.reply_text(
                "Could not load the settings catalog; using defaults. "
                "Some combinations may be rejected by BRAIN.",
            )

    await update.effective_message.reply_text(
        f"{bold('New alpha')}\n\nSend me the expression, for example:\n"
        f"{pre('ts_sum(vec_avg(nws18_qmb),120)')}\n"
        "Or /cancel to stop.",
        parse_mode=ParseMode.HTML,
    )
    return ASK_EXPRESSION


async def sim_expression(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.effective_message.text or "").strip()
    if not text:
        await update.effective_message.reply_text("That was empty — send an expression.")
        return ASK_EXPRESSION

    spec = _spec(context)
    spec.expression = text
    context.user_data["spec"] = _catalog(context).reconcile(spec)
    return await _show_settings(update, context, edit=False)


# --------------------------------------------------------------- settings


async def sim_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    parts = query.data.split(":", 3)
    action = parts[1]

    if action == "menu":
        await query.answer()
        return await _show_settings(update, context, edit=True)

    if action == "cancel":
        await query.answer("Cancelled.")
        await query.edit_message_text("Cancelled.", parse_mode=ParseMode.HTML)
        context.user_data.pop("spec", None)
        return ConversationHandler.END

    if action == "pick":
        field = parts[2]
        await query.answer()
        options = _choices(context, field)
        await query.edit_message_text(
            f"{bold(CHOICE_FIELDS.get(field, field))} — choose one:",
            parse_mode=ParseMode.HTML,
            reply_markup=_choice_keyboard(
                field, options, getattr(_spec(context), field)
            ),
        )
        return SETTINGS

    if action == "set":
        field, raw = parts[2], parts[3]
        spec = _spec(context)
        setattr(spec, field, int(raw) if field == "delay" else raw)
        context.user_data["spec"] = _catalog(context).reconcile(spec)
        await query.answer(f"{field} = {raw}")
        return await _show_settings(update, context, edit=True)

    if action == "edit":
        field = parts[2]
        context.user_data["editing"] = field
        await query.answer()
        hint = (
            "Send a decay value (whole number, 0–512):"
            if field == "decay"
            else "Send a truncation value (0–1, e.g. 0.08):"
        )
        await query.edit_message_text(hint, parse_mode=ParseMode.HTML)
        return ASK_VALUE

    if action == "run":
        await query.answer()
        spec = _spec(context)
        await query.edit_message_text(
            format_spec_card(spec, title="Confirm simulation"),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("Confirm & run", callback_data=f"{P}:go"),
                        InlineKeyboardButton("Back", callback_data=f"{P}:menu"),
                    ]
                ]
            ),
        )
        return CONFIRM

    await query.answer()
    return SETTINGS


async def sim_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    field = context.user_data.get("editing")
    if field not in ("decay", "truncation") or "spec" not in context.user_data:
        await update.effective_message.reply_text(
            "That edit expired. Send /sim to start again."
        )
        return ConversationHandler.END
    text = update.effective_message.text or ""
    parser = parse_decay if field == "decay" else parse_truncation

    try:
        value = parser(text)
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))
        return ASK_VALUE

    setattr(_spec(context), field, value)
    context.user_data.pop("editing", None)
    return await _show_settings(update, context, edit=False)


# ---------------------------------------------------------------- execute


async def sim_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    spec = _spec(context)
    chat_id = update.effective_chat.id
    runner = context.application.bot_data["runner"]

    await query.answer("Submitting…")
    queued = runner.in_flight >= runner.max_concurrent
    await query.edit_message_text(
        (
            f"{bold('Queued' if queued else 'Simulating…')}\n\n"
            f"{pre(spec.expression)}\n"
            f"{esc(spec.settings_line())}\n\n"
            + (
                f"{runner.in_flight} already running (limit {runner.max_concurrent}); "
                "this one starts when a slot frees up."
                if queued
                else "This takes a few minutes. I'll post the results here."
            )
        ),
        parse_mode=ParseMode.HTML,
    )

    context.application.create_task(_run_and_report(context, chat_id, spec))
    context.user_data.pop("spec", None)
    return ConversationHandler.END


async def _run_and_report(context, chat_id: int, spec: AlphaSpec) -> None:
    """Dispatch one simulation and post the outcome. Never raises."""
    brain = _brain(context)
    runner = context.application.bot_data["runner"]
    store = context.application.bot_data["store"]

    try:
        fresh = await brain.ensure_fresh(
            MIN_SESSION_SECONDS, persona_prompt(context, chat_id)
        )
        if not fresh:
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"{bold('Not simulated.')} The BRAIN session could not be "
                    "refreshed. Send /login and try again."
                ),
                parse_mode=ParseMode.HTML,
            )
            return

        outcome = await runner.run(spec)
        stored = store.record(outcome)

        text = format_outcome(outcome)
        if outcome.ok and not stored:
            text += f"\n\n{esc('(Result could not be written to the alpha store.)')}"
        await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception as exc:  # noqa: BLE001 -- a background task must not die silently
        log.exception("Simulation task failed")
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"{bold('Simulation crashed.')}\n{pre(repr(exc))}",
            parse_mode=ParseMode.HTML,
        )


async def sim_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("spec", None)
    context.user_data.pop("editing", None)
    await update.effective_message.reply_text("Cancelled.")
    return ConversationHandler.END


# ------------------------------------------------------------------ misc


async def alphas(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Recent simulations, newest first."""
    store = context.application.bot_data["store"]
    rows = store.recent(10)
    if not rows:
        await update.effective_message.reply_text(
            "No alphas recorded yet. Run /sim first."
        )
        return

    lines = [f"{bold(f'{store.count()} alpha(s) recorded')}", ""]
    for row in rows:
        flag = "PASS" if row["all_passed"] else f"{row['tests_failed']} fail"
        sharpe = f"{row['sharpe']:.2f}" if row["sharpe"] is not None else "-"
        fitness = f"{row['fitness']:.2f}" if row["fitness"] is not None else "-"
        lines.append(
            f"{code(row['alpha_id'])}  sh {sharpe}  fit {fitness}  [{esc(flag)}]\n"
            f"{esc(row['expression'][:80])}"
        )
    await update.effective_message.reply_text(
        "\n\n".join(lines), parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )


def register(app: Application, config: Config) -> None:
    allowed = filters.Chat(chat_id=set(config.allowed_chat_ids))

    app.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("sim", sim_start, filters=allowed)],
            states={
                ASK_EXPRESSION: [
                    MessageHandler(allowed & filters.TEXT & ~filters.COMMAND, sim_expression)
                ],
                SETTINGS: [CallbackQueryHandler(sim_button, pattern=rf"^{P}:")],
                ASK_VALUE: [
                    MessageHandler(allowed & filters.TEXT & ~filters.COMMAND, sim_value)
                ],
                CONFIRM: [
                    CallbackQueryHandler(sim_confirm, pattern=rf"^{P}:go$"),
                    CallbackQueryHandler(sim_button, pattern=rf"^{P}:"),
                ],
            },
            fallbacks=[CommandHandler("cancel", sim_cancel, filters=allowed)],
            name="sim",
            persistent=False,
        )
    )
    app.add_handler(CommandHandler("alphas", alphas, filters=allowed))
