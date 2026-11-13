"""BIS Triennial Central Bank Survey 2025: foreign-exchange turnover by currency (the fiat panel's ranking).

Provenance: the keyless BIS SDMX v2 data API, dataflow WS_DER_OTC_TOV (OTC derivatives and FX turnover), one
request for every currency,
    https://stats.bis.org/api/v2/data/dataflow/BIS/WS_DER_OTC_TOV/1.0/A.U.A.B.5J.A.5J.A..TO1.A.A.3.C
        ?format=csv&startPeriod=2025&endPeriod=2025
Key, dimension by dimension: FREQ A (survey year), DER_TYPE U (turnover), DER_INSTR A (all FX instruments),
DER_RISK B (foreign exchange), DER_REP_CTY 5J (all reporting countries), DER_SECTOR_CPY A (all counterparties),
DER_CPC 5J (all counterparty countries), DER_SECTOR_UDL A, DER_CURR_LEG1 <currency> (wildcard: every
currency), DER_CURR_LEG2 TO1 (against all currencies), DER_ISSUE_MAT A, DER_RATING A, DER_EX_METHOD 3,
DER_BASIS C (net-net: adjusted for local and cross-border inter-dealer double counting).
Values are the April 2025 daily average in USD millions (UNIT_MEASURE USD, UNIT_MULT 6). These are the
figures in the survey's statistical annex, Table 25.2 (statistics-rpfx25-fx-annex.pdf, landing page
https://www.bis.org/statistics/rpfx25_fx.htm), which is the cross-check; the API is used because the PDF needs a
parser this pipeline does not carry.

Every currency row (and the TO1 all-currency total) is written exactly as published; the ranking and the shares
(currency / TO1, summing to two hundred percent because every trade has two currencies) are derived in the
models, not here. Rows with OBS_VALUE NaN (status M: not published) are kept with a blank value.

Output: data/raw/bis_fx/fx_turnover_2025.csv, columns currency,period,value,obs_status, sorted by currency.
The file is deliberately outside data/raw/bis/: that directory holds monthly series with a freshness rule
(days after month end) that a one-off survey would fail.

Refresh rule: re-downloaded every run (one small request); a failed request keeps the previous file untouched.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import re

from pipeline.config import http_get, write_csv

NAME = "bis_fx"
REQUIRES: list[str] = []

BASE_URL = "https://stats.bis.org/api/v2/data/dataflow/BIS"
FLOW = "WS_DER_OTC_TOV"
KEY = "A.U.A.B.5J.A.5J.A..TO1.A.A.3.C"
SURVEY_YEAR = "2025"
FILE_NAME = f"fx_turnover_{SURVEY_YEAR}"
FIELDNAMES = ["currency", "period", "value", "obs_status"]
TOTAL_CODE = "TO1"
ANNEX_PDF = "https://www.bis.org/publications/commentary-otc-derivatives/statistics-rpfx25-fx-annex.pdf"
LANDING_PAGE = "https://www.bis.org/statistics/rpfx25_fx.htm"
REFRESH_RULE = "re-downloaded every run (one request); a failed request keeps the previous file"
# the dimensions that must hold on every row; LEG1 (the currency) is the only one left open
FIXED = {"FREQ": "A", "DER_TYPE": "U", "DER_INSTR": "A", "DER_RISK": "B", "DER_REP_CTY": "5J",
         "DER_SECTOR_CPY": "A", "DER_CPC": "5J", "DER_SECTOR_UDL": "A", "DER_CURR_LEG2": "TO1",
         "DER_ISSUE_MAT": "A", "DER_RATING": "A", "DER_EX_METHOD": "3", "DER_BASIS": "C",
         "UNIT_MEASURE": "USD", "UNIT_MULT": "6"}
_CODE = re.compile(r"^[A-Z]{3}$")   # an ISO 4217 letter code; the total TO1 is the only other code accepted
# codes that are not national currencies (precious metals, the SDR, "no currency", residual or total rows): kept in
# the file as published, never ranked (pipeline.models.load.bis_fx_turnover)
NON_CURRENCY = frozenset({"XAU", "XAG", "XPT", "XPD", "XDR", "XXX", "OTH", "TOT"})


def endpoint() -> str:
    return f"{BASE_URL}/{FLOW}/1.0/{KEY}"


def params() -> dict:
    return {"format": "csv", "startPeriod": SURVEY_YEAR, "endPeriod": SURVEY_YEAR}


def source_url() -> str:
    return f"{endpoint()}?format=csv&startPeriod={SURVEY_YEAR}&endPeriod={SURVEY_YEAR}"


def parse(text: str, year: str = SURVEY_YEAR) -> list[dict]:
    """Rows for `year`, one per currency (plus TO1), validated against the fixed key; values as published."""
    from pipeline.sources.money import plain_decimal
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿"), newline=""))
    needed = set(FIXED) | {"DER_CURR_LEG1", "TIME_PERIOD", "OBS_VALUE", "OBS_STATUS"}
    missing = needed - set(reader.fieldnames or [])
    if missing:
        raise ValueError(f"BIS turnover CSV lacks columns {sorted(missing)}")
    rows: dict[str, dict] = {}
    for rec in reader:
        if rec["TIME_PERIOD"] != year:
            continue
        for k, v in FIXED.items():
            if rec[k] != v:
                raise ValueError(f"BIS turnover {rec['DER_CURR_LEG1']}: {k}={rec[k]!r}, expected {v!r}")
        ccy = rec["DER_CURR_LEG1"]
        if ccy != TOTAL_CODE and not _CODE.match(ccy):
            raise ValueError(f"BIS turnover: unexpected currency code {ccy!r}")
        if ccy in rows:
            raise ValueError(f"BIS turnover: two rows for {ccy} {year}")
        raw = rec["OBS_VALUE"].strip()
        value = "" if raw.lower() in ("", "nan") else plain_decimal(raw)
        rows[ccy] = {"currency": ccy, "period": year, "value": value, "obs_status": rec["OBS_STATUS"]}
    if TOTAL_CODE not in rows or rows[TOTAL_CODE]["value"] == "":
        raise ValueError(f"BIS turnover {year}: no all-currency total ({TOTAL_CODE})")
    if sum(1 for r in rows.values() if r["value"] != "") < 11:
        raise ValueError(f"BIS turnover {year}: too few currencies with values to rank a top ten")
    return [rows[c] for c in sorted(rows)]


def pull(args: argparse.Namespace | None = None) -> None:
    resp = http_get(endpoint(), params=params())
    text = resp.content.decode("utf-8-sig")
    rows = parse(text)
    write_csv(NAME, FILE_NAME, rows, FIELDNAMES, source_url=source_url(),
              method="BIS SDMX v2 data API (CSV, keyless), dataflow WS_DER_OTC_TOV, one request",
              description="OTC foreign exchange turnover by currency, net-net basis, all instruments, all "
                          "reporting and counterparty countries, daily average in April 2025 (BIS Triennial "
                          "Central Bank Survey 2025)",
              units="USD million per day", survey="BIS Triennial Central Bank Survey 2025",
              reference_period="April 2025 (daily average)", basis="net-net (DER_BASIS C)",
              dataflow=FLOW, key=KEY, fixed_dimensions=FIXED, total_code=TOTAL_CODE,
              published_cross_check={"document": ANNEX_PDF, "table": "Table 25.2 (currency distribution, net-net)",
                                     "landing_page": LANDING_PAGE},
              currencies=sum(1 for r in rows if r["currency"] != TOTAL_CODE and r["currency"] not in NON_CURRENCY),
              not_ranked=sorted({r["currency"] for r in rows} & NON_CURRENCY),
              blank_rows=sum(1 for r in rows if r["value"] == ""),
              path_note="Stored under data/raw/bis_fx/ rather than data/raw/bis/ so "
                        "the monthly freshness rule on data/raw/bis/*.csv does not mark a triennial survey stale.",
              notes=["Each trade involves two currencies, so currency shares of the TO1 total sum to two "
                     "hundred percent.",
                     "Ranking and shares are derived in the models (pipeline.models.load.bis_fx_turnover)."],
              refresh=REFRESH_RULE, response_sha256=hashlib.sha256(resp.content).hexdigest(),
              first_date=f"{SURVEY_YEAR}-04-01", last_date=f"{SURVEY_YEAR}-04-01",
              last_observed_date=f"{SURVEY_YEAR}-04-01")
    ranked = sorted((r for r in rows if r["currency"] != TOTAL_CODE and r["currency"] not in NON_CURRENCY
                     and r["value"]),
                    key=lambda r: -float(r["value"]))
    print(f"[{NAME}] {len(rows)} rows; top by turnover: {', '.join(r['currency'] for r in ranked[:11])}")


if __name__ == "__main__":
    pull()
