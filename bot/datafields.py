"""Datafield and dataset discovery.

``ace.get_datafields`` cannot page: with a ``search`` term it hardcodes
``limit=50&offset=0`` (ace_lib.py:1285), with none it sends no ``limit`` at all,
and both paths read only ``["results"]`` -- discarding the response's ``count``.
So "50 results" and "50 of 800" look identical, and there is no way to reach
result 51.

``search_datafields`` therefore calls ``/data-fields`` directly. It still goes
through ``api_url()`` so it can never target a different host than ACE, and still
calls ACE's ``_check_rate_limit`` so a paging loop self-throttles exactly as
``get_datasets`` does. Datasets need none of this and use ACE unchanged.
"""

import logging
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlencode

from bot.ace_bridge import ace, api_url

log = logging.getLogger(__name__)

FIELD_TYPES = {"MATRIX", "VECTOR", "GROUP", "UNIVERSE"}

# Operators that turn a VECTOR field into a matrix. A VECTOR field fed straight
# into a time-series operator fails, which is the most common way a whole batch
# is wasted -- see batching.vector_warning.
VECTOR_OPERATOR = re.compile(r"\bvec_\w+", re.IGNORECASE)

_FILTER = re.compile(r"\b(dataset|type|region|delay|universe):(\S+)", re.IGNORECASE)


@dataclass
class FieldQuery:
    """A parsed /fields query: free text plus optional key:value filters."""

    text: str = ""
    dataset: Optional[str] = None
    field_type: Optional[str] = None
    region: str = "USA"
    delay: int = 1
    universe: str = "TOP3000"

    @property
    def context_line(self) -> str:
        return f"{self.region} · D{self.delay} · {self.universe}"

    def describe(self) -> str:
        bits = [f'"{self.text}"'] if self.text else []
        if self.dataset:
            bits.append(f"dataset:{self.dataset}")
        if self.field_type:
            bits.append(f"type:{self.field_type}")
        return " ".join(bits) or "all fields"


def parse_query(raw: str, *, region="USA", delay=1, universe="TOP3000") -> FieldQuery:
    """Pull `key:value` filters out of a query, leaving the rest as search text.

    ``/fields news18 type:VECTOR`` -> text="news18", field_type="VECTOR".
    Unknown keys are left in the text so they still reach BRAIN's own search.
    """
    query = FieldQuery(region=region, delay=delay, universe=universe)
    remainder = raw

    for match in _FILTER.finditer(raw):
        key, value = match.group(1).lower(), match.group(2)
        if key == "dataset":
            query.dataset = value
        elif key == "type":
            if value.upper() not in FIELD_TYPES:
                continue  # not a real type; leave it in the search text
            query.field_type = value.upper()
        elif key == "region":
            query.region = value.upper()
        elif key == "universe":
            query.universe = value.upper()
        elif key == "delay":
            try:
                query.delay = int(value)
            except ValueError:
                continue
        remainder = remainder.replace(match.group(0), " ")

    query.text = " ".join(remainder.split())
    return query


def _row(record: dict) -> dict:
    """Flatten one API record to the handful of columns worth showing.

    The API returns 21 columns with ``dataset``/``category``/``subcategory`` as
    nested dicts; ACE flattens these with ``expand_dict_columns``, but we read the
    JSON directly so we do it here.
    """
    dataset = record.get("dataset") or {}
    return {
        "id": record.get("id", ""),
        "description": record.get("description") or "",
        "type": record.get("type") or "",
        "dataset_id": dataset.get("id", "") if isinstance(dataset, dict) else "",
        "coverage": record.get("coverage"),
        "user_count": record.get("userCount"),
        "alpha_count": record.get("alphaCount"),
    }


def search_datafields(
    session, query: FieldQuery, *, limit: int, offset: int
) -> tuple[list[dict], int, bool]:
    """One page of datafields.

    Returns ``(rows, total, total_is_exact)``. ``total`` comes from the API's
    ``count`` when the whole query can be pushed server-side. A ``type:`` filter
    is applied client-side to the page, which makes the count an upper bound --
    hence the flag, so the UI can say "of ~137" rather than claiming precision it
    does not have.
    """
    params = {
        "instrumentType": "EQUITY",
        "region": query.region,
        "delay": query.delay,
        "universe": query.universe,
        "limit": limit,
        "offset": offset,
    }
    if query.text:
        params["search"] = query.text
    if query.dataset:
        params["dataset.id"] = query.dataset

    response = session.get(f"{api_url('/data-fields')}?{urlencode(params)}")
    ace._check_rate_limit(response)
    response.raise_for_status()
    payload = response.json()

    rows = [_row(r) for r in payload.get("results", [])]
    total = payload.get("count")
    exact = True

    if query.field_type:
        rows = [r for r in rows if r["type"] == query.field_type]
        exact = False

    if total is None:
        total = offset + len(rows)
        exact = False

    return rows, total, exact


def search_all_datafields(
    session, query: FieldQuery, *, page_size: int = 50, max_rows: int = 1000
) -> list[dict]:
    """Every match, for CSV export. Bounded so a bare query cannot run away.

    ``_check_rate_limit`` inside ``search_datafields`` throttles the loop, so this
    is slow rather than abusive on large result sets.
    """
    collected: list[dict] = []
    offset = 0
    while len(collected) < max_rows:
        rows, total, exact = search_datafields(
            session, query, limit=page_size, offset=offset
        )
        if not rows:
            break
        collected.extend(rows)
        offset += page_size
        if exact and offset >= total:
            break
    return collected[:max_rows]


def search_datasets(session, query: FieldQuery) -> list[dict]:
    """Datasets matching a query. Wraps ``ace.get_datasets`` unchanged.

    The endpoint has no text search, so filtering is client-side over the full
    list -- which is small (a few hundred) and already paged by ACE.
    """
    frame = ace.get_datasets(
        session,
        instrument_type="EQUITY",
        region=query.region,
        delay=query.delay,
        universe=query.universe,
    )
    if frame.empty:
        return []

    records = frame.to_dict("records")
    needle = query.text.lower()
    if needle:
        records = [
            r
            for r in records
            if needle in str(r.get("id", "")).lower()
            or needle in str(r.get("name", "")).lower()
        ]

    return [
        {
            "id": r.get("id", ""),
            "name": r.get("name", ""),
            "field_count": r.get("fieldCount"),
            "value_score": r.get("valueScore"),
            "user_count": r.get("userCount"),
            "alpha_count": r.get("alphaCount"),
        }
        for r in records
    ]
