"""Append-only record of every simulated alpha.

SQLite rather than CSV: simulation workers write concurrently, alpha expressions
contain commas and quotes, and "which alphas passed with sharpe > 1.5" should be
a query rather than a pandas load. ``export_csv`` covers the times a spreadsheet
is what you want.

Phase 3 adds querying on top of this; for now the job is simply that no result
is ever lost.
"""

import csv
import json
import logging
import sqlite3
from pathlib import Path
from typing import Optional

from bot.formatting import utc_now
from bot.simulation import SimOutcome

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS alphas (
    alpha_id        TEXT PRIMARY KEY,
    created_at      TEXT NOT NULL,
    expression      TEXT NOT NULL,
    region          TEXT,
    universe        TEXT,
    delay           INTEGER,
    decay           INTEGER,
    neutralization  TEXT,
    truncation      REAL,
    test_period     TEXT,
    sharpe          REAL,
    fitness         REAL,
    turnover        REAL,
    drawdown        REAL,
    margin          REAL,
    returns         REAL,
    pnl             REAL,
    long_count      INTEGER,
    short_count     INTEGER,
    tests_passed    INTEGER,
    tests_failed    INTEGER,
    all_passed      INTEGER,
    tests_json      TEXT
);
CREATE INDEX IF NOT EXISTS idx_alphas_sharpe ON alphas(sharpe);
CREATE INDEX IF NOT EXISTS idx_alphas_all_passed ON alphas(all_passed);
CREATE INDEX IF NOT EXISTS idx_alphas_created ON alphas(created_at);

CREATE TABLE IF NOT EXISTS sim_queue (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id        TEXT NOT NULL,
    chat_id         INTEGER NOT NULL,
    expression      TEXT NOT NULL,
    region          TEXT,
    universe        TEXT,
    delay           INTEGER,
    decay           INTEGER,
    neutralization  TEXT,
    truncation      REAL,
    test_period     TEXT,
    state           TEXT NOT NULL,
    queued_at       TEXT NOT NULL,
    started_at      TEXT,
    finished_at     TEXT,
    alpha_id        TEXT,
    error           TEXT
);
CREATE INDEX IF NOT EXISTS idx_queue_state ON sim_queue(state);
CREATE INDEX IF NOT EXISTS idx_queue_batch ON sim_queue(batch_id);
"""

# Every column that defines "the same experiment" -- used for duplicate detection.
SPEC_COLUMNS = [
    "expression", "region", "universe", "delay", "decay",
    "neutralization", "truncation", "test_period",
]

COLUMNS = [
    "alpha_id", "created_at", "expression", "region", "universe", "delay",
    "decay", "neutralization", "truncation", "test_period", "sharpe", "fitness",
    "turnover", "drawdown", "margin", "returns", "pnl", "long_count",
    "short_count", "tests_passed", "tests_failed", "all_passed", "tests_json",
]


class AlphaStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def record(self, outcome: SimOutcome) -> bool:
        """Store a completed simulation. Re-simulating an alpha updates its row.

        Never raises: a storage problem must not swallow a result the user is
        waiting on, so failures are logged and reported by return value.
        """
        if not outcome.ok or not outcome.alpha_id:
            return False

        spec = outcome.spec
        metrics = outcome.metrics
        row = {
            "alpha_id": outcome.alpha_id,
            "created_at": utc_now().isoformat(timespec="seconds"),
            "expression": spec.expression,
            "region": spec.region,
            "universe": spec.universe,
            "delay": spec.delay,
            "decay": spec.decay,
            "neutralization": spec.neutralization,
            "truncation": spec.truncation,
            "test_period": spec.test_period,
            "sharpe": metrics.get("sharpe"),
            "fitness": metrics.get("fitness"),
            "turnover": metrics.get("turnover"),
            "drawdown": metrics.get("drawdown"),
            "margin": metrics.get("margin"),
            "returns": metrics.get("returns"),
            "pnl": metrics.get("pnl"),
            "long_count": metrics.get("long_count"),
            "short_count": metrics.get("short_count"),
            "tests_passed": len(outcome.passed_tests),
            "tests_failed": len(outcome.failed_tests),
            "all_passed": int(outcome.all_passed),
            "tests_json": json.dumps(outcome.tests),
        }

        placeholders = ", ".join(f":{c}" for c in COLUMNS)
        try:
            with self._connect() as conn:
                conn.execute(
                    f"INSERT OR REPLACE INTO alphas ({', '.join(COLUMNS)}) "
                    f"VALUES ({placeholders})",
                    row,
                )
            return True
        except sqlite3.Error:
            log.exception("Could not record alpha %s", outcome.alpha_id)
            return False

    def count(self) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM alphas").fetchone()[0]

    def has_simulated(self, spec) -> bool:
        """Has this exact expression already run with these exact settings?

        Truncation is a REAL, so it is compared with a tolerance rather than for
        equality -- 0.08 does not necessarily round-trip through SQLite to the
        same bits it went in as.
        """
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT truncation FROM alphas
                WHERE expression = ? AND region = ? AND universe = ?
                  AND delay = ? AND decay = ? AND neutralization = ?
                  AND test_period = ? AND ABS(truncation - ?) < 1e-9
                LIMIT 1
                """,
                (
                    spec.expression, spec.region, spec.universe, spec.delay,
                    spec.decay, spec.neutralization, spec.test_period,
                    spec.truncation,
                ),
            ).fetchone()
        return row is not None

    def recent(self, limit: int = 10) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM alphas ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()

    def export_csv(self, destination: Path) -> Optional[Path]:
        """Dump the table to CSV. Returns None when there is nothing to export."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM alphas ORDER BY created_at DESC"
            ).fetchall()
        if not rows:
            return None

        destination.parent.mkdir(parents=True, exist_ok=True)
        with open(destination, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(rows[0].keys())
            writer.writerows(tuple(row) for row in rows)
        return destination
