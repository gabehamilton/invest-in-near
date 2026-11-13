"""NEAR staking yield against fed funds: liquid-staking yield, the protocol formula, spreads and the yield grid.

LST rates, staked share, ceiling, US rates; charts c01, c10, c12.

- F-lst: y = (P_end/P_start)^(365.25/Δd) - 1 on stNEAR (meta-pool.near get_st_near_price) and LiNEAR
  (linear-protocol.near ft_price); y_lst is their mean where both exist, else the one that does.
- Like-for-like spread (F-spread-adj): y_lst minus F-ffcomp (compounded DFF, ACT/360) over the same dates.
- One window rule for every trailing figure (y_lst_365/90, spread_adj_365/90, effr_mean_365, spread_naive_365,
  y_formula_365, lst_haircut): the window ends at the latest full-day (00:00 UTC) LST sample that DFF covers (the
  latest full-day LST sample when DFF is absent), so a one-day FRED lag never leaves a day uncompounded and a yield
  and its spread always describe the same dates. Samples are the merged RPC set (supply.load_samples: monthly
  history plus weekly-then-daily dense samples), so with daily samples each window spans exactly 365 or 90 days.
- Naive spread: y_lst minus the mean EFFR over the y_lst window.
- Formula yield: (1 - treasury_share) x max_inflation_rate / s, where s = validator stake / total supply (stake lags
  the sampled block by about two epochs).
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta

from pipeline.chartspec import Chart, Series
from pipeline.models import load
from pipeline.models.formulas import (add_months, correlation, ffcomp, mean_between, pct,
                                      quarter_label, quarter_start, yoy)
from pipeline.models import supply
from pipeline.models.params import param
from pipeline.models.supply import (FIX, GAP_DAYS, RPC_SOURCE, WINDOW_DAYS, Samples, as_of, block_queries,
                                    days_between, fred_query, fred_source, gaps,
                                    load_samples, lst_queries, lst_yield, growth, pick, pick_notes, put, r4,
                                    sample_ref, staker_fraction, val, validators_query, weekly, with_breaks)
from pipeline.numbers import Registry

# --- parameters (thresholds and scenario inputs, not output figures) --------------------------------------
# author-chosen parameters: thesis/rules.toml [params] (pinned to the rule literals by tests/test_params.py)
BAND_PP = param("band_pp")      # c12: |spread_q| <= BAND_PP counts as "in balance" (the zone the hypothesis implies)
S_HYPO = param("s_hypo")        # c10: hypothetical staked share column (tripwire T6 fires above it)
H4_SD_PP = param("h4_sd_pp")    # H4 supported needs spread_q_sd_rule below this; the c12 title switches on it
SHORT_DAYS = 90        # y_lst_90, spread_adj_90 and the c01 trailing yield
MERGE_DAYS = 30        # c01: annotations closer than this merge into one label
LABEL_MAX = 28         # chart annotation labels stay short; the full statement is in the sentence
CHART_START = date(2021, 1, 1)


@dataclass
class Window:
    r0: dict
    r1: dict
    y: float | None = None
    ff: float | None = None
    dff_mean: float | None = None
    label: str = ""

    @property
    def spread_pp(self) -> float | None:
        return None if self.y is None or self.ff is None else (self.y - self.ff) * 100


@dataclass
class StakingModel:
    smp: Samples
    files: list[str]
    dff: dict
    dff_files: list[str]
    dff_end: date | None
    effr: list
    effr_files: list[str]
    lst_start: date | None
    latest: dict | None                      # latest full-day sample with an LST price
    y365: Window | None
    y90: Window | None
    adj365: Window | None                    # like-for-like window (ends where DFF ends)
    adj90: Window | None
    quarters: list[Window] = field(default_factory=list)
    missing_q: list[str] = field(default_factory=list)
    qtd: Window | None = None
    end: dict | None = None                  # where every trailing window ends (see the module docstring)


def _trailing(smp: Samples, end: dict | None, days: int, col: str = "lst") -> Window | None:
    if end is None:
        return None
    r0 = smp.edge(end["date"] - timedelta(days=days), col)
    if r0 is None or r0["time"] >= end["time"]:
        return None
    return Window(r0, end)


def _dff_end_row(smp: Samples, dff_end: date | None) -> dict | None:
    """Latest LST sample whose date DFF covers: ffcomp over [start, date) needs DFF through date - 1."""
    if dff_end is None:
        return None
    rows = [r for r in smp.with_("lst", full_only=True) if r["date"] <= dff_end + timedelta(days=1)]
    return rows[-1] if rows else None


def _fill(w: Window | None, dff: dict) -> Window | None:
    if w is None:
        return None
    w.y = lst_yield(w.r0, w.r1)
    w.ff = ffcomp(dff, w.r0["date"], w.r1["date"])
    w.dff_mean = mean_between(sorted(dff.items()), w.r0["date"], w.r1["date"])
    if w.dff_mean is not None:
        w.dff_mean /= 100
    return w


def build_model() -> StakingModel:
    smp = load_samples()
    dff_series = load.fred("DFF")
    dff = dff_series.as_dict()
    non_null = dff_series.non_null()
    dff_end = non_null[-1][0] if non_null else None
    effr = load.fred("EFFR")
    lst_rows = smp.with_("lst", full_only=True)
    latest = lst_rows[-1] if lst_rows else None
    cover = _dff_end_row(smp, dff_end)
    end = cover if cover is not None else latest   # one window rule for yields and spreads
    m = StakingModel(smp, smp.files, dff, dff_series.files, dff_end, effr.points, effr.files,
                     lst_rows[0]["date"] if lst_rows else None, latest,
                     _trailing(smp, end, WINDOW_DAYS), _trailing(smp, end, SHORT_DAYS), None, None, end=end)
    for w in (m.y365, m.y90):
        if w is not None:
            w.y = lst_yield(w.r0, w.r1)
    m.adj365 = _fill(_trailing(smp, cover, WINDOW_DAYS), dff)
    m.adj90 = _fill(_trailing(smp, cover, SHORT_DAYS), dff)

    if latest is not None:
        qs = quarter_start(m.lst_start)
        started = False
        while add_months(qs, 3) <= latest["date"]:
            qe = add_months(qs, 3)
            r0, r1 = smp.edge(qs, "lst"), smp.edge(qe, "lst")
            w = _fill(Window(r0, r1, label=quarter_label(qs)), dff) if r0 and r1 and r0["time"] < r1["time"] \
                else None
            if w is not None and w.y is not None and w.ff is not None:
                m.quarters.append(w)
                started = True
            elif started:
                m.missing_q.append(quarter_label(qs))
            qs = qe
        qs = quarter_start(latest["date"])
        r0 = smp.edge(qs, "lst")
        end = cover if cover is not None and cover["date"] > qs else None
        if r0 is not None and end is not None and r0["time"] < end["time"]:
            m.qtd = _fill(Window(r0, end, label=quarter_label(qs) + " QTD"), dff)

    return m


def _formula_365(m: StakingModel) -> tuple[float | None, str | None, list[str]]:
    """Mean of (1 - treasury_share)·i/s over samples in the trailing year ending where y_lst_365 ends."""
    frac, cfiles = staker_fraction()
    if frac is None:
        return None, "thesis/constants.toml treasury_share is missing", cfiles
    end = m.end or m.smp.latest_full()
    if end is None:
        return None, "no NEAR RPC samples", cfiles
    start = end["date"] - timedelta(days=WINDOW_DAYS)
    rows = [r for r in m.smp.rows if start < r["date"] <= end["date"] and r["full_day"] and r.get("s") and
            r.get("max_inflation_rate") is not None]
    if not rows:
        return None, f"no sample with stake and max_inflation_rate between {start} and {end['date']}", cfiles
    edges = [start] + [r["date"] for r in rows] + [end["date"]]
    worst = max((b - a).days for a, b in zip(edges, edges[1:]))
    if worst > GAP_DAYS:
        return None, (f"samples with stake cover {start} to {end['date']} with a gap of {worst} days "
                      f"(more than {GAP_DAYS})"), cfiles
    return statistics.fmean(frac * r["max_inflation_rate"] / r["s"] for r in rows), None, cfiles


def _sep(m: StakingModel):
    md = load.fred("FEDTARMD")
    lr = load.fred("FEDTARMDLR")
    anchor = (m.latest or m.smp.latest() or {}).get("date")
    row = None
    if anchor is not None:
        row = next(((d, v) for d, v in md.non_null() if d > anchor), None)
    lr_last = lr.non_null()[-1] if lr.non_null() else None
    return row, md.files, lr_last, lr.files, anchor


def compute(reg: Registry) -> None:
    m = build_model()
    files = m.files
    no_lst = "no liquid-staking (stNEAR or LiNEAR) price in the RPC samples"

    put(reg, "lst_start_date", m.lst_start.isoformat() if m.lst_start else None, "date", "D3", files,
        ["first sample with a stNEAR or LiNEAR price"], no_lst)

    def edge_reason(days: int) -> str:
        if m.end is None:
            return no_lst
        target = m.end["date"] - timedelta(days=days)
        return (f"the window ends at {m.end['date']} (the latest LST sample DFF covers); no LST price sample within "
                f"7 days of {target.isoformat()} (window start)")

    def lst_inputs(w: Window) -> list[str]:
        return [f"LST prices {sample_ref(w.r0)}", f"LST prices {sample_ref(w.r1)}",
                f"{days_between(w.r0, w.r1):.3f} days", "window ends at the latest LST sample DFF covers"]

    for key, w, days in (("y_lst_365", m.y365, WINDOW_DAYS), ("y_lst_90", m.y90, SHORT_DAYS)):
        put(reg, key, pct(w.y) if w else None, "%", "F-lst", files, lst_inputs(w) if w else [], edge_reason(days))
    # supply growth over exactly the y_lst_365 samples, so "yield minus supply growth" (currencies reading A)
    # never mixes two windows (g_365 itself ends at the latest supply sample)
    g_w = growth(m.y365.r0, m.y365.r1) if m.y365 else None
    put(reg, "g_y_lst_365", pct(g_w), "%", "F-growth", files,
        [f"total_supply_near {sample_ref(m.y365.r0)}", f"total_supply_near {sample_ref(m.y365.r1)}",
         "the y_lst_365 window"] if m.y365 else [],
        edge_reason(WINDOW_DAYS) if m.y365 is None else "a y_lst_365 edge sample has no total supply")
    for key, col in (("y_st_365", "stnear_price"), ("y_li_365", "linear_price")):
        # the same window end as y_lst_365 when that sample carries this token's price
        end = m.end if m.end is not None and m.end.get(col) is not None else m.smp.latest_full(col)
        w = _trailing(m.smp, end, WINDOW_DAYS, col)
        y = growth(w.r0, w.r1, col) if w else None
        reason = (f"no {col} sample" if end is None else
                  f"no {col} sample within 7 days of {(end['date'] - timedelta(days=WINDOW_DAYS)).isoformat()}")
        put(reg, key, pct(y), "%", "F-lst", files,
            [f"{col} {sample_ref(w.r0)}", f"{col} {sample_ref(w.r1)}"] if w else [], reason)

    formula, why, cfiles = _formula_365(m)
    put(reg, "y_formula_365", pct(formula), "%", "F-formula-yield", files + cfiles,
        ["(1 - treasury_share) x max_inflation_rate / s, mean over samples in the trailing year",
         "validator stake lags the sampled block by about two epochs"], why)
    haircut = m.y365.y / formula if (m.y365 and m.y365.y is not None and formula) else None
    put(reg, "lst_haircut", haircut, "ratio", "F-haircut", files + cfiles, ["y_lst_365", "y_formula_365"],
        "needs both y_lst_365 and y_formula_365")

    # EFFR / DFF
    effr_window = (m.y365.r0["date"], m.y365.r1["date"]) if m.y365 else (
        (m.smp.latest_full()["date"] - timedelta(days=WINDOW_DAYS), m.smp.latest_full()["date"])
        if m.smp.latest_full() else None)
    effr_mean = mean_between(m.effr, *effr_window) if effr_window else None
    put(reg, "effr_mean_365", effr_mean, "%", "F-mean", m.effr_files,
        [f"mean of EFFR business days in [{effr_window[0]}, {effr_window[1]})"] if effr_window else [],
        "no EFFR observations in the window" if effr_window else "no NEAR samples to anchor the window")
    effr_nn = [(d, v) for d, v in m.effr if v is not None]
    put(reg, "effr_latest", effr_nn[-1][1] if effr_nn else None, "%", "D7", m.effr_files,
        [f"EFFR {effr_nn[-1][0].isoformat()}"] if effr_nn else [], "no EFFR observations")

    rate_files = sorted(set(files + m.dff_files))
    cover = _dff_end_row(m.smp, m.dff_end)
    for key, w, days in (("spread_adj_365", m.adj365, WINDOW_DAYS), ("spread_adj_90", m.adj90, SHORT_DAYS)):
        # The like-for-like window ends at the latest LST sample DFF covers (`cover`); only that window is tried.
        reason = (no_lst if m.latest is None else "DFF has no observations" if m.dff_end is None else
                  f"no LST sample on or before {m.dff_end + timedelta(days=1)} (DFF ends {m.dff_end})"
                  if cover is None else
                  f"the window ends at the latest LST sample DFF covers ({cover['date']}); no LST sample lies "
                  f"within 7 days of its start ({cover['date'] - timedelta(days=days)}), or DFF misses a day "
                  "inside it")
        sp = w.spread_pp if w else None
        put(reg, key, sp, "pp", "F-spread-adj", rate_files,
            lst_inputs(w) + [f"F-ffcomp DFF over [{w.r0['date']}, {w.r1['date']})"] if w else [], reason)
        # direction in words: prose prints {spread_adj_x:abs} {spread_adj_x_dir} fed funds, never a sign
        put(reg, f"{key}_dir", None if sp is None else ("above" if sp >= 0 else "below"), "text", "F-spread-adj",
            rate_files, [f"{key} >= 0 -> above"], f"{key} is not computed: {reason}")
        if key == "spread_adj_365":
            # the comparator itself, so a page can print "yield, fed funds, spread" and the three subtract
            ff = w.ff if w else None
            put(reg, "ffcomp_365", pct(ff) if ff is not None else None, "%", "F-ffcomp", rate_files,
                [f"F-ffcomp DFF over [{w.r0['date']}, {w.r1['date']})", "the spread_adj_365 window"] if w else [],
                reason)
    naive = (pct(m.y365.y) - effr_mean) if (m.y365 and m.y365.y is not None and effr_mean is not None) else None
    put(reg, "spread_naive_365", naive, "pp", "F-spread-naive", sorted(set(files + m.effr_files)),
        ["y_lst_365", "effr_mean_365"], "needs both y_lst_365 and effr_mean_365")

    # quarterly spread (c12): H4 reads only quarters under the issuance rule now in force
    qs = m.quarters
    q_reason = no_lst if m.latest is None else "no complete quarter has LST samples at both edges and DFF"
    # the band is a registered parameter, so the page prints it through the registry and the label names the key
    reg.put("band_pp", BAND_PP, "pp", "P-band_pp", source_files=["thesis/rules.toml"],
            inputs=["thesis/rules.toml [params] band_pp: the c12 in-balance band around zero"], signed=False)
    cut = val(reg, "cut_date")
    rule_qs = rule_quarters(m, cut)
    if cut is None:
        rule_reason = "cut_date is not registered (no change of issuance rule found in the samples)"
    else:
        rule_reason = q_reason if not qs else f"no complete quarter starts on or after {cut}"
    r_spreads = [w.spread_pp for w in rule_qs]
    r_inputs = ([f"{len(rule_qs)} complete quarters starting on or after cut_date {cut}: "
                 + ", ".join(w.label for w in rule_qs)] if rule_qs else [f"no complete quarter starts on or after {cut}"])
    reg.put("n_q_rule", len(rule_qs) if cut is not None else None, "count", "F-spread-q", source_files=rate_files,
            inputs=r_inputs, null_reason=None if cut is not None else rule_reason)
    reg.put("spread_q_sd_rule", statistics.stdev(r_spreads) if len(r_spreads) >= 2 else None, "pp", "F-spread-q",
            source_files=rate_files, inputs=r_inputs, signed=False,
            null_reason=None if len(r_spreads) >= 2 else
            (rule_reason if not r_spreads else "a sample sd needs at least two quarters"))
    r_levels = correlation([w.y for w in rule_qs], [w.dff_mean for w in rule_qs]) if rule_qs else None
    put(reg, "corr_levels_rule", r_levels, "ratio", "F-corr", rate_files,
        r_inputs + ["Pearson(quarterly LST yield, quarterly mean DFF)"],
        rule_reason if not rule_qs else "Pearson needs at least three quarters with varying values")
    tar = load.fred("DFEDTARU")
    if rule_qs:
        span = [v for d, v in tar.non_null() if rule_qs[0].r0["date"] <= d < rule_qs[-1].r1["date"]]
        r_range = round((max(span) - min(span)) * 100, 6) if span else None
    else:
        r_range = None
    put(reg, "fed_range_q_bp_rule", r_range, "count", "D7-range", tar.files,
        [f"max - min DFEDTARU over [{rule_qs[0].r0['date']}, {rule_qs[-1].r1['date']}), in basis points"]
        if rule_qs else [], rule_reason if not rule_qs else "DFEDTARU has no observations over the quarters",
        decimals=0)

    # c10 grid cells
    _compute_sensitivity(reg, m)


def rule_quarters(m: StakingModel, cut: str | None) -> list[Window]:
    """The complete quarters H4 reads: those whose start sample is on or after cut_date, the latest
    max_inflation_rate change (supply registers cut_date first)."""
    if cut is None:
        return []
    cut_d = date.fromisoformat(cut)
    return [w for w in m.quarters if w.r0["date"] >= cut_d]


def c10_cell(frac: float | None, i_pct: float | None, s_pct: float | None, h: float | None) -> float | None:
    """F-c10-cell: a liquid staker's real yield, % = (1 - treasury_share) x i / s x lst_haircut x 100, with i and s
    in percent. None where an input is null or s is not positive."""
    if None in (frac, h, i_pct, s_pct) or s_pct <= 0:
        return None
    return frac * (i_pct / 100) / (s_pct / 100) * h * 100


def _cells(reg: Registry) -> dict:
    """c10 grid: cell = (1 - treasury_share) x i / s x lst_haircut, in percent; None where an input is null."""
    frac, _ = staker_fraction()
    h = val(reg, "lst_haircut")
    rows = [("i_max_now", "On chain now", val(reg, "i_max_now"))]
    cols = [("s_now", "Staked share now", val(reg, "s_now")), ("s_max", "Highest staked share observed",
                                                                 val(reg, "s_max")),
            ("s_hypo", "Hypothetical staked share", S_HYPO * 100)]
    grid = {}
    for rk, rlabel, i_pct in rows:
        for ck, clabel, s_pct in cols:
            value = c10_cell(frac, i_pct, s_pct, h)
            grid[(rk, ck)] = {"row": rlabel, "col": clabel, "i": i_pct, "s": s_pct, "value": value}
    return grid


def _compute_sensitivity(reg: Registry, m: StakingModel) -> None:
    frac, cfiles = staker_fraction()
    if "s_hypo" not in reg:
        put(reg, "s_hypo", S_HYPO * 100, "%", "P-s_hypo", ["thesis/rules.toml"],
            ["thesis/rules.toml [params] s_hypo: the c10 hypothetical staked-share column"])
    sep_row, md_files, lr_last, lr_files, anchor = _sep(m)
    # the projection for the year already under way is skipped on purpose; the inputs say which one
    skipped = [f"FEDTARMD {d.isoformat()} ({d.year}, the year under way) skipped: dated before {anchor}"
               for d, _ in load.fred("FEDTARMD").non_null() if anchor is not None and d <= anchor
               and d.year == anchor.year]
    put(reg, "sep_year", str(sep_row[0].year) if sep_row else None, "text", "D7", md_files,
        [f"first FEDTARMD projection year after {anchor}"] + skipped if sep_row else skipped,
        "FEDTARMD has no projection year after the latest NEAR sample" if anchor else "no NEAR samples")
    put(reg, "sep_median", sep_row[1] if sep_row else None, "%", "D7", md_files,
        [f"FEDTARMD {sep_row[0].isoformat()}"] if sep_row else [],
        "FEDTARMD has no projection year after the latest NEAR sample" if anchor else "no NEAR samples")
    put(reg, "sep_longer_run", lr_last[1] if lr_last else None, "%", "D7", lr_files,
        [f"FEDTARMDLR {lr_last[0].isoformat()}"] if lr_last else [], "no FEDTARMDLR observations")
    grid = _cells(reg)
    files = sorted(set(m.files + cfiles))
    absent = [k for k in ("lst_haircut", "i_max_now", "s_now") if val(reg, k) is None]
    absent += ["treasury_share"] if frac is None else []
    reason = "needs lst_haircut, the ceiling and the staked share (null: " + ", ".join(absent or ["none"]) + ")"
    put(reg, "y_s60", grid[("i_max_now", "s_hypo")]["value"], "%", "F-c10-cell", files,
        ["lst_haircut", "i_max_now", "s_hypo", "treasury_share"], reason)


# =====================================================================================================
# charts
# =====================================================================================================

def charts(reg: Registry) -> list[Chart]:
    m = build_model()
    return [two_rates(reg, m), yield_sensitivity(reg, m), spread_quarterly(reg, m)]


def fed_turns(since: date) -> tuple[list[tuple[date, int]], list[str]]:
    """Dates where the direction of DFEDTARU moves flips (first hike after cuts, first cut after hikes)."""
    tar = load.fred("DFEDTARU")
    pts = tar.non_null()
    turns, last_sign = [], None
    for (_, prev), (d, cur) in zip(pts, pts[1:]):
        if cur == prev:
            continue
        sign = 1 if cur > prev else -1
        if sign != last_sign:
            if last_sign is not None and d >= since:
                turns.append((d, sign))
            last_sign = sign
    return turns, tar.files


def merge_annotations(items: list[dict], days: int = MERGE_DAYS) -> list[dict]:
    """Merge annotations closer than `days` on the same panel (an annotation without a panel spans all)."""
    out: list[dict] = []
    for a in sorted(items, key=lambda a: (a.get("panel") or "", a["x"])):
        prev = out[-1] if out else None
        if prev and prev.get("panel") == a.get("panel") and \
                (date.fromisoformat(a["x"]) - date.fromisoformat(prev["x"])).days < days:
            joined = prev["label"] + "; " + a["label"]
            if len(joined) <= LABEL_MAX:
                prev["label"] = joined
            else:  # too long for the plot: keep the first label, list the rest for the tooltip
                prev.setdefault("also", []).append(a["label"])
            prev["merged"] = True
        else:
            out.append(dict(a))
    return sorted(out, key=lambda a: (a["x"], a.get("panel") or ""))


def two_rates(reg: Registry, m: StakingModel) -> Chart:
    """c01: what you earn (LST yield, formula, EFFR) and what inflation takes (g_365, CPI-U YoY)."""
    sm = supply.build_model()
    frac, cfiles = staker_fraction()
    cpi = load.fred("CPIAUCSL")
    turns, tar_files = fed_turns(CHART_START)
    files = sorted(set(m.files + m.effr_files + cpi.files + tar_files + cfiles))
    chart = Chart(
        id="c01", section="thesis", priority="lead", claims=["H3", "H4"], title="", sentence="",
        form="small-multiples",
        sources=[RPC_SOURCE + ": meta-pool.near get_st_near_price, linear-protocol.near ft_price, validators, "
                 "EXPERIMENTAL_protocol_config, block", fred_source("EFFR", "CPIAUCSL", "DFEDTARU")],
        queries=lst_queries(m.smp) + [validators_query(m.smp)] + block_queries(m.smp) +
        [fred_query("EFFR"), fred_query("CPIAUCSL"), fred_query("DFEDTARU")],
        table={"columns": [], "rows": []}, code_ref="pipeline/models/staking.py::two_rates",
        formula_ids=["F-lst", "F-formula-yield", "F-growth", "F-yoy"], eyebrow="Lead chart · tests H3",
        x={"type": "time"}, y={"unit": "%"}, verdicts=["H3"], as_of=as_of(files),
        panels=[{"id": "earn", "label": "What you earn", "series_ids": ["lst_90", "formula", "effr"]},
                {"id": "inflation", "label": "What inflation takes", "series_ids": ["g_365", "cpi_yoy"]}])
    # magnitudes print unsigned ({key:abs}); the direction words are registry text keys chosen by sign
    # the gap is a trailing-year figure as of the CPI month, so the clause names that month (past tense)
    near_clause = "over the year to {cpi_month} its supply grew {gap_cpi:abs} {gap_cpi_dir} than US prices"
    lead = "In the last ninety days NEAR's staking rate ran {spread_adj_90:abs} {spread_adj_90_dir} fed funds"
    lead_y = "Over the last year NEAR's staking rate ran {spread_adj_365:abs} {spread_adj_365_dir} fed funds"
    # the trailing-year lead stands in while the ninety-day window has no edge
    chart.title_template = pick(reg, [
        *(f + "; " + near_clause + "." for f in (lead, lead_y)),
        lead + ".",
        lead_y + ".",
        "NEAR's liquid-staking yield against fed funds, and NEAR's supply growth against US prices.",
    ])
    cpi_blanks = sorted(d for d, v in cpi.points if v is None and d >= add_months(CHART_START, -12))
    chart.sentence_template = (
        "Left, what you earn: liquid staking over trailing ninety days (blue), the protocol formula before fees "
        "(gray) and fed funds (orange); right, what inflation takes: NEAR supply growth over the trailing year "
        "(blue) against US consumer prices (orange).")
    # the one thing that trips a reader: the two right-hand lines end on different dates
    chart.caveat_template = pick(reg, [
        "On the right, NEAR's line ends at the latest chain sample ({latest_rpc_date}) and CPI's at {cpi_month}, "
        "so the title compares the same month: NEAR {g_365_at_cpi} against CPI {cpi_yoy}.",
        "",
    ])
    chart.notes_templates = pick_notes(reg, [
        "BLS published no CPI for {cpi_blank_month} during the federal shutdown, so that month and the same month "
        "a year later are blank." if cpi_blanks else "",
        "Markers show the issuance ceiling cut, where the Fed turned to hiking or cutting, and where "
        "liquid-staking data starts ({lst_start_date}).",
        "The formula line uses validator stake, which lags the sampled block by about two epochs.",
        "The fed funds line is the monthly average of the effective rate.",
    ])

    lst90 = []   # the trailing ninety-day yield at every full-day sample date with a sample near its start
    for r in m.smp.with_("lst", full_only=True):
        w = _trailing(m.smp, r, SHORT_DAYS)
        y = lst_yield(w.r0, w.r1) if w else None
        if y is not None and r["date"] >= CHART_START:
            lst90.append([r["date"].isoformat(), r4(pct(y))])
    formula = []
    if frac is not None:
        for r in m.smp.rows:
            if r.get("s") and r.get("max_inflation_rate") is not None and r["date"] >= CHART_START:
                formula.append([r["date"].isoformat(), r4(pct(frac * r["max_inflation_rate"] / r["s"]))])
    effr_pts = []
    end = (m.smp.latest_full() or {}).get("date")
    if end is not None:
        mth = CHART_START
        while mth <= end:
            nxt = add_months(mth, 1)
            mean = mean_between(m.effr, mth, nxt)
            if mean is not None:
                effr_pts.append([mth.isoformat(), r4(mean)])
            mth = nxt
    g_pts = [[w["r1"]["date"].isoformat(), r4(pct(w["g"]))] for w in sm.series
             if w["r1"]["date"] >= CHART_START and (w["r1"]["date"].day == 1 or w["r1"] is sm.latest)]
    cpi_map = cpi.as_dict()
    cpi_pts = []
    if cpi_map:
        last_cpi = max(d for d, v in cpi_map.items() if v is not None)
        mth = CHART_START
        while mth <= last_cpi:
            y = yoy(cpi_map, mth)
            cpi_pts.append([mth.isoformat(), r4(pct(y))])
            mth = add_months(mth, 1)
    chart.series = [
        Series("lst_90", "Liquid-staking yield, trailing ninety days", "near", with_breaks(lst90), panel="earn"),
        Series("formula", "Protocol formula, before fees (model)", "context", with_breaks(formula), role="model",
               panel="earn"),
        Series("effr", "Effective fed funds rate, monthly mean", "usd", effr_pts, panel="earn"),
        Series("g_365", "NEAR supply growth, trailing year", "near", weekly(with_breaks(g_pts)), panel="inflation"),
        Series("cpi_yoy", "US CPI-U, year over year", "usd", cpi_pts, panel="inflation"),
    ]
    # the ceiling cut spans both panels; Fed turns and the LST start belong to "earn"; CPI blanks to "inflation"
    ann = [{"x": c.after.date.isoformat(), "label": "issuance ceiling cut", "kind": "ceiling_change"}
           for c in sm.chg if c.after.rate < c.before.rate]
    ann += [{"x": d.isoformat(), "label": "Fed hikes" if s > 0 else "Fed cuts",
             "kind": "fed_turn", "panel": "earn"} for d, s in turns]
    if m.lst_start:
        ann.append({"x": m.lst_start.isoformat(), "label": "LST data starts", "kind": "data_boundary",
                    "panel": "earn"})
    shown = {x for x, _ in cpi_pts}
    for d in cpi_blanks:
        for mth, label in ((d, "CPI not published"), (add_months(d, 12), "No CPI YoY (year-ago gap)")):
            if mth.isoformat() in shown:
                ann.append({"x": mth.isoformat(), "label": label, "kind": "data_boundary", "panel": "inflation"})
    chart.annotations = merge_annotations(ann)
    rows = []
    for s in chart.series:
        for x, y in s.points:
            if y is not None or s.id == "cpi_yoy":
                rows.append([s.panel, s.id, x, y])
    # the table keeps the full-resolution g_365 points
    rows = [r for r in rows if r[1] != "g_365"] + [["inflation", "g_365", x, y] for x, y in g_pts]
    columns = ["panel", "series", "date", "value_pct"]
    # the ninety-day LST line has a point at every sample date (weekly, then daily); the page table keeps its
    # month-start points and the latest (the chart JSON stays under its size budget), the CSV download every point
    last_lst = lst90[-1][0] if lst90 else None
    shown = [r for r in rows if r[1] != "lst_90" or r[2].endswith("-01") or r[2] == last_lst]
    chart.csv_table = {"columns": columns, "rows": rows}
    chart.table = {"columns": columns, "rows": shown,
                   "footnote": ("Liquid-staking rows: the trailing ninety-day yield on the first of each month and "
                                "at the latest sample. The CSV download lists it at every sample date.")}
    sample_dates = [r["date"] for r in m.smp.rows if r["date"] >= CHART_START and r["full_day"]]
    chart.coverage = {"first": sample_dates[0].isoformat() if sample_dates else None,
                      "last": sample_dates[-1].isoformat() if sample_dates else None, "n": len(sample_dates),
                      "gaps": gaps(sample_dates)}
    chart.resolve(reg)
    if not lst90 and not g_pts:
        chart.status = "unavailable"
        chart.empty_state = {"reason": "No NEAR RPC samples with liquid-staking prices or a full trailing year",
                             "needs": "rpc/history_samples.csv from the NEAR archival JSON-RPC",
                             "fix_command": FIX}
        return chart
    if chart.coverage["gaps"] or not (lst90 and formula and effr_pts and g_pts and cpi_pts):
        chart.status = "partial"
        chart.status_banner = "Some samples are missing (see coverage); lines break where a window lacks a sample."
    return chart


def yield_sensitivity(reg: Registry, m: StakingModel) -> Chart:
    """c10: staking yield at today's issuance ceiling and three staked shares, against the FOMC median projection
    (the chart tripwire T6 names)."""
    grid = _cells(reg)
    _, cfiles = staker_fraction()
    md, lr = load.fred("FEDTARMD"), load.fred("FEDTARMDLR")
    files = sorted(set(m.files + m.dff_files + cfiles + md.files + lr.files))
    chart = Chart(
        id="c10", section="mind", priority="supporting", claims=[], title="", sentence="", form="table-heatmap",
        sources=[RPC_SOURCE + ": validators, EXPERIMENTAL_protocol_config, LST prices",
                 fred_source("FEDTARMD", "FEDTARMDLR")],
        queries=[validators_query(m.smp)] + block_queries(m.smp)[1:] + lst_queries(m.smp) +
        [fred_query("FEDTARMD"), fred_query("FEDTARMDLR")],
        table={"columns": [], "rows": []}, code_ref="pipeline/models/staking.py::yield_sensitivity",
        formula_ids=["F-c10-cell", "F-haircut", "F-lst", "F-formula-yield"],
        eyebrow="T6 · staking yield by staked share",
        x={"type": "category"}, y={"unit": "%", "diverging_center": val(reg, "sep_median")}, as_of=as_of(files))
    chart.title_template = pick(reg, [
        "If {s_hypo} of NEAR were staked, today's issuance ceiling would pay liquid stakers about {y_s60}, "
        "against the Fed's median projection of {sep_median} for {sep_year}.",
        "Staking yield at today's issuance ceiling, by staked share.",
    ])
    chart.sentence_template = ("Each cell is the yield liquid stakers would get at today's ceiling and that staked "
                               "share, read against the Fed's median projected policy rate.")
    chart.notes_templates = pick_notes(reg, [
        "The protocol formula's yield is scaled by what liquid stakers actually received relative to the formula "
        "last year (factor {lst_haircut}).",
        "The Fed's longer-run median is {sep_longer_run}.",
        "Staked share uses validator stake, which lags the sampled block by about two epochs.",
    ])
    sep = val(reg, "sep_median")
    rows = []
    for (rk, ck), c in grid.items():
        rows.append([c["row"], r4(c["i"]), c["col"], r4(c["s"]), r4(c["value"]),
                     r4(c["value"] - sep) if (c["value"] is not None and sep is not None) else None])
    chart.table = {"columns": ["ceiling", "ceiling_pct", "staked_share", "staked_share_pct", "yield_pct",
                               "vs_fomc_median_pp"], "rows": rows}
    # Drawn grid: one cell per staked share at today's ceiling. Labels stay words; each header's value travels as
    # data (x.cat_values / x.series_values, the registry values of the same keys) and the page formats it next to
    # the label, so no figure is baked into a label string.
    ceilings = (("i_max_now", "On chain now"),)
    shares = (("s_now", "near"), ("s_max", "context"), ("s_hypo", "context"))
    for ck, entity in shares:
        chart.series.append(Series(ck, grid[("i_max_now", ck)]["col"], entity,
                                   [[rlabel, r4(grid[(rk, ck)]["value"])] for rk, rlabel in ceilings],
                                   role="emphasis"))
    chart.x = {"type": "category", "values_unit": "%",
               "cat_values": {rlabel: r4(grid[(rk, "s_now")]["i"]) for rk, rlabel in ceilings},
               "series_values": {ck: r4(grid[("i_max_now", ck)]["s"]) for ck, _ in shares}}
    chart.reference_lines = [{"y": sep, "label": "FOMC median projection"}] if sep is not None else []
    chart.resolve(reg)
    if all(c["value"] is None for c in grid.values()):
        chart.status = "unavailable"
        missing = [k for k in ("lst_haircut", "i_max_now", "s_now", "s_max") if val(reg, k) is None]
        chart.empty_state = {"reason": "Not computed: " + ", ".join(missing) + " is null",
                             "needs": "a year of RPC samples with liquid-staking prices and validator stake",
                             "fix_command": FIX}
    elif any(c["value"] is None for c in grid.values()) or sep is None:
        chart.status = "partial"
    return chart


def spread_quarterly(reg: Registry, m: StakingModel) -> Chart:
    """c12: liquid-staking yield minus compounded fed funds, per quarter, over the quarters H4 reads (quarter start
    on or after cut_date)."""
    tar = load.fred("DFEDTARU")
    files = sorted(set(m.files + m.dff_files + tar.files))
    chart = Chart(
        id="c12", section="A2", priority="lead", claims=["H4"], title="", sentence="", form="diverging-bar",
        sources=[RPC_SOURCE + ": meta-pool.near get_st_near_price, linear-protocol.near ft_price",
                 fred_source("DFF", "DFEDTARU")],
        queries=lst_queries(m.smp) + [fred_query("DFF"), fred_query("DFEDTARU")],
        table={"columns": [], "rows": []}, code_ref="pipeline/models/staking.py::spread_quarterly",
        formula_ids=["F-lst", "F-ffcomp", "F-spread-q", "F-corr"], eyebrow="A2 · tests H4",
        x={"type": "category"}, y={"unit": "pp"}, verdicts=["H4"], as_of=as_of(files))
    sd = val(reg, "spread_q_sd_rule")
    chart.title_template = pick(reg, [
        *(["Under the issuance rule in force since {cut_date}, the staking-minus-fed-funds spread varied with a "
           "standard deviation of {spread_q_sd_rule} over {n_q_rule} full quarters."]
          if sd is not None and sd < H4_SD_PP else []),
        "Under the issuance rule in force since {cut_date}, the staking-minus-fed-funds spread has {n_q_rule} full "
        "quarters, with a standard deviation of {spread_q_sd_rule}.",
        "Liquid-staking yield minus compounded fed funds, by quarter, under the issuance rule now in force.",
    ])
    chart.sentence_template = ("Each column is one quarter's liquid-staking yield minus that quarter's compounded fed "
                               "funds return; the gray band is where the two rates would be in balance.")
    chart.notes_templates = pick_notes(reg, [
        "The in-balance band is within {band_pp} of zero.",
        "The two rates' quarterly levels correlate at {corr_levels_rule}.",
        "The Fed's target ceiling moved {fed_range_q_bp_rule} basis points across these quarters.",
    ])
    cut = val(reg, "cut_date")
    quarters = rule_quarters(m, cut)
    cut_d = date.fromisoformat(cut) if cut else None
    missing = [q for q in m.missing_q if cut_d is not None
               and date(int(q[:4]), 3 * (int(q[5]) - 1) + 1, 1) >= cut_d]
    rows = []
    for w in quarters:
        rows.append([w.label, w.r0["date"].isoformat(), w.r1["date"].isoformat(),
                     round(days_between(w.r0, w.r1), 3), r4(pct(w.y)), r4(pct(w.ff)), r4(w.spread_pp),
                     r4(pct(w.dff_mean)), abs(w.spread_pp) <= BAND_PP, "in stats"])
    for label in missing:
        rows.append([label, None, None, None, None, None, None, None, None, "missing: no sample near an edge"])
    if m.qtd and m.qtd.spread_pp is not None:
        w = m.qtd
        rows.append([w.label, w.r0["date"].isoformat(), w.r1["date"].isoformat(),
                     round(days_between(w.r0, w.r1), 3), r4(pct(w.y)), r4(pct(w.ff)), r4(w.spread_pp),
                     r4(pct(w.dff_mean)), abs(w.spread_pp) <= BAND_PP, "quarter to date, excluded"])
    chart.table = {"columns": ["quarter", "from_sample", "to_sample", "days", "lst_yield_pct", "ffcomp_pct",
                               "spread_pp", "dff_mean_pct", "in_band", "included"], "rows": rows}
    chart.series = [Series("spread_q", "Liquid-staking yield minus fed funds", "near",
                           [[w.label, r4(w.spread_pp)] for w in quarters], role="diverging", unit="pp")]
    if m.qtd and m.qtd.spread_pp is not None:
        chart.series.append(Series("qtd", "Quarter to date (excluded from statistics)", "context",
                                   [[m.qtd.label, r4(m.qtd.spread_pp)]], role="context", unit="pp"))
    chart.bands = [{"axis": "y", "from": -BAND_PP, "to": BAND_PP,
                    "label": "in-balance band the hypothesis implies"}]
    chart.reference_lines = [{"y": 0, "label": "staking yield equals fed funds"}]
    chart.coverage = {"first": quarters[0].label if quarters else None,
                      "last": quarters[-1].label if quarters else None, "n": len(quarters), "gaps": missing}
    chart.resolve(reg)
    if not quarters:
        chart.status = "unavailable"
        chart.empty_state = {"reason": "No complete quarter under the issuance rule now in force has liquid-staking "
                                       "prices at both edges",
                             "needs": "rpc/history_samples.csv LST prices and fred/DFF.csv", "fix_command": FIX}
    elif missing:
        chart.status = "partial"
        chart.status_banner = "Some quarters lack a sample near an edge and are listed in the table, not drawn."
    return chart
