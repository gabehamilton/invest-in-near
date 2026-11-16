"""Fiat money panel: broad money for the ten most-traded currencies.

The panel (USD EUR JPY GBP CNY CHF AUD CAD HKD SGD) is the BIS Triennial Survey 2025 top ten, ranked from
data/raw/bis_fx/ by pipeline/sources/bis_fx.py; this module fetches the money series for those ten. Every
endpoint is public and keyless:

    FRED graph CSV (US)            fred.stlouisfed.org/graph/fredgraph.csv?id=<SERIES>
    ECB data portal (euro area)    data-api.ecb.europa.eu/service/data/BSI/<KEY>?format=csvdata
    Bank of Japan time-series API  www.stat-search.boj.or.jp/api/v1/getDataCode
    Bank of England IADB           www.bankofengland.co.uk/boeapps/database/_iadb-FromShowColumns.asp
    OECD SDMX                      sdmx.oecd.org/public/rest/data/<FLOW>/<KEY>
    SNB data portal                data.snb.ch/api/cube/<CUBE>/data/csv/en
    RBA statistical tables         www.rba.gov.au/statistics/tables/csv/d3-data.csv
    Bank of Canada Valet           www.bankofcanada.ca/valet/observations/<SERIES>/json
    HKMA open API (tried first)    api.hkma.gov.hk/public/market-data-and-statistics/monthly-statistical-bulletin/
                                   money/supply-adjusted
    HKMA statistical bulletin      www.hkma.gov.hk/media/eng/doc/market-data-and-statistics/.../T0101.xls
    SingStat Table Builder         tablebuilder.singstat.gov.sg/api/table/tabledata/<TABLE>

Output: data/raw/money/<ccy>/<field>.csv with columns period,value. Periods are months, YYYY-MM. A blank
value means the publisher lists the period without a number; a period it does not list has no row. Values are
kept exactly as published (no unit conversion, no rebasing, no fill), so the sidecar's `units` string and
`seasonal_adjustment` say how to read them. Unit strings are
"<ISO currency> <million|billion|100 million>", optionally followed by a qualifier in brackets, or "index
(...)" or "percent"; pipeline.models.load.unit_scale() maps them to multipliers in code.

Fields: money (primary aggregate), money_alt (AUD and CAD: the aggregate the OECD mirror reports, for
cross-check X9) and eur/money_growth_published (ECB annual growth rate, the headline).
data/raw/money/oecd/monagg.csv is the OECD broad-money mirror (cross-check X9). Responses that are the only copy of a publisher file (the Bank of England CSV, the HKMA
workbook) are also kept unchanged in data/raw/money/<ccy>/_raw/ (build.py skips underscore paths).

Refresh rule: history from START_MONTH is re-downloaded on every run (publishers revise in place). Requests go
out one at a time with a pause per host (PAUSE_SECONDS, longer for OECD and the Bank of England) and http_get's
retry on 429/5xx. The Bank of England gets exactly one plain-curl request (its edge blocks bursts and scripted
clients) and no retry. A series that fails keeps its previous csv untouched; its sidecar gains `error` /
`error_at` (so the status board shows the cache in use) while `fetched_at` keeps the old vintage's time. A
series that fails with no previous vintage gets a sidecar only, with `complete: false` and a `null_reason` for
the models.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable
from urllib.parse import urlencode, urlparse

import requests

from pipeline.config import RAW_DIR, ROOT, http_get, utc_now_iso, write_csv, write_meta

NAME = "money"
REQUIRES: list[str] = []

START_MONTH = "2015-01"
FIELDNAMES = ["period", "value"]
PAUSE_SECONDS = 1.0
HOST_PAUSE = {"sdmx.oecd.org": 5.0, "www.bankofengland.co.uk": 30.0}
REFRESH_RULE = (f"history from {START_MONTH} re-downloaded every run; "
                "a failed fetch keeps the previous file")
CURRENCIES = ("usd", "eur", "jpy", "gbp", "cny", "chf", "aud", "cad", "hkd", "sgd")

_MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
MONTH_ABBR = {m: i for i, m in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}


# =========================================================================== series specs

@dataclass(frozen=True)
class Spec:
    ccy: str
    field: str
    publisher: str            # key into FETCHERS
    series_id: str            # the publisher's own id (series code, SDMX key, table + row)
    description: str
    units: str                # "<ISO> <scale>[ (qualifier)]", "index (...)" or "percent"
    frequency: str            # "monthly"
    seasonal_adjustment: str  # as the publisher states it
    role: str                 # primary | alt | headline | level | context
    aggregate: str = ""       # money aggregate name (M2, M3, ...)
    basis: str = ""           # how a value relates to time (stock at end of month, monthly average, flow, ...)
    params: tuple = ()        # publisher-specific (key, value) pairs
    notes: tuple = ()         # caveats copied to the sidecar

    @property
    def name(self) -> str:
        return f"{self.ccy}/{self.field}"

    def param(self, key: str, default=None):
        return dict(self.params).get(key, default)


def S(ccy, fld, publisher, series_id, description, units, frequency, sa, role, **kw) -> Spec:
    kw["params"] = tuple(sorted((kw.pop("params", None) or {}).items()))
    kw["notes"] = tuple(kw.pop("notes", ()) or ())
    return Spec(ccy, fld, publisher, series_id, description, units, frequency, sa, role, **kw)


M = "monthly"
EOP = "stock at the end of the month"

SPECS: list[Spec] = [
    # ---------------------------------------------------------------- USD (FRED mirror of Fed H.6)
    S("usd", "money", "fred", "M2SL", "M2 money stock (Federal Reserve H.6), monthly average of daily figures",
      "USD billion", M, "seasonally adjusted", "primary", aggregate="M2",
      basis="monthly average of daily levels", notes=["Same series as data/raw/fred/M2SL.csv (FRED mirror of H.6)."]),
    # ---------------------------------------------------------------- EUR (ECB BSI)
    S("eur", "money", "ecb", "BSI.M.U2.Y.V.M30.X.I.U2.2300.Z01.E",
      "Monetary aggregate M3, euro area (changing composition), index of notional stocks (adjusted for "
      "reclassifications, revaluations and composition changes such as Bulgaria joining on 2026-01-01)",
      "index (December 2010=100; ECB index of notional stocks)", M, "working-day and seasonally adjusted", "primary",
      aggregate="M3", basis="index of end-of-month notional stocks",
      params={"dataset": "BSI", "key": "M.U2.Y.V.M30.X.I.U2.2300.Z01.E", "UNIT_INDEX_BASE": "Dec10 = 100",
              "UNIT_MULT": "0"},
      notes=["Method: the ECB's own published index of notional stocks (not reconstructed from flows)."]),
    S("eur", "money_growth_published", "ecb", "BSI.M.U2.Y.V.M30.X.I.U2.2300.Z01.A",
      "Monetary aggregate M3, euro area, annual growth rate as published by the ECB (break-adjusted)",
      "percent", M, "working-day and seasonally adjusted", "headline", aggregate="M3",
      basis="change on the same month a year earlier",
      params={"dataset": "BSI", "key": "M.U2.Y.V.M30.X.I.U2.2300.Z01.A", "UNIT": "PCCH"}),
    # ---------------------------------------------------------------- JPY (BoJ)
    S("jpy", "money", "boj", "MAM1XAM3M3MO", "M3, average amounts outstanding (Bank of Japan money stock)",
      "JPY 100 million", M, "seasonally adjusted", "primary", aggregate="M3",
      basis="monthly average of daily levels", params={"db": "MD02", "unit": "100 million yen"}),
    # ---------------------------------------------------------------- GBP (BoE, OECD)
    S("gbp", "money", "boe", "LPMAUYN",
      "M4: monthly amounts outstanding of M4 (monetary financial institutions' sterling M4 liabilities to the "
      "private sector), Bank of England", "GBP million", M, "seasonally adjusted", "primary", aggregate="M4",
      basis=EOP, notes=["M4ex (the Bank's preferred measure) is not used: its IADB code is unconfirmed."]),
    # ---------------------------------------------------------------- CNY (OECD mirror of PBoC)
    S("cny", "money", "oecd_monagg", "DF_MONAGG/CHN.M.MABM.XDC",
      "M2, end of month (People's Bank of China, via the OECD monetary aggregates dataflow)", "CNY million", M,
      "not seasonally adjusted", "primary", aggregate="M2", basis=EOP, params={"area": "CHN", "adjustment": "N"},
      notes=["Equals the PBoC's published M2 (checked against the PBoC Q3 2026 report); the PBoC "
             "publishes HTML only."]),
    # ---------------------------------------------------------------- CHF (SNB)
    S("chf", "money", "snb", "snbmonagg/B.GM3", "Monetary aggregate M3, level (Swiss National Bank)",
      "CHF million", M, "not stated (treated as not seasonally adjusted)", "primary", aggregate="M3", basis=EOP,
      params={"cube": "snbmonagg", "dims": "D0(B),D1(GM2,GM3)", "match": ("B", "GM3"),
              "labels": ("Level", "Monetary aggregate M3")},
      notes=["The SNB cube carries no unit field: CHF million is inferred from the level of currency in "
             "circulation."]),
    # ---------------------------------------------------------------- AUD (RBA)
    S("aud", "money", "rba", "DMABMS", "Broad money (RBA table D3)", "AUD billion", M, "seasonally adjusted",
      "primary", aggregate="Broad money", basis=EOP, params={"units_published": "$ billion"}),
    S("aud", "money_alt", "rba", "DMAM3S", "M3 (RBA table D3)", "AUD billion", M, "seasonally adjusted", "alt",
      aggregate="M3", basis=EOP, params={"units_published": "$ billion"}),
    # ---------------------------------------------------------------- CAD (Bank of Canada)
    S("cad", "money", "valet", "V41552801", "M2++ (gross), Bank of Canada", "CAD million", M,
      "seasonally adjusted", "primary", aggregate="M2++ (gross)", basis=EOP),
    S("cad", "money_alt", "valet", "V41552794", "M3 (gross), Bank of Canada", "CAD million", M,
      "seasonally adjusted", "alt", aggregate="M3 (gross)", basis=EOP),
    # ---------------------------------------------------------------- HKD (HKMA)
    S("hkd", "money", "hkma", "T0101/M2 HK$", "Hong Kong dollar M2, adjusted to include foreign currency swap "
      "deposits (HKMA Monthly Statistical Bulletin table 1.1)", "HKD million", M, "not seasonally adjusted",
      "primary", aggregate="HK$ M2", basis=EOP, params={"aggregate": "M2"},
      notes=["The bulletin carries only the latest monthly rows plus year-end rows (stored as December); the "
             "HKMA API that has full history refused this network (HTTP 405).",
             "HK$ column, not total M2 (which mixes in foreign-currency deposits)."]),
    # ---------------------------------------------------------------- SGD (SingStat, MAS data)
    S("sgd", "money", "singstat", "M701111/M2", "Money supply M2, end of period (MAS, via SingStat Table Builder)",
      "SGD million", M, "not seasonally adjusted", "primary", aggregate="M2", basis=EOP,
      params={"table": "M701111", "row": "M2", "uom": "Million Dollars"},
      notes=["Series break 2021-07: since then the series covers Singapore-dollar activity only, and this "
             "table starts there."]),
]

# OECD broad-money mirror for cross-check X9: one request, every area.
OECD_MONAGG_AREAS = ("USA", "EA20", "JPN", "GBR", "CHN", "CHE", "AUS", "CAN")


# =========================================================================== numbers and periods

def plain_decimal(text: str) -> str:
    """A finite number as written (thousands separators removed, exponent notation made plain)."""
    cleaned = str(text).strip().replace(",", "")
    try:
        number = Decimal(cleaned)
    except InvalidOperation:
        raise ValueError(f"not a number: {text!r}") from None
    if not number.is_finite():
        raise ValueError(f"not a finite number: {text!r}")
    return format(number, "f") if "e" in cleaned.lower() else cleaned


def month(year: int, mon: int) -> str:
    if not 1 <= mon <= 12:
        raise ValueError(f"bad month {mon}")
    return f"{year:04d}-{mon:02d}"


def period_start(period: str) -> str:
    """ISO date of the first day of a YYYY-MM period (the FRED dating convention)."""
    if _MONTH.match(period):
        return f"{period}-01"
    raise ValueError(f"not a period: {period!r}")


def finish(pairs: list[tuple[str, str]], spec: Spec) -> list[dict]:
    """Validate (period, value) pairs for one series and return period,value rows sorted, from the start period."""
    start = START_MONTH
    seen: dict[str, str] = {}
    for period, value in pairs:
        if not _MONTH.match(period):
            raise ValueError(f"{spec.name}: unexpected period {period!r} for a monthly series")
        if period in seen:
            if seen[period] == value:
                continue
            raise ValueError(f"{spec.name}: two values for {period}: {seen[period]!r} and {value!r}")
        seen[period] = value
    rows = [{"period": p, "value": seen[p]} for p in sorted(seen) if p >= start]
    if not any(r["value"] != "" for r in rows):
        raise ValueError(f"{spec.name}: no observations from {start}")
    return rows


def summarize(rows: list[dict]) -> dict:
    observed = [r["period"] for r in rows if r["value"] != ""]
    return {
        "first_period": rows[0]["period"], "latest_period": observed[-1],
        "first_date": period_start(rows[0]["period"]), "last_date": period_start(rows[-1]["period"]),
        "last_observed_date": period_start(observed[-1]), "blank_rows": len(rows) - len(observed),
    }


# =========================================================================== HTTP client (cache + pacing)

@dataclass
class Fetched:
    body: bytes
    url: str                      # the full request URL (with query string)
    headers: dict = field(default_factory=dict)


class Client:
    """Serial HTTP with a per-run response cache (series sharing a response cost one request) and per-host pacing."""

    def __init__(self, pause: float = PAUSE_SECONDS, host_pause: dict | None = None, sleep=time.sleep):
        self.pause, self.host_pause, self.sleep = pause, dict(HOST_PAUSE if host_pause is None else host_pause), sleep
        self.cache: dict[tuple, Fetched] = {}
        self.failed: dict[tuple, Exception] = {}   # a failed response is not requested again in the same run
        self.last: dict[str, float] = {}
        self.requests = 0
        self.last_url: str | None = None   # the URL last attempted, recorded in a failure's null sidecar

    def _wait(self, url: str) -> None:
        host = urlparse(url).netloc
        gap = self.host_pause.get(host, self.pause)
        if host in self.last:
            remaining = gap - (time.monotonic() - self.last[host])
            if remaining > 0:
                self.sleep(remaining)
        self.last[host] = time.monotonic()

    def get(self, url: str, params: dict | list | None = None, headers: dict | None = None) -> Fetched:
        items = list(params.items()) if isinstance(params, dict) else list(params or [])
        key = ("GET", url, tuple(items), tuple(sorted((headers or {}).items())))
        self.last_url = url + ("?" + urlencode(items) if items else "")
        if key in self.failed:
            raise self.failed[key]
        if key not in self.cache:
            self._wait(url)
            self.requests += 1
            try:
                resp = http_get(url, params=items or None, headers=headers)
            except requests.RequestException as exc:
                self.failed[key] = exc
                raise
            full = url + ("?" + urlencode(items) if items else "")
            self.cache[key] = Fetched(resp.content, full, dict(resp.headers))
        return self.cache[key]

    def curl(self, url: str) -> Fetched:
        """One plain curl request (no retry). Raises CurlError with the HTTP status on anything but 200."""
        key = ("CURL", url)
        self.last_url = url
        if key in self.failed:
            raise self.failed[key]
        if key not in self.cache:
            self._wait(url)
            self.requests += 1
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / "body"
                proc = subprocess.run(["curl", "-sS", "-L", "-m", "120", "-o", str(out), "-w", "%{http_code}", url],
                                      capture_output=True, text=True)
                status = proc.stdout.strip()[-3:]
                body = out.read_bytes() if out.exists() else b""
            if proc.returncode != 0 or status != "200":
                self.failed[key] = CurlError(status or "000", (proc.stderr or "").strip()[:300])
                raise self.failed[key]
            self.cache[key] = Fetched(body, url, {})
        return self.cache[key]


class CurlError(RuntimeError):
    def __init__(self, status: str, detail: str = ""):
        super().__init__(f"HTTP {status}" + (f": {detail}" if detail else ""))
        self.status = status


@dataclass
class Result:
    rows: list[dict]
    source_url: str
    method: str
    extra: dict = field(default_factory=dict)
    archive: tuple[str, bytes] | None = None   # (file name, bytes) kept unchanged in <ccy>/_raw/


# =========================================================================== parsers (pure; tested offline)

def parse_fred(text: str, series_id: str) -> list[tuple[str, str]]:
    from pipeline.sources.fred import parse_series
    out = []
    for row in parse_series(text, series_id):
        y, m, d = (int(x) for x in row["date"].split("-"))
        if d != 1:
            raise ValueError(f"{series_id}: {row['date']} is not the first day of a month")
        out.append((month(y, m), row["value"]))
    return out


def _sdmx_name(text: str) -> str:
    """SDMX-CSV with labels writes 'CODE: Label'; keep the code."""
    return text.split(": ", 1)[0].strip()


def read_sdmx_csv(text: str) -> list[dict]:
    reader = csv.reader(io.StringIO(text.lstrip("﻿"), newline=""))
    header = next(reader, None)
    if not header or "TIME_PERIOD" not in [_sdmx_name(h) for h in header] or \
            "OBS_VALUE" not in [_sdmx_name(h) for h in header]:
        raise ValueError(f"not an SDMX-CSV body (header {header!r})")
    names = [_sdmx_name(h) for h in header]
    return [{n: _sdmx_name(v) if n != "OBS_VALUE" else v.strip() for n, v in zip(names, line)}
            for line in reader if line]


def sdmx_pairs(records: list[dict], select: dict, expect: dict | None = None,
               period: Callable[[str], str] = lambda p: p) -> list[tuple[str, str]]:
    """(period, value) for records whose columns equal `select`; `expect` columns must hold on every match."""
    out = []
    for r in records:
        if any(r.get(k) != v for k, v in select.items()):
            continue
        for k, v in (expect or {}).items():
            if r.get(k) != v:
                raise ValueError(f"{k}={r.get(k)!r}, expected {v!r} ({select})")
        raw = r["OBS_VALUE"]
        out.append((period(r["TIME_PERIOD"]), "" if raw.lower() in ("", "nan") else plain_decimal(raw)))
    if not out:
        raise ValueError(f"no observations match {select}")
    return out


def sdmx_attributes(records: list[dict], select: dict, columns: tuple[str, ...]) -> dict:
    found: dict[str, str] = {}
    for r in records:
        if all(r.get(k) == v for k, v in select.items()):
            for c in columns:
                if r.get(c):
                    found[c] = r[c]
    return found


def parse_boj(text: str, code: str, unit: str) -> tuple[list[tuple[str, str]], dict]:
    lines = list(csv.reader(io.StringIO(text.lstrip("﻿"), newline="")))
    status = next((r for r in lines if r and r[0] == "STATUS"), None)
    if not status or status[1] != "200":
        raise ValueError(f"BoJ API status {status!r}")
    nxt = next((r for r in lines if r and r[0] == "NEXTPOSITION"), None)
    if nxt and len(nxt) > 1 and nxt[1].strip():
        raise ValueError("BoJ API response is paginated (NEXTPOSITION set); narrow the date range")
    header_at = next(i for i, r in enumerate(lines) if r and r[0] == "SERIES_CODE")
    cols = lines[header_at]
    out, meta = [], {}
    for r in lines[header_at + 1:]:
        if not r or r[0] != code:
            continue
        rec = dict(zip(cols, r))
        if rec["UNIT"] != unit:
            raise ValueError(f"BoJ {code}: unit {rec['UNIT']!r}, expected {unit!r}")
        survey = rec["SURVEY_DATES"]
        if not re.fullmatch(r"\d{6}", survey):
            raise ValueError(f"BoJ {code}: unexpected survey date {survey!r}")
        raw = rec["VALUES"].strip()
        out.append((month(int(survey[:4]), int(survey[4:])), "" if raw in ("", "NaN") else plain_decimal(raw)))
        meta = {"name_of_time_series": rec["NAME_OF_TIME_SERIES"], "boj_last_update": rec["LAST_UPDATE"]}
    if not out:
        raise ValueError(f"BoJ: no rows for {code}")
    return out, meta


def parse_iadb(text: str, code: str) -> list[tuple[str, str]]:
    """Bank of England IADB CSV (CSVF=TN): DATE,<CODE> then '31 Jan 2024,3045046' rows."""
    reader = csv.reader(io.StringIO(text.lstrip("﻿"), newline=""))
    header = next(reader, None)
    if not header or header[0].strip().upper() != "DATE" or code not in [h.strip() for h in header]:
        raise ValueError(f"BoE IADB: unexpected header {header!r} (blocked page or changed format)")
    col = [h.strip() for h in header].index(code)
    out = []
    for line in reader:
        if not line or not line[0].strip():
            continue
        d = datetime.strptime(line[0].strip(), "%d %b %Y")
        raw = line[col].strip() if col < len(line) else ""
        out.append((month(d.year, d.month), "" if raw == "" else plain_decimal(raw)))
    return out


def parse_snb(text: str, match: tuple[str, ...]) -> tuple[list[tuple[str, str]], dict]:
    lines = list(csv.reader(io.StringIO(text.lstrip("﻿"), newline=""), delimiter=";"))
    meta = {}
    for r in lines:
        if len(r) == 2 and r[0] == "PublishingDate":
            meta["publication_date"] = r[1]
    header_at = next((i for i, r in enumerate(lines) if r and r[0] == "Date"), None)
    if header_at is None:
        raise ValueError("SNB cube: no Date header")
    header = lines[header_at]
    dims = header[1:-1]
    if len(dims) != len(match):
        raise ValueError(f"SNB cube: dimensions {dims!r} do not fit selection {match!r}")
    out = []
    for r in lines[header_at + 1:]:
        if len(r) != len(header) or tuple(r[1:-1]) != tuple(match):
            continue
        if not _MONTH.match(r[0]):
            raise ValueError(f"SNB cube: unexpected date {r[0]!r}")
        out.append((r[0], plain_decimal(r[-1]) if r[-1].strip() else ""))
    if not out:
        raise ValueError(f"SNB cube: no rows for {match}")
    return out, meta


def snb_labels(doc: dict) -> list[dict[str, str]]:
    """Per dimension, {item id: published name} from an SNB cube's /dimensions/en document (nested items flattened)."""
    out = []
    for dim in doc.get("dimensions", []):
        names: dict[str, str] = {}
        stack = list(dim.get("dimensionItems", []))
        while stack:
            item = stack.pop()
            if "id" in item and "name" in item:
                names[item["id"]] = item["name"]
            stack.extend(item.get("dimensionItems", []))
        out.append(names)
    if not out:
        raise ValueError("SNB cube: no dimensions in the metadata")
    return out


def check_snb_labels(doc: dict, match: tuple[str, ...], expected: tuple[str, ...]) -> list[str]:
    """The published name of each selected item; raises unless each contains the expected text."""
    dims = snb_labels(doc)
    if len(dims) != len(match):
        raise ValueError(f"SNB cube: {len(dims)} dimensions, selection {match!r}")
    found = []
    for names, code, want in zip(dims, match, expected):
        name = names.get(code)
        if name is None or want not in name:
            raise ValueError(f"SNB cube: item {code} is named {name!r}, expected it to contain {want!r}")
        found.append(name)
    return found


def parse_rba(text: str, series_id: str, units: str) -> tuple[list[tuple[str, str]], dict]:
    lines = list(csv.reader(io.StringIO(text.lstrip("﻿"), newline="")))
    by_label = {r[0]: r for r in lines if r and r[0] in ("Title", "Type", "Units", "Publication date", "Series ID")}
    ids = by_label.get("Series ID")
    if not ids or series_id not in ids:
        raise ValueError(f"RBA table: no series {series_id}")
    col = ids.index(series_id)
    if by_label["Units"][col] != units:
        raise ValueError(f"RBA {series_id}: units {by_label['Units'][col]!r}, expected {units!r}")
    out = []
    for r in lines:
        if not r or not re.fullmatch(r"\d{2}/\d{2}/\d{4}", r[0]):
            continue
        d = datetime.strptime(r[0], "%d/%m/%Y")
        raw = r[col].strip() if col < len(r) else ""
        if raw:
            out.append((month(d.year, d.month), plain_decimal(raw)))
        elif out:   # a blank after the first observation is kept as a blank row, so it is counted (never filled)
            out.append((month(d.year, d.month), ""))
    meta = {"title_published": by_label["Title"][col], "type_published": by_label["Type"][col],
            "publication_date": by_label.get("Publication date", [""] * (col + 1))[col],
            "units_published": by_label["Units"][col]}
    return out, meta


def parse_valet(doc: dict, series_id: str) -> tuple[list[tuple[str, str]], dict]:
    detail = doc.get("seriesDetail", {}).get(series_id)
    if detail is None:
        raise ValueError(f"Valet: no series {series_id}")
    out = []
    for o in doc.get("observations", []):
        if series_id in o:
            d = o["d"]
            if not re.fullmatch(r"\d{4}-\d{2}-01", d):
                raise ValueError(f"Valet {series_id}: unexpected date {d!r}")
            raw = (o[series_id] or {}).get("v", "")
            out.append((d[:7], plain_decimal(raw) if raw not in ("", None) else ""))
    return out, {"label_published": detail.get("label")}


def xls_number(value: float) -> str:
    """A spreadsheet cell's binary double as a plain decimal at six places with trailing zeros dropped, so
    8043994.055000001 (binary noise on a figure kept to HK$ thousand) is written 8043994.055."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"not a numeric cell: {value!r}")
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return plain_decimal(text)


def parse_hkma_t0101(body: bytes, aggregate: str) -> tuple[list[tuple[str, str]], dict]:
    """HK$ column of M2 or M3 in HKMA bulletin table 1.1: monthly rows plus year-end rows (stored as December)."""
    import xlrd
    sheet = xlrd.open_workbook(file_contents=body).sheet_by_index(0)
    rows = [sheet.row_values(r) for r in range(sheet.nrows)]
    if not any("HK$ million" in str(c) for r in rows[:8] for c in r):
        raise ValueError("HKMA T0101: unit line 'HK$ million' not found")
    header_at = next(i for i, r in enumerate(rows) if str(r[0]).strip() == "As at end of")
    agg_row = next(r for r in rows[header_at - 5:header_at] if aggregate in [str(c).strip() for c in r])
    agg_col = [str(c).strip() for c in agg_row].index(aggregate)
    col = agg_col + 1
    if not str(rows[header_at][col]).strip().startswith("HK$"):
        raise ValueError(f"HKMA T0101: column after {aggregate} is {rows[header_at][col]!r}, not HK$")
    monthly: dict[str, str] = {}
    year_end: dict[str, str] = {}
    year = None
    for r in rows[header_at + 1:]:
        if any("per annum" in str(c) for c in r):
            break
        c0, c1, val = r[0], str(r[1]).strip(), r[col]
        if isinstance(c0, float):
            year = int(c0)
        if c1 in MONTH_ABBR:
            if year is None:
                raise ValueError("HKMA T0101: month row before any year")
            monthly[month(year, MONTH_ABBR[c1])] = "" if str(val).strip() in ("N.A.", "") else xls_number(val)
        elif isinstance(c0, float) and c1 == "" and val != "":
            year_end[month(int(c0), 12)] = xls_number(val)
    for per, v in year_end.items():
        if per in monthly and monthly[per] != "" and Decimal(monthly[per]) != Decimal(v):
            raise ValueError(f"HKMA T0101: year-end {per} {v} differs from the monthly row {monthly[per]}")
    merged = {**year_end, **monthly}
    if not merged:
        raise ValueError("HKMA T0101: no rows")
    return sorted(merged.items()), {"monthly_rows": len(monthly), "year_end_rows": len(year_end)}


def parse_hkma_api(doc: dict, aggregate: str) -> list[tuple[str, str]]:
    """HKMA open API money/supply-adjusted: one record per month. The field names (end_of_month, m2_hkd, m3_hkd)
    are taken from the API documentation and have not been seen in a live response (the API refused this
    network), so anything else fails loudly and the caller falls back to the bulletin."""
    header = doc.get("header") or {}
    if header.get("success") is not True:
        raise ValueError(f"HKMA API: unsuccessful response header {header!r}")
    result = doc.get("result") or {}
    records = result.get("records") or []
    if result.get("datasize") is not None and int(result["datasize"]) > len(records):
        raise ValueError(f"HKMA API: {result['datasize']} records, only {len(records)} returned (paginated)")
    key = f"{aggregate.lower()}_hkd"
    out = []
    for r in records:
        if "end_of_month" not in r or key not in r:
            raise ValueError(f"HKMA API: record lacks end_of_month or {key} (fields {sorted(r)})")
        m = re.fullmatch(r"(\d{4})-(\d{2})", str(r["end_of_month"]))
        if not m:
            raise ValueError(f"HKMA API: unexpected end_of_month {r['end_of_month']!r}")
        raw = r[key]
        out.append((month(int(m.group(1)), int(m.group(2))),
                    "" if raw in (None, "") else plain_decimal(repr(raw) if isinstance(raw, float) else str(raw))))
    if not out:
        raise ValueError("HKMA API: no records")
    return out


def singstat_period(key: str) -> str:
    m = re.fullmatch(r"(\d{4}) ([A-Z][a-z]{2})", key)
    if m and m.group(2) in MONTH_ABBR:
        return month(int(m.group(1)), MONTH_ABBR[m.group(2)])
    raise ValueError(f"SingStat: unexpected monthly period {key!r}")


def parse_singstat(doc: dict, row_text: str, uom: str | None) -> tuple[list, dict]:
    data = doc["Data"]
    matches = [r for r in data["row"] if r.get("rowText") == row_text]
    if len(matches) != 1:
        raise ValueError(f"SingStat {data.get('id')}: {len(matches)} rows named {row_text!r}")
    row = matches[0]
    if uom is not None and row.get("uoM") != uom:
        raise ValueError(f"SingStat {data.get('id')}: unit {row.get('uoM')!r}, expected {uom!r}")
    out = []
    for c in row["columns"]:
        raw = (c.get("value") or "").strip()
        out.append((singstat_period(c["key"]), "" if raw in ("", "-", "na", "NA") else plain_decimal(raw)))
    return out, {"publication_date": data.get("dataLastUpdated"), "singstat_title": data.get("title"),
                 "units_published": row.get("uoM"), "singstat_datasource": data.get("datasource")}


# =========================================================================== fetchers (one per publisher)

FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
ECB_URL = "https://data-api.ecb.europa.eu/service/data"
BOJ_URL = "https://www.stat-search.boj.or.jp/api/v1/getDataCode"
IADB_URL = "https://www.bankofengland.co.uk/boeapps/database/_iadb-FromShowColumns.asp"
OECD_URL = "https://sdmx.oecd.org/public/rest/data"
OECD_MONAGG = "OECD.SDD.STES,DSD_STES@DF_MONAGG,4.0"
OECD_CSV = {"Accept": "application/vnd.sdmx.data+csv; charset=utf-8"}
SNB_URL = "https://data.snb.ch/api/cube"
RBA_D3 = "https://www.rba.gov.au/statistics/tables/csv/d3-data.csv"
VALET_URL = "https://www.bankofcanada.ca/valet/observations"
HKMA_API = ("https://api.hkma.gov.hk/public/market-data-and-statistics/monthly-statistical-bulletin/money/"
            "supply-adjusted")
HKMA_T0101 = ("https://www.hkma.gov.hk/media/eng/doc/market-data-and-statistics/monthly-statistical-bulletin/"
              "T0101.xls")
SINGSTAT_URL = "https://tablebuilder.singstat.gov.sg/api/table/tabledata"


def _sha(body: bytes) -> str:
    """sha256 of the response bytes exactly as received."""
    return hashlib.sha256(body).hexdigest()


def fetch_fred(client: Client, spec: Spec) -> Result:
    got = client.get(FRED_URL, {"id": spec.series_id})
    pairs = parse_fred(got.body.decode("utf-8-sig"), spec.series_id)
    return Result(finish(pairs, spec), got.url, "FRED graph CSV (keyless), one request per series",
                  {"http_last_modified": got.headers.get("Last-Modified"), "response_sha256": _sha(got.body)})


def fetch_ecb(client: Client, spec: Spec) -> Result:
    url = f"{ECB_URL}/{spec.param('dataset')}/{spec.param('key')}"
    got = client.get(url, {"format": "csvdata", "startPeriod": START_MONTH})
    records = read_sdmx_csv(got.body.decode("utf-8-sig"))
    expect = {k: spec.param(k) for k in ("UNIT", "UNIT_MULT", "UNIT_INDEX_BASE") if spec.param(k)}
    pairs = sdmx_pairs(records, {"KEY": spec.series_id}, expect)
    attrs = sdmx_attributes(records, {"KEY": spec.series_id},
                            ("TITLE", "TITLE_COMPL", "UNIT", "UNIT_MULT", "UNIT_INDEX_BASE", "DECIMALS"))
    return Result(finish(pairs, spec), got.url, "ECB data portal SDMX (csvdata), series as published",
                  {"published_attributes": attrs, "response_sha256": _sha(got.body)})


def fetch_boj(client: Client, spec: Spec) -> Result:
    codes = ",".join(s.series_id for s in SPECS if s.publisher == "boj")
    got = client.get(BOJ_URL, [("format", "csv"), ("lang", "en"), ("db", spec.param("db")),
                               ("startDate", START_MONTH.replace("-", "")), ("code", codes)])
    pairs, meta = parse_boj(got.body.decode("utf-8-sig"), spec.series_id, spec.param("unit"))
    meta["response_sha256"] = _sha(got.body)
    return Result(finish(pairs, spec), got.url, "Bank of Japan time-series data search API (CSV, keyless)", meta)


def fetch_boe(client: Client, spec: Spec) -> Result:
    day, mon, year = START_MONTH[8:] or "01", START_MONTH[5:7], START_MONTH[:4]
    query = urlencode([("csv.x", "yes"), ("SeriesCodes", spec.series_id), ("CSVF", "TN"), ("UsingCodes", "Y"),
                       ("Datefrom", f"{day}/{list(MONTH_ABBR)[int(mon) - 1]}/{year}"), ("Dateto", "now")])
    url = f"{IADB_URL}?{query}"
    got = client.curl(url)
    pairs = parse_iadb(got.body.decode("utf-8-sig", errors="replace"), spec.series_id)
    return Result(finish(pairs, spec), url, "Bank of England IADB CSV, one plain curl request (no retry); the "
                  "response is archived unchanged under _raw/", {"response_sha256": _sha(got.body)},
                  archive=(f"iadb_{spec.series_id}.csv", got.body))


def fetch_oecd_monagg(client: Client, spec: Spec) -> Result:
    got = oecd_monagg_request(client)
    records = read_sdmx_csv(got.body.decode("utf-8-sig"))
    select = {"REF_AREA": spec.param("area"), "FREQ": "M", "MEASURE": "MABM", "UNIT_MEASURE": "XDC",
              "ADJUSTMENT": spec.param("adjustment")}
    pairs = sdmx_pairs(records, select, {"UNIT_MULT": "6"})
    return Result(finish(pairs, spec), got.url, "OECD SDMX DF_MONAGG (SDMX-CSV), one request for every area",
                  {"response_sha256": _sha(got.body)})


def oecd_monagg_request(client: Client) -> Fetched:
    key = "+".join(OECD_MONAGG_AREAS) + ".M.MABM.XDC....."
    return client.get(f"{OECD_URL}/{OECD_MONAGG}/{key}", {"startPeriod": START_MONTH}, headers=OECD_CSV)


def fetch_snb(client: Client, spec: Spec) -> Result:
    params = [("fromDate", START_MONTH)] + ([("dimSel", spec.param("dims"))] if spec.param("dims") else [])
    got = client.get(f"{SNB_URL}/{spec.param('cube')}/data/csv/en", params)
    pairs, meta = parse_snb(got.body.decode("utf-8-sig"), tuple(spec.param("match")))
    dims = client.get(f"{SNB_URL}/{spec.param('cube')}/dimensions/en")
    labels = check_snb_labels(json.loads(dims.body), tuple(spec.param("match")), tuple(spec.param("labels")))
    meta.update(response_sha256=_sha(got.body), item_labels_published=labels, labels_url=dims.url)
    return Result(finish(pairs, spec), got.url, "SNB data portal cube API (CSV, keyless)", meta)


def fetch_rba(client: Client, spec: Spec) -> Result:
    got = client.get(RBA_D3)
    pairs, meta = parse_rba(got.body.decode("utf-8-sig", errors="replace"), spec.series_id,
                            spec.param("units_published"))
    meta["response_sha256"] = _sha(got.body)
    return Result(finish(pairs, spec), got.url, "RBA statistical table D3 (CSV)", meta)


def fetch_valet(client: Client, spec: Spec) -> Result:
    ids = ",".join(s.series_id for s in SPECS if s.publisher == "valet")
    got = client.get(f"{VALET_URL}/{ids}/json", {"start_date": START_MONTH + "-01"})
    pairs, meta = parse_valet(json.loads(got.body), spec.series_id)
    meta["response_sha256"] = _sha(got.body)
    return Result(finish(pairs, spec), got.url, "Bank of Canada Valet API (JSON, keyless)", meta)


def fetch_hkma(client: Client, spec: Spec) -> Result:
    """The HKMA open API first (full monthly history); if it refuses or its shape is not the documented one, the
    Monthly Statistical Bulletin table 1.1 (latest months plus year-end rows only)."""
    try:
        api = client.get(HKMA_API, [("pagesize", "1000"), ("sortby", "end_of_month"), ("sortorder", "asc")])
        pairs = parse_hkma_api(json.loads(api.body), spec.param("aggregate"))
        return Result(finish(pairs, spec), api.url, "HKMA open API, money supply adjusted to include foreign "
                      "currency swap deposits (JSON, keyless)", {"response_sha256": _sha(api.body)})
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        api_error = f"{type(exc).__name__}: {str(exc)[:200]}"
    got = client.get(HKMA_T0101)
    pairs, meta = parse_hkma_t0101(got.body, spec.param("aggregate"))
    kept = api_vintage_rows(spec)
    if kept > sum(1 for _, v in pairs if v != ""):
        raise ValueError(f"HKMA API failed ({api_error}) and bulletin T0101 has {sum(1 for _, v in pairs if v != '')} "
                         f"observations against {kept} in the cached API vintage, which is kept")
    meta.update(response_sha256=_sha(got.body), hkma_api_url=HKMA_API, hkma_api_error=api_error,
                history_note="HKMA bulletin T0101 holds only the latest months (plus year-end rows) and the HKMA "
                             "API refused this network")
    return Result(finish(pairs, spec), got.url,
                  "HKMA Monthly Statistical Bulletin table 1.1 (XLS, parsed with xlrd), used because the HKMA open "
                  "API failed; the workbook is archived unchanged under _raw/", meta, archive=("T0101.xls", got.body))


def api_vintage_rows(spec: Spec) -> int:
    """Observations in the cached file when it came from the HKMA open API (0 otherwise or when there is none)."""
    path = raw_csv(spec)
    sidecar = path.with_suffix(".meta.json")
    if not (path.exists() and sidecar.exists()):
        return 0
    if not str(json.loads(sidecar.read_text()).get("source_url") or "").startswith(HKMA_API):
        return 0
    with path.open(newline="") as fh:
        return sum(1 for r in csv.DictReader(fh) if r.get("value"))


def fetch_singstat(client: Client, spec: Spec) -> Result:
    got = client.get(f"{SINGSTAT_URL}/{spec.param('table')}")
    pairs, meta = parse_singstat(json.loads(got.body), spec.param("row"), spec.param("uom"))
    meta["response_sha256"] = _sha(got.body)
    return Result(finish(pairs, spec), got.url, "SingStat Table Builder API (JSON, keyless)", meta)


FETCHERS: dict[str, Callable[[Client, Spec], Result]] = {
    "fred": fetch_fred, "ecb": fetch_ecb, "boj": fetch_boj, "boe": fetch_boe, "oecd_monagg": fetch_oecd_monagg,
    "snb": fetch_snb, "rba": fetch_rba, "valet": fetch_valet, "hkma": fetch_hkma, "singstat": fetch_singstat,
}

PUBLISHERS = {
    "fred": "Federal Reserve Bank of St. Louis (FRED), mirroring the original publisher",
    "ecb": "European Central Bank", "boj": "Bank of Japan", "boe": "Bank of England", "oecd_monagg": "OECD",
    "snb": "Swiss National Bank", "rba": "Reserve Bank of Australia", "valet": "Bank of Canada",
    "hkma": "Hong Kong Monetary Authority",
    "singstat": "Department of Statistics Singapore (data from the Monetary Authority of Singapore)",
}

NULL_REASONS = {
    "boe": "BoE IADB refused this network",
}


# =========================================================================== writing

def spec_meta(spec: Spec) -> dict:
    return {
        "currency": spec.ccy.upper(), "field": spec.field, "series_id": spec.series_id,
        "description": spec.description, "units": spec.units, "frequency": spec.frequency,
        "seasonal_adjustment": spec.seasonal_adjustment, "role": spec.role, "aggregate": spec.aggregate or None,
        "basis": spec.basis or None, "publisher": PUBLISHERS[spec.publisher],
        "notes": list(spec.notes), "refresh": REFRESH_RULE,
    }


def raw_csv(spec: Spec) -> Path:
    return RAW_DIR / NAME / spec.ccy / f"{spec.field}.csv"


def write_result(spec: Spec, result: Result) -> None:
    summary = summarize(result.rows)
    extra = {k: v for k, v in result.extra.items() if v not in (None, "", {}, [])}
    if result.archive:
        name, body = result.archive
        archived = raw_csv(spec).parent / "_raw" / name
        archived.parent.mkdir(parents=True, exist_ok=True)
        archived.write_bytes(body)
        write_meta(archived, source_url=result.source_url, method="raw response archived unchanged",
                   bytes=len(body), sha256=_sha(body), description=f"{PUBLISHERS[spec.publisher]}: {spec.series_id}")
        extra["archived_response"] = archived.relative_to(ROOT).as_posix()
    write_csv(NAME, spec.name, result.rows, FIELDNAMES, source_url=result.source_url, method=result.method,
              **spec_meta(spec), **summary, **extra)


def record_failure(spec: Spec, source_url: str | None, error: str) -> str:
    """Keep a previous vintage untouched (note the failed attempt and its URL in its sidecar); otherwise write a null
    sidecar that records the URL attempted."""
    path = raw_csv(spec)
    sidecar = path.with_suffix(".meta.json")
    now = utc_now_iso()
    if path.exists() and sidecar.exists():
        meta = json.loads(sidecar.read_text())
        meta.update(error=error, error_at=now, error_url=source_url)
        sidecar.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")
        return "failed, previous vintage kept"
    path.parent.mkdir(parents=True, exist_ok=True)
    reason = NULL_REASONS.get(spec.publisher, f"{PUBLISHERS[spec.publisher]} request failed")
    meta = {**spec_meta(spec), "source_url": source_url, "complete": False, "error": error, "error_at": now,
            "fetched_at": now, "null_reason": reason, "rows": 0,
            "file": path.relative_to(ROOT).as_posix()}
    sidecar.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")
    return "failed, no vintage: null sidecar written"


def write_monagg(client: Client) -> int:
    got = oecd_monagg_request(client)
    records = read_sdmx_csv(got.body.decode("utf-8-sig"))
    rows = []
    for r in records:
        if r.get("MEASURE") != "MABM" or r.get("UNIT_MEASURE") != "XDC" or r.get("FREQ") != "M":
            continue
        if r.get("UNIT_MULT") != "6":
            raise ValueError(f"OECD MONAGG {r.get('REF_AREA')}: UNIT_MULT {r.get('UNIT_MULT')!r}, expected 6")
        raw = r["OBS_VALUE"]
        rows.append({"area": r["REF_AREA"], "adjustment": r["ADJUSTMENT"], "period": r["TIME_PERIOD"],
                     "value": "" if raw.lower() in ("", "nan") else plain_decimal(raw)})
    rows.sort(key=lambda r: (r["area"], r["adjustment"], r["period"]))
    if not rows:
        raise ValueError("OECD MONAGG: no rows")
    coverage: dict[str, dict] = {}
    for r in rows:
        span = coverage.setdefault(f"{r['area']}.{r['adjustment']}", {"first_period": r["period"], "rows": 0})
        span["last_period"], span["rows"] = r["period"], span["rows"] + 1
    observed = [r["period"] for r in rows if r["value"] != ""]
    write_csv(NAME, "oecd/monagg", rows, ["area", "adjustment", "period", "value"], source_url=got.url,
              method="OECD SDMX DF_MONAGG (SDMX-CSV), broad money MABM in national currency (XDC), millions",
              description="OECD monetary aggregates: headline broad money per area, a mirror of the national "
                          "series used for cross-check X9",
              units="national currency million (USA USD, EA20 EUR, JPN JPY, GBR GBP, CHN CNY, CHE CHF, AUS AUD, "
                    "CAN CAD)", frequency=M, areas_requested=list(OECD_MONAGG_AREAS), coverage=coverage,
              adjustment_codes={"Y": "calendar and seasonally adjusted (as labelled by OECD)",
                                "N": "not adjusted (as labelled by OECD)"},
              notes=["OECD's ADJUSTMENT flag is swapped for EA20 (Y equals the ECB's unadjusted "
                     "series); compare like with like against the national series.",
                     "OECD's aggregate per area: USA M2, EA20 M3, JPN M3, GBR wider than M4, CHN M2, CHE M3, "
                     "AUS M3 (not RBA broad money), CAN M3 (not M2++)."],
              refresh=REFRESH_RULE, response_sha256=_sha(got.body),
              first_date=period_start(min(r["period"] for r in rows)),
              last_date=period_start(max(r["period"] for r in rows)), last_observed_date=period_start(max(observed)))
    return len(rows)


# what a fetch or a parse may raise; one tuple for the per-series loop and the OECD mirror (MC-11)
FETCH_ERRORS = (requests.RequestException, CurlError, ValueError, KeyError, TypeError, ET.ParseError, OSError)


def pull(args: argparse.Namespace | None = None, client: Client | None = None,
         specs: list[Spec] | None = None) -> dict:
    client = client or Client()
    specs = SPECS if specs is None else specs
    status: dict[str, str] = {}
    # Bank of England first and alone: one request, then the other hosts.
    for spec in sorted(specs, key=lambda s: s.publisher != "boe"):
        client.last_url = None
        try:
            result = FETCHERS[spec.publisher](client, spec)
            write_result(spec, result)
        except FETCH_ERRORS as exc:
            status[spec.name] = record_failure(spec, getattr(client, "last_url", None), f"{type(exc).__name__}: {exc}")
            print(f"[{NAME}] {spec.name} FAILED ({status[spec.name]}): {exc}")
            continue
        status[spec.name] = "ok"
        rows = result.rows
        print(f"[{NAME}] {spec.name}: {len(rows)} rows {rows[0]['period']}..{rows[-1]['period']} ({spec.series_id})")
    if specs is SPECS:
        try:
            n = write_monagg(client)
            status["oecd/monagg"] = "ok"
            print(f"[{NAME}] oecd/monagg: {n} rows")
        except FETCH_ERRORS as exc:
            status["oecd/monagg"] = f"failed: {exc}"
            print(f"[{NAME}] oecd/monagg FAILED, cached file kept: {exc}")
    print(f"[{NAME}] {client.requests} HTTP requests")
    failed = {k: v for k, v in status.items() if v != "ok"}
    if failed:
        detail = "; ".join(f"{k}: {v}" for k, v in failed.items())
        raise RuntimeError(f"{len(failed)} of {len(status)} money series failed ({detail})")
    return status


if __name__ == "__main__":
    pull()
