"""FRED: US policy rates, FOMC projections, US inflation indices, M2, the dollar-euro rate, the euro short-term rate
(€STR, the desk simulation's euro cash rate) and the one-year Treasury yield (its term check).

Provenance: the keyless FRED graph CSV endpoint, one request per series,
    https://fred.stlouisfed.org/graph/fredgraph.csv?id=<SERIES>
(a comma-separated id list returns a ZIP and the JSON API needs a key, so neither is used). The body is
`observation_date,<SERIES>` followed by one row per observation. FRED marks a missing observation with '.'
(daily series on bank holidays) or an empty cell (CPIAUCSL, CPIAUCNS and CPILFESL 2025-10: BLS collected no October 2025
CPI during the federal shutdown). Both become empty strings and the row is kept, so gaps stay visible
downstream; nothing is dropped, zeroed or interpolated.

Refresh rule: the full history of every series is re-downloaded on each run. It is one small request per series, FRED
revises history in place (seasonal factors, M2 benchmarks) and this endpoint ignores vintage_date, so cached
rows cannot be trusted to match the current vintage. A series that fails keeps its previous file untouched.
Rows dated after the UTC fetch day (scheduled target-range values) are kept as published and counted in the
meta as `rows_after_fetch_date`.

Output: data/raw/fred/<SERIES>.csv with columns date,value, sorted by date.
"""

from __future__ import annotations

import argparse
import csv
import io
import time
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

import requests

from pipeline.config import http_get, write_csv

NAME = "fred"
REQUIRES: list[str] = []

URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
FIELDNAMES = ["date", "value"]
MISSING_MARKERS = (".", "")
PAUSE_SECONDS = 1.0
REFRESH_RULE = "full history re-downloaded every run (FRED revises past values; vintage_date is ignored here)"

SERIES: dict[str, str] = {
    "DFF": "Federal funds effective rate, daily including weekends, percent",
    "EFFR": "Effective federal funds rate (NY Fed), business days, percent",
    "DFEDTARU": "Federal funds target range upper limit, daily, percent",
    "CPIAUCSL": "CPI for all urban consumers, all items, US city average, monthly, SA, index 1982-1984=100",
    "CPIAUCNS": "CPI for all urban consumers, all items, US city average, monthly, not seasonally adjusted, "
                "index 1982-1984=100 (cross-check X4 against BIS, which publishes unadjusted CPI)",
    "CPILFESL": "CPI for all urban consumers, all items less food and energy, monthly, SA, index 1982-1984=100",
    "PCEPI": "Personal consumption expenditures chain-type price index, monthly, SA, index 2017=100",
    "M2SL": "M2 money stock, monthly, SA, billions of US dollars",
    "DEXUSEU": "US dollars to one euro, noon buying rates in New York, business days",
    "FEDTARMD": "FOMC Summary of Economic Projections, fed funds rate, median, annual (by projection year), percent",
    "FEDTARMDLR": "FOMC Summary of Economic Projections, fed funds rate, median, longer run, percent",
    "ECBESTRVOLWGTTRMDMNRT": "Euro short-term rate (ECB, via FRED), volume-weighted trimmed mean, daily, percent",
    "DGS1": "Market yield on US Treasury securities at 1-year constant maturity, quoted on an investment basis, "
            "daily, percent",
}


def source_url(series_id: str) -> str:
    return f"{URL}?id={series_id}"


def plain_decimal(text: str) -> str:
    """Return a finite number as written, rewriting exponent notation as a plain decimal."""
    try:
        number = Decimal(text)
    except InvalidOperation:
        raise ValueError(f"not a number: {text!r}") from None
    if not number.is_finite():
        raise ValueError(f"not a finite number: {text!r}")
    return format(number, "f") if "e" in text.lower() else text


def _iso_date(text: str) -> str:
    if len(text) != 10 or date.fromisoformat(text).isoformat() != text:
        raise ValueError(f"not an ISO date: {text!r}")
    return text


def parse_series(text: str, series_id: str) -> list[dict]:
    """Parse a fredgraph.csv body into date,value rows sorted by date; '.' and '' become empty values."""
    reader = csv.reader(io.StringIO(text.lstrip("﻿"), newline=""))
    header = next(reader, None)
    if header is None or len(header) != 2 or header[1].strip() != series_id:
        raise ValueError(f"{series_id}: unexpected header {header!r}")
    values: dict[str, str] = {}
    for line in reader:
        if not line:
            continue
        if len(line) != 2:
            raise ValueError(f"{series_id}: malformed row {line!r}")
        day, raw = _iso_date(line[0].strip()), line[1].strip()
        if day in values:
            raise ValueError(f"{series_id}: duplicate date {day}")
        values[day] = "" if raw in MISSING_MARKERS else plain_decimal(raw)
    if not values:
        raise ValueError(f"{series_id}: no observations")
    return [{"date": day, "value": values[day]} for day in sorted(values)]


def summarize(rows: list[dict], fetch_day: str) -> dict:
    observed = [row["date"] for row in rows if row["value"] != ""]
    return {
        "first_date": rows[0]["date"],
        "last_date": rows[-1]["date"],
        "last_observed_date": observed[-1] if observed else None,
        "blank_rows": len(rows) - len(observed),
        "rows_after_fetch_date": sum(row["date"] > fetch_day for row in rows),
    }


def fetch_series(series_id: str) -> tuple[list[dict], str | None]:
    resp = http_get(URL, params={"id": series_id})
    return parse_series(resp.content.decode("utf-8-sig"), series_id), resp.headers.get("Last-Modified")


def selected(args: argparse.Namespace | None) -> dict[str, str]:
    """Every series, or only those named by --series (so adding a series never rewrites the cached others)."""
    names = [n for n in (getattr(args, "series", None) or "").split(",") if n]
    unknown = [n for n in names if n not in SERIES]
    if unknown:
        raise ValueError(f"unknown FRED series {', '.join(unknown)}; the series are {', '.join(SERIES)}")
    return {n: SERIES[n] for n in names} if names else dict(SERIES)


def pull(args: argparse.Namespace | None = None) -> None:
    fetch_day = datetime.now(timezone.utc).date().isoformat()
    failures: dict[str, str] = {}
    series = selected(args)
    for index, (series_id, description) in enumerate(series.items()):
        if index:
            time.sleep(PAUSE_SECONDS)
        try:
            rows, last_modified = fetch_series(series_id)
        except (requests.RequestException, ValueError) as exc:
            failures[series_id] = str(exc)
            print(f"[{NAME}] {series_id} FAILED, cached file kept: {exc}")
            continue
        summary = summarize(rows, fetch_day)
        write_csv(NAME, series_id, rows, FIELDNAMES, source_url=source_url(series_id), series_id=series_id,
                  description=description, missing_markers=list(MISSING_MARKERS), refresh=REFRESH_RULE,
                  http_last_modified=last_modified, fetch_day_utc=fetch_day, **summary)
        print(f"[{NAME}] {series_id}: {len(rows)} rows {summary['first_date']}..{summary['last_date']}, "
              f"{summary['blank_rows']} blank")
    if failures:
        detail = "; ".join(f"{series_id}: {error}" for series_id, error in failures.items())
        raise RuntimeError(f"{len(failures)} of {len(series)} FRED series failed ({detail})")


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description="Pull FRED series into data/raw/fred/.")
    cli.add_argument("--series", help="comma-separated series ids to pull (default: every series)")
    pull(cli.parse_args())
