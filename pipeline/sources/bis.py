"""BIS: monthly central-bank policy rates and CPI inflation (year-on-year %) for the comparison economies.

Provenance: the keyless BIS SDMX v2 data API in CSV form, one request per dataflow,
    https://stats.bis.org/api/v2/data/dataflow/BIS/<FLOW>/1.0/<KEY>?format=csv&startPeriod=2015-01
- WS_CBPOL, key M.<areas>: central bank policy rate, monthly, end of period, percent per annum. The instrument
  behind each economy's rate (e.g. the mid-point of the Fed target range) is the source COMPILATION field,
  copied into the meta as `series_attributes`.
- WS_LONG_CPI, key M.<areas>.771: consumer prices, change on a year earlier in percent (UNIT_MEASURE 771).
Areas are BIS REF_AREA codes: US, XM (euro area), GB, JP, CH, CA, AU, TR, BR, IN, AR.
- WS_CBPOL, key M.CN+HK: the same policy-rate series for China (one-year Loan Prime Rate) and Hong Kong (HKMA base
  rate), the two most-traded currencies outside the comparison economies that publish a policy rate. Kept in its
  own file so the comparison panel's file is unchanged.

Rows whose OBS_VALUE is NaN or empty are dropped and listed in the meta as `dropped_nan` (e.g. US CPI 2025-10,
which BLS never collected because of the 2025 federal shutdown), so a missing period is absent, never invented.
Periods BIS does not publish at all (Argentina has no policy rate since 2025-07-10) are simply absent too;
per-area first/last periods are in the meta as `coverage`.

Refresh rule: every flow is re-downloaded in full on every run (small requests; BIS revises recent periods).
A flow that fails keeps its previous file untouched.

Output: data/raw/bis/policy_rates_monthly.csv, data/raw/bis/cpi_yoy_monthly.csv and
data/raw/bis/policy_rates_monthly_cn_hk.csv, columns area,period,value, sorted by period then area.
"""

from __future__ import annotations

import argparse
import csv
import io
import re
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

import requests

from pipeline.config import http_get, write_csv

NAME = "bis"
REQUIRES: list[str] = []

BASE_URL = "https://stats.bis.org/api/v2/data/dataflow/BIS"
AREAS = ["US", "XM", "GB", "JP", "CH", "CA", "AU", "TR", "BR", "IN", "AR"]
TRADED_AREAS = ["CN", "HK"]
START_PERIOD = "2015-01"
FIELDNAMES = ["area", "period", "value"]
NAN_MARKERS = ("nan", "")
PAUSE_SECONDS = 1.0
REFRESH_RULE = f"full re-download from {START_PERIOD} every run (BIS revises recent periods)"
ATTRIBUTE_COLUMNS = ("TITLE", "TITLE_TS", "COMPILATION", "SOURCE_REF", "UNIT_MEASURE", "UNIT_MULT")
REQUIRED_COLUMNS = {"FREQ", "REF_AREA", "TIME_PERIOD", "OBS_VALUE"}
_PERIOD = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


@dataclass(frozen=True)
class Flow:
    name: str
    flow: str
    key: str
    unit_measure: str | None
    description: str
    areas: tuple[str, ...] = tuple(AREAS)


FLOWS = [
    Flow("policy_rates_monthly", "WS_CBPOL", "M." + "+".join(AREAS), None,
         "Central bank policy rates, monthly, end of period, percent per annum (BIS WS_CBPOL)"),
    Flow("cpi_yoy_monthly", "WS_LONG_CPI", "M." + "+".join(AREAS) + ".771", "771",
         "Consumer prices, year-on-year change in percent, monthly (BIS WS_LONG_CPI, unit 771)"),
    Flow("policy_rates_monthly_cn_hk", "WS_CBPOL", "M." + "+".join(TRADED_AREAS), None,
         "Central bank policy rates for China and Hong Kong, monthly, end of period, percent per annum (BIS WS_CBPOL)",
         tuple(TRADED_AREAS)),
]


def endpoint(flow: Flow) -> str:
    return f"{BASE_URL}/{flow.flow}/1.0/{flow.key}"


def source_url(flow: Flow) -> str:
    return f"{endpoint(flow)}?format=csv&startPeriod={START_PERIOD}"


def plain_decimal(text: str) -> str:
    """Return a finite number as written, rewriting exponent notation as a plain decimal."""
    try:
        number = Decimal(text)
    except InvalidOperation:
        raise ValueError(f"not a number: {text!r}") from None
    if not number.is_finite():
        raise ValueError(f"not a finite number: {text!r}")
    return format(number, "f") if "e" in text.lower() else text


def read_records(text: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿"), newline=""))
    missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
    if missing:
        raise ValueError(f"missing columns {sorted(missing)} in header {reader.fieldnames!r}")
    return list(reader)


def to_rows(records: list[dict], flow: Flow) -> tuple[list[dict], list[str]]:
    """Validate SDMX records and return (area,period,value rows sorted by period then area, dropped NaN keys)."""
    values: dict[tuple[str, str], str] = {}
    seen: set[tuple[str, str]] = set()
    dropped: list[tuple[str, str]] = []
    for record in records:
        area, period, raw = record["REF_AREA"], record["TIME_PERIOD"], record["OBS_VALUE"].strip()
        if record["FREQ"] != "M" or area not in flow.areas:
            raise ValueError(f"{flow.flow}: unexpected series {record['FREQ']}.{area}")
        if flow.unit_measure and record.get("UNIT_MEASURE") != flow.unit_measure:
            raise ValueError(f"{flow.flow}: unexpected UNIT_MEASURE {record.get('UNIT_MEASURE')!r} for {area}")
        if not _PERIOD.match(period):
            raise ValueError(f"{flow.flow}: unexpected TIME_PERIOD {period!r} for {area}")
        if (period, area) in seen:
            raise ValueError(f"{flow.flow}: duplicate observation {area} {period}")
        seen.add((period, area))
        if raw.lower() in NAN_MARKERS:
            dropped.append((period, area))
            continue
        values[(period, area)] = plain_decimal(raw)
    rows = [{"area": area, "period": period, "value": values[(period, area)]} for period, area in sorted(values)]
    return rows, [f"{area} {period}" for period, area in sorted(dropped)]


def series_attributes(records: list[dict]) -> dict[str, dict[str, str]]:
    """Per-area descriptive attributes (title, compilation note, source) as published by BIS."""
    attributes: dict[str, dict[str, str]] = {}
    for record in records:
        found = {col: record[col].strip() for col in ATTRIBUTE_COLUMNS if (record.get(col) or "").strip()}
        attributes.setdefault(record["REF_AREA"], {}).update(found)
    return dict(sorted(attributes.items()))


def coverage(rows: list[dict]) -> dict[str, dict]:
    spans: dict[str, dict] = {}
    for row in rows:
        span = spans.setdefault(row["area"], {"first_period": row["period"], "rows": 0})
        span["last_period"] = row["period"]
        span["rows"] += 1
    return dict(sorted(spans.items()))


def fetch_flow(flow: Flow) -> list[dict]:
    resp = http_get(endpoint(flow), params={"format": "csv", "startPeriod": START_PERIOD})
    return read_records(resp.content.decode("utf-8-sig"))


def pull_flow(flow: Flow) -> None:
    """Download one flow and write its CSV and meta; raises on a failed request or an invalid response."""
    records = fetch_flow(flow)
    rows, dropped = to_rows(records, flow)
    if not rows:
        raise ValueError(f"{flow.flow}: no observations")
    spans = coverage(rows)
    write_csv(NAME, flow.name, rows, FIELDNAMES, source_url=source_url(flow), description=flow.description,
              flow=flow.flow, key=flow.key, params={"format": "csv", "startPeriod": START_PERIOD},
              areas_requested=list(flow.areas), areas_without_data=[area for area in flow.areas if area not in spans],
              coverage=spans, dropped_nan=dropped, refresh=REFRESH_RULE,
              series_attributes=series_attributes(records))
    print(f"[{NAME}] {flow.name}: {len(rows)} rows {rows[0]['period']}..{rows[-1]['period']}, "
          f"{len(spans)} areas, {len(dropped)} NaN dropped")


def pull(args: argparse.Namespace | None = None) -> None:
    failures: dict[str, str] = {}
    for index, flow in enumerate(FLOWS):
        if index:
            time.sleep(PAUSE_SECONDS)
        try:
            pull_flow(flow)
        except (requests.RequestException, ValueError) as exc:
            failures[flow.name] = str(exc)
            print(f"[{NAME}] {flow.name} FAILED, cached file kept: {exc}")
    if failures:
        detail = "; ".join(f"{name}: {error}" for name, error in failures.items())
        raise RuntimeError(f"{len(failures)} of {len(FLOWS)} BIS flows failed ({detail})")


if __name__ == "__main__":
    pull()
