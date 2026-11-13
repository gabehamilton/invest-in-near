"""Offline tests for pipeline.sources.intents: cursor walk, aggregation, finality, planning, rate limiting, skip.

Rows are the OpenAPI spec's example object with fake variations. No test touches the network or data/raw/.
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402
import requests  # noqa: E402

from pipeline import config  # noqa: E402
from pipeline.sources import intents  # noqa: E402

EXAMPLE = json.loads((ROOT / "tests/fixtures/intents/transaction_example.json").read_text())["example"]
DAY = date(2025, 12, 31)
DAY_START = 1767139200
FAKE_JWT = "fake.jwt.value-for-tests"


def tx(n: int, day: date = DAY, **overrides) -> dict:
    """A copy of the spec example with a unique deposit address/memo, stamped newest-first inside `day`."""
    row = json.loads(json.dumps(EXAMPLE))
    stamp = int(intents.day_start(day).timestamp()) + 86399 - n
    row.update(depositAddress=f"ADDR{n:04d}", depositMemo=str(n), depositAddressAndMemo=f"ADDR{n:04d}_{n}",
               createdAtTimestamp=stamp,
               createdAt=datetime.fromtimestamp(stamp, timezone.utc).isoformat().replace("+00:00", ".000Z"))
    row.update(overrides)
    return row


class FakeExplorer:
    """Serves rows (newest first) honoring numberOfTransactions and the lastDepositAddress/lastDepositMemo cursor."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.calls: list[dict] = []

    def __call__(self, params: dict[str, str]) -> list[dict]:
        self.calls.append(dict(params))
        start = 0
        if "lastDepositAddress" in params:
            cursor = (params["lastDepositAddress"], params.get("lastDepositMemo"))
            start = 1 + next(i for i, row in enumerate(self.rows)
                             if (row["depositAddress"], row["depositMemo"]) == cursor)
        return [dict(row) for row in self.rows[start:start + int(params["numberOfTransactions"])]]


def tally_of(*pages: list[dict], day: date = DAY) -> intents.DayTally:
    tally = intents.DayTally(day)
    for page in pages:
        tally.add(page)
    return tally


def after_day_end(day: date, hours: float) -> datetime:
    return intents.day_start(day) + timedelta(days=1, hours=hours)


def test_day_window_is_non_inclusive_on_both_ends():
    window = intents.day_window(DAY)
    assert window == {"startTimestampUnix": str(DAY_START - 1), "endTimestampUnix": str(DAY_START + 86400)}
    assert int(window["startTimestampUnix"]) < EXAMPLE["createdAtTimestamp"] < int(window["endTimestampUnix"])
    assert intents.row_day(EXAMPLE) == DAY


def test_first_page_has_no_cursor_and_documented_params():
    params = intents.page_params(DAY, {})
    assert params == {"numberOfTransactions": "1000", "direction": "next",
                      "statuses": "FAILED,PROCESSING,REFUNDED,SUCCESS",
                      "startTimestampUnix": str(DAY_START - 1), "endTimestampUnix": str(DAY_START + 86400)}


def test_next_cursor_uses_last_row_and_omits_null_memo():
    assert intents.next_cursor([tx(1), EXAMPLE]) == {"lastDepositAddress": EXAMPLE["depositAddress"],
                                                     "lastDepositMemo": "12345"}
    assert intents.next_cursor([tx(1, depositMemo=None)]) == {"lastDepositAddress": "ADDR0001"}


def test_fetch_day_walks_cursor_until_short_page(monkeypatch):
    monkeypatch.setattr(intents, "PAGE_SIZE", 2)
    rows = [tx(n) for n in range(1, 6)]
    api = FakeExplorer(rows)
    tally = intents.fetch_day(DAY, api)
    assert len(api.calls) == 3
    assert "lastDepositAddress" not in api.calls[0]
    assert api.calls[1]["lastDepositAddress"] == "ADDR0002" and api.calls[1]["lastDepositMemo"] == "2"
    assert api.calls[2]["lastDepositAddress"] == "ADDR0004" and api.calls[2]["lastDepositMemo"] == "4"
    assert {call["startTimestampUnix"] for call in api.calls} == {str(DAY_START - 1)}
    assert all(call["numberOfTransactions"] == "2" for call in api.calls)
    assert (tally.pages, tally.rows_seen, len(tally.swaps)) == (3, 5, 5)


def test_fetch_day_exact_multiple_ends_on_empty_page(monkeypatch):
    monkeypatch.setattr(intents, "PAGE_SIZE", 2)
    api = FakeExplorer([tx(n) for n in range(1, 5)])
    tally = intents.fetch_day(DAY, api)
    assert len(api.calls) == 3 and (tally.pages, tally.rows_seen) == (3, 4)


def test_fetch_day_null_memo_cursor_sends_address_only(monkeypatch):
    monkeypatch.setattr(intents, "PAGE_SIZE", 2)
    api = FakeExplorer([tx(n, depositMemo=None) for n in range(1, 4)])
    tally = intents.fetch_day(DAY, api)
    assert api.calls[1]["lastDepositAddress"] == "ADDR0002" and "lastDepositMemo" not in api.calls[1]
    assert len(tally.swaps) == 3


def test_fetch_day_stops_when_a_full_page_has_no_row_in_the_day(monkeypatch):
    monkeypatch.setattr(intents, "PAGE_SIZE", 2)
    previous = DAY - timedelta(days=1)
    api = FakeExplorer([tx(1), tx(2), tx(3, day=previous), tx(4, day=previous), tx(5, day=previous)])
    tally = intents.fetch_day(DAY, api)
    assert len(api.calls) == 2
    assert (len(tally.swaps), tally.rows_outside_day) == (2, 2)


def test_fetch_day_refuses_a_cursor_that_does_not_advance(monkeypatch):
    monkeypatch.setattr(intents, "PAGE_SIZE", 2)
    page = [tx(1), tx(2)]
    with pytest.raises(intents.ExplorerError, match="cursor did not advance"):
        intents.fetch_day(DAY, lambda params: [dict(row) for row in page])


def test_fetch_day_refuses_to_store_a_truncated_day(monkeypatch):
    monkeypatch.setattr(intents, "PAGE_SIZE", 1)
    api = FakeExplorer([tx(n) for n in range(1, 10)])
    with pytest.raises(intents.ExplorerError, match="truncated"):
        intents.fetch_day(DAY, api, max_pages=3)


def test_aggregation_counts_statuses_sums_success_usd_and_dedupes():
    page_one = [tx(1),
                tx(2, amountInUsd="100.5", amountOutUsd="100.25", depositType="INTENTS"),
                tx(3, status="REFUNDED", amountInUsd="10", amountOutUsd="0", depositType="CONFIDENTIAL_INTENTS")]
    page_two = [tx(2, amountInUsd="100.5", amountOutUsd="100.25", depositType="INTENTS"),
                tx(4, status="FAILED"),
                tx(5, status="PROCESSING", amountIn="0", amountInUsd="0", amountOut="0", amountOutUsd="0")]
    row, entry = intents.summarize(tally_of(page_one, page_two), after_day_end(DAY, 100))
    assert row == {"date": "2025-12-31", "final": "false", "rows_seen": 6, "swaps_total": 5,
                   "swaps_success": 2, "swaps_refunded": 1, "swaps_failed": 1, "swaps_processing": 1,
                   "volume_in_usd_success": "122.6272", "volume_out_usd_success": "122.3608",
                   "deposit_origin_chain": 3, "deposit_intents": 1, "deposit_confidential": 1}
    assert (entry["pages"], entry["duplicates"], entry["rows_outside_day"]) == (2, 1, 0)
    assert entry["other_statuses"] == {} and entry["other_deposit_types"] == {}


def test_last_seen_copy_of_a_duplicate_wins():
    row, _ = intents.summarize(tally_of([tx(1, status="PROCESSING", amountInUsd="0")], [tx(1)]),
                               after_day_end(DAY, 100))
    assert (row["swaps_total"], row["swaps_success"], row["swaps_processing"]) == (1, 1, 0)
    assert row["volume_in_usd_success"] == "22.1272"


def test_volume_stays_empty_when_a_success_row_lacks_usd():
    row, entry = intents.summarize(tally_of([tx(1), tx(2, amountInUsd=None)]), after_day_end(DAY, 100))
    assert row["volume_in_usd_success"] == ""
    assert row["volume_out_usd_success"] == "44.2216"
    assert entry["success_usd_in_missing"] == 1 and entry["success_usd_out_missing"] == 0


def test_no_success_rows_sum_to_zero_but_an_empty_day_stays_empty():
    row, _ = intents.summarize(tally_of([tx(1, status="FAILED")]), after_day_end(DAY, 100))
    assert row["volume_in_usd_success"] == "0" and row["final"] == "true"
    row, entry = intents.summarize(tally_of([]), after_day_end(DAY, 100))
    assert row == {"date": "2025-12-31", "final": "false", "rows_seen": 0}
    assert entry["final"] is False


def test_rows_outside_the_day_are_counted_only_in_state():
    tally = tally_of([tx(1), tx(2, day=DAY + timedelta(days=1)), tx(3, day=DAY - timedelta(days=1))])
    row, entry = intents.summarize(tally, after_day_end(DAY, 100))
    assert (row["swaps_total"], row["rows_seen"], entry["rows_outside_day"], entry["duplicates"]) == (1, 3, 2, 0)


def test_unknown_status_and_deposit_type_are_reported_not_bucketed():
    row, entry = intents.summarize(tally_of([tx(1, status="PENDING_DEPOSIT", depositType=None)]),
                                   after_day_end(DAY, 100))
    assert row["swaps_total"] == 1 and row["swaps_success"] == 0 and row["deposit_origin_chain"] == 0
    assert entry["other_statuses"] == {"PENDING_DEPOSIT": 1} and entry["other_deposit_types"] == {"None": 1}


def test_final_needs_more_than_72h_since_day_end_and_no_processing():
    settled = tally_of([tx(1), tx(2, status="REFUNDED")])
    assert intents.summarize(settled, after_day_end(DAY, 72))[1]["final"] is False
    assert intents.summarize(settled, after_day_end(DAY, 72) + timedelta(seconds=1))[1]["final"] is True
    in_flight = tally_of([tx(1), tx(2, status="PROCESSING")])
    assert intents.summarize(in_flight, after_day_end(DAY, 500))[0]["final"] == "false"


def test_plan_reuses_final_days_and_always_refreshes_trailing_three():
    today = date(2026, 1, 10)
    window = [date(2026, 1, d) for d in range(3, 10)]
    cached = {day.isoformat(): {"final": "true"} for day in window}
    state = {day.isoformat(): {"final": True} for day in window}
    assert intents.plan(today, 7, cached, state) == [date(2026, 1, 9), date(2026, 1, 8), date(2026, 1, 7)]
    del cached["2026-01-04"]
    state["2026-01-05"]["final"] = False
    assert intents.plan(today, 7, cached, state) == [date(2026, 1, d) for d in (9, 8, 7, 5, 4)]
    assert intents.plan(today, 7, {}, {}) == sorted(window, reverse=True)


def response(status: int, body: object = None, headers: dict | None = None) -> requests.Response:
    resp = requests.Response()
    resp.status_code = status
    resp.encoding = "utf-8"
    resp._content = b"" if body is None else json.dumps(body).encode()
    resp.headers.update(headers or {})
    return resp


def http_error(status: int, body: object = None, headers: dict | None = None) -> requests.HTTPError:
    return requests.HTTPError(f"{status} error", response=response(status, body, headers))


class FakeGet:
    def __init__(self, outcomes: list) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url: str, **kwargs) -> requests.Response:
        self.calls.append((url, kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeTime:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def client_with(outcomes: list) -> tuple[intents.ExplorerClient, FakeGet, FakeTime]:
    get, fake_time = FakeGet(outcomes), FakeTime()
    throttle = intents.Throttle(intents.MIN_INTERVAL_S, sleep=fake_time.sleep, clock=fake_time.clock)
    return intents.ExplorerClient(FAKE_JWT, get=get, throttle=throttle), get, fake_time


def test_rate_limit_wait_prefers_header_then_body_and_never_goes_below_5_2s():
    assert intents.rate_limit_wait(response(429, headers={"Retry-After": "12"})) == 12.0
    assert intents.rate_limit_wait(response(429, headers={"Retry-After": "1"})) == intents.MIN_INTERVAL_S
    body = {"status": 429, "error": {"code": "RATE_LIMITED", "message": "Rate limit exceeded. Retry in 9 seconds"}}
    assert intents.rate_limit_wait(response(429, body)) == 9.0
    body["error"]["message"] = "Too many requests, wait 3500ms"
    assert intents.rate_limit_wait(response(429, body)) == intents.MIN_INTERVAL_S
    assert intents.rate_limit_wait(response(429, {"error": {"retryAfter": 30}})) == 30.0
    assert intents.rate_limit_wait(response(429, {"retryAfter": 10_000})) == intents.MAX_WAIT_S
    assert intents.rate_limit_wait(response(429)) == intents.MIN_INTERVAL_S
    http_date = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=60), usegmt=True)
    assert 55 <= intents.rate_limit_wait(response(429, headers={"Retry-After": http_date})) <= 60


def test_client_retries_429_on_the_same_cursor_and_spaces_requests():
    page = [tx(1)]
    client, get, fake_time = client_with([http_error(429, headers={"Retry-After": "7"}),
                                          response(200, page), response(200, [])])
    params = intents.page_params(DAY, {"lastDepositAddress": "ADDR0000", "lastDepositMemo": "0"})
    assert client.fetch_page(params) == page
    assert client.fetch_page(intents.page_params(DAY, {})) == []
    assert fake_time.sleeps == pytest.approx([7.0, intents.MIN_INTERVAL_S])
    assert get.calls[0] == get.calls[1]
    url, kwargs = get.calls[0]
    assert url == intents.TRANSACTIONS_URL and kwargs["params"] == params and kwargs["retries"] == 0
    assert kwargs["headers"]["Authorization"] == f"Bearer {FAKE_JWT}"
    assert client.requests == 3


def test_client_backs_off_on_5xx_and_gives_up_clearly():
    client, get, fake_time = client_with([http_error(503) for _ in range(intents.MAX_ATTEMPTS)])
    with pytest.raises(intents.ExplorerError, match="giving up after"):
        client.fetch_page(intents.page_params(DAY, {}))
    assert len(get.calls) == intents.MAX_ATTEMPTS
    assert fake_time.sleeps == pytest.approx([intents.backoff(n) for n in range(intents.MAX_ATTEMPTS - 1)])
    assert min(fake_time.sleeps) >= intents.MIN_INTERVAL_S - 1e-9


def test_auth_error_is_not_retried_and_never_leaks_the_token():
    body = {"code": "Bad Request", "error": {"code": "INVALID_TOKEN", "message": "Invalid JWT token. Please "
                                             "generate a new one at https://partners.near-intents.org/"}}
    client, get, _ = client_with([http_error(400, body)])
    with pytest.raises(intents.ExplorerError) as caught:
        client.fetch_page(intents.page_params(DAY, {}))
    assert len(get.calls) == 1
    assert "HTTP 400 INVALID_TOKEN" in str(caught.value) and "NEAR_INTENTS_JWT" in str(caught.value)
    assert FAKE_JWT not in str(caught.value) and FAKE_JWT not in repr(client)


def test_non_array_response_is_rejected():
    client, _, _ = client_with([response(200, {"rows": []})])
    with pytest.raises(intents.ExplorerError, match="JSON array"):
        client.fetch_page(intents.page_params(DAY, {}))


@pytest.fixture
def raw_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "RAW_DIR", tmp_path / "data" / "raw")
    return tmp_path / "data" / "raw"


def day_api(rows_per_day: int = 2, fail_on: date | None = None, processing_on: date | None = None):
    """A fake fetch serving `rows_per_day` SUCCESS rows (plus one PROCESSING row on `processing_on`) per window."""
    calls: list[date] = []

    def fetch(params: dict[str, str]) -> list[dict]:
        day = datetime.fromtimestamp(int(params["startTimestampUnix"]) + 1, timezone.utc).date()
        calls.append(day)
        if day == fail_on:
            raise intents.ExplorerError("HTTP 400 INVALID_TOKEN: test")
        rows = [tx(n, day=day, depositAddress=f"ADDR{day:%m%d}{n}") for n in range(rows_per_day)]
        if day == processing_on:
            rows.append(tx(rows_per_day, day=day, depositAddress=f"ADDR{day:%m%d}p", status="PROCESSING"))
        return rows

    return fetch, calls


def test_run_persists_only_aggregates_and_reuses_final_days(raw_dir, capsys):
    now = datetime(2026, 1, 10, 6, 0, tzinfo=timezone.utc)
    fetch, calls = day_api(processing_on=date(2026, 1, 5))
    done = intents.run(fetch, 7, clock=lambda: now)
    assert done == [date(2026, 1, d) for d in range(9, 2, -1)]
    daily = config.read_csv("intents", "explorer_daily")
    assert list(daily[0]) == intents.FIELDNAMES
    assert [row["date"] for row in daily] == [f"2026-01-0{d}" for d in range(3, 10)]
    assert [row["final"] for row in daily] == ["true", "true", "false", "true", "false", "false", "false"]
    assert daily[2]["swaps_processing"] == "1" and daily[2]["volume_in_usd_success"] == "44.2544"
    assert daily[0]["volume_in_usd_success"] == "44.2544" and daily[0]["swaps_total"] == "2"
    state = json.loads((raw_dir / "intents" / "explorer_state.json").read_text())
    assert (state["window_start"], state["window_end"]) == ("2026-01-03", "2026-01-09")
    assert set(state["days"]["2026-01-03"]) >= {"final", "pages", "fetched_at"}

    calls.clear()
    assert intents.run(fetch, 7, clock=lambda: now) == [date(2026, 1, d) for d in (9, 8, 7, 5)]
    assert calls == [date(2026, 1, d) for d in (9, 8, 7, 5)]

    written = "".join(path.read_text() for path in raw_dir.rglob("*") if path.is_file())
    for raw_value in ("ADDR", "nep141:", "somebody.near", EXAMPLE["intentHashes"], "Bearer"):
        assert raw_value not in written


def test_run_keeps_finished_days_when_a_later_day_fails(raw_dir, capsys):
    now = datetime(2026, 1, 10, 6, 0, tzinfo=timezone.utc)
    fetch, _ = day_api(fail_on=date(2026, 1, 7))
    with pytest.raises(intents.ExplorerError):
        intents.run(fetch, 5, clock=lambda: now)
    assert [row["date"] for row in config.read_csv("intents", "explorer_daily")] == ["2026-01-08", "2026-01-09"]
    assert "2026-01-07 FAILED after 2 completed day(s)" in capsys.readouterr().out


def test_pull_skips_cleanly_without_a_jwt(raw_dir, monkeypatch, capsys):
    monkeypatch.delenv("NEAR_INTENTS_JWT", raising=False)
    monkeypatch.setattr(intents, "ExplorerClient", lambda *a, **k: pytest.fail("client built without a JWT"))
    intents.pull()
    assert "[intents] skipped: set NEAR_INTENTS_JWT" in capsys.readouterr().out
    assert not raw_dir.exists() or not any(raw_dir.rglob("*"))


def test_intents_days_defaults_to_30_and_rejects_bad_values(monkeypatch):
    monkeypatch.delenv("INTENTS_DAYS", raising=False)
    assert intents.intents_days() == 30
    monkeypatch.setenv("INTENTS_DAYS", "7")
    assert intents.intents_days() == 7
    for bad in ("0", "-3", "thirty"):
        monkeypatch.setenv("INTENTS_DAYS", bad)
        with pytest.raises(ValueError, match="INTENTS_DAYS"):
            intents.intents_days()
