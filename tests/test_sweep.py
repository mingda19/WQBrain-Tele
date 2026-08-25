"""Tests for multi-value settings, sweep expansion, and help topics.

The cross-product is the part that can quietly go wrong: a four-way sweep is
200 simulations, and a region sweep can strand a universe that only one region
offers. Both are driven hard here.

Run: python -m pytest tests/ -q
"""

import pytest

from bot.alpha_spec import AlphaSpec, SettingsCatalog
from bot.batching import group_into_bundles
from bot.help_handlers import GROUPS, TOPICS, _normalise
from bot.results import format_sweep_card
from bot.settings_ui import multi_choice_keyboard, sweep_keyboard
from bot.sweep import (
    MAX_ALPHAS,
    SWEEPABLE,
    SweepError,
    SweepSettings,
    button_label,
    parse_number_list,
)

EXPRESSIONS = ["a", "b", "c", "d"]


@pytest.fixture
def sweep():
    return SweepSettings()


@pytest.fixture
def catalog():
    cat = SettingsCatalog()
    cat._rows = [
        {"InstrumentType": "EQUITY", "Region": "USA", "Delay": 1,
         "Universe": ["TOP3000", "TOP1000"],
         "Neutralization": ["INDUSTRY", "MARKET", "NONE"]},
        {"InstrumentType": "EQUITY", "Region": "USA", "Delay": 0,
         "Universe": ["TOP3000"], "Neutralization": ["INDUSTRY"]},
        {"InstrumentType": "EQUITY", "Region": "CHN", "Delay": 1,
         "Universe": ["TOP2000U"], "Neutralization": ["MARKET", "NONE"]},
    ]
    return cat


# ------------------------------------------------------------------- defaults


def test_defaults_track_alpha_spec(sweep):
    """The two must not drift; /sim and /batch should start identically."""
    base = AlphaSpec()
    for name in SWEEPABLE:
        assert sweep.values(name) == [getattr(base, name)]


def test_nothing_sweeps_by_default(sweep):
    assert sweep.swept() == []
    assert sweep.combinations() == 1
    assert sweep.total(4) == 4


# --------------------------------------------------------------------- sweeps


def test_more_than_one_value_makes_a_setting_sweep(sweep):
    sweep.set_values("decay", [0, 6, 12])
    assert sweep.swept() == ["decay"]
    assert sweep.total(4) == 12


def test_sweeps_multiply(sweep):
    sweep.set_values("decay", [0, 6, 12])
    sweep.set_values("neutralization", ["INDUSTRY", "SUBINDUSTRY"])
    assert sweep.combinations() == 6
    assert sweep.total(4) == 24
    assert sweep.summary(4) == "4 expr × 3 × 2 = 24 alphas"


def test_summary_without_a_sweep_is_just_a_count(sweep):
    assert sweep.summary(4) == "4 alphas"
    assert sweep.summary(1) == "1 alpha"


def test_expand_produces_every_combination(sweep):
    sweep.set_values("decay", [0, 6])
    sweep.set_values("neutralization", ["INDUSTRY", "MARKET"])
    specs = sweep.expand(["x"])

    assert len(specs) == 4
    assert {(s.decay, s.neutralization) for s in specs} == {
        (0, "INDUSTRY"), (0, "MARKET"), (6, "INDUSTRY"), (6, "MARKET")
    }
    assert {s.expression for s in specs} == {"x"}


def test_expand_covers_expressions_too(sweep):
    sweep.set_values("decay", [0, 6])
    specs = sweep.expand(EXPRESSIONS)

    assert len(specs) == 8
    assert sorted({s.expression for s in specs}) == EXPRESSIONS


def test_expand_is_bounded(sweep):
    sweep.set_values("decay", list(range(20)))
    sweep.set_values("truncation", [0.01 * i for i in range(1, 20)])
    with pytest.raises(SweepError, match=str(MAX_ALPHAS)):
        sweep.expand(EXPRESSIONS)


def test_region_sweep_produces_separate_bundles(sweep):
    """Multi-sim cannot mix regions, so a region sweep must split."""
    sweep.set_values("region", ["USA", "CHN"])
    specs = sweep.expand(EXPRESSIONS)
    bundles = group_into_bundles(specs, 10)

    assert len(specs) == 8
    assert len(bundles) == 2
    for bundle in bundles:
        assert len({s.region for s in bundle}) == 1


# --------------------------------------------------------------------- toggle


def test_toggle_adds_and_removes(sweep):
    sweep.toggle("neutralization", "MARKET")
    assert sweep.values("neutralization") == ["INDUSTRY", "MARKET"]

    sweep.toggle("neutralization", "MARKET")
    assert sweep.values("neutralization") == ["INDUSTRY"]


def test_toggle_refuses_to_empty_a_setting(sweep):
    with pytest.raises(SweepError, match="at least one"):
        sweep.toggle("neutralization", "INDUSTRY")


def test_toggle_coerces_numeric_settings(sweep):
    """Callback data is always a string; delay must come back as an int."""
    sweep.toggle("delay", "0")
    assert sweep.values("delay") == [1, 0]
    assert all(isinstance(v, int) for v in sweep.values("delay"))


def test_set_values_deduplicates_and_keeps_order(sweep):
    sweep.set_values("decay", [6, 0, 6, 12, 0])
    assert sweep.values("decay") == [6, 0, 12]


def test_set_values_rejects_an_empty_list(sweep):
    with pytest.raises(SweepError):
        sweep.set_values("decay", [])


# --------------------------------------------------------------- number lists


@pytest.mark.parametrize(
    "text,expected",
    [("0,6,12", [0, 6, 12]), ("0 6 12", [0, 6, 12]), (" 6 ", [6]), ("0,0,6", [0, 6])],
)
def test_parse_decay_list(text, expected):
    assert parse_number_list("decay", text) == expected


def test_parse_truncation_list():
    assert parse_number_list("truncation", "0.02,0.05") == [0.02, 0.05]


@pytest.mark.parametrize("text", ["", "   ", "abc", "0,999", "-1"])
def test_parse_number_list_rejects_bad_input(text):
    with pytest.raises(SweepError):
        parse_number_list("decay", text)


def test_parse_number_list_is_bounded():
    with pytest.raises(SweepError, match="too many"):
        parse_number_list("decay", ",".join(str(i) for i in range(25)))


# ------------------------------------------------------------------ reconcile


def test_reconcile_drops_a_universe_the_region_lacks(sweep, catalog):
    sweep.set_values("region", ["CHN"])
    sweep.reconcile(catalog)

    assert sweep.values("universe") == ["TOP2000U"]
    assert sweep.values("neutralization") == ["MARKET"]


def test_reconcile_keeps_values_valid_for_any_swept_region(sweep, catalog):
    """A region sweep should keep what at least one of the regions supports."""
    sweep.set_values("region", ["USA", "CHN"])
    sweep.set_values("neutralization", ["INDUSTRY", "MARKET"])
    sweep.reconcile(catalog)

    assert set(sweep.values("neutralization")) == {"INDUSTRY", "MARKET"}


def test_reconcile_is_a_noop_without_a_catalog(sweep):
    sweep.set_values("universe", ["ANYTHING"])
    sweep.reconcile(SettingsCatalog())
    assert sweep.values("universe") == ["ANYTHING"]


def test_reconcile_snaps_an_unavailable_delay(sweep, catalog):
    sweep.set_values("region", ["CHN"])
    sweep.set_values("delay", [0])  # CHN only has delay 1 in this catalog
    sweep.reconcile(catalog)
    assert sweep.values("delay") == [1]


# ------------------------------------------------------------------ rendering


def test_button_label_shows_value_or_count(sweep):
    assert button_label("decay", [0]) == "Decay: 0"
    assert button_label("decay", [0, 6, 12]) == "Decay: 3 values"
    assert button_label("truncation", [0.03]) == "Trunc: 0.03"


def test_sweep_card_shows_totals_and_what_sweeps(sweep):
    sweep.set_values("decay", [0, 6, 12])
    text = format_sweep_card(sweep, EXPRESSIONS, title="Batch — 4 expressions")

    assert "4 expr × 3 = 12 alphas" in text
    assert "sweeping decay" in text
    assert "0, 6, 12" in text
    assert "<pre>" in text, "the table must be monospace to line up"
    assert len(text) < 4096


def test_sweep_keyboard_has_a_button_per_setting(sweep):
    markup = sweep_keyboard(sweep, "bat", run_label="Queue 4 alphas")
    labels = [b.text for row in markup.inline_keyboard for b in row]

    assert len(labels) == len(SWEEPABLE) + 2  # + Queue + Cancel
    assert "Queue 4 alphas" in labels
    assert any(label.startswith("Region:") for label in labels)


def test_multi_choice_keyboard_marks_selected():
    markup = multi_choice_keyboard(
        "neutralization", ["INDUSTRY", "MARKET", "NONE"], ["INDUSTRY", "NONE"], "bat"
    )
    labels = [b.text for row in markup.inline_keyboard for b in row]

    assert "✓ INDUSTRY" in labels
    assert "MARKET" in labels
    assert "✓ NONE" in labels
    assert "Done" in labels


def test_multi_choice_callbacks_fit_telegrams_limit():
    markup = multi_choice_keyboard(
        "neutralization", ["SUBINDUSTRY", "STATISTICAL"], ["SUBINDUSTRY"], "bat"
    )
    for row in markup.inline_keyboard:
        for button in row:
            assert len(button.callback_data.encode()) <= 64


# ---------------------------------------------------------------------- help


def test_every_grouped_topic_exists():
    for _, names in GROUPS:
        for name in names:
            assert name in TOPICS


@pytest.mark.parametrize(
    "raw", ["fields", "-fields", "_fields", "/fields", "help_fields", "help-fields"]
)
def test_all_help_spellings_resolve(raw):
    assert _normalise(raw) == "fields"


def test_empty_help_argument_means_the_index():
    assert _normalise("") == ""
    assert _normalise("/help") == ""


def test_topics_document_their_own_command():
    for name, topic in TOPICS.items():
        assert topic.command.startswith("/")
        assert topic.blurb and topic.blurb[0].isupper()
        assert len(topic.blurb) < 80, f"{name} blurb too long for a command list"
