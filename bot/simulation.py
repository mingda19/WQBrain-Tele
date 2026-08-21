"""Running a simulation and normalising what comes back.

ACE returns pandas DataFrames nested inside a dict. Everything downstream --
Telegram rendering, the SQLite store -- wants plain values, so the DataFrame
handling is confined to this module.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from bot.ace_bridge import ace
from bot.alpha_spec import AlphaSpec

log = logging.getLogger(__name__)

# Refuse to dispatch with less than this much session left. ACE's own refresh is
# disarmed (see install_session_guards), so the bot tops the session up itself
# rather than letting a long simulation run off the end of it.
MIN_SESSION_SECONDS = 2400.0

METRIC_KEYS = {
    "sharpe": "sharpe",
    "fitness": "fitness",
    "turnover": "turnover",
    "drawdown": "drawdown",
    "margin": "margin",
    "returns": "returns",
    "pnl": "pnl",
    "longCount": "long_count",
    "shortCount": "short_count",
    "bookSize": "book_size",
}


@dataclass
class SimOutcome:
    spec: AlphaSpec
    ok: bool = False
    alpha_id: Optional[str] = None
    metrics: dict = field(default_factory=dict)
    tests: list[dict] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def failed_tests(self) -> list[dict]:
        return [t for t in self.tests if t.get("result") == "FAIL"]

    @property
    def passed_tests(self) -> list[dict]:
        return [t for t in self.tests if t.get("result") == "PASS"]

    @property
    def all_passed(self) -> bool:
        """True when nothing failed and at least one check actually ran.

        PENDING checks (self/prod correlation) are not treated as failures --
        they resolve later, via /check on the platform.
        """
        return bool(self.tests) and not self.failed_tests


def _clean(value):
    """pandas NaN and numpy scalars -> None / plain Python."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if hasattr(value, "item"):
        try:
            return value.item()
        except (ValueError, AttributeError):
            return value
    return value


def _extract_metrics(is_stats) -> dict:
    if not isinstance(is_stats, pd.DataFrame) or is_stats.empty:
        return {}
    row = is_stats.iloc[0]
    metrics = {}
    for source, target in METRIC_KEYS.items():
        if source in row.index:
            metrics[target] = _clean(row[source])
    return metrics


def _extract_tests(is_tests) -> list[dict]:
    if not isinstance(is_tests, pd.DataFrame) or is_tests.empty:
        return []
    tests = []
    for _, row in is_tests.iterrows():
        name = _clean(row.get("name"))
        if name is None:
            continue
        tests.append(
            {
                "name": str(name),
                "result": str(_clean(row.get("result")) or "UNKNOWN"),
                "limit": _clean(row.get("limit")),
                "value": _clean(row.get("value")),
            }
        )
    return tests


async def run_simulation(brain, spec: AlphaSpec) -> SimOutcome:
    """Submit one alpha and collect its in-sample stats and checks.

    Both ACE calls block for the duration of the simulation (they poll on
    Retry-After), so both go through ``brain.run_ace`` onto a worker thread.
    """
    simulate_data = spec.to_simulate_data()

    try:
        submitted = await brain.run_ace(ace.simulate_single_alpha, simulate_data)
    except Exception as exc:  # noqa: BLE001
        log.exception("Simulation submission failed")
        return SimOutcome(spec=spec, error=f"Could not submit: {exc}")

    alpha_id = submitted.get("alpha_id")
    if not alpha_id:
        return SimOutcome(
            spec=spec,
            error=(
                "BRAIN rejected the simulation. Usually the expression has a "
                "syntax error, or a datafield is not available for this "
                "region/delay/universe."
            ),
        )

    try:
        stats = await brain.run_ace(
            ace.get_specified_alpha_stats,
            alpha_id,
            submitted.get("simulate_data", simulate_data),
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("Fetching stats failed for %s", alpha_id)
        return SimOutcome(
            spec=spec,
            alpha_id=alpha_id,
            ok=True,
            error=f"Simulated, but stats could not be read: {exc}",
        )

    return SimOutcome(
        spec=spec,
        ok=True,
        alpha_id=alpha_id,
        metrics=_extract_metrics(stats.get("is_stats")),
        tests=_extract_tests(stats.get("is_tests")),
    )


class SimulationRunner:
    """Caps how many simulations are in flight at once.

    BRAIN allows a limited number of concurrent simulations per account and
    rejects the excess rather than queueing them, so the ceiling is enforced
    here. The semaphore is also what the batch queue will meter against.
    """

    def __init__(self, brain, max_concurrent: int) -> None:
        self._brain = brain
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self.max_concurrent = max_concurrent
        self._in_flight = 0

    @property
    def in_flight(self) -> int:
        return self._in_flight

    async def run(self, spec: AlphaSpec) -> SimOutcome:
        async with self._semaphore:
            self._in_flight += 1
            try:
                return await run_simulation(self._brain, spec)
            finally:
                self._in_flight -= 1
