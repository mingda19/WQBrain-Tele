"""/fields and /datasets — datafield discovery, with pagination.

Owns the ``fld:`` callback prefix. handlers.py owns ``persona:``/``session:``,
sim_handlers.py owns ``sim:``, queue_handlers.py owns ``bat:``; an unscoped
CallbackQueryHandler here would swallow all of them.
"""

import csv
import logging
import tempfile
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, filters

from bot.config import Config
from bot.datafields import parse_query, search_all_datafields, search_datafields, search_datasets
from bot.formatting import bold, pre
from bot.results import format_dataset_page, format_field_page

log = logging.getLogger(__name__)

P = "fld"
DATASET_PAGE = 8

CSV_COLUMNS = ["id", "type", "dataset_id", "coverage", "user_count", "alpha_count", "description"]


def _brain(context):
    return context.application.bot_data["brain"]


def _state(context) -> dict:
    return context.user_data.setdefault("fields", {})


def _keyboard(context, *, offset: int, page_size: int, total: int, count: int):
    state = _state(context)
    nav = []
    if offset > 0:
        nav.append(
            InlineKeyboardButton("◀", callback_data=f"{P}:page:{max(0, offset - page_size)}")
        )
    page_no = offset // page_size + 1
    pages = max(1, -(-total // page_size))
    nav.append(InlineKeyboardButton(f"{page_no} / {pages}", callback_data=f"{P}:noop"))
    if offset + page_size < total:
        nav.append(InlineKeyboardButton("▶", callback_data=f"{P}:page:{offset + page_size}"))

    rows = [nav] if len(nav) > 1 else []
    if state.get("kind") == "fields" and count:
        rows.append(
            [
                InlineKeyboardButton(
                    f"Use these {count} fields", callback_data=f"{P}:use"
                ),
                InlineKeyboardButton("Export CSV", callback_data=f"{P}:csv"),
            ]
        )
    return InlineKeyboardMarkup(rows) if rows else None


async def _render_fields(update, context, *, offset: int, edit: bool) -> None:
    state = _state(context)
    query = state["query"]
    page_size = state["page_size"]
    chat_id = update.effective_chat.id

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    brain = _brain(context)
    try:
        rows, total, exact = await brain.run_ace(
            lambda s: search_datafields(s, query, limit=page_size, offset=offset)
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("Datafield search failed")
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"{bold('Search failed.')}\n{pre(repr(exc))}",
            parse_mode=ParseMode.HTML,
        )
        return

    state.update(offset=offset, total=total, ids=[r["id"] for r in rows], rows=rows)

    text = format_field_page(rows, query, offset=offset, total=total, exact=exact)
    markup = _keyboard(
        context, offset=offset, page_size=page_size, total=total, count=len(rows)
    )

    if edit and update.callback_query:
        await update.callback_query.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=markup,
            disable_web_page_preview=True,
        )
    else:
        await context.bot.send_message(
            chat_id=chat_id, text=text, parse_mode=ParseMode.HTML,
            reply_markup=markup, disable_web_page_preview=True,
        )


async def fields(update: Update, context) -> None:
    brain = _brain(context)
    if not brain.is_authenticated:
        await update.effective_message.reply_text("Not logged in. Send /login first.")
        return

    raw = " ".join(context.args or [])
    config: Config = context.application.bot_data["config"]
    query = parse_query(raw)

    _state(context).clear()
    _state(context).update(
        kind="fields", query=query, page_size=config.fields_page_size, offset=0
    )
    await _render_fields(update, context, offset=0, edit=False)


async def datasets(update: Update, context) -> None:
    brain = _brain(context)
    if not brain.is_authenticated:
        await update.effective_message.reply_text("Not logged in. Send /login first.")
        return

    query = parse_query(" ".join(context.args or []))
    chat_id = update.effective_chat.id
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    try:
        rows = await brain.run_ace(lambda s: search_datasets(s, query))
    except Exception as exc:  # noqa: BLE001
        log.exception("Dataset search failed")
        await update.effective_message.reply_text(
            f"{bold('Search failed.')}\n{pre(repr(exc))}", parse_mode=ParseMode.HTML
        )
        return

    await update.effective_message.reply_text(
        format_dataset_page(rows[:DATASET_PAGE], query, offset=0, total=len(rows)),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def on_button(update: Update, context) -> None:
    query_cb = update.callback_query
    action = query_cb.data.split(":", 2)[1]
    state = _state(context)

    if action == "noop":
        await query_cb.answer()
        return

    if not state.get("query"):
        await query_cb.answer("That search expired. Send /fields again.", show_alert=True)
        return

    if action == "page":
        offset = int(query_cb.data.split(":", 2)[2])
        await query_cb.answer()
        await _render_fields(update, context, offset=offset, edit=True)
        return

    if action == "csv":
        await query_cb.answer("Building CSV…")
        await _send_csv(update, context)
        return

    await query_cb.answer()


async def _send_csv(update: Update, context) -> None:
    state = _state(context)
    chat_id = update.effective_chat.id
    brain = _brain(context)

    try:
        rows = await brain.run_ace(
            lambda s: search_all_datafields(s, state["query"])
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("CSV export failed")
        await context.bot.send_message(
            chat_id=chat_id, text=f"{bold('Export failed.')}\n{pre(repr(exc))}",
            parse_mode=ParseMode.HTML,
        )
        return

    if not rows:
        await context.bot.send_message(chat_id=chat_id, text="Nothing to export.")
        return

    path = Path(tempfile.gettempdir()) / "datafields.csv"
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    with open(path, "rb") as handle:
        await context.bot.send_document(
            chat_id=chat_id,
            document=handle,
            filename="datafields.csv",
            caption=f"{len(rows):,} datafields · {state['query'].context_line}",
        )
    path.unlink(missing_ok=True)


def register(app: Application, config: Config) -> None:
    allowed = filters.Chat(chat_id=set(config.allowed_chat_ids))
    app.add_handler(CommandHandler("fields", fields, filters=allowed))
    app.add_handler(CommandHandler("datasets", datasets, filters=allowed))
    # "fld:use" is deliberately excluded: it is an entry point of the /batch
    # ConversationHandler, which is the only thing that can move the chat into a
    # state waiting for a template.
    app.add_handler(CallbackQueryHandler(on_button, pattern=rf"^{P}:(page|csv|noop)"))
