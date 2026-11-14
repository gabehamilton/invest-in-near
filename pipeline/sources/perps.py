"""NEAR perpetual futures: funding-rate histories, perp candles and the live contract terms, from keyless public data.

No checked venue lists options on NEAR (pipeline/sources/derivatives.py); the listed NEAR derivative is the
perpetual future. A perp's funding rate is what one side pays the other every period (positive: longs pay shorts, on
every venue below), so these files record what a short NEAR perp received or paid, period by period.

Provenance (all keyless: plain GETs, and read-only POSTs to Hyperliquid's public info endpoint; no account, key,
wallet or signature):
- Hyperliquid, POST https://api.hyperliquid.xyz/info (the order book NEAR's own perps front end routes to):
    {"type": "fundingHistory", "coin": "NEAR", "startTime": ms} returns hourly rows {coin, fundingRate, premium,
    time}, at most 500 per call. Paged forward from startTime 0 with startTime = last time + 1 until a page holds
    fewer than 500 rows. The first row is the listing hour, 2023-11-01 17:00 UTC.
    {"type": "candleSnapshot", "req": {"coin": "NEAR", "interval": "1d" | "1h", "startTime": 0, "endTime": now}}
    returns only the latest 5,000 candles of an interval: every day since 2021-02-13, about seven months of hours.
    Candles before the first one with trades carry zero volume and zero trades (prices from before the perp
    listed); they are excluded and counted in the sidecar, so the daily file starts on the first traded day.
    {"type": "metaAndAssetCtxs"} gives the NEAR contract (max leverage, margin table id), its margin table and its
    live context (funding, premium, open interest, mark, oracle, 24h volume).
- Kraken Derivatives (futures.kraken.com), linear perpetual PF_NEARUSD (contract size 1 NEAR, listed 2022-06-13):
    GET /derivatives/api/v4/historicalfundingrates?symbol=PF_NEARUSD returns hourly {timestamp, fundingRate,
    relativeFundingRate} in one call, for a rolling year only. relativeFundingRate is the period's rate as a
    fraction of price (written as funding_rate); fundingRate is the same rate in USD per contract (funding_usd).
    GET /api/charts/v1/trade/PF_NEARUSD/1d?from=<unix seconds> returns {candles: [{time, open, high, low, close,
    volume}], more_candles}; paged with from = last candle + 1 day while more_candles is true. Volume is in
    contracts (1 NEAR each).
    GET /derivatives/api/v3/instruments (the PF_NEARUSD record: margin levels, funding coefficient and cap,
    permitted platforms) and /derivatives/api/v3/tickers/PF_NEARUSD (live mark, index, funding, open interest).
- Binance USD-M futures, NEARUSDT perpetual (listed 2020-10-14), from the bulk file host https://data.binance.vision
  (plain file downloads; Binance's live futures API refuses US addresses, this host does not):
    data/futures/um/monthly/fundingRate/NEARUSDT/NEARUSDT-fundingRate-YYYY-MM.zip (calc_time,
    funding_interval_hours, last_funding_rate) and data/futures/um/monthly/klines/NEARUSDT/1d/NEARUSDT-1d-YYYY-MM.zip,
    plus the daily kline files data/futures/um/daily/klines/NEARUSDT/1d/NEARUSDT-1d-YYYY-MM-DD.zip for days no
    monthly file covers: the current month, last month while its monthly file is not published, and days a monthly
    file lacks (some monthly kline files miss days that the daily files hold). Binance publishes no daily funding
    files, so the funding file ends with the last published month. Every zip is checked against the sha256 in its
    published .CHECKSUM file before it is read (a mismatch is an error); older files have no header row and newer
    ones do, both are read.
- Coinbase Derivatives (Coinbase Derivatives, LLC, a CFTC-regulated exchange; US customers trade it through the futures
  commission merchant Coinbase Financial Markets), the NEAR Perp NER-20DEC30-CDE (contract code NER, 500 NEAR per
  contract, hourly funding, cash-margined in USD; daily candles start 2026-02-28, the product record's new_at day),
  from Coinbase's public Advanced Trade market endpoints (no key):
    GET https://api.coinbase.com/api/v3/brokerage/market/products/NER-20DEC30-CDE returns the product record: contract
    size, funding interval, the current funding rate and its time, open interest and 24h volume in contracts, intraday
    and overnight margin rates for longs and shorts, index, settlement and last price. It is kept whole as a snapshot.
    GET .../candles?granularity=ONE_DAY&start=<unix s>&end=<unix s> returns at most 350 candles, newest first, both
    ends inclusive, and none for days before the listing; paged forward from COINBASE_FROM in windows of
    COINBASE_PAGE_DAYS days. Volume is in contracts of 500 NEAR; the venue reports no quote volume or trade count.
    No keyless funding history exists: the exchange's REST GET https://api.exchange.fairx.net/rest/funding-rate answers
    HTTP 401 ("missing request header: CB-ACCESS-KEY"), and Coinbase's help page says live funding goes out over its
    FIX and SBE market data while historical funding is "available upon request". So the funding rate is recorded only
    as the current value in the snapshot, and no Coinbase funding file is written.

Rows: hourly and 8-hour rows are labelled by `time`, the UTC hour of the settlement (ISO; the venue timestamp
floored to the hour), and keep the venue's millisecond timestamp in time_ms where the venue reports one. Daily
candles cover the UTC day starting at `date`; the unfinished candle of the current day (or hour) is dropped. Numbers
are copied as published (exponent notation is rewritten as a plain decimal); in the terms snapshot, JSON numbers with
a fraction are kept as the published text. Columns a venue does not report stay empty.

Sidecars: source_url, params, fetched_at, pages, rows, first and last key, and response_sha256 = sha256 over the raw
response bodies in request order. Binance sidecars hold zip_sha256 instead (the sha256 of each file, equal to the
checksum Binance publishes for it).

Refresh rule:
- Hyperliquid funding and daily candles, Kraken and Coinbase daily candles: full history re-downloaded every run
  (Hyperliquid calls are paced HL_PAUSE_SECONDS apart, well inside its per-address request budget).
- Hyperliquid hourly candles and Kraken funding come from rolling windows, so their files accumulate: fresh rows
  replace cached rows of the same hour and cached hours older than the window are kept (rows_from_previous_file).
- Binance files do not change once published: a file already archived (listed in the previous sidecar, its period
  present in the cached file) is not downloaded again, nor are the daily files archived for its month. A daily file
  or last month's monthly file that is not published (HTTP 404) is listed in not_published; any other 404 is an
  error.
- On a mid-run error the fetched rows replace matching cached rows, the rest of the cached file is kept, the meta
  records the error (complete: false) and the run fails loudly. A series that fails before any row arrives keeps its
  previous file untouched; the terms snapshot is written only when every venue answered, the Coinbase snapshot only
  when Coinbase answered with the NEAR Perp's record.

Output: data/raw/perps/near_funding_<hyperliquid|kraken>_hourly.csv, near_funding_binance_8h.csv,
near_perp_<hyperliquid|kraken|binance|coinbase>_daily.csv, near_perp_hyperliquid_hourly.csv, near_perp_terms.json and
near_perp_coinbase_terms.json. `python -m pipeline.sources.perps --venue coinbase` fetches the two Coinbase outputs
only.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import time
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import requests

from pipeline.config import http_get, http_post, read_csv, read_meta, utc_now_iso, write_csv, write_json
from pipeline.sources.prices import describe_error, merge_cached, plain, report, utc_day

NAME = "perps"
REQUIRES: list[str] = []

SOURCE = "perps"
HOUR_MS = 3_600_000
MAX_PAGES = 400

HL_URL = "https://api.hyperliquid.xyz/info"
HL_COIN = "NEAR"
HL_PAGE_ROWS = 500
HL_PAUSE_SECONDS = 3.0

KRAKEN = "https://futures.kraken.com"
KRAKEN_SYMBOL = "PF_NEARUSD"
KRAKEN_FUNDING_URL = f"{KRAKEN}/derivatives/api/v4/historicalfundingrates"
KRAKEN_CANDLES_URL = f"{KRAKEN}/api/charts/v1/trade/{KRAKEN_SYMBOL}/1d"
KRAKEN_INSTRUMENTS_URL = f"{KRAKEN}/derivatives/api/v3/instruments"
KRAKEN_TICKER_URL = f"{KRAKEN}/derivatives/api/v3/tickers/{KRAKEN_SYMBOL}"
KRAKEN_FROM = 1_500_000_000   # unix seconds (2017-07-14), before the listing: the first page starts at the listing
KRAKEN_PAUSE_SECONDS = 1.0

BINANCE_HOST = "https://data.binance.vision"
BINANCE_SYMBOL = "NEARUSDT"
BINANCE_FIRST_MONTH = date(2020, 10, 1)
BINANCE_PAUSE_SECONDS = 0.25

COINBASE_MARKET = "https://api.coinbase.com/api/v3/brokerage/market"
COINBASE_PRODUCT = "NER-20DEC30-CDE"
COINBASE_PRODUCT_URL = f"{COINBASE_MARKET}/products/{COINBASE_PRODUCT}"
COINBASE_CANDLES_URL = f"{COINBASE_PRODUCT_URL}/candles"
COINBASE_FROM = date(2026, 2, 1)   # before the listing: a window before it returns no candles
COINBASE_PAGE_DAYS = 300           # days per candles call; the venue serves at most 350 candles per call
COINBASE_PAUSE_SECONDS = 0.5

FUNDING_HL = ["time", "time_ms", "funding_rate", "premium"]
FUNDING_KRAKEN = ["time", "funding_rate", "funding_usd"]
FUNDING_BINANCE = ["time", "time_ms", "interval_hours", "funding_rate"]
CANDLES_DAILY = ["date", "open", "high", "low", "close", "volume_base", "volume_quote", "trades"]
CANDLES_HOURLY = ["time", "open", "high", "low", "close", "volume_base", "volume_quote", "trades"]
CANDLES_COINBASE = ["date", "open", "high", "low", "close", "volume_contracts"]

REFRESH_FULL = ("full history re-downloaded every run; on a mid-run HTTP error the fetched rows replace matching "
                "cached rows and the rest of the cached file is kept (meta complete=false)")
REFRESH_ROLLING = ("the venue serves a rolling window only, so the file accumulates: fresh rows replace cached rows "
                   "with the same key and cached rows older than the window are kept (rows_from_previous_file)")
REFRESH_FILES = ("published files do not change: a file listed in the previous sidecar whose period is in the cached "
                 "file is not downloaded again; new files are checked against Binance's published sha256 and their "
                 "rows replace cached rows with the same key")


class VenueError(Exception):
    """The venue answered HTTP 200 but the body is not the documented payload."""


class ChecksumError(Exception):
    """A downloaded file does not match the sha256 its publisher lists for it."""


@dataclass
class Fetched:
    """The rows of one output and what its sidecar records: pages, the first error, a hash of the raw bodies."""
    rows: list[dict] = field(default_factory=list)
    pages: int = 0
    error: str | None = None
    meta: dict = field(default_factory=dict)
    digest: Any = field(default_factory=hashlib.sha256, repr=False)

    def took(self, body: bytes) -> None:
        self.pages += 1
        self.digest.update(body)

    def sidecar(self) -> dict:
        extra = {"response_sha256": self.digest.hexdigest()} if self.pages and "zip_sha256" not in self.meta else {}
        return {"pages": self.pages, **extra, **self.meta}


def hour_iso(ms: int) -> str:
    """The UTC hour a millisecond timestamp falls in, as ISO text (2023-11-01T17:00:00Z)."""
    return datetime.fromtimestamp(ms // HOUR_MS * 3600, timezone.utc).strftime("%Y-%m-%dT%H:00:00Z")


def iso_hour(text: str) -> str:
    """An ISO timestamp (Z or offset) floored to its UTC hour, in the same form as hour_iso."""
    moment = datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:00:00Z")


def is_404(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    return isinstance(exc, requests.HTTPError) and response is not None and response.status_code == 404


# ------------------------------------------------------------------------------------------------- Hyperliquid

def hl_info(payload: dict, fetched: Fetched) -> Any:
    resp = http_post(HL_URL, payload)
    fetched.took(resp.content)
    return resp.json()


def hl_funding_rows(page: list[dict]) -> list[dict]:
    return [{"time": hour_iso(row["time"]), "time_ms": str(row["time"]), "funding_rate": plain(row["fundingRate"]),
             "premium": plain(row["premium"])} for row in page]


def fetch_hl_funding() -> Fetched:
    """Page hourly funding forward from startTime 0; a page shorter than HL_PAGE_ROWS is the last one."""
    out = Fetched()
    rows: dict[str, dict] = {}
    start = 0
    while out.pages < MAX_PAGES:
        if out.pages:
            time.sleep(HL_PAUSE_SECONDS)
        try:
            page = hl_info({"type": "fundingHistory", "coin": HL_COIN, "startTime": start}, out)
            fresh = hl_funding_rows(page)
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            out.error = f"fundingHistory startTime={start}: {describe_error(exc)}"
            break
        clash = []
        for row in fresh:
            if row["time"] in rows and rows[row["time"]]["time_ms"] != row["time_ms"]:
                clash.append(row["time"])
            rows[row["time"]] = row
        if clash:
            out.error = f"fundingHistory: two settlements in one UTC hour at {clash[:3]}"
            break
        if len(page) < HL_PAGE_ROWS:
            break
        start = page[-1]["time"] + 1
    out.rows = [rows[key] for key in sorted(rows)]
    if not out.rows and out.error is None:
        out.error = "fundingHistory: no rows returned"
    return out


def candle_row(key: str, label: str, open_: Any, high: Any, low: Any, close: Any, volume_base: Any,
               volume_quote: Any = "", trades: Any = "") -> dict:
    return {key: label, "open": plain(open_), "high": plain(high), "low": plain(low), "close": plain(close),
            "volume_base": plain(volume_base), "volume_quote": plain(volume_quote),
            "trades": "" if trades == "" else str(trades)}


def fetch_hl_candles(interval: str, now_ms: int) -> Fetched:
    """One candleSnapshot call (the venue's latest 5,000 candles); unfinished and pre-trading candles are dropped."""
    out = Fetched()
    try:
        candles = sorted(hl_info({"type": "candleSnapshot", "req": {"coin": HL_COIN, "interval": interval,
                                                                    "startTime": 0, "endTime": now_ms}}, out),
                         key=lambda c: c["t"])
        finished = [c for c in candles if c["T"] < now_ms]
        first_traded = next((i for i, c in enumerate(finished) if int(c["n"]) > 0), len(finished))
        backfill, traded = finished[:first_traded], finished[first_traded:]
        key, label = ("date", lambda c: utc_day(c["t"])) if interval == "1d" else ("time", lambda c: hour_iso(c["t"]))
        out.rows = [candle_row(key, label(c), c["o"], c["h"], c["l"], c["c"], c["v"], trades=c["n"]) for c in traded]
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        out.error = f"candleSnapshot {interval}: {describe_error(exc)}"
        return out
    unfinished = len(candles) - len(finished)
    out.meta["excluded"] = (
        (f"{len(backfill)} candle(s) {label(backfill[0])}..{label(backfill[-1])} with zero volume and zero trades, "
         "before the first traded candle; " if backfill else "")
        + f"{unfinished} unfinished candle(s) at fetch time")
    if not out.rows:
        out.error = f"candleSnapshot {interval}: no traded candles returned"
    return out


# ------------------------------------------------------------------------------------------------------ Kraken

def kraken_get(url: str, params: dict | None, fetched: Fetched) -> Any:
    resp = http_get(url, params=params)
    fetched.took(resp.content)
    return resp.json(parse_float=Decimal)


def fetch_kraken_funding() -> Fetched:
    out = Fetched()
    try:
        body = kraken_get(KRAKEN_FUNDING_URL, {"symbol": KRAKEN_SYMBOL}, out)
        if body.get("result") != "success":
            raise VenueError(json.dumps(body, default=str)[:2000])
        rows = {iso_hour(r["timestamp"]): {"time": iso_hour(r["timestamp"]),
                                           "funding_rate": plain(r["relativeFundingRate"]),
                                           "funding_usd": plain(r["fundingRate"])} for r in body["rates"]}
    except (requests.RequestException, VenueError, ValueError, KeyError, TypeError) as exc:
        out.error = f"historicalfundingrates: {describe_error(exc)}"
        return out
    out.rows = [rows[key] for key in sorted(rows)]
    if not out.rows:
        out.error = "historicalfundingrates: no rows returned"
    return out


def fetch_kraken_candles(today: date) -> Fetched:
    """Page daily trade candles forward from KRAKEN_FROM while the venue says more_candles."""
    out = Fetched()
    rows: dict[str, dict] = {}
    start = KRAKEN_FROM
    while out.pages < MAX_PAGES:
        if out.pages:
            time.sleep(KRAKEN_PAUSE_SECONDS)
        try:
            body = kraken_get(KRAKEN_CANDLES_URL, {"from": start}, out)
            candles = body["candles"]
            rows.update({r["date"]: r for r in (
                candle_row("date", utc_day(c["time"]), c["open"], c["high"], c["low"], c["close"], c["volume"])
                for c in candles)})
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            out.error = f"charts trade 1d from={start}: {describe_error(exc)}"
            break
        if not body.get("more_candles") or not candles:
            break
        start = candles[-1]["time"] // 1000 + 86_400
    out.rows = [rows[d] for d in sorted(rows) if d < today.isoformat()]
    out.meta["excluded"] = f"unfinished candle for {today} (UTC)"
    if not out.rows and out.error is None:
        out.error = "charts trade 1d: no complete daily candles returned"
    return out


# ----------------------------------------------------------------------------------------------------- Binance

BINANCE_KINDS = {
    "funding": {"name": "near_funding_binance_8h", "key": "time", "fields": FUNDING_BINANCE,
                "monthly": "data/futures/um/monthly/fundingRate/{s}/{s}-fundingRate-{p}.zip", "daily": None},
    "klines": {"name": "near_perp_binance_daily", "key": "date", "fields": CANDLES_DAILY,
               "monthly": "data/futures/um/monthly/klines/{s}/1d/{s}-1d-{p}.zip",
               "daily": "data/futures/um/daily/klines/{s}/1d/{s}-1d-{p}.zip"},
}


def binance_rows(kind: str, records: list[list[str]]) -> list[dict]:
    if kind == "funding":
        return [{"time": hour_iso(int(r[0])), "time_ms": r[0], "interval_hours": r[1], "funding_rate": plain(r[2])}
                for r in records]
    return [candle_row("date", utc_day(int(r[0])), r[1], r[2], r[3], r[4], r[5], r[7], r[8]) for r in records]


def binance_file(path: str, fetched: Fetched) -> list[list[str]]:
    """Download one bulk zip, check it against its published .CHECKSUM, return its CSV records (header dropped)."""
    url = f"{BINANCE_HOST}/{path}"
    body = http_get(url).content
    time.sleep(BINANCE_PAUSE_SECONDS)
    published = http_get(f"{url}.CHECKSUM").text.split()[0].lower()
    sha = hashlib.sha256(body).hexdigest()
    if sha != published:
        raise ChecksumError(f"sha256 {sha} differs from the published {published}")
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        members = [n for n in archive.namelist() if n.endswith(".csv")]
        if len(members) != 1:
            raise VenueError(f"expected one CSV in the zip, found {archive.namelist()}")
        text = archive.read(members[0]).decode("utf-8")
    records = [r for r in csv.reader(io.StringIO(text)) if r]
    if records and not records[0][0].strip().isdigit():
        records = records[1:]
    fetched.pages += 1
    fetched.meta["zip_sha256"][path.rsplit("/", 1)[1]] = sha
    return records


def month_starts(first: date, last: date) -> list[date]:
    months, month = [], first.replace(day=1)
    while month <= last:
        months.append(month)
        month = (month + timedelta(days=32)).replace(day=1)
    return months


def fetch_binance(kind: str, today: date) -> Fetched:
    """Every monthly file from the listing month to last month, then daily files for days no monthly file covers."""
    spec = BINANCE_KINDS[kind]
    out = Fetched(meta={"zip_sha256": {}, "not_published": [], "files_reused": 0})
    archived = read_meta(SOURCE, spec["name"]).get("zip_sha256") or {}
    cached = {row[spec["key"]][:10] for row in read_csv(SOURCE, spec["name"])}
    cached_months = {d[:7] for d in cached}
    current = today.replace(day=1)
    last_month = (current - timedelta(days=1)).replace(day=1)
    rows: dict[str, dict] = {}

    def days(first: date, end: date, skip: set[str] = frozenset()) -> list[tuple[str, str, bool]]:
        """Daily-file steps for first <= day < end, skipping days already held."""
        steps = [("daily", (first + timedelta(days=i)).isoformat(), True) for i in range((end - first).days)]
        return [step for step in steps if step[1] not in skip]

    def holes(month: date, end: date, held: set[str]) -> list[tuple[str, str, bool]]:
        """Days of a month that its monthly file lacks (in the listing month: from the first day it holds)."""
        if not held:
            return days(month, end)
        first = date.fromisoformat(min(held)) if month == BINANCE_FIRST_MONTH.replace(day=1) else month
        return days(first, end, held)

    # (file kind, period, a 404 means "not published"). Daily files are queued right after a monthly file for the
    # days it lacks, or for its whole month when last month's file is not published yet; then the current month.
    plan = [("monthly", m.strftime("%Y-%m"), m == last_month) for m in month_starts(BINANCE_FIRST_MONTH, last_month)]
    plan += days(current, today) if spec["daily"] else []
    path, index = "", 0
    try:
        while index < len(plan):
            period_kind, period, may_be_unpublished = plan[index]
            index += 1
            path = spec[period_kind].format(s=BINANCE_SYMBOL, p=period)
            filename = path.rsplit("/", 1)[1]
            month = date.fromisoformat(f"{period[:7]}-01")
            month_end = min((month + timedelta(days=32)).replace(day=1), today)
            fills_holes = period_kind == "monthly" and spec["daily"] is not None
            if filename in archived and period in (cached_months if period_kind == "monthly" else cached):
                out.meta["zip_sha256"][filename] = archived[filename]
                out.meta["files_reused"] += 1
                if fills_holes:
                    # the daily files archived for this month's holes stay listed; days still missing are retried
                    prefix = spec["daily"].format(s=BINANCE_SYMBOL, p=period).rsplit("/", 1)[1].removesuffix(".zip")
                    carried = {n: s for n, s in archived.items() if n.startswith(f"{prefix}-")}
                    out.meta["zip_sha256"].update(carried)
                    out.meta["files_reused"] += len(carried)
                    plan[index:index] = holes(month, month_end, {d for d in cached if d.startswith(period)})
                continue
            time.sleep(BINANCE_PAUSE_SECONDS)
            try:
                records = binance_file(path, out)
            except requests.HTTPError as exc:
                if not (may_be_unpublished and is_404(exc)):
                    raise
                out.meta["not_published"].append(filename)
                if fills_holes:
                    plan[index:index] = days(month, month_end)
                continue
            fresh = binance_rows(kind, records)
            rows.update({row[spec["key"]]: row for row in fresh})
            if fills_holes:
                plan[index:index] = holes(month, month_end, {row["date"] for row in fresh})
    except (requests.RequestException, ChecksumError, VenueError, zipfile.BadZipFile, ValueError, IndexError) as exc:
        out.error = f"{path}: {describe_error(exc)}"
    out.rows = [rows[key] for key in sorted(rows)]
    return out


# ---------------------------------------------------------------------------------------------------- Coinbase

def unix_day(day: date) -> int:
    """Unix seconds at the start of a UTC day."""
    return int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())


def fetch_coinbase_candles(today: date) -> Fetched:
    """Page daily candles forward in windows of COINBASE_PAGE_DAYS days (both ends inclusive) from COINBASE_FROM
    through today; the unfinished candle of today is dropped."""
    out = Fetched()
    rows: dict[str, dict] = {}
    start = COINBASE_FROM
    while start <= today and out.pages < MAX_PAGES:
        if out.pages:
            time.sleep(COINBASE_PAUSE_SECONDS)
        end = start + timedelta(days=COINBASE_PAGE_DAYS - 1)
        params = {"granularity": "ONE_DAY", "start": str(unix_day(start)), "end": str(unix_day(end))}
        try:
            resp = http_get(COINBASE_CANDLES_URL, params=params)
            out.took(resp.content)
            for c in json.loads(resp.text, parse_float=str)["candles"]:
                day = utc_day(int(c["start"]) * 1000)
                rows[day] = {"date": day, "open": plain(c["open"]), "high": plain(c["high"]), "low": plain(c["low"]),
                             "close": plain(c["close"]), "volume_contracts": plain(c["volume"])}
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            out.error = f"candles ONE_DAY start={start}: {describe_error(exc)}"
            break
        start = end + timedelta(days=1)
    out.rows = [rows[d] for d in sorted(rows) if d < today.isoformat()]
    out.meta["excluded"] = f"unfinished candle for {today} (UTC)"
    if not out.rows and out.error is None:
        out.error = "candles ONE_DAY: no complete daily candles returned"
    return out


def fetch_coinbase_terms() -> tuple[dict | None, str | None]:
    """The NEAR Perp's product record as Coinbase publishes it now (contract size, funding, open interest, margin)."""
    try:
        resp = http_get(COINBASE_PRODUCT_URL)
        record = json.loads(resp.text, parse_float=str)
        if record.get("product_id") != COINBASE_PRODUCT or not record["future_product_details"].get("contract_size"):
            raise VenueError(resp.text.strip()[:2000])
    except (requests.RequestException, VenueError, ValueError, KeyError, TypeError, AttributeError) as exc:
        return None, f"product {COINBASE_PRODUCT}: {describe_error(exc)}"
    return {"checked_at": utc_now_iso(), "venue": "Coinbase Derivatives", "product_id": COINBASE_PRODUCT,
            "source_url": COINBASE_PRODUCT_URL, "product": record,
            "response_sha256": hashlib.sha256(resp.content).hexdigest()}, None


# ------------------------------------------------------------------------------------------------ terms snapshot

def fetch_terms() -> tuple[dict, list[str]]:
    """The NEAR contract as each venue describes it now: leverage, margin tiers, funding, open interest."""
    venues: list[dict] = []
    errors: list[str] = []
    try:
        resp = http_post(HL_URL, {"type": "metaAndAssetCtxs"})
        meta, contexts = json.loads(resp.text, parse_float=str)
        index = next(i for i, asset in enumerate(meta["universe"]) if asset["name"] == HL_COIN)
        asset = meta["universe"][index]
        table = next((t[1] for t in meta.get("marginTables", []) if t[0] == asset.get("marginTableId")), None)
        venues.append({"venue": "Hyperliquid", "symbol": HL_COIN, "source_url": HL_URL,
                       "request": {"type": "metaAndAssetCtxs"}, "contract": asset, "margin_table": table,
                       "context": contexts[index], "response_sha256": hashlib.sha256(resp.content).hexdigest()})
    except (requests.RequestException, ValueError, KeyError, TypeError, IndexError, StopIteration) as exc:
        errors.append(f"Hyperliquid metaAndAssetCtxs: {describe_error(exc)}")
    time.sleep(KRAKEN_PAUSE_SECONDS)
    try:
        instruments = http_get(KRAKEN_INSTRUMENTS_URL)
        record = next(i for i in json.loads(instruments.text, parse_float=str)["instruments"]
                      if i.get("symbol") == KRAKEN_SYMBOL)
        time.sleep(KRAKEN_PAUSE_SECONDS)
        ticker = http_get(KRAKEN_TICKER_URL)
        venues.append({"venue": "Kraken Derivatives", "symbol": KRAKEN_SYMBOL,
                       "source_url": f"{KRAKEN_INSTRUMENTS_URL} {KRAKEN_TICKER_URL}", "contract": record,
                       "context": json.loads(ticker.text, parse_float=str)["ticker"],
                       "response_sha256": [hashlib.sha256(instruments.content).hexdigest(),
                                           hashlib.sha256(ticker.content).hexdigest()]})
    except (requests.RequestException, ValueError, KeyError, TypeError, StopIteration) as exc:
        errors.append(f"Kraken instruments/ticker: {describe_error(exc)}")
    return {"checked_at": utc_now_iso(), "venues": venues}, errors


# ------------------------------------------------------------------------------------------------------- output

def store(name: str, key: str, fetched: Fetched, fieldnames: list[str], *, accumulate: bool,
          **meta: Any) -> list[dict] | None:
    """Write one series with provenance. Accumulating series (and any series after an error) are merged over the
    cached file; a series that failed before any row arrived leaves the cached file untouched (returns None)."""
    if fetched.error and not fetched.rows:
        return None
    rows, kept = fetched.rows, 0
    if accumulate or fetched.error:
        rows, kept = merge_cached(SOURCE, name, key, fetched.rows)
    if not rows:
        return None
    meta[f"first_{key}"], meta[f"last_{key}"] = rows[0][key], rows[-1][key]
    write_csv(SOURCE, name, rows, fieldnames, complete=fetched.error is None, error=fetched.error,
              rows_from_previous_file=kept, **fetched.sidecar(), **meta)
    return rows


def run_series(failures: list[str], name: str, key: str, fetched: Fetched, fieldnames: list[str], accumulate: bool,
               **meta: Any) -> None:
    """Store one series, print its status line and collect its failure."""
    saved = store(name, key, fetched, fieldnames, accumulate=accumulate, **meta)
    if saved is None and fetched.error is None:
        fetched.error = "no rows returned and no cached file"
    report(NAME, name, saved, key, fetched.error, failures)


def pull_coinbase(failures: list[str], today: date) -> None:
    """The Coinbase Derivatives NEAR Perp: daily candles since the listing and the product snapshot."""
    run_series(failures, "near_perp_coinbase_daily", "date", fetch_coinbase_candles(today), CANDLES_COINBASE, False,
               source_url=COINBASE_CANDLES_URL,
               params={"granularity": "ONE_DAY", "from": COINBASE_FROM.isoformat(), "page_days": COINBASE_PAGE_DAYS},
               description="Coinbase Derivatives NEAR Perp (NER-20DEC30-CDE) daily candles (UTC days); "
                           "volume_contracts is the number of contracts traded, 500 NEAR each; the venue reports no "
                           "quote volume or trade count",
               refresh=REFRESH_FULL)
    time.sleep(COINBASE_PAUSE_SECONDS)
    terms, error = fetch_coinbase_terms()
    if error:
        print(f"[{NAME}] near_perp_coinbase_terms FAILED, cached file kept: {error}")
        failures.append(f"near_perp_coinbase_terms: {error}")
        return
    write_json(SOURCE, "near_perp_coinbase_terms", terms, rows=1, source_url=COINBASE_PRODUCT_URL,
               description="The Coinbase Derivatives NEAR Perp (NER-20DEC30-CDE) product record at fetch time, kept "
                           "whole as published: contract size (NEAR per contract), funding interval, the current "
                           "funding rate and its time, open interest and 24h volume in contracts, intraday and "
                           "overnight margin rates for longs and shorts (fractions of notional), index, settlement "
                           "and last price. Coinbase publishes no keyless funding history, so funding appears only "
                           "as the current rate",
               refresh="snapshot replaced every run; the cached file is kept when the product does not answer")
    print(f"[{NAME}] near_perp_coinbase_terms: {COINBASE_PRODUCT} at {terms['checked_at']}")


def pull(args: argparse.Namespace | None = None) -> None:
    now = datetime.now(timezone.utc)
    today, now_ms = now.date(), int(now.timestamp() * 1000)
    failures: list[str] = []

    def run(name: str, key: str, fetched: Fetched, fieldnames: list[str], accumulate: bool, **meta: Any) -> None:
        run_series(failures, name, key, fetched, fieldnames, accumulate, **meta)

    run("near_funding_hyperliquid_hourly", "time", fetch_hl_funding(), FUNDING_HL, False,
        source_url=HL_URL, params={"type": "fundingHistory", "coin": HL_COIN, "startTime": 0,
                                   "page_rows": HL_PAGE_ROWS},
        description="Hyperliquid NEAR perpetual, hourly funding rate (fraction of notional for the hour; positive: "
                    "longs pay shorts) and premium, as published; time is the UTC hour, time_ms the venue timestamp",
        refresh=REFRESH_FULL)
    for interval, name, fields, accumulate in (("1d", "near_perp_hyperliquid_daily", CANDLES_DAILY, False),
                                               ("1h", "near_perp_hyperliquid_hourly", CANDLES_HOURLY, True)):
        time.sleep(HL_PAUSE_SECONDS)
        unit = "UTC days" if interval == "1d" else "UTC hours"
        run(name, "date" if interval == "1d" else "time", fetch_hl_candles(interval, now_ms), fields, accumulate,
            source_url=HL_URL, params={"type": "candleSnapshot", "coin": HL_COIN, "interval": interval,
                                       "startTime": 0, "endTime": now_ms},
            description=f"Hyperliquid NEAR perpetual candles ({unit}, traded candles only); volume_base in NEAR, "
                        "trades is the number of trades; the venue reports no quote volume",
            refresh=REFRESH_ROLLING if accumulate else REFRESH_FULL)

    run("near_funding_kraken_hourly", "time", fetch_kraken_funding(), FUNDING_KRAKEN, True,
        source_url=KRAKEN_FUNDING_URL, params={"symbol": KRAKEN_SYMBOL},
        description="Kraken Derivatives PF_NEARUSD hourly funding: funding_rate is relativeFundingRate (fraction of "
                    "price for the hour), funding_usd is fundingRate (USD per contract of 1 NEAR for the hour); "
                    "positive: longs pay shorts",
        refresh=REFRESH_ROLLING)
    time.sleep(KRAKEN_PAUSE_SECONDS)
    run("near_perp_kraken_daily", "date", fetch_kraken_candles(today), CANDLES_DAILY, False,
        source_url=KRAKEN_CANDLES_URL, params={"from": KRAKEN_FROM},
        description="Kraken Derivatives PF_NEARUSD daily trade candles (UTC days); volume_base in contracts of "
                    "1 NEAR; the venue reports no quote volume or trade count",
        refresh=REFRESH_FULL)

    for kind, description in (
            ("funding", "Binance USD-M NEARUSDT perpetual funding, one row per settlement (interval_hours apart); "
                        "funding_rate is last_funding_rate as published; positive: longs pay shorts"),
            ("klines", "Binance USD-M NEARUSDT perpetual daily candles (UTC days); volume_base in NEAR, "
                       "volume_quote in USDT, trades is the trade count")):
        spec = BINANCE_KINDS[kind]
        run(spec["name"], spec["key"], fetch_binance(kind, today), spec["fields"], True,
            source_url=f"{BINANCE_HOST}/{spec['monthly'].format(s=BINANCE_SYMBOL, p='YYYY-MM')}"
                       + (f" {BINANCE_HOST}/{spec['daily'].format(s=BINANCE_SYMBOL, p='YYYY-MM-DD')}"
                          if spec["daily"] else ""),
            params={"symbol": BINANCE_SYMBOL, "first_month": BINANCE_FIRST_MONTH.strftime("%Y-%m")},
            description=description, refresh=REFRESH_FILES)

    pull_coinbase(failures, today)

    time.sleep(KRAKEN_PAUSE_SECONDS)
    terms, errors = fetch_terms()
    if errors:
        print(f"[{NAME}] near_perp_terms FAILED, cached file kept: {'; '.join(errors)}")
        failures.extend(f"near_perp_terms: {e}" for e in errors)
    else:
        write_json(SOURCE, "near_perp_terms", terms, rows=len(terms["venues"]),
                   source_url=" ".join(v["source_url"] for v in terms["venues"]),
                   description="The NEAR perpetual as each venue describes it at fetch time: contract record "
                               "(leverage, margin tiers, funding parameters) and live context (funding, open "
                               "interest, mark, index); one entry per venue, numbers with a fraction kept as the "
                               "published text")
        print(f"[{NAME}] near_perp_terms: {len(terms['venues'])} venues at {terms['checked_at']}")

    if failures:
        raise RuntimeError(f"{NAME}: {len(failures)} series failed ({'; '.join(failures)})")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="NEAR perpetual futures from keyless public data")
    parser.add_argument("--venue", choices=["all", "coinbase"], default="all",
                        help="coinbase: only the Coinbase Derivatives NEAR Perp outputs (default: every venue)")
    if parser.parse_args(argv).venue == "all":
        pull()
        return
    failures: list[str] = []
    pull_coinbase(failures, datetime.now(timezone.utc).date())
    if failures:
        raise RuntimeError(f"{NAME}: {len(failures)} series failed ({'; '.join(failures)})")


if __name__ == "__main__":
    main()
