"""Offline tests for pipeline.sources.bigquery: schedules, month chunks, cache reuse, budget gate, and a full
run against a fake client that records every query (no credentials, no network)."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline import config  # noqa: E402
from pipeline.sources import bigquery  # noqa: E402

GB = bigquery.BYTES_PER_GB
TODAY = date(2026, 11, 10)


@dataclass
class Call:
    name: str
    start: date
    end: date
    dry_run: bool
    max_bytes: int | None
    location: str | None


class FakeJob:
    def __init__(self, rows: list[dict], size: int, job_id: str | None):
        self._rows = rows
        self.total_bytes_processed = size
        self.total_bytes_billed = size
        self.job_id = job_id
        self.cache_hit = False

    def result(self) -> list[dict]:
        return list(self._rows)


class FakeClient:
    def __init__(self, freshness: dict, bytes_per_day: int = GB // 10, fail_on: tuple[str, str] | None = None,
                 error: Exception | None = None):
        self.freshness = freshness
        self.bytes_per_day = bytes_per_day
        self.fail_on = fail_on
        self.error = error
        self.calls: list[Call] = []

    def query(self, sql: str, job_config, location=None) -> FakeJob:
        name = bigquery.parse_header(sql)["name"]
        params = {param.name: param.value for param in job_config.query_parameters}
        start, end = params["start_date"], params["end_date"]
        self.calls.append(Call(name, start, end, job_config.dry_run, job_config.maximum_bytes_billed, location))
        if self.error is not None:
            raise self.error
        size = 10_000_000 if name == "freshness" else ((end - start).days + 1) * self.bytes_per_day
        if job_config.dry_run:
            return FakeJob([], size, None)
        if self.fail_on == (name, bigquery.month_key(start)):
            raise RuntimeError("503 backend error")
        return FakeJob(self.rows(name, start, end), size, f"job_{len(self.calls)}")

    def rows(self, name: str, start: date, end: date) -> list[dict]:
        days = bigquery.days_between(start, end)
        if name == "freshness":
            first, last = max(self.freshness["min_block_date"], start), self.freshness["max_block_date"]
            if first > last:
                return [dict.fromkeys(bigquery.FRESHNESS.columns)]
            span = (last - first).days + 1
            return [{**self.freshness, "min_block_date": first, "days_present": span, "days_spanned": span}]
        if name == "daily_supply_flows":
            return [{"block_date": d, "eod_supply_near": 1.0e9 + d.day, "minted_near": 25760.09,
                     "burnt_near": 1e-05, "epoch_boundaries": None if d.day == 2 else 3} for d in reversed(days)]
        if name == "fee_burn":
            return [{"block_date": d, "gross_fee_burn_near": 12.5, "gas_burnt_tgas": 1.0e6} for d in days]
        raise AssertionError(name)

    def real(self, name: str | None = None) -> list[Call]:
        return [call for call in self.calls if not call.dry_run and (name is None or call.name == name)]


def freshness_row(max_date: str, last_block: str) -> dict:
    return {"min_block_date": date(2020, 7, 21), "max_block_date": date.fromisoformat(max_date), "days_present": 1,
            "days_spanned": 1, "max_block_height": 123,
            "max_block_timestamp_utc": datetime.fromisoformat(last_block)}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("GCP_PROJECT", "BQ_BUDGET_GB", "BQ_MAX_BYTES_BILLED", "BQ_LOCATION", "BQ_START_DATE",
                 *(f"BQ_SCHEDULE_{query.name.upper()}" for query in bigquery.QUERIES)):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def raw_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "RAW_DIR", tmp_path / "data" / "raw")
    return tmp_path / "data" / "raw" / "bigquery"


def settings(**overrides) -> bigquery.Settings:
    values = {"budget_gb": 500.0, "max_bytes": 50 * GB, "location": None, "start": date(2020, 8, 25)}
    return bigquery.Settings(**{**values, **overrides})


def read_rows(path: Path) -> list[dict]:
    import csv

    with path.open(newline="") as fh:
        return list(csv.DictReader(fh))


def test_parse_schedule_accepts_terms_and_rejects_typos():
    assert bigquery.parse_schedule("monthly:15, last:14") == [("monthly", 15), ("last", 14)]
    assert bigquery.parse_schedule("quarterly:1") == [("quarterly", 1)]
    for bad in ("weekly:1", "monthly:31", "last:0", "daily:2", "monthly"):
        with pytest.raises(ValueError):
            bigquery.parse_schedule(bad)


def test_scheduled_dates():
    start, end = date(2020, 7, 21), date(2021, 1, 20)
    assert len(bigquery.scheduled_dates("daily", start, end)) == 184
    assert bigquery.scheduled_dates("monthly:15", start, end)[:2] == [date(2020, 8, 15), date(2020, 9, 15)]
    assert bigquery.scheduled_dates("quarterly:1", start, end) == [date(2020, 10, 1), date(2021, 1, 1)]
    assert bigquery.scheduled_dates("none", start, end) == []
    sampled = bigquery.scheduled_dates("monthly:15,last:14", start, end)
    assert sampled[-14:] == bigquery.days_between(date(2021, 1, 7), end)
    assert sampled.count(date(2021, 1, 15)) == 1
    short_end = date(2020, 7, 25)
    assert bigquery.scheduled_dates("last:14", start, short_end) == bigquery.days_between(start, short_end)


def test_contiguous_ranges():
    days = [date(2020, 10, 15), date(2020, 10, 6), date(2020, 10, 7), date(2020, 10, 9)]
    assert bigquery.contiguous_ranges(days) == [
        (date(2020, 10, 6), date(2020, 10, 7)), (date(2020, 10, 9), date(2020, 10, 9)),
        (date(2020, 10, 15), date(2020, 10, 15))]


def test_last_complete_date():
    partial = {"max_block_date": "2026-03-24", "max_block_timestamp_utc": "2026-03-24T11:02:03+00:00"}
    complete = {"max_block_date": "2026-03-24", "max_block_timestamp_utc": "2026-03-24T23:59:58.9+00:00"}
    live = {"max_block_date": "2026-11-10", "max_block_timestamp_utc": "2026-11-10T23:59:59+00:00"}
    assert bigquery.last_complete_date(partial, TODAY) == date(2026, 3, 23)
    assert bigquery.last_complete_date(complete, TODAY) == date(2026, 3, 24)
    assert bigquery.last_complete_date(live, TODAY) == date(2026, 11, 9)


def test_plan_is_month_chunked_and_clamped_to_freshness(raw_dir):
    freshness = {"min_block_date": "2020-07-21", "max_block_date": "2020-10-20",
                 "max_block_timestamp_utc": "2020-10-20T12:00:00+00:00"}
    plan = bigquery.build_plan(bigquery.QUERIES, freshness, settings(), TODAY)
    assert (plan.start, plan.end, plan.data_through) == (date(2020, 8, 25), date(2020, 10, 19), "2020-10-19")
    daily = plan.chunks_for(bigquery.QUERIES[0])
    assert [(c.month, c.dates[0], c.dates[-1], c.is_last) for c in daily] == [
        ("2020-08", date(2020, 8, 25), date(2020, 8, 31), False),
        ("2020-09", date(2020, 9, 1), date(2020, 9, 30), False),
        ("2020-10", date(2020, 10, 1), date(2020, 10, 19), True)]
    burn = plan.chunks_for(bigquery.QUERIES[1])
    assert [(c.month, [(j.start, j.end) for j in c.jobs]) for c in burn] == [
        ("2020-09", [(date(2020, 9, 15), date(2020, 9, 15))]),
        ("2020-10", [(date(2020, 10, 6), date(2020, 10, 19))])]
    assert all(job.end <= plan.end for job in plan.jobs())
    assert not any(chunk.cached for chunk in plan.chunks)


def test_schedule_env_override(raw_dir, monkeypatch):
    monkeypatch.setenv("BQ_SCHEDULE_FEE_BURN", "quarterly:1")
    plan = bigquery.build_plan(bigquery.QUERIES, None, settings(), date(2021, 2, 1))
    assert [c.dates for c in plan.chunks_for(bigquery.QUERIES[1])] == [(date(2020, 10, 1),), (date(2021, 1, 1),)]


def test_chunk_reusable_rules():
    at_end = {"sql_fingerprint": "abc", "dates": ["2020-10-01", "2020-10-02"], "data_through": "2020-10-02"}
    after = {**at_end, "data_through": "2020-10-05"}
    days = [date(2020, 10, 1), date(2020, 10, 2)]
    assert bigquery.chunk_reusable(at_end, days, "abc", "2020-10-02")
    assert not bigquery.chunk_reusable(at_end, days, "abc", "2020-10-09")
    assert bigquery.chunk_reusable(after, days, "abc", "2020-10-09")
    assert bigquery.chunk_reusable(after, days[:1], "abc", None)
    assert not bigquery.chunk_reusable(after, days, "changed", "2020-10-09")
    assert not bigquery.chunk_reusable(after, [*days, date(2020, 10, 3)], "abc", "2020-10-09")
    assert not bigquery.chunk_reusable({**after, "data_through": None}, days, "abc", "2020-10-09")
    assert not bigquery.chunk_reusable({}, days, "abc", None)
    # accepted hashes (current, legacy): either one reuses the chunk, neither does not
    assert bigquery.chunk_reusable(after, days, ("new", "abc"), "2020-10-09")
    assert not bigquery.chunk_reusable(after, days, ("new", "old"), "2020-10-09")
    # an empty chunk is reused only where a day can have no rows
    empty = {**after, "row_count": 0}
    assert bigquery.chunk_reusable(empty, days, "abc", "2020-10-09")
    assert not bigquery.chunk_reusable(empty, days, "abc", "2020-10-09", empty_ok=False)
    assert bigquery.chunk_reusable({**after, "row_count": 5}, days, "abc", "2020-10-09", empty_ok=False)


def test_sql_fingerprint_ignores_header_prose():
    sql = bigquery.load_sql("daily_supply_flows")
    edited = bigquery.SqlFile(sql.name, sql.text.replace("-- grain: one row per block_date", "-- grain: changed"),
                              sql.header)
    assert edited.fingerprint == sql.fingerprint
    assert bigquery.SqlFile(sql.name, sql.text + "\nLIMIT 1", sql.header).fingerprint != sql.fingerprint


def test_sql_fingerprint_ignores_reflowed_whitespace_and_keeps_the_legacy_hash():
    """Splitting a SELECT one column per line must not bill a byte-identical re-pull: the hash collapses whitespace,
    and the per-line hash is still accepted for chunks written under it."""
    sql = bigquery.load_sql("fee_burn")
    reflowed = bigquery.SqlFile(sql.name, sql.text.replace(",\n", ",\n\n    ").replace(" FROM ", "\nFROM  "),
                                sql.header)
    assert reflowed.text != sql.text and reflowed.fingerprint == sql.fingerprint
    assert reflowed.legacy_fingerprint != sql.legacy_fingerprint
    assert sql.fingerprints == (sql.fingerprint, sql.legacy_fingerprint)
    changed = bigquery.SqlFile(sql.name, sql.text.replace("/ 1e12 AS gas_burnt_tgas", "/ 1e9 AS gas_burnt_tgas"),
                               sql.header)
    assert changed.text != sql.text and changed.fingerprint != sql.fingerprint
    assert bigquery.SqlFile(sql.name, sql.text + "\nLIMIT 1", sql.header).fingerprint != sql.fingerprint


def test_cell_formats_plain_decimals_and_keeps_missing_empty():
    assert bigquery.cell(None) == ""
    assert bigquery.cell(float("nan")) == ""
    assert bigquery.cell(1e-05) == "0.00001"
    assert bigquery.cell(1.311369666584115e9) == "1311369666.584115"
    assert bigquery.cell(2.5e22) == "25000000000000000000000"
    assert bigquery.cell(0.0) == "0.0"
    assert bigquery.cell(42) == "42"
    assert bigquery.cell(True) == "true"
    assert bigquery.cell(Decimal("1E+3")) == "1000"
    assert bigquery.cell(date(2026, 3, 24)) == "2026-03-24"
    assert bigquery.cell(datetime(2026, 3, 24, 7, 0, tzinfo=timezone.utc)) == "2026-03-24T07:00:00+00:00"
    assert bigquery.cell(datetime(2026, 3, 24, 7, 0)) == "2026-03-24T07:00:00+00:00"


def test_format_row_rejects_missing_columns():
    with pytest.raises(ValueError, match="epoch_boundaries"):
        bigquery.format_row({"block_date": date(2020, 1, 1), "eod_supply_near": 1.0, "minted_near": 2.0,
                             "burnt_near": 0.5}, bigquery.QUERIES[0])


def test_gate_budget_and_per_job_cap():
    job = bigquery.Job(bigquery.QUERIES[0], date(2020, 8, 1), date(2020, 8, 31))
    bigquery.gate({job: 10 * GB}, settings(budget_gb=20), yes=False)
    with pytest.raises(bigquery.BudgetExceeded, match="--yes"):
        bigquery.gate({job: 30 * GB}, settings(budget_gb=20), yes=False)
    bigquery.gate({job: 30 * GB}, settings(budget_gb=20), yes=True)
    with pytest.raises(bigquery.BudgetExceeded, match="BQ_MAX_BYTES_BILLED"):
        bigquery.gate({job: 60 * GB}, settings(budget_gb=1000), yes=True)


def test_full_run_writes_chunks_outputs_and_meta(raw_dir):
    client = FakeClient(freshness_row("2020-10-20", "2020-10-20T12:00:00+00:00"))
    bigquery.run(client, settings(location="us-east1"), yes=False, today=TODAY)

    first, second = client.calls[:2]
    assert (first.name, first.dry_run, second.name, second.dry_run) == ("freshness", True, "freshness", False)
    assert (second.start, second.end) == (date(2020, 8, 25), TODAY)
    later = client.calls[2:]
    first_real = next(i for i, call in enumerate(later) if not call.dry_run)
    assert all(call.dry_run for call in later[:first_real]) and not any(call.dry_run for call in later[first_real:])
    assert {call.name for call in later[:first_real]} == {query.name for query in bigquery.QUERIES}
    assert all(call.max_bytes == 50 * GB for call in client.real())
    assert all(call.location == "us-east1" for call in client.calls)
    assert max(call.end for call in later) == date(2020, 10, 19)

    freshness = read_rows(raw_dir / "freshness.csv")
    assert freshness[0]["max_block_date"] == "2020-10-20"
    assert json.loads((raw_dir / "freshness.meta.json").read_text())["last_complete_date"] == "2020-10-19"

    supply = read_rows(raw_dir / "daily_supply_flows.csv")
    assert len(supply) == 7 + 30 + 19
    assert [row["block_date"] for row in supply] == sorted(row["block_date"] for row in supply)
    assert {row["epoch_boundaries"] for row in supply if row["block_date"].endswith("-02")} == {""}
    assert supply[0] == {"block_date": "2020-08-25", "eod_supply_near": "1000000025.0", "minted_near": "25760.09",
                         "burnt_near": "0.00001", "epoch_boundaries": "3"}

    burn = read_rows(raw_dir / "fee_burn_sampled.csv")
    assert [row["block_date"] for row in burn] == ["2020-09-15", *[f"2020-10-{d:02d}" for d in range(6, 20)]]
    assert burn[0] == {"block_date": "2020-09-15", "gross_fee_burn_near": "12.5", "gas_burnt_tgas": "1000000.0"}

    chunk_meta = json.loads((raw_dir / "_chunks" / "daily_supply_flows" / "2020-09.meta.json").read_text())
    assert chunk_meta["dates"][0] == "2020-09-01" and chunk_meta["data_through"] == "2020-10-19"
    assert chunk_meta["jobs"][0]["job_id"].startswith("job_")
    assert chunk_meta["jobs"][0]["bytes_billed"] == 30 * (GB // 10)
    output_meta = json.loads((raw_dir / "fee_burn_sampled.meta.json").read_text())
    assert output_meta["sql_file"] == "sql/fee_burn.sql" and output_meta["incomplete_months"] == []
    assert [c["month"] for c in output_meta["chunks"]] == ["2020-09", "2020-10"]
    assert all(job["job_id"] and job["bytes_processed"] for c in output_meta["chunks"] for job in c["jobs"])
    assert "GCP_PROJECT" not in json.dumps(output_meta)


def test_rerun_reuses_cache_and_refreshes_only_what_moved(raw_dir):
    client = FakeClient(freshness_row("2020-10-20", "2020-10-20T12:00:00+00:00"))
    bigquery.run(client, settings(), yes=False, today=TODAY)

    same = FakeClient(client.freshness)
    bigquery.run(same, settings(), yes=False, today=TODAY)
    assert [call.name for call in same.calls] == ["freshness", "freshness"]
    assert len(read_rows(raw_dir / "daily_supply_flows.csv")) == 56

    moved = FakeClient(freshness_row("2020-11-03", "2020-11-03T23:59:30+00:00"))
    bigquery.run(moved, settings(), yes=False, today=TODAY)
    assert sorted({bigquery.month_key(call.start) for call in moved.real("daily_supply_flows")}) == [
        "2020-10", "2020-11"]
    assert [(call.start, call.end) for call in moved.real("fee_burn")] == [
        (date(2020, 10, 15), date(2020, 10, 15)), (date(2020, 10, 21), date(2020, 10, 31)),
        (date(2020, 11, 1), date(2020, 11, 3))]
    burn_days = sorted({row["block_date"] for row in read_rows(raw_dir / "fee_burn_sampled.csv")})
    assert "2020-10-06" not in burn_days and burn_days[-1] == "2020-11-03"
    assert len(read_rows(raw_dir / "daily_supply_flows.csv")) == 7 + 30 + 31 + 3


def test_budget_abort_runs_only_freshness(raw_dir):
    client = FakeClient(freshness_row("2020-10-20", "2020-10-20T12:00:00+00:00"))
    with pytest.raises(bigquery.BudgetExceeded, match="BQ_BUDGET_GB"):
        bigquery.run(client, settings(budget_gb=5), yes=False, today=TODAY)
    assert [call.name for call in client.real()] == ["freshness"]
    assert not (raw_dir / "_chunks").exists()

    bigquery.run(FakeClient(client.freshness), settings(budget_gb=5), yes=True, today=TODAY)
    assert (raw_dir / "daily_supply_flows.csv").exists()


def test_per_job_cap_refuses_even_with_yes(raw_dir):
    client = FakeClient(freshness_row("2020-10-20", "2020-10-20T12:00:00+00:00"))
    with pytest.raises(bigquery.BudgetExceeded, match="BQ_MAX_BYTES_BILLED"):
        bigquery.run(client, settings(max_bytes=GB), yes=True, today=TODAY)
    assert [call.name for call in client.real()] == ["freshness"]


def test_failure_keeps_finished_chunks_and_reports(raw_dir, capsys):
    client = FakeClient(freshness_row("2020-10-20", "2020-10-20T12:00:00+00:00"),
                        fail_on=("daily_supply_flows", "2020-09"))
    with pytest.raises(RuntimeError, match="daily_supply_flows 2020-09"):
        bigquery.run(client, settings(), yes=False, today=TODAY)
    assert "FAILED daily_supply_flows 2020-09" in capsys.readouterr().out
    assert len(read_rows(raw_dir / "daily_supply_flows.csv")) == 7
    meta = json.loads((raw_dir / "daily_supply_flows.meta.json").read_text())
    assert meta["incomplete_months"] == ["2020-09", "2020-10"]
    assert (raw_dir / "fee_burn_sampled.csv").exists()


def test_dry_run_report_estimates_everything_and_bills_nothing(raw_dir, capsys):
    client = FakeClient(freshness_row("2020-10-20", "2020-10-20T12:00:00+00:00"))
    estimates = bigquery.dry_run_report(client, settings(), today=date(2020, 10, 21))
    assert client.real() == []
    assert {job.query.name for job in estimates} == {"freshness", *(q.name for q in bigquery.QUERIES)}
    out = capsys.readouterr().out
    assert "daily_supply_flows" in out and "total to run" in out


def test_plan_command_is_offline(raw_dir, capsys):
    assert bigquery.main(["--plan", "--queries", "fee_burn"]) == 0
    out = capsys.readouterr().out
    assert "freshness" in out and "fee_burn" in out and "daily_supply_flows" not in out
    with pytest.raises(ValueError):
        bigquery.selected_queries("nope")


def test_plan_uses_cached_freshness(raw_dir, capsys):
    raw_dir.mkdir(parents=True)
    (raw_dir / "freshness.csv").write_text(
        "min_block_date,max_block_date,days_present,days_spanned,max_block_height,max_block_timestamp_utc\n"
        "2020-07-21,2026-03-24,1343,1343,190000000,2026-03-24T10:00:00+00:00\n")
    assert bigquery.main(["--plan"]) == 0
    assert "2020-07-21..2026-03-23" in capsys.readouterr().out


def test_month_end_day_is_refetched_once_after_the_window_leaves_it(raw_dir):
    bigquery.run(FakeClient(freshness_row("2020-10-31", "2020-10-31T23:59:30+00:00")), settings(), yes=False,
                 today=TODAY)
    next_day = FakeClient(freshness_row("2020-11-01", "2020-11-01T23:59:30+00:00"))
    bigquery.run(next_day, settings(), yes=False, today=TODAY)
    assert [(call.start, call.end) for call in next_day.real("daily_supply_flows")] == [
        (date(2020, 10, 1), date(2020, 10, 31)), (date(2020, 11, 1), date(2020, 11, 1))]
    october = json.loads((raw_dir / "_chunks" / "daily_supply_flows" / "2020-10.meta.json").read_text())
    assert october["data_through"] == "2020-11-01"

    later = FakeClient(freshness_row("2020-11-02", "2020-11-02T23:59:30+00:00"))
    bigquery.run(later, settings(), yes=False, today=TODAY)
    assert [(call.start, call.end) for call in later.real("daily_supply_flows")] == [
        (date(2020, 11, 1), date(2020, 11, 2))]
    assert json.loads((raw_dir / "daily_supply_flows.meta.json").read_text())["incomplete_months"] == []


def test_freshness_scans_from_the_cached_max_date_after_the_first_run(raw_dir):
    bigquery.run(FakeClient(freshness_row("2020-10-20", "2020-10-20T12:00:00+00:00")), settings(), yes=False,
                 today=TODAY)
    moved = FakeClient(freshness_row("2020-11-03", "2020-11-03T23:59:30+00:00"))
    bigquery.run(moved, settings(), yes=False, today=TODAY)
    assert [(call.name, call.start, call.end, call.dry_run) for call in moved.calls[:2]] == [
        ("freshness", date(2020, 10, 20), TODAY, True), ("freshness", date(2020, 10, 20), TODAY, False)]
    row = read_rows(raw_dir / "freshness.csv")[0]
    assert [row[key] for key in ("min_block_date", "max_block_date", "days_present", "days_spanned")] == [
        "2020-08-25", "2020-11-03", "71", "71"]
    meta = json.loads((raw_dir / "freshness.meta.json").read_text())
    assert (meta["scan"], meta["window_start"], meta["params"]["start_date"]) == (
        "incremental", "2020-08-25", "2020-10-20")

    restarted = FakeClient(moved.freshness)
    bigquery.run(restarted, settings(start=date(2020, 9, 1)), yes=False, today=TODAY)
    assert restarted.real("freshness")[0].start == date(2020, 9, 1)
    assert json.loads((raw_dir / "freshness.meta.json").read_text())["scan"] == "full"


def test_freshness_refuses_to_extend_a_cache_the_table_no_longer_matches(raw_dir):
    bigquery.run(FakeClient(freshness_row("2020-10-20", "2020-10-20T12:00:00+00:00")), settings(), yes=False,
                 today=TODAY)
    shrunk = FakeClient(freshness_row("2020-10-10", "2020-10-10T23:59:30+00:00"))
    with pytest.raises(RuntimeError, match="no longer in blocks"):
        bigquery.run(shrunk, settings(), yes=False, today=TODAY)
    assert [call.name for call in shrunk.real()] == ["freshness"]
    assert read_rows(raw_dir / "freshness.csv")[0]["max_block_date"] == "2020-10-20"


@pytest.mark.parametrize("argv", [[], ["--dry-run"]])
@pytest.mark.parametrize("error_name, hint", [("NotFound", "BQ_LOCATION=us-east1"),
                                              ("Forbidden", "roles/bigquery.jobUser"),
                                              ("RefreshError", "gcloud auth application-default login")])
def test_google_errors_print_a_hint_and_mask_the_project(raw_dir, monkeypatch, capsys, argv, error_name, hint):
    from google.api_core import exceptions as api
    from google.auth import exceptions as auth

    error_type = getattr(api, error_name, None) or getattr(auth, error_name)
    monkeypatch.setenv("GCP_PROJECT", "my-billing-proj")
    error = error_type("Dataset my-billing-proj:crypto_near_mainnet_us was not found in location US")
    client = FakeClient(freshness_row("2020-10-20", "2020-10-20T12:00:00+00:00"), error=error)
    monkeypatch.setattr(bigquery, "make_client", lambda _settings: client)
    assert bigquery.main(argv) == 1
    out = capsys.readouterr().out
    assert error_name in out and hint in out and "<GCP_PROJECT>" in out
    assert "my-billing-proj" not in out and "Traceback" not in out


def test_lookback_day_counts_in_the_header_estimate():
    supply = next(query for query in bigquery.QUERIES if query.name == "daily_supply_flows")
    burn = next(query for query in bigquery.QUERIES if query.name == "fee_burn")
    assert (supply.lookback_days, burn.lookback_days) == (1, 0)
    assert bigquery.Job(supply, date(2026, 11, 7), date(2026, 11, 10)).scan_days == 5
    assert bigquery.Job(burn, date(2026, 11, 7), date(2026, 11, 10)).scan_days == 4


class NoOctoberBurnRows(FakeClient):
    def rows(self, name: str, start: date, end: date) -> list[dict]:
        return [] if name == "fee_burn" and start.month == 10 else super().rows(name, start, end)


def test_a_chunk_that_returns_no_rows_is_flagged(raw_dir, capsys):
    """An empty chunk prints a warning naming the query, the month and the SQL file, and is recorded in the
    sidecars."""
    bigquery.run(NoOctoberBurnRows(freshness_row("2020-10-20", "2020-10-20T12:00:00+00:00")), settings(), yes=False,
                 today=TODAY)
    out = capsys.readouterr().out
    warnings = [line for line in out.splitlines() if "WARNING" in line]
    assert warnings == ["[bigquery] WARNING fee_burn 2020-10: no rows for 14 planned day(s); check "
                        "sql/fee_burn.sql against the live schema before relying on the output; the "
                        "chunk is listed in empty_months"]
    assert [row["block_date"] for row in read_rows(raw_dir / "fee_burn_sampled.csv")] == ["2020-09-15"]
    # the empty chunk is recorded, not only printed: row_count 0 in the chunk, empty_months in the output sidecar
    chunk_meta = json.loads((raw_dir / "_chunks" / "fee_burn" / "2020-10.meta.json").read_text())
    assert chunk_meta["row_count"] == 0
    output_meta = json.loads((raw_dir / "fee_burn_sampled.meta.json").read_text())
    assert output_meta["empty_months"] == ["2020-10"] and output_meta["incomplete_months"] == []
    assert json.loads((raw_dir / "daily_supply_flows.meta.json").read_text())["empty_months"] == []


class NoSeptemberSupplyRows(FakeClient):
    def rows(self, name: str, start: date, end: date) -> list[dict]:
        return [] if name == "daily_supply_flows" and start.month == 9 else super().rows(name, start, end)


def test_an_empty_chunk_of_a_query_with_a_row_every_day_is_incomplete_and_rerun(raw_dir, capsys):
    """daily_supply_flows reads blocks, which exist every day: an empty month is a fault, so it is listed in
    incomplete_months and re-run by the next pull, while the full months stay cached."""
    supply = next(query for query in bigquery.QUERIES if query.name == "daily_supply_flows")
    assert supply.rows_every_day and not next(q for q in bigquery.QUERIES if q.name == "fee_burn").rows_every_day
    fresh = freshness_row("2020-10-20", "2020-10-20T23:59:30+00:00")
    bigquery.run(NoSeptemberSupplyRows(fresh), settings(), yes=False, today=TODAY)
    assert "incomplete_months and re-run next time" in capsys.readouterr().out
    meta = json.loads((raw_dir / "daily_supply_flows.meta.json").read_text())
    assert meta["empty_months"] == ["2020-09"] and meta["incomplete_months"] == ["2020-09"]
    again = FakeClient(fresh)
    bigquery.run(again, settings(), yes=False, today=TODAY)
    assert [(call.start, call.end) for call in again.real("daily_supply_flows")] == [
        (date(2020, 9, 1), date(2020, 9, 30))]
    meta = json.loads((raw_dir / "daily_supply_flows.meta.json").read_text())
    assert meta["empty_months"] == [] and meta["incomplete_months"] == []


def test_a_chunk_written_before_rows_were_recorded_is_counted_from_its_csv(raw_dir):
    fresh = freshness_row("2020-10-20", "2020-10-20T23:59:30+00:00")
    bigquery.run(NoSeptemberSupplyRows(fresh), settings(), yes=False, today=TODAY)
    meta_path = raw_dir / "_chunks" / "daily_supply_flows" / "2020-09.meta.json"
    legacy = json.loads(meta_path.read_text())
    del legacy["row_count"]
    meta_path.write_text(json.dumps(legacy))
    supply = next(query for query in bigquery.QUERIES if query.name == "daily_supply_flows")
    assert bigquery.chunk_rows(supply, "2020-09", legacy) == 0
    dates = [date.fromisoformat(d) for d in legacy["dates"]]
    assert not bigquery.chunk_is_fresh(supply, "2020-09", dates, legacy["data_through"])
