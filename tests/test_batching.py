"""Tests for query parsing, templating, bundling and duplicate detection.

Run: python -m pytest tests/ -q
"""

import pytest

from bot.alpha_spec import AlphaSpec
from bot.batching import (
    MAX_BATCH,
    TemplateError,
    bundle_key,
    expand_template,
    find_duplicates,
    group_into_bundles,
    parse_expression_list,
    specs_from_expressions,
    vector_warning,
)
from bot.datafields import parse_query
from bot.results import format_field_page
from bot.simulation import SimOutcome
from bot.store import AlphaStore

FIELDS = ["nws18_bam", "nws18_bee", "nws18_ber"]


def rows(*ids, field_type="VECTOR"):
    return [
        {
            "id": i,
            "type": field_type,
            "description": f"desc {i}",
            "dataset_id": "news18",
            "coverage": 1.0,
            "user_count": 10,
            "alpha_count": 20,
        }
        for i in ids
    ]


# ------------------------------------------------------------- query parsing


def test_parse_plain_text():
    q = parse_query("news sentiment")
    assert q.text == "news sentiment"
    assert q.field_type is None and q.dataset is None


def test_parse_pulls_filters_out_of_text():
    q = parse_query("sentiment dataset:news18 type:VECTOR")
    assert q.text == "sentiment"
    assert q.dataset == "news18"
    assert q.field_type == "VECTOR"


def test_parse_overrides_market_context():
    q = parse_query("growth region:CHN delay:0 universe:TOP2000U")
    assert (q.region, q.delay, q.universe) == ("CHN", 0, "TOP2000U")
    assert q.text == "growth"


def test_parse_keeps_unknown_type_as_search_text():
    """type:BANANA is not a field type, so it belongs to BRAIN's text search."""
    q = parse_query("type:BANANA")
    assert q.field_type is None
    assert "BANANA" in q.text


def test_parse_ignores_non_numeric_delay():
    q = parse_query("x delay:soon")
    assert q.delay == 1


def test_context_line_reads_like_the_platform():
    assert parse_query("x").context_line == "USA · D1 · TOP3000"


# ----------------------------------------------------------------- templating


def test_expand_template_substitutes_each_field():
    out = expand_template("ts_sum(vec_avg({f}),120)", FIELDS)
    assert out == [
        "ts_sum(vec_avg(nws18_bam),120)",
        "ts_sum(vec_avg(nws18_bee),120)",
        "ts_sum(vec_avg(nws18_ber),120)",
    ]


def test_expand_template_requires_the_token():
    with pytest.raises(TemplateError, match=r"\{f\}"):
        expand_template("ts_rank(close,20)", FIELDS)


def test_expand_template_rejects_empty_inputs():
    with pytest.raises(TemplateError):
        expand_template("", FIELDS)
    with pytest.raises(TemplateError):
        expand_template("vec_avg({f})", [])


def test_expand_template_is_bounded():
    with pytest.raises(TemplateError, match="limit"):
        expand_template("vec_avg({f})", [f"f{i}" for i in range(MAX_BATCH + 1)])


def test_template_can_use_the_field_twice():
    out = expand_template("ts_corr(vec_avg({f}),ts_delay(vec_avg({f}),1),20)", ["x"])
    assert out == ["ts_corr(vec_avg(x),ts_delay(vec_avg(x),1),20)"]


def test_vector_warning_fires_without_a_vec_operator():
    warning = vector_warning("ts_sum({f},120)", rows(*FIELDS))
    assert warning is not None
    assert "VECTOR" in warning and "vec_" in warning


def test_vector_warning_silent_when_vec_operator_present():
    assert vector_warning("ts_sum(vec_avg({f}),120)", rows(*FIELDS)) is None


def test_vector_warning_silent_for_matrix_fields():
    assert vector_warning("ts_sum({f},120)", rows("close", field_type="MATRIX")) is None


def test_expression_list_drops_blanks_and_comments():
    out = parse_expression_list(
        "ts_rank(close,20)\n\n# a comment\n  ts_delta(vwap,5)  \n"
    )
    assert out == ["ts_rank(close,20)", "ts_delta(vwap,5)"]


def test_expression_list_rejects_nothing_usable():
    with pytest.raises(TemplateError):
        parse_expression_list("\n# only comments\n")


# ------------------------------------------------------------------ bundling


def test_specs_share_settings_and_differ_only_by_expression():
    base = AlphaSpec(region="CHN", decay=6, universe="TOP2000U")
    specs = specs_from_expressions(["a", "b"], base)
    assert [s.expression for s in specs] == ["a", "b"]
    assert all(s.region == "CHN" and s.decay == 6 for s in specs)
    assert base.expression == "", "the base spec must not be mutated"


def test_bundles_never_mix_region_or_delay():
    """The documented multi-sim constraint: one region and delay per bundle."""
    specs = [
        AlphaSpec(expression="a", region="USA", delay=1),
        AlphaSpec(expression="b", region="CHN", delay=1),
        AlphaSpec(expression="c", region="USA", delay=0),
        AlphaSpec(expression="d", region="USA", delay=1),
    ]
    bundles = group_into_bundles(specs, 10)

    assert len(bundles) == 3
    for bundle in bundles:
        assert len({bundle_key(s) for s in bundle}) == 1
    biggest = max(bundles, key=len)
    assert [s.expression for s in biggest] == ["a", "d"]


def test_bundles_chunk_at_the_limit():
    specs = [AlphaSpec(expression=f"e{i}") for i in range(25)]
    bundles = group_into_bundles(specs, 10)

    assert [len(b) for b in bundles] == [10, 10, 5]
    assert sum(len(b) for b in bundles) == 25


def test_bundle_size_is_clamped_to_brains_ceiling():
    specs = [AlphaSpec(expression=f"e{i}") for i in range(30)]
    assert all(len(b) <= 10 for b in group_into_bundles(specs, 999))


def test_single_spec_still_forms_a_bundle():
    assert group_into_bundles([AlphaSpec(expression="a")], 10) == [
        [AlphaSpec(expression="a")]
    ]


def test_grouping_preserves_order_within_a_group():
    specs = [AlphaSpec(expression=str(i)) for i in range(15)]
    bundles = group_into_bundles(specs, 10)
    assert [s.expression for s in bundles[0]] == [str(i) for i in range(10)]


# ------------------------------------------------------------------- dedup


@pytest.fixture
def store(tmp_path):
    return AlphaStore(tmp_path / "alphas.db")


def record(store, spec, alpha_id="A1"):
    store.record(
        SimOutcome(
            spec=spec, ok=True, alpha_id=alpha_id,
            metrics={"sharpe": 1.0},
            tests=[{"name": "T", "result": "PASS", "limit": None, "value": None}],
        )
    )


def test_duplicate_requires_expression_and_settings(store):
    spec = AlphaSpec(expression="ts_rank(close,20)", decay=0)
    record(store, spec)

    assert find_duplicates(store, [spec]) == {0}

    different_decay = AlphaSpec(expression="ts_rank(close,20)", decay=6)
    assert find_duplicates(store, [different_decay]) == set(), (
        "a different decay is a new experiment, not a duplicate"
    )


def test_duplicate_survives_float_truncation_round_trip(store):
    """SQLite stores truncation as REAL; equality on floats is not safe."""
    spec = AlphaSpec(expression="x", truncation=0.08)
    record(store, spec)
    assert find_duplicates(store, [AlphaSpec(expression="x", truncation=0.08)]) == {0}


def test_duplicate_indices_map_back_to_input_order(store):
    a = AlphaSpec(expression="a")
    c = AlphaSpec(expression="c")
    record(store, a, "A")
    record(store, c, "C")

    specs = [a, AlphaSpec(expression="b"), c]
    assert find_duplicates(store, specs) == {0, 2}


def test_nothing_is_a_duplicate_in_an_empty_store(store):
    assert find_duplicates(store, [AlphaSpec(expression="a")]) == set()


# ---------------------------------------------------------------- rendering


def test_field_page_shows_ids_types_and_counts():
    text = format_field_page(
        rows(*FIELDS), parse_query("sentiment"), offset=0, total=137, exact=True
    )
    assert "nws18_bam" in text
    assert "VECTOR" in text
    assert "1–3 of 137" in text
    assert "USA · D1 · TOP3000" in text
    assert len(text) < 4096


def test_field_page_marks_an_inexact_total():
    text = format_field_page(
        rows(*FIELDS), parse_query("x type:VECTOR"), offset=0, total=137, exact=False
    )
    assert "~137" in text, "a client-side filter makes the count an upper bound"


def test_field_page_offset_is_human_numbered():
    text = format_field_page(
        rows(*FIELDS), parse_query("x"), offset=16, total=137, exact=True
    )
    assert "17–19 of 137" in text


def test_empty_field_page_suggests_what_to_do():
    text = format_field_page([], parse_query("zzz"), offset=0, total=0, exact=True)
    assert "No datafields found" in text


def test_field_page_escapes_html_in_descriptions():
    row = rows("x")[0]
    row["description"] = "a < b & c"
    text = format_field_page([row], parse_query("x"), offset=0, total=1, exact=True)
    assert "&lt;" in text and "&amp;" in text
