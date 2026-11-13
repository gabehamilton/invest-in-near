"""NEAR mainnet aggregates from the public BigQuery dataset bigquery-public-data.crypto_near_mainnet_us.

Provenance: every number comes from one of the SQL files in sql/<query>.sql, run with the
DATE query parameters @start_date/@end_date on block_date. The dataset is published by NEAR
(https://docs.near.org/data-infrastructure/big-query) and billed to GCP_PROJECT on-demand.
Credentials are Application Default Credentials (`gcloud auth application-default login`).

Cost control, in order:
  1. freshness.sql runs first (dry-run, budget check, then the real query). Without a cached
     freshness.csv for the same BQ_START_DATE it scans three blocks columns over the whole
     window (about 4.6 GB by the header estimate, more on recent data); later runs scan only
     from the cached max_block_date and carry min_block_date forward. Its last complete
     block_date clamps every other job: no job asks for dates beyond it.
  2. Every planned job is dry-run; a GB table is printed; the run aborts when any job is
     above BQ_MAX_BYTES_BILLED or the total is above BQ_BUDGET_GB (the latter unless --yes).
  3. Real jobs carry maximum_bytes_billed, so BigQuery itself refuses anything larger.
  4. Results are cached per month in data/raw/bigquery/_chunks/<query>/<YYYY-MM>.csv. A chunk
     is reused when its SQL fingerprint matches, it covers every planned date of the month, and
     it was fetched either at the current data-through date or after every planned date. So the
     month holding the window end is re-run whenever freshness moves, and once more after the
     window leaves it: its last day was fetched as the window end, when tables other than
     blocks may still have been loading. The fingerprint hashes the executable SQL with its
     whitespace collapsed, so re-flowing a SELECT costs nothing; chunks written under the older
     per-line hash stay reusable. Each chunk records its row count. An empty chunk is listed in
     the output sidecar's empty_months; for a query whose grain gives every day a row
     (rows_every_day) it is also never reused and is listed in incomplete_months.

Schedules (env BQ_SCHEDULE_<QUERY>, comma-separated terms): daily | monthly:<1-28> |
quarterly:<1-28> | last:<n> | none. Defaults: daily_supply_flows daily; fee_burn monthly:15,last:14.
BQ_START_DATE (default 2020-07-21, mainnet genesis) bounds every window; BQ_LOCATION pins the
job location (the Nov 2024 schema dump says us-east1).

Usage:
    uv run python -m pipeline.sources.bigquery --plan       # offline: planned jobs, header estimates
    uv run python -m pipeline.sources.bigquery --dry-run    # dry-run every planned job (no bytes billed)
    uv run python -m pipeline.sources.bigquery [--yes]      # freshness, dry-run, budget gate, run, write
"""

from __future__ import annotations

import argparse
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from functools import cache
from math import isfinite
from typing import Any, Iterable

from pipeline import config
from pipeline.config import env, read_csv, read_meta, sha256_text, write_csv

NAME = "bigquery"
REQUIRES = ["GCP_PROJECT"]

DATASET = "bigquery-public-data.crypto_near_mainnet_us"
GENESIS_DATE = date(2020, 7, 21)
BYTES_PER_GB = 1024 ** 3
DEFAULT_BUDGET_GB = 500.0
DEFAULT_MAX_BYTES_BILLED = 50 * BYTES_PER_GB
DRY_RUN_WORKERS = 8
CHUNK_SOURCE = f"{NAME}/_chunks"
LAST_BLOCK_OF_DAY = time(23, 59)


@dataclass(frozen=True)
class Query:
    name: str
    output: str
    columns: tuple[str, ...]
    schedule: str
    date_column: str = "block_date"
    sort_keys: tuple[str, ...] = ()
    lookback_days: int = 0
    # the grain gives every day at least one row (blocks and chunks exist every day), so an empty month chunk is a fault
    rows_every_day: bool = False


FRESHNESS = Query(
    "freshness", "freshness",
    ("min_block_date", "max_block_date", "days_present", "days_spanned", "max_block_height",
     "max_block_timestamp_utc"),
    schedule="", date_column="max_block_date",
)

QUERIES = (
    Query("daily_supply_flows", "daily_supply_flows",
          ("block_date", "eod_supply_near", "minted_near", "burnt_near", "epoch_boundaries"), "daily",
          lookback_days=1, rows_every_day=True),
    Query("fee_burn", "fee_burn_sampled",
          ("block_date", "gross_fee_burn_near", "gas_burnt_tgas"), "monthly:15,last:14"),
)


class BudgetExceeded(RuntimeError):
    """Raised before any billable work when the dry-run estimate breaks a limit."""


@dataclass(frozen=True)
class Settings:
    budget_gb: float
    max_bytes: int
    location: str | None
    start: date

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            budget_gb=float(env("BQ_BUDGET_GB", str(DEFAULT_BUDGET_GB))),
            max_bytes=int(env("BQ_MAX_BYTES_BILLED", str(DEFAULT_MAX_BYTES_BILLED))),
            location=env("BQ_LOCATION"),
            start=date.fromisoformat(env("BQ_START_DATE", GENESIS_DATE.isoformat())),
        )


@dataclass(frozen=True)
class SqlFile:
    name: str
    text: str
    header: dict[str, str]

    @property
    def relpath(self) -> str:
        return f"sql/{self.name}.sql"

    def _body(self) -> list[str]:
        return [line.strip() for line in self.text.splitlines() if line.strip() and not line.strip().startswith("--")]

    @property
    def fingerprint(self) -> str:
        """Hash of the executable SQL only, with every run of whitespace (line breaks included) collapsed to one space,
        so editing header prose or re-flowing a statement never invalidates the cache. A literal that differs only in
        its inner whitespace would hash the same; no query here has one."""
        return sha256_text(" ".join(" ".join(self._body()).split()))

    @property
    def legacy_fingerprint(self) -> str:
        """The per-line hash (stripped lines joined by newlines). Chunks that recorded it are reused while the SQL still
        matches it, so they bill no re-pull."""
        return sha256_text("\n".join(self._body()))

    @property
    def fingerprints(self) -> tuple[str, str]:
        return self.fingerprint, self.legacy_fingerprint

    @property
    def gb_per_day(self) -> float:
        match = re.match(r"\s*([\d.]+)\s*GB/day", self.header.get("est", ""))
        if not match:
            raise ValueError(f"{self.relpath}: '-- est:' must start with '<number> GB/day'")
        return float(match.group(1))


@dataclass(frozen=True)
class Job:
    query: Query
    start: date
    end: date

    @property
    def scan_days(self) -> int:
        return (self.end - self.start).days + 1 + self.query.lookback_days

    @property
    def label(self) -> str:
        span = self.start.isoformat() if self.start == self.end else f"{self.start}..{self.end}"
        return f"{self.query.name} {span}"


@dataclass(frozen=True)
class Chunk:
    query: Query
    month: str
    dates: tuple[date, ...]
    is_last: bool
    cached: bool

    @property
    def jobs(self) -> list[Job]:
        return [Job(self.query, start, end) for start, end in contiguous_ranges(self.dates)]


@dataclass(frozen=True)
class Plan:
    start: date
    end: date
    data_through: str | None
    chunks: tuple[Chunk, ...]

    def chunks_for(self, query: Query) -> list[Chunk]:
        return [chunk for chunk in self.chunks if chunk.query == query]

    def jobs(self, *, include_cached: bool = False) -> list[Job]:
        return [job for chunk in self.chunks if include_cached or not chunk.cached for job in chunk.jobs]


def parse_header(text: str) -> dict[str, str]:
    header: dict[str, str] = {}
    for line in text.splitlines():
        if not line.startswith("--"):
            break
        key, sep, value = line[2:].partition(":")
        if sep:
            header[key.strip()] = value.strip()
    return header


@cache
def load_sql(name: str) -> SqlFile:
    text = (config.SQL_DIR / f"{name}.sql").read_text()
    return SqlFile(name, text, parse_header(text))


def parse_schedule(spec: str) -> list[tuple[str, int]]:
    terms: list[tuple[str, int]] = []
    for term in (part.strip() for part in spec.split(",")):
        kind, _, arg = term.partition(":")
        if kind in ("daily", "none") and not arg:
            terms.append((kind, 0))
        elif kind in ("monthly", "quarterly") and arg.isdigit() and 1 <= int(arg) <= 28:
            terms.append((kind, int(arg)))
        elif kind == "last" and arg.isdigit() and int(arg) > 0:
            terms.append((kind, int(arg)))
        else:
            raise ValueError(f"bad schedule term {term!r}: use daily, monthly:<1-28>, quarterly:<1-28>, "
                             "last:<n> or none")
    return terms


def schedule_for(query: Query) -> str:
    return env(f"BQ_SCHEDULE_{query.name.upper()}", query.schedule)


def days_between(start: date, end: date) -> list[date]:
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def scheduled_dates(spec: str, start: date, end: date) -> list[date]:
    dates: set[date] = set()
    for kind, n in parse_schedule(spec):
        if kind == "daily":
            dates.update(days_between(start, end))
        elif kind == "last":
            dates.update(days_between(max(start, end - timedelta(days=n - 1)), end))
        elif kind in ("monthly", "quarterly"):
            months = range(1, 13) if kind == "monthly" else (1, 4, 7, 10)
            dates.update(d for year in range(start.year, end.year + 1) for month in months
                         if start <= (d := date(year, month, n)) <= end)
    return sorted(dates)


def contiguous_ranges(dates: Iterable[date]) -> list[tuple[date, date]]:
    ranges: list[tuple[date, date]] = []
    for day in sorted(dates):
        if ranges and day - ranges[-1][1] == timedelta(days=1):
            ranges[-1] = (ranges[-1][0], day)
        else:
            ranges.append((day, day))
    return ranges


def month_key(day: date) -> str:
    return day.strftime("%Y-%m")


def chunk_csv_exists(query: Query, month: str) -> bool:
    return (config.RAW_DIR / CHUNK_SOURCE / query.name / f"{month}.csv").exists()


def chunk_reusable(meta: dict, dates: Iterable[date], fingerprint: str | Iterable[str], data_through: str | None,
                   *, empty_ok: bool = True) -> bool:
    """A chunk fetched while one of its planned days was the window end is reused only at that same end. fingerprint
    is one hash or the accepted hashes (current, legacy). With empty_ok False a chunk that recorded no rows is a fault
    and is never reused."""
    planned = {day.isoformat() for day in dates}
    accepted = {fingerprint} if isinstance(fingerprint, str) else set(fingerprint)
    if not meta or meta.get("sql_fingerprint") not in accepted or not planned <= set(meta.get("dates", [])):
        return False
    if not empty_ok and meta.get("row_count") == 0:
        return False
    fetched_through = meta.get("data_through")
    return bool(fetched_through) and (fetched_through == data_through or fetched_through > max(planned, default=""))


def chunk_rows(query: Query, month: str, meta: dict) -> int:
    """The chunk's recorded row count; a chunk written before rows were recorded is counted from its CSV."""
    count = meta.get("row_count")
    return int(count) if count is not None else len(read_csv(f"{CHUNK_SOURCE}/{query.name}", month))


def chunk_is_fresh(query: Query, month: str, dates: Iterable[date], data_through: str | None) -> bool:
    if not chunk_csv_exists(query, month):
        return False
    meta = read_meta(f"{CHUNK_SOURCE}/{query.name}", month)
    if meta and query.rows_every_day:
        meta = {**meta, "row_count": chunk_rows(query, month, meta)}
    return chunk_reusable(meta, dates, load_sql(query.name).fingerprints, data_through,
                          empty_ok=not query.rows_every_day)


def as_utc(moment: datetime) -> datetime:
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment.astimezone(timezone.utc)


def last_complete_date(freshness: dict, today: date) -> date:
    max_date = date.fromisoformat(freshness["max_block_date"])
    last_block = as_utc(datetime.fromisoformat(freshness["max_block_timestamp_utc"]))
    complete = last_block >= datetime.combine(max_date, LAST_BLOCK_OF_DAY, timezone.utc)
    return min(max_date if complete else max_date - timedelta(days=1), today - timedelta(days=1))


def plan_window(freshness: dict | None, settings: Settings, today: date) -> tuple[date, date, str | None]:
    if not freshness:
        return settings.start, today - timedelta(days=1), None
    end = last_complete_date(freshness, today)
    return max(settings.start, date.fromisoformat(freshness["min_block_date"])), end, end.isoformat()


def build_plan(queries: Iterable[Query], freshness: dict | None, settings: Settings, today: date) -> Plan:
    start, end, through = plan_window(freshness, settings, today)
    chunks: list[Chunk] = []
    for query in queries:
        by_month: dict[str, list[date]] = {}
        for day in scheduled_dates(schedule_for(query), start, end):
            by_month.setdefault(month_key(day), []).append(day)
        for month, dates in by_month.items():
            cached = chunk_is_fresh(query, month, dates, through)
            chunks.append(Chunk(query, month, tuple(dates), month == month_key(end), cached))
    return Plan(start, end, through, tuple(chunks))


def selected_queries(names: str | None) -> tuple[Query, ...]:
    if not names:
        return QUERIES
    wanted = {name.strip() for name in names.split(",") if name.strip()}
    unknown = wanted - {query.name for query in QUERIES}
    if unknown:
        raise ValueError(f"unknown queries {sorted(unknown)}; choose from {[q.name for q in QUERIES]}")
    return tuple(query for query in QUERIES if query.name in wanted)


def cached_freshness() -> dict | None:
    rows = read_csv(NAME, FRESHNESS.output)
    return rows[0] if rows and rows[0].get("max_block_date") else None


def freshness_base(settings: Settings) -> dict | None:
    """The cached freshness row an incremental scan can extend: same BQ_START_DATE, else None (full scan)."""
    row = cached_freshness()
    same_window = read_meta(NAME, FRESHNESS.output).get("window_start") == settings.start.isoformat()
    return row if row and same_window else None


def merge_freshness(base: dict | None, scanned: dict[str, str] | None, job: Job) -> dict[str, str]:
    """Extend the cached row with a scan that starts on its max_block_date; that shared day is counted once."""
    if base is not None and (not scanned or scanned["min_block_date"] != base["max_block_date"]):
        raise RuntimeError(
            f"freshness: cached max_block_date {base['max_block_date']} is no longer in blocks (a scan from it "
            f"starts at {(scanned or {}).get('min_block_date') or 'nothing'}); delete "
            f"data/raw/{NAME}/{FRESHNESS.output}.csv to rescan the whole window")
    if not scanned or not scanned["max_block_date"]:
        raise RuntimeError(f"freshness found no blocks between {job.start} and {job.end}")
    if base is None:
        return scanned
    days_spanned = (date.fromisoformat(scanned["max_block_date"]) - date.fromisoformat(base["min_block_date"])).days
    return {**scanned, "min_block_date": base["min_block_date"],
            "days_present": str(int(base["days_present"]) + int(scanned["days_present"]) - 1),
            "days_spanned": str(days_spanned + 1)}


def cell(value: Any) -> str:
    """Render a BigQuery value for CSV: empty for NULL/non-finite, plain decimals, ISO dates in UTC."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        return as_utc(value).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float):
        return format(Decimal(repr(value)), "f") if isfinite(value) else ""
    if isinstance(value, Decimal):
        return format(value, "f") if value.is_finite() else ""
    return str(value)


def format_row(record: dict, query: Query) -> dict[str, str]:
    missing = [column for column in query.columns if column not in record]
    if missing:
        raise ValueError(f"{query.name}: result is missing columns {missing}")
    return {column: cell(record[column]) for column in query.columns}


def job_config(job: Job, *, dry_run: bool, max_bytes: int | None = None) -> Any:
    from google.cloud import bigquery

    query_config = bigquery.QueryJobConfig(
        dry_run=dry_run,
        use_query_cache=not dry_run,
        query_parameters=[
            bigquery.ScalarQueryParameter("start_date", "DATE", job.start),
            bigquery.ScalarQueryParameter("end_date", "DATE", job.end),
        ],
    )
    if max_bytes is not None:
        query_config.maximum_bytes_billed = max_bytes
    return query_config


def make_client(settings: Settings) -> Any:
    import google.auth
    from google.auth.exceptions import DefaultCredentialsError
    from google.cloud import bigquery

    try:
        credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    except DefaultCredentialsError as exc:
        raise RuntimeError("no Google credentials: run `gcloud auth application-default login`") from exc
    return bigquery.Client(project=env("GCP_PROJECT"), credentials=credentials, location=settings.location)


def google_errors() -> tuple[type[Exception], ...]:
    from google.api_core.exceptions import GoogleAPICallError
    from google.auth.exceptions import GoogleAuthError

    return GoogleAPICallError, GoogleAuthError


def mask(text: str) -> str:
    """The project id from .env replaced by <GCP_PROJECT> (Google and HTTP errors often quote it in a URL)."""
    project = env("GCP_PROJECT")
    return text.replace(project, "<GCP_PROJECT>") if project else text


def describe(exc: BaseException) -> str:
    """Exception type and message, with the project id masked."""
    return mask(f"{type(exc).__name__}: {exc}")


def error_hint(exc: BaseException, settings: Settings) -> str:
    from google.api_core.exceptions import Forbidden, NotFound
    from google.auth.exceptions import GoogleAuthError

    if isinstance(exc, NotFound):
        return (f"; the dataset may not be in BQ_LOCATION={settings.location or 'auto'}: try BQ_LOCATION=us-east1 "
                "(Nov 2024 schema dump) or US")
    if isinstance(exc, Forbidden):
        return ("; check that GCP_PROJECT has billing and the BigQuery API enabled and that your account can "
                "create jobs in it (roles/bigquery.jobUser)")
    if isinstance(exc, GoogleAuthError):
        return "; run `gcloud auth application-default login`"
    return ""


def dry_run_bytes(client: Any, job: Job, settings: Settings) -> int:
    query_job = client.query(load_sql(job.query.name).text, job_config=job_config(job, dry_run=True),
                             location=settings.location)
    return int(query_job.total_bytes_processed or 0)


def dry_run_all(client: Any, jobs: list[Job], settings: Settings) -> dict[Job, int]:
    with ThreadPoolExecutor(max_workers=DRY_RUN_WORKERS) as pool:
        sizes = list(pool.map(lambda job: dry_run_bytes(client, job, settings), jobs))
    return dict(zip(jobs, sizes))


def execute(client: Any, job: Job, settings: Settings) -> tuple[list[dict[str, str]], dict]:
    query_job = client.query(load_sql(job.query.name).text,
                             job_config=job_config(job, dry_run=False, max_bytes=settings.max_bytes),
                             location=settings.location)
    rows = [format_row(dict(row.items()), job.query) for row in query_job.result()]
    info = {
        "start_date": job.start.isoformat(),
        "end_date": job.end.isoformat(),
        "job_id": query_job.job_id,
        "bytes_processed": query_job.total_bytes_processed,
        "bytes_billed": query_job.total_bytes_billed,
        "cache_hit": getattr(query_job, "cache_hit", None),
        "rows": len(rows),
    }
    return rows, info


def gate(estimates: dict[Job, int], settings: Settings, *, yes: bool) -> None:
    over_cap = [job.label for job, size in estimates.items() if size > settings.max_bytes]
    if over_cap:
        raise BudgetExceeded(
            f"refused: {len(over_cap)} job(s) estimated above BQ_MAX_BYTES_BILLED "
            f"({settings.max_bytes / BYTES_PER_GB:.1f} GB): {', '.join(over_cap[:5])}; "
            "raise the cap or narrow the schedule")
    total_gb = sum(estimates.values()) / BYTES_PER_GB
    if total_gb > settings.budget_gb and not yes:
        raise BudgetExceeded(
            f"estimated {total_gb:.1f} GB > BQ_BUDGET_GB {settings.budget_gb:.1f} GB; "
            "re-run with --yes to proceed")


def gb(size: float | None) -> str:
    return "-" if size is None else f"{size / BYTES_PER_GB:.2f}"


def estimate_table(plan: Plan, queries: Iterable[Query], estimates: dict[Job, int], settings: Settings,
                   first: Job | None = None) -> str:
    lines = [
        f"BigQuery estimate (GB = 2^30 bytes; window {plan.start}..{plan.end}; "
        f"data through {plan.data_through or 'unknown, freshness not run'}; location {settings.location or 'auto'})",
        f"{'query':<20}{'chunks':>8}{'cached':>8}{'jobs':>6}{'run_GB':>10}{'all_GB':>10}{'max_job_GB':>12}",
    ]
    rows: list[tuple[str, int, int, list[Job], list[Job]]] = []
    if first:
        rows.append((FRESHNESS.name, 1, 0, [first], [first]))
    for query in queries:
        chunks = plan.chunks_for(query)
        to_run = [job for chunk in chunks if not chunk.cached for job in chunk.jobs]
        every = [job for chunk in chunks for job in chunk.jobs]
        rows.append((query.name, len(chunks), sum(chunk.cached for chunk in chunks), to_run, every))
    total_run = 0
    for name, n_chunks, n_cached, to_run, every in rows:
        run_bytes = sum(estimates.get(job, 0) for job in to_run)
        all_bytes = sum(estimates[job] for job in every) if all(job in estimates for job in every) else None
        largest = max((estimates.get(job, 0) for job in to_run), default=0)
        total_run += run_bytes
        lines.append(f"{name:<20}{n_chunks:>8}{n_cached:>8}{len(to_run):>6}{gb(run_bytes):>10}"
                     f"{gb(all_bytes):>10}{gb(largest):>12}")
    lines.append(f"{'total to run':<42}{gb(total_run):>10}   budget {settings.budget_gb:.1f} GB, "
                 f"per-job cap {settings.max_bytes / BYTES_PER_GB:.1f} GB")
    return "\n".join(lines)


def header_gb(job: Job) -> float:
    return job.scan_days * load_sql(job.query.name).gb_per_day


def plan_text(plan: Plan, queries: Iterable[Query], settings: Settings, first: Job) -> str:
    lines = [
        f"Offline plan: window {plan.start}..{plan.end}; data through "
        f"{plan.data_through or 'unknown (no cached freshness.csv; the run clamps to it)'}",
        "Estimates use each SQL header's GB/day x days scanned (Nov 2024 averages); the dry run is authoritative.",
        "",
        f"{'job':<20}{'start':>12}{'end':>12}{'scan_days':>11}{'est_GB':>9}  status",
        f"{FRESHNESS.name:<20}{first.start.isoformat():>12}{first.end.isoformat():>12}"
        f"{first.scan_days:>11}{header_gb(first):>9.2f}  run first",
    ]
    for chunk in plan.chunks:
        for job in chunk.jobs:
            status = "cached" if chunk.cached else ("run (last month)" if chunk.is_last else "run")
            lines.append(f"{chunk.query.name:<20}{job.start.isoformat():>12}{job.end.isoformat():>12}"
                         f"{job.scan_days:>11}{header_gb(job):>9.2f}  {status}")
    lines += ["", f"{'query':<20}{'schedule':<22}{'dates':>7}{'chunks':>8}{'cached':>8}{'jobs':>6}{'run_GB':>9}"]
    total = header_gb(first)
    lines.append(f"{FRESHNESS.name:<20}{'-':<22}{1:>7}{1:>8}{0:>8}{1:>6}{total:>9.2f}")
    for query in queries:
        chunks = plan.chunks_for(query)
        to_run = [job for chunk in chunks if not chunk.cached for job in chunk.jobs]
        run_gb = sum(header_gb(job) for job in to_run)
        total += run_gb
        lines.append(f"{query.name:<20}{schedule_for(query):<22}{sum(len(c.dates) for c in chunks):>7}"
                     f"{len(chunks):>8}{sum(c.cached for c in chunks):>8}{len(to_run):>6}{run_gb:>9.2f}")
    lines.append(f"{'total':<71}{total:>9.2f}   budget {settings.budget_gb:.1f} GB, per-job cap "
                 f"{settings.max_bytes / BYTES_PER_GB:.1f} GB, location {settings.location or 'auto'}")
    return "\n".join(lines)


def freshness_job(settings: Settings, today: date, base: dict | None = None) -> Job:
    start = min(date.fromisoformat(base["max_block_date"]), today) if base else settings.start
    return Job(FRESHNESS, start, today)


def write_freshness(row: dict[str, str], info: dict, through: str, settings: Settings, *, incremental: bool) -> None:
    sql = load_sql(FRESHNESS.name)
    write_csv(NAME, FRESHNESS.output, [row], list(FRESHNESS.columns),
              sql_file=sql.relpath, sql_sha256=sha256_text(sql.text), description=sql.header["description"],
              grain=sql.header["grain"], params={"start_date": info["start_date"], "end_date": info["end_date"]},
              dataset=DATASET, location=settings.location or "auto", window_start=settings.start.isoformat(),
              scan="incremental" if incremental else "full",
              scan_rule="full scan from window_start when no freshness.csv exists for the same window_start; "
                        "otherwise from the cached max_block_date, keeping the cached min_block_date and "
                        "days_present = cached + scanned - 1 (the shared day). Assumes blocks before the cached "
                        "max_block_date do not change; delete freshness.csv to rescan the whole window",
              last_complete_date=through,
              last_complete_rule="max_block_date if its last block is at or after 23:59 UTC, else the day "
                                 "before; never later than yesterday UTC",
              jobs=[info])


def write_chunk(chunk: Chunk, rows: list[dict[str, str]], jobs: list[dict], data_through: str | None) -> None:
    sql = load_sql(chunk.query.name)
    write_csv(f"{CHUNK_SOURCE}/{chunk.query.name}", chunk.month, rows, list(chunk.query.columns),
              description=f"Month chunk {chunk.month} of {sql.relpath}", sql_file=sql.relpath,
              sql_fingerprint=sql.fingerprint, query=chunk.query.name, month=chunk.month,
              dates=[day.isoformat() for day in chunk.dates],
              ranges=[[start.isoformat(), end.isoformat()] for start, end in contiguous_ranges(chunk.dates)],
              data_through=data_through, row_count=len(rows), jobs=jobs)


def write_output(query: Query, plan: Plan, settings: Settings) -> int:
    sql = load_sql(query.name)
    source = f"{CHUNK_SOURCE}/{query.name}"
    rows: list[dict[str, str]] = []
    chunks_meta: list[dict] = []
    for chunk in plan.chunks_for(query):
        wanted = {day.isoformat() for day in chunk.dates}
        rows += [row for row in read_csv(source, chunk.month) if row[query.date_column][:10] in wanted]
        meta = read_meta(source, chunk.month)
        chunks_meta.append({
            "month": chunk.month,
            "planned_dates": len(chunk.dates),
            "row_count": chunk_rows(query, chunk.month, meta) if chunk_csv_exists(query, chunk.month) else None,
            "reused_from_cache": chunk.cached,
            "complete": chunk_is_fresh(query, chunk.month, chunk.dates, plan.data_through),
            "fetched_at": meta.get("fetched_at"),
            "jobs": meta.get("jobs", []),
        })
    rows.sort(key=lambda row: (row[query.date_column], *(row[key] for key in query.sort_keys)))
    incomplete = [entry["month"] for entry in chunks_meta if not entry["complete"]]
    empty = [entry["month"] for entry in chunks_meta if entry["row_count"] == 0]
    write_csv(NAME, query.output, rows, list(query.columns),
              sql_file=sql.relpath, sql_sha256=sha256_text(sql.text), sql_fingerprint=sql.fingerprint,
              description=sql.header["description"], grain=sql.header["grain"], params=sql.header["params"],
              dataset=DATASET, location=settings.location or "auto", schedule=schedule_for(query),
              window={"start": plan.start.isoformat(), "end": plan.end.isoformat()},
              data_through=plan.data_through, incomplete_months=incomplete, empty_months=empty,
              empty_rule=("a month chunk that returned no rows; for this query that is a fault (every day has a "
                          "row), so the chunk is never reused" if query.rows_every_day else
                          "a month chunk that returned no rows; for this query a day can have none (the chain's "
                          "first weeks had days with no transactions), so the chunk is kept and listed here"),
              bytes_processed=sum(job.get("bytes_processed") or 0 for c in chunks_meta for job in c["jobs"]),
              bytes_billed=sum(job.get("bytes_billed") or 0 for c in chunks_meta for job in c["jobs"]),
              incremental_rule="month chunks in data/raw/bigquery/_chunks/<query>/<YYYY-MM>.csv are reused when "
                               "their SQL fingerprint matches, they cover every planned date of the month, and "
                               "their data_through equals the current one or is after every planned date; so a "
                               "day fetched as the window end is re-fetched once after freshness moves past it",
              chunks=chunks_meta)
    return len(rows)


def run_query(client: Any, query: Query, plan: Plan, settings: Settings) -> str | None:
    """Run every uncached chunk of one query, then rebuild its output. Returns an error or None."""
    error = None
    for chunk in plan.chunks_for(query):
        if chunk.cached:
            continue
        try:
            results = [execute(client, job, settings) for job in chunk.jobs]
        except Exception as exc:
            error = f"{query.name} {chunk.month}: {describe(exc)}"
            print(f"[{NAME}] FAILED {error}; finished chunks are kept")
            break
        write_chunk(chunk, [row for rows, _ in results for row in rows], [info for _, info in results],
                    plan.data_through)
        fetched = sum(len(rows) for rows, _ in results)
        print(f"[{NAME}] {query.name} {chunk.month}: {fetched} rows")
        if not fetched:
            # every query returns at least one row per day with data: none at all usually means the SQL no longer
            # matches the live tables (a column that changed type, for example)
            print(f"[{NAME}] WARNING {query.name} {chunk.month}: no rows for {len(chunk.dates)} planned day(s); "
                  f"check {load_sql(query.name).relpath} against the live schema before relying on the output"
                  + ("; the chunk is listed in incomplete_months and re-run next time" if query.rows_every_day
                     else "; the chunk is listed in empty_months"))
    count = write_output(query, plan, settings)
    print(f"[{NAME}] wrote {NAME}/{query.output}.csv ({count} rows)")
    return error


def run(client: Any, settings: Settings, *, yes: bool, today: date, queries: tuple[Query, ...] = QUERIES) -> None:
    base = freshness_base(settings)
    first = freshness_job(settings, today, base)
    first_estimate = dry_run_all(client, [first], settings)
    gate(first_estimate, settings, yes=yes)
    rows, info = execute(client, first, settings)
    freshness = merge_freshness(base, rows[0] if rows else None, first)
    through = last_complete_date(freshness, today).isoformat()
    write_freshness(freshness, info, through, settings, incremental=base is not None)
    print(f"[{NAME}] blocks span {freshness['min_block_date']}..{freshness['max_block_date']} "
          f"(last block {freshness['max_block_timestamp_utc']}); using data through {through}")

    plan = build_plan(queries, freshness, settings, today)
    estimates = {**first_estimate, **dry_run_all(client, plan.jobs(), settings)}
    print(estimate_table(plan, queries, estimates, settings, first))
    gate(estimates, settings, yes=yes)

    errors = [error for query in queries if (error := run_query(client, query, plan, settings))]
    if errors:
        raise RuntimeError(f"{len(errors)} BigQuery query(ies) failed; partial results kept: " + "; ".join(errors))


def dry_run_report(client: Any, settings: Settings, *, today: date,
                   queries: tuple[Query, ...] = QUERIES) -> dict[Job, int]:
    plan = build_plan(queries, cached_freshness(), settings, today)
    first = freshness_job(settings, today, freshness_base(settings))
    estimates = dry_run_all(client, [first, *plan.jobs(include_cached=True)], settings)
    print(estimate_table(plan, queries, estimates, settings, first))
    over_cap = [job.label for job, size in estimates.items() if size > settings.max_bytes]
    if over_cap:
        print(f"[{NAME}] {len(over_cap)} job(s) above the per-job cap would be refused: {', '.join(over_cap[:10])}")
    return estimates


def pull(args: argparse.Namespace) -> None:
    """Google API and auth errors (wrong location, billing, expired login) become one short RuntimeError."""
    settings = Settings.from_env()
    queries = selected_queries(getattr(args, "queries", None))
    today = datetime.now(timezone.utc).date()
    try:
        client = make_client(settings)
        if getattr(args, "dry_run", False):
            dry_run_report(client, settings, today=today, queries=queries)
            return
        run(client, settings, yes=bool(getattr(args, "yes", False)), today=today, queries=queries)
    except BudgetExceeded:
        raise
    except google_errors() as exc:
        raise RuntimeError(f"BigQuery {describe(exc)}{error_hint(exc, settings)}") from None
    except RuntimeError as exc:
        raise RuntimeError(mask(str(exc))) from None
    except Exception as exc:  # requests/HTTP and anything else on the fetch, dry-run or freshness path
        raise RuntimeError(f"BigQuery {describe(exc)}") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--plan", action="store_true", help="print planned jobs offline (no credentials needed)")
    parser.add_argument("--dry-run", action="store_true", help="dry-run every planned job and print the GB table")
    parser.add_argument("--yes", action="store_true", help="run even if the estimate exceeds BQ_BUDGET_GB")
    parser.add_argument("--queries", help=f"comma-separated subset of {','.join(q.name for q in QUERIES)}")
    args = parser.parse_args(argv)
    settings = Settings.from_env()
    if args.plan:
        today = datetime.now(timezone.utc).date()
        queries = selected_queries(args.queries)
        plan = build_plan(queries, cached_freshness(), settings, today)
        print(plan_text(plan, queries, settings, freshness_job(settings, today, freshness_base(settings))))
        return 0
    if not env("GCP_PROJECT"):
        print(f"[{NAME}] set GCP_PROJECT in .env and run `gcloud auth application-default login`")
        return 1
    try:
        pull(args)
    except BudgetExceeded as exc:
        print(f"[{NAME}] {exc}")
        return 2
    except RuntimeError as exc:
        print(f"[{NAME}] {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
