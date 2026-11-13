"""Group B, market: NEAR held in dollars (c07 rolling 12-month USD return, c08 carry vs noise, c09 rolling vol).

Formulas (F-rv, F-mdd-365, F-ffcomp):
  F-rv        sample stdev of daily log returns x sqrt(365) for crypto (returns only between consecutive calendar
              days; a window must be complete), x sqrt(252) for DEXUSEU over consecutive observations (business
              days; a holiday return spans the closed days).
  F-mdd-365   min(P / running max - 1) over the daily closes of the trailing 365 days, the running max starting inside
              the window.
  F-worst     min daily simple return P_t / P_{t-1} - 1 over consecutive calendar days since listing.
  F-usd-12m   V(d) = stNEAR rate at RPC sample date d x NEAR close of day d-1 (the candle that closes at 00:00 UTC
              of d; no interpolation). ret(d) = V(d) / V(d') - 1 with d' the sample nearest d - 365 days within
              7 days; cash(d) = compounded DFF over the same days [d', d) (formulas.ffcomp_total). Window ends d
              are on one weekly cadence: every Monday of rpc/dense_samples.csv, plus the latest sample; starts d'
              may be any merged sample (history or dense).
Prices: Binance NEARUSDT first; Coinbase NEAR-USD only when Binance is missing (chart status fallback + banner).
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import date, timedelta

from pipeline.chartspec import Chart, Query, Series
from pipeline.models import formulas as F
from pipeline.models import load
from pipeline.models.macro import (coverage, derived_query, empty_state, fed_turns, files_as_of, fred_query, iso,
                                   notes_for,
                                   ready, rnd, rpc_stnear_query, upstream, weekly)
from pipeline.models.params import param
from pipeline.numbers import Registry

WITHIN_PP = param("within_pp")   # H9: |ret - cash| <= WITHIN_PP counts toward pct_within_2pp_1y (rules.toml [params])
BINANCE_NEAR = "NEARUSDT_binance_daily"
COINBASE_NEAR = "near_usd_coinbase_daily"
KLINES = "https://data-api.binance.vision/api/v3/klines"
SQRT365 = math.sqrt(365)


@dataclass
class Prices:
    points: list[tuple[date, float | None]]
    files: list[str]
    venue: str          # "binance" | "coinbase" | ""
    source: str         # human source line
    banner: str | None  # fallback banner (digit-free)

    @property
    def ok(self) -> bool:
        return any(v is not None for _, v in self.points)

    @property
    def last(self) -> date | None:
        nn = [d for d, v in self.points if v is not None]
        return nn[-1] if nn else None


def near_prices() -> Prices:
    """Binance NEARUSDT closes; Coinbase NEAR-USD only if Binance is missing."""
    b = load.price_close(BINANCE_NEAR)
    if b.non_null():
        return Prices(b.points, b.files, "binance", f"Binance spot NEARUSDT daily klines ({KLINES})", None)
    c = load.price_close(COINBASE_NEAR)
    if c.non_null():
        meta = load.meta("market", COINBASE_NEAR)
        return Prices(c.points, c.files, "coinbase",
                      f"Coinbase Exchange NEAR-USD daily candles ({meta.get('source_url', 'api.exchange.coinbase.com')})",
                      "Built from Coinbase NEAR-USD daily closes; Binance NEARUSDT is missing, so windows "
                      "before the Coinbase series begins are omitted.")
    return Prices([], [], "", "", None)


def binance_prices(symbol: str) -> Prices:
    s = load.price_close(f"{symbol}_binance_daily")
    return Prices(s.points, s.files, "binance" if s.non_null() else "",
                  f"Binance spot {symbol} daily klines ({KLINES})", None)


def klines_query(symbol: str) -> Query:
    url = f"{KLINES}?symbol={symbol}&interval=1d&limit=1000&startTime=<ms, paged>"
    return Query("http", KLINES, f"GET {url}", f"data/raw/market/{symbol}_binance_daily.csv")


def near_query(prices: Prices) -> Query:
    if prices.venue == "coinbase":
        meta = load.meta("market", COINBASE_NEAR)
        url = meta.get("source_url", "https://api.exchange.coinbase.com/products/NEAR-USD/candles")
        return Query("http", url, f"GET {url}?granularity=86400&start=<iso>&end=<iso>",
                     f"data/raw/market/{COINBASE_NEAR}.csv")
    return klines_query("NEARUSDT")


def simple_returns(points) -> list[tuple[date, float]]:
    """Daily simple returns between consecutive non-null closes on consecutive calendar days."""
    return [(d1, p1 / p0 - 1) for (d0, p0), (d1, p1) in zip(points, points[1:])
            if p0 and p1 and (d1 - d0).days == 1]


def obs_log_returns(points) -> list[tuple[date, float]]:
    """Log returns between consecutive non-null observations (business-day FX series; holidays are skipped)."""
    nn = [(d, v) for d, v in points if v]
    return [(d1, math.log(p1 / p0)) for (_, p0), (d1, p1) in zip(nn, nn[1:])]


def rolling_rv(returns, window_days: int, periods: int, min_obs: int) -> list[tuple[date, float | None]]:
    """Trailing-window RV at each return date: returns dated in (d - window_days, d]."""
    out, lo = [], 0
    for hi, (d, _) in enumerate(returns):
        while returns[lo][0] <= d - timedelta(days=window_days):
            lo += 1
        vals = [r for _, r in returns[lo:hi + 1]]
        out.append((d, statistics.stdev(vals) * math.sqrt(periods) if len(vals) >= max(min_obs, 2) else None))
    return out


def window_rv(returns, end_excl: date, days: int, periods: int = 365, strict: bool = True):
    """RV over returns dated in [end_excl - days, end_excl); strict = every calendar day present."""
    start = end_excl - timedelta(days=days)
    n = sum(1 for d, _ in returns if start <= d < end_excl)
    return F.realized_vol(returns, start, end_excl, periods, min_obs=days if strict else 20), n


def stnear_usd(prices: Prices) -> tuple[list[dict], list[str]]:
    """V(d) = stNEAR rate at RPC sample date d x NEAR close of day d-1. One row per full-day (00:00 UTC) sample of
    the merged RPC set (history_samples and dense_samples); the history file's latest-block row is not one."""
    rows, files = load.rpc_merged()
    close = dict(prices.points)
    out, seen = [], set()
    for r in rows:
        rate = r.get("stnear_price")
        if rate is None or not r["full_day"] or r["date"] in seen:
            continue
        seen.add(r["date"])
        cd = r["date"] - timedelta(days=1)
        c = close.get(cd)
        out.append({"date": r["date"], "height": r.get("height"), "rate": rate, "close_date": cd, "close": c,
                    "value": rate * c if c else None, "dense": "dense" in (r.get("sources") or "")})
    return out, files


def window_ends(samples: list[dict]) -> list[dict]:
    """The weekly cadence of c07 window ends: dense-sample Mondays, plus the latest sample with a dollar value."""
    valued = [s for s in samples if s["value"] is not None]
    ends = [s for s in valued if s.get("dense") and s["date"].weekday() == 0]
    if valued and (not ends or ends[-1] is not valued[-1]):
        ends.append(valued[-1])
    return ends


def usd_windows(samples: list[dict], dff: dict) -> list[dict]:
    pts = [(s["date"], s["value"]) for s in samples]
    out = []
    for s in window_ends(samples):
        if s["value"] is None:
            continue
        prev = F.nearest(pts, s["date"] - timedelta(days=365), 7)
        if not prev or prev[0] >= s["date"]:
            continue
        ret = s["value"] / prev[1] - 1
        cash = F.ffcomp_total(dff, prev[0], s["date"])
        out.append({"date": s["date"], "start": prev[0], "days": (s["date"] - prev[0]).days, "rate": s["rate"],
                    "close": s["close"], "value": s["value"], "start_value": prev[1],
                    "ret": F.pct(ret), "cash": F.pct(cash)})
    return out


@dataclass
class MarketData:
    near: Prices
    btc: Prices
    eth: Prices
    eur_points: list
    eur_files: list[str]
    dff: dict
    dff_files: list[str]
    dff_last: date | None
    latest_common: date | None
    near_lr: list
    btc_lr: list
    eth_lr: list
    eur_lr: list
    near_sr: list


def analyze() -> MarketData:
    near, btc, eth = near_prices(), binance_prices("BTCUSDT"), binance_prices("ETHUSDT")
    eur = load.fred("DEXUSEU")
    dff = load.fred("DFF")
    dff_nn = dff.non_null()
    lasts = [p.last for p in (near, btc, eth) if p.last] + ([dff_nn[-1][0]] if dff_nn else [])
    return MarketData(
        near=near, btc=btc, eth=eth, eur_points=eur.points, eur_files=eur.files,
        dff=dff.as_dict(), dff_files=dff.files, dff_last=dff_nn[-1][0] if dff_nn else None,
        latest_common=min(lasts) if (near.ok and lasts) else None,
        near_lr=F.log_returns(near.points), btc_lr=F.log_returns(btc.points), eth_lr=F.log_returns(eth.points),
        eur_lr=obs_log_returns(eur.points), near_sr=simple_returns(near.points))


def _null_all(reg: Registry, keys: dict[str, tuple[str, str]], reason: str, files=()) -> None:
    for key, (unit, fid) in keys.items():
        if key not in reg:
            reg.put(key, None, unit, fid, source_files=files, null_reason=reason)


def compute(reg: Registry) -> None:
    md = analyze()
    near, nf = md.near, md.near.files
    end_note = f"window ends {iso(md.latest_common)}" if md.latest_common else ""

    # latest_common_date: the last day every market input (NEAR, BTC, ETH closes and DFF) has data. Every
    # trailing window in this module ends on it.
    common = md.latest_common
    reg.put("latest_common_date", iso(common), "date", "F-latest",
            source_files=nf + md.btc.files + md.eth.files + md.dff_files,
            inputs=[f"NEAR {iso(near.last)}", f"BTC {iso(md.btc.last)}", f"ETH {iso(md.eth.last)}",
                    f"DFF {iso(md.dff_last)}"],
            null_reason=None if common else "no NEAR price series in data/raw/market")

    if not near.ok:
        reason = "no NEAR price series (Binance NEARUSDT or Coinbase NEAR-USD) in data/raw/market"
        _null_all(reg, {k: ("%", "F-rv") for k in ("rv_365_near", "rv30_median", "sigma_d")}, reason)
        _null_all(reg, {"worst_day": ("%", "F-worst"), "worst_day_date": ("date", "F-worst"),
                        "near_close_latest": ("USD", "F-latest"), "near_close_date": ("date", "F-latest"), "x_btc": ("x", "F-ratio"), "x_eur": ("x", "F-ratio"),
                        "ratio_gap_to_day": ("x", "F-ratio"), "share_days_gt_spread": ("%", "F-share-days"),
                        "rv_90_near": ("%", "F-rv"), "mdd_365": ("%", "F-mdd-365"),
                        "n_windows_usd_1y": ("count", "F-usd-12m")}, reason)
        _null_all(reg, {k: ("%", "F-usd-12m") for k in ("pct_within_2pp_1y", "pct_beat_cash_1y")}, reason)
    end = md.latest_common + timedelta(days=1) if md.latest_common else None

    # Realized volatility, trailing 365 days ending at latest_common_date.
    for key, prices, lr in (("rv_365_near", near, md.near_lr), ("rv_365_btc", md.btc, md.btc_lr),
                            ("rv_365_eth", md.eth, md.eth_lr)):
        if key in reg:
            continue
        if not prices.ok or end is None:
            reg.put(key, None, "%", "F-rv", source_files=prices.files,
                    null_reason="price series missing in data/raw/market")
            continue
        rv, n = window_rv(lr, end, 365)
        reg.put(key, F.pct(rv), "%", "F-rv", source_files=prices.files,
                inputs=[end_note, f"{n} daily log returns of 365 required", "x sqrt(365)"],
                null_reason=None if rv is not None else f"only {n} of 365 daily returns in the trailing window")
    if end is not None and md.eur_lr:
        rv_eur, n = window_rv(md.eur_lr, end, 365, 252, strict=False)
        reg.put("rv_365_eur", F.pct(rv_eur), "%", "F-rv", source_files=md.eur_files,
                inputs=[end_note, f"{n} DEXUSEU business-day log returns", "x sqrt(252)"],
                null_reason=None if rv_eur is not None else f"only {n} DEXUSEU returns in the trailing window")
    else:
        reg.put("rv_365_eur", None, "%", "F-rv", source_files=md.eur_files,
                null_reason="FRED DEXUSEU or the NEAR price series is missing")

    if not near.ok:
        return

    # H7: the latest ninety days, ending at latest_common_date, every calendar day required
    if end is not None:
        rv90, n90 = window_rv(md.near_lr, end, 90)
        reg.put("rv_90_near", F.pct(rv90), "%", "F-rv", source_files=nf,
                inputs=[end_note, f"{n90} daily log returns of 90 required", "x sqrt(365)"],
                null_reason=None if rv90 is not None else f"only {n90} of 90 daily returns in the trailing window")
    else:
        reg.put("rv_90_near", None, "%", "F-rv", source_files=nf, null_reason="no latest common market date")

    rv_near = reg.value("rv_365_near")
    for key, other in (("x_btc", "rv_365_btc"), ("x_eur", "rv_365_eur")):
        o = reg.value(other)
        reg.put(key, rv_near / o if (rv_near is not None and o) else None, "x", "F-ratio",
                source_files=nf + reg.get(other).source_files, inputs=["rv_365_near", other],
                null_reason=None if (rv_near is not None and o) else f"rv_365_near or {other} is not computed")

    first = next((d for d, v in near.points if v is not None), None)

    rv30 = [v for _, v in rolling_rv(md.near_lr, 30, 365, 30) if v is not None]
    reg.put("rv30_median", F.pct(statistics.median(rv30)) if rv30 else None, "%", "F-rv", source_files=nf,
            inputs=[f"median of {len(rv30)} complete trailing 30-day windows since {iso(first)}"],
            null_reason=None if rv30 else "no complete 30-day window")

    # F-mdd-365: min(P / running max - 1) over the daily closes in [end - 365 d, end), the running max starting
    # inside the window
    yr = [(d, v) for d, v in near.points if end is not None and end - timedelta(days=365) <= d < end]
    dd1 = F.max_drawdown(yr) if yr else None
    reg.put("mdd_365", F.pct(dd1[0]) if dd1 else None, "%", "F-mdd-365", source_files=nf,
            inputs=[end_note, f"{sum(1 for _, v in yr if v is not None)} daily closes in the trailing 365 days "
                              f"({near.venue})"] + ([f"high {iso(dd1[1])}, low {iso(dd1[2])}"] if dd1 else []),
            null_reason=None if dd1 else "no NEAR closes in the trailing 365 days")

    worst = min(md.near_sr, key=lambda p: p[1]) if md.near_sr else None
    reg.put("worst_day", F.pct(worst[1]) if worst else None, "%", "F-worst", source_files=nf,
            inputs=[f"{len(md.near_sr)} daily simple returns since {iso(first)}"],
            null_reason=None if worst else "fewer than two consecutive NEAR closes")
    reg.put("worst_day_date", iso(worst[0]) if worst else None, "date", "F-worst", source_files=nf,
            null_reason=None if worst else "fewer than two consecutive NEAR closes")

    last_close = dict(near.points).get(near.last)
    reg.put("near_close_latest", last_close, "USD", "F-latest", source_files=nf, decimals=2,
            inputs=[f"{near.venue} close {iso(near.last)}"])
    reg.put("near_close_date", iso(near.last) if last_close is not None else None, "date", "F-latest",
            source_files=nf, inputs=[f"{near.venue} daily closes"],
            null_reason=None if last_close is not None else "no NEAR close on the series' last date")

    sigma = rv_near / SQRT365 if rv_near is not None else None
    reg.put("sigma_d", sigma, "%", "F-rv", source_files=nf, inputs=["rv_365_near / sqrt(365)"],
            null_reason=None if sigma is not None else "rv_365_near is not computed")

    spread, why = upstream(reg, "spread_adj_365")
    if spread is not None and sigma:
        # the size of the yearly gap in typical days (unsigned; the prose says above or below fed funds)
        reg.put("ratio_gap_to_day", abs(spread) / sigma, "x", "F-ratio", source_files=nf,
                inputs=["|spread_adj_365|", "sigma_d"])
    else:
        reg.put("ratio_gap_to_day", None, "x", "F-ratio", source_files=nf,
                null_reason=why or "sigma_d is not computed")
    if spread is not None and end is not None:
        start = end - timedelta(days=365)
        window = [r for d, r in md.near_sr if start <= d < end]
        big = sum(1 for r in window if abs(r) * 100 > abs(spread))
        reg.put("share_days_gt_spread", 100.0 * big / len(window) if window else None, "%", "F-share-days",
                source_files=nf, inputs=[f"{big} of {len(window)} days with |daily return| > |spread_adj_365|",
                                         end_note],
                null_reason=None if window else "no daily returns in the trailing 365 days")
    else:
        reg.put("share_days_gt_spread", None, "%", "F-share-days", source_files=nf,
                null_reason=why or "no trailing window")

    # c07 rolling 12-month USD return of stNEAR against compounded fed funds.
    samples, rpc_files = stnear_usd(near)
    wins = usd_windows(samples, md.dff)
    both = [w for w in wins if w["ret"] is not None and w["cash"] is not None]
    files = nf + rpc_files + md.dff_files
    if both:
        _windows_1y(reg, both, end, files)
    else:
        n_lst = len(samples)
        reason = (f"no stNEAR sample pair about a year apart with a NEAR close and DFF for every day "
                  f"({n_lst} stNEAR samples in rpc/history_samples.csv and rpc/dense_samples.csv; each weekly "
                  f"window end (dense-sample Mondays and the latest sample) needs a sample within 7 days of 365 "
                  f"days earlier)")
        for key in ("pct_within_2pp_1y", "pct_beat_cash_1y"):
            reg.put(key, None, "%", "F-usd-12m", source_files=files, null_reason=reason)
        reg.put("n_windows_usd_1y", None, "count", "F-usd-12m", source_files=files, null_reason=reason)


def _windows_1y(reg: Registry, both: list[dict], end: date | None, files: list[str]) -> None:
    """H9 and H6 row 3: only the weekly one-year windows whose end date falls in
    the trailing 365 days, (end - 365 d, end] with end = latest_common_date + 1 day (the first sample date whose value
    uses the latest common close)."""
    keys = {"n_windows_usd_1y": "count", "pct_within_2pp_1y": "%", "pct_beat_cash_1y": "%"}
    lo = end - timedelta(days=365) if end is not None else None
    w1 = [w for w in both if lo is not None and lo < w["date"] <= end]
    if not w1:
        reason = ("no latest common market date" if end is None else
                  f"no weekly one-year window ends after {iso(lo)} and on or before {iso(end)}")
        for key, unit in keys.items():
            reg.put(key, None, unit, "F-usd-12m", source_files=files, null_reason=reason)
        return
    rng = f"weekly windows ending {iso(w1[0]['date'])}..{iso(w1[-1]['date'])}, ends after {iso(lo)}"
    within = sum(1 for w in w1 if abs(w["ret"] - w["cash"]) <= WITHIN_PP)
    beat = sum(1 for w in w1 if w["ret"] > w["cash"])
    reg.put("n_windows_usd_1y", len(w1), "count", "F-usd-12m", source_files=files, inputs=[rng])
    reg.put("pct_within_2pp_1y", 100.0 * within / len(w1), "%", "F-usd-12m", source_files=files,
            inputs=[rng, f"{within} of {len(w1)} windows with |ret - cash| <= within_pp"])
    reg.put("pct_beat_cash_1y", 100.0 * beat / len(w1), "%", "F-usd-12m", source_files=files,
            inputs=[rng, f"{beat} of {len(w1)} windows with ret > cash"])


# ---------------------------------------------------------------------------
# Charts
# ---------------------------------------------------------------------------

def charts(reg: Registry) -> list[Chart]:
    md = analyze()
    return [rolling_usd(reg, md), carry_vs_noise(reg, md), rolling_vol(reg, md)]


def _price_status(chart: Chart, prices: Prices) -> None:
    if prices.venue == "coinbase":
        chart.status = "fallback"
        chart.status_banner = prices.banner


def rolling_usd(reg: Registry, md: MarketData | None = None) -> Chart:
    """c07 usd-rolling-12m: trailing-year USD return of stNEAR vs trailing-year compounded fed funds."""
    md = md or analyze()
    samples, rpc_files = stnear_usd(md.near)
    wins = usd_windows(samples, md.dff)
    files = md.near.files + rpc_files + md.dff_files
    dates = [w["date"] for w in wins]
    ann = []
    if dates:
        turns, tf = fed_turns(dates[0], dates[-1])
        files += tf
        ann += turns
        cut, _ = upstream(reg, "cut_date")
        if cut and dates[0] <= date.fromisoformat(cut) <= dates[-1]:
            ann.append({"x": cut, "label": "NEAR issuance ceiling cut", "kind": "ceiling_change"})
    chart = Chart(
        id="c07", section="O5", priority="lead", claims=["H9"], verdicts=["H9"], title="", sentence="",
        form="line",
        sources=[md.near.source or "Binance spot NEARUSDT daily klines",
                 "NEAR archival RPC (meta-pool.near get_st_near_price)",
                 "FRED (fredgraph.csv: DFF)"],
        queries=[near_query(md.near), rpc_stnear_query(), fred_query("DFF"),
                 derived_query("V(d) = stNEAR rate(d) x NEAR close(d-1); ret = V(d)/V(d') - 1, d' nearest "
                               "d-365 within 7 days; cash = prod(1 + DFF/36000) over [d', d) - 1")],
        table={"columns": ["window_end", "window_start", "days", "stnear_rate_end", "near_close_day_before_end",
                           "usd_value_end", "usd_value_start", "stnear_usd_return_pct", "fed_funds_return_pct",
                           "difference_pp"],
               "rows": [[iso(w["date"]), iso(w["start"]), w["days"], rnd(w["rate"], 8), w["close"],
                         rnd(w["value"], 6), rnd(w["start_value"], 6), rnd(w["ret"]), rnd(w["cash"]),
                         rnd(w["ret"] - w["cash"]) if w["cash"] is not None else None] for w in wins]},
        code_ref="pipeline/models/market.py::rolling_usd", formula_ids=["F-usd-12m", "F-ffcomp"],
        x={"type": "time", "unit": "day"}, y={"unit": "%", "floor": -100},
        series=[Series("stnear_usd_12m", "Staked NEAR (stNEAR), dollar return over the previous year", "near",
                       [[iso(w["date"]), rnd(w["ret"])] for w in wins]),
                Series("fed_funds_12m", "Fed funds, compounded over the same days", "usd",
                       [[iso(w["date"]), rnd(w["cash"])] for w in wins])],
        reference_lines=[{"y": 0, "label": "zero"}], annotations=ann,
        # a gap is a weekly window end after the first window with no sample a year back (the first year of LST
        # data cannot end a window: it is the lead-in, not a gap)
        coverage=coverage(dates) | {"gaps": [iso(s["date"]) for s in window_ends(samples)
                                              if dates and s["date"] > dates[0] and s["date"] not in set(dates)]},
        as_of=files_as_of(files),
    )
    _price_status(chart, md.near)
    # the page lists the first window of each month and the latest; the chart draws every weekly window
    chart.thin_page_table("Rows: the first weekly window ending in each month, and the latest. The chart draws "
                          "every weekly window, and the CSV download lists them all.")
    if not any(w["ret"] is not None and w["cash"] is not None for w in wins):
        chart.status = "unavailable"
        chart.title = "A dollar in staked NEAR against a dollar at fed funds, one year at a time"
        chart.sentence = ("Not computed: there is no pair of staked NEAR samples about a year apart with dollar "
                          "prices for both ends.")
        chart.series = []
        chart.empty_state = empty_state(
            "No stNEAR rate samples about a year apart with a NEAR close for both ends.",
            "RPC stNEAR rate samples (meta-pool.near) a year apart, NEAR daily closes and FRED DFF.",
            "uv run python -m pipeline.pull --only near_rpc,prices,fred && make charts")
        return chart
    # the title and sentence count only the windows that end in the trailing year (H9, H6 row 3); the line draws
    # every window since the first
    if ready(reg, "n_windows_usd_1y", "pct_within_2pp_1y", "pct_beat_cash_1y"):
        chart.title_template = ("Of the {n_windows_usd_1y} weekly one-year windows that ended in the last year, a "
                                "dollar in stNEAR beat a dollar at fed funds in {pct_beat_cash_1y}.")
        chart.sentence_template = ("Each point is what a dollar held in staked NEAR for the year before that date "
                                   "returned in dollars; of the windows that ended in the last year it landed within "
                                   "two percentage points of fed funds in {pct_within_2pp_1y}.")
    else:
        chart.title = "A dollar in staked NEAR against a dollar at fed funds, one year at a time"
        chart.sentence = ("Each point is what a dollar held in staked NEAR for the year before that date returned in "
                          "dollars, beside fed funds over the same days.")
    chart.caveat_template = ("Consecutive windows share most of their days, so the points are not independent "
                             "years.")
    chart.notes_templates = ["Windows end each Monday, plus the latest sample.",
                             "The axis stops at a total loss, the most a dollar can lose."]
    return chart.resolve(reg)


def carry_vs_noise(reg: Registry, md: MarketData | None = None) -> Chart:
    """c08 carry-vs-noise: the staking edge over fed funds against NEAR's ordinary price moves."""
    md = md or analyze()
    bars = [  # fixed order; magnitudes of moves, the gap keeps its sign
        ("gap", "Yield gap over fed funds, one year", "near", "spread_adj_365", 1),
        ("sigma_d", "Typical one-day move (one standard deviation)", "context", "sigma_d", 1),
        ("rv_365", "Typical one-year move (one standard deviation)", "context", "rv_365_near", 1),
        ("worst_day", "Worst single day since listing (fall)", "context", "worst_day", -1),
        ("mdd", "Largest fall within the last year", "context", "mdd_365", -1),
    ]
    series, rows = [], []
    for sid, label, entity, key, sign in bars:
        v, _ = upstream(reg, key)
        shown = None if v is None else v * sign
        series.append(Series(sid, label, entity, [[label, rnd(shown)]], role="emphasis" if sid == "gap" else "context",
                             unit="pp" if sid == "gap" else "%"))
        rows.append([label, key, rnd(v), rnd(shown)])
    for key in ("rv_365_btc", "rv_365_eth", "rv_365_eur", "worst_day_date"):
        v, _ = upstream(reg, key)
        rows.append([key, key, rnd(v) if isinstance(v, float) else v, None])
    files = md.near.files + md.btc.files + md.eth.files + md.eur_files
    chart = Chart(
        id="c08", section="O5", priority="lead", claims=["H7"], verdicts=["H7"], title="", sentence="", form="bar",
        sources=[md.near.source or "Binance spot NEARUSDT daily klines",
                 f"Binance spot BTCUSDT and ETHUSDT daily klines ({KLINES})", "FRED (fredgraph.csv: DEXUSEU)",
                 "Staking spread from the staking model (LST yield minus compounded fed funds)"],
        queries=[near_query(md.near), klines_query("BTCUSDT"), klines_query("ETHUSDT"), fred_query("DEXUSEU"),
                 derived_query("rv = stdev(ln P_t/P_t-1) x sqrt(365) over the trailing 365 days; sigma_d = rv / "
                               "sqrt(365); mdd_365 = min(P / running max - 1) over the trailing 365 days; worst "
                               "day = min(P_t / P_t-1 - 1)")],
        table={"columns": ["measure", "key", "value", "bar_value"], "rows": rows},
        code_ref="pipeline/models/market.py::carry_vs_noise", formula_ids=["F-rv", "F-mdd-365", "F-worst"],
        x={"type": "category"}, y={"unit": "%"}, series=series,
        coverage=coverage([d for d, v in md.near.points if v is not None]),
        as_of=files_as_of(files),
    )
    _price_status(chart, md.near)
    if not ready(reg, "sigma_d", "rv_365_near", "mdd_365", "worst_day"):
        chart.status = "unavailable"
        chart.title = "NEAR's price moves against the staking edge over fed funds"
        chart.sentence = "Not computed: there is no complete year of NEAR daily closes."
        chart.series = []
        chart.empty_state = empty_state("No complete trailing year of NEAR daily closes.",
                                        "Binance NEARUSDT (or Coinbase NEAR-USD) daily closes.",
                                        "uv run python -m pipeline.pull --only prices && make charts")
        return chart
    if ready(reg, "spread_adj_365", "spread_adj_365_dir", "ratio_gap_to_day", "share_days_gt_spread"):
        spread = reg.value("spread_adj_365")
        edge = "staking edge over fed funds" if spread >= 0 else "staking shortfall against fed funds"
        if abs(spread) > reg.value("sigma_d"):
            chart.title = f"The {edge} exceeds one typical day's price move."
        else:
            chart.title = f"The {edge} is smaller than one ordinary day's price move."
        chart.sentence_template = ("Staking ran {spread_adj_365:abs} {spread_adj_365_dir} fed funds over the year, "
                                   "about {ratio_gap_to_day} a single typical day's move ({sigma_d}); on "
                                   "{share_days_gt_spread} of days in the last year NEAR moved more than that whole "
                                   "annual gap.")
    else:
        chart.status = "partial" if chart.status == "ok" else chart.status
        chart.title = "NEAR's ordinary price moves, set against the staking edge over fed funds"
        chart.sentence_template = ("A typical day moves NEAR by {sigma_d} and a typical year by {rv_365_near}; the "
                                   "largest fall within the last year was {mdd_365}.")
        missing = "The staking edge over fed funds is not computed, so its bar is missing."
        chart.status_banner = f"{chart.status_banner} {missing}" if chart.status_banner else missing
    return chart.resolve(reg)


def rolling_vol(reg: Registry, md: MarketData | None = None) -> Chart:
    """c09 rolling-vol: trailing 90-day realized volatility, NEAR against bitcoin and the euro."""
    md = md or analyze()
    first = next((d for d, v in md.near.points if v is not None), None)
    near90 = rolling_rv(md.near_lr, 90, 365, 90)
    btc90 = [p for p in rolling_rv(md.btc_lr, 90, 365, 90) if first and p[0] >= first]
    eth90 = [p for p in rolling_rv(md.eth_lr, 90, 365, 90) if first and p[0] >= first]
    eur90 = [p for p in rolling_rv(md.eur_lr, 90, 252, 20) if first and p[0] >= first]
    by_date: dict[date, list] = {}
    for col, pts in enumerate((near90, btc90, eth90, eur90)):
        for d, v in pts:
            by_date.setdefault(d, [None, None, None, None])[col] = rnd(F.pct(v), 2)
    rows = [[iso(d)] + vals for d, vals in sorted(by_date.items())]
    # The table twin holds the weekly points that are drawn (each column is that series' own last reading of
    # the ISO week, so the business-day euro series is not nulled on weekends); csv/c09.csv keeps every day.
    week_rows: dict[tuple[int, int], list] = {}
    for col, pts in enumerate((near90, btc90, eth90, eur90)):
        for x, v in weekly([(d, F.pct(v)) for d, v in pts], 2):
            row = week_rows.setdefault(date.fromisoformat(x).isocalendar()[:2], [x, None, None, None, None])
            row[0] = max(row[0], x)
            row[col + 1] = v
    columns = ["date", "near_rv90_pct", "btc_rv90_pct", "eth_rv90_pct", "eur_usd_rv90_pct"]
    files = md.near.files + md.btc.files + md.eth.files + md.eur_files
    chart = Chart(
        id="c09", section="O5", priority="supporting", claims=["H7"], verdicts=[], title="", sentence="", form="line",
        sources=[md.near.source or "Binance spot NEARUSDT daily klines",
                 f"Binance spot BTCUSDT and ETHUSDT daily klines ({KLINES})", "FRED (fredgraph.csv: DEXUSEU)"],
        queries=[near_query(md.near), klines_query("BTCUSDT"), klines_query("ETHUSDT"), fred_query("DEXUSEU"),
                 derived_query("rv90(d) = stdev of daily log returns dated (d-90, d] x sqrt(365); DEXUSEU over "
                               "business-day observations x sqrt(252)")],
        table={"columns": columns, "rows": [week_rows[k] for k in sorted(week_rows)]},
        csv_table={"columns": columns, "rows": rows},
        code_ref="pipeline/models/market.py::rolling_vol", formula_ids=["F-rv"],
        x={"type": "time", "unit": "day"}, y={"unit": "%"},
        series=[Series("near_rv90", "NEAR", "near", weekly([(d, F.pct(v)) for d, v in near90], 2)),
                Series("btc_rv90", "Bitcoin", "context", weekly([(d, F.pct(v)) for d, v in btc90], 2),
                       role="context"),
                # dashed: the second gray line must differ from bitcoin's by more than its end label
                Series("eur_rv90", "Euro", "context",
                       weekly([(d, F.pct(v)) for d, v in eur90], 2), role="context", dashed=True)],
        coverage=coverage([d for d, v in near90 if v is not None]),
        as_of=files_as_of(files),
    )
    # csv_table already holds every day; the page keeps the first drawn week of each month and the latest
    chart.thin_page_table("Rows: each series' last reading of the first drawn week in each month, and the latest "
                          "week. The chart draws every week, and the CSV download has every day.")
    _price_status(chart, md.near)
    if not ready(reg, "rv_365_near", "rv_365_btc", "rv_365_eur", "rv30_median", "x_btc", "x_eur"):
        if not any(v is not None for _, v in near90):
            chart.status = "unavailable"
            chart.title = "NEAR's price volatility against bitcoin and the euro"
            chart.sentence = "Not computed: there is no ninety-day window of NEAR daily closes."
            chart.series = []
            chart.table = {"columns": chart.table["columns"], "rows": []}
            chart.csv_table = None
            chart.empty_state = empty_state("No complete ninety-day window of NEAR daily closes.",
                                            "Binance NEARUSDT, BTCUSDT and FRED DEXUSEU daily series.",
                                            "uv run python -m pipeline.pull --only prices,fred && make charts")
            return chart
        chart.status = "partial" if chart.status == "ok" else chart.status
        chart.title = "NEAR's price volatility against bitcoin and the euro"
        chart.sentence = ("Each line is trailing ninety-day realized volatility; the trailing-year comparison is not "
                          "computed because one of the three series is missing.")
        return chart
    chart.title_template = "NEAR's price is {x_btc} as volatile as bitcoin's and {x_eur} the euro's."
    chart.sentence_template = ("Each line is the price volatility of NEAR, bitcoin or the euro against the dollar "
                               "over the trailing ninety days.")
    chart.caveat_template = ("The title compares trailing-year volatility ({rv_365_near} for NEAR, {rv_365_btc} for "
                             "bitcoin, {rv_365_eur} for the euro), so the lines' end values differ from it.")
    chart.notes_templates = notes_for(reg, [
        "The median one-month reading for NEAR since listing is {rv30_median}.",
        "Volatility is the annualized standard deviation of daily log returns.",
    ])
    return chart.resolve(reg)
