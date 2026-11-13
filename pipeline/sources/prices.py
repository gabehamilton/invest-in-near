"""Daily OHLCV candles for NEAR, BTC and ETH from keyless public exchange APIs.

Provenance:
- Binance spot klines from https://data-api.binance.vision/api/v3/klines (interval 1d), Binance's public
  market-data host. Unlike api.binance.com it answers from the US. Each symbol is paged 1000 candles per
  request from startTime=0, so every series begins at its first Binance candle (NEARUSDT: 2020-10-14).
  Kline fields used: open time, open, high, low, close, base-asset volume, quote-asset volume, copied as
  published (exponent notation would be rewritten as a plain decimal).
- Coinbase Exchange NEAR-USD candles from https://api.exchange.coinbase.com/products/NEAR-USD/candles
  (granularity 86400), from 2022-09-01, the first day Coinbase returns. A request may span at most 300
  candles, so the range is walked in 300-day windows (start and end are both inclusive). Coinbase rows are
  [time, low, high, open, close, volume] with base-asset volume only, so volume_quote is left empty.
  Days Coinbase does not return stay absent; nothing is filled or interpolated.

Every candle covers the UTC day starting at `date`. The candle for the current UTC day is unfinished and is
dropped, so the last row is yesterday's close.

Refresh rule: full history is re-downloaded on every run (under 20 requests; candles are cheap and
exchanges occasionally restate them). If a request fails mid-way, the rows already fetched replace the
matching dates of the previous file, the remaining dates keep their cached rows, the meta records the error
(`complete: false`), and the run fails loudly. A series that fails before any row arrives keeps its previous
file untouched.

Output: data/raw/market/<SYMBOL>_binance_daily.csv for NEARUSDT, BTCUSDT and ETHUSDT, and
data/raw/market/near_usd_coinbase_daily.csv, columns date,open,high,low,close,volume_base,volume_quote,
sorted by date.
"""

from __future__ import annotations

import argparse
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import requests

from pipeline.config import http_get, read_csv, write_csv

NAME = "prices"
REQUIRES: list[str] = []

SOURCE = "market"
FIELDNAMES = ["date", "open", "high", "low", "close", "volume_base", "volume_quote"]

BINANCE_URL = "https://data-api.binance.vision/api/v3/klines"
BINANCE_SYMBOLS = ["NEARUSDT", "BTCUSDT", "ETHUSDT"]
BINANCE_LIMIT = 1000
BINANCE_PAUSE_SECONDS = 0.5

COINBASE_URL = "https://api.exchange.coinbase.com/products/NEAR-USD/candles"
COINBASE_START = date(2022, 9, 1)
COINBASE_WINDOW_DAYS = 300
COINBASE_PAUSE_SECONDS = 0.5

MAX_PAGES = 100
REFRESH_RULE = ("full history re-downloaded every run; on a mid-run HTTP error the fetched rows replace "
                "matching cached dates and the rest of the cached file is kept (meta complete=false)")


def plain(value: Any) -> str:
    """Render a number as a plain decimal string; None and '' stay empty."""
    if value is None or value == "":
        return ""
    number = value if isinstance(value, Decimal) else Decimal(str(value))
    if not number.is_finite():
        raise ValueError(f"not a finite number: {value!r}")
    return format(number, "f")


def utc_day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).date().isoformat()


def day_ms(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp() * 1000)


def describe_error(exc: Exception) -> str:
    """HTTP status plus the response body as sent, or the exception text."""
    response = getattr(exc, "response", None)
    if response is not None:
        return f"HTTP {response.status_code}: {response.text.strip()[:2000]}"
    return f"{type(exc).__name__}: {exc}"


def merge_cached(source: str, name: str, key: str, fresh: list[dict]) -> tuple[list[dict], int]:
    """Fresh rows win; cached rows survive only for keys the fresh fetch did not return."""
    merged = {row[key]: row for row in read_csv(source, name)}
    fresh_keys = {row[key] for row in fresh}
    kept = sum(1 for k in merged if k not in fresh_keys)
    merged.update({row[key]: row for row in fresh})
    return [merged[k] for k in sorted(merged)], kept


def save(source: str, name: str, key: str, rows: list[dict], fieldnames: list[str], error: str | None,
         **meta: Any) -> list[dict] | None:
    """Write rows with provenance. On error merge them over the cached file; if nothing arrived, leave the cached
    file untouched and return None."""
    if not rows:
        return None
    cached_kept = 0
    if error:
        rows, cached_kept = merge_cached(source, name, key, rows)
    meta[f"first_{key}"], meta[f"last_{key}"] = rows[0][key], rows[-1][key]
    write_csv(source, name, rows, fieldnames, complete=error is None, error=error,
              rows_from_previous_file=cached_kept, **meta)
    return rows


def report(module: str, name: str, rows: list[dict] | None, key: str, error: str | None,
           failures: list[str]) -> None:
    """Print one status line per output and collect failures for the end-of-run error."""
    if rows is None:
        print(f"[{module}] {name} FAILED, cached file kept: {error}")
    else:
        partial = f" PARTIAL, cached rows kept for the rest: {error}" if error else ""
        print(f"[{module}] {name}: {len(rows)} rows {rows[0][key]}..{rows[-1][key]}{partial}")
    if error:
        failures.append(f"{name}: {error}")


def binance_rows(klines: list[list]) -> list[dict]:
    return [{"date": utc_day(k[0]), "open": plain(k[1]), "high": plain(k[2]), "low": plain(k[3]),
             "close": plain(k[4]), "volume_base": plain(k[5]), "volume_quote": plain(k[7])} for k in klines]


def fetch_binance(symbol: str, today: date) -> tuple[list[dict], int, str | None]:
    """Page klines forward from startTime=0; return complete-day rows, page count and any error."""
    rows: dict[str, dict] = {}
    start, pages, error = 0, 0, None
    while pages < MAX_PAGES:
        if pages:
            time.sleep(BINANCE_PAUSE_SECONDS)
        params = {"symbol": symbol, "interval": "1d", "startTime": start, "limit": BINANCE_LIMIT}
        try:
            klines = http_get(BINANCE_URL, params=params).json()
        except (requests.RequestException, ValueError) as exc:
            error = f"{symbol} startTime={start}: {describe_error(exc)}"
            break
        pages += 1
        rows.update({row["date"]: row for row in binance_rows(klines)})
        if len(klines) < BINANCE_LIMIT:
            break
        start = klines[-1][0] + 86_400_000
    complete = [rows[d] for d in sorted(rows) if d < today.isoformat()]
    return complete, pages, error or (None if complete else f"{symbol}: no complete daily candles returned")


def coinbase_rows(candles: list[list]) -> list[dict]:
    return [{"date": utc_day(c[0] * 1000), "open": plain(c[3]), "high": plain(c[2]), "low": plain(c[1]),
             "close": plain(c[4]), "volume_base": plain(c[5]), "volume_quote": ""} for c in candles]


def coinbase_windows(start: date, last: date) -> list[tuple[date, date]]:
    """Inclusive [start, end] day windows of at most COINBASE_WINDOW_DAYS candles each."""
    windows = []
    while start <= last:
        end = min(start + timedelta(days=COINBASE_WINDOW_DAYS - 1), last)
        windows.append((start, end))
        start = end + timedelta(days=1)
    return windows


def fetch_coinbase(today: date) -> tuple[list[dict], int, str | None]:
    rows: dict[str, dict] = {}
    pages, error = 0, None
    for index, (start, end) in enumerate(coinbase_windows(COINBASE_START, today - timedelta(days=1))):
        if index:
            time.sleep(COINBASE_PAUSE_SECONDS)
        params = {"granularity": 86400, "start": f"{start.isoformat()}T00:00:00Z",
                  "end": f"{end.isoformat()}T00:00:00Z"}
        try:
            candles = http_get(COINBASE_URL, params=params).json(parse_float=Decimal)
        except (requests.RequestException, ValueError) as exc:
            error = f"window {start}..{end}: {describe_error(exc)}"
            break
        pages += 1
        rows.update({row["date"]: row for row in coinbase_rows(candles)})
    complete = [rows[d] for d in sorted(rows) if d < today.isoformat()]
    return complete, pages, error or (None if complete else "NEAR-USD: no complete daily candles returned")


def pull(args: argparse.Namespace | None = None) -> None:
    today = datetime.now(timezone.utc).date()
    failures: list[str] = []
    for index, symbol in enumerate(BINANCE_SYMBOLS):
        if index:
            time.sleep(BINANCE_PAUSE_SECONDS)
        rows, pages, error = fetch_binance(symbol, today)
        saved = save(SOURCE, f"{symbol}_binance_daily", "date", rows, FIELDNAMES, error,
                     source_url=BINANCE_URL, description=f"Binance spot {symbol} daily candles (UTC days)",
                     params={"symbol": symbol, "interval": "1d", "startTime": 0, "limit": BINANCE_LIMIT},
                     pages=pages, refresh=REFRESH_RULE, excluded=f"unfinished candle for {today} (UTC)")
        report(NAME, f"{symbol}_binance_daily", saved, "date", error, failures)
    rows, pages, error = fetch_coinbase(today)
    saved = save(SOURCE, "near_usd_coinbase_daily", "date", rows, FIELDNAMES, error,
                 source_url=COINBASE_URL, description="Coinbase Exchange NEAR-USD daily candles (UTC days); "
                 "Coinbase reports base volume only, so volume_quote is empty",
                 params={"granularity": 86400, "start": COINBASE_START.isoformat(),
                         "window_days": COINBASE_WINDOW_DAYS},
                 pages=pages, refresh=REFRESH_RULE, excluded=f"unfinished candle for {today} (UTC)")
    report(NAME, "near_usd_coinbase_daily", saved, "date", error, failures)
    if failures:
        raise RuntimeError(f"{NAME}: {len(failures)} series failed ({'; '.join(failures)})")


if __name__ == "__main__":
    pull()
