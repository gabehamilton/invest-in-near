"""Group B, derivatives: is there a NEAR volatility market to sell into? (chart c15; claim H8).

Formulas:
  F-venues        counts from market/options_venues.json; has_near_options null = the venue could not be queried
                  (geoblock or error): counted in venues_unchecked and labelled "not checked: HTTP <code>", never "no".
  F-vrp           context only (BTC/ETH, not NEAR): DVOL(t) - forward 30-day realized vol, where the forward RV is
                  the sample stdev of the 30 daily log returns dated t+1..t+30 (Binance closes) x sqrt(365); the
                  mean over every day where both exist.
"""

from __future__ import annotations

import math
import statistics
from datetime import date, timedelta

from pipeline.chartspec import Chart, Query
from pipeline.models import formulas as F
from pipeline.models import load
from pipeline.models.macro import coverage, derived_query, empty_state, files_as_of, iso, notes_for, ready
from pipeline.models.market import binance_prices, klines_query
from pipeline.numbers import Registry

VRP_DAYS = 30


def venues() -> tuple[dict | None, list[str]]:
    data, files = load.json_file("market", "options_venues")
    return (data if isinstance(data, dict) else None), files


def vrp(symbol: str, dvol_name: str) -> tuple[dict | None, list[str]]:
    """Mean of DVOL(t) - forward 30-day RV(t) over the overlap; None when no day has both."""
    dvol = load.csv_series("market", dvol_name, "close")
    prices = binance_prices(symbol)
    files = dvol.files + prices.files
    rets = dict(F.log_returns(prices.points))
    diffs, ivs, rvs, days = [], [], [], []
    for t, iv in dvol.non_null():
        fwd = [rets.get(t + timedelta(days=k)) for k in range(1, VRP_DAYS + 1)]
        if any(r is None for r in fwd):
            continue
        rv = statistics.stdev(fwd) * math.sqrt(365) * 100
        diffs.append(iv - rv)
        ivs.append(iv)
        rvs.append(rv)
        days.append(t)
    if not diffs:
        return None, files
    return {"mean": statistics.fmean(diffs), "n": len(diffs), "first": days[0], "last": days[-1],
            "iv": statistics.fmean(ivs), "rv": statistics.fmean(rvs),
            "share_pos": 100.0 * sum(1 for d in diffs if d > 0) / len(diffs)}, files


def compute(reg: Registry) -> None:
    data, vfiles = venues()
    if data is None:
        reason = "market/options_venues.json is missing"
        for key in ("n_venues_with_near_options", "n_option_venues_checked", "venues_unchecked"):
            reg.put(key, None, "count", "F-venues", null_reason=reason)
    else:
        vs = data.get("venues") or []
        checked = [v for v in vs if v.get("has_near_options") is not None]
        unchecked = [v for v in vs if v.get("has_near_options") is None]
        listed = [v for v in checked if v.get("has_near_options") is True]
        stamp = f"checked_at {data.get('checked_at')}"
        reg.put("n_venues_with_near_options", len(listed), "count", "F-venues", source_files=vfiles,
                inputs=[stamp] + [f"{v['venue']}: lists NEAR options" for v in listed])
        reg.put("n_option_venues_checked", len(checked), "count", "F-venues", source_files=vfiles,
                inputs=[stamp] + [f"{v['venue']}: HTTP {v.get('http_status')}" for v in checked])
        reg.put("venues_unchecked", len(unchecked), "count", "F-venues", source_files=vfiles,
                inputs=[stamp] + [f"{v['venue']}: not checked: HTTP {v.get('http_status')}" for v in unchecked])

    for key, symbol, dvol_name in (("vrp_btc", "BTCUSDT", "dvol_deribit_btc_daily"),
                                   ("vrp_eth", "ETHUSDT", "dvol_deribit_eth_daily")):
        res, files = vrp(symbol, dvol_name)
        if res:
            reg.put(key, res["mean"], "pp", "F-vrp", source_files=files,
                    inputs=[f"{res['n']} days {iso(res['first'])}..{iso(res['last'])}",
                            f"mean DVOL {res['iv']:.2f}, mean forward 30-day RV {res['rv']:.2f}",
                            f"DVOL above forward RV on {res['share_pos']:.1f}% of days"])
        else:
            reg.put(key, None, "pp", "F-vrp", source_files=files,
                    null_reason=f"no day with both Deribit DVOL and 30 forward Binance {symbol} returns")

    # H8's supported/refuted rules read a NEAR variance risk premium. No checked venue lists NEAR options, so
    # there is no NEAR implied volatility to compute it from: register the keys as null with that reason, so the
    # rule reads registered keys (and says why they are empty) rather than names no model writes.
    n_near = reg.value("n_venues_with_near_options") if "n_venues_with_near_options" in reg else None
    why_vrp = ("no checked venue lists NEAR options, so there is no NEAR implied volatility to compare with "
               "realized volatility" if n_near == 0 else
               "NEAR options listings were not checked" if n_near is None else
               "a venue lists NEAR options, but no NEAR implied-volatility history is pulled yet")
    for key, unit in (("vrp_near_mean", "pp"), ("vrp_near_t", "ratio"), ("vrp_near_days", "days")):
        reg.put(key, None, unit, "F-vrp", source_files=vfiles, null_reason=why_vrp)


def charts(reg: Registry) -> list[Chart]:
    return [vol_market(reg)]


def _checked_on(data: dict | None) -> list[date]:
    try:
        return [date.fromisoformat(str((data or {}).get("checked_at"))[:10])]
    except ValueError:
        return []


def vol_market(reg: Registry) -> Chart:
    """c15 vol-market: which options venues list NEAR options, with BTC/ETH implied-minus-realized as context."""
    data, vfiles = venues()
    venue_rows = []
    for v in (data or {}).get("venues") or []:
        has = v.get("has_near_options")
        label = "yes" if has is True else "no" if has is False else f"not checked: HTTP {v.get('http_status')}"
        venue_rows.append([v.get("venue"), " ".join(v.get("option_underlyings") or []) or "n/a", label,
                           f"{(data or {}).get('checked_at')} (HTTP {v.get('http_status')})", v.get("endpoint")])
    venue_table = {"columns": ["venue", "option_underlyings", "near_options", "checked_at_http_status", "endpoint"],
                   "rows": venue_rows}
    vmeta = load.meta("market", "options_venues")
    queries = [Query("http", v.get("endpoint", ""), f"GET {v.get('endpoint', '')}", "data/raw/market/options_venues.json")
               for v in (data or {}).get("venues") or []]
    if not queries and vmeta.get("source_url"):
        queries.append(Query("http", vmeta["source_url"], f"GET {vmeta['source_url']}",
                             "data/raw/market/options_venues.json"))
    dvol_url = load.meta("market", "dvol_deribit_btc_daily").get(
        "source_url", "https://www.deribit.com/api/v2/public/get_volatility_index_data")
    queries += [
        Query("http", dvol_url, f"GET {dvol_url}?currency=BTC|ETH&resolution=1D&start_timestamp=0",
              "data/raw/market/dvol_deribit_btc_daily.csv"),
        klines_query("BTCUSDT"), klines_query("ETHUSDT"),
        derived_query("VRP = DVOL(t) - stdev(ln returns t+1..t+30) x sqrt(365), averaged"),
    ]
    files = vfiles + [f for k in ("vrp_btc", "vrp_eth") if k in reg for f in reg.get(k).source_files]
    chart = Chart(
        id="c15", section="A5", priority="lead", claims=["H8"], verdicts=["H8"], title="", sentence="",
        form="table",
        sources=["Deribit, OKX, Aevo, Bybit and Binance public instrument endpoints (options listings)",
                 f"Deribit DVOL ({dvol_url}) and Binance BTCUSDT/ETHUSDT daily klines (context note)"],
        queries=queries, table=venue_table,
        code_ref="pipeline/models/derivatives.py::vol_market", formula_ids=["F-venues", "F-vrp"],
        x={"type": "category"}, y={"unit": "text"},
        panels=[{"id": "venues", "label": "Which venues list options", "series_ids": [], "table": venue_table}],
        coverage=coverage(_checked_on(data)),
        as_of=files_as_of(files),
    )
    if data is None:
        chart.status = "unavailable"
        chart.title = "Is there a NEAR volatility market to sell into?"
        chart.sentence = "Not computed: the options venue check was not pulled."
        chart.empty_state = empty_state("No options venue check.", "market/options_venues.json.",
                                        "uv run python -m pipeline.pull --only derivatives && make charts")
        return chart
    chart.sentence_template = ("The table lists which derivatives venues offer options, on what, and whether NEAR is "
                               "one.")
    chart.notes_templates = notes_for(reg, [
        "The unchecked venues refused requests from a US network."
        if ready(reg, "venues_unchecked") and reg.value("venues_unchecked") else "",
        "Where options exist, one-month implied minus realized volatility averaged {vrp_btc} for bitcoin and "
        "{vrp_eth} for ether (a proxy, not NEAR).",
    ])
    if ready(reg, "n_venues_with_near_options", "n_option_venues_checked") and reg.value("n_option_venues_checked"):
        if reg.value("n_venues_with_near_options") == 0:
            # say only what was checked: venues that refused the request are not counted as "no"
            if ready(reg, "venues_unchecked") and reg.value("venues_unchecked"):
                chart.title_template = ("{n_venues_with_near_options} of the {n_option_venues_checked} options venues "
                                        "I could check list NEAR options; {venues_unchecked} more could not be "
                                        "checked.")
            else:
                chart.title_template = ("{n_venues_with_near_options} of the {n_option_venues_checked} options venues "
                                        "checked list NEAR options, so there is no NEAR volatility to sell there.")
        else:
            chart.title_template = ("{n_venues_with_near_options} of {n_option_venues_checked} checked venues list "
                                    "NEAR options.")
    else:
        chart.status = "partial"
        chart.title = "Whether NEAR volatility can be sold: no options venue was checked"
        chart.status_banner = ("The options venue check is missing or every venue was unreachable, so NEAR options "
                               "listings are not computed.")
    return chart.resolve(reg)
