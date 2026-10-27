"""The robot class a statistic is pooled by (docs/region_stats_2026-10-27.md 4.3).

There is no robot type id; the class is read off the robot's VDA5050 factsheet (RobotTypeIdentifierV1: `agv_class`,
`length`, `width`, `speed_max`). Robots whose factsheets agree on these four figures drive alike, so the cell
summaries (run_cells) and the region statistics (region_stats) are keyed by this string, and the planner will
call the SAME function on the planning robot's factsheet to find its numbers. Pure: no I/O, importable without
the API; it takes the factsheet object, a dict with the same field names, or None.

    robot_class(fs) = "<agv_class or '-'>|<length:.2f>x<width:.2f>|<speed_max:.2f>"

A figure the robot has not sent (-1, any negative, None, NaN) is written `?`. A robot without a factsheet, or
one that sent none of the four, has class `''` (unknown).
"""
import math
from typing import Any, Optional

UNKNOWN = ""


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


def robot_class(factsheet: Any) -> str:
    if factsheet is None:
        return UNKNOWN
    agv = str(_field(factsheet, "agv_class") or "").strip()
    length, width, speed = (_figure(_field(factsheet, n)) for n in ("length", "width", "speed_max"))
    if not agv and length is None and width is None and speed is None:
        return UNKNOWN
    return f"{agv or '-'}|{_fmt(length)}x{_fmt(width)}|{_fmt(speed)}"
