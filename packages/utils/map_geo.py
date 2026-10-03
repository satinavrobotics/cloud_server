"""Map frames for typed maps and mapping sessions (docs/satinav-maps-redesign.md §2-§3, M1).

A `geo` map's frame is UTM grid metres in one zone fixed per map, relative to the map origin
(`geo.origin_e`, `geo.origin_n`) and rotated by `geo.bearing_deg` (the angle of the map's +X axis
from grid east, CCW; 0 for every map whose origin came from a session, set only when a local map
is converted to geo and keeps its own coordinates): UTM = origin + R(bearing) (x, y). A mapping
session records the robot's datum for that run and
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


def geo_of_datum_frame(datum: Mapping[str, Any]) -> Dict[str, Any]:
    """The `geo` block whose frame is the datum's own frame: origin at the datum, rotated by its
    bearing (plus, for an 'enu' datum, the grid convergence there: its +X is true east). Used
    where a map's display datum (datum_*) describes the frame, so the two agree."""
    block = geo_from_datum(datum)
    bearing = math.radians(float(datum.get("bearing_deg") or 0.0))
    if geo.normalize_frame(datum.get("frame")) == geo.FRAME_ENU:
        bearing += grid_convergence_rad(float(datum["latitude"]), float(datum["longitude"]),
                                        block["utm_zone"], block["utm_north"])
    block["bearing_deg"] = math.degrees(normalize_yaw(bearing))
    return block


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


def geo_bearing_rad(map_geo: Optional[Mapping[str, Any]]) -> float:
    """The map frame's rotation against the UTM grid (radians); 0 when absent."""
    return math.radians(float((map_geo or {}).get("bearing_deg") or 0.0))


def grid_to_map(map_geo: Mapping[str, Any], de: float, dn: float) -> Tuple[float, float]:
    """UTM offsets from the map origin (grid east, grid north) -> map (x, y): R(-bearing)."""
    b = geo_bearing_rad(map_geo)
    c, s = math.cos(b), math.sin(b)
    return c * de + s * dn, -s * de + c * dn


def map_to_grid(map_geo: Mapping[str, Any], x: float, y: float) -> Tuple[float, float]:
    """Map (x, y) -> UTM offsets from the map origin: R(bearing)."""
    b = geo_bearing_rad(map_geo)
    c, s = math.cos(b), math.sin(b)
    return c * x - s * y, s * x + c * y


def latlon_to_map(map_geo: Mapping[str, Any], lat: float, lon: float) -> Tuple[float, float]:
    """WGS84 -> the geo map's frame (exact UTM in the map's zone, then the map's rotation)."""
    e, n = geo.latlon_to_utm(lat, lon, int(map_geo["utm_zone"]), bool(map_geo["utm_north"]))
    return grid_to_map(map_geo, e - float(map_geo["origin_e"]), n - float(map_geo["origin_n"]))


def map_to_latlon(map_geo: Mapping[str, Any], x: float, y: float) -> Tuple[float, float]:
    """The geo map's frame -> WGS84 (lat, lon); inverse of latlon_to_map."""
    de, dn = map_to_grid(map_geo, x, y)
    return geo.utm_to_latlon(float(map_geo["origin_e"]) + de, float(map_geo["origin_n"]) + dn,
                             int(map_geo["utm_zone"]), bool(map_geo["utm_north"]))


def grid_convergence_rad(lat: float, lon: float, zone: int, north: bool) -> float:
    """Angle of true east in the UTM grid of `zone` at (lat, lon), CCW (radians): a bearing
    measured from true east plus this is the same direction measured from grid east."""
    e0, n0 = geo.latlon_to_utm(lat, lon, zone, north)
    e1, n1 = geo.latlon_to_utm(*geo.enu_to_latlon(_YAW_PROBE_M, 0.0, lat, lon), zone, north)
    return math.atan2(n1 - n0, e1 - e0)


# UTM is defined between 80 deg S and 84 deg N; a zone is accepted for an anchor up to one zone
# width from its central meridian (the projection stays exact to ~1 mm there).
UTM_LAT_MIN, UTM_LAT_MAX = -80.0, 84.0
MAX_ZONE_OFFSET_DEG = 6.0


def zone_offset_deg(lon: float, zone: int) -> float:
    """|longitude - the zone's central meridian| in degrees, wrapped to [0, 180]."""
    d = (lon - (zone * 6.0 - 183.0) + 180.0) % 360.0 - 180.0
    return abs(d)


def geo_from_anchor(latitude: float, longitude: float, anchor_x: float = 0.0,
                    anchor_y: float = 0.0, bearing_deg: float = 0.0, frame: Optional[str] = None,
                    utm_zone: Optional[int] = None, utm_north: Optional[bool] = None
                    ) -> Dict[str, Any]:
    """The `geo` block that puts the map-frame point (anchor_x, anchor_y) at (latitude,
    longitude) with the map's +X axis at `bearing_deg` from east (CCW): from grid east for
    frame 'utm' (the default), from true east at the anchor for 'enu'. The map's coordinates are
    not touched: the origin is wherever (0, 0) lands. Zone: `utm_zone` or the anchor
    longitude's; hemisphere: `utm_north` or the anchor's. Raises ValueError for an anchor
    outside UTM (80 S .. 84 N), a zone more than MAX_ZONE_OFFSET_DEG from the anchor, or a
    non-finite input."""
    values = (latitude, longitude, anchor_x, anchor_y, bearing_deg)
    if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in values):
        raise ValueError("latitude, longitude, anchor and bearing must be finite numbers")
    if not UTM_LAT_MIN <= latitude <= UTM_LAT_MAX:
        raise ValueError(f"latitude {latitude} is outside UTM ({UTM_LAT_MIN} .. {UTM_LAT_MAX})")
    if not -180.0 <= longitude <= 180.0:
        raise ValueError(f"longitude {longitude} is outside -180 .. 180")
    zone = int(utm_zone) if utm_zone is not None else geo.utm_zone_from_longitude(longitude)
    if not 1 <= zone <= 60:
        raise ValueError(f"UTM zone {zone} is outside 1 .. 60")
    if zone_offset_deg(longitude, zone) > MAX_ZONE_OFFSET_DEG:
        raise ValueError(f"UTM zone {zone} is too far from longitude {longitude} (more than "
                         f"{MAX_ZONE_OFFSET_DEG:g} deg from its central meridian)")
    north = bool(utm_north) if utm_north is not None else latitude >= 0.0
    bearing = math.radians(float(bearing_deg))
    if geo.normalize_frame(frame or geo.FRAME_UTM) == geo.FRAME_ENU:  # None = utm here
        bearing += grid_convergence_rad(latitude, longitude, zone, north)
    bearing = normalize_yaw(bearing)
    ea, na = geo.latlon_to_utm(latitude, longitude, zone, north)
    c, s = math.cos(bearing), math.sin(bearing)
    origin_e = ea - (c * anchor_x - s * anchor_y)
    origin_n = na - (s * anchor_x + c * anchor_y)
    return {"utm_zone": zone, "utm_north": north, "origin_e": origin_e, "origin_n": origin_n,
            "bearing_deg": math.degrees(bearing)}


def former_datum_of(map_geo: Mapping[str, Any]) -> Dict[str, Any]:
    """A geo block as `former_datum` (without `converted_at`): the origin's WGS84 position plus
    the block itself, so a conversion back with the same anchor restores the frame."""
    lat, lon = map_to_latlon(map_geo, 0.0, 0.0)
    return {"latitude": lat, "longitude": lon,
            "bearing_deg": float(map_geo.get("bearing_deg") or 0.0),
            "utm_zone": int(map_geo["utm_zone"]), "utm_north": bool(map_geo["utm_north"]),
            "origin_e": float(map_geo["origin_e"]), "origin_n": float(map_geo["origin_n"])}


def session_transform(map_geo: Mapping[str, Any], datum: Mapping[str, Any]) -> Dict[str, float]:
    """map_T_session of a session on a geo map (see the module docstring): the run frame in
    UTM offsets from the map origin, then the map's own rotation (`bearing_deg`)."""
    grid = _session_grid_transform(map_geo, datum)
    if not geo_bearing_rad(map_geo):
        return grid
    x, y = grid_to_map(map_geo, grid["tx"], grid["ty"])
    return {"tx": x, "ty": y, "yaw": normalize_yaw(grid["yaw"] - geo_bearing_rad(map_geo))}


def _session_grid_transform(map_geo: Mapping[str, Any], datum: Mapping[str, Any]
                            ) -> Dict[str, float]:
    """The run frame in UTM grid offsets from the map origin (no map rotation)."""
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


def apply_pose(t: Mapping[str, float], x: float, y: float, yaw: float
               ) -> Tuple[float, float, float]:
    """A pose through `t`: the point transformed, yaw + t.yaw wrapped to (-pi, pi]."""
    px, py = apply_transform(t, float(x), float(y))
    return px, py, normalize_yaw(float(yaw) + float(t["yaw"]))


def invert_transform(t: Mapping[str, float]) -> Dict[str, float]:
    """The inverse of `t` (map_T_robot -> robot_T_map)."""
    c, s = math.cos(t["yaw"]), math.sin(t["yaw"])
    return {"tx": -(c * t["tx"] + s * t["ty"]), "ty": -(-s * t["tx"] + c * t["ty"]),
            "yaw": normalize_yaw(-t["yaw"])}


def is_identity(t: Mapping[str, float], tol: float = 1e-12) -> bool:
    return all(abs(float(t.get(k, 0.0))) <= tol for k in ("tx", "ty", "yaw"))
