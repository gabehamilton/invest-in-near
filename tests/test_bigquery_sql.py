"""Static checks on sql/*.sql: header block, partition filters on block_date, no SELECT *, contract columns."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline import config  # noqa: E402
from pipeline.sources import bigquery  # noqa: E402

SQL_FILES = sorted(config.SQL_DIR.glob("*.sql"))
ALL_QUERIES = (bigquery.FRESHNESS, *bigquery.QUERIES)
REQUIRED_HEADER = ("name", "description", "grain", "params", "est")
TABLE_REF = re.compile(r"`bigquery-public-data\.crypto_near_mainnet_us\.(\w+)`")
PARTITION_FILTER = re.compile(
    r"WHERE\s+block_date\s+BETWEEN\s+(?:@start_date|DATE_SUB\(@start_date,\s*INTERVAL\s+(\d+)\s+DAY\))"
    r"\s+AND\s+@end_date\b")


def body(name: str) -> str:
    text = bigquery.load_sql(name).text
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("--"))


def ids(paths: list[Path]) -> list[str]:
    return [path.stem for path in paths]


def test_one_sql_file_per_query():
    assert {path.stem for path in SQL_FILES} == {query.name for query in ALL_QUERIES}


@pytest.mark.parametrize("path", SQL_FILES, ids=ids(SQL_FILES))
def test_header_block(path):
    sql = bigquery.load_sql(path.stem)
    assert set(REQUIRED_HEADER) <= set(sql.header)
    assert all(sql.header[key] for key in REQUIRED_HEADER)
    assert sql.header["name"] == path.stem
    assert "@start_date DATE" in sql.header["params"] and "@end_date DATE" in sql.header["params"]
    assert "block_date" in sql.header["params"]
    assert 0 < sql.gb_per_day < 5


@pytest.mark.parametrize("path", SQL_FILES, ids=ids(SQL_FILES))
def test_every_table_read_is_partition_filtered_by_the_parameters(path):
    text = body(path.stem)
    refs = list(TABLE_REF.finditer(text))
    assert refs, "query must read the NEAR dataset"
    for ref in refs:
        assert PARTITION_FILTER.match(text[ref.end():].lstrip()), f"{ref.group(1)} lacks a block_date filter"
    assert text.count("@start_date") >= len(refs) and text.count("@end_date") >= len(refs)


@pytest.mark.parametrize("query", ALL_QUERIES, ids=[q.name for q in ALL_QUERIES])
def test_lookback_in_sql_matches_runner(query):
    pattern = r"DATE_SUB\(@start_date,\s*INTERVAL\s+(\d+)\s+DAY\)"
    lookbacks = {int(days) for days in re.findall(pattern, body(query.name))}
    assert lookbacks == ({query.lookback_days} if query.lookback_days else set())


def test_partition_filter_rejects_an_unbounded_or_unparameterised_range():
    for where in ("WHERE block_date >= @start_date", "WHERE block_date BETWEEN '2026-01-01' AND @end_date",
                  "WHERE block_date BETWEEN @start_date AND CURRENT_DATE()",
                  "WHERE block_date BETWEEN @start_date AND DATE_ADD(CURRENT_DATE(), INTERVAL 1 DAY)",
                  "WHERE block_date BETWEEN @start_date AND DATE_ADD(@end_date, INTERVAL 1 DAY)"):
        assert not PARTITION_FILTER.match(where), where
    assert PARTITION_FILTER.match("WHERE block_date BETWEEN DATE_SUB(@start_date, INTERVAL 1 DAY) AND @end_date")


@pytest.mark.parametrize("path", SQL_FILES, ids=ids(SQL_FILES))
def test_no_select_star(path):
    text = re.sub(r"COUNT\(\*\)", "", body(path.stem), flags=re.IGNORECASE)
    assert "*" not in text


@pytest.mark.parametrize("path", SQL_FILES, ids=ids(SQL_FILES))
def test_sql_is_one_balanced_statement(path):
    text = body(path.stem)
    assert text.count("(") == text.count(")")
    assert text.count("[") == text.count("]")
    assert text.count("`") % 2 == 0 and text.count("'") % 2 == 0
    assert ";" not in text
    assert re.match(r"\s*(WITH|SELECT)\b", text)


@pytest.mark.parametrize("query", ALL_QUERIES, ids=[q.name for q in ALL_QUERIES])
def test_contract_columns_are_selected(query):
    text = body(query.name)
    for column in query.columns:
        aliased = re.search(rf"\bAS\s+{column}\b", text)
        bare = re.search(rf"^\s*(?:\w+\.)?{column},?\s*$", text, re.MULTILINE)
        assert aliased or bare, f"{query.name} does not select {column}"


def test_contract_column_lists_match_the_raw_data_contract():
    columns = {query.output: query.columns for query in bigquery.QUERIES}
    assert columns == {
        "daily_supply_flows": ("block_date", "eod_supply_near", "minted_near", "burnt_near", "epoch_boundaries"),
        "fee_burn_sampled": ("block_date", "gross_fee_burn_near", "gas_burnt_tgas"),
    }


def test_supply_flows_lag_by_height_and_drop_lookback_day():
    text = body("daily_supply_flows")
    assert "WINDOW chain AS (ORDER BY block_height)" in text
    assert "LAG(total_supply) OVER chain" in text
    assert re.search(r"FROM deltas\s+WHERE block_date BETWEEN @start_date AND @end_date", text)


def test_fee_burn_adds_receipt_and_conversion_burn():
    text = body("fee_burn")
    assert set(TABLE_REF.findall(text)) == {"execution_outcomes", "transactions"}
    for column in ("tokens_burnt", "gas_burnt", "receipt_conversion_tokens_burnt", "receipt_conversion_gas_burnt"):
        assert re.search(rf"\b{column}\b", text)
