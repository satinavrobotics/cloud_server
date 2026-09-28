"""Map frames for typed maps and mapping sessions (docs/satinav-maps-redesign.md §2-§3, M1).

A `geo` map's frame is UTM grid metres in one zone fixed per map, relative to the map origin
(`geo.origin_e`, `geo.origin_n`). A mapping session records the robot's datum for that run and
`map_T_session` = {tx, ty, yaw}: a robot-frame point p maps to R(yaw) p + (tx, ty) in the map
frame (metres, radians, counter-clockwise).

- Datum in the map's zone and frame 'utm' (real robot with GNSS): a pure translation, the
  datum's UTM minus the map origin; yaw = the datum's bearing (grid-referenced).
- Any other datum ('enu' tangent plane, or a 'utm' datum of another zone/hemisphere): the
  run's origin and +X axis are projected into the map's zone, so yaw includes the grid
  convergence at the datum (about -1.45 deg in Budapest, zone 34) plus the datum's bearing.

Pure functions only (no I/O); the M1 Alembic migration imports them too.
"""

import math
from typing import Any, Dict, Mapping, Optional, Tuple

from cloud_common.objects.map import has_real_datum
from packages.utils import geo

IDENTITY: Dict[str, float] = {"tx": 0.0, "ty": 0.0, "yaw": 0.0}
# Length of the robot-frame +X probe used to measure the session yaw in the map zone. 1 m keeps
# the tangent-plane curvature term (~d / 2R) below 1e-7 rad; UTM round-off is ~1e-9 m.
_YAW_PROBE_M = 1.0


def _get(datum: Any, key: str, default: Any = None) -> Any:
    if isinstance(datum, Mapping):
        value = datum.get(key, default)
    else:
        value = getattr(datum, key, default)
    return default if value is None else value


def robot_datum(datum: Any) -> Optional[Dict[str, Any]]:
    """A robot datum (RobotDatumV1 or its dict) as the session's `datum` JSON; None if it does
    not anchor anything (no lat/lon, or (0, 0))."""
    lat, lon = _get(datum, "latitude"), _get(datum, "longitude")
    if not has_real_datum(lat, lon):
        return None
    return {
        "latitude": float(lat), "longitude": float(lon),
        "bearing_deg": float(_get(datum, "bearing_deg", 0.0)),
        "frame": geo.normalize_frame(_get(datum, "frame")),
        "utm_zone": _get(datum, "utm_zone"), "utm_north": _get(datum, "utm_north"),
        "utm_easting": _get(datum, "utm_easting"), "utm_northing": _get(datum, "utm_northing"),
    }


def map_datum(spec: Any) -> Optional[Dict[str, Any]]:
    """A map spec's legacy datum_* fields in the same shape as robot_datum()."""
    return robot_datum({
        "latitude": _get(spec, "datum_latitude"), "longitude": _get(spec, "datum_longitude"),
        "bearing_deg": _get(spec, "datum_bearing_deg", 0.0), "frame": _get(spec, "datum_frame"),
        "utm_zone": _get(spec, "datum_utm_zone"), "utm_north": _get(spec, "datum_utm_north"),
        "utm_easting": _get(spec, "datum_utm_easting"),
        "utm_northing": _get(spec, "datum_utm_northing"),
    })


def datum_utm(datum: Mapping[str, Any]) -> Tuple[int, bool, float, float]:
    """(zone, north, easting, northing) of a datum's origin in its own natural zone.

    'utm' frame: the zone/hemisphere/easting/northing the robot sent (each defaulting to the
    datum lat/lon's own); 'enu' frame: the datum lat/lon projected in its longitude's zone."""
    lat, lon = float(datum["latitude"]), float(datum["longitude"])
    if geo.normalize_frame(datum.get("frame")) == geo.FRAME_UTM:
        return geo._datum_utm(lat, lon, datum.get("utm_zone"), datum.get("utm_north"),
                              datum.get("utm_easting"), datum.get("utm_northing"))
    zone = geo.utm_zone_from_longitude(lon)
    north = lat >= 0.0
    e, n = geo.latlon_to_utm(lat, lon, zone, north)
    return zone, north, e, n


def geo_from_datum(datum: Mapping[str, Any]) -> Dict[str, Any]:
    """The `geo` block of a geo map whose origin is this datum (doc Q1): the datum's own zone."""
    zone, north, e, n = datum_utm(datum)
    return {"utm_zone": int(zone), "utm_north": bool(north), "origin_e": e, "origin_n": n}


def classify(spec: Mapping[str, Any]) -> Tuple[str, Optional[Dict[str, Any]]]:
    """(type, geo) of a pre-M1 map from its datum_* fields (doc §12): 'geo' with the datum's UTM
    point as origin if it has a real datum, else ('local', None)."""
    datum = map_datum(spec)
    if datum is None:
        return "local", None
    return "geo", geo_from_datum(datum)


def normalize_yaw(yaw: float) -> float:
    """Wrap to (-pi, pi]."""
    wrapped = math.atan2(math.sin(yaw), math.cos(yaw))
    return math.pi if wrapped == -math.pi else wrapped


def session_transform(map_geo: Mapping[str, Any], datum: Mapping[str, Any]) -> Dict[str, float]:
    """map_T_session of a session on a geo map (see the module docstring)."""
    zone, north = int(map_geo["utm_zone"]), bool(map_geo["utm_north"])
    oe, on = float(map_geo["origin_e"]), float(map_geo["origin_n"])
    bearing = float(datum.get("bearing_deg") or 0.0)
    frame = geo.normalize_frame(datum.get("frame"))
    if frame == geo.FRAME_UTM:
        dzone, dnorth, de, dn = datum_utm(datum)
        if dzone == zone and dnorth == north:
            return {"tx": de - oe, "ty": dn - on, "yaw": normalize_yaw(math.radians(bearing))}
    kwargs = dict(datum_lat=float(datum["latitude"]), datum_lon=float(datum["longitude"]),
                  datum_bearing_deg=bearing, frame=frame, utm_zone=datum.get("utm_zone"),
                  utm_north=datum.get("utm_north"), utm_easting=datum.get("utm_easting"),
                  utm_northing=datum.get("utm_northing"))
    if frame == geo.FRAME_ENU:
        lat0, lon0 = float(datum["latitude"]), float(datum["longitude"])
    else:
        lat0, lon0 = geo.local_to_gps(0.0, 0.0, **kwargs)
    e0, n0 = geo.latlon_to_utm(lat0, lon0, zone, north)
    e1, n1 = geo.latlon_to_utm(*geo.local_to_gps(_YAW_PROBE_M, 0.0, **kwargs), zone, north)
    return {"tx": e0 - oe, "ty": n0 - on, "yaw": normalize_yaw(math.atan2(n1 - n0, e1 - e0))}


def apply_transform(t: Mapping[str, float], x: float, y: float) -> Tuple[float, float]:
    """A robot-frame point in the map frame: R(yaw) (x, y) + (tx, ty)."""
    c, s = math.cos(t["yaw"]), math.sin(t["yaw"])
    return c * x - s * y + t["tx"], s * x + c * y + t["ty"]
