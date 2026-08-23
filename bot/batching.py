"""Turning ideas into a queueable list of alphas.

Three jobs: expand a template over datafields, warn about the mistakes that waste
a whole batch, and group specs into bundles BRAIN will actually accept.
"""

import logging
from dataclasses import replace
from typing import Iterable, Optional

from bot.alpha_spec import AlphaSpec
from bot.datafields import VECTOR_OPERATOR

log = logging.getLogger(__name__)

FIELD_TOKEN = "{f}"
MAX_BUNDLE = 10  # BRAIN's ceiling for one multi-simulation
MAX_BATCH = 200  # sanity bound on one /batch or template expansion


class TemplateError(ValueError):
    """The template cannot produce expressions. Message is user-facing."""


def expand_template(template: str, field_ids: Iterable[str]) -> list[str]:
    """`ts_sum(vec_avg({f}),120)` over N fields -> N expressions."""
    template = template.strip()
    if not template:
        raise TemplateError("The template is empty.")
    if FIELD_TOKEN not in template:
        raise TemplateError(
            f"The template must contain {FIELD_TOKEN}, which is replaced by each "
            f"field id. For example: ts_sum(vec_avg({FIELD_TOKEN}),120)"
        )

    fields = [f.strip() for f in field_ids if f and f.strip()]
    if not fields:
        raise TemplateError("No datafields selected.")
    if len(fields) > MAX_BATCH:
        raise TemplateError(
            f"That would be {len(fields)} alphas; the limit is {MAX_BATCH}. "
            "Narrow the field selection."
        )

    return [template.replace(FIELD_TOKEN, field) for field in fields]


def parse_expression_list(text: str) -> list[str]:
    """One expression per line, blanks and #comments dropped."""
    expressions = []
    for line in (text or "").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            expressions.append(line)

    if not expressions:
        raise TemplateError("No expressions found. Send one per line.")
    if len(expressions) > MAX_BATCH:
        raise TemplateError(
            f"That is {len(expressions)} expressions; the limit is {MAX_BATCH}."
        )
    return expressions


def vector_warning(template: str, rows: list[dict]) -> Optional[str]:
    """Flag VECTOR fields used without a vec_ operator.

    A VECTOR datafield fed straight into a time-series operator fails on BRAIN's
    side, and because a template applies to every field at once, the mistake
    costs the entire batch rather than one simulation.
    """
    vector_ids = [r["id"] for r in rows if r.get("type") == "VECTOR"]
    if not vector_ids or VECTOR_OPERATOR.search(template):
        return None

    sample = ", ".join(vector_ids[:3])
    more = f" and {len(vector_ids) - 3} more" if len(vector_ids) > 3 else ""
    return (
        f"{len(vector_ids)} of these are VECTOR fields ({sample}{more}) but the "
        f"template has no vec_ operator. VECTOR fields must be aggregated first, "
        f"e.g. vec_avg({FIELD_TOKEN}) — otherwise every one of these will fail."
    )


def specs_from_expressions(expressions: Iterable[str], base: AlphaSpec) -> list[AlphaSpec]:
    """Same settings, one spec per expression."""
    return [replace(base, expression=expression) for expression in expressions]


def bundle_key(spec: AlphaSpec) -> tuple:
    """What must match for two alphas to share a multi-simulation.

    The documented constraint is region and delay (notebook cell 42). Universe is
    included because it is part of the same market definition and mixing it has
    no legitimate use. Decay, neutralization and truncation are deliberately left
    out so a settings sweep still packs into full bundles -- tighten here if
    BRAIN turns out to reject mixed settings within a bundle.
    """
    return (spec.region, spec.delay, spec.universe)


def group_into_bundles(
    specs: list[AlphaSpec], max_size: int = MAX_BUNDLE
) -> list[list[AlphaSpec]]:
    """Group by bundle key, then chunk. Order within a group is preserved."""
    max_size = max(1, min(max_size, MAX_BUNDLE))
    groups: dict[tuple, list[AlphaSpec]] = {}
    for spec in specs:
        groups.setdefault(bundle_key(spec), []).append(spec)

    bundles = []
    for group in groups.values():
        for i in range(0, len(group), max_size):
            bundles.append(group[i : i + max_size])
    return bundles


def find_duplicates(store, specs: list[AlphaSpec]) -> set[int]:
    """Indices of specs already simulated with identical settings.

    Matches on expression plus every settings column, so the same expression at a
    different decay is correctly treated as a new experiment.
    """
    duplicates = set()
    for index, spec in enumerate(specs):
        if store.has_simulated(spec):
            duplicates.add(index)
    return duplicates
