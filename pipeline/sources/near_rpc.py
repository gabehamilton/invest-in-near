"""NEAR mainnet state read straight from JSON-RPC: monthly archival samples, a live snapshot and recent epoch mints.

Provenance
- History (rpc/history_samples.csv) comes from the archival node https://archival-rpc.mainnet.fastnear.com, with
  the deprecated https://archival-rpc.mainnet.near.org as a fallback. Each target instant is 00:00:00 UTC on the 1st
  of every month from 2020-11-01, plus 2020-10-15, 2025-10-29 and 2025-11-15. The sampled block is the last block
  whose header.timestamp_nanosec <= the target. It is found by interpolation search (bisection when interpolation
  stalls), and heights with no block are skipped. One more row is the latest final block from
  https://rpc.mainnet.fastnear.com (fallback https://rpc.mainnet.near.org), dated by its UTC day. That row is
  omitted when its day is itself a sampling date, so `date` stays unique and always means the 00:00 sample there.
  Per sampled block:
    total_supply        block header total_supply (yocto -> NEAR)
    validator stake     `validators` with block_id = header.epoch_id. That hash is the last block of epoch N-2, so
                        stake and n_validators lag the sample by about 2 epochs. Early epochs answer UNKNOWN_EPOCH,
                        so those cells stay empty.
    staked_ratio        validator stake / total_supply at the sampled block
    LST prices          meta-pool.near get_st_near_price, linear-protocol.near ft_price (yocto NEAR per token -> NEAR)
    treasury            view_account treasury.near amount + locked
    protocol            EXPERIMENTAL_protocol_config {block_id}: protocol_version, max_inflation_rate as a decimal
- Epoch mints (rpc/epoch_mints.csv) walk back from the tip one epoch at a time. header.next_epoch_id is the hash of
  the last block of the previous epoch, and the boundary is the first block after it (supply jumps there). Mint =
  supply(boundary) - supply(last block before it) + sum(balance_burnt of chunks included in the boundary block).
  On ordinary blocks that sum exactly cancels the supply drop. The mint at the first block of epoch E pays for
  epoch E-1. It equals max_inflation_rate x total_supply(last block of E-1) x duration(E-1) / 31,536,000 s, reduced
  only for validators below the online threshold. This was checked on 2026-11-10 against the treasury.near credit x 10
  (nearcore reward_calculator.rs), within 10 yoctoNEAR. So epoch_seconds is the duration of the epoch that just ended.
  The file keeps every boundary from 00:00 UTC EPOCH_MINTS_DAYS (737: two years and a week) before the latest
  full-day dense sample date to the tip, so the exact burn covers both the trailing year and the year before it. Each row carries prev_boundary_height, the boundary before it, so the rows form one checked chain; the walk
  reuses linked cached rows without calls and writes progress as it goes, so it is incremental and resumable.
- The snapshot (rpc/snapshot_now.json) pins every view call to the same latest final block.
- Dense samples (rpc/dense_samples.csv): total_supply, stNEAR and LiNEAR prices at 00:00 UTC (same block rule as the
  history) every Monday from 2022-04-04, then every day for the 400 days before the tip's UTC date (today excluded).
  The search starts from the nearest known samples and extrapolates the block rate, so a row costs a few calls.
- Protocol changes (rpc/protocol_changes.csv): max_inflation_rate and runtime_config.transaction_costs.
  burnt_gas_reward are read at every history target height and the tip (rpc/protocol_config_samples.csv); each
  interval whose values differ is binary-searched to the exact first block with the new value.
- Epoch history (rpc/epoch_history.csv): the complete epoch containing each history target sample, with the mint
  paid for it at the next boundary and the fees burnt during it (supply falls inside an epoch only by the burn).
- RPC_DENSE=0 skips those three outputs. All three are append-only and resumable.

RPC reports protocol_reward_rate [0, 1], but nearcore hard-codes the mainnet treasury share to 1/10. This module
records what RPC says and does not reinterpret it.
FastNEAR rate-limits with HTTP 429 / code -429, sometimes for minutes at a time. Requests are paced (1 s, widened on
429), retried with exponential backoff (~2 min per endpoint), then sent to the fallback endpoint. History rows are
written after every date, so an interrupted run keeps them.
"""

from __future__ import annotations

import argparse
import base64
import json
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from decimal import Context, Decimal
from typing import Any, Callable

import requests

from pipeline.config import env, http_post_json, read_csv, read_meta, write_csv, write_json

NAME = "near_rpc"
REQUIRES: list[str] = []
DIR = "rpc"

ARCHIVAL_ENDPOINTS = ["https://archival-rpc.mainnet.fastnear.com", "https://archival-rpc.mainnet.near.org"]
LATEST_ENDPOINTS = ["https://rpc.mainnet.fastnear.com", "https://rpc.mainnet.near.org"]

FIRST_MONTH = date(2020, 11, 1)
EXTRA_DATES = ("2020-10-15", "2025-10-29", "2025-11-15")
# epoch_mints keeps every boundary from 00:00 UTC this many days before the latest full-day dense sample date: the
# models' trailing year (365 days) plus a week of slack for a dense pull that lags the tip.
EPOCH_MINTS_DAYS = 737  # two trailing years plus a week's margin
EPOCH_PROGRESS_EVERY = 25  # epoch_mints.csv is rewritten after this many newly fetched boundaries
TOP_POOLS = 25
SECONDS_PER_YEAR = 365 * 24 * 60 * 60  # nearcore NUM_SECONDS_IN_A_YEAR; protocol config num_blocks_per_year
TIP_MARGIN_NS = 600 * 10**9  # targets must be this far behind the tip so the archival node surely has the blocks

TREASURY = "treasury.near"
METAPOOL = "meta-pool.near"
LINEAR = "linear-protocol.near"
FT_CONTRACTS = [
    "usdt.tether-token.near",
    "17208628f84f5d6ad33f0da3bbbeb27ffcb398eac501a31bd6ad2011e36133a1",
    "a0b86991c6218b36c1d19d4a2e9eb0ce3606eb48.factory.bridge.near",
    "dac17f958d2ee523a2206206994597c13d831ec7.factory.bridge.near",
    METAPOOL,
    LINEAR,
    "lst.rhealab.near",
    "v2-nearx.stader-labs.near",
]
METAPOOL_AMOUNTS = {"nslp_liquidity": "nslp_liquidity_near", "nslp_target": "nslp_target_near",
                    "total_for_staking": "total_for_staking_near",
                    "total_actually_staked": "total_actually_staked_near",
                    "total_unstaked_and_waiting": "total_unstaked_and_waiting_near",
                    "total_stake_shares": "stnear_supply"}
METAPOOL_COUNTS = ("nslp_current_discount_basis_points", "nslp_min_discount_basis_points",
                   "nslp_max_discount_basis_points", "env_epoch_height", "accounts_count", "staking_pools_count")

HISTORY_FIELDS = ["date", "height", "block_time_utc", "epoch_id", "total_supply_near", "validator_stake_near",
                  "n_validators", "stake_epoch_start_height", "staked_ratio", "stnear_price", "linear_price",
                  "treasury_near", "protocol_version", "max_inflation_rate"]
EPOCH_FIELDS = ["boundary_height", "block_time_utc", "minted_near", "epoch_seconds", "supply_before_near",
                "annualized_mint_rate", "prev_boundary_height"]

# Dense samples, protocol-parameter changes and per-sample epoch history (skipped with RPC_DENSE=0).
DENSE_PARTS = ("changes", "epoch_history", "dense")
DEFAULT_PARTS = ",".join(("history", "epochs", "snapshot") + DENSE_PARTS)
DENSE_FIRST = date(2022, 4, 1)  # weekly Mondays start at the first Monday on/after this date
DENSE_DAILY_DAYS = 400  # every day for this many days before the tip's UTC date (today excluded)
DENSE_FIELDS = ["date", "height", "block_time_utc", "total_supply_near", "stnear_price", "linear_price"]
CONFIG_SAMPLE_FIELDS = ["height", "date", "protocol_version", "max_inflation_rate", "burnt_gas_reward"]
CHANGE_FIELDS = ["param", "old_value", "new_value", "first_height", "block_time_utc", "protocol_version",
                 "prev_height"]
EPOCH_HISTORY_FIELDS = ["sample_date", "epoch_id", "start_height", "start_time_utc", "end_height", "end_time_utc",
                        "next_start_height", "epoch_seconds", "supply_start_near", "supply_end_near",
                        "minted_next_boundary_near", "burnt_in_epoch_near", "annualized_mint_rate",
                        "annualized_burn_rate", "protocol_version", "max_inflation_rate"]
# EXPERIMENTAL_protocol_config paths of the tracked parameters (each a [numerator, denominator] pair).
TRACKED_PARAMS = {"max_inflation_rate": ("max_inflation_rate",),
                  "burnt_gas_reward": ("runtime_config", "transaction_costs", "burnt_gas_reward")}
EPOCH_LENGTH = 43200  # mainnet epoch_length in every config since genesis; the live config value is used when present

# Errors that answer the question ("this does not exist here"), so retrying elsewhere cannot help.
DEFINITIVE_CAUSES = {"UNKNOWN_BLOCK", "UNKNOWN_ACCOUNT", "UNKNOWN_EPOCH", "VALIDATOR_INFO_UNAVAILABLE",
                     "CONTRACT_EXECUTION_ERROR", "NO_CONTRACT_CODE", "UNKNOWN_CHUNK", "INVALID_ACCOUNT"}
# Errors that mean this endpoint cannot serve the request (pruned, lagging); move to the next endpoint at once.
ENDPOINT_CAUSES = {"GARBAGE_COLLECTED_BLOCK", "NOT_SYNCED_YET"}

_CTX = Context(prec=80)
_YOCTO = Decimal(10) ** 24
_RATIO_PLACES = Decimal("1e-15")
_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class RpcError(Exception):
    """A definitive JSON-RPC answer: the block, account, epoch or method does not exist."""

    def __init__(self, cause: str, detail: str) -> None:
        super().__init__(f"{cause}: {detail}")
        self.cause = cause


class RpcUnavailable(RuntimeError):
    """Every endpoint kept failing (rate limit, HTTP or network error) after retries."""


class Rpc:
    """Paced JSON-RPC client over an ordered endpoint list, with a per-run block cache."""

    def __init__(self, endpoints: list[str], *, interval: float = 1.0, attempts: int = 8,
                 max_backoff: float = 60.0, timeout: float = 60.0,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.endpoints = endpoints
        self.base_interval = interval
        self.interval = interval
        self.attempts = attempts
        self.max_backoff = max_backoff
        self.timeout = timeout
        self.sleep = sleep
        self.stats: Counter = Counter()
        self._last = 0.0
        self._blocks: dict[int | str, dict | None] = {}

    def call(self, method: str, params: dict | list) -> Any:
        payload = {"jsonrpc": "2.0", "id": NAME, "method": method, "params": params}
        failures: list[str] = []
        for url in self.endpoints:
            for attempt in range(self.attempts):
                self._pace()
                self.stats["calls"] += 1
                outcome, value = self._post(url, payload)
                if outcome == "ok":
                    self.interval = max(self.base_interval, self.interval * 0.9)
                    if url != self.endpoints[0]:
                        self.stats["fallback_ok"] += 1
                    return value
                if outcome == "definitive":
                    raise value
                failures.append(f"{url}: {value}")
                if outcome == "endpoint":
                    break
                if outcome == "rate_limited":
                    self.stats["rate_limited"] += 1
                    self.interval = min(self.interval * 1.5, 10.0)
                if attempt + 1 < self.attempts:
                    self.sleep(min(self.max_backoff, 2.0 ** attempt))
        raise RpcUnavailable(f"{method} {json.dumps(params)[:120]} failed on every endpoint; last: {failures[-1]}")

    def _pace(self) -> None:
        wait = self._last + self.interval - time.monotonic()
        if wait > 0:
            self.sleep(wait)
        self._last = time.monotonic()

    def _post(self, url: str, payload: dict) -> tuple[str, Any]:
        try:
            body = http_post_json(url, payload, retries=0, timeout=self.timeout)
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status == 429:
                return "rate_limited", "HTTP 429"
            # FastNEAR answers e.g. UNKNOWN_BLOCK with HTTP 422 and a JSON-RPC error body: trust a definitive body.
            body = json_error_body(exc.response)
            if body is not None and classify(body)[0] in ("definitive", "rate_limited"):
                return classify(body)
            return ("endpoint" if status in (401, 402, 403, 404, 410) else "transient"), f"HTTP {status}"
        except requests.RequestException as exc:
            return "transient", type(exc).__name__
        return classify(body)

    def block(self, block_id: int | str) -> dict | None:
        """Slim block view (header + chunk burn) by height or hash, or None when no block exists at that height."""
        if block_id not in self._blocks:
            try:
                result = self.call("block", {"block_id": block_id})
            except RpcError as exc:
                if exc.cause != "UNKNOWN_BLOCK":
                    raise
                self.stats["missing_heights"] += 1
                self._blocks[block_id] = None
            else:
                slim = slim_block(result)
                self._blocks[block_id] = slim
                self._blocks[slim["header"]["height"]] = slim
                self._blocks[slim["header"]["hash"]] = slim
        return self._blocks[block_id]

    def block_after(self, height: int, limit: int | None = None, max_scan: int = 200) -> dict | None:
        """First existing block at a height >= `height` (and < `limit`), skipping heights that have no block."""
        end = height + max_scan if limit is None else min(limit, height + max_scan)
        for candidate in range(height, end):
            found = self.block(candidate)
            if found is not None:
                return found
        if limit is not None and end >= limit:
            return None
        raise RpcUnavailable(f"no block in heights {height}..{end - 1}; refusing to scan further")

    def view(self, account: str, method: str, block_id: int, args: dict | None = None) -> Any:
        """Call a view method at a block; None when the account, code or method does not exist there."""
        params = {"request_type": "call_function", "block_id": block_id, "account_id": account,
                  "method_name": method, "args_base64": base64.b64encode(json.dumps(args or {}).encode()).decode()}
        try:
            result = self.call("query", params)
        except RpcError:
            return None
        return json.loads(bytes(result["result"]).decode())

    def account_near(self, account: str, block_id: int) -> Decimal | None:
        try:
            result = self.call("query", {"request_type": "view_account", "block_id": block_id, "account_id": account})
        except RpcError:
            return None
        return near(int(result["amount"]) + int(result["locked"]))


def json_error_body(response: requests.Response | None) -> dict | None:
    """The JSON-RPC error object carried by a non-2xx HTTP response, if there is one."""
    if response is None:
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) and isinstance(body.get("error"), dict) else None


def classify(body: dict) -> tuple[str, Any]:
    """Map a JSON-RPC response body to ok / definitive / endpoint / rate_limited / transient."""
    error = body.get("error")
    if error is None:
        result = body.get("result")
        if isinstance(result, dict) and "error" in result and "result" not in result:
            return "definitive", RpcError("CONTRACT_EXECUTION_ERROR", str(result["error"])[:200])
        return "ok", result
    message = str(error.get("message", ""))
    if error.get("code") == -429 or "rate limit" in message.lower():
        return "rate_limited", message[:120]
    cause = (error.get("cause") or {}).get("name") or error.get("name") or str(error.get("code"))
    detail = str(error.get("data") or message)[:200]
    if cause in DEFINITIVE_CAUSES:
        return "definitive", RpcError(cause, detail)
    if cause in ENDPOINT_CAUSES:
        return "endpoint", f"{cause}: {detail}"
    return "transient", f"{cause}: {detail}"


def slim_block(result: dict) -> dict:
    header = result["header"]
    keep = ("height", "hash", "prev_hash", "epoch_id", "next_epoch_id", "timestamp_nanosec", "total_supply",
            "gas_price")
    chunks = [{"balance_burnt": c["balance_burnt"], "height_included": c["height_included"],
               "gas_used": c.get("gas_used", 0)} for c in result.get("chunks", [])]
    return {"header": {k: header[k] for k in keep}, "chunks": chunks}


def near(yocto: int | str) -> Decimal:
    return _CTX.divide(Decimal(int(yocto)), _YOCTO)


def ratio(numerator: Decimal | None, denominator: Decimal | None) -> Decimal | None:
    if numerator is None or not denominator:
        return None
    return _CTX.divide(numerator, denominator).quantize(_RATIO_PLACES, context=_CTX)


def rational(pair: list | None) -> Decimal | None:
    if not pair or len(pair) != 2 or not pair[1]:
        return None
    return ratio(Decimal(pair[0]), Decimal(pair[1]))


def fmt(value: Decimal | int | None) -> str:
    if value is None:
        return ""
    if isinstance(value, int):
        return str(value)
    return format(_CTX.normalize(value), "f")


def as_float(value: Decimal | str | None) -> float | None:
    return None if value is None or value == "" else float(value)


def iso_utc(ns: int | str) -> str:
    return (_UNIX_EPOCH + timedelta(microseconds=int(ns) // 1000)).isoformat()


def day_ns(day: str) -> int:
    return int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp()) * 10**9


def ts(block: dict) -> int:
    return int(block["header"]["timestamp_nanosec"])


def target_dates(until_ns: int) -> list[str]:
    """Monthly 1st-of-month dates from FIRST_MONTH plus EXTRA_DATES, keeping those strictly before `until_ns`."""
    days = set(EXTRA_DATES)
    month = FIRST_MONTH
    while day_ns(month.isoformat()) < until_ns:
        days.add(month.isoformat())
        month = date(month.year + month.month // 12, month.month % 12 + 1, 1)
    return sorted(d for d in days if day_ns(d) < until_ns)


def is_target_date(day: str) -> bool:
    """True for a sampling date (1st of a month from FIRST_MONTH, or one of EXTRA_DATES), whatever the tip."""
    parsed = date.fromisoformat(day)
    return day in EXTRA_DATES or (parsed.day == 1 and parsed >= FIRST_MONTH)


def find_block_at(rpc: Rpc, target_ns: int, lo: dict, hi: dict, max_steps: int = 80) -> dict:
    """Last block with timestamp <= target_ns, given brackets lo (ts <= target) and hi (ts > target)."""
    limit = hi["header"]["height"]
    bisect = False
    for _ in range(max_steps):
        low = lo["header"]["height"]
        if limit - low <= 1:
            return lo if "hash" in lo["header"] else rpc.block(low)
        if bisect:
            guess = (low + limit) // 2
        else:
            span = ts(hi) - ts(lo)
            frac = (target_ns - ts(lo)) / span if span > 0 else 0.5
            guess = low + round(frac * (hi["header"]["height"] - low))
        guess = min(max(guess, low + 1), limit - 1)
        width = limit - low
        found = rpc.block_after(guess, limit)
        if found is None:
            limit = guess
        elif ts(found) <= target_ns:
            lo = found
        else:
            hi, limit = found, found["header"]["height"]
        bisect = (limit - lo["header"]["height"]) * 2 > width
    raise RpcUnavailable(f"interpolation search did not converge for {iso_utc(target_ns)}")


def bracket(height: int, ns: int) -> dict:
    """Search bracket from a known (height, timestamp) without fetching the block."""
    return {"header": {"height": height, "timestamp_nanosec": str(ns)}}


def validator_stake(rpc: Rpc, epoch_id: str) -> dict | None:
    try:
        result = rpc.call("validators", {"block_id": epoch_id})
    except RpcError:
        return None
    validators = result["current_validators"]
    return {"stake": near(sum(int(v["stake"]) for v in validators)), "n": len(validators),
            "epoch_start_height": result["epoch_start_height"]}


def protocol_config(rpc: Rpc, height: int) -> dict:
    try:
        return rpc.call("EXPERIMENTAL_protocol_config", {"block_id": height})
    except RpcError:
        return {}


def lst_price(rpc: Rpc, account: str, method: str, height: int) -> Decimal | None:
    value = rpc.view(account, method, height)
    return None if value is None else near(value)


def sample_row(rpc: Rpc, day: str, block: dict) -> dict:
    header = block["header"]
    height = header["height"]
    supply = near(header["total_supply"])
    stake = validator_stake(rpc, header["epoch_id"])
    config = protocol_config(rpc, height)
    return {
        "date": day,
        "height": height,
        "block_time_utc": iso_utc(header["timestamp_nanosec"]),
        "epoch_id": header["epoch_id"],
        "total_supply_near": fmt(supply),
        "validator_stake_near": fmt(stake and stake["stake"]),
        "n_validators": fmt(stake and stake["n"]),
        "stake_epoch_start_height": fmt(stake and stake["epoch_start_height"]),
        "staked_ratio": fmt(ratio(stake and stake["stake"], supply)),
        "stnear_price": fmt(lst_price(rpc, METAPOOL, "get_st_near_price", height)),
        "linear_price": fmt(lst_price(rpc, LINEAR, "ft_price", height)),
        "treasury_near": fmt(rpc.account_near(TREASURY, height)),
        "protocol_version": fmt(config.get("protocol_version")),
        "max_inflation_rate": fmt(rational(config.get("max_inflation_rate"))),
    }


def cached_samples(rows: list[dict], targets: list[str]) -> dict[str, dict]:
    """Cached target rows by date. Rows for non-target dates or stamped after 00:00 (old 'latest' rows) are dropped."""
    wanted = set(targets)
    keep: dict[str, dict] = {}
    for row in rows:
        day = row.get("date", "")
        if day not in wanted or not row.get("height") or not row.get("block_time_utc"):
            continue
        if datetime.fromisoformat(row["block_time_utc"]) > datetime.fromisoformat(day).replace(tzinfo=timezone.utc):
            continue
        if day not in keep or int(row["height"]) > int(keep[day]["height"]):
            keep[day] = row
    return keep


def row_bracket(row: dict) -> dict:
    moment = datetime.fromisoformat(row["block_time_utc"])
    micros = (moment - _UNIX_EPOCH) // timedelta(microseconds=1)
    return bracket(int(row["height"]), micros * 1000)


def sort_rows(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=lambda r: (r["date"], int(r["height"])))


def write_history(rows: list[dict], latest: dict | None, run: dict) -> None:
    write_csv(
        DIR, "history_samples", sort_rows(rows), HISTORY_FIELDS,
        source_url=ARCHIVAL_ENDPOINTS[0],
        endpoints={"history": ARCHIVAL_ENDPOINTS, "latest_row": LATEST_ENDPOINTS},
        description="NEAR mainnet state sampled at 00:00 UTC on the 1st of each month since 2020-11-01, plus "
                    "2020-10-15, 2025-10-29, 2025-11-15 and the latest final block (archival JSON-RPC).",
        sampling="Row = last block with header.timestamp_nanosec <= 00:00:00 UTC of `date`, found by interpolation "
                 "search on block timestamps (bisection fallback), skipping heights with no block. The latest row "
                 "is the latest final block; its `date` is that block's UTC date. It is omitted (latest_row null, "
                 "run.latest_omitted says why) when that date is itself a sampling date, so `date` is unique.",
        latest_row=latest,
        units={"*_near": "NEAR (yocto / 1e24, exact decimal)", "stnear_price / linear_price": "NEAR per token",
               "staked_ratio": "validator_stake_near / total_supply_near",
               "max_inflation_rate": "EXPERIMENTAL_protocol_config max_inflation_rate as a decimal"},
        methods={"total_supply_near": "block header total_supply",
                 "validator_stake_near": "sum(current_validators.stake) from `validators` {block_id: "
                                         "header.epoch_id}; epoch_id = hash of the last block of epoch N-2, so "
                                         "stake lags the sample by ~2 epochs (stake_epoch_start_height shows which "
                                         "epoch). Empty where the node answers UNKNOWN_EPOCH (early mainnet).",
                 "stnear_price": "meta-pool.near get_st_near_price (empty before the contract existed)",
                 "linear_price": "linear-protocol.near ft_price (empty before the contract existed)",
                 "treasury_near": "view_account treasury.near amount + locked",
                 "protocol_version": "EXPERIMENTAL_protocol_config {block_id: height}"},
        incremental_rule="Target-date rows are cached in this CSV and never re-fetched; only the latest-final-block "
                         "row is replaced each run. A row is written only after every call for it returned data or "
                         "a definitive does-not-exist error, so transient failures are retried on the next run.",
        run=run,
    )


def rpc_stats(archival: Rpc, latest: Rpc) -> dict:
    return {"rpc": {"archival": dict(archival.stats), "latest": dict(latest.stats)}}


def pull_history(archival: Rpc, latest: Rpc, tip: dict, genesis: dict) -> list[str]:
    targets = target_dates(ts(tip) - TIP_MARGIN_NS)
    rows = cached_samples(read_csv(DIR, "history_samples"), targets)
    todo = [d for d in targets if d not in rows]
    run: dict[str, Any] = {"targets": len(targets), "cached_reused": len(rows), "fetched": [], "failed": {}}
    print(f"[{NAME}] history: {len(targets)} target dates, {len(rows)} cached, {len(todo)} to fetch")

    tip_day = iso_utc(ts(tip))[:10]
    latest_row = None
    if is_target_date(tip_day):
        run["latest_omitted"] = (f"tip {tip['header']['height']} falls on sampling date {tip_day}, whose row is the "
                                 "00:00 UTC sample; no latest-block row is written so `date` stays unique")
    else:
        try:
            latest_row = sample_row(latest, tip_day, tip)
        except (RpcError, RpcUnavailable) as exc:
            run["failed"]["latest"] = str(exc)
    latest_meta = latest_row and {"date": latest_row["date"], "height": latest_row["height"]}

    def snapshot_rows() -> list[dict]:
        return list(rows.values()) + ([latest_row] if latest_row else [])

    streak = 0
    for day in todo:
        known = sorted(rows.values(), key=lambda r: r["date"])
        before = [r for r in known if r["date"] < day]
        after = [r for r in known if r["date"] > day]
        lo = row_bracket(before[-1]) if before else genesis
        hi = row_bracket(after[0]) if after else tip
        calls = archival.stats["calls"]
        try:
            block = find_block_at(archival, day_ns(day), lo, hi)
            rows[day] = sample_row(archival, day, block)
        except (RpcError, RpcUnavailable) as exc:
            run["failed"][day] = str(exc)
            print(f"[{NAME}]   {day} FAILED: {exc}")
            streak += 1
            if streak >= 3:
                run["stopped_early"] = "3 consecutive dates failed; remaining dates left for the next run"
                break
            continue
        streak = 0
        run["fetched"].append(day)
        print(f"[{NAME}]   {day} -> height {rows[day]['height']} ({archival.stats['calls'] - calls} calls)")
        write_history(snapshot_rows(), latest_meta, run | rpc_stats(archival, latest))

    write_history(snapshot_rows(), latest_meta, run | rpc_stats(archival, latest))
    return [f"history {day}: {err}" for day, err in run["failed"].items()]


def boundary_row(prev_last: dict, last: dict, first: dict) -> dict:
    """Mint at the first block of an epoch; prev_last/last are the last blocks of the two epochs before it."""
    head, before = first["header"], last["header"]
    if head["prev_hash"] != before["hash"] or head["epoch_id"] == before["epoch_id"]:
        raise ValueError(f"block {first['header']['height']} is not the epoch start after {last['header']['height']}")
    height = first["header"]["height"]
    burnt = sum(int(c["balance_burnt"]) for c in first["chunks"] if c["height_included"] == height)
    supply_before = int(last["header"]["total_supply"])
    minted = near(int(first["header"]["total_supply"]) - supply_before + burnt)
    seconds = _CTX.divide(Decimal(ts(last) - ts(prev_last)), Decimal(10**9))
    annualized = ratio(_CTX.multiply(minted, SECONDS_PER_YEAR), _CTX.multiply(near(supply_before), seconds))
    return {"boundary_height": height, "block_time_utc": iso_utc(ts(first)), "minted_near": fmt(minted),
            "epoch_seconds": fmt(seconds), "supply_before_near": fmt(near(supply_before)),
            "annualized_mint_rate": fmt(annualized)}


def iso_ns(stamp: str) -> int:
    """Nanoseconds since the Unix epoch for an ISO 8601 UTC timestamp written by iso_utc (microsecond precision)."""
    moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    return (moment - _UNIX_EPOCH) // timedelta(microseconds=1) * 1000


def epoch_floor(tip_ns: int, days: int) -> date:
    """First date of the epoch_mints window: `days` before the latest full-day dense sample date.

    That date is the earlier of the newest date dense_dates() samples for this tip and the newest cached
    rpc/dense_samples.csv date, so the floor never sits after the start of the models' trailing year."""
    newest = dense_dates(tip_ns)
    latest = date.fromisoformat(newest[-1]) if newest else date.fromisoformat(iso_utc(tip_ns)[:10])
    cached = [r["date"] for r in read_csv(DIR, "dense_samples") if r.get("date")]
    if cached:
        latest = min(latest, date.fromisoformat(max(cached)))
    return latest - timedelta(days=days)


def epoch_chain_gaps(rows: list[dict]) -> list[dict]:
    """Links that break the chain: rows (oldest excepted) whose prev_boundary_height is not the row below them."""
    ordered = sorted(rows, key=lambda r: int(r["boundary_height"]))
    return [{"boundary_height": int(row["boundary_height"]),
             "prev_boundary_height": str(row.get("prev_boundary_height") or "") or None,
             "row_below": int(below["boundary_height"])}
            for below, row in zip(ordered, ordered[1:])
            if str(row.get("prev_boundary_height") or "") != str(below["boundary_height"])]


def same_boundary(fresh: dict, cached: dict) -> bool:
    """A recomputed boundary row agrees with a cached one on every field the cached row carries."""
    return all(str(fresh[k]) == str(cached[k]) for k in EPOCH_FIELDS if cached.get(k) not in (None, ""))


def need(block: dict | None, what: str) -> dict:
    """The block, or a ValueError naming what was missing (the walk records it and keeps its rows)."""
    if block is None:
        raise ValueError(f"no block for {what}")
    return block


def check_link(height: int, prev: int, epoch_length: int | None) -> None:
    """A boundary's link must point below it and, when the epoch length is known, about one epoch below: within
    epoch_length plus or minus one percent (at least one block). Raises ValueError otherwise."""
    gap = height - prev
    if gap <= 0:
        raise ValueError(f"boundary {height} links to {prev}, which is not below it")
    if epoch_length is not None:
        band = max(1, epoch_length // 100)
        if abs(gap - epoch_length) > band:
            raise ValueError(f"boundary {height} links to {prev}, {gap} blocks below, not one epoch "
                             f"({epoch_length} +/- {band})")


def walk_epochs(rpc: Rpc, tip: dict, floor_ns: int, cached: list[dict], *, limit: int | None = None,
                progress: Callable[[list[dict]], None] | None = None,
                every: int = 25, epoch_length: int | None = None) -> tuple[list[dict], str | None]:
    """Newest-first boundary rows from the tip's epoch back to the oldest boundary at or after floor_ns.

    Each row links to the boundary before it (prev_boundary_height), so the result is one contiguous chain. Cached
    rows that carry a link are reused without RPC calls while the cached links hold; where the cached chain breaks
    the walk fetches again. A row recomputed over a cached row must equal it (rows are immutable). `progress` gets
    the rows so far after every `every` fetched rows, so an interrupted walk keeps its work. Every link, cached or
    fetched, must point below its row and (with `epoch_length`) about one epoch below (check_link); a block the RPC
    does not return stops the walk with an error rather than an exception."""
    known = {int(r["boundary_height"]): r for r in cached if r.get("boundary_height")}
    rows: list[dict] = []
    error = None
    fetched = 0
    try:
        last = need(rpc.block(tip["header"]["next_epoch_id"]), "the tip's next_epoch_id")
        height = need(rpc.block_after(last["header"]["height"] + 1), "the newest epoch start")["header"]["height"]
        while limit is None or len(rows) < limit:
            row = known.get(height)
            if row is not None and row.get("prev_boundary_height"):
                if iso_ns(row["block_time_utc"]) < floor_ns:
                    break
                check_link(height, int(row["prev_boundary_height"]), epoch_length)
                rows.append(row)
                last = None
            else:
                first = rpc.block(height)
                if first is None:
                    raise ValueError(f"no block at the linked boundary height {height}")
                if ts(first) < floor_ns:
                    break
                if last is None or last["header"]["hash"] != first["header"]["prev_hash"]:
                    last = need(rpc.block(first["header"]["prev_hash"]), f"the block before boundary {height}")
                prev_last = need(rpc.block(last["header"]["next_epoch_id"]), f"the epoch end before {height}")
                prev_first = need(rpc.block_after(prev_last["header"]["height"] + 1),
                                  f"the epoch start before {height}")
                if prev_first["header"]["prev_hash"] != prev_last["header"]["hash"] \
                        or prev_first["header"]["epoch_id"] == prev_last["header"]["epoch_id"]:
                    raise ValueError(f"block {prev_first['header']['height']} is not the epoch start after "
                                     f"{prev_last['header']['height']}")
                fresh = boundary_row(prev_last, last, first) | {
                    "prev_boundary_height": prev_first["header"]["height"]}
                if row is not None and not same_boundary(fresh, row):
                    raise ValueError(f"boundary {height} recomputed differs from the cached row")
                check_link(height, int(fresh["prev_boundary_height"]), epoch_length)
                rows.append(fresh)
                last = prev_last
                fetched += 1
                if progress is not None and fetched % every == 0:
                    progress(rows)
            height = int(rows[-1]["prev_boundary_height"])
    except (RpcError, RpcUnavailable, ValueError) as exc:
        error = f"epoch walk stopped after {len(rows)} boundaries ({fetched} fetched): {exc}"
    gaps = epoch_chain_gaps(rows)
    if gaps:  # the walk follows links only, so this cannot happen; refuse to hand back a broken chain
        raise AssertionError(f"epoch walk produced a non-contiguous chain: {gaps[:3]}")
    return rows, error


def epoch_file_rows(walked: list[dict], cached: list[dict], floor_ns: int, complete: bool) -> list[dict]:
    """Rows to write, oldest first. A complete walk is the whole window, so it alone is written; an interrupted one
    keeps every cached row at or after the floor that it did not reach (the next run reuses or recomputes them)."""
    rows = {int(r["boundary_height"]): r for r in walked}
    if not complete:
        for r in cached:
            if r.get("boundary_height") and r.get("block_time_utc") and iso_ns(r["block_time_utc"]) >= floor_ns:
                rows.setdefault(int(r["boundary_height"]), r)
    return [rows[h] for h in sorted(rows)]


def write_epochs(rows: list[dict], tip: dict, floor: date, days: int, error: str | None, rpc: Rpc,
                 complete: bool) -> None:
    gaps = epoch_chain_gaps(rows)
    write_csv(
        DIR, "epoch_mints", rows, EPOCH_FIELDS,
        source_url=ARCHIVAL_ENDPOINTS[0],
        description=f"Supply minted at every epoch boundary from {floor.isoformat()} 00:00 UTC ({days} days before "
                    "the latest full-day dense sample date) to the latest final block: the exact mint behind the "
                    "trailing-year burn share, and cheap ground truth for current issuance.",
        method="Walk back from the tip: block(header.next_epoch_id) is the last block of the previous epoch; "
               "boundary = first block after it (prev_hash checked, epoch_id changes there). Each row links to the "
               "boundary before it (prev_boundary_height), found the same way, so the rows form one chain.",
        columns_doc={
            "minted_near": "supply(boundary) - supply(previous block) + sum(chunk.balance_burnt for chunks with "
                           "height_included == boundary). On ordinary blocks supply delta + burnt == 0 exactly.",
            "epoch_seconds": "timestamp(last block of epoch E-1) - timestamp(last block of epoch E-2): the "
                             "duration the mint at the start of epoch E pays for (nearcore finalize_epoch).",
            "supply_before_near": "total_supply of the block before the boundary (the supply nearcore uses).",
            "annualized_mint_rate": f"minted_near / supply_before_near * {SECONDS_PER_YEAR} / epoch_seconds; "
                                    "equals max_inflation_rate when every validator is above the online "
                                    "threshold.",
            "prev_boundary_height": "first block of epoch E-1 (the boundary before this one): the block after "
                                    "the last block of E-2, prev_hash checked. The row below must carry this height."},
        verified="2026-11-10: treasury.near credit x 10 at boundaries 223962737 and 223919537 equals "
                 "max_inflation_rate x supply_before x epoch_seconds / 31536000 within 10 yoctoNEAR.",
        incremental_rule=f"Boundary rows are immutable. The window is every boundary at or after 00:00 UTC "
                         f"{days} days before the latest full-day dense sample date (EPOCH_MINTS_DAYS or "
                         "--epoch-days); rows older than that are dropped. The walk follows prev_boundary_height "
                         "links through cached rows without RPC calls and fetches only where the cached chain "
                         "breaks; a recomputed row must equal its cached copy. Progress is written every "
                         f"{EPOCH_PROGRESS_EVERY} fetched rows, so an interrupted walk resumes where it stopped.",
        floor_date=floor.isoformat(),
        window_days=days,
        complete=complete,
        contiguous=not gaps,
        chain_gaps=gaps[:20],
        n_chain_gaps=len(gaps),
        first_boundary=rows[0]["boundary_height"] if rows else None,
        last_boundary=rows[-1]["boundary_height"] if rows else None,
        tip_height=tip["header"]["height"],
        walk_error=error,
        rpc=dict(rpc.stats),
    )


def pull_epochs(rpc: Rpc, tip: dict, days: int | None = None,
                epoch_length: int | None = None) -> tuple[list[dict], list[str]]:
    days = EPOCH_MINTS_DAYS if days is None else days
    cached = read_csv(DIR, "epoch_mints")
    floor = epoch_floor(ts(tip), days)
    floor_ns = day_ns(floor.isoformat())

    def progress(rows: list[dict]) -> None:
        write_epochs(epoch_file_rows(rows, cached, floor_ns, False), tip, floor, days, "walk in progress", rpc,
                     False)
        print(f"[{NAME}]   epoch_mints: {len(rows)} boundaries back to {rows[-1]['block_time_utc'][:10]} "
              f"({rpc.stats['calls']} calls, {rpc.stats['rate_limited']} rate-limited)", flush=True)

    rows, error = walk_epochs(rpc, tip, floor_ns, cached, progress=progress, every=EPOCH_PROGRESS_EVERY,
                              epoch_length=epoch_length)
    if rows or error:
        write_epochs(epoch_file_rows(rows, cached, floor_ns, error is None), tip, floor, days, error, rpc,
                     error is None)
    return rows, [error] if error else []


def pool_fees(rpc: Rpc, validators: list[dict], height: int) -> list[dict]:
    fees = []
    for v in sorted(validators, key=lambda v: (-int(v["stake"]), v["account_id"]))[:TOP_POOLS]:
        fee = rpc.view(v["account_id"], "get_reward_fee_fraction", height)
        valid = isinstance(fee, dict) and fee.get("denominator")
        fees.append({"pool": v["account_id"], "stake_near": float(near(v["stake"])),
                     "fee_fraction": float(rational([fee["numerator"], fee["denominator"]])) if valid else None,
                     "fee_numerator": fee.get("numerator") if valid else None,
                     "fee_denominator": fee.get("denominator") if valid else None})
    return fees


def metapool_state(rpc: Rpc, height: int) -> dict | None:
    state = rpc.view(METAPOOL, "get_contract_state", height)
    if not isinstance(state, dict):
        return None
    out: dict[str, Any] = {name: float(near(state[k])) for k, name in METAPOOL_AMOUNTS.items() if k in state}
    out.update({k.replace("basis_points", "bp"): int(state[k]) for k in METAPOOL_COUNTS if k in state})
    return out


def ft_supply(rpc: Rpc, contract: str, height: int) -> dict:
    meta = rpc.view(contract, "ft_metadata", height)
    raw = rpc.view(contract, "ft_total_supply", height)
    decimals = meta.get("decimals") if isinstance(meta, dict) else None
    total = None if raw is None or decimals is None else float(_CTX.divide(Decimal(int(raw)), Decimal(10) ** decimals))
    return {"contract": contract, "symbol": meta.get("symbol") if isinstance(meta, dict) else None,
            "decimals": decimals, "total_supply": total, "total_supply_raw": None if raw is None else str(raw)}


def build_snapshot(rpc: Rpc, tip: dict, config: dict, boundary: dict | None) -> dict:
    header = tip["header"]
    height = header["height"]
    supply = near(header["total_supply"])
    validators = rpc.call("validators", {"epoch_id": header["epoch_id"]})["current_validators"]
    total_stake = near(sum(int(v["stake"]) for v in validators))
    current = [{"account_id": v["account_id"], "stake_near": float(near(v["stake"]))}
               for v in sorted(validators, key=lambda v: (-int(v["stake"]), v["account_id"]))]
    return {
        "height": height,
        "block_hash": header["hash"],
        "block_time_utc": iso_utc(header["timestamp_nanosec"]),
        "total_supply_near": float(supply),
        "gas_price": int(header["gas_price"]),
        "protocol_version": config.get("protocol_version"),
        "max_inflation_rate": as_float(rational(config.get("max_inflation_rate"))),
        "protocol_reward_rate_rpc": config.get("protocol_reward_rate"),
        "protocol_treasury_account": config.get("protocol_treasury_account"),
        "epoch_length": config.get("epoch_length"),
        "current_validators": current,
        "total_stake_near": float(total_stake),
        "staked_ratio": as_float(ratio(total_stake, supply)),
        "last_epoch_boundary": boundary and {
            "height": int(boundary["boundary_height"]), "block_time_utc": boundary["block_time_utc"],
            "minted_near": as_float(boundary["minted_near"]), "epoch_seconds": as_float(boundary["epoch_seconds"]),
            "supply_before_near": as_float(boundary["supply_before_near"]),
            "annualized_mint_rate": as_float(boundary["annualized_mint_rate"])},
        "pool_fees": pool_fees(rpc, validators, height),
        "lst": {"stnear_price": as_float(lst_price(rpc, METAPOOL, "get_st_near_price", height)),
                "linear_price": as_float(lst_price(rpc, LINEAR, "ft_price", height)),
                "metapool_state": metapool_state(rpc, height)},
        "ft_supplies": [ft_supply(rpc, c, height) for c in FT_CONTRACTS],
    }


def write_snapshot(payload: dict, rpc: Rpc) -> None:
    write_json(
        DIR, "snapshot_now", payload,
        rows=1,
        source_url=LATEST_ENDPOINTS[0],
        endpoints=LATEST_ENDPOINTS,
        description="Live NEAR state at the latest final block; every view call is pinned to `height`.",
        methods={"current_validators": "`validators` {epoch_id: header.epoch_id} (the tip's own epoch)",
                 "staked_ratio": "total_stake_near / total_supply_near",
                 "gas_price": "block header gas_price, yoctoNEAR per gas unit",
                 "max_inflation_rate": "EXPERIMENTAL_protocol_config max_inflation_rate as a decimal",
                 "protocol_reward_rate_rpc": "verbatim RPC value. nearcore hard-codes the mainnet treasury share to "
                                             "1/10 (reward_calculator.rs use_hardcoded_value); not reinterpreted here.",
                 "last_epoch_boundary": "newest row of rpc/epoch_mints.csv (archival walk)",
                 "pool_fees": f"top {TOP_POOLS} current validators by stake, get_reward_fee_fraction "
                              "(null where the account is not a staking-pool contract)",
                 "lst": "meta-pool.near get_st_near_price / get_contract_state, linear-protocol.near ft_price; "
                        "metapool_state *_near and stnear_supply are yocto / 1e24, *_bp are basis points",
                 "ft_supplies": "ft_metadata + ft_total_supply; total_supply = raw / 10^decimals"},
        rpc=dict(rpc.stats),
    )


# --- dense samples (rpc/dense_samples.csv) -----------------------------------------------------------------------

def dense_first_monday() -> date:
    return DENSE_FIRST + timedelta(days=(7 - DENSE_FIRST.weekday()) % 7)


def dense_daily_window(tip_ns: int) -> tuple[date, date]:
    """(first daily date, the tip's UTC date). Daily dates run from the first up to the day before the second."""
    today = date.fromisoformat(iso_utc(tip_ns)[:10])
    return max(today - timedelta(days=DENSE_DAILY_DAYS), DENSE_FIRST), today


def dense_dates(tip_ns: int) -> list[str]:
    """Dense sampling dates for a tip: Mondays from the first Monday on/after DENSE_FIRST until the daily window,
    then every day from DENSE_DAILY_DAYS days before the tip's UTC date through the day before it (today excluded).
    Only dates whose 00:00 UTC is more than TIP_MARGIN_NS before the tip are kept."""
    daily_from, today = dense_daily_window(tip_ns)
    days: list[date] = []
    day = dense_first_monday()
    while day < daily_from:
        days.append(day)
        day += timedelta(days=7)
    day = daily_from
    while day < today:
        days.append(day)
        day += timedelta(days=1)
    until = tip_ns - TIP_MARGIN_NS
    return [d.isoformat() for d in days if day_ns(d.isoformat()) < until]


def find_block_fast(rpc: Rpc, target_ns: int, lo: dict, hi: dict, guess: int | None = None,
                    max_steps: int = 60) -> dict:
    """Last block with timestamp <= target_ns, given brackets lo (ts <= target) and hi (ts > target).

    Built for regular block times. Each probe aims at the height where the clock should read target_ns: `guess`
    first, then interpolation inside the current bracket (local after one probe). When the estimate puts the answer
    just before a fetched `hi`, that block is read by hash (hi.prev_hash), and the search stops as soon as lo and hi are
    chain neighbours, so skipped heights cost nothing there. Heights with no block are skipped as in find_block_at.
    If probes keep landing on one side, a doubling pad pushes them across; after 8 in a row it bisects."""
    limit = hi["header"]["height"]
    same_side, last_side = 0, ""
    for step in range(max_steps):
        low = lo["header"]["height"]
        if limit - low <= 1 or ("hash" in lo["header"] and hi["header"].get("prev_hash") == lo["header"]["hash"]):
            return lo if "hash" in lo["header"] else rpc.block(low)
        span = ts(hi) - ts(lo)
        if step == 0 and guess is not None:
            x = float(guess)
        elif span > 0:
            x = low + (target_ns - ts(lo)) * (hi["header"]["height"] - low) / span
        else:
            x = (low + limit) / 2
        if same_side < 8 and "prev_hash" in hi["header"] and hi["header"]["height"] - x < 1:
            found = rpc.block(hi["header"]["prev_hash"])
            if found is None:
                raise RpcUnavailable(f"parent of block {hi['header']['height']} not found")
            probe = found["header"]["height"]
        else:
            if same_side >= 8:
                probe = (low + limit) // 2
            else:
                pad = 2 ** (same_side - 2) if same_side >= 2 else 0
                probe = int(x // 1) + (pad if last_side == "lo" else -pad)
            probe = min(max(probe, low + 1), limit - 1)
            found = rpc.block_after(probe, limit)
        if found is None:
            limit, side = probe, "hi"
        elif ts(found) <= target_ns:
            lo, side = found, "lo"
        else:
            hi, limit, side = found, found["header"]["height"], "hi"
        same_side = same_side + 1 if side == last_side else 0
        last_side = side
    raise RpcUnavailable(f"block search did not converge for {iso_utc(target_ns)}")


def dense_bracket(points: list[tuple[int, int]], target_ns: int) -> tuple[dict, dict, int | None]:
    """Brackets around target_ns from known (timestamp_ns, height) points sorted by time, plus a first guess:
    extrapolate from lo at the block rate of the known span that ends at lo."""
    below = [p for p in points if p[0] <= target_ns]
    above = [p for p in points if p[0] > target_ns]
    (lo_ns, lo_h), (hi_ns, hi_h) = below[-1], above[0]
    guess = None
    if len(below) >= 2 and lo_ns > below[-2][0]:
        prev_ns, prev_h = below[-2]
        guess = round(lo_h + (target_ns - lo_ns) * (lo_h - prev_h) / (lo_ns - prev_ns))
    return bracket(lo_h, lo_ns), bracket(hi_h, hi_ns), guess


def dense_row(rpc: Rpc, day: str, block: dict) -> dict:
    header = block["header"]
    height = header["height"]
    return {"date": day, "height": height, "block_time_utc": iso_utc(header["timestamp_nanosec"]),
            "total_supply_near": fmt(near(header["total_supply"])),
            "stnear_price": fmt(lst_price(rpc, METAPOOL, "get_st_near_price", height)),
            "linear_price": fmt(lst_price(rpc, LINEAR, "ft_price", height))}


def dense_overlap(dense: dict[str, dict], history: dict[str, dict]) -> dict:
    """Dates sampled by both files; the two independent searches and reads must agree exactly."""
    common = sorted(set(dense) & set(history))
    keys = ("height", "block_time_utc", "total_supply_near", "stnear_price", "linear_price")
    mismatched = {d: [k for k in keys if str(dense[d].get(k, "")) != str(history[d].get(k, ""))] for d in common}
    return {"dates": len(common), "first": common[0] if common else None, "last": common[-1] if common else None,
            "compared": list(keys), "mismatched": {d: ks for d, ks in mismatched.items() if ks}}


def write_dense(rows: dict[str, dict], history: dict[str, dict], days: list[str], tip: dict, run: dict,
                cumulative: dict | None = None) -> None:
    daily_from, today = dense_daily_window(ts(tip))
    write_csv(
        DIR, "dense_samples", sorted(rows.values(), key=lambda r: r["date"]), DENSE_FIELDS,
        source_url=ARCHIVAL_ENDPOINTS[0],
        endpoints=ARCHIVAL_ENDPOINTS,
        description="NEAR total supply and liquid-staking exchange rates at 00:00 UTC: every Monday from the first "
                    f"Monday on/after {DENSE_FIRST}, then every day for the {DENSE_DAILY_DAYS} days before the tip's "
                    "UTC date (archival JSON-RPC).",
        sampling="Row = last block with header.timestamp_nanosec <= 00:00:00 UTC of `date` (the history_samples "
                 "rule), skipping heights with no block. Today (the tip's UTC date) is never sampled, and a date is "
                 "sampled only once its 00:00 is more than 10 minutes behind the tip.",
        schedule={"weekly": f"Mondays from {dense_first_monday()} up to (not including) daily_from",
                  "daily_days": DENSE_DAILY_DAYS, "daily_from": daily_from.isoformat(),
                  "daily_through": (today - timedelta(days=1)).isoformat(), "scheduled_dates": len(days),
                  "tip_height": tip["header"]["height"], "tip_time_utc": iso_utc(ts(tip))},
        units={"total_supply_near": "NEAR (yocto / 1e24, exact decimal)",
               "stnear_price / linear_price": "NEAR per token (yocto / 1e24)"},
        methods={"total_supply_near": "block header total_supply",
                 "stnear_price": "meta-pool.near get_st_near_price, args_base64 e30= (empty if the call has no answer)",
                 "linear_price": "linear-protocol.near ft_price, args_base64 e30= (empty if the call has no answer)"},
        search="Bracket = nearest known samples on each side (history_samples rows, dense rows, genesis, tip), "
               "excluding any sample of the same date so overlap dates are found independently. First probe = "
               "extrapolation from the lower bracket at the block rate of the known span ending there; then "
               "interpolation inside the shrinking bracket, the parent block read by hash when the answer is just "
               "below a fetched upper bracket, a doubling pad when probes stay on one side, bisection after 8.",
        incremental_rule="Rows are append-only and never re-fetched. A run fetches only scheduled dates with no row. "
                         "Rows fetched under an earlier schedule (daily dates that have since aged past the daily "
                         "window) are kept. A row is written only after its block and both views answered (data or a "
                         "definitive does-not-exist error), so transient failures are retried on the next run.",
        overlap_with_history=dense_overlap(rows, history),
        run=run,
        cumulative=cumulative,
        cumulative_note="cumulative = counters summed over every run that fetched rows into this file (run = the "
                        "latest run only); calls count every HTTP attempt, answered = calls - rate_limited.",
    )


def pull_dense(rpc: Rpc, tip: dict, genesis: dict) -> list[str]:
    days = dense_dates(ts(tip))
    history_rows = read_csv(DIR, "history_samples")
    history = cached_samples(history_rows, target_dates(ts(tip) - TIP_MARGIN_NS))
    rows = {r["date"]: r for r in read_csv(DIR, "dense_samples")
            if r.get("date") and r.get("height") and r.get("block_time_utc")}
    todo = [d for d in days if d not in rows]
    known = sorted({(ts(row_bracket(r)), int(r["height"]), r["date"])
                    for r in history_rows + list(rows.values()) if r.get("height") and r.get("block_time_utc")}
                   | {(ts(genesis), genesis["header"]["height"], ""), (ts(tip), tip["header"]["height"], "")})
    run: dict[str, Any] = {"scheduled": len(days), "cached_reused": len(set(days) & set(rows)),
                           "kept_unscheduled": len(set(rows) - set(days)), "fetched": 0, "first_fetched": None,
                           "last_fetched": None, "failed": {}, "calls": 0, "search_calls": 0, "view_calls": 0,
                           "rate_limited": 0}
    print(f"[{NAME}] dense: {len(days)} scheduled dates, {run['cached_reused']} cached, {len(todo)} to fetch")
    previous = read_meta(DIR, "dense_samples") if rows else {}  # no rows left = start the counters over
    base = previous.get("cumulative") or previous.get("run") or {}
    base = {k: base.get(k, 0) for k in DENSE_COUNTERS}

    def stats() -> dict:
        total = {k: base[k] + run[k] for k in DENSE_COUNTERS}
        return {"run": run | dense_averages(run) | rpc_stats_one(rpc), "cumulative": total | dense_averages(total)}

    streak = 0
    for day in todo:
        target = day_ns(day)
        lo, hi, guess = dense_bracket([(n, h) for n, h, d in known if d != day], target)
        calls, limited = rpc.stats["calls"], rpc.stats["rate_limited"]
        try:
            block = find_block_fast(rpc, target, lo, hi, guess)
            searched = rpc.stats["calls"] - calls
            row = dense_row(rpc, day, block)
        except (RpcError, RpcUnavailable) as exc:
            run["failed"][day] = str(exc)
            print(f"[{NAME}]   dense {day} FAILED: {exc}")
            streak += 1
            if streak >= 3:
                run["stopped_early"] = "3 consecutive dates failed; remaining dates left for the next run"
                break
            continue
        streak = 0
        used = rpc.stats["calls"] - calls
        run["calls"] += used
        run["search_calls"] += searched
        run["view_calls"] += used - searched
        run["rate_limited"] += rpc.stats["rate_limited"] - limited
        run["fetched"] += 1
        run["first_fetched"] = run["first_fetched"] or day
        run["last_fetched"] = day
        rows[day] = row
        known.append((ts(block), block["header"]["height"], day))
        known.sort()
        print(f"[{NAME}]   dense {day} -> height {row['height']} ({used} calls, {searched} search)")
        write_dense(rows, history, days, tip, **stats())
    write_dense(rows, history, days, tip, **stats())
    overlap = dense_overlap(rows, history)
    if overlap["mismatched"]:
        print(f"[{NAME}] dense: WARNING overlap with history_samples differs on {overlap['mismatched']}")
    return [f"dense {day}: {err}" for day, err in run["failed"].items()]


DENSE_COUNTERS = ("fetched", "calls", "search_calls", "view_calls", "rate_limited")


def dense_averages(run: dict) -> dict:
    n = run["fetched"]
    if not n:
        return {"avg_calls_per_row": None, "avg_answered_calls_per_row": None, "avg_search_calls_per_row": None}
    return {"avg_calls_per_row": round(run["calls"] / n, 3),
            "avg_answered_calls_per_row": round((run["calls"] - run["rate_limited"]) / n, 3),
            "avg_search_calls_per_row": round(run["search_calls"] / n, 3)}


def rpc_stats_one(rpc: Rpc) -> dict:
    return {"rpc": {"archival": dict(rpc.stats)}}


# --- protocol parameter changes (rpc/protocol_changes.csv) ---------------------------------------------------------

def config_values(config: dict) -> dict[str, str]:
    """protocol_version plus each tracked parameter as an exact decimal ('' when the config lacks it)."""
    out = {"protocol_version": fmt(config.get("protocol_version"))}
    for param, path in TRACKED_PARAMS.items():
        value: Any = config
        for key in path:
            value = value.get(key) if isinstance(value, dict) else None
        out[param] = fmt(rational(value))
    return out


class ConfigProbe:
    """EXPERIMENTAL_protocol_config by height reduced to config_values, memoised; None where no block exists."""

    def __init__(self, rpc: Rpc, known: dict[int, dict]) -> None:
        self.rpc = rpc
        self.values: dict[int, dict | None] = dict(known)

    def at(self, height: int) -> dict | None:
        if height not in self.values:
            try:
                self.values[height] = config_values(self.rpc.call("EXPERIMENTAL_protocol_config",
                                                                  {"block_id": height}))
            except RpcError as exc:
                if exc.cause != "UNKNOWN_BLOCK":
                    raise
                self.rpc.stats["missing_heights"] += 1
                self.values[height] = None
        return self.values[height]

    def first_after(self, height: int, limit: int, max_scan: int = 200) -> tuple[int, dict] | None:
        """First height in [height, limit) that has a block, with its values; None when there is none."""
        end = min(limit, height + max_scan)
        for candidate in range(height, end):
            values = self.at(candidate)
            if values is not None:
                return candidate, values
        if end >= limit:
            return None
        raise RpcUnavailable(f"no block in heights {height}..{end - 1}; refusing to scan further")


def bisect_change(probe: ConfigProbe, param: str, lo: tuple[int, str], hi: tuple[int, str],
                  depth: int = 0) -> list[tuple[int, int, str, str]]:
    """(prev_height, first_height, old, new) for each change of `param` between two heights whose values differ.

    Binary search on heights; a height with no block moves to the next existing one. A third value in between splits
    the interval and both halves are searched. A change that reverts between two probes is not seen."""
    (lo_h, lo_v), (hi_h, hi_v) = lo, hi
    limit = hi_h
    while limit - lo_h > 1:
        mid = (lo_h + limit) // 2
        found = probe.first_after(mid, limit)
        if found is None:
            limit = mid
            continue
        height, values = found
        value = values[param]
        if value == lo_v:
            lo_h = height
        elif value == hi_v:
            hi_h = limit = height
        elif value == "" or depth >= 8:
            raise ValueError(f"{param} unreadable or changing too often near height {height} (value {value!r})")
        else:
            return (bisect_change(probe, param, (lo_h, lo_v), (height, value), depth + 1)
                    + bisect_change(probe, param, (height, value), (hi_h, hi_v), depth + 1))
    return [(lo_h, hi_h, lo_v, hi_v)]


def change_row(rpc: Rpc, probe: ConfigProbe, param: str, change: tuple[int, int, str, str]) -> tuple[dict, dict]:
    prev_h, first_h, old, new = change
    first, prev = rpc.block(first_h), rpc.block(prev_h)
    if first is None or prev is None or first["header"]["prev_hash"] != prev["header"]["hash"]:
        raise ValueError(f"{param}: blocks {prev_h} and {first_h} are not chain neighbours")
    row = {"param": param, "old_value": old, "new_value": new, "first_height": first_h,
           "block_time_utc": iso_utc(ts(first)),
           "protocol_version": (probe.at(first_h) or {}).get("protocol_version", ""),
           "prev_height": prev_h}
    check = {"parent_hash_verified": True,
             "first_block_starts_epoch": first["header"]["epoch_id"] != prev["header"]["epoch_id"],
             "prev_protocol_version": (probe.at(prev_h) or {}).get("protocol_version", "")}
    return row, check


def find_param_changes(rpc: Rpc, samples: dict[int, dict], cached: list[dict],
                       checks: dict) -> tuple[list[dict], dict[str, str]]:
    """Change rows for every tracked parameter: cached rows reused, new intervals bisected."""
    probe = ConfigProbe(rpc, {h: {k: s[k] for k in ("protocol_version", *TRACKED_PARAMS)} for h, s in samples.items()})
    rows: list[dict] = []
    failed: dict[str, str] = {}
    for param in TRACKED_PARAMS:
        mine = [r for r in cached if r.get("param") == param and r.get("prev_height") and r.get("first_height")]
        points = {h: s[param] for h, s in samples.items() if s.get(param, "") != ""}
        recorded = {}
        for r in mine:
            prev_h, first_h = int(r["prev_height"]), int(r["first_height"])
            points[prev_h], points[first_h] = r["old_value"], r["new_value"]
            recorded[(prev_h, first_h, r["old_value"], r["new_value"])] = r
        ordered = sorted(points.items())
        for (a_h, a_v), (b_h, b_v) in zip(ordered, ordered[1:]):
            if a_v == b_v:
                continue
            if (a_h, b_h, a_v, b_v) in recorded:
                rows.append(recorded[(a_h, b_h, a_v, b_v)])
                continue
            try:
                for change in bisect_change(probe, param, (a_h, a_v), (b_h, b_v)):
                    row, check = change_row(rpc, probe, param, change)
                    rows.append(row)
                    checks[f"{param}@{row['first_height']}"] = check
                    print(f"[{NAME}]   {param} {row['old_value']} -> {row['new_value']} first at height "
                          f"{row['first_height']} ({row['block_time_utc']}, protocol {row['protocol_version']})")
            except (RpcError, RpcUnavailable, ValueError) as exc:
                failed[f"{param} {a_h}..{b_h}"] = str(exc)
    unique = {(r["param"], int(r["first_height"])): r for r in rows}
    return sorted(unique.values(), key=lambda r: (int(r["first_height"]), r["param"])), failed


def pull_protocol_changes(rpc: Rpc, tip: dict, tip_config: dict) -> list[str]:
    history = cached_samples(read_csv(DIR, "history_samples"), target_dates(ts(tip) - TIP_MARGIN_NS))
    cached = {int(r["height"]): r for r in read_csv(DIR, "protocol_config_samples") if r.get("height")}
    calls = rpc.stats["calls"]
    run: dict[str, Any] = {"failed": {}}
    samples: dict[int, dict] = {}
    for day, row in sorted(history.items()):
        height = int(row["height"])
        if height in cached and cached[height].get("date") == day:
            samples[height] = cached[height]
            continue
        try:
            values = config_values(rpc.call("EXPERIMENTAL_protocol_config", {"block_id": height}))
        except (RpcError, RpcUnavailable) as exc:
            run["failed"][f"config {day} (height {height})"] = str(exc)
            continue
        samples[height] = {"height": str(height), "date": day, **values}
    tip_height = tip["header"]["height"]
    samples[tip_height] = {"height": str(tip_height), "date": iso_utc(ts(tip))[:10], **config_values(tip_config)}
    run["config_sample_calls"] = rpc.stats["calls"] - calls
    write_csv(
        DIR, "protocol_config_samples", [samples[h] for h in sorted(samples)], CONFIG_SAMPLE_FIELDS,
        source_url=ARCHIVAL_ENDPOINTS[0],
        endpoints={"history_heights": ARCHIVAL_ENDPOINTS, "tip": LATEST_ENDPOINTS},
        description="Tracked protocol parameters read at every history_samples target height (00:00 UTC samples) "
                    "plus the latest final block; the cache behind rpc/protocol_changes.csv.",
        sampling=f"One row per history_samples target date (1st of each month plus {', '.join(EXTRA_DATES)}, at "
                 "that row's height: the last block with header.timestamp <= 00:00 UTC) plus one row at the tip (the "
                 "latest final block of this run, dated by its UTC timestamp).",
        units={"protocol_version": "integer protocol version",
               **{p: "dimensionless exact decimal ([numerator, denominator] divided; a fraction, not percent)"
                  for p in TRACKED_PARAMS}},
        methods={"protocol_version": "EXPERIMENTAL_protocol_config {block_id: height}",
                 **{p: "EXPERIMENTAL_protocol_config " + ".".join(path) + " as a decimal"
                    for p, path in TRACKED_PARAMS.items()}},
        incremental_rule="Rows for history target heights are never re-read; the tip row is replaced each run (its "
                         "config is the one the run already read for the snapshot). A failed read leaves no row "
                         "(see protocol_changes.meta.json run.failed) and is retried next run.",
        tip_height=tip_height,
    )

    meta = read_meta(DIR, "protocol_changes")
    checks = dict(meta.get("checks", {}))
    calls = rpc.stats["calls"]
    rows, failed = find_param_changes(rpc, samples, read_csv(DIR, "protocol_changes"), checks)
    run["failed"].update(failed)
    run["bisection_calls"] = rpc.stats["calls"] - calls
    write_csv(
        DIR, "protocol_changes", rows, CHANGE_FIELDS,
        source_url=ARCHIVAL_ENDPOINTS[0],
        endpoints=ARCHIVAL_ENDPOINTS,
        description="Exact first block at which each tracked protocol parameter took a new value: "
                    + ", ".join(f"{p} ({'.'.join(path)})" for p, path in TRACKED_PARAMS.items()) + ".",
        sampling="Configs are read at the monthly history_samples target heights plus the tip "
                 "(rpc/protocol_config_samples.csv); each interval between two consecutive reads whose values differ "
                 "is then bisected down to adjacent blocks. One row per change found.",
        units={"old_value / new_value": "dimensionless exact decimal (a fraction, not percent: 0.025 = 2.5%/year "
                                        "for max_inflation_rate, 0.3 = 30% of burnt gas for burnt_gas_reward)",
               "first_height / prev_height": "block height", "block_time_utc": "ISO 8601 UTC",
               "protocol_version": "integer protocol version"},
        method="Candidate intervals are consecutive rows of rpc/protocol_config_samples.csv (history target heights "
               "+ tip) whose values differ. Each is binary-searched with EXPERIMENTAL_protocol_config {block_id: "
               "height}, moving past heights with no block, to the first height whose config has the new value. "
               "first_height's parent hash is checked to be prev_height's block hash, so no block lies between.",
        columns_doc={"old_value / new_value": "the parameter as a decimal ([numerator, denominator] divided)",
                     "first_height": "first block whose config has new_value",
                     "prev_height": "the block just before it (config has old_value)",
                     "block_time_utc": "header timestamp of first_height",
                     "protocol_version": "protocol_version in the config at first_height"},
        limits="A change that reverts between two consecutive config samples (about a month apart) is not seen. "
               "Values are never inferred: an unreadable config leaves its interval unsearched and listed in "
               "run.failed.",
        checks=checks,
        incremental_rule="Recorded change rows are immutable: an interval already explained by a recorded change (prev_height "
                         "old value, first_height new value) is not searched again.",
        run=run | rpc_stats_one(rpc),
    )
    return [f"protocol_changes {key}: {err}" for key, err in run["failed"].items()]


# --- epoch history (rpc/epoch_history.csv) --------------------------------------------------------------------------

def burnt_in_block(block: dict) -> int:
    """yoctoNEAR burnt by `block`: balance_burnt of the chunks included at its height (nearcore Block::produce)."""
    height = block["header"]["height"]
    return sum(int(c["balance_burnt"]) for c in block["chunks"] if c["height_included"] == height)


def last_block_where(rpc: Rpc, lo: dict, hi: dict, pred: Callable[[dict], bool], max_steps: int = 80) -> dict:
    """Last block in [lo, hi) for which pred holds, given pred(lo), not pred(hi), and pred monotone in height."""
    limit = hi["header"]["height"]
    for _ in range(max_steps):
        low = lo["header"]["height"]
        if limit - low <= 1 or hi["header"].get("prev_hash") == lo["header"]["hash"]:
            return lo
        mid = (low + limit) // 2
        found = rpc.block_after(mid, limit)
        if found is None:
            limit = mid
        elif pred(found):
            lo = found
        else:
            hi, limit = found, found["header"]["height"]
    raise RpcUnavailable("bisection on header.epoch_id did not converge")


def epoch_start(rpc: Rpc, block: dict, epoch_length: int) -> tuple[dict, dict]:
    """(last block of the previous epoch, first block of `block`'s epoch).

    header.next_epoch_id is the hash of the last block of the previous epoch, so one lookup finds it. If that lookup
    fails, step back by epoch_length until outside the epoch and bisect on header.epoch_id."""
    eid = block["header"]["epoch_id"]
    last_prev = rpc.block(block["header"]["next_epoch_id"])
    if last_prev is None or last_prev["header"]["epoch_id"] == eid:
        outside = block
        for _ in range(8):
            outside = rpc.block_after(max(outside["header"]["height"] - epoch_length, 1))
            if outside is not None and outside["header"]["epoch_id"] != eid:
                break
        else:
            raise RpcUnavailable(f"no block before the epoch of {block['header']['height']} found")
        last_prev = last_block_where(rpc, outside, block, lambda b: b["header"]["epoch_id"] != eid)
    start = rpc.block_after(last_prev["header"]["height"] + 1)
    if start["header"]["prev_hash"] != last_prev["header"]["hash"] or start["header"]["epoch_id"] != eid:
        raise ValueError(f"block {start['header']['height']} is not the first block of epoch {eid}")
    return last_prev, start


def epoch_end(rpc: Rpc, start: dict, epoch_length: int, tip_height: int) -> tuple[dict, dict] | None:
    """(last block of start's epoch, first block of the next epoch), or None when the epoch has not ended by the tip.

    Probe start + epoch_length (normally the next epoch's first block); a block of the next epoch carries the hash of
    this epoch's last block in header.next_epoch_id. Longer epochs are stepped through, anything else is bisected on
    header.epoch_id."""
    eid = start["header"]["epoch_id"]
    probe_h = start["header"]["height"] + epoch_length
    for _ in range(16):
        if probe_h > tip_height:
            return None
        probe = rpc.block_after(probe_h, tip_height + 1)
        if probe is None:
            return None
        if probe["header"]["epoch_id"] == eid:
            probe_h = probe["header"]["height"] + max(1, epoch_length // 4)
            continue
        last = rpc.block(probe["header"]["next_epoch_id"])
        if last is None or last["header"]["epoch_id"] != eid:
            last = last_block_where(rpc, start, probe, lambda b: b["header"]["epoch_id"] == eid)
        following = rpc.block_after(last["header"]["height"] + 1, tip_height + 1)
        if following is None:
            return None
        if following["header"]["prev_hash"] != last["header"]["hash"] or following["header"]["epoch_id"] == eid:
            raise ValueError(f"block {following['header']['height']} does not start the epoch after {eid}")
        return last, following
    raise RpcUnavailable(f"end of epoch {eid} not found")


def epoch_history_row(sample_date: str, last_prev: dict, start: dict, end: dict, next_start: dict,
                      protocol_version: str = "", max_inflation_rate: str = "") -> dict:
    """One complete epoch E = blocks start..end. last_prev ends E-1; next_start is E+1's first block."""
    if start["header"]["prev_hash"] != last_prev["header"]["hash"] \
            or start["header"]["epoch_id"] == last_prev["header"]["epoch_id"]:
        raise ValueError(f"block {start['header']['height']} is not the epoch start after "
                         f"{last_prev['header']['height']}")
    if end["header"]["epoch_id"] != start["header"]["epoch_id"]:
        raise ValueError(f"block {end['header']['height']} is not in the epoch of {start['header']['height']}")
    boundary = boundary_row(last_prev, end, next_start)  # mint at next_start (pays for E), epoch_seconds of E
    minted = Decimal(boundary["minted_near"])
    seconds = Decimal(boundary["epoch_seconds"])
    supply_start, supply_end = int(start["header"]["total_supply"]), int(end["header"]["total_supply"])
    burnt_yocto = supply_start - supply_end + burnt_in_block(start)
    if burnt_yocto < 0:
        raise ValueError(f"supply rose inside the epoch starting at {start['header']['height']}")
    burnt = near(burnt_yocto)
    base = _CTX.multiply(near(supply_start), seconds)
    return {
        "sample_date": sample_date,
        "epoch_id": start["header"]["epoch_id"],
        "start_height": start["header"]["height"],
        "start_time_utc": iso_utc(ts(start)),
        "end_height": end["header"]["height"],
        "end_time_utc": iso_utc(ts(end)),
        "next_start_height": next_start["header"]["height"],
        "epoch_seconds": fmt(seconds),
        "supply_start_near": fmt(near(supply_start)),
        "supply_end_near": fmt(near(supply_end)),
        "minted_next_boundary_near": fmt(minted),
        "burnt_in_epoch_near": fmt(burnt),
        "annualized_mint_rate": fmt(ratio(_CTX.multiply(minted, SECONDS_PER_YEAR), base)),
        "annualized_burn_rate": fmt(ratio(_CTX.multiply(burnt, SECONDS_PER_YEAR), base)),
        "protocol_version": protocol_version,
        "max_inflation_rate": max_inflation_rate,
    }


def validators_start_check(rpc: Rpc, end: dict, start: dict) -> str:
    """Cross-check the start height with `validators` {block_id: last block of the epoch} where the node has it."""
    try:
        info = rpc.call("validators", {"block_id": end["header"]["height"]})
    except RpcError as exc:
        return f"unavailable: {exc.cause}"
    if info.get("epoch_start_height") != start["header"]["height"]:
        raise ValueError(f"validators epoch_start_height {info.get('epoch_start_height')} != start height "
                         f"{start['header']['height']}")
    return "match"


def epoch_for_sample(rpc: Rpc, sample: dict, tip_height: int, epoch_length: int) -> tuple[dict, str] | None:
    block = rpc.block(int(sample["height"]))
    if block is None or block["header"]["epoch_id"] != sample.get("epoch_id", block["header"]["epoch_id"]):
        raise ValueError(f"block {sample['height']} does not match the history row for {sample['date']}")
    last_prev, start = epoch_start(rpc, block, epoch_length)
    found = epoch_end(rpc, start, epoch_length, tip_height)
    if found is None:
        return None
    end, next_start = found
    row = epoch_history_row(sample["date"], last_prev, start, end, next_start, sample.get("protocol_version", ""),
                            sample.get("max_inflation_rate", ""))
    return row, validators_start_check(rpc, end, start)


def mint_rate_above_max(rows: dict[str, dict]) -> dict[str, dict]:
    """Rows whose annualized_mint_rate exceeds the max_inflation_rate recorded with them (see the meta caveat)."""
    out = {}
    for day in sorted(rows):
        rate, cap = rows[day].get("annualized_mint_rate", ""), rows[day].get("max_inflation_rate", "")
        if rate != "" and cap != "" and Decimal(str(rate)) > Decimal(str(cap)):
            out[day] = {"annualized_mint_rate": str(rate), "max_inflation_rate": str(cap),
                        "protocol_version": str(rows[day].get("protocol_version", ""))}
    return out


def write_epoch_history(rows: dict[str, dict], checks: dict, run: dict) -> None:
    write_csv(
        DIR, "epoch_history", [rows[d] for d in sorted(rows)], EPOCH_HISTORY_FIELDS,
        source_url=ARCHIVAL_ENDPOINTS[0],
        endpoints=ARCHIVAL_ENDPOINTS,
        description="One complete epoch per history_samples target date (the epoch containing the 00:00 UTC sample "
                    "block): its bounds, duration, supply, the mint paid for it and the fees burnt during it.",
        sampling="Epoch E = the epoch of the history_samples block for sample_date. Rows exist only for epochs that "
                 "ended (next epoch's first block exists) by the tip of the run that wrote them.",
        method="last block of E-1 = block(header.next_epoch_id of the sample block); start = the next block (parent "
               "hash checked). end = block(header.next_epoch_id of the block at start + epoch_length, which is in "
               "E+1); next_start = the block after end (parent hash checked). Bisection on header.epoch_id is the "
               "fallback when a hash lookup does not land in the expected epoch. start_height is cross-checked "
               "against `validators` {block_id: end_height}.epoch_start_height where the node has it (Mar 2021+).",
        columns_doc={
            "epoch_id": "header.epoch_id of the epoch's blocks",
            "start_height / end_height": "first / last block of E; next_start_height = first block of E+1",
            "epoch_seconds": "timestamp(end) - timestamp(last block of E-1): the duration nearcore pays for at "
                             "next_start (as epoch_mints.epoch_seconds)",
            "supply_start_near": "total_supply of the start block (after the mint for E-1 and the start block's own "
                                 "burn)",
            "supply_end_near": "total_supply of the end block",
            "minted_next_boundary_near": "supply(next_start) - supply(end) + burnt_in_block(next_start): the mint "
                                         "paid at E+1's first block for epoch E (as epoch_mints.minted_near)",
            "burnt_in_epoch_near": "fees burnt by blocks start..end = supply(start) - supply(end) + "
                                   "burnt_in_block(start). Inside an epoch supply only falls, by exactly each block's "
                                   "burn; the start block's supply delta also holds the mint, so its burn is read "
                                   "from its chunks instead. Supply-based, so it is the net burn after any gas "
                                   "rebate paid to contracts (burnt_gas_reward, 0.3 until protocol 87).",
            "burnt_in_block(b)": "sum(chunk.balance_burnt for chunks of block b with height_included == b.height); "
                                 "nearcore Block::produce sets supply(b) = supply(parent) + mint - this sum",
            "annualized_mint_rate": f"minted_next_boundary_near * {SECONDS_PER_YEAR} / (supply_start_near * "
                                    "epoch_seconds). Caveat: before protocol 45 nearcore sized the epoch reward by "
                                    "block count, not time (supply * max_inflation_rate * epoch_length / "
                                    "num_blocks_per_year; ProtocolFeature::RectifyInflation => 45 switched it to "
                                    "epoch_duration / num_seconds_per_year; nearcore 1.20.0 "
                                    "chain/epoch_manager/src/reward_calculator.rs and core/primitives/src/"
                                    "version.rs). In those epochs this time-based rate can exceed "
                                    "max_inflation_rate when blocks came faster than 1 per second; do not read it "
                                    "as the configured inflation rate there. mint_rate_above_max_inflation_rate "
                                    "lists the rows where it does.",
            "annualized_burn_rate": f"burnt_in_epoch_near * {SECONDS_PER_YEAR} / (supply_start_near * "
                                    "epoch_seconds)",
            "protocol_version / max_inflation_rate": "copied from the history_samples row (config at the sample "
                                                     "height, inside E; protocol version is fixed per epoch)"},
        units={"*_height": "block height", "*_time_utc": "ISO 8601 UTC (block header timestamp)",
               "epoch_seconds": "seconds (exact decimal from nanosecond timestamps)",
               "*_near": "NEAR (yocto / 1e24, exact decimal)",
               "annualized_mint_rate / annualized_burn_rate / max_inflation_rate": "fraction per year, not percent "
                                                                                   "(0.05 = 5%/year)",
               "protocol_version": "integer protocol version"},
        mint_rate_above_max_inflation_rate=mint_rate_above_max(rows),
        start_height_check=checks,
        incremental_rule="Rows are immutable once written; only samples without a row are computed. Samples whose "
                         "epoch has not ended by the tip are skipped and tried again next run.",
        run=run,
    )


def pull_epoch_history(rpc: Rpc, tip: dict, epoch_length: int) -> list[str]:
    samples = cached_samples(read_csv(DIR, "history_samples"), target_dates(ts(tip) - TIP_MARGIN_NS))
    rows = {r["sample_date"]: r for r in read_csv(DIR, "epoch_history")
            if r.get("sample_date") and r.get("start_height")}
    checks = dict(read_meta(DIR, "epoch_history").get("start_height_check", {}))
    todo = [d for d in sorted(samples) if d not in rows]
    run: dict[str, Any] = {"samples": len(samples), "cached_reused": len(set(samples) & set(rows)), "fetched": [],
                           "incomplete": [], "failed": {}}
    print(f"[{NAME}] epoch_history: {len(samples)} samples, {run['cached_reused']} cached, {len(todo)} to compute")
    streak = 0
    for day in todo:
        calls = rpc.stats["calls"]
        try:
            result = epoch_for_sample(rpc, samples[day], tip["header"]["height"], epoch_length)
        except (RpcError, RpcUnavailable, ValueError) as exc:
            run["failed"][day] = str(exc)
            print(f"[{NAME}]   epoch {day} FAILED: {exc}")
            streak += 1
            if streak >= 3:
                run["stopped_early"] = "3 consecutive samples failed; remaining samples left for the next run"
                break
            continue
        streak = 0
        if result is None:
            run["incomplete"].append(day)
            continue
        rows[day], checks[day] = result
        run["fetched"].append(day)
        print(f"[{NAME}]   epoch {day} -> {rows[day]['start_height']}..{rows[day]['end_height']} "
              f"({rpc.stats['calls'] - calls} calls, validators {checks[day]})")
        write_epoch_history(rows, checks, run | rpc_stats_one(rpc))
    write_epoch_history(rows, checks, run | rpc_stats_one(rpc))
    return [f"epoch_history {day}: {err}" for day, err in run["failed"].items()]


def dense_enabled() -> bool:
    """RPC_DENSE=0 skips the dense samples, protocol changes and epoch history (default: on)."""
    return env("RPC_DENSE", "1") != "0"


def genesis_bracket(config: dict) -> dict:
    moment = datetime.fromisoformat(config["genesis_time"].replace("Z", "+00:00"))
    micros = (moment - _UNIX_EPOCH) // timedelta(microseconds=1)
    return bracket(int(config["genesis_height"]), micros * 1000)


def pull(args: argparse.Namespace | None = None) -> None:
    parts = set((getattr(args, "parts", None) or DEFAULT_PARTS).split(","))
    if not dense_enabled() and parts & set(DENSE_PARTS):
        print(f"[{NAME}] RPC_DENSE=0: skipping {', '.join(p for p in DENSE_PARTS if p in parts)}")
        parts -= set(DENSE_PARTS)
    days = getattr(args, "epoch_days", None) or EPOCH_MINTS_DAYS
    latest = Rpc(LATEST_ENDPOINTS)
    archival = Rpc(ARCHIVAL_ENDPOINTS)
    pinned = getattr(args, "tip_height", None)
    # --tip-height re-runs against an earlier tip (an archival block) so a partial re-pull matches the other files
    tip = slim_block(archival.call("block", {"block_id": pinned}) if pinned
                     else latest.call("block", {"finality": "final"}))
    config = (archival if pinned else latest).call("EXPERIMENTAL_protocol_config",
                                                   {"block_id": tip["header"]["height"]})
    print(f"[{NAME}] tip height {tip['header']['height']} at {iso_utc(ts(tip))}")
    errors: list[str] = []
    if "history" in parts:
        errors += pull_history(archival, latest, tip, genesis_bracket(config))
    boundaries: list[dict] = []
    if "epochs" in parts:
        boundaries, errs = pull_epochs(archival, tip, days, int(config.get("epoch_length") or EPOCH_LENGTH))
        errors += errs
        print(f"[{NAME}] epoch_mints: {len(boundaries)} boundaries")
    if "snapshot" in parts:
        if not boundaries:
            boundaries, error = walk_epochs(archival, tip, 0, [], limit=1)
            errors += [error] if error else []
        try:
            write_snapshot(build_snapshot(latest, tip, config, boundaries[0] if boundaries else None), latest)
            print(f"[{NAME}] snapshot_now: height {tip['header']['height']}")
        except (RpcError, RpcUnavailable) as exc:
            errors.append(f"snapshot: {exc}")
    if "changes" in parts:
        errors += pull_protocol_changes(archival, tip, config)
    if "epoch_history" in parts:
        errors += pull_epoch_history(archival, tip, int(config.get("epoch_length") or EPOCH_LENGTH))
    if "dense" in parts:
        errors += pull_dense(archival, tip, genesis_bracket(config))
    print(f"[{NAME}] rpc calls: archival {dict(archival.stats)}, latest {dict(latest.stats)}")
    if errors:
        raise RuntimeError(f"{len(errors)} near_rpc step(s) incomplete (partial results kept): " + " | ".join(errors))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--parts", default=DEFAULT_PARTS,
                        help=f"comma-separated subset of {DEFAULT_PARTS} (RPC_DENSE=0 drops {','.join(DENSE_PARTS)})")
    parser.add_argument("--epoch-days", type=int, default=EPOCH_MINTS_DAYS,
                        help="epoch_mints.csv keeps every boundary from this many days before the latest dense date")
    parser.add_argument("--tip-height", type=int, default=None,
                        help="walk from this archival block instead of the latest final block (re-runs, resumes)")
    pull(parser.parse_args(argv))


if __name__ == "__main__":
    main()
