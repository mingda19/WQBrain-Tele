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
"""

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
