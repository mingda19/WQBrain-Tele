"""Settings that can hold more than one value.

A batch does not need a separate "which variables are adjustable?" step: every
setting holds a list, and a setting sweeps precisely when that list has more than
one entry. Fixed and adjustable are the same control, so there is one screen
instead of two and the totals stay visible while you edit.

Expanding is the cross-product of the expressions with every setting list, which
grows fast -- four expressions over three decays and two neutralizations is
already 24 alphas -- so ``expand`` is bounded and ``total`` is shown live.
"""

from dataclasses import dataclass, field, replace
from itertools import product
from typing import Iterable

from bot.alpha_spec import (
    DECAY_RANGE,
    TRUNCATION_RANGE,
    AlphaSpec,
    parse_decay,
    parse_truncation,
)

MAX_ALPHAS = 200

# Order is the order shown on the card and used for the cross-product.
SWEEPABLE = [
    "region",
    "universe",
    "delay",
    "decay",
    "neutralization",
    "truncation",
    "test_period",
]

LABELS = {
    "region": "Region",
    "universe": "Universe",
    "delay": "Delay",
    "decay": "Decay",
    "neutralization": "Neutral",
    "truncation": "Trunc",
    "test_period": "Period",
}

# Settings whose values come from the catalog (multi-select) rather than typing.
ENUMERATED = {"region", "universe", "delay", "neutralization", "test_period"}
NUMERIC = {"decay", "truncation"}


class SweepError(ValueError):
    """Invalid sweep input. The message is user-facing."""


def _defaults(name: str) -> list:
    """Single-value defaults taken from AlphaSpec, so the two never drift."""
    return [getattr(AlphaSpec(), name)]


@dataclass
class SweepSettings:
    """One batch's settings, each as a list of values to try."""

    region: list = field(default_factory=lambda: _defaults("region"))
    universe: list = field(default_factory=lambda: _defaults("universe"))
    delay: list = field(default_factory=lambda: _defaults("delay"))
    decay: list = field(default_factory=lambda: _defaults("decay"))
    neutralization: list = field(default_factory=lambda: _defaults("neutralization"))
    truncation: list = field(default_factory=lambda: _defaults("truncation"))
    test_period: list = field(default_factory=lambda: _defaults("test_period"))

    # ------------------------------------------------------------------ shape

    def values(self, name: str) -> list:
        return list(getattr(self, name))

    def set_values(self, name: str, values: Iterable) -> None:
        deduped = _dedupe(values)
        if not deduped:
            raise SweepError(f"{LABELS.get(name, name)} needs at least one value.")
        setattr(self, name, deduped)

    def toggle(self, name: str, value) -> None:
        """Add or remove one value. The last value cannot be removed."""
        current = self.values(name)
        matches = [v for v in current if str(v) == str(value)]
        if matches:
            if len(current) == 1:
                raise SweepError(
                    f"{LABELS.get(name, name)} must keep at least one value."
                )
            setattr(self, name, [v for v in current if str(v) != str(value)])
        else:
            setattr(self, name, current + [_coerce(name, value)])

    def swept(self) -> list[str]:
        return [name for name in SWEEPABLE if len(self.values(name)) > 1]

    def combinations(self) -> int:
        total = 1
        for name in SWEEPABLE:
            total *= len(self.values(name))
        return total

    def total(self, expression_count: int) -> int:
        return expression_count * self.combinations()

    def summary(self, expression_count: int) -> str:
        """`4 expr × 2 × 3 = 24 alphas`, or just the count when nothing sweeps."""
        total = self.total(expression_count)
        swept = self.swept()
        if not swept:
            return f"{total} alpha{'s' if total != 1 else ''}"
        factors = " × ".join(str(len(self.values(n))) for n in swept)
        return f"{expression_count} expr × {factors} = {total} alphas"

    # ----------------------------------------------------------------- expand

    def expand(self, expressions: list[str]) -> list[AlphaSpec]:
        """Cross-product of expressions with every setting list."""
        total = self.total(len(expressions))
        if total == 0:
            raise SweepError("Nothing to queue.")
        if total > MAX_ALPHAS:
            raise SweepError(
                f"That is {total} alphas; the limit is {MAX_ALPHAS}. "
                "Reduce a swept setting or use fewer expressions."
            )

        specs = []
        for expression in expressions:
            for combo in product(*(self.values(name) for name in SWEEPABLE)):
                specs.append(
                    replace(
                        AlphaSpec(expression=expression),
                        **dict(zip(SWEEPABLE, combo)),
                    )
                )
        return specs

    def reconcile(self, catalog) -> None:
        """Drop values the catalog says are unavailable for the chosen regions.

        Universe and neutralization depend on region and delay, so a region sweep
        can strand values that only some of the regions offer. Anything left with
        nothing valid falls back to the first option BRAIN does offer.
        """
        if not catalog.loaded:
            return

        self.set_values(
            "region", [r for r in self.values("region") if r in catalog.regions()]
            or [catalog.regions()[0]]
        )

        valid_delays = {d for r in self.values("region") for d in catalog.delays(r)}
        self.set_values(
            "delay",
            [d for d in self.values("delay") if d in valid_delays]
            or [sorted(valid_delays)[0]],
        )

        for name, lookup in (
            ("universe", catalog.universes),
            ("neutralization", catalog.neutralizations),
        ):
            # Compute the intersection: values valid for ALL (region, delay) pairs.
            # Start with values from the first pair, then intersect with each remaining pair.
            pairs = [
                (r, d) for r in self.values("region") for d in self.values("delay")
            ]
            if not pairs:
                continue  # no pairs to validate against

            allowed = set(lookup(pairs[0][0], pairs[0][1]))
            for r, d in pairs[1:]:
                allowed &= set(lookup(r, d))

            if not allowed:
                continue  # catalog has nothing to say; leave the choice alone
            kept = [v for v in self.values(name) if v in allowed]
            self.set_values(name, kept or [sorted(allowed)[0]])


def _coerce(name: str, value):
    if name == "delay":
        return int(value)
    if name == "decay":
        return int(value)
    if name == "truncation":
        return float(value)
    return value


def _dedupe(values: Iterable) -> list:
    """Preserve order, drop repeats. Order is what the preview shows."""
    seen = set()
    out = []
    for value in values:
        key = str(value)
        if key not in seen:
            seen.add(key)
            out.append(value)
    return out


def parse_number_list(name: str, text: str) -> list:
    """`0,6,12` or `0 6 12` -> [0, 6, 12], validated per value.

    Reuses the single-value parsers so a batch and a /sim reject the same input.
    """
    raw = [part for part in text.replace(",", " ").split() if part]
    if not raw:
        raise SweepError(
            f"Send one or more values, comma separated. "
            f"For example: {'0,6,12' if name == 'decay' else '0.02,0.05,0.08'}"
        )

    parser = parse_decay if name == "decay" else parse_truncation
    values = []
    for part in raw:
        try:
            values.append(parser(part))
        except ValueError as exc:
            raise SweepError(str(exc)) from exc

    deduped = _dedupe(values)
    if len(deduped) > 20:
        raise SweepError(f"{len(deduped)} values is too many; keep it under 20.")
    return deduped


def format_values(name: str, values: list) -> str:
    if name == "truncation":
        return ", ".join(f"{v:g}" for v in values)
    return ", ".join(str(v) for v in values)


def button_label(name: str, values: list) -> str:
    """`Decay: 0` when fixed, `Decay: 3 values` when swept."""
    if len(values) == 1:
        return f"{LABELS[name]}: {format_values(name, values)}"
    return f"{LABELS[name]}: {len(values)} values"


__all__ = [
    "DECAY_RANGE",
    "ENUMERATED",
    "LABELS",
    "MAX_ALPHAS",
    "NUMERIC",
    "SWEEPABLE",
    "TRUNCATION_RANGE",
    "SweepError",
    "SweepSettings",
    "button_label",
    "format_values",
    "parse_number_list",
]
