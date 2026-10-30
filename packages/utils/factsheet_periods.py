"""robot_factsheet_periods: what a robot's factsheet said, from when (migration 20261030_05, docs/region_stats_2026-10-27.md).

A robot keeps its identity when its speed profile changes. The class (robot_class.py) is the physical body; the speed limits are a
per-run condition. A run is labelled from the period it started in: the latest row with `valid_from <= started_at`
(run_cells.summarise_run). The dispatcher's FleetRecorder writes a row when a received factsheet differs from the robot's latest
row (source 'factsheet'); scripts/seed_factsheet_periods.py writes known history (source 'seed').

Pure helpers plus the SQL, shared by the writer, the run labelling and the seed script. A figure the robot has not sent (-1, any
negative) is stored NULL. The queries take a psycopg cursor (sync or async: the caller awaits).
"""
import datetime
import math
from typing import Any, Dict, Optional, Tuple

TABLE = "robot_factsheet_periods"
# The compared / stored figures, in column order after (robot_name, valid_from).
FIELDS: Tuple[str, ...] = ("agv_class", "speed_max", "angular_speed_max", "acceleration_max", "length", "width")
NUMERIC: Tuple[str, ...] = FIELDS[1:]
# factsheet message field (physicalParameters) per column
MESSAGE_KEYS = {"speed_max": "speedMax", "angular_speed_max": "angularSpeedMax", "acceleration_max": "accelerationMax",
                "length": "length", "width": "width"}
FUTURE_TOLERANCE_S = 60.0

COLUMNS = ("robot_name", "valid_from") + FIELDS + ("source",)
INSERT_SQL = (f"INSERT INTO {TABLE} ({', '.join(COLUMNS)}) VALUES ({', '.join(['%s'] * len(COLUMNS))}) "
              "ON CONFLICT (robot_name, valid_from) DO NOTHING")
LATEST_SQL = (f"SELECT valid_from, {', '.join(FIELDS)} FROM {TABLE} WHERE robot_name = %s "
              "ORDER BY valid_from DESC LIMIT 1")
AT_SQL = (f"SELECT valid_from, {', '.join(FIELDS)} FROM {TABLE} WHERE robot_name = %s AND valid_from <= %s "
          "ORDER BY valid_from DESC LIMIT 1")


def figure(value: Any) -> Optional[float]:
    """The value as a float, None when unknown (missing, negative, not finite)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) and v >= 0 else None


def from_message(message: Any, previous: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The period values a VDA5050 factsheet gives, by the dispatcher's rules (server._on_client_factsheet): agv_class as sent, a
    figure below its smallest valid value (robot_object.FACTSHEET_PHYSICAL_FIELDS) or missing keeps the `previous` period's (None
    without one)."""
    from cloud_common.objects import robot as robot_object
    minimums = {field: minimum for field, _key, minimum in robot_object.FACTSHEET_PHYSICAL_FIELDS}
    physical = getattr(message, "physicalParameters", None)
    out: Dict[str, Any] = {}
    agv = str(getattr(getattr(message, "typeSpecification", None), "agvClass", None) or "").strip()
    out["agv_class"] = agv or None
    for field in NUMERIC:
        value = getattr(physical, MESSAGE_KEYS[field], None)
        if value is not None and figure(value) is not None and value >= minimums.get(field, 0.0):
            out[field] = float(value)
        else:
            out[field] = None if previous is None else previous.get(field)
    return out


def differs(a: Optional[Dict[str, Any]], b: Dict[str, Any]) -> bool:
    """True when `b` is not the same set of figures as `a` (None: no period yet, so it differs)."""
    if a is None:
        return True
    if (a.get("agv_class") or None) != (b.get("agv_class") or None):
        return True
    for field in NUMERIC:
        x, y = a.get(field), b.get(field)
        if (x is None) != (y is None) or (x is not None and abs(float(x) - float(y)) > 1e-9):
            return True
    return False


def valid_from(header_ts: datetime.datetime, now: datetime.datetime, latest: Optional[datetime.datetime]) -> datetime.datetime:
    """The header timestamp, unless it is in the future (+60 s) or not after the latest period (then the received time)."""
    if header_ts > now + datetime.timedelta(seconds=FUTURE_TOLERANCE_S):
        return now
    if latest is not None and header_ts <= latest:
        return now
    return header_ts


def row_dict(row: Any) -> Dict[str, Any]:
    """A LATEST_SQL / AT_SQL row as a dict (valid_from plus the FIELDS)."""
    out = dict(zip(("valid_from",) + FIELDS, row))
    for field in NUMERIC:
        out[field] = None if out[field] is None else float(out[field])
    return out


async def period_at(cursor: Any, robot_name: str, at: Optional[datetime.datetime]) -> Optional[Dict[str, Any]]:
    """The period in force at `at` (latest valid_from <= at) of an async cursor, or None (no period yet, or `at` unknown)."""
    if at is None:
        return None
    await cursor.execute(AT_SQL, (robot_name, at))
    row = await cursor.fetchone()
    return None if row is None else row_dict(row)
