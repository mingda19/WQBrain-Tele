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


def format_spec_card(spec, *, title: str) -> str:
    """The settings card shown while building and confirming an alpha."""
    expression = spec.expression or "(not set yet)"
    return (
        f"{bold(title)}\n\n"
        f"{pre(expression)}\n"
        f"{bold('Region')}  {esc(spec.region)}\n"
        f"{bold('Universe')}  {esc(spec.universe)}\n"
        f"{bold('Delay')}  {esc(spec.delay)}\n"
        f"{bold('Decay')}  {esc(spec.decay)}\n"
        f"{bold('Neutralization')}  {esc(spec.neutralization)}\n"
        f"{bold('Truncation')}  {esc(f'{spec.truncation:g}')}\n"
        f"{bold('Test period')}  {esc(spec.test_period)}"
    )
