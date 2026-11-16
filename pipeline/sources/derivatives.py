"""NEAR derivatives evidence: which venues list NEAR options, and BTC/ETH implied vol.

Provenance (all keyless public endpoints):
- Options venue check, one request per venue, written to market/options_venues.json:
    Deribit  https://www.deribit.com/api/v2/public/get_instruments?currency=any&kind=option -> base_currency
    OKX      https://www.okx.com/api/v5/public/underlying?instType=OPTION -> uly (e.g. BTC-USD)
    Aevo     https://api.aevo.xyz/markets?instrument_type=OPTION -> underlying_asset
    Bybit    https://api.bybit.com/v5/market/instruments-info?category=option&baseCoin=NEAR -> baseCoin
    Binance  https://eapi.binance.com/eapi/v1/exchangeInfo -> optionContracts[].baseAsset
  option_underlyings lists the venue's names as returned; has_near_options is true when any of them has base
  NEAR. When a venue cannot be queried (geoblock, outage, error code) ok is false, has_near_options and
  option_underlyings are null (unknown, not "no"), and error holds the response body exactly as received
  (cut at 4000 characters). Bybit and Binance block US addresses (CloudFront 403, Binance 451), so their
  parsers follow the documented payloads and are only exercised from an unblocked location.
- Deribit DVOL (30-day implied volatility index, annualized percent) for BTC and ETH via
  public/get_volatility_index_data, resolution 1D. A call returns up to 1000 daily candles ending at
  end_timestamp plus a `continuation` timestamp; pages are walked backwards until continuation is null.
  `close` is the daily candle close; the current day's unfinished candle is excluded.

Refresh rule: DVOL histories are re-downloaded on every run (a few small requests, paced). If a request fails
mid-way the rows already fetched replace the matching cached rows, the rest of the cached file is kept, the meta
records the error (complete: false) and the run fails loudly. The venue check never fails the run: its errors are data.

Output: data/raw/market/options_venues.json and dvol_deribit_<btc|eth>_daily.csv (date,close).
"""

from __future__ import annotations

import argparse
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

import requests

from pipeline.config import http_get, utc_now_iso, write_json
from pipeline.sources.prices import day_ms, describe_error, plain, report, save, utc_day

NAME = "derivatives"
REQUIRES: list[str] = []

SOURCE = "market"
DERIBIT = "https://www.deribit.com/api/v2/public"
DVOL_CURRENCIES = ["BTC", "ETH"]
PAUSE_SECONDS = 0.25
MAX_PAGES = 200
ERROR_CHARS = 4000
REFRESH_RULE = ("full history re-downloaded every run; on a mid-run HTTP error the fetched rows replace "
                "matching cached rows and the rest of the cached file is kept (meta complete=false)")


class VenueError(Exception):
    """The venue answered HTTP 200 but with an error code in the body."""


def _deribit_underlyings(body: dict) -> list[str]:
    return sorted({item["base_currency"] for item in body["result"]})


def _okx_underlyings(body: dict) -> list[str]:
    if body.get("code") != "0":
        raise VenueError(body.get("msg"))
    return sorted({uly for group in body["data"] for uly in group})


def _aevo_underlyings(body: list) -> list[str]:
    return sorted({market["underlying_asset"] for market in body})


def _bybit_underlyings(body: dict) -> list[str]:
    if body.get("retCode") != 0:
        raise VenueError(body.get("retMsg"))
    return sorted({item["baseCoin"] for item in body["result"]["list"]})


def _binance_underlyings(body: dict) -> list[str]:
    if "optionContracts" not in body:
        raise VenueError(body.get("msg"))
    return sorted({contract["baseAsset"] for contract in body["optionContracts"]})


@dataclass(frozen=True)
class Venue:
    name: str
    url: str
    params: dict
    underlyings: Callable[[Any], list[str]]


VENUES = [
    Venue("Deribit", f"{DERIBIT}/get_instruments", {"currency": "any", "kind": "option"}, _deribit_underlyings),
    Venue("OKX", "https://www.okx.com/api/v5/public/underlying", {"instType": "OPTION"}, _okx_underlyings),
    Venue("Aevo", "https://api.aevo.xyz/markets", {"instrument_type": "OPTION"}, _aevo_underlyings),
    Venue("Bybit", "https://api.bybit.com/v5/market/instruments-info", {"category": "option", "baseCoin": "NEAR"},
          _bybit_underlyings),
    Venue("Binance", "https://eapi.binance.com/eapi/v1/exchangeInfo", {}, _binance_underlyings),
]


def endpoint(venue: Venue) -> str:
    return requests.Request("GET", venue.url, params=venue.params).prepare().url


def verbatim(text: str) -> str:
    return text if len(text) <= ERROR_CHARS else text[:ERROR_CHARS] + f"...[cut, {len(text)} chars total]"


def is_near(underlying: str) -> bool:
    return re.split(r"[-_/]", underlying, maxsplit=1)[0].upper() == "NEAR"


def check_venue(venue: Venue) -> dict:
    record = {"venue": venue.name, "endpoint": endpoint(venue), "ok": False, "http_status": None,
              "has_near_options": None, "option_underlyings": None, "error": None}
    try:
        resp = http_get(venue.url, params=venue.params)
    except requests.HTTPError as exc:
        record.update(http_status=exc.response.status_code, error=verbatim(exc.response.text.strip()))
        return record
    except requests.RequestException as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"
        return record
    record["http_status"] = resp.status_code
    try:
        underlyings = venue.underlyings(resp.json())
    except (VenueError, ValueError, KeyError, TypeError):
        record["error"] = verbatim(resp.text.strip())
        return record
    record.update(ok=True, option_underlyings=underlyings, has_near_options=any(map(is_near, underlyings)))
    return record


def write_venue_check() -> list[dict]:
    """Check every venue and write options_venues.json; rows counts venues, source_url lists endpoints."""
    checked_at = utc_now_iso()
    venues = [check_venue(venue) for venue in VENUES]
    write_json(SOURCE, "options_venues", {"checked_at": checked_at, "venues": venues}, rows=len(venues),
               source_url=" ".join(endpoint(venue) for venue in VENUES),
               description="Which venues list options on NEAR, one row per venue; source_url is the "
                           "space-separated endpoint of each venue in row order; null has_near_options means "
                           "the venue could not be queried from this machine (error is the response body as "
                           "received)")
    return venues


def deribit(method: str, **params: Any) -> Any:
    return http_get(f"{DERIBIT}/{method}", params=params).json(parse_float=Decimal)["result"]


def fetch_dvol(currency: str, today: date) -> tuple[list[dict], int, str | None]:
    """Walk 1D DVOL candles backwards from the end of yesterday until continuation is null."""
    closes: dict[int, Decimal] = {}
    end, pages, error = day_ms(today) - 1, 0, None
    while pages < MAX_PAGES:
        time.sleep(PAUSE_SECONDS)
        try:
            result = deribit("get_volatility_index_data", currency=currency, start_timestamp=0,
                             end_timestamp=end, resolution="1D")
        except (requests.RequestException, ValueError, KeyError) as exc:
            error = f"end_timestamp={end}: {describe_error(exc)}"
            break
        pages += 1
        new = {row[0]: row[4] for row in result["data"] if row[0] not in closes}
        closes.update(new)
        end = result.get("continuation")
        if not new or end is None:
            break
    rows = [{"date": utc_day(ts), "close": plain(close)} for ts, close in sorted(closes.items())
            if utc_day(ts) < today.isoformat()]
    return rows, pages, error or (None if rows else f"{currency}: no DVOL candles returned")


def pull(args: argparse.Namespace | None = None) -> None:
    today = datetime.now(timezone.utc).date()
    failures: list[str] = []

    for record in write_venue_check():
        print(f"[{NAME}] options {record['venue']}: http {record['http_status']}, "
              f"has_near_options={record['has_near_options']}, underlyings={record['option_underlyings']}"
              + (f", error={record['error'][:120]!r}" if record["error"] else ""))

    for currency in DVOL_CURRENCIES:
        rows, pages, error = fetch_dvol(currency, today)
        name = f"dvol_deribit_{currency.lower()}_daily"
        saved = save(SOURCE, name, "date", rows, ["date", "close"], error,
                     source_url=f"{DERIBIT}/get_volatility_index_data",
                     params={"currency": currency, "resolution": "1D", "start_timestamp": 0},
                     description=f"Deribit DVOL {currency} (30-day implied volatility index, annualized "
                                 "percent), daily candle close", pages=pages, refresh=REFRESH_RULE,
                     excluded=f"unfinished candle for {today} (UTC)")
        report(NAME, name, saved, "date", error, failures)

    if failures:
        raise RuntimeError(f"{NAME}: {len(failures)} series failed ({'; '.join(failures)})")


if __name__ == "__main__":
    pull()
