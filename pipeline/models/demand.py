"""NEAR Intents volume from DefiLlama (chart c06; tripwire T5).

Formulas:
  F-sum            sum of daily volume over the last N calendar days ending at the series' latest day; every day in
                   the window must be present (no filling). With no gaps this is "the last N daily points".
  F-ratio-90d-seq  sum of the latest 90 days / sum of the 90 days before them; null unless both windows are fully
                   covered.
  F-monthly        sums by UTC month, complete months only (every day present); the running month is reported as
                   month-to-date in the sentence, never as a short column.
DefiLlama marks its trailing days provisional (meta provisional_dates): values are kept as published and the count
inside each window is recorded in the number's inputs.
Explorer overlay (1Click aggregates) only when intents/explorer_daily.csv exists AND thesis/sources.toml lets the
intents_explorer source publish (publish = false until NEAR Intents grants written permission).
"""

from __future__ import annotations

from datetime import date, timedelta

import tomllib

from pipeline import config
from pipeline.chartspec import Chart, Query, Series
from pipeline.models import formulas as F
from pipeline.models import load
from pipeline.models.macro import coverage, empty_state, files_as_of, iso, notes_for, put_constant, ready, rnd, ym
from pipeline.numbers import Registry

VOLUME, FEES, EXPLORER = "defillama_volume_daily", "defillama_fees_daily", "explorer_daily"
VOLUME_URL = "https://api.llama.fi/summary/dexs/near-intents?excludeTotalDataChartBreakdown=true"
FEES_URL = "https://api.llama.fi/summary/fees/near-intents?excludeTotalDataChartBreakdown=true&dataType=dailyFees"
# key -> (unit, formula id, signed) of the like-for-like volume growth readings (put_intents)
INTENTS_KEYS = {
    "intents_vol_90d_seq_ratio": ("x", "F-ratio-90d-seq", False),
}


def _daily(name: str, col: str) -> tuple[dict[date, float | None], list[str], dict]:
    s = load.csv_series("intents", name, col)
    return dict(s.points), s.files, load.meta("intents", name)


def window_sum(daily: dict[date, float | None], end: date, days: int) -> tuple[float | None, list[date]]:
    """Sum over [end - days + 1, end]; (None, missing days) unless every day has a value."""
    window = [end - timedelta(days=k) for k in range(days - 1, -1, -1)]
    missing = [d for d in window if daily.get(d) is None]
    return (None, missing) if missing else (sum(daily[d] for d in window), [])


def monthly(daily: dict[date, float | None]) -> list[tuple[date, float | None, bool]]:
    """(month, sum, complete) for every month the series touches; incomplete months sum the days present."""
    if not daily:
        return []
    first, last = min(daily), max(daily)
    out = []
    m = F.month_start(first)
    while m <= last:
        nxt = F.add_months(m, 1)
        days = [m + timedelta(days=k) for k in range((nxt - m).days)]
        vals = [daily.get(d) for d in days]
        complete = all(v is not None for v in vals)
        total = sum(v for v in vals if v is not None) if any(v is not None for v in vals) else None
        out.append((m, total, complete))
        m = nxt
    return out


def _provisional(meta: dict, days: list[date]) -> list[str]:
    marked = set(meta.get("provisional_dates") or [])
    return sorted(iso(d) for d in days if iso(d) in marked)


def explorer_state() -> tuple[str, list[dict], list[str]]:
    """('absent' | 'withheld' | 'ok', rows, files) for the optional Explorer aggregates."""
    if not load.exists("intents", EXPLORER):
        return "absent", [], []
    rows, files = load.csv_rows("intents", EXPLORER)
    path = config.ROOT / "thesis" / "sources.toml"
    explorer = (tomllib.loads(path.read_text()) if path.exists() else {}).get("sources", {}).get("intents_explorer", {})
    if explorer.get("publish", True) is False:
        return "withheld", rows, files
    return "ok", rows, files


def _w(w: tuple[date, date]) -> str:
    return f"{w[0].isoformat()}..{w[1].isoformat()}"


def _days(w: tuple[date, date]) -> list[date]:
    return [w[0] + timedelta(days=i) for i in range((w[1] - w[0]).days + 1)]


def put_intents(reg: Registry) -> None:
    """Intents volume growth, like-for-like windows only: the latest ninety days against the ninety before
    (tripwire T5, the c06 title)."""
    s = load.csv_series("intents", VOLUME, "volume_usd")
    vol, files, meta = dict(s.points), s.files, load.meta("intents", VOLUME)
    nn = sorted(d for d, v in vol.items() if v is not None)

    def put(key: str, value, inputs=(), reason: str | None = None) -> None:
        unit, fid, signed = INTENTS_KEYS[key]
        if value is None and not reason:
            reason = "not computed"
        reg.put(key, value, unit, fid, source_files=sorted(set(files)), inputs=list(inputs),
                null_reason=reason if value is None else None, signed=signed)

    if not nn:
        for key in INTENTS_KEYS:
            put(key, None, [], "no DefiLlama NEAR Intents volume series in data/raw/intents")
        return
    last = nn[-1]

    def ratio(key: str, days: int, back: int, label: str) -> None:
        now, miss_now = window_sum(vol, last, days)
        then_end = last - timedelta(days=back)
        then, miss_then = window_sum(vol, then_end, days)
        w_now = (last - timedelta(days=days - 1), last)
        w_then = (then_end - timedelta(days=days - 1), then_end)
        marked = set(meta.get("provisional_dates") or [])
        prov = [d.isoformat() for d in _days(w_now) if d.isoformat() in marked]
        if now is None or then is None or not then:
            miss = miss_now or miss_then
            why = (f"{len(miss)} days missing in {_w(w_now if miss_now else w_then)} (series starts {nn[0]})"
                   if miss else f"zero volume over {_w(w_then)}")
            put(key, None, [], why)
            return
        put(key, now / then, [f"{_w(w_now)} / {_w(w_then)} ({label})"]
            + ([f"includes provisional days {', '.join(prov)} (values as published)"] if prov else []))

    ratio("intents_vol_90d_seq_ratio", 90, 90, "the latest ninety days against the ninety before")


def compute(reg: Registry) -> None:
    put_intents(reg)
    vol, vfiles, vmeta = _daily(VOLUME, "volume_usd")
    fees, ffiles, fmeta = _daily(FEES, "fees_usd")
    put_constant(reg, "intents_confidential_from", "intents_confidential_from", "date")
    nn = sorted(d for d, v in vol.items() if v is not None)
    if not nn:
        reason = "no DefiLlama NEAR Intents volume series in data/raw/intents"
        for key, unit in (("intents_vol_30d", "USD"), ("intents_vol_365d", "USD"),
                          ("intents_series_start", "date"), ("intents_latest_date", "date"),
                          ("intents_vol_mtd", "USD"), ("intents_mtd_through", "date"),
                          ("intents_n_provisional", "count")):
            reg.put(key, None, unit, "F-sum", null_reason=reason)
    else:
        last = nn[-1]
        reg.put("intents_series_start", iso(nn[0]), "date", "F-sum", source_files=vfiles,
                inputs=["first day of the DefiLlama daily volume series"])
        reg.put("intents_latest_date", iso(last), "date", "F-sum", source_files=vfiles,
                inputs=["last day of the DefiLlama daily volume series"])
        for key, days in (("intents_vol_30d", 30), ("intents_vol_365d", 365)):
            total, missing = window_sum(vol, last, days)
            window = [last - timedelta(days=k) for k in range(days)]
            prov = _provisional(vmeta, window)
            reg.put(key, total, "USD", "F-sum", source_files=vfiles,
                    inputs=[f"{iso(last - timedelta(days=days - 1))}..{iso(last)} ({days} daily points)"]
                    + ([f"includes provisional days {', '.join(prov)} (values as published)"] if prov else []),
                    null_reason=None if total is not None else
                    f"{len(missing)} of {days} days missing in the window (series starts {iso(nn[0])})")
        prov_all = list(vmeta.get("provisional_dates") or [])
        reg.put("intents_n_provisional", len(prov_all), "count", "F-sum", source_files=vfiles,
                inputs=[f"DefiLlama trailing days marked provisional in the meta: {', '.join(prov_all) or 'none'}"])
        mtd_month = F.month_start(last)
        if F.add_months(mtd_month, 1) - timedelta(days=1) == last:
            reg.put("intents_vol_mtd", None, "USD", "F-monthly", source_files=vfiles,
                    null_reason="the latest month is complete, so there is no month to date")
            reg.put("intents_mtd_through", None, "date", "F-monthly", source_files=vfiles,
                    null_reason="the latest month is complete, so there is no month to date")
        else:
            total, missing = window_sum(vol, last, (last - mtd_month).days + 1)
            reg.put("intents_vol_mtd", total, "USD", "F-monthly", source_files=vfiles,
                    inputs=[f"{iso(mtd_month)}..{iso(last)}"],
                    null_reason=None if total is not None else f"{len(missing)} days missing this month")
            reg.put("intents_mtd_through", iso(last), "date", "F-monthly", source_files=vfiles,
                    inputs=["last day of the DefiLlama daily volume series"])

    fnn = sorted(d for d, v in fees.items() if v is not None)
    if fnn:
        total, missing = window_sum(fees, fnn[-1], 365)
        prov = _provisional(fmeta, [fnn[-1] - timedelta(days=k) for k in range(365)])
        reg.put("intents_fees_365d", total, "USD", "F-sum", source_files=ffiles,
                inputs=[f"{iso(fnn[-1] - timedelta(days=364))}..{iso(fnn[-1])} (365 daily points)"]
                + ([f"includes provisional days {', '.join(prov)} (values as published)"] if prov else []),
                null_reason=None if total is not None else
                f"{len(missing)} of 365 days missing (fees series starts {iso(fnn[0])})")
    else:
        reg.put("intents_fees_365d", None, "USD", "F-sum",
                null_reason="no DefiLlama NEAR Intents fees series in data/raw/intents")


def charts(reg: Registry) -> list[Chart]:
    return [intents_volume(reg)]


def _queries() -> list[Query]:
    from pipeline.sources import intents as src  # constants only; models never call the network
    vmeta, fmeta = load.meta("intents", VOLUME), load.meta("intents", FEES)
    vurl, furl = vmeta.get("source_url", VOLUME_URL), fmeta.get("source_url", FEES_URL)
    explorer = (f"GET {src.TRANSACTIONS_URL}?numberOfTransactions={src.PAGE_SIZE}&direction=next"
                f"&statuses={','.join(src.STATUSES)}&startTimestampUnix=<UTC day start - 1>"
                f"&endTimestampUnix=<UTC day start + 86400>\nAuthorization: Bearer <redacted partner token>\n"
                f"(one UTC day per walk, cursor lastDepositAddress/lastDepositMemo, >= 5.2 s between requests; "
                f"only per-day aggregates are kept)")
    return [Query("http", vurl, f"GET {vurl}  -> totalDataChart", f"data/raw/intents/{VOLUME}.csv"),
            Query("http", furl, f"GET {furl}  -> totalDataChart", f"data/raw/intents/{FEES}.csv"),
            Query("http", src.TRANSACTIONS_URL, explorer, f"data/raw/intents/{EXPLORER}.csv")]


def _explorer_monthly(rows: list[dict]) -> dict[date, float]:
    """Monthly sums of volume_in_usd_success for months whose every day is present and final."""
    daily: dict[date, float | None] = {}
    for r in rows:
        try:
            d = date.fromisoformat(r["date"])
        except (KeyError, ValueError):
            continue
        v = r.get("volume_in_usd_success")
        ok = str(r.get("final", "")).lower() == "true" and v not in (None, "")
        daily[d] = float(v) if ok else None
    return {m: total for m, total, complete in monthly(daily) if complete and total is not None}


def intents_volume(reg: Registry) -> Chart:
    """c06 intents-volume: monthly NEAR Intents volume (DefiLlama), complete months only."""
    vol, vfiles, _ = _daily(VOLUME, "volume_usd")
    fees, ffiles, _ = _daily(FEES, "fees_usd")
    vm = monthly(vol)
    fm = {m: (t, c) for m, t, c in monthly(fees)}
    state, erows, efiles = explorer_state()
    files = vfiles + ffiles + efiles
    complete = [(m, t) for m, t, c in vm if c]
    series = [Series("intents_month", "NEAR Intents monthly volume (DefiLlama)", "third",
                     [[iso(m), rnd(t, 0)] for m, t in complete], role="emphasis", unit="USD")]
    emonth = _explorer_monthly(erows) if state == "ok" else {}
    if emonth:
        series.append(Series("explorer_month", "Explorer API swaps only (narrower scope)", "context",
                             [[iso(m), rnd(t, 0)] for m, t in sorted(emonth.items())], role="context", unit="USD"))
    rows = []
    for m, t, c in vm:
        ft, fc = fm.get(m, (None, False))
        rows.append([ym(m), rnd(t, 0), c, rnd(ft, 0), fc] + ([rnd(emonth.get(m), 0)] if emonth else []))
    ann = []
    conf = reg.value("intents_confidential_from") if "intents_confidential_from" in reg else None
    if conf and complete and complete[0][0] <= date.fromisoformat(conf) <= max(m for m, _, _ in vm):
        # short label on the chart; the full statement is in the sentence
        ann.append({"x": conf, "label": "One-click swaps confidential", "kind": "data_boundary"})
    chart = Chart(
        id="c06", section="O4", priority="lead", claims=[], verdicts=[], title="", sentence="", form="column",
        sources=[f"DefiLlama NEAR Intents volume and fees ({VOLUME_URL.split('?')[0]}, "
                 f"{FEES_URL.split('?')[0]}); upstream: a Dune table maintained by NEAR"]
        + (["NEAR Intents Explorer API (explorer.near-intents.org/api/v0/transactions), daily aggregates"]
           if emonth else []),
        queries=_queries(),
        table={"columns": ["month", "volume_usd", "volume_month_complete", "fees_usd", "fees_month_complete"]
               + (["explorer_volume_in_usd_success"] if emonth else []), "rows": rows},
        code_ref="pipeline/models/demand.py::monthly", formula_ids=["F-monthly", "F-sum", "F-ratio-90d-seq"],
        x={"type": "time", "unit": "month"}, y={"unit": "USD"}, series=series, annotations=ann,
        coverage=coverage([d for d, v in vol.items() if v is not None]),
        as_of=files_as_of(files),
    )
    if state in ("absent", "withheld"):
        chart.status_banner = ("The NEAR Intents Explorer comparison needs partner access and written permission to "
                               "publish, so this chart rests on DefiLlama alone.")
    if not complete:
        chart.status = "unavailable"
        chart.title = "NEAR Intents swap volume by month"
        chart.sentence = "Not computed: no complete month of DefiLlama NEAR Intents volume yet."
        chart.series = []
        chart.empty_state = empty_state("No complete month of DefiLlama NEAR Intents volume.",
                                        "DefiLlama summary/dexs/near-intents daily volume.",
                                        "uv run python -m pipeline.pull --only defillama && make charts")
        return chart
    # the title compares the latest ninety days with the ninety before (intents_vol_90d_seq_ratio)
    if ready(reg, "intents_vol_30d", "intents_vol_90d_seq_ratio"):
        chart.title_template = ("NEAR Intents settled {intents_vol_30d} in the last thirty days; the last ninety days "
                                "were {intents_vol_90d_seq_ratio} the ninety days before.")
    elif ready(reg, "intents_vol_365d", "intents_series_start"):
        chart.title_template = ("NEAR Intents settled {intents_vol_365d} over the last year (series starts "
                                "{intents_series_start}).")
    elif ready(reg, "intents_vol_30d", "intents_series_start"):
        chart.title_template = ("NEAR Intents settled {intents_vol_30d} in the last thirty days (series starts "
                                "{intents_series_start}).")
    else:
        chart.title = "NEAR Intents swap volume by month"
    chart.sentence_template = ("Each column is one month of swap volume routed through NEAR Intents, as reported "
                               "by DefiLlama.")
    chart.caveat_template = "Volume measures usage of the network, not value that accrues to the token."
    chart.notes_templates = notes_for(reg, [
        "DefiLlama's figures come from a Dune table maintained by NEAR.",
        "The latest {intents_n_provisional} days are provisional and may be revised.",
        "Month to date: {intents_vol_mtd} through {intents_mtd_through}.",
        "Fees over the last year: {intents_fees_365d}.",
        "From {intents_confidential_from}, one-click API swaps settle confidentially on the NEAR Private Shard "
        "(NEAR Intents documentation)." if ann else "",
    ])
    return chart.resolve(reg)
