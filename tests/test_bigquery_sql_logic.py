"""Run the real sql/*.sql text on tiny synthetic tables in stdlib SQLite, to check the logic and not just the shape.

BigQuery-only syntax is rewritten by REWRITES (each one a narrow, exact pattern) and BigQuery aggregates are
emulated by Python functions with BigQuery's NULL semantics. Anything else that SQLite cannot parse fails the
test, so a new BigQuery construct in the SQL has to be added here before its logic is trusted.
"""

from __future__ import annotations

import re
import sqlite3
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline.sources import bigquery  # noqa: E402

YOCTO = 10 ** 24
SUPPLY = 1_000_000_000 * YOCTO
REWRITES = (
    (r"`bigquery-public-data\.crypto_near_mainnet_us\.(\w+)`", r"\1"),
    (r"DATE_SUB\(@start_date, INTERVAL (\d+) DAY\)", r"date(@start_date, '-\1 day')"),
    (r"DATE_DIFF\(([^,()]+(?:\([^()]*\))?), ([^,()]+(?:\([^()]*\))?), DAY\)",
     r"CAST(julianday(\1) - julianday(\2) AS INTEGER)"),
    (r"DATE '(\d{4}-\d{2}-\d{2})'", r"'\1'"),
    (r"ARRAY_AGG\((\w+) IGNORE NULLS ORDER BY (\w+) DESC LIMIT 1\)\[SAFE_OFFSET\(0\)\]", r"LAST_BY(\1, \2)"),
    (r"\bAS FLOAT64\b", "AS REAL"),
    (r"\bIF\(", "iif("),
)


class CountIf:
    def __init__(self) -> None:
        self.count = 0

    def step(self, value) -> None:
        self.count += bool(value)

    def finalize(self) -> int:
        return self.count


class LogicalAnd:
    def __init__(self) -> None:
        self.result: bool | None = None

    def step(self, value) -> None:
        if value is not None:
            self.result = bool(value) if self.result is None else self.result and bool(value)

    def finalize(self) -> bool | None:
        return self.result


class LastBy:
    """ARRAY_AGG(value IGNORE NULLS ORDER BY key DESC LIMIT 1)[SAFE_OFFSET(0)]."""

    def __init__(self) -> None:
        self.best: tuple | None = None

    def step(self, value, key) -> None:
        if value is not None and (self.best is None or key > self.best[0]):
            self.best = (key, value)

    def finalize(self):
        return None if self.best is None else self.best[1]


def to_sqlite(sql: str) -> str:
    for pattern, replacement in REWRITES:
        sql = re.sub(pattern, replacement, sql)
    return sql


def connect(tables: dict[str, tuple[tuple[str, ...], list[tuple]]]) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.create_aggregate("COUNTIF", 1, CountIf)
    conn.create_aggregate("LOGICAL_AND", 1, LogicalAnd)
    conn.create_aggregate("LAST_BY", 2, LastBy)
    for table, (columns, rows) in tables.items():
        conn.execute(f"CREATE TABLE {table} ({', '.join(columns)})")
        conn.executemany(f"INSERT INTO {table} VALUES ({', '.join('?' * len(columns))})", reversed(rows))
    return conn


def run_sql(name: str, conn: sqlite3.Connection, start: str, end: str) -> list[dict[str, str]]:
    cursor = conn.execute(to_sqlite(bigquery.load_sql(name).text), {"start_date": start, "end_date": end})
    columns = [column[0] for column in cursor.description]
    query = next(query for query in (bigquery.FRESHNESS, *bigquery.QUERIES) if query.name == name)
    return [bigquery.format_row(dict(zip(columns, row)), query) for row in cursor.fetchall()]


def near(value: float) -> str:
    return str(int(round(value * YOCTO)))


def blocks_table() -> dict[str, tuple[tuple[str, ...], list[tuple]]]:
    """Supply per block in yoctoNEAR (stored as text, as the CAST expects). 2021-01-04 has no blocks."""
    supply = [
        ("2020-07-21", 9_820_210, "2020-07-21T23:00:00", 0),
        ("2020-07-21", 9_820_211, "2020-07-21T23:10:00", -0.3),
        ("2021-01-01", 30_000_100, "2021-01-01T10:00:00", 0),
        ("2021-01-01", 30_000_101, "2021-01-01T20:00:00", -1),
        ("2021-01-02", 30_000_102, "2021-01-02T01:00:00", -0.5),
        ("2021-01-02", 30_000_103, "2021-01-02T07:00:00", 25_000 - 0.25),
        ("2021-01-02", 30_000_104, "2021-01-02T23:59:00", -0.75),
        ("2021-01-03", 30_000_106, "2021-01-03T04:00:00", -0.1),
        ("2021-01-03", 30_000_107, "2021-01-03T23:59:30", 2),
        ("2021-01-05", 30_000_110, "2021-01-05T02:00:00", 5),
        ("2021-01-05", 30_000_111, "2021-01-05T23:59:59", -0.2),
    ]
    rows, total = [], 0.0
    for block_date, height, timestamp, delta in supply:
        total = 0.0 if height in (9_820_210, 30_000_100) else total + delta
        rows.append((block_date, height, f"{timestamp}+00:00", str(SUPPLY + int(round(total * YOCTO)))))
    return {"blocks": (("block_date", "block_height", "block_timestamp_utc", "total_supply"), rows)}


def assert_near(row: dict[str, str], expected: dict[str, float | str]) -> None:
    for column, value in expected.items():
        if isinstance(value, str):
            assert row[column] == value, column
        else:
            assert float(row[column]) == pytest.approx(value, abs=1e-6), column


def test_supply_flows_sum_signed_deltas_and_blank_days_after_a_gap():
    rows = run_sql("daily_supply_flows", connect(blocks_table()), "2021-01-02", "2021-01-05")
    assert [row["block_date"] for row in rows] == ["2021-01-02", "2021-01-03", "2021-01-05"]
    assert_near(rows[0], {"minted_near": 24_999.75, "burnt_near": 1.25, "epoch_boundaries": "1",
                          "eod_supply_near": 1e9 - 1 - 0.5 + 24_999.75 - 0.75})
    assert_near(rows[1], {"minted_near": 2, "burnt_near": 0.1, "epoch_boundaries": "0"})
    assert_near(rows[2], {"minted_near": "", "burnt_near": "", "epoch_boundaries": ""})
    assert float(rows[2]["eod_supply_near"]) == pytest.approx(float(rows[1]["eod_supply_near"]) + 5 - 0.2, abs=1e-6)


def test_supply_flows_blank_a_first_day_whose_previous_day_is_missing():
    rows = run_sql("daily_supply_flows", connect(blocks_table()), "2021-01-05", "2021-01-05")
    assert [(row["block_date"], row["minted_near"], row["burnt_near"]) for row in rows] == [("2021-01-05", "", "")]
    assert rows[0]["eod_supply_near"]


def test_supply_flows_genesis_day_has_no_predecessor_but_is_complete():
    rows = run_sql("daily_supply_flows", connect(blocks_table()), "2020-07-21", "2020-07-21")
    assert len(rows) == 1
    assert_near(rows[0], {"minted_near": 0, "burnt_near": 0.3, "epoch_boundaries": "0", "eod_supply_near": 1e9 - 0.3})


def test_freshness_incremental_merge_equals_a_full_scan():
    conn = connect(blocks_table())
    full = run_sql("freshness", conn, "2020-07-21", "2021-01-10")[0]
    assert [full[key] for key in ("min_block_date", "max_block_date", "max_block_height")] == [
        "2020-07-21", "2021-01-05", "30000111"]
    assert full["days_present"] == "5" and full["days_spanned"] == str((date(2021, 1, 5) - date(2020, 7, 21)).days + 1)
    base = run_sql("freshness", conn, "2020-07-21", "2021-01-02")[0]
    job = bigquery.freshness_job(bigquery.Settings(1.0, 1, None, date(2020, 7, 21)), date(2021, 1, 10), base)
    scanned = run_sql("freshness", conn, job.start.isoformat(), job.end.isoformat())[0]
    assert job.start == date(2021, 1, 2) and scanned["days_present"] == "3"
    assert bigquery.merge_freshness(base, scanned, job) == full
