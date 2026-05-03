"""Analytics: window parsing, top-N, and change-over-window stats.

Window parsing: '7d', '30d', '90d', '1m', '3m', '1y'. We treat 1 month as
30 days for the timedelta and as 1.0 for the per-month divisor — close
enough for a price-history dashboard.

/change semantics (per the user's spec):
  - pct_change_window: total % change between first and last sample within
    the window. For a 1-month window this is also the monthly %.
  - dollar_per_month:  total $ change / number of months in the window.
"""

import re
from datetime import timedelta

from .log import get_logger

log = get_logger(__name__)

DAYS_PER_MONTH = 30.0
DAYS_PER_YEAR = 365.0

_WINDOW_RE = re.compile(r"^\s*(\d+)\s*([dmy])\s*$", re.IGNORECASE)


class WindowParseError(ValueError):
    """Raised when a window string can't be parsed."""


def parse_window(window: str) -> tuple[timedelta, float]:
    """Parse '7d', '30d', '1m', '3m', '1y' into (timedelta, months_in_window).

    The months_in_window value is used as the denominator for the
    dollar-per-month figure in /change.
    """
    if not window:
        raise WindowParseError("window is required")
    match = _WINDOW_RE.match(window)
    if not match:
        raise WindowParseError(
            f"unrecognized window {window!r}; expected like '7d', '1m', '1y'"
        )
    n, unit = int(match.group(1)), match.group(2).lower()
    if n <= 0:
        raise WindowParseError(f"window must be positive, got {window!r}")
    if unit == "d":
        return timedelta(days=n), n / DAYS_PER_MONTH
    if unit == "m":
        return timedelta(days=int(n * DAYS_PER_MONTH)), float(n)
    if unit == "y":
        return timedelta(days=int(n * DAYS_PER_YEAR)), n * 12.0
    # Should be unreachable thanks to the regex.
    raise WindowParseError(f"unrecognized unit {unit!r}")


def top_n(records: list[dict], n: int = 10) -> list[dict]:
    """Sort by price desc and return the top n. Ignores rows with no price."""
    valid = [r for r in records if r.get("price") is not None]
    valid.sort(key=lambda r: float(r["price"]), reverse=True)
    return valid[:n]


def compute_change(history: list[dict], months_in_window: float) -> dict | None:
    """Compute change stats from a sorted-ascending history list.

    Returns None if there aren't at least two snapshots, or if the start price
    is zero (so the % change is undefined).
    """
    if not history or len(history) < 2:
        log.info("compute_change: insufficient samples (%d)", len(history))
        return None
    start = float(history[0]["price"])
    end = float(history[-1]["price"])
    if start == 0:
        log.info("compute_change: start price is 0; pct change undefined")
        return None

    delta = end - start
    pct_total = (delta / start) * 100.0
    if months_in_window > 0:
        dollar_per_month = delta / months_in_window
    else:
        dollar_per_month = delta

    return {
        "start_price": start,
        "end_price": end,
        "delta": delta,
        "pct_change_window": pct_total,
        "dollar_per_month": dollar_per_month,
        "samples": len(history),
        "first_ts": int(history[0]["timestamp"]),
        "last_ts": int(history[-1]["timestamp"]),
    }
