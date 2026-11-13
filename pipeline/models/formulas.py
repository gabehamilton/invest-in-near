"""Shared formulas (the F-* ids on the data page), pure functions over loader output.

Rates come back as fractions (0.0248) unless a name says pct; models convert to percent
when they register. Every function returns None instead of approximating when an input
is missing: no interpolation, no forward-fill.
"""

from __future__ import annotations

import calendar
import math
import statistics
from bisect import bisect_left
from datetime import date, timedelta

DAYS_PER_YEAR = 365.25


# --- dates -----------------------------------------------------------------

def month_start(d: date) -> date:
    return d.replace(day=1)


def add_months(d: date, months: int) -> date:
    """Same day-of-month `months` later, clamped to the month's last day."""
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def quarter_start(d: date) -> date:
    return date(d.year, 3 * ((d.month - 1) // 3) + 1, 1)


def quarter_label(d: date) -> str:
    return f"{d.year}Q{(d.month - 1) // 3 + 1}"


# --- sampling --------------------------------------------------------------

def nearest(points: list[tuple[date, float | None]], target: date, max_days: int = 7):
    """The non-null sample closest to target within max_days (earlier wins ties), else None.

    points must be sorted by date. Returns (date, value) or None.
    """
    lo = bisect_left([d for d, _ in points], target - timedelta(days=max_days))
    best = None
    for d, v in points[lo:]:
        gap = (d - target).days
        if gap > max_days:
            break
        if v is not None and (best is None or abs(gap) < abs((best[0] - target).days)):
            best = (d, v)
    return best


def annualize(ratio: float, days: float) -> float:
    """F-growth: ratio^(365.25/days) - 1 (ratio = end/start)."""
    return ratio ** (DAYS_PER_YEAR / days) - 1


def growth_between(points, start: date, end: date, max_days: int = 7) -> float | None:
    """Annualized growth of a level series between the samples nearest start and end."""
    a, b = nearest(points, start, max_days), nearest(points, end, max_days)
    if not a or not b or b[0] <= a[0] or a[1] <= 0:
        return None
    return annualize(b[1] / a[1], (b[0] - a[0]).days)


def trailing_growth(points, end: date, days: int = 365, max_days: int = 7) -> float | None:
    """g_365-style trailing growth ending at `end`."""
    return growth_between(points, end - timedelta(days=days), end, max_days)


# --- macro -----------------------------------------------------------------

def yoy(monthly: dict[date, float | None], month: date) -> float | None:
    """F-yoy: X_t / X_{t-12} - 1, None if either month is missing."""
    now, then = monthly.get(month), monthly.get(add_months(month, -12))
    if now is None or then is None or then == 0:
        return None
    return now / then - 1


def ffcomp(daily_pct: dict[date, float | None], start: date, end: date) -> float | None:
    """F-ffcomp: compounded fed funds over [start, end), ACT/360, annualized on 365 days.

    daily_pct is DFF (percent, every calendar day). Any missing day -> None.
    """
    days = (end - start).days
    if days <= 0:
        return None
    acc = 1.0
    for k in range(days):
        rate = daily_pct.get(start + timedelta(days=k))
        if rate is None:
            return None
        acc *= 1 + rate / 36000
    return acc ** (365 / days) - 1


def ffcomp_total(daily_pct: dict[date, float | None], start: date, end: date) -> float | None:
    """Compounded fed funds over [start, end), not annualized."""
    days = (end - start).days
    annual = ffcomp(daily_pct, start, end)
    return None if annual is None else (1 + annual) ** (days / 365) - 1


def mean_between(points, start: date, end: date) -> float | None:
    vals = [v for d, v in points if start <= d < end and v is not None]
    return statistics.fmean(vals) if vals else None


# --- market ----------------------------------------------------------------

def log_returns(points) -> list[tuple[date, float]]:
    """Daily log returns between consecutive non-null closes on consecutive calendar days only."""
    out = []
    for (d0, p0), (d1, p1) in zip(points, points[1:]):
        if p0 and p1 and (d1 - d0).days == 1:
            out.append((d1, math.log(p1 / p0)))
    return out


def realized_vol(returns, start: date, end: date, periods_per_year: int = 365, min_obs: int = 20) -> float | None:
    """F-rv: sample stdev of log returns in [start, end) x sqrt(periods_per_year)."""
    vals = [r for d, r in returns if start <= d < end]
    if len(vals) < min_obs:
        return None
    return statistics.stdev(vals) * math.sqrt(periods_per_year)


def max_drawdown(points):
    """F-mdd: min(P / running max - 1). Returns (mdd, peak_date, trough_date) or None."""
    peak_v, peak_d, worst = None, None, None
    for d, p in points:
        if p is None:
            continue
        if peak_v is None or p > peak_v:
            peak_v, peak_d = p, d
        dd = p / peak_v - 1
        if worst is None or dd < worst[0]:
            worst = (dd, peak_d, d)
    return worst


def correlation(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3 or len(xs) != len(ys):
        return None
    try:
        return statistics.correlation(xs, ys)
    except statistics.StatisticsError:
        return None


def pct(x: float | None) -> float | None:
    return None if x is None else x * 100
