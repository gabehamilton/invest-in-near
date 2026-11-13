"""What holding each of the ten most-traded currencies pays, against NEAR's liquid-staking yield, and the two put
together: what one year does to a holder's share of the total.

No claim of its own and no chart: plain comparisons the title shows after the money comparison, read in full by
H3 (staking against fed funds, like for like) and H5 (the spreads). Runs last in build.MODEL_ORDER: it reads the
money model's panel (the ten largest by BIS turnover) and money growth (mon_<c>_gm_latest), and the supply and
staking models' g_365 and y_lst_365. Reads data/raw only, no network:
  bis/policy_rates_monthly.csv         central bank policy rates (BIS WS_CBPOL), monthly, end of period, % a year
  bis/policy_rates_monthly_cn_hk.csv   the same series for China (the one-year Loan Prime Rate, BIS's policy-rate
                                       series for China) and Hong Kong (the HKMA base rate, set from the US rate
                                       under the peg)
  fred/DFF.csv                         the effective federal funds rate, daily: the US dollar's rate
A currency whose area has no series in those files has no rate. Nothing is borrowed from another currency, carried
over from an older survey or guessed; the null reason names the files and the currency's exchange-rate regime.

Formulas (percent in, percent out):
  F-policy-latest  pol_c = the latest end-of-month policy rate in BIS WS_CBPOL for the currency's area
                   (USD: the latest daily effective federal funds rate, FRED DFF)
  F-pay-count      pay_n_with_rate = count of the panel with a pol_c;
                   pay_n_below = count of those with pol_c < y_lst_365 (strictly below NEAR's staking yield)
  F-hold-net       one year's change in a holder's share of the total, exactly:
                   hold_near_net = (1 + y_lst_365/100) / (1 + g_365/100) - 1, a staker's share of NEAR's supply;
                   hold_c_net = (1 + pol_c/100) / (1 + mon_c_gm_latest/100) - 1, the share of the currency's money
                   stock held by cash that earns the policy rate (in percent)
  F-hold-rank      hold_n_ranked = 1 + count of currencies with a hold_c_net (a currency needs both figures);
                   hold_near_rank = 1 + count of those with hold_c_net > hold_near_net
"""

from __future__ import annotations

import re

from pipeline.models import load, money
from pipeline.models.macro import iso, upstream, ym
from pipeline.numbers import Registry

# the comparison economies' file first, then the flow that adds China and Hong Kong (kept apart, never merged)
BIS_POLICY = ("policy_rates_monthly", "policy_rates_monthly_cn_hk")
FED_FUNDS = "DFF"
# BIS reference area of each currency's policy rate; the euro is the euro area (XM). USD reads FRED instead.
AREAS = {"EUR": "XM", "JPY": "JP", "GBP": "GB", "CNY": "CN", "CHF": "CH", "AUD": "AU", "CAD": "CA", "HKD": "HK",
         "SGD": "SG", "INR": "IN", "KRW": "KR", "MXN": "MX", "NZD": "NZ", "SEK": "SE", "NOK": "NO"}
# what the BIS series is, where it is not simply "the policy rate" (from the files' COMPILATION attributes)
DATED = re.compile(r"\d{4}-\d{2}-\d{2}")
INSTRUMENT = {"CN": "one-year Loan Prime Rate", "HK": "HKMA base rate, set from the US rate under the peg"}


def _latest(points) -> tuple | None:
    return max(((d, v) for d, v in points if v is not None), default=None)


def rate(code: str) -> tuple[float | None, str | None, list[str], list[str], str | None]:
    """(percent, month, inputs, files, null reason) for one currency's latest policy rate (F-policy-latest)."""
    if code == "USD":
        s = load.fred(FED_FUNDS)
        last = _latest(s.points)
        if last is None:
            return None, None, [], s.files, "no effective federal funds rate in data/raw/fred/DFF.csv"
        return last[1], ym(last[0]), [f"FRED {FED_FUNDS} {iso(last[0])}"], s.files, None
    area = AREAS.get(code)
    regime = money.REGIMES.get(code, money.FLOATING)
    if area is None:
        return None, None, [], [], f"{code} has no BIS reference area mapped for its policy rate"
    files: list[str] = []
    for kind in BIS_POLICY:
        s = load.bis(kind, area)
        last = _latest(s.points)
        if last is not None:
            what = f" ({INSTRUMENT[area]})" if area in INSTRUMENT else ""
            return last[1], ym(last[0]), [f"BIS WS_CBPOL {area} {ym(last[0])}{what}"], s.files, None
        files += s.files
    named = " or ".join(f"data/raw/bis/{k}.csv" for k in BIS_POLICY)
    return None, None, [], files, f"no policy-rate series for area {area} in {named}; exchange-rate regime: {regime}"


def _net(r: float, g: float) -> float:
    """F-hold-net, percent: the yearly change in a holder's share when holdings grow r% and the total g%."""
    return ((1.0 + r / 100.0) / (1.0 + g / 100.0) - 1.0) * 100.0


def _files(reg: Registry, *keys: str) -> set[str]:
    return {f for k in keys if k in reg for f in reg.get(k).source_files}


def compute(reg: Registry) -> None:
    rows, _meta, bis_files = money.panel()
    y, why_y = upstream(reg, "y_lst_365")
    y_files = reg.get("y_lst_365").source_files if "y_lst_365" in reg else []
    with_rate, below, files = [], [], set(bis_files) | set(y_files)
    pols: dict[str, tuple[float, str | None, list[str], list[str]]] = {}
    for r in rows:
        code = r["currency"]
        c = code.lower()
        v, month, inputs, f, why = rate(code)
        files |= set(f)
        reg.put(f"pol_{c}_latest", v, "%", "F-policy-latest", source_files=f, inputs=inputs, null_reason=why)
        reg.put(f"pol_{c}_month", month, "month", "F-policy-latest", source_files=f, inputs=inputs, null_reason=why)
        if v is not None:
            with_rate.append(code)
            pols[code] = (v, month, inputs, f)
            if y is not None and v < y:
                below.append(code)
    none_reason = "no currency in the panel has a policy rate in data/raw/bis or data/raw/fred"
    reg.put("pay_n_with_rate", len(with_rate) if rows else None, "count", "F-pay-count", source_files=sorted(files),
            inputs=with_rate, null_reason=None if rows else "no BIS turnover file: the panel is empty")
    n_below = len(below) if (with_rate and y is not None) else None
    reg.put("pay_n_below", n_below, "count", "F-pay-count", source_files=sorted(files),
            inputs=[f"pol_<c>_latest < y_lst_365: {', '.join(below) or 'none'}"],
            null_reason=None if n_below is not None else (why_y if y is None else none_reason))
    _hold(reg, rows, pols, y, why_y)


def _hold(reg: Registry, rows: list[dict], pols: dict, y: float | None, why_y: str | None) -> None:
    """The rate figure and the money figure put together, one per holder (F-hold-net), and NEAR's place (F-hold-rank).
    Each input is named with the month (or the window) it is from."""
    g, why_g = upstream(reg, "g_365")
    near_files = sorted(_files(reg, "y_lst_365", "g_365"))
    window = lambda k: "; ".join(i for i in reg.get(k).inputs if DATED.search(i)) if k in reg else ""   # noqa: E731
    near = _net(y, g) if (y is not None and g is not None) else None
    reg.put("hold_near_net", near, "%", "F-hold-net", source_files=near_files,
            inputs=[f"y_lst_365 {y}% ({window('y_lst_365')})", f"g_365 {g}% ({window('g_365')})"]
            if near is not None else [], null_reason=None if near is not None else (why_y or why_g))
    nets: dict[str, float] = {}
    for r in rows:
        code = r["currency"]
        c = code.lower()
        m, why_m = upstream(reg, f"mon_{c}_gm_latest")
        month = reg.get(f"mon_{c}_gm_month").value if f"mon_{c}_gm_month" in reg else None
        files = sorted(_files(reg, f"pol_{c}_latest", f"mon_{c}_gm_latest"))
        if code not in pols or m is None:
            why = (f"no policy rate: {reg.get(f'pol_{c}_latest').null_reason}" if code not in pols
                   else f"no money growth: {why_m}")
            reg.put(f"hold_{c}_net", None, "%", "F-hold-net", source_files=files, null_reason=why)
            continue
        v, pmonth, pin, _f = pols[code]
        nets[code] = _net(v, m)
        reg.put(f"hold_{c}_net", nets[code], "%", "F-hold-net", source_files=files,
                inputs=[f"pol_{c}_latest {v}% ({pmonth}; {'; '.join(pin)})",
                        f"mon_{c}_gm_latest {m}% ({month}; {'; '.join(reg.get(f'mon_{c}_gm_latest').inputs)})"])
    files = sorted(set(near_files) | _files(reg, *(f"hold_{r['currency'].lower()}_net" for r in rows)))
    ranked = [f"{k} {v:.4f}%" for k, v in sorted(nets.items(), key=lambda kv: (-kv[1], kv[0]))]
    left_out = [r["currency"] for r in rows if r["currency"] not in nets]
    inputs = [f"hold_near_net {near:.4f}%" if near is not None else "hold_near_net not computed"] + ranked + (
        [f"not ranked (no policy rate or no money growth): {', '.join(left_out)}"] if left_out else [])
    ok = near is not None and bool(nets)
    reason = None if ok else ("hold_near_net is not computed" if near is None else "no currency has both figures")
    reg.put("hold_n_ranked", 1 + len(nets) if ok else None, "count", "F-hold-rank", source_files=files,
            inputs=inputs, null_reason=reason)
    rank = 1 + sum(1 for v in nets.values() if v > near) if ok else None
    reg.put("hold_near_rank", rank, "count", "F-hold-rank", source_files=files, inputs=inputs, null_reason=reason)


def charts(reg: Registry) -> list:
    return []
