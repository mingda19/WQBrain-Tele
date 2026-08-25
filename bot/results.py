"""Rendering simulation outcomes for Telegram."""

from bot.config import DEFAULT_BRAIN_URL
from bot.formatting import bold, code, esc, pre
from bot.simulation import SimOutcome

# Ordered as they read on the platform's IS summary.
METRIC_ROWS = [
    ("Sharpe", "sharpe", "{:.2f}"),
    ("Fitness", "fitness", "{:.2f}"),
    ("Turnover", "turnover", "{:.2%}"),
    ("Returns", "returns", "{:.2%}"),
    ("Drawdown", "drawdown", "{:.2%}"),
    ("Margin", "margin", "bps"),
    ("PnL", "pnl", "money"),
    ("Long/Short", "long_count", "counts"),
]

TEST_MARKS = {"PASS": "PASS", "FAIL": "FAIL", "PENDING": "pend", "WARNING": "warn"}


def alpha_url(alpha_id: str) -> str:
    return f"{DEFAULT_BRAIN_URL}/alpha/{alpha_id}"


def _format_metric(fmt: str, value, metrics: dict) -> str:
    if value is None:
        return "-"
    if fmt == "bps":
        # BRAIN reports margin as a fraction; the platform shows basis points.
        return f"{value * 10000:.2f} bps"
    if fmt == "money":
        return f"{value:,.0f}"
    if fmt == "counts":
        short = metrics.get("short_count")
        return f"{value:,.0f} / {short:,.0f}" if short is not None else f"{value:,.0f}"
    return fmt.format(value)


def format_metrics_block(metrics: dict) -> str:
    if not metrics:
        return "No in-sample stats returned."
    lines = []
    for label, key, fmt in METRIC_ROWS:
        if key not in metrics:
            continue
        lines.append(f"{label:<11}{_format_metric(fmt, metrics[key], metrics)}")
    return "\n".join(lines)


def format_tests_block(tests: list[dict], limit: int = 12) -> str:
    """Failures first -- they are the reason an alpha cannot be submitted."""
    if not tests:
        return "No checks returned."

    order = {"FAIL": 0, "WARNING": 1, "PASS": 2, "PENDING": 3}
    ranked = sorted(tests, key=lambda t: (order.get(t["result"], 4), t["name"]))

    lines = []
    for test in ranked[:limit]:
        mark = TEST_MARKS.get(test["result"], test["result"][:4])
        line = f"{mark}  {test['name']}"
        if test.get("value") is not None and test.get("limit") is not None:
            line += f"  ({test['value']:g} vs {test['limit']:g})"
        lines.append(line)

    if len(ranked) > limit:
        lines.append(f"... and {len(ranked) - limit} more")
    return "\n".join(lines)


def _checks_heading(outcome: SimOutcome) -> str:
    pending = sum(1 for t in outcome.tests if t["result"] == "PENDING")
    parts = [f"Checks: {len(outcome.passed_tests)} pass"]
    if outcome.failed_tests:
        parts.append(f"{len(outcome.failed_tests)} fail")
    if pending:
        parts.append(f"{pending} pending")
    return " · ".join(parts)


def format_outcome(outcome: SimOutcome) -> str:
    """The message posted when a simulation finishes."""
    spec = outcome.spec

    if not outcome.ok:
        return (
            f"{bold('Simulation failed')}\n\n"
            f"{pre(spec.expression)}\n"
            f"{esc(spec.settings_line())}\n\n"
            f"{esc(outcome.error or 'Unknown error.')}"
        )

    if outcome.all_passed:
        headline = "Simulation complete — all checks passed"
    elif outcome.failed_tests:
        failed = len(outcome.failed_tests)
        headline = f"Simulation complete — {failed} check{'s' if failed > 1 else ''} failed"
    else:
        headline = "Simulation complete"

    parts = [
        bold(headline),
        "",
        f"{code(outcome.alpha_id)}  ·  {esc(alpha_url(outcome.alpha_id))}",
        "",
        pre(spec.expression),
        esc(spec.settings_line()),
        "",
        pre(format_metrics_block(outcome.metrics)),
        bold(_checks_heading(outcome)),
        pre(format_tests_block(outcome.tests)),
    ]

    if outcome.error:
        parts.append(esc(outcome.error))

    return "\n".join(parts)


def _truncate(text: str, width: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= width else text[: width - 1].rstrip() + "…"


def _stats_line(row: dict) -> str:
    bits = []
    if row.get("coverage") is not None:
        bits.append(f"cov {row['coverage']:.2f}")
    if row.get("user_count") is not None:
        bits.append(f"{row['user_count']:,} users")
    if row.get("alpha_count") is not None:
        bits.append(f"{row['alpha_count']:,} alphas")
    return " · ".join(bits)


def format_field_page(
    rows: list[dict], query, *, offset: int, total: int, exact: bool,
    selected: set | None = None,
) -> str:
    """One page of datafield cards.

    Cards rather than a table: the description is what tells you whether a field
    is worth testing, and it does not survive being squeezed into a column.
    """
    if not rows:
        return (
            f"{bold('No datafields found')}\n"
            f"{esc(query.describe())} · {esc(query.context_line)}\n\n"
            "Try a broader term, or check the region/delay/universe."
        )

    selected = selected or set()
    shown = f"{offset + 1}–{offset + len(rows)}"
    count = f"{total:,}" if exact else f"~{total:,}"
    header = (
        f"{bold('Datafields')} — {esc(query.describe())}\n"
        f"{esc(shown)} of {esc(count)} · {esc(query.context_line)}"
    )
    if selected:
        header += f"\n{bold(f'{len(selected)} selected')}"

    cards = []
    for row in rows:
        mark = "☑ " if row["id"] in selected else ""
        block = [f"{esc(mark)}{code(row['id'])}   {esc(row['type'])}"]
        if row["description"]:
            block.append(f"  {esc(_truncate(row['description'], 62))}")
        stats = _stats_line(row)
        if stats:
            block.append(f"  {esc(stats)}")
        cards.append("\n".join(block))

    return header + "\n\n" + "\n\n".join(cards)


def format_dataset_page(
    rows: list[dict], query, *, offset: int, total: int
) -> str:
    if not rows:
        return (
            f"{bold('No datasets found')}\n"
            f"{esc(query.describe())} · {esc(query.context_line)}"
        )

    shown = f"{offset + 1}–{offset + len(rows)}"
    header = (
        f"{bold('Datasets')} — {esc(query.describe())}\n"
        f"{esc(shown)} of {total:,} · {esc(query.context_line)}"
    )

    cards = []
    for row in rows:
        bits = []
        if row.get("field_count") is not None:
            bits.append(f"{row['field_count']:,} fields")
        if row.get("value_score") is not None:
            bits.append(f"value {row['value_score']:g}")
        if row.get("user_count") is not None:
            bits.append(f"{row['user_count']:,} users")
        cards.append(
            f"{code(row['id'])}\n"
            f"  {esc(_truncate(row['name'], 62))}\n"
            f"  {esc(' · '.join(bits))}"
        )
    return header + "\n\n" + "\n\n".join(cards)


def format_sweep_card(sweep, expressions: list[str], *, title: str) -> str:
    """Batch settings card: every setting with its value(s), plus the total.

    Swept settings are marked with their count so the cross-product is never a
    surprise -- the total line is the thing that stops a four-way sweep from
    quietly becoming 200 simulations.
    """
    from bot.sweep import LABELS, SWEEPABLE, format_values

    # The table goes in <pre>: Telegram renders <b> in a proportional font, so
    # space-padded columns only line up inside a monospace block.
    table = []
    for name in SWEEPABLE:
        values = sweep.values(name)
        rendered = format_values(name, values)
        marker = f"   ({len(values)})" if len(values) > 1 else ""
        table.append(f"{LABELS[name]:<10}{rendered}{marker}")

    parts = [bold(title), pre("\n".join(table)), bold(sweep.summary(len(expressions)))]

    swept = sweep.swept()
    if swept:
        parts.append(esc(f"sweeping {', '.join(LABELS[n].lower() for n in swept)}"))

    return "\n".join(parts) + "\n\n" + _expression_preview(expressions)


def _expression_preview(expressions: list[str], limit: int = 5) -> str:
    shown = expressions[:limit]
    body = "\n".join(shown)
    if len(expressions) > limit:
        body += f"\n… and {len(expressions) - limit} more"
    return pre(body)


def format_spec_card(spec, *, title: str, show_expression: bool = True) -> str:
    """The settings card shown while building and confirming an alpha.

    ``show_expression=False`` is for the batch flow, where the spec carries only
    settings and the expressions are previewed as their own list.
    """
    expression = (
        f"{pre(spec.expression or '(not set yet)')}\n" if show_expression else ""
    )
    return (
        f"{bold(title)}\n\n"
        f"{expression}"
        f"{bold('Region')}  {esc(spec.region)}\n"
        f"{bold('Universe')}  {esc(spec.universe)}\n"
        f"{bold('Delay')}  {esc(spec.delay)}\n"
        f"{bold('Decay')}  {esc(spec.decay)}\n"
        f"{bold('Neutralization')}  {esc(spec.neutralization)}\n"
        f"{bold('Truncation')}  {esc(f'{spec.truncation:g}')}\n"
        f"{bold('Test period')}  {esc(spec.test_period)}"
    )
