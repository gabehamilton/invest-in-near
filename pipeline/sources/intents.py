"""NEAR Intents Explorer API: per-UTC-day aggregates of 1Click swaps (status mix, USD volume, deposit type).

Provenance: GET https://explorer.near-intents.org/api/v0/transactions, documented by the OpenAPI spec at
https://explorer.near-intents.org/api/v0/openapi.yaml. Auth is a partner JWT from https://partners.near-intents.org/
sent as `Authorization: Bearer <NEAR_INTENTS_JWT>` (this host rejects X-API-Key). Scope is 1Click swaps only, test
transactions excluded (showTestTxs unset). The spec says unfiltered results can include "masking" rows; no
swap-only filter is applied, so those rows, if any, are counted.

Walk: one UTC day D at a time, newest first. Both timestamp filters are non-inclusive, so the window is
startTimestampUnix = D 00:00 - 1 and endTimestampUnix = D 00:00 + 86400. Each page asks for
numberOfTransactions=1000, direction=next (older) and statuses=FAILED,PROCESSING,REFUNDED,SUCCESS (the documented
default, sent explicitly). The next page's cursor is the last row's depositAddress/depositMemo as
lastDepositAddress/lastDepositMemo (memo omitted when null). A page shorter than 1000 rows, or one with no row in D,
ends the day. Requests start at least 5.2 s after the previous response (limit: 1 per 5 s per partner). A 429 waits
for Retry-After or the wait stated in the body (never less than 5.2 s) and retries the same cursor; 5xx and network
errors back off exponentially. Other errors stop the run.

Aggregation (in memory, per day): rows are deduped on depositAddress|depositMemo (last seen wins) and placed by the
UTC day of createdAtTimestamp; rows outside D are counted in the state, never in D. swaps_* count rows by status.
volume_in_usd_success / volume_out_usd_success sum amountInUsd / amountOutUsd over SUCCESS rows as exact decimals and
stay empty if any SUCCESS row lacks a parseable value (the state records how many). deposit_* count rows by
depositType. rows_seen is every row returned for D's window, before dedupe. A day with no rows in D gets empty
metrics (no coverage and no activity look the same) and is never final.

Terms: the 1ClickSwap API Terms 4.1(e) forbid storing API data for more than 24 hours, so rows never leave memory.
Only the daily aggregates and a small state file are written. No cursor inside a day is persisted, since resuming
a day would require storing row identifiers; the day is the unit of resumption.

Refresh rule: the window is the last INTENTS_DAYS (default 30) full UTC days. A day is final once its end is more than
72 h in the past and it has no PROCESSING rows. Final days are reused from cache; every other day in the window,
always including the trailing 3 days, is re-fetched and overwritten. Cached days outside the window are kept. Both
files are rewritten after each completed day, so an interrupted or failed run keeps every finished day.

Output: data/raw/intents/explorer_daily.csv and data/raw/intents/explorer_state.json.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from typing import Callable

import requests

from pipeline.config import env, http_get, raw_path, read_csv, write_csv, write_json

NAME = "intents"
REQUIRES = ["NEAR_INTENTS_JWT"]

TRANSACTIONS_URL = "https://explorer.near-intents.org/api/v0/transactions"
SPEC_URL = "https://explorer.near-intents.org/api/v0/openapi.yaml"
DAILY = "explorer_daily"
STATE = "explorer_state"

PAGE_SIZE = 1000
STATUSES = ("FAILED", "PROCESSING", "REFUNDED", "SUCCESS")
MIN_INTERVAL_S = 5.2
MAX_WAIT_S = 300.0
MAX_ATTEMPTS = 8
TIMEOUT_S = 90.0
MAX_PAGES_PER_DAY = 1000
DEFAULT_DAYS = 30
OPEN_DAYS = 3
FINAL_AFTER = timedelta(hours=72)
DAY_SECONDS = 86400

STATUS_COLUMNS = {"SUCCESS": "swaps_success", "REFUNDED": "swaps_refunded", "FAILED": "swaps_failed",
                  "PROCESSING": "swaps_processing"}
DEPOSIT_COLUMNS = {"ORIGIN_CHAIN": "deposit_origin_chain", "INTENTS": "deposit_intents",
                   "CONFIDENTIAL_INTENTS": "deposit_confidential"}
FIELDNAMES = ["date", "final", "swaps_total", *STATUS_COLUMNS.values(), "volume_in_usd_success",
              "volume_out_usd_success", *DEPOSIT_COLUMNS.values(), "rows_seen"]
RETRY_STATUSES = {500, 502, 503, 504}
WAIT_IN_TEXT = re.compile(r"(\d+(?:\.\d+)?)\s*(ms|milliseconds?|s|secs?|seconds?)\b", re.IGNORECASE)

Fetch = Callable[[dict[str, str]], list[dict]]

METHOD = {
    "params": {"numberOfTransactions": PAGE_SIZE, "direction": "next", "statuses": ",".join(STATUSES)},
    "window": "per UTC day D: startTimestampUnix = D-1, endTimestampUnix = D+86400 (both non-inclusive)",
    "cursor": "lastDepositAddress/lastDepositMemo of the last row of the previous page (memo omitted when null); "
              "a short page or a page with no row in D ends the day",
    "rate_limit": f">= {MIN_INTERVAL_S} s between a response and the next request; 429 honors Retry-After or the body",
    "dedupe": "depositAddress|depositMemo within a day, last seen wins",
    "day_assignment": "UTC day of createdAtTimestamp; rows outside D are counted in the state only",
    "column_definitions": {
        "final": "true once the day ended more than 72 h before it was fetched and had no PROCESSING rows",
        "swaps_total": "unique rows in the day (all returned statuses)",
        "swaps_success/refunded/failed/processing": "unique rows by status",
        "volume_in_usd_success": "sum of amountInUsd over SUCCESS rows; empty if any SUCCESS row lacks it",
        "volume_out_usd_success": "sum of amountOutUsd over SUCCESS rows; empty if any SUCCESS row lacks it",
        "deposit_origin_chain/intents/confidential": "unique rows by depositType "
                                                     "ORIGIN_CHAIN/INTENTS/CONFIDENTIAL_INTENTS",
        "rows_seen": "rows returned for the day's window across all pages, before dedupe",
    },
    "empty_day": "a day with no rows has empty metrics and is never final",
    "scope": "1Click swaps only (not all NEAR Intents activity); test transactions excluded; masking rows, if "
             "returned, are not filtered; in-flight swaps read 0 USD until settled",
    "terms": "1ClickSwap API Terms 4.1(e): raw rows are never persisted, only daily aggregates and state",
    "incremental": "last INTENTS_DAYS full UTC days; final days reused from cache, all others (always the trailing "
                   "3 days) re-fetched; cached days outside the window kept",
}


class ExplorerError(RuntimeError):
    """The Explorer API refused or failed a request in a way that retrying will not fix."""


@dataclass(frozen=True)
class Swap:
    status: str | None
    deposit_type: str | None
    usd_in: Decimal | None
    usd_out: Decimal | None


@dataclass
class DayTally:
    day: date
    pages: int = 0
    rows_seen: int = 0
    rows_outside_day: int = 0
    swaps: dict[str, Swap] = field(default_factory=dict)

    def add(self, rows: list[dict]) -> int:
        """Fold one page into the tally and return how many of its rows belong to this day."""
        self.pages += 1
        self.rows_seen += len(rows)
        inside = 0
        for row in rows:
            if row_day(row) != self.day:
                self.rows_outside_day += 1
                continue
            inside += 1
            self.swaps[row_key(row)] = Swap(row.get("status"), row.get("depositType"),
                                            parse_usd(row.get("amountInUsd")), parse_usd(row.get("amountOutUsd")))
        return inside


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def day_start(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)


def day_window(day: date) -> dict[str, str]:
    start = int(day_start(day).timestamp())
    return {"startTimestampUnix": str(start - 1), "endTimestampUnix": str(start + DAY_SECONDS)}


def page_params(day: date, cursor: dict[str, str]) -> dict[str, str]:
    return {"numberOfTransactions": str(PAGE_SIZE), "direction": "next", "statuses": ",".join(STATUSES),
            **day_window(day), **cursor}


def deposit_address(row: dict) -> str:
    address = row.get("depositAddress")
    if not isinstance(address, str) or not address:
        raise ExplorerError("row without depositAddress; cannot dedupe or paginate")
    return address


def row_key(row: dict) -> str:
    memo = row.get("depositMemo")
    return f"{deposit_address(row)}|{'' if memo is None else memo}"


def next_cursor(page: list[dict]) -> dict[str, str]:
    last = page[-1]
    cursor = {"lastDepositAddress": deposit_address(last)}
    if last.get("depositMemo") is not None:
        cursor["lastDepositMemo"] = str(last["depositMemo"])
    return cursor


def row_day(row: dict) -> date:
    stamp = row.get("createdAtTimestamp")
    if isinstance(stamp, (int, float)) and not isinstance(stamp, bool):
        return datetime.fromtimestamp(stamp, timezone.utc).date()
    try:
        created = datetime.fromisoformat(str(row.get("createdAt")))
    except ValueError:
        raise ExplorerError("row without a usable createdAtTimestamp or createdAt") from None
    return (created if created.tzinfo else created.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).date()


def parse_usd(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def decimal_sum(values: list[Decimal | None]) -> str:
    if any(value is None for value in values):
        return ""
    return format(sum(values, Decimal(0)), "f")


def fetch_day(day: date, fetch: Fetch, max_pages: int = MAX_PAGES_PER_DAY) -> DayTally:
    """Walk every page of one UTC day's window and return its in-memory tally."""
    tally = DayTally(day)
    cursor: dict[str, str] = {}
    while True:
        page = fetch(page_params(day, cursor))
        inside = tally.add(page)
        if len(page) < PAGE_SIZE or inside == 0:
            return tally
        following = next_cursor(page)
        if following == cursor:
            raise ExplorerError(f"{day}: cursor did not advance after page {tally.pages}")
        if tally.pages >= max_pages:
            raise ExplorerError(f"{day}: more than {max_pages} pages; refusing to store a truncated day")
        cursor = following


def summarize(tally: DayTally, now: datetime) -> tuple[dict, dict]:
    """Return the CSV row and the state entry for a fully walked day."""
    swaps = list(tally.swaps.values())
    statuses = Counter(swap.status for swap in swaps)
    deposits = Counter(swap.deposit_type for swap in swaps)
    success = [swap for swap in swaps if swap.status == "SUCCESS"]
    old_enough = now - (day_start(tally.day) + timedelta(days=1)) > FINAL_AFTER
    final = bool(swaps) and statuses["PROCESSING"] == 0 and old_enough
    row: dict = {"date": tally.day.isoformat(), "final": "true" if final else "false", "rows_seen": tally.rows_seen}
    if swaps:
        row.update({"swaps_total": len(swaps),
                    **{column: statuses[status] for status, column in STATUS_COLUMNS.items()},
                    "volume_in_usd_success": decimal_sum([swap.usd_in for swap in success]),
                    "volume_out_usd_success": decimal_sum([swap.usd_out for swap in success]),
                    **{column: deposits[kind] for kind, column in DEPOSIT_COLUMNS.items()}})
    entry = {
        "final": final,
        "pages": tally.pages,
        "fetched_at": now.replace(microsecond=0).isoformat(),
        "rows_seen": tally.rows_seen,
        "rows_outside_day": tally.rows_outside_day,
        "duplicates": tally.rows_seen - tally.rows_outside_day - len(swaps),
        "success_usd_in_missing": sum(swap.usd_in is None for swap in success),
        "success_usd_out_missing": sum(swap.usd_out is None for swap in success),
        "other_statuses": {str(k): v for k, v in statuses.items() if k not in STATUS_COLUMNS},
        "other_deposit_types": {str(k): v for k, v in deposits.items() if k not in DEPOSIT_COLUMNS},
    }
    return row, entry


def window_days(today: date, n_days: int) -> list[date]:
    return [today - timedelta(days=offset) for offset in range(n_days, 0, -1)]


def plan(today: date, n_days: int, cached: dict[str, dict], state_days: dict[str, dict]) -> list[date]:
    """Days to fetch, newest first: every non-final day in the window plus the trailing OPEN_DAYS."""
    reopen_from = today - timedelta(days=OPEN_DAYS)

    def reusable(day: date) -> bool:
        key = day.isoformat()
        return state_days.get(key, {}).get("final") is True and cached.get(key, {}).get("final") == "true"

    return sorted((day for day in window_days(today, n_days) if day >= reopen_from or not reusable(day)),
                  reverse=True)


def load_state() -> dict:
    path = raw_path(NAME, STATE, "json")
    state = json.loads(path.read_text()) if path.exists() else {}
    return {"window_start": state.get("window_start"), "window_end": state.get("window_end"),
            "days": dict(state.get("days") or {})}


def save(rows: dict[str, dict], state: dict, run_info: dict) -> None:
    ordered = [rows[key] for key in sorted(rows)]
    finals = sum(row.get("final") == "true" for row in ordered)
    write_csv(NAME, DAILY, ordered, FIELDNAMES, source_url=TRANSACTIONS_URL, spec_url=SPEC_URL,
              description="Per-UTC-day aggregates of NEAR Intents 1Click swaps from the Explorer API",
              first_date=ordered[0]["date"] if ordered else None, last_date=ordered[-1]["date"] if ordered else None,
              days_final=finals, days_open=len(ordered) - finals, run=run_info, **METHOD)
    write_json(NAME, STATE, state, source_url=TRANSACTIONS_URL,
               description="Explorer pull state: window of the last run and per-day finality, pages and diagnostics")


def run(fetch: Fetch, n_days: int, clock: Callable[[], datetime] = utc_now) -> list[date]:
    """Fetch every planned day through `fetch`, saving after each one; return the days fetched."""
    started = clock()
    days = window_days(started.date(), n_days)
    rows = {row["date"]: row for row in read_csv(NAME, DAILY)}
    state = load_state()
    state["window_start"], state["window_end"] = days[0].isoformat(), days[-1].isoformat()
    todo = plan(started.date(), n_days, rows, state["days"])
    done: list[date] = []
    run_info = {"started_at": started.replace(microsecond=0).isoformat(), "intents_days": n_days,
                "window_start": state["window_start"], "window_end": state["window_end"],
                "planned_days": [day.isoformat() for day in todo], "completed_days": []}
    print(f"[{NAME}] window {days[0]}..{days[-1]}: {len(todo)} day(s) to fetch, "
          f"{n_days - len(todo)} final day(s) reused")
    for day in todo:
        try:
            tally = fetch_day(day, fetch)
        except Exception as exc:
            print(f"[{NAME}] {day} FAILED after {len(done)} completed day(s); finished days are saved: {exc}")
            raise
        row, entry = summarize(tally, clock())
        rows[row["date"]], state["days"][row["date"]] = row, entry
        done.append(day)
        run_info["completed_days"] = [finished.isoformat() for finished in done]
        save(rows, state, run_info)
        outside = f", {tally.rows_outside_day} outside the day" if tally.rows_outside_day else ""
        print(f"[{NAME}] {day}: {tally.pages} page(s), {tally.rows_seen} rows, {len(tally.swaps)} unique"
              f"{outside}, final={entry['final']}")
    if not done:
        save(rows, state, run_info)
    return done


def header_wait(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        pass
    try:
        moment = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    moment = moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
    return (moment - utc_now()).total_seconds()


def body_wait(resp: requests.Response) -> float | None:
    try:
        body = resp.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    error = body.get("error") if isinstance(body.get("error"), dict) else {}
    for container in (body, error):
        for key in ("retryAfter", "retry_after"):
            value = container.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
    match = WAIT_IN_TEXT.search(str(error.get("message") or body.get("message") or ""))
    if not match:
        return None
    amount = float(match.group(1))
    return amount / 1000 if match.group(2).lower().startswith("m") else amount


def rate_limit_wait(resp: requests.Response) -> float:
    wait = header_wait(resp.headers.get("Retry-After"))
    if wait is None:
        wait = body_wait(resp)
    return min(max(wait if wait is not None else MIN_INTERVAL_S, MIN_INTERVAL_S), MAX_WAIT_S)


def backoff(attempt: int) -> float:
    return min(MIN_INTERVAL_S * 2 ** attempt, MAX_WAIT_S)


def describe_error(resp: requests.Response | None) -> str:
    if resp is None:
        return "no response"
    try:
        body = resp.json()
    except ValueError:
        body = {}
    error = body.get("error") if isinstance(body, dict) and isinstance(body.get("error"), dict) else {}
    code, message = str(error.get("code") or ""), str(error.get("message") or resp.reason or "")[:300]
    label = f"HTTP {resp.status_code} {code}".strip()
    hint = ""
    if resp.status_code == 401 or "TOKEN" in code:
        hint = " (check NEAR_INTENTS_JWT in .env; tokens are issued at https://partners.near-intents.org/)"
    return f"{label}: {message}{hint}"


class Throttle:
    """Keeps at least `interval` seconds between the end of one request and the start of the next."""

    def __init__(self, interval: float = MIN_INTERVAL_S, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.interval, self._sleep, self._clock = interval, sleep, clock
        self._ready_at = float("-inf")

    def wait(self) -> None:
        delay = self._ready_at - self._clock()
        if delay > 0:
            self._sleep(delay)

    def done(self, extra_wait: float = 0.0) -> None:
        self._ready_at = self._clock() + max(self.interval, extra_wait)


class ExplorerClient:
    """Rate-limited GET /transactions. Holds the JWT only in a request header; never logs it."""

    def __init__(self, jwt: str, get: Callable[..., requests.Response] = http_get,
                 throttle: Throttle | None = None) -> None:
        self._headers = {"Authorization": f"Bearer {jwt}", "Accept": "application/json"}
        self._get = get
        self._throttle = throttle or Throttle()
        self.requests = 0

    def __repr__(self) -> str:
        return f"ExplorerClient(requests={self.requests})"

    def fetch_page(self, params: dict[str, str]) -> list[dict]:
        problem = "no attempt made"
        for attempt in range(MAX_ATTEMPTS):
            self._throttle.wait()
            self.requests += 1
            try:
                resp = self._get(TRANSACTIONS_URL, params=params, headers=self._headers, retries=0, timeout=TIMEOUT_S)
            except requests.HTTPError as exc:
                problem = describe_error(exc.response)
                status = exc.response.status_code if exc.response is not None else None
                if status == 429:
                    self._throttle.done(rate_limit_wait(exc.response))
                elif status in RETRY_STATUSES:
                    self._throttle.done(backoff(attempt))
                else:
                    self._throttle.done()
                    raise ExplorerError(problem) from None
                continue
            except (requests.ConnectionError, requests.Timeout) as exc:
                problem = f"{type(exc).__name__}: {exc}"[:300]
                self._throttle.done(backoff(attempt))
                continue
            self._throttle.done()
            return parse_page(resp)
        raise ExplorerError(f"giving up after {MAX_ATTEMPTS} attempts: {problem}")


def parse_page(resp: requests.Response) -> list[dict]:
    try:
        payload = resp.json()
    except ValueError:
        raise ExplorerError("response was not JSON") from None
    if not isinstance(payload, list) or not all(isinstance(row, dict) for row in payload):
        raise ExplorerError(f"expected a JSON array of transactions, got {type(payload).__name__}")
    return payload


def intents_days() -> int:
    value = env("INTENTS_DAYS", str(DEFAULT_DAYS))
    try:
        days = int(value)
    except ValueError:
        raise ValueError(f"INTENTS_DAYS must be a whole number of days, got {value!r}") from None
    if days < 1:
        raise ValueError(f"INTENTS_DAYS must be at least 1, got {days}")
    return days


def pull(args: argparse.Namespace | None = None) -> None:
    jwt = env("NEAR_INTENTS_JWT")
    if not jwt:
        print(f"[{NAME}] skipped: set NEAR_INTENTS_JWT in .env (cached raw files kept)")
        return
    n_days = intents_days()
    client = ExplorerClient(jwt)
    try:
        run(client.fetch_page, n_days)
    finally:
        print(f"[{NAME}] {client.requests} request(s) to {TRANSACTIONS_URL}")


if __name__ == "__main__":
    pull()
