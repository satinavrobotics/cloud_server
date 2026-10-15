"""The time axis of the per-map `series` blocks (docs/map_series_2026-10-15.md).

A series is a set of arrays aligned with `t`: one entry per UTC day (`series=day`) of the request window, the first
and last day partial when the window does not start / end at midnight. Counts are 0 on a day without data, ratios,
medians and rates are null.
"""
import datetime
import math
from typing import Any, List, Optional, Sequence

from packages.api import fleet_reads

UTC = datetime.timezone.utc
MAX_BUCKETS = 120
DAY = datetime.timedelta(days=1)


def check_series(value: Optional[str], allowed: Sequence[str]) -> Optional[str]:
    """None (off) or one of `allowed`; anything else is a 422."""
    if not isinstance(value, str):          # unset (or a FastAPI default when a handler is called directly)
        return None
    return fleet_reads.check_choice(value or None, "series", allowed)


def day_axis(start: datetime.datetime, end: datetime.datetime) -> List[datetime.datetime]:
    """The UTC day starts covering [start, end). More than MAX_BUCKETS days is a 422."""
    first = start.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    n = max(1, int(math.ceil((end.astimezone(UTC) - first) / DAY)))
    if n > MAX_BUCKETS:
        raise fleet_reads._invalid(  # pylint: disable=protected-access
            "from", f"series=day covers at most {MAX_BUCKETS} days; narrow from/to")
    return [first + i * DAY for i in range(n)]


def index_of(axis: Sequence[datetime.datetime]) -> dict:
    """UTC date -> bucket index."""
    return {t.date(): i for i, t in enumerate(axis)}


def iso_axis(axis: Sequence[datetime.datetime]) -> List[str]:
    return [t.isoformat() for t in axis]


def zeros(n: int) -> List[int]:
    return [0] * n


def nulls(n: int) -> List[Any]:
    return [None] * n


def ratio(num: float, den: float, digits: int = 4) -> Optional[float]:
    return round(num / den, digits) if den else None
