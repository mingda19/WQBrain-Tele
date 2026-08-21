"""HTML-safe rendering for Telegram.

Every BRAIN identifier is full of underscores -- ``nws18_qmb``, ``ts_sum``,
``vec_avg`` -- which Telegram's Markdown parser silently eats as italics markers.
So all outgoing text uses ParseMode.HTML with escaped content, established here
once rather than retrofitted later across alpha expressions.
"""

import html
from datetime import datetime, timezone


def esc(text: object) -> str:
    """Escape arbitrary text for Telegram's HTML parse mode."""
    return html.escape(str(text), quote=False)


def code(text: object) -> str:
    """Inline monospace, safe for expressions and identifiers."""
    return f"<code>{esc(text)}</code>"


def pre(text: object) -> str:
    """Monospace block, for anything multi-line."""
    return f"<pre>{esc(text)}</pre>"


def bold(text: object) -> str:
    return f"<b>{esc(text)}</b>"


def human_duration(seconds: float) -> str:
    """`14395` -> `3h 59m`. Coarse on purpose: this is for glanceable status."""
    seconds = int(max(0, seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def local_time(moment: datetime) -> str:
    """Render an aware datetime in the host's local zone."""
    return moment.astimezone().strftime("%H:%M:%S %Z")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
