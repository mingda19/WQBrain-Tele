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
from bot.formatting import bold, esc, pre
from bot.results import format_dataset_page, format_field_page

log = logging.getLogger(__name__)

P = "fld"
DATASET_PAGE = 8

CSV_COLUMNS = ["id", "type", "dataset_id", "coverage", "user_count", "alpha_count", "description"]


def _brain(context):
    return context.application.bot_data["brain"]


def _state(context) -> dict:
    return context.user_data.setdefault("fields", {})


def _selection(context) -> dict:
    """Chosen field id -> its row.

    Kept outside the page state so it survives paging: you can gather fields from
    several pages, or several searches, before queueing them.
    """
    return context.user_data.setdefault("field_selection", {})


def _keyboard(context, rows_on_page: list[dict], *, offset: int, page_size: int, total: int):
    state = _state(context)
    query = state["query"]
    selected = _selection(context)

    keyboard = [
        [
            InlineKeyboardButton(
                f"{'☑' if row['id'] in selected else '☐'} {row['id']}",
                callback_data=f"{P}:tog:{row['id']}",
            )
        ]
        for row in rows_on_page
    ]

    if rows_on_page:
        page_ids = {r["id"] for r in rows_on_page}
        all_on_page = page_ids <= set(selected)
        keyboard.append(
            [
                InlineKeyboardButton(
                    "Clear page" if all_on_page else "Select page",
                    callback_data=f"{P}:{'clearpage' if all_on_page else 'selpage'}",
                ),
                InlineKeyboardButton("Clear all", callback_data=f"{P}:clear"),
            ]
        )

    # Market context -- the same catalog /sim uses, so only valid combinations.
    keyboard.append(
        [
            InlineKeyboardButton(f"Region: {query.region}", callback_data=f"{P}:ctx:region"),
            InlineKeyboardButton(f"Delay: {query.delay}", callback_data=f"{P}:ctx:delay"),
        ]
    )
    keyboard.append(
        [
            InlineKeyboardButton(
                f"Universe: {query.universe}", callback_data=f"{P}:ctx:universe"
            )
        ]
    )

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
    if len(nav) > 1:
        keyboard.append(nav)

    action = [InlineKeyboardButton("Export CSV", callback_data=f"{P}:csv")]
    if selected:
        action.insert(
            0,
            InlineKeyboardButton(
                f"Use {len(selected)} selected", callback_data=f"{P}:use"
            ),
        )
    keyboard.append(action)

    return InlineKeyboardMarkup(keyboard)


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

    text = format_field_page(
        rows, query, offset=offset, total=total, exact=exact,
        selected=set(_selection(context)),
    )
    markup = _keyboard(
        context, rows, offset=offset, page_size=page_size, total=total
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
    parts = query_cb.data.split(":", 2)
    action = parts[1]
    argument = parts[2] if len(parts) > 2 else ""
    state = _state(context)

    if action == "noop":
        await query_cb.answer()
        return

    if not state.get("query"):
        await query_cb.answer("That search expired. Send /fields again.", show_alert=True)
        return

    if action == "page":
        await query_cb.answer()
        await _render_fields(update, context, offset=int(argument), edit=True)
        return

    if action == "tog":
        selected = _selection(context)
        if argument in selected:
            del selected[argument]
        else:
            row = next((r for r in state.get("rows", []) if r["id"] == argument), None)
            if row:
                selected[argument] = row
        await query_cb.answer(f"{len(selected)} selected")
        await _render_fields(update, context, offset=state["offset"], edit=True)
        return

    if action in ("selpage", "clearpage", "clear"):
        selected = _selection(context)
        if action == "clear":
            selected.clear()
        elif action == "selpage":
            selected.update({r["id"]: r for r in state.get("rows", [])})
        else:
            for row in state.get("rows", []):
                selected.pop(row["id"], None)
        await query_cb.answer(f"{len(selected)} selected")
        await _render_fields(update, context, offset=state["offset"], edit=True)
        return

    if action == "ctx":
        await query_cb.answer()
        await _show_context_choices(update, context, argument)
        return

    if action == "setctx":
        name, value = argument.split(":", 1)
        query = state["query"]
        setattr(query, name, int(value) if name == "delay" else value)
        _snap_context(context)
        await query_cb.answer(f"{name} = {getattr(query, name)}")
        # A different market means different fields, so start from page one.
        await _render_fields(update, context, offset=0, edit=True)
        return

    if action == "csv":
        await query_cb.answer("Building CSV…")
        await _send_csv(update, context)
        return

    await query_cb.answer()


def _catalog(context):
    return context.application.bot_data["catalog"]


def _snap_context(context) -> None:
    """Pull universe and delay back to something the chosen region offers.

    Changing region can strand a universe that only the previous region had, and
    BRAIN would return an empty result rather than an error.
    """
    catalog = _catalog(context)
    if not catalog.loaded:
        return
    query = _state(context)["query"]

    delays = catalog.delays(query.region)
    if query.delay not in delays:
        query.delay = delays[0]

    universes = catalog.universes(query.region, query.delay)
    if universes and query.universe not in universes:
        query.universe = universes[0]


async def _show_context_choices(update: Update, context, name: str) -> None:
    catalog = _catalog(context)
    query = _state(context)["query"]

    if name == "region":
        options = catalog.regions()
    elif name == "delay":
        options = catalog.delays(query.region)
    else:
        options = catalog.universes(query.region, query.delay)

    buttons = [
        InlineKeyboardButton(
            f"{'• ' if str(option) == str(getattr(query, name)) else ''}{option}",
            callback_data=f"{P}:setctx:{name}:{option}",
        )
        for option in options
    ]
    rows = [buttons[i : i + 3] for i in range(0, len(buttons), 3)]
    rows.append(
        [InlineKeyboardButton("Back", callback_data=f"{P}:page:{_state(context)['offset']}")]
    )

    await update.callback_query.edit_message_text(
        f"{bold(name.title())} — choose one\n"
        f"{esc('changing this re-runs the search')}",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows),
    )


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

    fd, path_str = tempfile.mkstemp(suffix=".csv", prefix="datafields_")
    path = Path(path_str)
    try:
        with open(fd, "w", newline="", encoding="utf-8") as handle:
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
    finally:
        path.unlink(missing_ok=True)


def register(app: Application, config: Config) -> None:
    allowed = filters.Chat(chat_id=set(config.allowed_chat_ids))
    app.add_handler(CommandHandler("fields", fields, filters=allowed))
    app.add_handler(CommandHandler("datasets", datasets, filters=allowed))
    # "fld:use" is deliberately excluded: it is an entry point of the /batch
    # ConversationHandler, which is the only thing that can move the chat into a
    # state waiting for a template.
    app.add_handler(
        CallbackQueryHandler(
            on_button,
            pattern=rf"^{P}:(page|csv|noop|tog|selpage|clearpage|clear|ctx|setctx)",
        )
    )
