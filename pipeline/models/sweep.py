"""Group B, sweep: staked NEAR against the broker sweep-account checklist (chart c14; claims H6, H6_near).

Rows, each scored once as a USD sweep and once as a NEAR-denominated treasury:
  1 Principal stable in the unit of account.
      USD  F-nav-par: V(t) = stNEAR rate x NEAR close of the day before (market.stnear_usd, merged full-day RPC
           samples, daily over the last year). t0 = the sample on the date 365 days before the latest, and every
           later sample through the latest is a day tested (n_nav_days, one per day with daily samples); a day is
           "at par" when |V(t)/V(t0) - 1| <= mmf_penny_band % (thesis/constants.toml, Rule 2a-7 penny rounding).
           Pass only if every sampled day is at par.
      NEAR F-down-days: samples where the stNEAR rate fell against the previous sample. Pass only if none did.
  2 Same-day liquidity (both columns). Pass if native unstake (unlock_epochs.hi x epoch_hours_median_30d) is
      within a day; partial if it is longer but Meta Pool's instant pool holds liquidity; fail otherwise.
  3 Yield beats the benchmark in the unit of account.
      USD  pct_beat_cash_1y (market): pass when 100 % of the rolling one-year windows that end within the trailing
           year beat fed funds, partial when at least
           half do, fail otherwise.  NEAR staker_share_365 (holders): pass when a liquid staker's share of
           supply grew over the trailing year, fail otherwise.
  4 Legal vehicle. 15c3-3(a)(17) (constants sweep_definition) names only a 2a-7 money fund or an FDIC deposit:
      USD fails by definition; NEAR is not applicable.
H6 (USD): h6_rows_passed of h6_rows_total; h6_critical_failed counts rows 1 and 4 that fail.
H6_near: h6n_rows13_pass (rows 1 and 3 passing, 0..2) and h6n_row2_fail (0/1).
"""

from __future__ import annotations

import statistics
import string
from datetime import datetime, timedelta

from pipeline.chartspec import Chart, Series
from pipeline.models import formulas as F
from pipeline.models import load
from pipeline.models.params import param
from pipeline.models.macro import (RPC_TIP, derived_query, empty_state, files_as_of, iso, put_constant, rpc_query,
                                   rpc_stnear_query, upstream)
from pipeline.models.market import near_prices, near_query, stnear_usd
from pipeline.models.supply import EPOCH_WINDOW_DAYS
from pipeline.numbers import Registry, fill

SAME_DAY_HOURS = 24          # row 2: "same-day" = native exit within one day
MAJORITY = param("sweep_majority")   # row 3: partial at or above this share (thesis/rules.toml [params])
ROWS = ("Principal stable in the unit of account", "Same-day liquidity",
        "Yield beats the benchmark in the unit of account", "Legal vehicle for a broker sweep")


THEN_YEAR = 2021             # the unstake_hours_2021_* keys: the first full calendar year of mainnet staking


def epoch_hours_then(year: int = THEN_YEAR) -> tuple[float | None, int, list[str], str | None, int]:
    """Median epoch duration in calendar `year`: the median epoch_seconds of the complete epochs sampled that year
    (rpc/epoch_history.csv, one per sample date) / 3600. Returns (hours, year, files, null reason, n epochs)."""
    rows, files = load.rpc_epoch_history()
    secs = [r["epoch_seconds"] for r in rows if r.get("sample_date") and r["sample_date"].year == year
            and r.get("epoch_seconds")]
    if not secs:
        return None, year, files, f"rpc/epoch_history.csv has no sampled epoch in {year}", 0
    return statistics.median(secs) / 3600, year, files, None, len(secs)


def put_constant_value(name: str, fld: str = "value"):
    from pipeline.models.macro import constants
    return (constants().get(name) or {}).get(fld)


def _outcome_ratio(num, den) -> str | None:
    if num is None or not den:
        return None
    share = num / den
    return "pass" if share >= 1 else "partial" if share >= MAJORITY else "fail"


def analyze(reg: Registry) -> dict:
    prices = near_prices()
    samples, rpc_files = stnear_usd(prices)
    band = put_constant_value("mmf_penny_band")
    out: dict = {"files": prices.files + rpc_files, "prices": prices}

    vs = [s for s in samples if s["value"] is not None]
    if vs and band is not None:
        last = vs[-1]["date"]
        # t0 is the sample a full year back (the window's first day); the days tested run after it to `last`
        window = [s for s in vs if last - timedelta(days=365) <= s["date"] <= last]
        t0 = window[0]
        days = window[1:]
        at_par = [s for s in days if abs(s["value"] / t0["value"] - 1) <= band / 100]
        out["nav"] = {"t0": t0["date"], "last": last, "n": len(days), "at_par": len(at_par),
                      "worst": min((s["value"] / t0["value"] - 1 for s in days), default=None)}
    else:
        out["nav"] = None
    out["nav_reason"] = ("mmf_penny_band is not declared in thesis/constants.toml" if band is None else
                         "no stNEAR rate sample with a NEAR close for the day before")

    rates = [(s["date"], s["rate"]) for s in samples]
    pairs = list(zip(rates, rates[1:]))
    out["down"] = {"n": len(pairs), "down": sum(1 for (_, a), (_, b) in pairs if b < a),
                   "first": rates[0][0] if rates else None, "last": rates[-1][0] if rates else None}

    snap, sfiles = load.json_file("rpc", "snapshot_now")
    mp = ((snap or {}).get("lst") or {}).get("metapool_state") if isinstance(snap, dict) else None
    out["metapool"], out["snap_files"], out["snap"] = mp, sfiles, snap
    out["then"] = epoch_hours_then()
    return out


def compute(reg: Registry) -> None:
    d = analyze(reg)
    files = d["files"]
    band = put_constant(reg, "mmf_penny_band", "mmf_penny_band", "%")
    lo = put_constant(reg, "unlock_epochs_lo", "unlock_epochs", "count", "lo")
    hi = put_constant(reg, "unlock_epochs_hi", "unlock_epochs", "count", "hi")

    nav = d["nav"]
    if nav and nav["n"]:
        inputs = [f"reference {iso(nav['t0'])}, {nav['n']} later samples through {iso(nav['last'])}",
                  f"band +/-{band}% of V(t0)", f"worst V(t)/V(t0) - 1 = {F.pct(nav['worst']):.2f}%"]
        reg.put("nav_days_at_par_365", nav["at_par"], "count", "F-nav-par", source_files=files, inputs=inputs)
        reg.put("n_nav_days", nav["n"], "count", "F-nav-par", source_files=files, inputs=inputs)
    else:
        reason = d["nav_reason"] if not nav else "only one stNEAR sample in the last 365 days"
        reg.put("nav_days_at_par_365", None, "count", "F-nav-par", source_files=files, null_reason=reason)
        reg.put("n_nav_days", None, "count", "F-nav-par", source_files=files, null_reason=reason)

    dn = d["down"]
    reason = None if dn["n"] else "fewer than two stNEAR rate samples in rpc/history_samples.csv or rpc/dense_samples.csv"
    inputs = [f"stNEAR samples {iso(dn['first'])}..{iso(dn['last'])}"] if dn["n"] else []
    reg.put("stnear_down_days", dn["down"] if dn["n"] else None, "count", "F-down-days", source_files=files,
            inputs=inputs, null_reason=reason)
    reg.put("n_lst_days", dn["n"] if dn["n"] else None, "count", "F-down-days", source_files=files,
            inputs=inputs + ["consecutive sample pairs compared"], null_reason=reason)

    eh, why_eh = upstream(reg, "epoch_hours_median_30d")
    eh_files = reg.get("epoch_hours_median_30d").source_files if "epoch_hours_median_30d" in reg else []
    for key, n in (("unstake_hours_lo", lo), ("unstake_hours_hi", hi)):
        ok = eh is not None and n is not None
        reg.put(key, eh * n if ok else None, "hours", "F-unstake", source_files=eh_files,
                inputs=[f"{n} epochs (unlock_epochs) x epoch_hours_median_30d"],
                null_reason=None if ok else (why_eh or "unlock_epochs is not declared in thesis/constants.toml"))
    then, year, tfiles, why_then, n_then = d["then"]
    reg.put("epoch_hours_2021", then, "hours", "F-epoch-then", source_files=tfiles,
            inputs=[f"median epoch_seconds of the {n_then} complete epochs sampled in {year} "
                    "(rpc/epoch_history.csv, one per sample date) / 3600"] if then else [],
            null_reason=why_then, decimals=2)
    reg.put("unstake_then_year", str(year) if then else None, "text", "F-epoch-then", source_files=tfiles,
            inputs=["calendar year of the early epoch-duration window"], null_reason=why_then)
    for key, n in (("unstake_hours_2021_lo", lo), ("unstake_hours_2021_hi", hi)):
        ok = then is not None and n is not None
        reg.put(key, then * n if ok else None, "hours", "F-unstake", source_files=tfiles,
                inputs=[f"{n} epochs (unlock_epochs) x epoch_hours_2021"],
                null_reason=None if ok else (why_then or "unlock_epochs is not declared"))

    mp, sfiles = d["metapool"], d["snap_files"]
    stamp = f"snapshot height {(d['snap'] or {}).get('height')} at {(d['snap'] or {}).get('block_time_utc')}"
    def mp_val(field):
        return None if not mp else mp.get(field)
    reason = "rpc/snapshot_now.json has no lst.metapool_state"
    nslp = mp_val("nslp_liquidity_near")
    reg.put("nslp_near", nslp, "NEAR", "F-nslp", source_files=sfiles,
            inputs=[stamp, "meta-pool.near get_contract_state nslp_liquidity"], null_reason=None if nslp is not None else reason)
    for key, fld in (("nslp_fee_now", "nslp_current_discount_bp"), ("nslp_fee_min", "nslp_min_discount_bp"),
                     ("nslp_fee_max", "nslp_max_discount_bp")):
        bp = mp_val(fld)
        reg.put(key, bp / 100 if bp is not None else None, "%", "F-nslp", source_files=sfiles,
                inputs=[stamp, f"{fld} / 100"], null_reason=None if bp is not None else reason)
    close, why_close = upstream(reg, "near_close_latest")
    ok = nslp is not None and close is not None
    reg.put("nslp_usd", nslp * close if ok else None, "USD", "F-nslp",
            source_files=sfiles + (reg.get("near_close_latest").source_files if "near_close_latest" in reg else []),
            inputs=["nslp_near x near_close_latest"],
            null_reason=None if ok else (reason if nslp is None else why_close))

    # Row outcomes.
    out = row_outcomes(reg)
    for i, (usd, near) in enumerate(out, start=1):
        reg.put(f"h6_row{i}_usd", usd, "text", "F-h6-rows", inputs=[ROWS[i - 1]],
                null_reason=None if usd else "an input of this row is not computed")
        if i < 4:
            reg.put(f"h6n_row{i}", near, "text", "F-h6-rows", inputs=[ROWS[i - 1]],
                    null_reason=None if near else "an input of this row is not computed")
    usd = [u for u, _ in out]
    reg.put("h6_rows_total", len(ROWS), "count", "F-h6-rows", inputs=list(ROWS))
    reg.put("h6_rows_passed", sum(1 for u in usd if u == "pass"), "count", "F-h6-rows",
            inputs=[f"row {i}: {u or 'not computed'}" for i, u in enumerate(usd, start=1)])
    reg.put("h6_critical_failed", sum(1 for i in (0, 3) if usd[i] == "fail"), "count", "F-h6-rows",
            inputs=[f"row 1: {usd[0] or 'not computed'}", f"row 4: {usd[3] or 'not computed'}"])
    n1, n2, n3 = out[0][1], out[1][1], out[2][1]
    reg.put("h6n_rows13_pass", sum(1 for o in (n1, n3) if o == "pass") if (n1 and n3) else None, "count",
            "F-h6-rows", inputs=[f"row 1: {n1}", f"row 3: {n3}"],
            null_reason=None if (n1 and n3) else "NEAR row 1 or row 3 is not computed")
    reg.put("h6n_row2_fail", (1 if n2 == "fail" else 0) if n2 else None, "count", "F-h6-rows",
            inputs=[f"row 2: {n2}"], null_reason=None if n2 else "row 2 is not computed")


def row_outcomes(reg: Registry) -> list[tuple[str | None, str | None]]:
    def v(key):
        return upstream(reg, key)[0]
    nav, n_nav = v("nav_days_at_par_365"), v("n_nav_days")
    row1_usd = None if not n_nav else ("pass" if nav == n_nav else "fail")
    down, n_lst = v("stnear_down_days"), v("n_lst_days")
    row1_near = None if not n_lst else ("pass" if down == 0 else "fail")
    hi, nslp = v("unstake_hours_hi"), v("nslp_near")
    if hi is None:
        row2 = None
    elif hi <= SAME_DAY_HOURS:
        row2 = "pass"
    else:
        row2 = "partial" if (nslp or 0) > 0 else "fail" if nslp is not None else None
    beat = v("pct_beat_cash_1y")   # one-year windows ending in the trailing year
    row3_usd = _outcome_ratio(beat, 100) if beat is not None else None
    share = v("staker_share_365")   # the trailing year
    row3_near = None if share is None else ("pass" if share > 0 else "fail")
    row4_usd = "fail" if put_constant_value("sweep_definition", "quote") else None
    return [(row1_usd, row1_near), (row2, row2), (row3_usd, row3_near), (row4_usd, "n/a")]


ROW_TEMPLATES = (
    ("stNEAR in dollars stayed within plus or minus {mmf_penny_band} of its starting value on "
     "{nav_days_at_par_365} of {n_nav_days} sampled days in the last year; NEAR's spot price (not stNEAR) fell "
     "{mdd_365:abs} from its high within the last year at worst",
     "The stNEAR rate fell on {stnear_down_days} of {n_lst_days} samples (by construction it cannot fall unless "
     "validators are slashed; see the slashing open item)"),
    ("Native unstake takes {unstake_hours_lo} to {unstake_hours_hi} (in {unstake_then_year}: "
     "{unstake_hours_2021_lo} to {unstake_hours_2021_hi}); Meta Pool's instant pool holds {nslp_near} (about "
     "{nslp_usd}) at a {nslp_fee_now} fee (range {nslp_fee_min} to {nslp_fee_max}); LiNEAR instant exit not "
     "measured", None),
    ("stNEAR beat fed funds in dollars in {pct_beat_cash_1y} of the {n_windows_usd_1y} weekly one-year windows "
     "that end within the last year",
     "Over the last year a liquid staker's share of all NEAR changed by {staker_share_365}; it grows only when "
     "staking yield beats supply growth"),
    ("The SEC's sweep-program definition names only a money market fund or an FDIC-insured bank deposit, so a "
     "token cannot qualify", "Not applicable: a NEAR treasury is not a broker sweep"),
)


def _keys(template: str) -> list[str]:
    return [name for _, name, _, _ in string.Formatter().parse(template) if name]


def _cell(reg: Registry, template: str) -> tuple[str, str]:
    """(template usable by the page's text(), resolved text) — a template with an unregistered key degrades."""
    missing = [k for k in _keys(template) if k not in reg]
    if missing:
        text = "not computed: " + ", ".join(missing) + " not registered"
        return text, text
    return template, fill(template, reg)


CRITICAL_ROWS = ((0, "principal stability"), (3, "the legal-vehicle rule"))   # the rows h6_critical_failed counts
SAME_DAY_TAIL = {"pass": "; same-day cash passes.", "partial": "; same-day cash is only partial.",
                 "fail": "; same-day cash fails too."}


def checklist_title(usd: list[str | None]) -> str:
    """c14 title from the USD row states: it names the critical rows that fail (the rows h6_critical_failed
    counts, so the title cannot disagree with the H6 verdict) and states row 2 as measured, never as assumed."""
    failed = [name for i, name in CRITICAL_ROWS if usd[i] == "fail"]
    if not failed:
        return "Staked NEAR against the sweep-account checklist"
    return ("NEAR fails the sweep-account tests that decide the verdict: " + " and ".join(failed)
            + SAME_DAY_TAIL.get(usd[1], "."))


def checklist(reg: Registry) -> Chart:
    """c14 sweep-checklist: requirement x (USD sweep, NEAR treasury), with an epoch-duration sparkline."""
    d = analyze(reg)
    outcomes = row_outcomes(reg)
    rows, templates = [], []
    for i, (req, (usd_t, near_t)) in enumerate(zip(ROWS, ROW_TEMPLATES)):
        usd_tpl, usd_txt = _cell(reg, usd_t)
        near_tpl, near_txt = _cell(reg, near_t or usd_t)
        usd_o, near_o = outcomes[i]
        rows.append([req, usd_txt, usd_o or "not computed", near_txt, near_o or "not computed"])
        templates.append({"requirement": req, "usd": usd_tpl, "usd_outcome_key": f"h6_row{i + 1}_usd",
                          "near": near_tpl, "near_outcome_key": f"h6n_row{i + 1}" if i < 3 else None})
    mints, mfiles = load.csv_rows("rpc", "epoch_mints")
    spark = []
    for r in mints:
        try:
            spark.append([r["block_time_utc"], round(float(r["epoch_seconds"]) / 3600, 4)])
        except (KeyError, TypeError, ValueError):
            continue
    # recent epochs only: the file now holds every boundary of the trailing year (the exact burn share), so keep the
    # same window as epoch_hours_median_30d, the duration the unstake rows use
    spark.sort(key=lambda p: p[0])
    if spark:
        newest = datetime.fromisoformat(spark[-1][0])
        spark = [p for p in spark if datetime.fromisoformat(p[0]) >= newest - timedelta(days=EPOCH_WINDOW_DAYS)]
    files = d["files"] + d["snap_files"] + mfiles + d["then"][2]
    sweep_src = put_constant_value("sweep_definition", "source") or "https://www.ecfr.gov/current/title-17/section-240.15c3-3"
    chart = Chart(
        id="c14", section="A4", priority="lead", claims=["H6", "H6_near"], verdicts=["H6", "H6_near"],
        title="", sentence="", form="table",
        sources=["NEAR archival RPC (meta-pool.near get_st_near_price; block heights and times)",
                 f"NEAR RPC ({RPC_TIP}, meta-pool.near get_contract_state at the tip)",
                 d["prices"].source or "Binance spot NEARUSDT daily klines",
                 "FRED (fredgraph.csv: DFF) via the rolling dollar-return model",
                 f"Declared constants: Rule 2a-7 penny rounding and the 15c3-3 sweep definition ({sweep_src})"],
        queries=[rpc_stnear_query(), near_query(d["prices"]),
                 rpc_query("query", {"request_type": "call_function", "finality": "final",
                                     "account_id": "meta-pool.near", "method_name": "get_contract_state",
                                     "args_base64": "e30="}, endpoint=RPC_TIP, file="data/raw/rpc/snapshot_now.json"),
                 rpc_query("block", {"block_id": "<epoch boundary height>"}, file="data/raw/rpc/epoch_mints.csv"),
                 derived_query("par: |V(t)/V(t0) - 1| <= mmf_penny_band% with V = stNEAR rate x NEAR close(d-1); "
                               "unstake = unlock_epochs x epoch hours; NSLP fee = discount bp / 100")],
        table={"columns": ["requirement", "as_a_usd_sweep", "usd_outcome", "as_a_near_treasury", "near_outcome"],
               "rows": rows},
        code_ref="pipeline/models/sweep.py::checklist",
        formula_ids=["F-nav-par", "F-down-days", "F-unstake", "F-nslp", "F-h6-rows"],
        x={"type": "category"}, y={"unit": "text"},
        panels=[{"id": "checklist", "label": "Sweep-account checklist", "series_ids": [], "row_templates": templates},
                {"id": "liquidity", "label": "Epoch duration, recent epochs", "series_ids": ["epoch_hours"]}],
        series=[Series("epoch_hours", "Epoch duration (hours)", "context", spark, role="context", unit="hours",
                       panel="liquidity")],
        coverage={"first": spark[0][0] if spark else None, "last": spark[-1][0] if spark else None,
                  "n": len(spark), "expected": len(spark), "gaps": []},
        as_of=files_as_of(files),
    )
    chart.sentence = ("Rows list what a broker sweep must provide under SEC rules, next to what staked NEAR measured "
                      "on each test, once as a dollar vehicle and once for balances already held in NEAR.")
    usd = [u for u, _ in outcomes]
    chart.title = checklist_title(usd)
    if any(u is None for u in usd) or any(n is None for _, n in outcomes[:3]):
        chart.status = "partial"
        chart.status_banner = ("Some checklist rows are not computed; they read \"not computed\" and "
                               "do not count as passes.")
    if not any(o for pair in outcomes[:3] for o in pair):
        chart.status = "unavailable"
        chart.empty_state = empty_state("No staked NEAR rate samples, prices or Meta Pool state.",
                                        "RPC stNEAR samples, NEAR daily closes and rpc/snapshot_now.json.",
                                        "uv run python -m pipeline.pull --only near_rpc,prices && make charts")
    return chart


def charts(reg: Registry) -> list[Chart]:
    return [checklist(reg)]
