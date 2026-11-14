"""What holding each of the ten most-traded currencies pays, against NEAR's liquid-staking yield, and where staked NEAR
ranks among those rates.

No claim of its own and no chart: plain comparisons the title shows after the money comparison, read in full by
H3 (staking against fed funds, like for like) and H5 (the spreads). Runs last in build.MODEL_ORDER: it reads the
money model's panel (the ten largest by BIS turnover) and the staking model's y_lst_365. Reads data/raw only, no
network:
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
  F-pay-rank       the plain yearly rates, no adjustment: staked NEAR's liquid-staking yield over the last year
                   (y_lst_365) against each currency's latest policy rate (pol_c).
                   pay_n_ranked = 1 + count of currencies with a pol_c;
                   pay_near_rank = 1 + count of those with pol_c >= y_lst_365 (a tie never ranks NEAR ahead)
"""

from __future__ import annotations

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


def compute(reg: Registry) -> None:
    rows, _meta, bis_files = money.panel()
    y, why_y = upstream(reg, "y_lst_365")
    y_files = reg.get("y_lst_365").source_files if "y_lst_365" in reg else []
    with_rate, below, files = [], [], set(bis_files) | set(y_files)
    pols: dict[str, float] = {}
    for r in rows:
        code = r["currency"]
        c = code.lower()
        v, month, inputs, f, why = rate(code)
        files |= set(f)
        reg.put(f"pol_{c}_latest", v, "%", "F-policy-latest", source_files=f, inputs=inputs, null_reason=why)
        reg.put(f"pol_{c}_month", month, "month", "F-policy-latest", source_files=f, inputs=inputs, null_reason=why)
        if v is not None:
            with_rate.append(code)
            pols[code] = v
            if y is not None and v < y:
                below.append(code)
    none_reason = "no currency in the panel has a policy rate in data/raw/bis or data/raw/fred"
    reg.put("pay_n_with_rate", len(with_rate) if rows else None, "count", "F-pay-count", source_files=sorted(files),
            inputs=with_rate, null_reason=None if rows else "no BIS turnover file: the panel is empty")
    n_below = len(below) if (with_rate and y is not None) else None
    reg.put("pay_n_below", n_below, "count", "F-pay-count", source_files=sorted(files),
            inputs=[f"pol_<c>_latest < y_lst_365: {', '.join(below) or 'none'}"],
            null_reason=None if n_below is not None else (why_y if y is None else none_reason))
    _rank(reg, rows, pols, y, why_y, sorted(files))


def _rank(reg: Registry, rows: list[dict], pols: dict[str, float], y: float | None, why_y: str | None,
          files: list[str]) -> None:
    """F-pay-rank: staked NEAR's place among the plain yearly rates, its liquid-staking yield against each currency's
    latest policy rate, highest first. A currency with no policy rate is not ranked; a tie ranks the currency ahead."""
    ranked = [f"{k} {v:.4f}%" for k, v in sorted(pols.items(), key=lambda kv: (-kv[1], kv[0]))]
    left_out = [r["currency"] for r in rows if r["currency"] not in pols]
    inputs = [f"y_lst_365 {y:.4f}%" if y is not None else "y_lst_365 not computed"] + ranked + (
        [f"not ranked (no policy rate): {', '.join(left_out)}"] if left_out else [])
    ok = y is not None and bool(pols)
    reason = None if ok else (why_y if y is None else "no currency in the panel has a policy rate")
    reg.put("pay_n_ranked", 1 + len(pols) if ok else None, "count", "F-pay-rank", source_files=files,
            inputs=inputs, null_reason=reason)
    rank = 1 + sum(1 for v in pols.values() if v >= y) if ok else None
    reg.put("pay_near_rank", rank, "count", "F-pay-rank", source_files=files, inputs=inputs, null_reason=reason)


def charts(reg: Registry) -> list:
    return []
