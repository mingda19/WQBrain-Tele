"""/menu and per-topic help.

Telegram's command grammar is ``/[a-zA-Z0-9_]+``, so ``/help-fields`` arrives as
``/help`` with a dangling ``-fields`` and BotFather will not accept a hyphenated
name for the autocomplete menu. ``/help_fields`` is therefore the real command --
it autocompletes -- while ``/help fields`` and ``/help-fields`` are both accepted
so whatever you type works.
"""

import logging

from telegram import BotCommand, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, filters

from bot.config import Config
from bot.formatting import bold, code, esc, pre

log = logging.getLogger(__name__)


class Topic:
    def __init__(self, name, command, blurb, detail, examples=()):
        self.name = name
        self.command = command
        self.blurb = blurb
        self.detail = detail
        self.examples = examples


TOPICS = {
    "login": Topic(
        "login", "/login",
        "Authenticate with BRAIN.",
        "Signs in using BRAIN_CREDENTIAL_EMAIL and BRAIN_CREDENTIAL_PASSWORD from "
        ".env — your password never travels through Telegram. If BRAIN asks for "
        "biometrics you get a one-time link and an 'I've completed it' button; the "
        "bot also polls every 5s for 10 minutes, so the button is a shortcut, not a "
        "requirement.\n\n"
        "A session lasts about 4 hours. You get a warning 5 minutes before it dies, "
        "with buttons to re-login or snooze. A background check every 10 minutes "
        "catches the cases a timer cannot see: the machine sleeping, or the session "
        "being ended from the platform in a browser.",
        ["/login", "/status", "/relogin", "/logout"],
    ),
    "status": Topic(
        "status", "/status",
        "Time left on the BRAIN session.",
        "Re-checks against BRAIN rather than trusting the local clock, so it is "
        "accurate after the machine has slept. Shows remaining time, wall-clock "
        "expiry and when you logged in.",
    ),
    "whoami": Topic(
        "whoami", "/whoami",
        "Verify the session actually works.",
        "Reads your account record. A successful /login only proves the handshake "
        "returned 201; this proves the session can really read from BRAIN.",
    ),
    "sim": Topic(
        "sim", "/sim",
        "Build and run one alpha, guided.",
        "Asks for an expression, then shows a settings card — region, universe, "
        "delay, neutralization, decay, truncation, test period. Tap any button to "
        "change it, then confirm.\n\n"
        "The menus are built from BRAIN's own list of valid combinations, so "
        "changing region snaps universe and neutralization to values that region "
        "actually offers. That stops the most common cause of a simulation failing "
        "only after submission.\n\n"
        "A simulation takes minutes, so it runs in the background and the result "
        "arrives as its own message.",
        ["/sim", "then: ts_sum(vec_avg(nws18_qmb),120)"],
    ),
    "fields": Topic(
        "fields", "/fields",
        "Search datafields, then pick the ones you want.",
        "Bare words go to BRAIN's search. Filters can be mixed in:\n"
        "  dataset:news18   type:VECTOR   region:CHN   delay:0   universe:TOP2000U\n\n"
        "Results come back as cards with a toggle button each. Your selection "
        "survives paging and further searches, so you can gather fields from "
        "several pages before queueing them. Region, delay and universe also have "
        "buttons — changing one re-runs the search.\n\n"
        "Once you have a selection, 'Use N selected' takes you into a template so "
        "one expression becomes one alpha per field.",
        [
            "/fields sentiment",
            "/fields news18 type:VECTOR",
            "/fields dataset:fundamental23 region:CHN delay:0",
        ],
    ),
    "datasets": Topic(
        "datasets", "/datasets",
        "List datasets and their field counts.",
        "Shows id, name, number of fields, value score and how many users are on "
        "each. Use the id with /fields dataset:<id> to see its fields.",
        ["/datasets news", "/datasets fundamental"],
    ),
    "batch": Topic(
        "batch", "/batch",
        "Queue many alphas at once, with optional parameter sweeps.",
        "Two ways in: paste expressions one per line, or pick datafields with "
        "/fields and give a template where {f} is the field.\n\n"
        "Then the settings card. Every setting holds a list of values — pick one "
        "value to fix it, or several to sweep it. The total is the cross-product of "
        "your expressions with every swept setting, shown live so a four-way sweep "
        "cannot quietly become 200 simulations.\n\n"
        "Before queueing, anything already simulated with identical settings is "
        "flagged, and you choose whether to queue all or skip the duplicates.",
        [
            "/batch",
            "then: ts_rank(close,20)",
            "      ts_corr(close,volume,10)",
            "then set Decay to 0,6,12 to sweep it",
        ],
    ),
    "queue": Topic(
        "queue", "/queue",
        "Queue state and progress.",
        "Counts by state plus per-batch progress.\n\n"
        "  /queue          show state\n"
        "  /queue cancel   cancel everything still queued\n"
        "  /queue retry    requeue interrupted and failed alphas\n\n"
        "The queue lives on disk, so a restart or a closed lid does not lose it. "
        "Anything caught mid-flight by a shutdown is marked interrupted rather than "
        "retried automatically — it may well have finished on BRAIN's side, so "
        "re-running it would waste quota. /queue retry requeues those deliberately.\n\n"
        "Bundles never mix region, delay or universe, because multi-simulation "
        "requires them to match.",
        ["/queue", "/queue cancel", "/queue retry"],
    ),
    "alphas": Topic(
        "alphas", "/alphas",
        "The 10 most recently simulated alphas.",
        "Alpha id, sharpe, fitness and whether every check passed. Everything ever "
        "simulated is recorded, including failures — /export for the full table.",
    ),
    "export": Topic(
        "export", "/export",
        "CSV of every recorded alpha.",
        "Expression, all settings, sharpe, fitness, turnover, drawdown, margin, "
        "returns, PnL and the pass/fail counts.",
    ),
}

# Order shown in /menu.
GROUPS = [
    ("Session", ["login", "status", "whoami"]),
    ("Research", ["fields", "datasets"]),
    ("Simulate", ["sim", "batch", "queue"]),
    ("Results", ["alphas", "export"]),
]


def _normalise(raw: str) -> str:
    """Accept `fields`, `-fields`, `_fields`, `/fields`, `help_fields` alike.

    The three entry shapes all land here: /help fields gives "fields" in args,
    /help_fields and /help-fields leave "_fields" / "-fields" after the command.
    """
    text = raw.strip().lower().lstrip("-/_ ")
    if text.startswith("help"):
        text = text[4:].lstrip("-/_ ")
    return text


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lines = [bold("What this bot can do"), ""]
    for heading, names in GROUPS:
        lines.append(bold(heading))
        lines.extend(
            f"{code(TOPICS[n].command)} — {esc(TOPICS[n].blurb)}" for n in names
        )
        lines.append("")
    lines.append(f"{code('/relogin')} · {code('/logout')} · {code('/cancel')}")
    lines.append("")
    lines.append(
        esc("Details for any of these: /help_fields, /help_batch, … or /help <name>.")
    )
    await update.effective_message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.HTML
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles /help, /help <name>, /help-<name> and /help_<name>."""
    raw = " ".join(context.args or [])
    if not raw:
        # /help-fields arrives as command "help" with "-fields" glued to the text.
        text = update.effective_message.text or ""
        raw = text.partition("/help")[2]

    name = _normalise(raw)
    if not name:
        await _topic_index(update)
        return

    topic = TOPICS.get(name)
    if topic is None:
        matches = [k for k in TOPICS if k.startswith(name)]
        if len(matches) == 1:
            topic = TOPICS[matches[0]]
        else:
            await update.effective_message.reply_text(
                f"No help for {code(name)}.\n"
                f"Topics: {esc(', '.join(sorted(TOPICS)))}",
                parse_mode=ParseMode.HTML,
            )
            return

    body = [f"{bold(topic.command)} — {esc(topic.blurb)}", "", esc(topic.detail)]
    if topic.examples:
        body += ["", bold("Examples"), pre("\n".join(topic.examples))]
    await update.effective_message.reply_text(
        "\n".join(body), parse_mode=ParseMode.HTML
    )


async def _topic_index(update: Update) -> None:
    lines = [bold("Help topics"), ""]
    lines.extend(
        f"{code('/help_' + name)} — {esc(topic.blurb)}"
        for name, topic in sorted(TOPICS.items())
    )
    lines.append("")
    lines.append(esc("/menu for the full command list."))
    await update.effective_message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.HTML
    )


async def post_init(app: Application) -> None:
    """Register the command list Telegram shows in its autocomplete menu."""
    commands = [BotCommand("menu", "Everything this bot can do")]
    commands += [
        BotCommand(topic.command.lstrip("/"), topic.blurb)
        for _, names in GROUPS
        for topic in (TOPICS[n] for n in names)
    ]
    commands.append(BotCommand("help", "Help on a topic"))
    try:
        await app.bot.set_my_commands(commands)
    except Exception:  # noqa: BLE001 -- cosmetic; never block startup
        log.exception("Could not register the command menu")


def register(app: Application, config: Config) -> None:
    allowed = filters.Chat(chat_id=set(config.allowed_chat_ids))
    app.add_handler(CommandHandler("menu", menu, filters=allowed))
    app.add_handler(CommandHandler("help", help_cmd, filters=allowed))
    # Real commands, so they appear in autocomplete and work on their own.
    for name in TOPICS:
        app.add_handler(CommandHandler(f"help_{name}", help_cmd, filters=allowed))
