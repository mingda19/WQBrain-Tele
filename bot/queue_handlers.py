"""/batch, /queue and /export.

Two ways in, one path out. `/batch` takes pasted expressions; the *Use these N
fields* button on a /fields page takes a template. Both converge on the same
settings card, duplicate check and confirmation, then land in the queue.

Owns the ``bat:`` callback prefix.
"""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from bot.alpha_spec import AlphaSpec, parse_decay, parse_truncation
from bot.batching import (
    FIELD_TOKEN,
    TemplateError,
    expand_template,
    find_duplicates,
    group_into_bundles,
    parse_expression_list,
    specs_from_expressions,
    vector_warning,
)
from bot.config import Config
from bot.formatting import bold, code, esc, pre
from bot.results import format_spec_card
from bot.settings_ui import (
    CHOICE_LABELS,
    choice_keyboard,
    choices_for,
    edit_hint,
    settings_keyboard,
)
from bot.simqueue import (
    CANCELLED,
    DONE,
    FAILED,
    INTERRUPTED,
    QUEUED,
    RUNNING,
    new_batch_id,
)

log = logging.getLogger(__name__)

ASK_TEMPLATE, ASK_EXPRESSIONS, SETTINGS, ASK_VALUE, CONFIRM = range(5)

P = "bat"
PREVIEW_LIMIT = 5

STATE_ORDER = [QUEUED, RUNNING, DONE, FAILED, INTERRUPTED, CANCELLED]


def _brain(context):
    return context.application.bot_data["brain"]


def _catalog(context):
    return context.application.bot_data["catalog"]


def _queue(context):
    return context.application.bot_data["queue"]


def _worker(context):
    return context.application.bot_data["worker"]


def _store(context):
    return context.application.bot_data["store"]


def _draft(context) -> dict:
    return context.user_data.setdefault("batch", {})


def _spec(context) -> AlphaSpec:
    return _draft(context)["spec"]


# ------------------------------------------------------------------- entries


async def batch_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _brain(context).is_authenticated:
        await update.effective_message.reply_text("Not logged in. Send /login first.")
        return ConversationHandler.END

    _draft(context).clear()
    await update.effective_message.reply_text(
        f"{bold('New batch')}\n\nSend your expressions, one per line:\n"
        f"{pre('ts_rank(close,20)\nts_corr(close,volume,10)\nts_delta(vwap,5)')}\n"
        "Lines starting with # are ignored. /cancel to stop.",
        parse_mode=ParseMode.HTML,
    )
    return ASK_EXPRESSIONS


async def from_fields(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for the *Use these N fields* button on a /fields page.

    Registered as an entry point of this conversation rather than handled in
    field_handlers, because only a ConversationHandler can move the chat into a
    state that waits for the template.
    """
    query = update.callback_query
    rows = (context.user_data.get("fields") or {}).get("rows") or []
    if not rows:
        await query.answer("That search expired. Send /fields again.", show_alert=True)
        return ConversationHandler.END

    await query.answer()
    _draft(context).clear()
    _draft(context)["field_rows"] = rows

    ids = ", ".join(r["id"] for r in rows[:3])
    more = f" and {len(rows) - 3} more" if len(rows) > 3 else ""
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=(
            f"{bold(f'Template over {len(rows)} fields')}\n"
            f"{esc(ids)}{esc(more)}\n\n"
            f"Send a template. {code(FIELD_TOKEN)} is replaced by each field id:\n"
            f"{pre(f'ts_sum(vec_avg({FIELD_TOKEN}),120)')}\n"
            "/cancel to stop."
        ),
        parse_mode=ParseMode.HTML,
    )
    return ASK_TEMPLATE


async def got_template(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    draft = _draft(context)
    rows = draft.get("field_rows") or []
    template = (update.effective_message.text or "").strip()

    try:
        expressions = expand_template(template, [r["id"] for r in rows])
    except TemplateError as exc:
        await update.effective_message.reply_text(str(exc))
        return ASK_TEMPLATE

    warning = vector_warning(template, rows)
    if warning:
        await update.effective_message.reply_text(
            f"{bold('Heads up')}\n{esc(warning)}", parse_mode=ParseMode.HTML
        )

    draft["expressions"] = expressions
    return await _show_settings(update, context, edit=False)


async def got_expressions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        expressions = parse_expression_list(update.effective_message.text or "")
    except TemplateError as exc:
        await update.effective_message.reply_text(str(exc))
        return ASK_EXPRESSIONS

    _draft(context)["expressions"] = expressions
    return await _show_settings(update, context, edit=False)


# ------------------------------------------------------------------ settings


async def _show_settings(update: Update, context, *, edit: bool) -> int:
    draft = _draft(context)
    draft.setdefault("spec", AlphaSpec())
    spec = _catalog(context).reconcile(draft["spec"])
    draft["spec"] = spec

    count = len(draft["expressions"])
    card = format_spec_card(
        spec, title=f"Batch settings — {count} alphas", show_expression=False
    )
    text = f"{card}\n\n{_preview(draft['expressions'])}"
    markup = settings_keyboard(spec, P, run_label=f"Queue {count} alphas")

    if edit and update.callback_query:
        await update.callback_query.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=markup
        )
    else:
        await context.bot.send_message(
            chat_id=update.effective_chat.id, text=text,
            parse_mode=ParseMode.HTML, reply_markup=markup,
        )
    return SETTINGS


def _preview(expressions: list[str]) -> str:
    shown = expressions[:PREVIEW_LIMIT]
    body = "\n".join(shown)
    if len(expressions) > PREVIEW_LIMIT:
        body += f"\n… and {len(expressions) - PREVIEW_LIMIT} more"
    return pre(body)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    parts = query.data.split(":", 3)
    action = parts[1]

    if "expressions" not in _draft(context):
        await query.answer("That batch expired. Send /batch again.", show_alert=True)
        return ConversationHandler.END

    if action == "menu":
        await query.answer()
        return await _show_settings(update, context, edit=True)

    if action == "cancel":
        await query.answer("Cancelled.")
        await query.edit_message_text("Cancelled.", parse_mode=ParseMode.HTML)
        context.user_data.pop("batch", None)
        return ConversationHandler.END

    if action == "pick":
        field = parts[2]
        await query.answer()
        options = choices_for(_catalog(context), _spec(context), field)
        await query.edit_message_text(
            f"{bold(CHOICE_LABELS.get(field, field))} — choose one:",
            parse_mode=ParseMode.HTML,
            reply_markup=choice_keyboard(
                field, options, getattr(_spec(context), field), P
            ),
        )
        return SETTINGS

    if action == "set":
        field, raw = parts[2], parts[3]
        spec = _spec(context)
        setattr(spec, field, int(raw) if field == "delay" else raw)
        _draft(context)["spec"] = _catalog(context).reconcile(spec)
        await query.answer(f"{field} = {raw}")
        return await _show_settings(update, context, edit=True)

    if action == "edit":
        field = parts[2]
        _draft(context)["editing"] = field
        await query.answer()
        await query.edit_message_text(edit_hint(field), parse_mode=ParseMode.HTML)
        return ASK_VALUE

    if action == "run":
        await query.answer()
        return await _show_confirm(update, context)

    if action in ("queue_all", "skip_dupes"):
        await query.answer()
        return await _enqueue(update, context, skip_duplicates=action == "skip_dupes")

    await query.answer()
    return SETTINGS


async def got_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    draft = _draft(context)
    field = draft.get("editing")
    if field not in ("decay", "truncation") or "expressions" not in draft:
        await update.effective_message.reply_text(
            "That edit expired. Send /batch to start again."
        )
        return ConversationHandler.END

    parser = parse_decay if field == "decay" else parse_truncation
    try:
        value = parser(update.effective_message.text or "")
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))
        return ASK_VALUE

    setattr(draft["spec"], field, value)
    draft.pop("editing", None)
    return await _show_settings(update, context, edit=False)


# ------------------------------------------------------------------- confirm


async def _show_confirm(update: Update, context) -> int:
    draft = _draft(context)
    specs = specs_from_expressions(draft["expressions"], draft["spec"])
    draft["specs"] = specs

    duplicates = await _find_duplicates(context, specs)
    draft["duplicates"] = duplicates

    bundles = group_into_bundles(specs, _bundle_size(context))
    lines = [
        f"{bold('Confirm batch')}",
        "",
        f"{len(specs)} alphas · {len(bundles)} multi-sim bundle"
        f"{'s' if len(bundles) != 1 else ''}",
        esc(draft["spec"].settings_line()),
    ]

    buttons = []
    if duplicates:
        lines += [
            "",
            f"{bold(f'{len(duplicates)} already simulated')} with these exact settings.",
        ]
        buttons.append(
            [
                InlineKeyboardButton(
                    f"Queue all {len(specs)}", callback_data=f"{P}:queue_all"
                ),
                InlineKeyboardButton(
                    f"Skip {len(duplicates)} duplicates", callback_data=f"{P}:skip_dupes"
                ),
            ]
        )
    else:
        buttons.append(
            [InlineKeyboardButton(f"Queue {len(specs)} alphas", callback_data=f"{P}:queue_all")]
        )
    buttons.append([InlineKeyboardButton("Back", callback_data=f"{P}:menu")])

    await update.callback_query.edit_message_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return CONFIRM


async def _find_duplicates(context, specs) -> set[int]:
    import asyncio

    return await asyncio.to_thread(find_duplicates, _store(context), specs)


def _bundle_size(context) -> int:
    return context.application.bot_data["config"].multi_sim_bundle_size


async def _enqueue(update: Update, context, *, skip_duplicates: bool) -> int:
    import asyncio

    draft = _draft(context)
    specs = draft["specs"]
    duplicates = draft.get("duplicates") or set()

    if skip_duplicates:
        specs = [s for i, s in enumerate(specs) if i not in duplicates]

    if not specs:
        await update.callback_query.edit_message_text(
            "Every alpha in that batch was already simulated. Nothing queued.",
            parse_mode=ParseMode.HTML,
        )
        context.user_data.pop("batch", None)
        return ConversationHandler.END

    batch_id = new_batch_id()
    chat_id = update.effective_chat.id
    await asyncio.to_thread(_queue(context).enqueue, specs, chat_id, batch_id)
    _worker(context).nudge()

    bundles = group_into_bundles(specs, _bundle_size(context))
    skipped = len(duplicates) if skip_duplicates else 0
    note = f"\nSkipped {skipped} already simulated." if skipped else ""

    await update.callback_query.edit_message_text(
        f"{bold(f'Queued {len(specs)} alphas')}  ·  batch {code(batch_id)}\n"
        f"{len(bundles)} bundle{'s' if len(bundles) != 1 else ''} · "
        f"{esc(draft['spec'].settings_line())}{esc(note)}\n\n"
        "Results as they finish. /queue for progress.",
        parse_mode=ParseMode.HTML,
    )
    context.user_data.pop("batch", None)
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("batch", None)
    await update.effective_message.reply_text("Cancelled.")
    return ConversationHandler.END


# -------------------------------------------------------------------- /queue


async def queue_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    import asyncio

    queue = _queue(context)
    arg = (context.args or [""])[0].lower()

    if arg == "cancel":
        n = await asyncio.to_thread(queue.cancel_pending)
        await update.effective_message.reply_text(
            f"Cancelled {n} queued alpha{'s' if n != 1 else ''}. "
            "Anything already running will finish."
        )
        return

    if arg == "retry":
        n = await asyncio.to_thread(queue.retry)
        _worker(context).nudge()
        await update.effective_message.reply_text(
            f"Requeued {n} alpha{'s' if n != 1 else ''}."
        )
        return

    counts = await asyncio.to_thread(queue.counts)
    if not counts:
        await update.effective_message.reply_text(
            "Queue is empty. /batch to add some, or /fields to find datafields."
        )
        return

    # Counts go inside <pre> so the columns line up; headings stay outside it.
    body = [f"{state:<12}{counts[state]:>4}" for state in STATE_ORDER if state in counts]
    parts = [bold("Simulation queue"), pre("\n".join(body))]

    batches = await asyncio.to_thread(queue.active_batches)
    if batches:
        rows = []
        for batch_id in batches:
            progress = await asyncio.to_thread(queue.batch_progress, batch_id)
            total = sum(progress.values())
            settled = progress.get(DONE, 0) + progress.get(FAILED, 0)
            rows.append(f"{batch_id}  {settled}/{total}")
        parts.append(bold("Active batches"))
        parts.append(pre("\n".join(rows)))

    worker = _worker(context)
    parts.append(esc(f"worker: {'running' if worker.running else 'stopped'}"))

    await update.effective_message.reply_text(
        "\n".join(parts), parse_mode=ParseMode.HTML
    )


async def export_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    import asyncio

    config: Config = context.application.bot_data["config"]
    store = _store(context)
    path = await asyncio.to_thread(store.export_csv, config.export_path)
    if path is None:
        await update.effective_message.reply_text("No alphas recorded yet.")
        return

    with open(path, "rb") as handle:
        await context.bot.send_document(
            chat_id=update.effective_chat.id,
            document=handle,
            filename="alphas.csv",
            caption=f"{store.count():,} alphas",
        )


def register(app: Application, config: Config) -> None:
    allowed = filters.Chat(chat_id=set(config.allowed_chat_ids))
    text = allowed & filters.TEXT & ~filters.COMMAND

    app.add_handler(
        ConversationHandler(
            entry_points=[
                CommandHandler("batch", batch_start, filters=allowed),
                CallbackQueryHandler(from_fields, pattern=r"^fld:use$"),
            ],
            states={
                ASK_TEMPLATE: [MessageHandler(text, got_template)],
                ASK_EXPRESSIONS: [MessageHandler(text, got_expressions)],
                SETTINGS: [CallbackQueryHandler(on_button, pattern=rf"^{P}:")],
                ASK_VALUE: [MessageHandler(text, got_value)],
                CONFIRM: [CallbackQueryHandler(on_button, pattern=rf"^{P}:")],
            },
            fallbacks=[CommandHandler("cancel", cancel, filters=allowed)],
            name="batch",
            persistent=False,
        )
    )
    app.add_handler(CommandHandler("queue", queue_cmd, filters=allowed))
    app.add_handler(CommandHandler("export", export_cmd, filters=allowed))
