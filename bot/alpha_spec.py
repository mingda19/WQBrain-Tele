"""The alpha being built up in a /sim conversation, and what BRAIN will accept.

``AlphaSpec`` holds the settings; turning it into a payload is delegated to
``ace.generate_alpha`` (ace_lib.py:217), which is a pure dict-builder with no
network call -- exactly the seam a chat flow wants.

``SettingsCatalog`` wraps ``ace.get_instrument_type_region_delay`` so the menus
only ever offer combinations BRAIN actually supports. Universe and neutralization
depend on region and delay, and mismatched settings are a common cause of
simulations failing after the fact rather than being rejected up front.
"""

import asyncio
import logging
from dataclasses import dataclass, replace
from typing import Optional

from bot.ace_bridge import ace

log = logging.getLogger(__name__)

INSTRUMENT_TYPE = "EQUITY"

DECAY_RANGE = (0, 512)
TRUNCATION_RANGE = (0.0, 1.0)
TEST_PERIOD_CHOICES = ["P0Y0M0D", "P1Y", "P2Y", "P3Y", "P5Y"]

# Used only until the catalog loads, and as the starting point for a new /sim.
FALLBACK_REGIONS = ["USA", "GLB", "EUR", "ASI", "CHN"]
FALLBACK_UNIVERSES = ["TOP3000", "TOP1000", "TOP500", "TOP200"]
FALLBACK_NEUTRALIZATIONS = [
    "NONE",
    "MARKET",
    "SECTOR",
    "INDUSTRY",
    "SUBINDUSTRY",
]


@dataclass
class AlphaSpec:
    """One alpha, as assembled through the chat flow.

    Defaults mirror ``ace.generate_alpha`` so an untouched spec behaves exactly
    like the notebook's one-argument call.
    """

    expression: str = ""
    region: str = "USA"
    universe: str = "TOP3000"
    delay: int = 1
    decay: int = 0
    neutralization: str = "INDUSTRY"
    truncation: float = 0.03
    test_period: str = "P1Y"

    def to_simulate_data(self) -> dict:
        return ace.generate_alpha(
            regular=self.expression,
            alpha_type="REGULAR",
            region=self.region,
            universe=self.universe,
            delay=self.delay,
            decay=self.decay,
            neutralization=self.neutralization,
            truncation=self.truncation,
            test_period=self.test_period,
        )

    def settings_line(self) -> str:
        """Compact one-line summary, e.g. 'USA · TOP3000 · D1 · decay 0 · ...'."""
        return " · ".join(
            [
                self.region,
                self.universe,
                f"D{self.delay}",
                f"decay {self.decay}",
                self.neutralization,
                f"trunc {self.truncation:g}",
                self.test_period,
            ]
        )


class SettingsCatalog:
    """Valid region/delay/universe/neutralization combinations, fetched once.

    ``get_instrument_type_region_delay`` issues an OPTIONS request and returns a
    row per (InstrumentType, Region, Delay) with Universe and Neutralization as
    list columns. It is account-wide and effectively static, so one fetch per
    process is plenty.
    """

    def __init__(self) -> None:
        self._rows: Optional[list[dict]] = None
        self._lock = asyncio.Lock()

    @property
    def loaded(self) -> bool:
        return self._rows is not None

    async def load(self, brain) -> bool:
        """Fetch the catalog. Returns False if BRAIN could not be reached."""
        async with self._lock:
            if self._rows is not None:
                return True
            try:
                frame = await brain.run_ace(ace.get_instrument_type_region_delay)
            except Exception:  # noqa: BLE001 -- fall back to defaults, never block /sim
                log.exception("Could not load the settings catalog")
                return False
            self._rows = [
                row
                for row in frame.to_dict("records")
                if row.get("InstrumentType") == INSTRUMENT_TYPE
            ]
            log.info("Settings catalog loaded (%d region/delay rows)", len(self._rows))
            return True

    def _row(self, region: str, delay: int) -> Optional[dict]:
        if not self._rows:
            return None
        for row in self._rows:
            if row["Region"] == region and int(row["Delay"]) == int(delay):
                return row
        return None

    def regions(self) -> list[str]:
        if not self._rows:
            return list(FALLBACK_REGIONS)
        return sorted({row["Region"] for row in self._rows})

    def delays(self, region: str) -> list[int]:
        if not self._rows:
            return [0, 1]
        found = sorted(
            {int(row["Delay"]) for row in self._rows if row["Region"] == region}
        )
        return found or [0, 1]

    def universes(self, region: str, delay: int) -> list[str]:
        row = self._row(region, delay)
        if row is None:
            return list(FALLBACK_UNIVERSES)
        return list(row.get("Universe") or FALLBACK_UNIVERSES)

    def neutralizations(self, region: str, delay: int) -> list[str]:
        row = self._row(region, delay)
        if row is None:
            return list(FALLBACK_NEUTRALIZATIONS)
        return list(row.get("Neutralization") or FALLBACK_NEUTRALIZATIONS)

    def reconcile(self, spec: AlphaSpec) -> AlphaSpec:
        """Pull a spec back into a combination BRAIN will accept.

        Changing region can strand the universe or neutralization on a value that
        region does not offer, which BRAIN rejects only once the simulation is
        already submitted. Snapping here keeps the menus honest.
        """
        if not self._rows:
            return spec

        delays = self.delays(spec.region)
        delay = spec.delay if spec.delay in delays else delays[0]

        universes = self.universes(spec.region, delay)
        universe = spec.universe if spec.universe in universes else universes[0]

        neutralizations = self.neutralizations(spec.region, delay)
        neutralization = (
            spec.neutralization
            if spec.neutralization in neutralizations
            else neutralizations[0]
        )

        return replace(
            spec,
            delay=delay,
            universe=universe,
            neutralization=neutralization,
        )


def parse_decay(text: str) -> int:
    low, high = DECAY_RANGE
    try:
        value = int(text.strip())
    except ValueError:
        raise ValueError(f"Decay must be a whole number between {low} and {high}.")
    if not low <= value <= high:
        raise ValueError(f"Decay must be between {low} and {high}, got {value}.")
    return value


def parse_truncation(text: str) -> float:
    low, high = TRUNCATION_RANGE
    try:
        value = float(text.strip())
    except ValueError:
        raise ValueError(f"Truncation must be a number between {low} and {high}.")
    if not low <= value <= high:
        raise ValueError(f"Truncation must be between {low} and {high}, got {value}.")
    return value
