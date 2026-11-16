"""Offline tests for pipeline.sources.near_rpc.

A synthetic chain (missing heights, epochs, mints and burns) is served through a fake http_post_json, so the real
Rpc client, interpolation search, epoch walk and incremental cache logic all run without network access.
Fixtures in tests/fixtures/near_rpc/ are trimmed real RPC responses captured on 2026-11-10.
"""

from __future__ import annotations

import csv
import json
import sys
from decimal import Decimal, localcontext
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline import config  # noqa: E402
from pipeline.sources import near_rpc  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "near_rpc"
YOCTO = 10**24
SECOND = 10**9


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


class FakeChain:
    """In-memory chain answering JSON-RPC like a nearcore archival node."""

    def __init__(self, start_ns: int, step_ns: int, n: int, *, missing: set[int] = frozenset(), epoch_len: int = 10,
                 mint: int = 1000 * YOCTO, burn: int = YOCTO, first_epoch_with_info: int = 0,
                 genesis_time: str = "2026-01-01T00:00:00Z", param_changes: dict[int, dict] | None = None) -> None:
        self.epoch_len = epoch_len
        self.first_epoch_with_info = first_epoch_with_info
        self.genesis_time = genesis_time
        self.param_changes = sorted((param_changes or {}).items())  # from height h on, config.update(changes)
        self.calls: list[tuple[str, str]] = []
        self.by_height: dict[int, dict] = {}
        heights = [h for h in range(n) if h not in missing]
        last_of_epoch: dict[int, int] = {}
        self.first_of_epoch: dict[int, int] = {}
        for h in heights:
            last_of_epoch[h // epoch_len] = h
            self.first_of_epoch.setdefault(h // epoch_len, h)
        self.last_of_epoch = last_of_epoch
        supply = 1_000_000_000 * YOCTO
        prev = None
        for h in heights:
            epoch = h // epoch_len
            is_start = prev is not None and prev // epoch_len != epoch
            supply += (mint if is_start else 0) - burn
            self.by_height[h] = {
                "header": {
                    "height": h, "hash": f"h{h}", "prev_hash": f"h{prev}", "timestamp_nanosec": str(start_ns + h * step_ns),
                    "epoch_id": f"h{last_of_epoch[epoch - 2]}" if epoch >= 2 else "11111111111111111111111111111111",
                    "next_epoch_id": f"h{last_of_epoch[epoch - 1]}" if epoch >= 1 else "11111111111111111111111111111111",
                    "total_supply": str(supply), "gas_price": "100000000"},
                "chunks": [{"balance_burnt": str(burn), "height_included": h},
                           {"balance_burnt": "999", "height_included": h - 1}],
            }
            prev = h
        self.by_hash = {b["header"]["hash"]: b for b in self.by_height.values()}
        self.tip = self.by_height[heights[-1]]

    def __call__(self, url: str, payload: dict, **_: object) -> dict:
        method, params = payload["method"], payload["params"]
        self.calls.append((url, method))
        if method == "block":
            if "finality" in params:
                return {"result": self.tip}
            found = self.by_height.get(params["block_id"]) or self.by_hash.get(params["block_id"])
            return {"result": found} if found else fixture("errors.json")["unknown_block"]
        if method == "validators" and isinstance(params.get("block_id"), int):  # nearcore: last block of an epoch
            block = self.by_height.get(params["block_id"])
            if block is None:
                return fixture("errors.json")["unknown_block"]
            epoch = params["block_id"] // self.epoch_len
            if epoch < self.first_epoch_with_info:
                return fixture("errors.json")["unknown_epoch"]
            if self.last_of_epoch[epoch] != params["block_id"]:
                return fixture("errors.json")["validator_info_unavailable"]
            return {"result": {"epoch_start_height": self.first_of_epoch[epoch], "epoch_height": epoch,
                               "current_validators": []}}
        if method == "validators":
            block = self.by_hash.get(params.get("block_id"))
            if block is None or block["header"]["height"] // self.epoch_len < self.first_epoch_with_info:
                return fixture("errors.json")["unknown_epoch"]
            start = (block["header"]["height"] // self.epoch_len) * self.epoch_len
            return {"result": {"epoch_start_height": start, "current_validators": [
                {"account_id": "a.poolv1.near", "stake": str(300 * 10**6 * YOCTO)},
                {"account_id": "b.poolv1.near", "stake": str(100 * 10**6 * YOCTO + 5)}]}}
        if method == "EXPERIMENTAL_protocol_config":
            block_id = params.get("block_id")
            if block_id is None:
                return {"result": self.config_at(None)}
            block = self.by_height.get(block_id) or self.by_hash.get(block_id)
            if block is None:
                return fixture("errors.json")["unknown_block"]
            return {"result": self.config_at(block["header"]["height"])}
        if method == "query" and params["request_type"] == "view_account":
            return {"result": {"amount": str(7 * YOCTO), "locked": str(YOCTO // 2)}}
        if method == "query" and params["account_id"] == "meta-pool.near":
            value = json.dumps("1500000000000000000000001").encode()
            return {"result": {"result": list(value), "logs": []}}
        return fixture("errors.json")["unknown_account"]


    def config_at(self, height: int | None) -> dict:
        config = {"protocol_version": 80, "max_inflation_rate": [1, 20], "protocol_reward_rate": [0, 1],
                  "epoch_length": self.epoch_len, "genesis_height": 0, "genesis_time": self.genesis_time,
                  "runtime_config": {"transaction_costs": {"burnt_gas_reward": [3, 10]}}}
        for start, changes in self.param_changes:
            if height is not None and height >= start:
                config.update(changes)
        return config


def make_rpc(chain: FakeChain, monkeypatch, endpoints: list[str] | None = None) -> near_rpc.Rpc:
    monkeypatch.setattr(near_rpc, "http_post_json", chain)
    return near_rpc.Rpc(endpoints or ["https://archival.test"], interval=0, sleep=lambda s: None)


@pytest.fixture
def raw_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "RAW_DIR", tmp_path / "data" / "raw")
    return tmp_path / "data" / "raw" / "rpc"


def read_rows(path: Path) -> list[dict]:
    with path.open(newline="") as fh:
        return list(csv.DictReader(fh))


def test_target_dates_monthly_plus_extras_and_cut_at_tip():
    days = near_rpc.target_dates(near_rpc.day_ns("2026-11-10"))
    assert days[:3] == ["2020-10-15", "2020-11-01", "2020-12-01"]
    assert days[-1] == "2026-11-01"
    assert {"2025-10-29", "2025-11-15", "2021-01-01"} <= set(days)
    assert len(days) == 73 + 3
    assert "2026-11-01" not in near_rpc.target_dates(near_rpc.day_ns("2026-11-01"))
    assert near_rpc.is_target_date("2026-12-01") and near_rpc.is_target_date("2025-10-29")
    assert not near_rpc.is_target_date("2026-11-10") and not near_rpc.is_target_date("2020-10-01")


def test_find_block_at_matches_brute_force_with_missing_heights_and_rate_change(monkeypatch):
    missing = {50, 51, 52, 53, 120, 199, 200, 201, 202}
    chain = FakeChain(0, SECOND, 400, missing=missing)
    for h in range(250, 400):  # block time halves after height 250, like NEAR in 2025
        if h in chain.by_height:
            chain.by_height[h]["header"]["timestamp_nanosec"] = str(250 * SECOND + (h - 250) * SECOND // 2)
    rpc = make_rpc(chain, monkeypatch)
    lo, hi = chain.by_height[0], chain.tip
    existing = sorted(chain.by_height)
    for target in [49 * SECOND, 49 * SECOND + 1, 53 * SECOND + 5, 54 * SECOND, 198 * SECOND + 7, 203 * SECOND - 1,
                   250 * SECOND, 250 * SECOND + SECOND // 4, 312 * SECOND, int(323.7 * SECOND)]:
        expected = max(h for h in existing if int(chain.by_height[h]["header"]["timestamp_nanosec"]) <= target)
        assert near_rpc.find_block_at(rpc, target, lo, hi)["header"]["height"] == expected, target


def test_find_block_at_accepts_synthetic_brackets(monkeypatch):
    chain = FakeChain(0, SECOND, 300)
    rpc = make_rpc(chain, monkeypatch)
    found = near_rpc.find_block_at(rpc, 150 * SECOND + 3, near_rpc.bracket(0, 0), near_rpc.bracket(299, 299 * SECOND))
    assert found["header"]["hash"] == "h150"


def test_classify_real_error_bodies():
    errors = fixture("errors.json")
    outcome, exc = near_rpc.classify(errors["unknown_epoch"])
    assert outcome == "definitive" and exc.cause == "UNKNOWN_EPOCH"
    assert near_rpc.classify(errors["unknown_account"])[1].cause == "UNKNOWN_ACCOUNT"
    assert near_rpc.classify(errors["unknown_block"])[1].cause == "UNKNOWN_BLOCK"
    assert near_rpc.classify(errors["validator_info_unavailable"])[0] == "definitive"
    assert near_rpc.classify(errors["rate_limited"])[0] == "rate_limited"
    assert near_rpc.classify(errors["deprecated_near_org"])[0] == "rate_limited"
    assert near_rpc.classify({"result": {"error": "wasm execution failed", "logs": []}})[0] == "definitive"
    assert near_rpc.classify({"error": {"name": "HANDLER_ERROR", "cause": {"name": "TIMEOUT_ERROR"}}})[0] == "transient"
    assert near_rpc.classify({"error": {"cause": {"name": "GARBAGE_COLLECTED_BLOCK"}}})[0] == "endpoint"
    assert near_rpc.classify({"result": {"height": 1}}) == ("ok", {"height": 1})


def http_429() -> requests.HTTPError:
    response = requests.Response()
    response.status_code = 429
    return requests.HTTPError("429", response=response)


def test_rpc_backs_off_on_429_then_falls_back(monkeypatch):
    seen: list[str] = []
    sleeps: list[float] = []

    def post(url, payload, **_):
        seen.append(url)
        if url == "https://primary.test":
            raise http_429()
        return {"result": {"ok": True}}

    monkeypatch.setattr(near_rpc, "http_post_json", post)
    rpc = near_rpc.Rpc(["https://primary.test", "https://fallback.test"], interval=0, attempts=3, sleep=sleeps.append)
    assert rpc.call("status", []) == {"ok": True}
    assert seen == ["https://primary.test"] * 3 + ["https://fallback.test"]
    assert rpc.stats["rate_limited"] == 3 and rpc.stats["fallback_ok"] == 1
    assert [s for s in sleeps if s >= 1] == [1.0, 2.0]


def test_rpc_definitive_error_is_not_retried_and_exhaustion_raises(monkeypatch):
    errors = fixture("errors.json")
    calls: list[str] = []
    monkeypatch.setattr(near_rpc, "http_post_json", lambda url, payload, **_: calls.append(url) or errors["unknown_epoch"])
    rpc = near_rpc.Rpc(["https://a.test", "https://b.test"], interval=0, attempts=2, sleep=lambda s: None)
    with pytest.raises(near_rpc.RpcError):
        rpc.call("validators", {"block_id": "x"})
    assert calls == ["https://a.test"]

    monkeypatch.setattr(near_rpc, "http_post_json", lambda url, payload, **_: errors["rate_limited"])
    with pytest.raises(near_rpc.RpcUnavailable):
        rpc.call("block", {"block_id": 1})


def test_boundary_row_on_real_blocks_matches_treasury_verified_mint():
    prev_last, last, first = fixture("boundary_blocks.json")["blocks"]
    row = near_rpc.boundary_row(prev_last, last, first)
    assert row == {"boundary_height": 223962737, "block_time_utc": "2026-11-10T19:55:25.520410+00:00",
                   "minted_near": "25760.091347300114256055489323", "epoch_seconds": "24829.860052706",
                   "supply_before_near": "1311344000.075118008903342749654916",
                   "annualized_mint_rate": "0.024949572029083"}
    burnt = sum(int(c["balance_burnt"]) for c in first["chunks"] if c["height_included"] == 223962737)
    delta = int(first["header"]["total_supply"]) - int(last["header"]["total_supply"])
    with localcontext() as ctx:
        ctx.prec = 60
        assert Decimal(delta + burnt) / YOCTO == Decimal(row["minted_near"])
    with pytest.raises(ValueError):
        near_rpc.boundary_row(prev_last, prev_last, first)


def test_epoch_walk_mints_and_burn_adjustment(monkeypatch):
    chain = FakeChain(0, SECOND, 205)
    rows, error = near_rpc.walk_epochs(make_rpc(chain, monkeypatch), chain.tip, 160 * SECOND, [])
    assert error is None
    assert [r["boundary_height"] for r in rows] == [200, 190, 180, 170, 160]
    assert [r["prev_boundary_height"] for r in rows] == [190, 180, 170, 160, 150]
    assert {r["minted_near"] for r in rows} == {"1000"}
    assert {r["epoch_seconds"] for r in rows} == {"10"}
    assert rows[0]["supply_before_near"] == str(1_000_000_000 + 19 * 1000 - 200)
    assert near_rpc.epoch_chain_gaps(rows) == []


def test_epoch_walk_limit_takes_the_newest_boundary(monkeypatch):
    chain = FakeChain(0, SECOND, 205)
    rows, error = near_rpc.walk_epochs(make_rpc(chain, monkeypatch), chain.tip, 0, [], limit=1)
    assert error is None and [r["boundary_height"] for r in rows] == [200]


def cached_epoch_rows(heights: list[int], *, linked: bool = True) -> list[dict]:
    return [{"boundary_height": str(h), "block_time_utc": near_rpc.iso_utc(h * SECOND), "minted_near": "1000",
             "epoch_seconds": "10", "supply_before_near": "", "annualized_mint_rate": "",
             "prev_boundary_height": str(h - 10) if linked else ""} for h in heights]


def test_epoch_walk_follows_cached_links_without_calls(monkeypatch):
    chain = FakeChain(0, SECOND, 205)
    rpc = make_rpc(chain, monkeypatch)
    rows, error = near_rpc.walk_epochs(rpc, chain.tip, 160 * SECOND, cached_epoch_rows([150, 160, 170, 180, 190]))
    assert error is None
    assert [int(r["boundary_height"]) for r in rows] == [200, 190, 180, 170, 160]
    assert len(chain.calls) == 4  # tip's previous epoch end, the newest boundary, its link, the boundary before
    assert near_rpc.epoch_chain_gaps(rows) == []


def test_epoch_walk_refetches_where_the_cached_chain_breaks(monkeypatch):
    chain = FakeChain(0, SECOND, 205)
    cached = cached_epoch_rows([150, 160, 170]) + cached_epoch_rows([120], linked=True)  # 130 and 140 missing
    rows, error = near_rpc.walk_epochs(make_rpc(chain, monkeypatch), chain.tip, 100 * SECOND, cached)
    assert error is None
    assert [int(r["boundary_height"]) for r in rows] == [200, 190, 180, 170, 160, 150, 140, 130, 120, 110, 100]
    assert near_rpc.epoch_chain_gaps(rows) == []
    assert rows[7]["minted_near"] == "1000" and rows[7]["prev_boundary_height"] == 120


def test_epoch_walk_recomputes_unlinked_rows_and_checks_them(monkeypatch):
    chain = FakeChain(0, SECOND, 205)
    legacy = [{k: v for k, v in near_rpc.boundary_row(chain.by_height[h - 11], chain.by_height[h - 1],
                                                      chain.by_height[h]).items()} for h in (170, 180, 190)]
    legacy = [{k: str(v) for k, v in r.items()} for r in legacy]
    rows, error = near_rpc.walk_epochs(make_rpc(chain, monkeypatch), chain.tip, 170 * SECOND, legacy)
    assert error is None and [r["boundary_height"] for r in rows] == [200, 190, 180, 170]
    assert all(r["prev_boundary_height"] for r in rows)

    legacy[1]["minted_near"] = "999"  # a cached row that disagrees with the chain stops the walk
    rows, error = near_rpc.walk_epochs(make_rpc(chain, monkeypatch), chain.tip, 170 * SECOND, legacy)
    assert [r["boundary_height"] for r in rows] == [200, 190] and "recomputed differs" in error


def test_epoch_walk_keeps_partial_progress_on_rate_limit(monkeypatch):
    chain = FakeChain(0, SECOND, 205)

    def flaky(url, payload, **kw):
        return chain(url, payload) if len(chain.calls) < 4 else fixture("errors.json")["rate_limited"]

    monkeypatch.setattr(near_rpc, "http_post_json", flaky)
    rpc = near_rpc.Rpc(["https://archival.test"], interval=0, attempts=1, sleep=lambda s: None)
    rows, error = near_rpc.walk_epochs(rpc, chain.tip, 100 * SECOND, [])
    assert [int(r["boundary_height"]) for r in rows] == [200]
    assert "stopped after 1 boundaries" in error


def test_epoch_walk_checks_every_link_points_about_one_epoch_below(monkeypatch):
    chain = FakeChain(0, SECOND, 205)
    rows, error = near_rpc.walk_epochs(make_rpc(chain, monkeypatch), chain.tip, 160 * SECOND, [], epoch_length=10)
    assert error is None and [r["boundary_height"] for r in rows] == [200, 190, 180, 170, 160]

    looped = cached_epoch_rows([150, 160, 170, 180, 190])
    looped[3]["prev_boundary_height"] = "180"  # a corrupt self-link at 180 would loop forever without the check
    rows, error = near_rpc.walk_epochs(make_rpc(chain, monkeypatch), chain.tip, 0, looped)
    assert [int(r["boundary_height"]) for r in rows] == [200, 190] and "not below it" in error

    skipped = cached_epoch_rows([150, 160, 170, 180, 190])
    skipped[3]["prev_boundary_height"] = "150"  # a link that jumps three epochs
    rows, error = near_rpc.walk_epochs(make_rpc(chain, monkeypatch), chain.tip, 0, skipped, epoch_length=10)
    assert [int(r["boundary_height"]) for r in rows] == [200, 190] and "not one epoch" in error
    assert near_rpc.epoch_chain_gaps(rows) == []


def test_epoch_walk_records_a_missing_block_instead_of_raising(monkeypatch):
    chain = FakeChain(0, SECOND, 205)
    rpc = make_rpc(chain, monkeypatch)
    real = rpc.block_after

    def gone(height, limit=None, max_scan=200):
        return None if height < 195 else real(height, limit, max_scan)

    monkeypatch.setattr(rpc, "block_after", gone)
    rows, error = near_rpc.walk_epochs(rpc, chain.tip, 0, [])
    assert [r["boundary_height"] for r in rows] == [] and "no block for the epoch start before 200" in error
    with pytest.raises(ValueError, match="no block for x"):
        near_rpc.need(None, "x")


def test_epoch_chain_gaps_name_the_broken_link():
    rows = cached_epoch_rows([150, 160, 180])
    assert near_rpc.epoch_chain_gaps(rows) == [{"boundary_height": 180, "prev_boundary_height": "170",
                                                "row_below": 160}]
    assert near_rpc.epoch_chain_gaps(cached_epoch_rows([150, 160], linked=False))[0]["prev_boundary_height"] is None


def hourly_chain(days: int = 20) -> FakeChain:
    """Hourly blocks from 2026-01-01 00:00 UTC in six-hour epochs."""
    return FakeChain(near_rpc.day_ns("2026-01-01"), 3600 * SECOND, 24 * days, epoch_len=6)


def test_epoch_floor_is_days_before_the_latest_dense_date(raw_dir, monkeypatch):
    chain = hourly_chain()
    tip_ns = near_rpc.ts(chain.tip)
    assert near_rpc.iso_utc(tip_ns) == "2026-01-20T23:00:00+00:00"
    assert near_rpc.epoch_floor(tip_ns, 3) == near_rpc.date(2026, 1, 16)  # latest dense date 2026-01-19
    raw_dir.mkdir(parents=True)
    (raw_dir / "dense_samples.csv").write_text("date,height\n2026-01-12,1\n2026-01-17,2\n")
    assert near_rpc.epoch_floor(tip_ns, 3) == near_rpc.date(2026, 1, 14)  # a lagging dense file moves it earlier


def test_pull_epochs_writes_the_window_and_resumes(raw_dir, monkeypatch):
    chain = hourly_chain()
    monkeypatch.setattr(near_rpc, "EPOCH_PROGRESS_EVERY", 4)
    budget = {"left": 15}

    def limited(url, payload, **kw):
        budget["left"] -= 1
        return chain(url, payload) if budget["left"] > 0 else fixture("errors.json")["rate_limited"]

    monkeypatch.setattr(near_rpc, "http_post_json", limited)
    rpc = near_rpc.Rpc(["https://archival.test"], interval=0, attempts=1, sleep=lambda s: None)
    rows, errors = near_rpc.pull_epochs(rpc, chain.tip, 3)
    assert errors and 0 < len(rows) < 20
    partial = read_rows(raw_dir / "epoch_mints.csv")
    assert [int(r["boundary_height"]) for r in partial] == sorted(int(r["boundary_height"]) for r in rows)
    meta = json.loads((raw_dir / "epoch_mints.meta.json").read_text())
    assert meta["complete"] is False and meta["walk_error"]

    chain.calls.clear()
    rows, errors = near_rpc.pull_epochs(make_rpc(chain, monkeypatch), chain.tip, 3)
    assert errors == []
    written = read_rows(raw_dir / "epoch_mints.csv")
    assert list(written[0]) == near_rpc.EPOCH_FIELDS
    heights = [int(r["boundary_height"]) for r in written]
    assert heights == list(range(360, 475, 6))  # every boundary from 2026-01-16 00:00 UTC to the tip
    assert written[0]["block_time_utc"] == "2026-01-16T00:00:00+00:00"
    assert near_rpc.epoch_chain_gaps(written) == []
    assert [r for r in written if r["boundary_height"] in {r2["boundary_height"] for r2 in partial}] == partial
    meta = json.loads((raw_dir / "epoch_mints.meta.json").read_text())
    assert meta["complete"] is True and meta["contiguous"] is True and meta["floor_date"] == "2026-01-16"
    assert meta["rows"] == 20 and meta["walk_error"] is None
    assert len(chain.calls) < 3 * (20 - len(partial)) + 6  # cached links are not fetched again


def test_cached_samples_drop_old_latest_rows():
    rows = [
        {"date": "2026-01-01", "height": "10", "block_time_utc": "2025-12-31T23:59:59.500000+00:00"},
        {"date": "2026-01-01", "height": "99", "block_time_utc": "2026-01-01T05:00:00+00:00"},
        {"date": "2026-01-07", "height": "50", "block_time_utc": "2026-01-07T12:00:00+00:00"},
        {"date": "2026-02-01", "height": "", "block_time_utc": ""},
    ]
    kept = near_rpc.cached_samples(rows, ["2026-01-01", "2026-02-01"])
    assert list(kept) == ["2026-01-01"] and kept["2026-01-01"]["height"] == "10"


@pytest.fixture
def small_history(monkeypatch):
    monkeypatch.setattr(near_rpc, "FIRST_MONTH", near_rpc.date(2026, 1, 1))
    monkeypatch.setattr(near_rpc, "EXTRA_DATES", ("2026-01-15",))
    monkeypatch.setattr(near_rpc, "TIP_MARGIN_NS", 0)
    start = near_rpc.day_ns("2025-12-31")
    return FakeChain(start, 3600 * SECOND, 24 * 63, missing={24, 25, 768}, epoch_len=24, first_epoch_with_info=20)


def test_pull_history_writes_rows_and_is_incremental(raw_dir, monkeypatch, small_history):
    chain = small_history
    archival = make_rpc(chain, monkeypatch)
    latest = near_rpc.Rpc(["https://latest.test"], interval=0, sleep=lambda s: None)
    genesis = near_rpc.genesis_bracket({"genesis_height": 0, "genesis_time": "2025-12-31T00:00:00Z"})
    assert near_rpc.pull_history(archival, latest, chain.tip, genesis) == []

    rows = read_rows(raw_dir / "history_samples.csv")
    assert list(rows[0]) == near_rpc.HISTORY_FIELDS
    assert [r["date"] for r in rows] == ["2026-01-01", "2026-01-15", "2026-02-01", "2026-03-01", "2026-03-03"]
    jan = rows[0]
    assert jan["height"] == "23" and jan["block_time_utc"] == "2025-12-31T23:00:00+00:00"
    assert jan["validator_stake_near"] == "" and jan["n_validators"] == "" and jan["staked_ratio"] == ""
    assert jan["stnear_price"] == "1.500000000000000000000001" and jan["linear_price"] == ""
    assert jan["treasury_near"] == "7.5" and jan["max_inflation_rate"] == "0.05" and jan["protocol_version"] == "80"
    mar = rows[3]
    assert mar["validator_stake_near"] == "400000000.000000000000000000000005" and mar["n_validators"] == "2"
    assert int(mar["stake_epoch_start_height"]) == (int(mar["height"]) // 24 - 2) * 24
    meta = json.loads((raw_dir / "history_samples.meta.json").read_text())
    assert meta["latest_row"] == {"date": "2026-03-03", "height": chain.tip["header"]["height"]}
    assert meta["run"]["fetched"] == ["2026-01-01", "2026-01-15", "2026-02-01", "2026-03-01"]

    chain.calls.clear()
    archival = make_rpc(chain, monkeypatch)
    assert near_rpc.pull_history(archival, latest, chain.tip, genesis) == []
    assert all(url == "https://latest.test" for url, _ in chain.calls)
    assert read_rows(raw_dir / "history_samples.csv") == rows


def test_pull_history_omits_latest_row_when_tip_is_on_a_sampling_date(raw_dir, monkeypatch):
    monkeypatch.setattr(near_rpc, "FIRST_MONTH", near_rpc.date(2026, 1, 1))
    monkeypatch.setattr(near_rpc, "EXTRA_DATES", ("2026-01-15",))
    monkeypatch.setattr(near_rpc, "TIP_MARGIN_NS", 0)
    chain = FakeChain(near_rpc.day_ns("2025-12-31"), 3600 * SECOND, 24 * 60 + 13, missing={24, 25, 768},
                      epoch_len=24, first_epoch_with_info=20)
    assert near_rpc.iso_utc(near_rpc.ts(chain.tip)) == "2026-03-01T12:00:00+00:00"
    archival = make_rpc(chain, monkeypatch)
    latest = near_rpc.Rpc(["https://latest.test"], interval=0, sleep=lambda s: None)
    genesis = near_rpc.genesis_bracket({"genesis_height": 0, "genesis_time": "2025-12-31T00:00:00Z"})
    assert near_rpc.pull_history(archival, latest, chain.tip, genesis) == []

    rows = read_rows(raw_dir / "history_samples.csv")
    assert [r["date"] for r in rows] == ["2026-01-01", "2026-01-15", "2026-02-01", "2026-03-01"]
    assert rows[-1]["height"] == "1440" and rows[-1]["block_time_utc"] == "2026-03-01T00:00:00+00:00"
    assert not any(url == "https://latest.test" for url, _ in chain.calls)
    meta = json.loads((raw_dir / "history_samples.meta.json").read_text())
    assert meta["latest_row"] is None and "2026-03-01" in meta["run"]["latest_omitted"]


def test_pull_history_keeps_partial_progress_on_rate_limit(raw_dir, monkeypatch, small_history):
    chain = small_history
    errors = fixture("errors.json")
    state = {"budget": 25}

    def limited(url, payload, **kw):
        state["budget"] -= 1
        return chain(url, payload) if state["budget"] > 0 or url == "https://latest.test" else errors["rate_limited"]

    monkeypatch.setattr(near_rpc, "http_post_json", limited)
    archival = near_rpc.Rpc(["https://archival.test"], interval=0, attempts=2, sleep=lambda s: None)
    latest = near_rpc.Rpc(["https://latest.test"], interval=0, sleep=lambda s: None)
    genesis = near_rpc.genesis_bracket({"genesis_height": 0, "genesis_time": "2025-12-31T00:00:00Z"})
    failures = near_rpc.pull_history(archival, latest, chain.tip, genesis)
    rows = read_rows(raw_dir / "history_samples.csv")
    fetched = [r["date"] for r in rows if r["date"] != "2026-03-03"]
    assert failures and len(fetched) < 4
    assert all("failed on every endpoint" in f for f in failures)
    meta = json.loads((raw_dir / "history_samples.meta.json").read_text())
    assert set(meta["run"]["failed"]) == {d for d in ["2026-01-01", "2026-01-15", "2026-02-01", "2026-03-01"]
                                          if d not in fetched}


def test_snapshot_shape_and_epoch_csv(raw_dir, monkeypatch):
    chain = FakeChain(0, SECOND, 205)
    rpc = make_rpc(chain, monkeypatch)
    rows, error = near_rpc.walk_epochs(rpc, chain.tip, 180 * SECOND, [])
    assert error is None and [r["boundary_height"] for r in rows] == [200, 190, 180]

    real_validators = {"result": {"current_validators": [
        {"account_id": "a.poolv1.near", "stake": str(3 * YOCTO)}, {"account_id": "b.poolv1.near", "stake": str(YOCTO)}],
        "epoch_start_height": 200}}

    def route(url, payload, **kw):
        if payload["method"] == "validators":
            assert payload["params"] == {"epoch_id": chain.tip["header"]["epoch_id"]}
            return real_validators
        params = payload["params"]
        if payload["method"] == "query" and params.get("method_name") == "get_reward_fee_fraction":
            if params["account_id"] == "b.poolv1.near":
                return fixture("errors.json")["unknown_account"]
            return {"result": {"result": list(json.dumps({"numerator": 5, "denominator": 100}).encode())}}
        if payload["method"] == "query" and params.get("method_name") in ("ft_metadata", "ft_total_supply"):
            body = {"symbol": "USDt", "decimals": 6} if params["method_name"] == "ft_metadata" else "1234567"
            return {"result": {"result": list(json.dumps(body).encode())}}
        return chain(url, payload)

    monkeypatch.setattr(near_rpc, "http_post_json", route)
    config_view = chain(None, {"method": "EXPERIMENTAL_protocol_config", "params": {}})["result"]
    snap = near_rpc.build_snapshot(rpc, near_rpc.slim_block(chain.tip), config_view, rows[0])
    assert snap["height"] == 204 and snap["gas_price"] == 100000000
    assert snap["max_inflation_rate"] == 0.05 and snap["protocol_reward_rate_rpc"] == [0, 1]
    assert snap["total_stake_near"] == 4.0
    assert snap["current_validators"][0] == {"account_id": "a.poolv1.near", "stake_near": 3.0}
    assert snap["pool_fees"][0]["fee_fraction"] == 0.05 and snap["pool_fees"][1]["fee_fraction"] is None
    assert snap["last_epoch_boundary"]["height"] == 200 and snap["last_epoch_boundary"]["minted_near"] == 1000.0
    assert snap["lst"]["stnear_price"] == float(Decimal("1.500000000000000000000001"))
    assert snap["lst"]["linear_price"] is None and snap["lst"]["metapool_state"] is None
    assert snap["ft_supplies"][0] == {"contract": "usdt.tether-token.near", "symbol": "USDt", "decimals": 6,
                                      "total_supply": 1.234567, "total_supply_raw": "1234567"}
    assert len(snap["ft_supplies"]) == 8

    near_rpc.write_snapshot(snap, rpc)
    meta = json.loads((raw_dir / "snapshot_now.meta.json").read_text())
    assert meta["rows"] == 1 and meta["file"] == "data/raw/rpc/snapshot_now.json"


def test_number_formatting_is_exact_and_missing_stays_empty():
    assert near_rpc.fmt(near_rpc.near("1311369666584115506324521205144239")) == "1311369666.584115506324521205144239"
    assert near_rpc.fmt(near_rpc.near(10**24)) == "1"
    assert near_rpc.fmt(None) == ""
    assert near_rpc.fmt(near_rpc.rational([1, 40])) == "0.025"
    assert near_rpc.rational(None) is None and near_rpc.ratio(Decimal(1), None) is None
    assert near_rpc.iso_utc("1602719975137985720") == "2020-10-14T23:59:35.137985+00:00"


# --- dense samples, protocol changes, epoch history ------------------------------------------------------------------

def http_error(status: int, body: dict) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status
    response._content = json.dumps(body).encode()
    return requests.HTTPError(str(status), response=response)


def test_http_422_json_rpc_error_is_definitive_not_retried(monkeypatch):
    """FastNEAR answers a height with no block as HTTP 422 + JSON-RPC UNKNOWN_BLOCK (seen live 2026-11-10)."""
    seen: list[str] = []

    def post(url, payload, **_):
        seen.append(url)
        raise http_error(422, fixture("errors.json")["unknown_block"])

    monkeypatch.setattr(near_rpc, "http_post_json", post)
    rpc = near_rpc.Rpc(["https://a.test", "https://b.test"], interval=0, attempts=3, sleep=lambda s: None)
    assert rpc.block(9820211) is None and rpc.block(9820211) is None
    assert seen == ["https://a.test"] and rpc.stats["missing_heights"] == 1

    seen.clear()
    monkeypatch.setattr(near_rpc, "http_post_json", lambda url, payload, **_: seen.append(url) or (_ for _ in ()).throw(
        http_error(500, {"error": {"name": "HANDLER_ERROR", "cause": {"name": "TIMEOUT_ERROR"}}})))
    with pytest.raises(near_rpc.RpcUnavailable):
        rpc.call("block", {"block_id": 1})
    assert len(seen) == 6  # transient: retried on both endpoints


def test_dense_dates_weekly_then_daily_excluding_today():
    tip = near_rpc.day_ns("2026-11-10") + (23 * 3600 + 48 * 60) * SECOND
    days = near_rpc.dense_dates(tip)
    assert days[0] == "2022-04-04"  # first Monday on/after 2022-04-01 (a Friday)
    daily_from = days.index("2025-10-06")  # 400 days before 2026-11-10
    weekly, daily = days[:daily_from], days[daily_from:]
    assert all(near_rpc.date.fromisoformat(d).weekday() == 0 for d in weekly)
    assert weekly[-1] == "2025-09-29" and len(weekly) == 183
    assert len(daily) == 400 and daily[-1] == "2026-11-09" and "2026-11-10" not in days
    assert len(set(days)) == len(days) == sorted(days).__len__() and days == sorted(days)
    assert {"2025-11-01", "2025-11-15", "2026-11-01", "2022-08-01"} <= set(days)  # history dates overlap
    assert "2022-05-01" not in days  # a Sunday in the weekly span
    # Just after midnight: yesterday is in (its 00:00 is a day old), today is not.
    early = near_rpc.dense_dates(near_rpc.day_ns("2026-11-10") + 5 * 60 * SECOND)
    assert early[-1] == "2026-11-09" and early[0] == "2022-04-04"
    assert near_rpc.dense_dates(near_rpc.day_ns("2026-11-11"))[-1] == "2026-11-10"


def jittered_chain(n: int, missing: set[int]) -> FakeChain:
    """~0.6 s blocks with +-0.25 s jitter, slowing to ~1.1 s after height n//2 (seeded, deterministic)."""
    import random
    chain = FakeChain(0, SECOND, n, missing=missing)
    rng = random.Random(11)
    for h in sorted(chain.by_height):
        base = h * 600 if h < n // 2 else (n // 2) * 600 + (h - n // 2) * 1100
        chain.by_height[h]["header"]["timestamp_nanosec"] = str((base + rng.randint(-250, 250)) * 10**6)
    return chain


def test_find_block_fast_matches_brute_force_with_missing_heights(monkeypatch):
    missing = {500, 501, 502, 1777, 3000, 3001, 4999, 5000, 5001, 5002, 5003, 7210}
    chain = jittered_chain(9000, missing)
    rpc = make_rpc(chain, monkeypatch)
    existing = sorted(chain.by_height)
    stamps = [near_rpc.ts(chain.by_height[h]) for h in existing]

    def brute(target):
        return max(h for h, t in zip(existing, stamps) if t <= target)

    targets = [int(t * 10**6) for t in range(1000, 7_000_000, 77_777)]
    targets += [stamps[existing.index(h)] for h in (499, 503, 1776, 1778, 2999, 4998, 5004, 7209)]  # exact hits
    calls_far, calls_near = [], []
    for target in targets:
        expected = brute(target)
        lo, hi = chain.by_height[0], chain.tip
        before = len(chain.calls)
        rpc._blocks.clear()
        assert near_rpc.find_block_fast(rpc, target, lo, hi)["header"]["height"] == expected, target
        calls_far.append(len(chain.calls) - before)
        # Near brackets as in pull_dense: known samples ~2000 blocks either side, guess from the span before lo.
        lo_h = max(h for h in existing if h <= expected - 2000) if expected > 2100 else 0
        prev_h = max(h for h in existing if h <= lo_h - 2000) if lo_h > 2100 else None
        hi_h = min(h for h in existing if h >= expected + 2000) if expected + 2000 < existing[-1] else existing[-1]
        pts = ([(near_rpc.ts(chain.by_height[prev_h]), prev_h)] if prev_h is not None else []) + [
            (near_rpc.ts(chain.by_height[lo_h]), lo_h), (near_rpc.ts(chain.by_height[hi_h]), hi_h)]
        lo_b, hi_b, guess = near_rpc.dense_bracket(pts, target)
        assert "hash" not in lo_b["header"] and lo_b["header"]["height"] == lo_h
        rpc._blocks.clear()
        before = len(chain.calls)
        assert near_rpc.find_block_fast(rpc, target, lo_b, hi_b, guess)["header"]["height"] == expected, target
        calls_near.append(len(chain.calls) - before)
    print("find_block_fast calls: near", sum(calls_near) / len(calls_near), "far", sum(calls_far) / len(calls_far),
          "n", len(targets))
    assert sum(calls_near) / len(calls_near) <= 6, sum(calls_near) / len(calls_near)
    assert sum(calls_far) / len(calls_far) <= 12, sum(calls_far) / len(calls_far)
    assert max(calls_near) <= 20


@pytest.fixture
def epoch_chain(monkeypatch):
    """Hourly blocks, 24-block epochs starting 06:00 UTC, some epoch first/last heights missing; tip 2026-03-01 12:00."""
    monkeypatch.setattr(near_rpc, "FIRST_MONTH", near_rpc.date(2026, 1, 1))
    monkeypatch.setattr(near_rpc, "EXTRA_DATES", ("2026-01-15",))
    monkeypatch.setattr(near_rpc, "TIP_MARGIN_NS", 0)
    monkeypatch.setattr(near_rpc, "DENSE_FIRST", near_rpc.date(2026, 1, 1))
    monkeypatch.setattr(near_rpc, "DENSE_DAILY_DAYS", 10)
    start = near_rpc.day_ns("2025-12-20") + 6 * 3600 * SECOND
    return FakeChain(start, 3600 * SECOND, 1711, missing={600, 623, 1031, 1032, 1305}, epoch_len=24,
                     genesis_time="2025-12-20T06:00:00Z",
                     param_changes={1000: {"protocol_version": 81, "max_inflation_rate": [1, 40]},
                                    1306: {"protocol_version": 87,
                                           "runtime_config": {"transaction_costs": {"burnt_gas_reward": [0, 1]}}}})


def history_for(chain: FakeChain, monkeypatch) -> None:
    archival = make_rpc(chain, monkeypatch)
    latest = near_rpc.Rpc(["https://latest.test"], interval=0, sleep=lambda s: None)
    genesis = near_rpc.genesis_bracket(chain.config_at(None))
    assert near_rpc.pull_history(archival, latest, chain.tip, genesis) == []


def test_pull_dense_rows_overlap_and_incremental(raw_dir, monkeypatch, epoch_chain):
    chain = epoch_chain
    history_for(chain, monkeypatch)
    chain.calls.clear()
    rpc = make_rpc(chain, monkeypatch)
    genesis = near_rpc.genesis_bracket(chain.config_at(None))
    assert near_rpc.pull_dense(rpc, chain.tip, genesis) == []

    rows = read_rows(raw_dir / "dense_samples.csv")
    assert list(rows[0]) == near_rpc.DENSE_FIELDS
    dates = [r["date"] for r in rows]
    assert dates[:7] == ["2026-01-05", "2026-01-12", "2026-01-19", "2026-01-26", "2026-02-02", "2026-02-09",
                         "2026-02-16"]
    assert dates[7:] == ["2026-02-19", "2026-02-20", "2026-02-21", "2026-02-22", "2026-02-23", "2026-02-24",
                         "2026-02-25", "2026-02-26", "2026-02-27", "2026-02-28"]
    existing = sorted(chain.by_height)
    for row in rows:
        target = near_rpc.day_ns(row["date"])
        expected = max(h for h in existing if near_rpc.ts(chain.by_height[h]) <= target)
        assert int(row["height"]) == expected
        assert row["stnear_price"] == "1.500000000000000000000001" and row["linear_price"] == ""
    meta = json.loads((raw_dir / "dense_samples.meta.json").read_text())
    assert meta["run"]["fetched"] == 17 and meta["run"]["failed"] == {}
    assert meta["run"]["avg_calls_per_row"] <= 8
    assert meta["schedule"]["daily_from"] == "2026-02-19" and meta["schedule"]["daily_through"] == "2026-02-28"
    assert all(url == "https://archival.test" for url, _ in chain.calls)

    assert meta["cumulative"]["fetched"] == 17 and meta["cumulative"]["calls"] == meta["run"]["calls"]

    chain.calls.clear()
    assert near_rpc.pull_dense(make_rpc(chain, monkeypatch), chain.tip, genesis) == []
    assert chain.calls == []
    assert read_rows(raw_dir / "dense_samples.csv") == rows
    again = json.loads((raw_dir / "dense_samples.meta.json").read_text())
    assert again["run"]["fetched"] == 0 and again["run"]["avg_calls_per_row"] is None
    assert again["cumulative"] == meta["cumulative"]  # the cold run's counts survive an incremental run


def test_dense_overlap_with_history_is_found_independently_and_equal(raw_dir, monkeypatch, epoch_chain):
    chain = epoch_chain
    monkeypatch.setattr(near_rpc, "DENSE_DAILY_DAYS", 40)  # daily window now covers 2026-02-01 and 2026-03-01
    history_for(chain, monkeypatch)
    rpc = make_rpc(chain, monkeypatch)
    assert near_rpc.pull_dense(rpc, chain.tip, near_rpc.genesis_bracket(chain.config_at(None))) == []
    dense = {r["date"]: r for r in read_rows(raw_dir / "dense_samples.csv")}
    history = {r["date"]: r for r in read_rows(raw_dir / "history_samples.csv")}
    overlap = sorted(set(dense) & set(history))
    assert overlap == ["2026-02-01"]  # 2026-03-01 is the tip's date, so neither file samples it as a day yet
    for day in overlap:
        for key in ("height", "block_time_utc", "total_supply_near", "stnear_price", "linear_price"):
            assert dense[day][key] == history[day][key], (day, key)
    meta = json.loads((raw_dir / "dense_samples.meta.json").read_text())
    assert meta["overlap_with_history"]["dates"] == 1 and meta["overlap_with_history"]["mismatched"] == {}


def test_bisect_change_finds_first_height_across_missing_heights(monkeypatch):
    chain = FakeChain(0, SECOND, 3000, missing={1497, 1498, 1499, 2100},
                      param_changes={1500: {"max_inflation_rate": [1, 40]}, 2101: {"max_inflation_rate": [1, 80]}})
    rpc = make_rpc(chain, monkeypatch)
    probe = near_rpc.ConfigProbe(rpc, {})
    assert near_rpc.bisect_change(probe, "max_inflation_rate", (10, "0.05"), (1600, "0.025")) == [
        (1496, 1500, "0.05", "0.025")]
    # Two changes inside one interval: the middle value splits it.
    assert near_rpc.bisect_change(probe, "max_inflation_rate", (10, "0.05"), (2900, "0.0125")) == [
        (1496, 1500, "0.05", "0.025"), (2099, 2101, "0.025", "0.0125")]
    row, check = near_rpc.change_row(rpc, probe, "max_inflation_rate", (1496, 1500, "0.05", "0.025"))
    assert row["first_height"] == 1500 and row["prev_height"] == 1496 and row["protocol_version"] == "80"
    assert check["parent_hash_verified"] and check["first_block_starts_epoch"] is True
    with pytest.raises(ValueError):
        near_rpc.change_row(rpc, probe, "max_inflation_rate", (1400, 1500, "0.05", "0.025"))


def test_pull_protocol_changes_bisects_then_reuses_cache(raw_dir, monkeypatch, epoch_chain):
    chain = epoch_chain
    history_for(chain, monkeypatch)
    rpc = make_rpc(chain, monkeypatch)
    tip_config = chain.config_at(chain.tip["header"]["height"])
    assert near_rpc.pull_protocol_changes(rpc, chain.tip, tip_config) == []
    samples = read_rows(raw_dir / "protocol_config_samples.csv")
    assert list(samples[0]) == near_rpc.CONFIG_SAMPLE_FIELDS
    assert [s["date"] for s in samples] == ["2026-01-01", "2026-01-15", "2026-02-01", "2026-03-01", "2026-03-01"]
    assert samples[-1]["height"] == str(chain.tip["header"]["height"]) and samples[-1]["burnt_gas_reward"] == "0"
    assert samples[0]["burnt_gas_reward"] == "0.3" and samples[0]["max_inflation_rate"] == "0.05"
    rows = read_rows(raw_dir / "protocol_changes.csv")
    assert list(rows[0]) == near_rpc.CHANGE_FIELDS
    assert [(r["param"], r["old_value"], r["new_value"], r["first_height"], r["prev_height"], r["protocol_version"])
            for r in rows] == [("max_inflation_rate", "0.05", "0.025", "1000", "999", "81"),
                               ("burnt_gas_reward", "0.3", "0", "1306", "1304", "87")]
    assert rows[1]["block_time_utc"] == near_rpc.iso_utc(near_rpc.ts(chain.by_height[1306]))
    meta = json.loads((raw_dir / "protocol_changes.meta.json").read_text())
    assert meta["checks"]["burnt_gas_reward@1306"]["parent_hash_verified"] is True
    assert meta["run"]["failed"] == {}
    sample_meta = json.loads((raw_dir / "protocol_config_samples.meta.json").read_text())
    for m in (meta, sample_meta):
        assert m["sampling"] and m["units"]  # every sidecar states its sampling rule and units

    chain.calls.clear()
    assert near_rpc.pull_protocol_changes(make_rpc(chain, monkeypatch), chain.tip, tip_config) == []
    assert chain.calls == []
    assert read_rows(raw_dir / "protocol_changes.csv") == rows


def test_epoch_history_row_on_recorded_epoch_4956():
    data = fixture("epoch_blocks.json")
    last_prev, start, end, next_start = data["blocks"]
    row = near_rpc.epoch_history_row("2026-11-10", last_prev, start, end, next_start, "88", "0.025")
    assert row["start_height"] == data["validators_223962736"]["epoch_start_height"] == 223919537
    assert (row["end_height"], row["next_start_height"]) == (223962736, 223962737)
    assert row["epoch_id"] == start["header"]["epoch_id"] == end["header"]["epoch_id"]
    assert row["epoch_seconds"] == "24829.860052706"
    assert row["minted_next_boundary_near"] == "25760.091347300114256055489323"  # = epoch_mints row for 223962737
    with localcontext() as ctx:
        ctx.prec = 80
        start_burn = sum(int(c["balance_burnt"]) for c in start["chunks"] if c["height_included"] == 223919537)
        supply_start = Decimal(int(start["header"]["total_supply"])) / YOCTO
        supply_end = Decimal(int(end["header"]["total_supply"])) / YOCTO
        burnt = supply_start - supply_end + Decimal(start_burn) / YOCTO
        assert Decimal(row["supply_start_near"]) == supply_start and Decimal(row["supply_end_near"]) == supply_end
        assert Decimal(row["burnt_in_epoch_near"]) == burnt
        assert Decimal("357.68") < burnt < Decimal("357.71")  # research: 357.68 from supply alone + start-block burn
        base = supply_start * Decimal("24829.860052706")
        q = Decimal("1e-15")
        assert Decimal(row["annualized_mint_rate"]) == (Decimal(row["minted_next_boundary_near"]) * 31536000 / base
                                                        ).quantize(q)
        assert Decimal(row["annualized_burn_rate"]) == (burnt * 31536000 / base).quantize(q)
    assert Decimal("0.0249") < Decimal(row["annualized_mint_rate"]) < Decimal("0.025")
    assert Decimal("0.0003") < Decimal(row["annualized_burn_rate"]) < Decimal("0.0004")
    with pytest.raises(ValueError):
        near_rpc.epoch_history_row("x", last_prev, end, end, next_start)


def test_pull_epoch_history_synthetic_epochs_and_incremental(raw_dir, monkeypatch, epoch_chain):
    chain = epoch_chain
    history_for(chain, monkeypatch)
    rpc = make_rpc(chain, monkeypatch)
    assert near_rpc.pull_epoch_history(rpc, chain.tip, 24) == []
    rows = {r["sample_date"]: r for r in read_rows(raw_dir / "epoch_history.csv")}
    assert list(next(iter(rows.values()))) == near_rpc.EPOCH_HISTORY_FIELDS
    assert sorted(rows) == ["2026-01-01", "2026-01-15", "2026-02-01", "2026-03-01"]
    jan15 = rows["2026-01-15"]  # sample 618, epoch 25 = heights 600..623 with 600 and 623 missing
    assert (jan15["start_height"], jan15["end_height"], jan15["next_start_height"]) == ("601", "622", "624")
    assert jan15["epoch_seconds"] == str(23 * 3600)  # ts(622) - ts(599)
    assert jan15["minted_next_boundary_near"] == "1000" and jan15["burnt_in_epoch_near"] == "22"
    feb = rows["2026-02-01"]  # epoch 42 = 1008..1031, 1031 and 1032 missing
    assert (feb["start_height"], feb["end_height"], feb["next_start_height"]) == ("1008", "1030", "1033")
    assert feb["burnt_in_epoch_near"] == "23" and feb["minted_next_boundary_near"] == "1000"
    assert feb["protocol_version"] == "81" and feb["max_inflation_rate"] == "0.025"
    with localcontext() as ctx:
        ctx.prec = 60
        rate = Decimal(1000) * 31536000 / (Decimal(feb["supply_start_near"]) * Decimal(feb["epoch_seconds"]))
        assert Decimal(feb["annualized_mint_rate"]) == rate.quantize(Decimal("1e-15"))
    meta = json.loads((raw_dir / "epoch_history.meta.json").read_text())
    assert meta["start_height_check"] == {d: "match" for d in rows}
    assert meta["sampling"] and meta["units"] and "RectifyInflation" in meta["columns_doc"]["annualized_mint_rate"]
    assert meta["mint_rate_above_max_inflation_rate"] == {
        d: {"annualized_mint_rate": r["annualized_mint_rate"], "max_inflation_rate": r["max_inflation_rate"],
            "protocol_version": r["protocol_version"]}
        for d, r in rows.items() if Decimal(r["annualized_mint_rate"]) > Decimal(r["max_inflation_rate"])}

    chain.calls.clear()
    assert near_rpc.pull_epoch_history(make_rpc(chain, monkeypatch), chain.tip, 24) == []
    assert chain.calls == []


def test_mint_rate_above_max_lists_only_rows_over_the_cap():
    rows = {"2020-11-01": {"annualized_mint_rate": "0.062868065796997", "max_inflation_rate": "0.05",
                           "protocol_version": "36"},
            "2021-06-01": {"annualized_mint_rate": "0.05", "max_inflation_rate": "0.05", "protocol_version": "45"},
            "2025-11-01": {"annualized_mint_rate": "0.02494", "max_inflation_rate": "0.025", "protocol_version": "81"},
            "2026-01-01": {"annualized_mint_rate": "", "max_inflation_rate": "0.025", "protocol_version": "82"}}
    assert near_rpc.mint_rate_above_max(rows) == {"2020-11-01": {"annualized_mint_rate": "0.062868065796997",
                                                                 "max_inflation_rate": "0.05",
                                                                 "protocol_version": "36"}}


def test_epoch_history_skips_epoch_not_complete_by_tip(raw_dir, monkeypatch, epoch_chain):
    short = FakeChain(near_rpc.day_ns("2025-12-20") + 6 * 3600 * SECOND, 3600 * SECOND, 1702, epoch_len=24,
                      genesis_time="2025-12-20T06:00:00Z")
    assert short.tip["header"]["height"] == 1701  # 2026-03-01 03:00; epoch 70 (1680..1703) still running
    history_for(short, monkeypatch)
    assert near_rpc.pull_epoch_history(make_rpc(short, monkeypatch), short.tip, 24) == []
    assert [r["sample_date"] for r in read_rows(raw_dir / "epoch_history.csv")] == ["2026-01-01", "2026-01-15",
                                                                                     "2026-02-01"]
    assert json.loads((raw_dir / "epoch_history.meta.json").read_text())["run"]["incomplete"] == ["2026-03-01"]


def test_epoch_start_falls_back_to_bisection_on_epoch_id(monkeypatch):
    chain = FakeChain(0, SECOND, 400, missing={240, 241}, epoch_len=24)
    sample = chain.by_height[250]  # epoch 10 = 240..263, first existing block 242
    real = chain.__call__

    def no_hash_lookup(url, payload, **kw):
        if payload["method"] == "block" and payload["params"].get("block_id") == sample["header"]["next_epoch_id"]:
            return fixture("errors.json")["unknown_block"]
        return real(url, payload)

    monkeypatch.setattr(near_rpc, "http_post_json", no_hash_lookup)
    rpc = near_rpc.Rpc(["https://archival.test"], interval=0, sleep=lambda s: None)
    last_prev, start = near_rpc.epoch_start(rpc, sample, 24)
    assert (last_prev["header"]["height"], start["header"]["height"]) == (239, 242)
    end, following = near_rpc.epoch_end(rpc, start, 24, 399)
    assert (end["header"]["height"], following["header"]["height"]) == (263, 264)


def test_rpc_dense_0_skips_new_outputs(raw_dir, monkeypatch, epoch_chain):
    import functools
    chain = epoch_chain
    monkeypatch.setattr(near_rpc, "http_post_json", chain)
    monkeypatch.setattr(near_rpc, "Rpc", functools.partial(near_rpc.Rpc, interval=0, sleep=lambda s: None))
    args = near_rpc.argparse.Namespace(parts="history,changes,epoch_history,dense", epochs=None)
    monkeypatch.setenv("RPC_DENSE", "0")
    near_rpc.pull(args)
    assert (raw_dir / "history_samples.csv").exists()
    for name in ("dense_samples", "protocol_changes", "protocol_config_samples", "epoch_history"):
        assert not (raw_dir / f"{name}.csv").exists(), name
    methods = {m for _, m in chain.calls}
    assert methods <= {"block", "validators", "EXPERIMENTAL_protocol_config", "query"}

    monkeypatch.delenv("RPC_DENSE")
    near_rpc.pull(args)
    for name in ("dense_samples", "protocol_changes", "protocol_config_samples", "epoch_history"):
        assert (raw_dir / f"{name}.csv").exists(), name
    assert near_rpc.DEFAULT_PARTS.split(",") == ["history", "epochs", "snapshot", "changes", "epoch_history", "dense"]
    assert near_rpc.DENSE_PARTS == ("changes", "epoch_history", "dense")


def test_loaders_parse_new_outputs(raw_dir, monkeypatch, epoch_chain):
    from pipeline.models import load
    monkeypatch.setattr(load, "RAW_DIR", config.RAW_DIR)
    monkeypatch.setattr(load, "ROOT", config.ROOT)
    chain = epoch_chain
    history_for(chain, monkeypatch)
    rpc = make_rpc(chain, monkeypatch)
    near_rpc.pull_dense(rpc, chain.tip, near_rpc.genesis_bracket(chain.config_at(None)))
    near_rpc.pull_protocol_changes(rpc, chain.tip, chain.config_at(chain.tip["header"]["height"]))
    near_rpc.pull_epoch_history(rpc, chain.tip, 24)

    dense, files = load.rpc_dense()
    assert files == ["data/raw/rpc/dense_samples.csv"] and len(dense) == 17
    assert dense[0]["date"] == near_rpc.date(2026, 1, 5) and isinstance(dense[0]["height"], float)
    assert dense[0]["linear_price"] is None and dense[0]["stnear_price"] == 1.5
    changes, files = load.rpc_protocol_changes()
    assert files == ["data/raw/rpc/protocol_changes.csv"]
    assert [(c["param"], c["old_value"], c["new_value"], c["first_height"]) for c in changes] == [
        ("max_inflation_rate", 0.05, 0.025, 1000.0), ("burnt_gas_reward", 0.3, 0.0, 1306.0)]
    epochs, files = load.rpc_epoch_history()
    assert files == ["data/raw/rpc/epoch_history.csv"] and [e["sample_date"] for e in epochs] == [
        near_rpc.date(2026, 1, 1), near_rpc.date(2026, 1, 15), near_rpc.date(2026, 2, 1), near_rpc.date(2026, 3, 1)]
    assert epochs[1]["burnt_in_epoch_near"] == 22.0 and epochs[1]["epoch_id"].startswith("h")
    rows, _ = load.rpc_samples()  # unchanged loader still reads history_samples
    assert len(rows) == 4
