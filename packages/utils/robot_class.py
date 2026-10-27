"""The robot class a statistic is pooled by (docs/region_stats_2026-10-27.md 4.3, docs/planner_region_stats_2026-10-27.md 10).

There is no robot type id; the class is read off the robot's VDA5050 factsheet (RobotTypeIdentifierV1: `agv_class`,
`length`, `width`). Robots whose factsheets agree on these figures drive alike, so the cell summaries (run_cells) and the
region statistics (region_stats) are keyed by this string, and the planner calls the SAME function on the planning
robot's factsheet to find its numbers. Pure: no I/O, importable without the API; it takes the factsheet object, a dict
with the same field names, or None.

    robot_class(fs) = "<agv_class or '-'>|<length:.2f>x<width:.2f>"

`speed_max` is NOT part of the class (decision 2): a speed change must not split the history. The statistics
normalise pace by each run's own `speed_max` instead, which is stored with the run (`speed_max`) together with how far
to trust it (`speed_basis`):

* "factsheet": a real speed_max, usable to normalise;
* "placeholder": speed_max equal to ROBOT_PLACEHOLDER_SPEED_MAX with no footprint at all (length and width unknown): the
  default a robot reports before anyone configured it (the robots whose factsheet has length = width = -1 and
  speed_max = 1.0 on the live system), so it says nothing about how fast the robot can drive. Its runs are kept but do
  not enter the normalised sums, and the region term is off for such a robot (decision 5);
* "unknown": no usable speed_max (missing, negative, zero, no factsheet).

A figure the robot has not sent (-1, any negative, None, NaN) is written `?`. A robot without a factsheet, or one that
sent none of agv_class, length and width, has class `''` (unknown).
"""
import math
from typing import Any, Optional

from packages import config

UNKNOWN = ""
SPEED_FACTSHEET, SPEED_PLACEHOLDER, SPEED_UNKNOWN = "factsheet", "placeholder", "unknown"


def _field(factsheet: Any, name: str) -> Any:
    if isinstance(factsheet, dict):
        return factsheet.get(name)
    return getattr(factsheet, name, None)


def _figure(value: Any) -> Optional[float]:
    """The value as a float, None when it is unknown (-1 / negative / missing / not finite)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) and v >= 0 else None


def _fmt(value: Optional[float]) -> str:
    return "?" if value is None else f"{value:.2f}"


def speed_max_of(factsheet: Any) -> Optional[float]:
    """The factsheet's speed_max in m/s, None when unknown."""
    return None if factsheet is None else _figure(_field(factsheet, "speed_max"))


def speed_basis(factsheet: Any) -> str:
    """How far the factsheet's speed_max can be trusted (see the module docstring)."""
    speed = speed_max_of(factsheet)
    if speed is None or speed <= 0:
        return SPEED_UNKNOWN
    if (_figure(_field(factsheet, "length")) is None and _figure(_field(factsheet, "width")) is None
            and abs(speed - config.ROBOT_PLACEHOLDER_SPEED_MAX) < 1e-9):
        return SPEED_PLACEHOLDER
    return SPEED_FACTSHEET


def robot_class(factsheet: Any) -> str:
    if factsheet is None:
        return UNKNOWN
    agv = str(_field(factsheet, "agv_class") or "").strip()
    length, width = (_figure(_field(factsheet, n)) for n in ("length", "width"))
    if not agv and length is None and width is None:
        return UNKNOWN
    return f"{agv or '-'}|{_fmt(length)}x{_fmt(width)}"
