"""Tests for spec building, result extraction, storage and formatting.

The DataFrame fixtures reproduce the exact shapes saved in
ACE_API/how_to_use.ipynb (cells 54 and 56), so extraction is tested against what
BRAIN really returns rather than an idealised guess.

Run: python -m pytest tests/ -q
"""

import asyncio

import pandas as pd
import pytest

from bot import simulation
from bot.alpha_spec import (
    AlphaSpec,
    SettingsCatalog,
    parse_decay,
    parse_truncation,
)
from bot.results import format_metrics_block, format_outcome, format_tests_block
from bot.simulation import SimOutcome, SimulationRunner, run_simulation
from bot.store import AlphaStore

EXPRESSION = "ts_sum(vec_avg(nws18_qmb),120)"


@pytest.fixture
def is_stats():
    """Cell 54 of the notebook, verbatim."""
    return pd.DataFrame(
        [
            {
                "pnl": 2593654,
                "bookSize": 20000000,
                "longCount": 1567,
                "shortCount": 1502,
                "turnover": 0.0506,
                "returns": 0.026,
                "drawdown": 0.0661,
                "margin": 0.001027,
                "sharpe": 0.66,
                "fitness": 0.3,
                "startDate": "2014-01-01",
                "alpha_id": "0mkRPZjk",
            }
        ]
    )


@pytest.fixture
def is_tests():
    """Cell 56 of the notebook, including PENDING and WARNING rows."""
    return pd.DataFrame(
        [
            {"name": "LOW_SHARPE", "result": "FAIL", "limit": 1.58, "value": 0.66},
            {"name": "LOW_FITNESS", "result": "FAIL", "limit": 1.00, "value": 0.30},
            {"name": "LOW_TURNOVER", "result": "PASS", "limit": 0.01, "value": 0.0506},
            {"name": "HIGH_TURNOVER", "result": "PASS", "limit": 0.70, "value": 0.0506},
            {
                "name": "CONCENTRATED_WEIGHT",
                "result": "PASS",
                "limit": float("nan"),
                "value": float("nan"),
            },
            {
                "name": "SELF_CORRELATION",
                "result": "PENDING",
                "limit": float("nan"),
                "value": float("nan"),
            },
            {
                "name": "MATCHES_THEMES",
                "result": "WARNING",
                "limit": float("nan"),
                "value": float("nan"),
            },
        ]
    )


# ----------------------------------------------------------------- alpha spec


def test_spec_builds_a_payload_via_ace():
    spec = AlphaSpec(expression=EXPRESSION, region="USA", universe="TOP3000", decay=6)
    payload = spec.to_simulate_data()

    assert payload["type"] == "REGULAR"
    assert payload["regular"] == EXPRESSION
    settings = payload["settings"]
    assert settings["region"] == "USA"
    assert settings["universe"] == "TOP3000"
    assert settings["decay"] == 6
    assert settings["language"] == "FASTEXPR"
    assert settings["instrumentType"] == "EQUITY"


def test_settings_line_is_compact():
    line = AlphaSpec(expression=EXPRESSION, truncation=0.08).settings_line()
    assert "USA" in line and "TOP3000" in line and "D1" in line
    assert "trunc 0.08" in line


@pytest.mark.parametrize(
    "text,expected", [("0", 0), ("6", 6), ("512", 512), ("  12  ", 12)]
)
def test_parse_decay_accepts_valid(text, expected):
    assert parse_decay(text) == expected


@pytest.mark.parametrize("text", ["-1", "513", "abc", "1.5", ""])
def test_parse_decay_rejects_invalid(text):
    with pytest.raises(ValueError):
        parse_decay(text)


@pytest.mark.parametrize("text", ["-0.1", "1.1", "abc", ""])
def test_parse_truncation_rejects_invalid(text):
    with pytest.raises(ValueError):
        parse_truncation(text)


def test_parse_truncation_accepts_valid():
    assert parse_truncation("0.08") == pytest.approx(0.08)


# -------------------------------------------------------------------- catalog


@pytest.fixture
def catalog():
    cat = SettingsCatalog()
    cat._rows = [
        {
            "InstrumentType": "EQUITY",
            "Region": "USA",
            "Delay": 1,
            "Universe": ["TOP3000", "TOP1000"],
            "Neutralization": ["INDUSTRY", "MARKET", "NONE"],
        },
        {
            "InstrumentType": "EQUITY",
            "Region": "CHN",
            "Delay": 1,
            "Universe": ["TOP2000U"],
            "Neutralization": ["MARKET", "NONE"],
        },
    ]
    return cat


def test_catalog_lists_valid_choices(catalog):
    assert catalog.regions() == ["CHN", "USA"]
    assert catalog.universes("CHN", 1) == ["TOP2000U"]
    assert catalog.neutralizations("USA", 1) == ["INDUSTRY", "MARKET", "NONE"]


def test_reconcile_snaps_stranded_settings(catalog):
    """Switching region can strand universe/neutralization on invalid values."""
    spec = AlphaSpec(
        expression=EXPRESSION,
        region="CHN",
        universe="TOP3000",  # USA-only
        neutralization="INDUSTRY",  # not offered for CHN
    )
    fixed = catalog.reconcile(spec)

    assert fixed.universe == "TOP2000U"
    assert fixed.neutralization == "MARKET"
    assert fixed.expression == EXPRESSION


def test_reconcile_leaves_valid_settings_alone(catalog):
    spec = AlphaSpec(expression=EXPRESSION, region="USA", universe="TOP1000")
    assert catalog.reconcile(spec).universe == "TOP1000"


def test_catalog_falls_back_when_unloaded():
    cat = SettingsCatalog()
    assert "USA" in cat.regions()
    spec = AlphaSpec(expression=EXPRESSION, universe="ANYTHING")
    assert cat.reconcile(spec).universe == "ANYTHING", "no catalog, no snapping"


# ---------------------------------------------------------------- extraction


def test_metrics_extracted_from_real_shape(is_stats):
    metrics = simulation._extract_metrics(is_stats)

    assert metrics["sharpe"] == pytest.approx(0.66)
    assert metrics["fitness"] == pytest.approx(0.30)
    assert metrics["turnover"] == pytest.approx(0.0506)
    assert metrics["drawdown"] == pytest.approx(0.0661)
    assert metrics["margin"] == pytest.approx(0.001027)
    assert metrics["long_count"] == 1567
    assert metrics["short_count"] == 1502


def test_tests_extracted_with_nan_cleaned(is_tests):
    tests = simulation._extract_tests(is_tests)

    assert len(tests) == 7
    low_sharpe = next(t for t in tests if t["name"] == "LOW_SHARPE")
    assert low_sharpe["result"] == "FAIL"
    assert low_sharpe["value"] == pytest.approx(0.66)

    concentrated = next(t for t in tests if t["name"] == "CONCENTRATED_WEIGHT")
    assert concentrated["limit"] is None, "NaN must become None, not float('nan')"
    assert concentrated["value"] is None


def test_extraction_tolerates_empty_frames():
    assert simulation._extract_metrics(pd.DataFrame()) == {}
    assert simulation._extract_tests(pd.DataFrame()) == []
    assert simulation._extract_metrics(None) == {}
    assert simulation._extract_tests(None) == []


def test_all_passed_requires_tests_and_no_failures(is_tests):
    outcome = SimOutcome(spec=AlphaSpec(expression=EXPRESSION), ok=True)
    outcome.tests = simulation._extract_tests(is_tests)
    assert outcome.all_passed is False
    assert len(outcome.failed_tests) == 2

    outcome.tests = [{"name": "LOW_SHARPE", "result": "PASS"}]
    assert outcome.all_passed is True

    outcome.tests = []
    assert outcome.all_passed is False, "no checks is not the same as passing"


# ------------------------------------------------------------- run_simulation


class FakeBrain:
    """Routes run_ace calls to scripted returns, like BrainSession would."""

    def __init__(self, submit=None, stats=None, submit_error=None, stats_error=None):
        self._submit = submit
        self._stats = stats
        self._submit_error = submit_error
        self._stats_error = stats_error
        self.calls = []

    async def run_ace(self, func, *args, **kwargs):
        self.calls.append(func.__name__)
        if func.__name__ == "simulate_single_alpha":
            if self._submit_error:
                raise self._submit_error
            return self._submit
        if self._stats_error:
            raise self._stats_error
        return self._stats


def test_run_simulation_success(is_stats, is_tests):
    spec = AlphaSpec(expression=EXPRESSION)
    brain = FakeBrain(
        submit={"alpha_id": "0mkRPZjk", "simulate_data": spec.to_simulate_data()},
        stats={"is_stats": is_stats, "is_tests": is_tests},
    )

    outcome = asyncio.run(run_simulation(brain, spec))

    assert outcome.ok and outcome.alpha_id == "0mkRPZjk"
    assert outcome.metrics["sharpe"] == pytest.approx(0.66)
    assert len(outcome.failed_tests) == 2
    assert brain.calls == ["simulate_single_alpha", "get_specified_alpha_stats"]


def test_run_simulation_reports_a_rejected_alpha():
    """ACE returns alpha_id=None for a bad expression rather than raising."""
    spec = AlphaSpec(expression="this is not fastexpr")
    brain = FakeBrain(submit={"alpha_id": None, "simulate_data": {}})

    outcome = asyncio.run(run_simulation(brain, spec))

    assert not outcome.ok
    assert outcome.alpha_id is None
    assert "syntax error" in outcome.error
    assert brain.calls == ["simulate_single_alpha"], "must not fetch stats"


def test_run_simulation_keeps_alpha_id_when_stats_fail():
    """The simulation succeeded; losing the id because stats broke would be worse."""
    spec = AlphaSpec(expression=EXPRESSION)
    brain = FakeBrain(
        submit={"alpha_id": "0mkRPZjk", "simulate_data": {}},
        stats_error=RuntimeError("boom"),
    )

    outcome = asyncio.run(run_simulation(brain, spec))

    assert outcome.ok and outcome.alpha_id == "0mkRPZjk"
    assert "stats could not be read" in outcome.error


def test_run_simulation_survives_a_submission_exception():
    spec = AlphaSpec(expression=EXPRESSION)
    brain = FakeBrain(submit_error=RuntimeError("network down"))

    outcome = asyncio.run(run_simulation(brain, spec))

    assert not outcome.ok
    assert "network down" in outcome.error


def test_runner_caps_concurrency(monkeypatch):
    """BRAIN rejects excess concurrent simulations rather than queueing them."""
    spec = AlphaSpec(expression=EXPRESSION)
    runner = SimulationRunner(brain=None, max_concurrent=2)
    peak = 0
    started = 0

    async def fake_run(_brain, _spec):
        nonlocal peak, started
        started += 1
        peak = max(peak, runner.in_flight)
        await asyncio.sleep(0.01)
        return SimOutcome(spec=_spec, ok=True, alpha_id="x")

    monkeypatch.setattr(simulation, "run_simulation", fake_run)

    async def scenario():
        return await asyncio.gather(*(runner.run(spec) for _ in range(6)))

    outcomes = asyncio.run(scenario())

    assert started == 6, "every simulation must eventually run"
    assert len(outcomes) == 6
    assert peak == 2, f"concurrency ceiling not enforced (peaked at {peak})"
    assert runner.in_flight == 0, "counter must unwind"


# ---------------------------------------------------------------------- store


@pytest.fixture
def store(tmp_path):
    return AlphaStore(tmp_path / "alphas.db")


def make_outcome(alpha_id="0mkRPZjk", sharpe=0.66, tests=None):
    return SimOutcome(
        spec=AlphaSpec(expression=EXPRESSION, decay=6),
        ok=True,
        alpha_id=alpha_id,
        metrics={"sharpe": sharpe, "fitness": 0.3, "turnover": 0.0506,
                 "drawdown": 0.0661, "margin": 0.001027, "returns": 0.026,
                 "pnl": 2593654, "long_count": 1567, "short_count": 1502},
        tests=tests if tests is not None else [
            {"name": "LOW_SHARPE", "result": "FAIL", "limit": 1.58, "value": 0.66},
            {"name": "LOW_TURNOVER", "result": "PASS", "limit": 0.01, "value": 0.0506},
        ],
    )


def test_store_records_metrics_and_settings(store):
    assert store.record(make_outcome()) is True

    row = store.recent(1)[0]
    assert row["alpha_id"] == "0mkRPZjk"
    assert row["expression"] == EXPRESSION
    assert row["decay"] == 6
    assert row["sharpe"] == pytest.approx(0.66)
    assert row["margin"] == pytest.approx(0.001027)
    assert row["tests_failed"] == 1
    assert row["tests_passed"] == 1
    assert row["all_passed"] == 0


def test_store_marks_a_clean_alpha_as_passed(store):
    store.record(
        make_outcome(tests=[{"name": "LOW_SHARPE", "result": "PASS", "limit": 1, "value": 2}])
    )
    assert store.recent(1)[0]["all_passed"] == 1


def test_store_ignores_failed_simulations(store):
    assert store.record(SimOutcome(spec=AlphaSpec(), ok=False, error="nope")) is False
    assert store.count() == 0


def test_resimulating_updates_rather_than_duplicates(store):
    store.record(make_outcome(sharpe=0.66))
    store.record(make_outcome(sharpe=1.90))

    assert store.count() == 1
    assert store.recent(1)[0]["sharpe"] == pytest.approx(1.90)


def test_expressions_with_commas_survive_a_round_trip(store):
    """The reason this is SQLite and not a CSV append."""
    tricky = 'ts_corr(close, open, 20) + "quoted", ts_sum(x,5)'
    outcome = make_outcome()
    outcome.spec.expression = tricky
    store.record(outcome)

    assert store.recent(1)[0]["expression"] == tricky


def test_csv_export_round_trips(store, tmp_path):
    store.record(make_outcome())
    destination = store.export_csv(tmp_path / "out.csv")

    assert destination is not None
    frame = pd.read_csv(destination)
    assert len(frame) == 1
    assert frame.iloc[0]["alpha_id"] == "0mkRPZjk"
    assert frame.iloc[0]["sharpe"] == pytest.approx(0.66)


def test_csv_export_of_an_empty_store_returns_none(store, tmp_path):
    assert store.export_csv(tmp_path / "out.csv") is None


# ----------------------------------------------------------------- formatting


def test_metrics_block_renders_percentages_and_bps(is_stats):
    text = format_metrics_block(simulation._extract_metrics(is_stats))

    assert "Sharpe" in text and "0.66" in text
    assert "5.06%" in text, "turnover as a percentage"
    assert "10.27 bps" in text, "margin as basis points, like the platform"
    assert "1,567 / 1,502" in text


def test_tests_block_puts_failures_first(is_tests):
    text = format_tests_block(simulation._extract_tests(is_tests))
    lines = text.splitlines()

    assert lines[0].startswith("FAIL")
    assert lines[1].startswith("FAIL")
    assert "LOW_SHARPE" in lines[0] or "LOW_FITNESS" in lines[0]
    assert "(0.66 vs 1.58)" in text


def test_outcome_message_contains_id_expression_and_link(is_stats, is_tests):
    outcome = SimOutcome(
        spec=AlphaSpec(expression=EXPRESSION),
        ok=True,
        alpha_id="0mkRPZjk",
        metrics=simulation._extract_metrics(is_stats),
        tests=simulation._extract_tests(is_tests),
    )
    text = format_outcome(outcome)

    assert "0mkRPZjk" in text
    assert "platform.worldquantbrain.com/alpha/0mkRPZjk" in text
    assert "2 checks failed" in text
    assert "&lt;" not in EXPRESSION  # sanity: nothing to escape here
    assert len(text) < 4096, "must fit in one Telegram message"


def test_outcome_message_escapes_html_in_expressions():
    outcome = SimOutcome(
        spec=AlphaSpec(expression="if_else(a < b, 1, 2) & c"),
        ok=True,
        alpha_id="X",
        metrics={"sharpe": 1.0},
        tests=[{"name": "T", "result": "PASS", "limit": None, "value": None}],
    )
    text = format_outcome(outcome)

    assert "&lt;" in text and "&amp;" in text
    assert "<pre>" in text, "the pre tags themselves must survive"


def test_failed_outcome_explains_itself():
    outcome = SimOutcome(
        spec=AlphaSpec(expression=EXPRESSION), ok=False, error="Could not submit: 500"
    )
    text = format_outcome(outcome)

    assert "Simulation failed" in text
    assert "Could not submit: 500" in text
