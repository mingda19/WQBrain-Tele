"""Tests for picking datafields and changing the market context.

The point of the selection living outside the page state is that it survives
paging and further searches -- so that is what these check hardest.

Run: python -m pytest tests/ -q
"""

import asyncio

import pytest

from bot import field_handlers as fh
from bot.alpha_spec import SettingsCatalog
from bot.datafields import parse_query
from tests.fakes import FakeContext, FakeUpdate

CHAT = 4242

PAGE_ONE = [
    {"id": "nws18_bam", "type": "VECTOR", "description": "M&A", "coverage": 1.0,
     "user_count": 307, "alpha_count": 899},
    {"id": "nws18_bee", "type": "VECTOR", "description": "earnings", "coverage": 1.0,
     "user_count": 195, "alpha_count": 434},
]
PAGE_TWO = [
    {"id": "nws18_ber", "type": "VECTOR", "description": "result", "coverage": 1.0,
     "user_count": 230, "alpha_count": 563},
]


@pytest.fixture
def catalog():
    cat = SettingsCatalog()
    cat._rows = [
        {"InstrumentType": "EQUITY", "Region": "USA", "Delay": 1,
         "Universe": ["TOP3000", "TOP1000"], "Neutralization": ["INDUSTRY"]},
        {"InstrumentType": "EQUITY", "Region": "CHN", "Delay": 1,
         "Universe": ["TOP2000U"], "Neutralization": ["MARKET"]},
    ]
    return cat


@pytest.fixture
def config():
    class C:
        fields_page_size = 2
        allowed_chat_ids = frozenset({CHAT})

        def is_allowed(self, chat_id):
            return chat_id == CHAT

    return C()


@pytest.fixture
def context(config, catalog):
    ctx = FakeContext(config)
    ctx.application.bot_data["catalog"] = catalog
    ctx.user_data = {}
    ctx.application.bot_data["brain"] = None
    return ctx


def state(context, rows, *, offset=0, query=None):
    fh._state(context).update(
        kind="fields",
        query=query or parse_query("sentiment"),
        page_size=2,
        offset=offset,
        rows=rows,
        ids=[r["id"] for r in rows],
        total=3,
    )


def press(context, data, rows_after=None):
    """Run on_button with rendering stubbed, since rendering needs the network."""
    update = FakeUpdate(CHAT, callback_data=data)
    rendered = {}

    async def fake_render(_update, _context, *, offset, edit):
        rendered["offset"] = offset
        if rows_after is not None:
            fh._state(_context)["rows"] = rows_after

    original = fh._render_fields
    fh._render_fields = fake_render
    try:
        asyncio.run(fh.on_button(update, context))
    finally:
        fh._render_fields = original
    return update, rendered


# ------------------------------------------------------------------ selecting


def test_tapping_a_field_selects_it(context):
    state(context, PAGE_ONE)
    press(context, "fld:tog:nws18_bam")

    assert set(fh._selection(context)) == {"nws18_bam"}


def test_tapping_again_deselects(context):
    state(context, PAGE_ONE)
    press(context, "fld:tog:nws18_bam")
    press(context, "fld:tog:nws18_bam")

    assert fh._selection(context) == {}


def test_selection_survives_paging(context):
    """The whole point: gather fields from several pages before queueing."""
    state(context, PAGE_ONE)
    press(context, "fld:tog:nws18_bam")

    state(context, PAGE_TWO, offset=2)
    press(context, "fld:tog:nws18_ber")

    assert set(fh._selection(context)) == {"nws18_bam", "nws18_ber"}


def test_select_page_adds_everything_on_it(context):
    state(context, PAGE_ONE)
    press(context, "fld:selpage")

    assert set(fh._selection(context)) == {"nws18_bam", "nws18_bee"}


def test_clear_page_leaves_other_pages_selected(context):
    state(context, PAGE_ONE)
    press(context, "fld:selpage")
    state(context, PAGE_TWO, offset=2)
    press(context, "fld:selpage")

    state(context, PAGE_ONE, offset=0)
    press(context, "fld:clearpage")

    assert set(fh._selection(context)) == {"nws18_ber"}


def test_clear_all_empties_the_selection(context):
    state(context, PAGE_ONE)
    press(context, "fld:selpage")
    press(context, "fld:clear")

    assert fh._selection(context) == {}


def test_selecting_keeps_the_row_not_just_the_id(context):
    """The template step needs `type` to warn about VECTOR fields."""
    state(context, PAGE_ONE)
    press(context, "fld:tog:nws18_bam")

    assert fh._selection(context)["nws18_bam"]["type"] == "VECTOR"


def test_toggle_of_an_unknown_id_is_ignored(context):
    state(context, PAGE_ONE)
    press(context, "fld:tog:not_on_this_page")

    assert fh._selection(context) == {}


def test_paging_does_not_disturb_the_selection(context):
    state(context, PAGE_ONE)
    press(context, "fld:tog:nws18_bam")
    _, rendered = press(context, "fld:page:2")

    assert rendered["offset"] == 2
    assert set(fh._selection(context)) == {"nws18_bam"}


# ------------------------------------------------------------ market context


def test_changing_region_updates_the_query(context):
    state(context, PAGE_ONE)
    press(context, "fld:setctx:region:CHN")

    assert fh._state(context)["query"].region == "CHN"


def test_changing_region_snaps_a_stranded_universe(context):
    """TOP3000 does not exist for CHN; leaving it would return nothing."""
    state(context, PAGE_ONE)
    assert fh._state(context)["query"].universe == "TOP3000"

    press(context, "fld:setctx:region:CHN")

    assert fh._state(context)["query"].universe == "TOP2000U"


def test_changing_context_restarts_at_page_one(context):
    state(context, PAGE_ONE, offset=4)
    _, rendered = press(context, "fld:setctx:region:CHN")

    assert rendered["offset"] == 0, "a different market has different fields"


def test_delay_from_callback_data_becomes_an_int(context):
    state(context, PAGE_ONE)
    press(context, "fld:setctx:delay:1")

    assert fh._state(context)["query"].delay == 1
    assert isinstance(fh._state(context)["query"].delay, int)


def test_buttons_report_an_expired_search(context):
    update, _ = press(context, "fld:tog:nws18_bam")
    assert "expired" in (update.callback_query.answers[0] or "")


def test_keyboard_shows_selection_and_context(context):
    state(context, PAGE_ONE)
    press(context, "fld:tog:nws18_bam")

    markup = fh._keyboard(context, PAGE_ONE, offset=0, page_size=2, total=3)
    labels = [b.text for row in markup.inline_keyboard for b in row]

    assert "☑ nws18_bam" in labels
    assert "☐ nws18_bee" in labels
    assert "Region: USA" in labels
    assert "Universe: TOP3000" in labels
    assert "Use 1 selected" in labels


def test_use_button_appears_only_with_a_selection(context):
    state(context, PAGE_ONE)
    markup = fh._keyboard(context, PAGE_ONE, offset=0, page_size=2, total=3)
    labels = [b.text for row in markup.inline_keyboard for b in row]

    assert not any(label.startswith("Use ") for label in labels)


def test_field_callbacks_fit_telegrams_limit(context):
    state(context, PAGE_ONE)
    markup = fh._keyboard(context, PAGE_ONE, offset=0, page_size=2, total=3)

    for row in markup.inline_keyboard:
        for button in row:
            assert len(button.callback_data.encode()) <= 64
