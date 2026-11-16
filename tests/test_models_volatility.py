"""Offline tests for pipeline.models.volatility (chart c25; claim H10) on synthetic raw files.

Each test writes a made-up NEAR close path (Binance daily klines), a constant or gapped DFF and liquid-staking
samples whose price grows at a chosen yield, then reads the strategies, the shuffles and the registry keys. Tests
that only need the strategies set NULL_DRAWS low; one test runs the full parameter count on a short span. The perp
ladder's rows of c25 come from pipeline.models.perps (tests/test_models_perps.py).
"""

from __future__ import annotations

import csv
import json
import math
import random
import re
import shutil
import statistics
import sys
import tomllib
from datetime import date, timedelta
from pathlib import Path

import pytest

from pipeline import config, numbers
from pipeline.chartspec import dump
from pipeline.models import load, verdicts, volatility as V
from pipeline.models.params import RULES_PATH, param
from pipeline.models.portfolio import Prng, q7
from pipeline.numbers import Registry

sys.path.insert(0, str(Path(__file__).parent))
import test_no_traces as NT  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
STAMP = "2026-11-10T00:00:00+00:00"
D0 = date(2021, 1, 1)
H = 365
RPC_COLS = ["date", "height", "block_time_utc", "epoch_id", "total_supply_near", "validator_stake_near",
            "n_validators", "stake_epoch_start_height", "staked_ratio", "stnear_price", "linear_price",
            "treasury_near", "protocol_version", "max_inflation_rate"]
FEW = 20     # shuffles for the tests that only need the strategies


@pytest.fixture
def raw(tmp_path, monkeypatch):
    raw_dir = tmp_path / "data" / "raw"
    raw_dir.mkdir(parents=True)
    for mod in (config, load, numbers):
        monkeypatch.setattr(mod, "ROOT", tmp_path)
        monkeypatch.setattr(mod, "RAW_DIR", raw_dir)
    (tmp_path / "thesis").mkdir()
    for name in ("constants.toml", "sources.toml"):
        shutil.copy(REPO / "thesis" / name, tmp_path / "thesis" / name)
    monkeypatch.setattr(V, "NULL_DRAWS", FEW)
    return tmp_path


def put_csv(root: Path, rel: str, header: list[str], rows: list[list]) -> None:
    path = root / "data" / "raw" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows([["" if v is None else v for v in r] for r in rows])
    path.with_suffix(".meta.json").write_text(json.dumps({"fetched_at": STAMP, "source_url": "test"}))


def day(k: int) -> date:
    return D0 + timedelta(days=k)


def world(root: Path, path: list[float], *, dff: float = 3.0, lst_growth: float = 1.05, lst_every: int = 1,
          close_skip: tuple[int, ...] = (), dff_skip: tuple[int, ...] = (), plant: dict[int, float] | None = None):
    """Closes path[k] on day k from D0; DFF `dff` percent every day from ten days before D0 (blank on dff_skip);
    liquid-staking samples every `lst_every` days, priced lst_growth^(k/365.25), plus planted {day: price} rows."""
    n = len(path)
    put_csv(root, "market/NEARUSDT_binance_daily.csv",
            ["date", "open", "high", "low", "close", "volume_base", "volume_quote"],
            [[day(k).isoformat(), 1, 1, 1, repr(p), 1, 1] for k, p in enumerate(path) if k not in close_skip])
    put_csv(root, "fred/DFF.csv", ["date", "value"],
            [[day(k).isoformat(), None if k in dff_skip else dff] for k in range(-10, n + 10)])
    prices = {k: lst_growth ** (k / 365.25) for k in range(0, n + 2) if k % lst_every == 0}
    prices.update(plant or {})
    put_csv(root, "rpc/history_samples.csv", RPC_COLS,
            [[day(k).isoformat(), 1000 + k, None, "E", 1e9, None, None, None, None, repr(p), repr(p), None, 80,
              0.025] for k, p in sorted(prices.items())])


def wiggle(n: int, seed: int = 7, sd: float = 0.04) -> list[float]:
    rng = random.Random(seed)
    out, x = [], 0.0
    for _ in range(n):
        out.append(round(5 * math.exp(x), 10))
        x += rng.gauss(0, sd)
    return out


def registry(share: float | None = 10) -> Registry:
    reg = Registry()
    if share is not None:
        reg.put("sim_share", share, "%", "FAKE")
    return reg


def by_start(res: V.Result) -> dict[date, tuple[V.Win, dict]]:
    return {v.s: (v, p) for v, p in zip(res.wins, res.per)}


def log_returns(path: list[float]) -> dict[date, float]:
    return {day(k): math.log(path[k] / path[k - 1]) for k in range(1, len(path))}


def h10_rule() -> dict:
    with RULES_PATH.open("rb") as fh:
        return tomllib.load(fh)["H10"]


# --------------------------------------------------------------------------- 1. a flat path

def test_flat_path_never_sells_and_rebalancing_gains_nothing(raw):
    # LST grows at exactly the cash rate, so u(k) = C(k) and the two assets of strategy (b) are the same
    world(raw, [2.0] * 800, dff=4.0, lst_growth=(1 + 4 / 36000) ** 365.25)
    reg = registry()
    V.compute(reg)
    res = V.result(reg)
    assert res.wins and all(p["taus"] == [None] * len(res.levels) for p in res.per)
    assert reg.value("vol_rally_mean") == 0 and reg.value("vol_rally_sold") == 0
    assert reg.value("vol_rebal_mean") == pytest.approx(0, abs=1e-6)
    assert all(p["rb"] == pytest.approx(p["bh"], rel=1e-12) for p in res.per)


def test_black_scholes_at_zero_volatility_is_the_intrinsic_value():
    assert V.bs_call(1.0, 1.0, 1.0, 0.0, 0.05, 0.01) == pytest.approx(math.exp(-0.01) - math.exp(-0.05), abs=1e-15)
    assert V.bs_call(1.0, 1.0, 1.0, 0.0, 0.01, 0.05) == 0.0
    assert V.bs_call(1.0, 1.0, 1.0, 1e-9, 0.05, 0.01) == pytest.approx(math.exp(-0.01) - math.exp(-0.05), abs=1e-9)


# --------------------------------------------------------------------------- 2. selling into rallies on a hand path

def rally_path(n: int = 420) -> list[float]:
    return [1.0] * 40 + [1.25, 1.3] + [1.1] * (n - 42)


def test_first_close_at_the_trigger_sells_at_the_next_close(raw):
    world(raw, rally_path(), dff=3.0)
    res = V.run(registry())
    v, p = res.wins[0], res.per[0]
    assert v.s == D0
    main = res.levels.index(V.RALLY_PCT)
    assert p["taus"][main] == 40                      # +25% exactly on day 40 counts as reached
    c = 1 + 3 / 36000
    v_a = 1.3 * (1 + v.y_w) ** (41 / 365.25) * c ** H / c ** 41
    assert p["v_a"] == pytest.approx(v_a, rel=1e-12)
    assert v.y_w == pytest.approx(0.05, abs=1e-12)
    assert 100 * (p["v_a"] - p["cash_h"]) == pytest.approx(100 * (v_a - c ** H), rel=1e-10)
    assert p["cash_h"] == pytest.approx(c ** H, rel=1e-12)


def test_a_close_exactly_at_the_trigger_after_several_moves_counts(raw):
    # 1.828 to 2.285 is exactly +25%, but the four log moves between them sum a hair below ln(1.25)
    path = [1.828, 1.9, 2.0, 2.1, 2.285] + [2.0] * 415
    world(raw, path, dff=3.0)
    res = V.run(registry())
    assert res.wins[0].s == D0
    assert res.per[0]["taus"][res.levels.index(V.RALLY_PCT)] == 4


# --------------------------------------------------------------------------- 3. no look-ahead

def test_prices_after_the_sale_leave_the_sale_alone(raw):
    world(raw, rally_path(), dff=3.0)
    before = V.run(registry())
    later = rally_path()
    later = later[:42] + [x * (0.5 if k % 2 else 3.0) for k, x in enumerate(later[42:])]
    world(raw, later, dff=3.0)
    after = V.run(registry())
    main = before.levels.index(V.RALLY_PCT)
    a, b = before.per[0], after.per[0]
    assert a["taus"][main] == b["taus"][main] == 40
    assert a["v_a"] == b["v_a"]
    assert a["hold"] != b["hold"]                     # the later path did change


# --------------------------------------------------------------------------- 4. timing

def test_window_edges_and_volatility_windows(raw):
    path = wiggle(800)
    world(raw, path)
    res = V.run(registry())
    ret = log_returns(path)
    for v in res.wins:
        assert v.r0["date"] == v.s + timedelta(days=1) and v.r1["date"] == v.e + timedelta(days=1)
    for v in (res.wins[0], res.wins[-1]):
        own = [ret[v.s + timedelta(days=i)] for i in range(1, H + 1)]          # dated s+1..e
        assert v.sig_w == pytest.approx(statistics.stdev(own) * math.sqrt(365), rel=1e-12)


# --------------------------------------------------------------------------- 5. rebalancing on an oscillation

def test_rebalancing_wins_on_an_oscillation_and_stays_under_the_ceiling(raw):
    n = 30 * 26 + 1                                   # ends where it started
    path = [1.0 if (k // 30) % 2 == 0 else 1.05 for k in range(n)]
    assert path[-1] == path[0]
    world(raw, path, dff=0.0, lst_growth=1.0)
    reg = registry()
    V.compute(reg)
    assert reg.value("vol_rebal_mean") > 0
    assert reg.value("vol_rebal_log") <= reg.value("vol_rebal_theory") + 0.001


# --------------------------------------------------------------------------- 6. the shuffles

def test_block_orders_are_deterministic_per_seed():
    a = V.block_orders(12, 5, 7)
    assert a == V.block_orders(12, 5, 7)
    assert a != V.block_orders(12, 5, 8)
    assert all(sorted(o) == list(range(12)) for o in a)


def test_block_order_follows_the_fisher_yates_rule_on_the_prng():
    rng = Prng(int(param("sim_seed")))
    order = list(range(10))
    for i in range(9, 0, -1):
        j = (rng.next() * (i + 1)) >> 32
        order[i], order[j] = order[j], order[i]
    first = V.block_orders(10, 3, int(param("sim_seed")))[0]
    assert first == order
    assert first == FIRST_ORDER


FIRST_ORDER = [2, 8, 0, 3, 7, 5, 4, 9, 1, 6]   # block_orders(10, 1, sim_seed)[0], pinned


def test_a_shuffle_reorders_whole_blocks():
    rng = random.Random(3)
    ret = [rng.gauss(0, 0.05) for _ in range(95)]
    block = V.NULL_BLOCK_DAYS
    order = V.block_orders(math.ceil(len(ret) / block), 1, 11)[0]
    out = V.shuffled(ret, block, order)
    assert sorted(out) == sorted(ret)
    assert sum(out) == pytest.approx(sum(ret), abs=1e-12)
    full = [ret[i:i + block] for i in range(0, len(ret), block) if len(ret[i:i + block]) == block]
    for b in full:
        assert any(out[i:i + block] == b for i in range(len(out) - block + 1))


def test_each_shuffle_keeps_the_windows_cash_and_staking(raw):
    path = wiggle(800)
    world(raw, path)
    res = V.run(registry())
    close = {d: v for d, v in res.prices.points if v}
    cx, ret, gap = V._context(res, close, load.fred("DFF").as_dict())
    assert not gap
    real_means, real = V._strategies(cx, V.line(ret), keep=True)
    # the identity order is the real history
    nb = math.ceil(len(ret) / V.NULL_BLOCK_DAYS)
    same = V._strategies(cx, V.line(V.shuffled(ret, V.NULL_BLOCK_DAYS, list(range(nb)))))
    assert same == pytest.approx(real_means, rel=1e-12)
    order = V.block_orders(nb, 1, 5)[0]
    shuf_means, shuf = V._strategies(cx, V.line(V.shuffled(ret, V.NULL_BLOCK_DAYS, order)), keep=True)
    # the build's parallel job scores exactly this shuffled line
    assert V.null_chunk(cx, ret, V.NULL_BLOCK_DAYS, [order]) == [shuf_means]
    assert any(a["p_h"] != b["p_h"] for a, b in zip(real, shuf))
    for a, b in zip(real, shuf):
        assert a["cash_h"] == b["cash_h"]                                      # C(H)
        assert a["hold"] / a["p_h"] == pytest.approx(b["hold"] / b["p_h"], rel=1e-12)   # u(H)


# --------------------------------------------------------------------------- 7. rank and band

def test_rank_counts_only_shuffles_strictly_below():
    nulls = [1.0, 2.0, 3.0, 3.0, 4.0]
    s = V.summarize(3.0, nulls)
    assert s["rank"] == 40                               # the two ties are not below
    xs = sorted(nulls)
    assert s["lo"] == q7(xs, V.RANK_LO / 100) and s["hi"] == q7(xs, V.RANK_HI / 100)
    assert s["excess"] == pytest.approx(3.0 - statistics.fmean(nulls))
    assert s["null_mean"] == pytest.approx(2.6)
    assert V.summarize(None, nulls) is None and V.summarize(1.0, []) is None


def test_band_edges():
    assert V.band(V.RANK_HI) == "above" and V.band(100) == "above"
    assert V.band(V.RANK_LO) == "below" and V.band(0) == "below"
    assert V.band(50) == V.band(V.RANK_HI - 0.1) == V.band(V.RANK_LO + 0.1) == "inside"
    assert V.band(None) is None


# --------------------------------------------------------------------------- 8. missing data

def test_a_missing_last_close_drops_only_the_last_window(raw):
    path = wiggle(800)
    world(raw, path)
    base = registry()
    V.compute(base)
    world(raw, path, close_skip=(799,))
    reg = registry()
    V.compute(reg)
    assert reg.value("vol_windows") == base.value("vol_windows") - 1
    assert reg.value("vol_last_end") == day(798).isoformat()
    assert reg.value("vol_rally_rank") is not None


def test_a_missing_first_dff_day_drops_only_the_first_window(raw):
    path = wiggle(800)
    world(raw, path)
    base = registry()
    V.compute(base)
    world(raw, path, dff_skip=(0,))
    reg = registry()
    V.compute(reg)
    assert reg.value("vol_windows") == base.value("vol_windows") - 1
    assert reg.value("vol_first_start") == day(1).isoformat()


def test_a_missing_close_inside_the_span_nulls_the_shuffles(raw):
    path = wiggle(800)
    world(raw, path, close_skip=(400,))
    reg = registry()
    V.compute(reg)
    res = V.result(reg)
    starts = {v.s for v in res.wins}
    assert day(34) in starts and day(401) in starts and day(35) not in starts and day(400) not in starts
    assert reg.value("vol_rally_mean") is not None
    for key in ("vol_rally_rank", "vol_rebal_rank", "vol_rally_null_mean", "vol_rally_excess", "vol_ladder_1_rank"):
        assert reg.value(key) is None and reg.get(key).null_reason == V.SPAN_GAP, key
    assert verdicts.evaluate_claim("H10", h10_rule(), reg)["verdict"] == verdicts.NOT_COMPUTED


# --------------------------------------------------------------------------- 9. the rule

def ranks(a: float, b: float, hl: float = 50, bn: float = 50, years: int = 5, hl_years: int = 3,
          bn_years: int = 4) -> Registry:
    reg = Registry()
    reg.put("vol_rally_rank", a, "%", "FAKE")
    reg.put("vol_rebal_rank", b, "%", "FAKE")
    reg.put("vol_perp_hl_rank", hl, "%", "FAKE")
    reg.put("vol_perp_bn_rank", bn, "%", "FAKE")
    reg.put("vol_years", years, "count", "FAKE")
    reg.put("vol_perp_hl_years", hl_years, "count", "FAKE")
    reg.put("vol_perp_bn_years", bn_years, "count", "FAKE")
    return reg


@pytest.mark.parametrize("a, b, hl, bn, want", [
    (97.5, 50, 50, 50, verdicts.SUPPORTED), (50, 97.5, 50, 50, verdicts.SUPPORTED),
    (2.5, 50, 50, 50, verdicts.REFUTED), (50, 2.5, 50, 50, verdicts.REFUTED),
    (97.5, 2.5, 50, 50, verdicts.PARTLY), (50, 50, 50, 50, verdicts.PARTLY),
    # the ladder counts only when both venues agree
    (50, 50, 97.5, 97.5, verdicts.SUPPORTED), (50, 50, 97.5, 50, verdicts.PARTLY), (50, 50, 50, 97.5, verdicts.PARTLY),
    (50, 50, 2.5, 2.5, verdicts.REFUTED), (50, 50, 2.5, 50, verdicts.PARTLY), (50, 50, 97.5, 2.5, verdicts.PARTLY),
    # one strategy above chance and the ladder below it on both venues
    (97.5, 50, 2.5, 2.5, verdicts.PARTLY), (2.5, 50, 97.5, 97.5, verdicts.PARTLY),
    # the latest figures: the ladder above on one venue only
    (50, 50, 97.2, 46.9, verdicts.PARTLY)])
def test_h10_rule_on_hand_ranks(a, b, hl, bn, want):
    rule = h10_rule()
    assert verdicts.evaluate_claim("H10", rule, ranks(a, b, hl, bn))["verdict"] == want
    read = set(re.findall(r"[a-z_]+", rule["supported"] + rule["refuted"])) - {"and", "or"}
    assert read == {"vol_rally_rank", "vol_rebal_rank", "vol_perp_hl_rank", "vol_perp_bn_rank"}   # ranks only


@pytest.mark.parametrize("years, hl_years, bn_years", [(2, 3, 4), (5, 2, 4), (5, 3, 2)])
def test_h10_needs_three_separate_years_on_every_record(years, hl_years, bn_years):
    reg = ranks(99, 99, 99, 99, years=years, hl_years=hl_years, bn_years=bn_years)
    assert verdicts.evaluate_claim("H10", h10_rule(), reg)["verdict"] == verdicts.NOT_COMPUTED


def test_h10_needs_three_separate_years(raw):
    world(raw, wiggle(800))
    reg = registry()
    V.compute(reg)
    assert reg.value("vol_years") < V.MIN_YEARS
    assert verdicts.evaluate_claim("H10", h10_rule(), reg)["verdict"] == verdicts.NOT_COMPUTED


# --------------------------------------------------------------------------- 10. the desk share

def test_the_desk_share_sets_w_and_its_absence_nulls_strategy_b(raw):
    world(raw, wiggle(800))
    with_share = registry(10)
    V.compute(with_share)
    assert V.result(with_share).w == pytest.approx(0.1)
    assert with_share.value("vol_rebal_share") == 10                       # c25's note prints the share it used
    reg = registry(None)
    V.compute(reg)
    for key in ("vol_rebal_share", "vol_rebal_mean", "vol_rebal_rank", "vol_rebal_log", "vol_rebal_theory"):
        assert reg.value(key) is None and reg.get(key).null_reason == V.NO_SHARE, key
    assert reg.value("vol_rally_rank") is not None
    assert verdicts.evaluate_claim("H10", h10_rule(), reg)["verdict"] == verdicts.NOT_COMPUTED


# --------------------------------------------------------------------------- 11. chart c25

def copy_with(reg: Registry, **over) -> Registry:
    """A copy of reg with some values replaced; keys reg lacks are added (a text value as text, a number as %)."""
    out = Registry()
    for key, n in reg.items():
        value = over.pop(key) if key in over else n.value
        out.put(key, value, n.unit, n.formula_id, source_files=n.source_files, inputs=n.inputs,
                null_reason=n.null_reason if value is None else None, decimals=n.decimals, signed=n.signed)
    for key, value in over.items():
        out.put(key, value, "text" if isinstance(value, str) else "%", "FAKE")
    return out


def test_c25_validates(raw):
    world(raw, wiggle(800))
    reg = registry()
    V.compute(reg)
    chart = V.strategies_chart(reg)
    dump(chart.to_json())
    assert chart.claims == ["H10"] and chart.x["legend"] == "series"
    shuffled, real = (next(s for s in chart.series if s.id == sid) for sid in ("shuffled", "real"))
    assert len(shuffled.points) == len(real.points) == 4
    assert all(len(p) == 4 for p in shuffled.points) and all(len(p) == 2 for p in real.points)
    assert [p[0] for p in real.points] == [label for label, _ in V.ROWS]
    assert len(chart.table["rows"]) == 6
    assert chart.table["columns"][0] == "strategy"
    assert [r[0] for r in chart.table["rows"][-2:]] == ["Perp ladder, Hyperliquid", "Perp ladder, Binance"]
    assert len(chart.csv_table["rows"]) == reg.value("vol_windows")
    assert chart.formula_ids[-4:] == ["F-perp-windows", "F-perp-ladder", "F-lst", "F-ffcomp"]
    assert chart.status == "partial"                  # the perp ladder's ranks come from perps, not run here


def test_c25_title_endings(raw):
    world(raw, wiggle(800))
    reg = registry()
    V.compute(reg)
    res = V.result(reg)
    titles = {}
    for name, bands in (("inside", ("inside", "inside", "inside")), ("split", ("above", "inside", "below")),
                        ("above", ("inside", "inside", "above")), ("below", ("below", "inside", "inside")),
                        ("no ladder", ("inside", "inside", None))):
        over = dict(vol_rally_band=bands[0], vol_rebal_band=bands[1], vol_perp_hl_rank=97.2, vol_perp_bn_rank=46.9)
        if bands[2] is not None:
            over["vol_perp_band"] = bands[2]
        r2 = copy_with(reg, **over)
        V._RESULTS[r2] = res
        chart = V.strategies_chart(r2)
        dump(chart.to_json())
        titles[name] = chart.title
    assert titles["inside"].endswith("the perp ladder 97.2% on Hyperliquid and 46.9% on Binance, inside what chance "
                                     "gives, so no capture shows.")
    assert titles["split"].endswith(": one strategy beat chance and another lost to it.")
    assert titles["above"].endswith(": NEAR's real order of moves paid a patient holder more than chance.")
    assert titles["below"].endswith(": NEAR's real order of moves cost a patient holder against chance.")
    assert titles["no ladder"] == "Three ways to sell NEAR's volatility, against holding and against NEAR's own moves shuffled."
    assert not any(NT.says(t, NT.BANNED_TITLES) for t in titles.values())


def test_c25_is_unavailable_on_empty_raw(raw):
    reg = registry()
    V.compute(reg)
    assert reg.value("vol_rally_rank") is None and reg.get("vol_rally_rank").null_reason
    chart = V.strategies_chart(reg)
    dump(chart.to_json())
    assert chart.status == "unavailable" and chart.empty_state


# --------------------------------------------------------------------------- 12. the full shuffle count

def test_full_shuffle_count_on_a_short_span(raw, monkeypatch):
    monkeypatch.setenv("SIM_WORKERS", "1")
    path = wiggle(H + 61)
    world(raw, path)
    few = V.run(registry())
    monkeypatch.setattr(V, "NULL_DRAWS", int(param("vol_null_draws")))
    reg = registry()
    V.compute(reg)
    res = V.result(reg)
    assert res.draws == int(param("vol_null_draws")) and len(res.wins) == 61
    assert all(len(xs) == res.draws for xs in res.nulls.values())
    # one stream: the first shuffles of the full run are the shuffles of the short run
    assert res.nulls["a"][:FEW] == few.nulls["a"]
    rank = reg.value("vol_rally_rank")
    assert rank is not None and (rank * res.draws / 100) == pytest.approx(round(rank * res.draws / 100))
    assert reg.value("vol_null_draws") == res.draws
