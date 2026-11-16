"""DefiLlama: NEAR Intents daily volume and daily fees in USD (the keyless cross-check for the Explorer API).

Provenance: DefiLlama's dimension API, one request per series,
    https://api.llama.fi/summary/dexs/near-intents?excludeTotalDataChartBreakdown=true
    https://api.llama.fi/summary/fees/near-intents?excludeTotalDataChartBreakdown=true&dataType=dailyFees
(dailyFees is the endpoint's default, sent explicitly so the request is self-describing). `totalDataChart` is a
list of [unix seconds at 00:00 UTC, usd] pairs, one per day. The volume adapter
(github.com/DefiLlama/dimension-adapters, dexs/near-intents) sums `volume_amount_usd` from the Dune table
dune.near.dataset_near_intents_metrics, so DefiLlama and Dune are one source, not two. The fees adapter's own
methodology text and URL are copied from the response into the meta.

Values are written exactly as published: JSON numbers are parsed as Decimal (never through float) and printed as
plain decimals. A null value becomes an empty string. Days DefiLlama does not list are not added; they are listed
in the meta as missing_dates. A point dated on or after the UTC fetch day would be an unfinished day, so it is
dropped and listed in the meta as dropped_unfinished_dates.

Recent finished days can still be partial: a day is computed from upstream tables that may lag and can be refilled
later, and DefiLlama publishes no finality horizon. The PROVISIONAL_DAYS days before the fetch day are kept as published
but listed in the meta as provisional_dates, and latest_day records the last point next to the one before it with
latest_day_provisional, so the build step can annotate or exclude a trailing point instead of reading it as a drop.

Refresh rule: the full series is re-downloaded on every run (two small requests). DefiLlama restates history when
adapters change, so cached rows are never reused. A series that fails keeps its previous file untouched.

Output: data/raw/intents/defillama_volume_daily.csv (date,volume_usd) and
data/raw/intents/defillama_fees_daily.csv (date,fees_usd), sorted by date.
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import urlencode

import requests

from pipeline.config import http_get, write_csv

NAME = "defillama"
REQUIRES: list[str] = []

OUTPUT_SOURCE = "intents"
API = "https://api.llama.fi/summary"
SLUG = "near-intents"
DAY_SECONDS = 86400
PAUSE_SECONDS = 1.0
PROVISIONAL_DAYS = 3
REFRESH_RULE = "full series re-downloaded every run (DefiLlama restates history when adapters change)"
PROVISIONAL_RULE = (f"the {PROVISIONAL_DAYS} UTC days before fetch_day_utc may still be partial or restated upstream; "
                    "values are kept as published (pipeline convention, matching the Explorer pull's open window; "
                    "DefiLlama publishes no finality horizon)")


@dataclass(frozen=True)
class Series:
    name: str
    column: str
    kind: str
    description: str
    upstream: str
    params: dict[str, str] = field(default_factory=dict)

    @property
    def url(self) -> str:
        return f"{API}/{self.kind}/{SLUG}"

    @property
    def source_url(self) -> str:
        return f"{self.url}?{urlencode(self.params)}"


SERIES = (
    Series(
        name="defillama_volume_daily",
        column="volume_usd",
        kind="dexs",
        description="NEAR Intents daily volume in USD per UTC day, as published by DefiLlama",
        upstream="DefiLlama dexs/near-intents adapter: sum(volume_amount_usd) from Dune table "
                 "dune.near.dataset_near_intents_metrics per date_at (not independent of Dune)",
        params={"excludeTotalDataChartBreakdown": "true"},
    ),
    Series(
        name="defillama_fees_daily",
        column="fees_usd",
        kind="fees",
        description="NEAR Intents daily gross fees (protocol plus affiliate) in USD per UTC day, "
                    "as published by DefiLlama",
        upstream="DefiLlama fees/near-intents adapter; see methodology and methodology_url",
        params={"excludeTotalDataChartBreakdown": "true", "dataType": "dailyFees"},
    ),
)


def plain_decimal(value: object) -> str:
    """Render a JSON number (int or Decimal) as a plain decimal string; null becomes ''."""
    if value is None:
        return ""
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise ValueError(f"not a number: {value!r}")
    number = Decimal(value)
    if not number.is_finite():
        raise ValueError(f"not a finite number: {value!r}")
    return format(number, "f")


def utc_day(stamp: object) -> date:
    if isinstance(stamp, bool) or not isinstance(stamp, int) or stamp % DAY_SECONDS:
        raise ValueError(f"not a UTC-midnight unix timestamp: {stamp!r}")
    return datetime.fromtimestamp(stamp, timezone.utc).date()


def decode(text: str) -> dict:
    payload = json.loads(text, parse_float=Decimal)
    if not isinstance(payload, dict):
        raise ValueError(f"expected a JSON object, got {type(payload).__name__}")
    return payload


def parse_chart(payload: dict, column: str, fetch_day: date) -> tuple[list[dict], list[str]]:
    """Turn totalDataChart into date,<column> rows before fetch_day; also return the dropped unfinished dates."""
    chart = payload.get("totalDataChart")
    if not isinstance(chart, list) or not chart:
        raise ValueError("response has no totalDataChart points")
    values: dict[date, str] = {}
    for point in chart:
        if not isinstance(point, list) or len(point) != 2:
            raise ValueError(f"malformed totalDataChart point {point!r}")
        day = utc_day(point[0])
        if day in values:
            raise ValueError(f"duplicate date {day}")
        values[day] = plain_decimal(point[1])
    dropped = [day.isoformat() for day in sorted(values) if day >= fetch_day]
    rows = [{"date": day.isoformat(), column: values[day]} for day in sorted(values) if day < fetch_day]
    if not rows:
        raise ValueError(f"no finished days before {fetch_day}")
    return rows, dropped


def missing_dates(rows: list[dict]) -> list[str]:
    present = {row["date"] for row in rows}
    first, last = date.fromisoformat(rows[0]["date"]), date.fromisoformat(rows[-1]["date"])
    span = (first + timedelta(days=offset) for offset in range((last - first).days + 1))
    return [day.isoformat() for day in span if day.isoformat() not in present]


def summarize(rows: list[dict], column: str, payload: dict) -> dict:
    observed = [Decimal(row[column]) for row in rows if row[column] != ""]
    methodology = payload.get("methodology")
    return {
        "first_date": rows[0]["date"],
        "last_date": rows[-1]["date"],
        "blank_rows": len(rows) - len(observed),
        "missing_dates": missing_dates(rows),
        "sum_of_rows_usd": format(sum(observed, Decimal(0)), "f"),
        "reported_total_all_time_usd": plain_decimal(payload.get("totalAllTime")),
        "protocol": {key: payload[key] for key in ("name", "slug", "defillamaId") if isinstance(payload.get(key), str)},
        "methodology_url": payload.get("methodologyURL") if isinstance(payload.get("methodologyURL"), str) else None,
        "methodology": methodology if isinstance(methodology, dict) and methodology else None,
    }


def provisional(rows: list[dict], column: str, fetch_day: date) -> dict:
    """Flag the trailing days DefiLlama may still restate, and pair the latest point with the one before it."""
    cutoff = (fetch_day - timedelta(days=PROVISIONAL_DAYS)).isoformat()
    dates = [row["date"] for row in rows if row["date"] >= cutoff]
    latest = rows[-1]
    previous = rows[-2] if len(rows) > 1 else {"date": None, column: None}
    return {
        "provisional_rule": PROVISIONAL_RULE,
        "provisional_dates": dates,
        "latest_day_provisional": latest["date"] in dates,
        "latest_day": {"date": latest["date"], "value": latest[column],
                       "previous_date": previous["date"], "previous_value": previous[column]},
    }


def fetch_payload(series: Series) -> dict:
    return decode(http_get(series.url, params=series.params).text)


def pull(args: argparse.Namespace | None = None) -> None:
    fetch_day = datetime.now(timezone.utc).date()
    failures: dict[str, str] = {}
    for index, series in enumerate(SERIES):
        if index:
            time.sleep(PAUSE_SECONDS)
        try:
            payload = fetch_payload(series)
            rows, dropped = parse_chart(payload, series.column, fetch_day)
        except (requests.RequestException, ValueError) as exc:
            failures[series.name] = str(exc)
            print(f"[{NAME}] {series.name} FAILED, cached file kept: {exc}")
            continue
        summary = summarize(rows, series.column, payload)
        flags = provisional(rows, series.column, fetch_day)
        write_csv(OUTPUT_SOURCE, series.name, rows, ["date", series.column], source_url=series.source_url,
                  endpoint=series.url, params=series.params, description=series.description,
                  upstream=series.upstream, refresh=REFRESH_RULE, fetch_day_utc=fetch_day.isoformat(),
                  dropped_unfinished_dates=dropped, **summary, **flags)
        latest = flags["latest_day"]
        print(f"[{NAME}] {series.name}: {len(rows)} rows {summary['first_date']}..{summary['last_date']}, "
              f"{len(summary['missing_dates'])} missing dates, {summary['blank_rows']} blank, "
              f"provisional {','.join(flags['provisional_dates']) or 'none'} "
              f"(latest {latest['date']}={latest['value']} vs {latest['previous_date']}={latest['previous_value']})")
    if failures:
        detail = "; ".join(f"{name}: {error}" for name, error in failures.items())
        raise RuntimeError(f"{len(failures)} of {len(SERIES)} DefiLlama series failed ({detail})")


if __name__ == "__main__":
    pull()
