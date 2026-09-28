"""GPS <-> local Cartesian coordinate conversion utilities.

A map's local x/y (metres) is tied to WGS84 by its datum, in one of two frames:

- ``"utm"``: x/y are UTM grid offsets from the datum (real robots with GNSS: sati_pose_module
  anchors on the UTM easting/northing of its first fix). Converted exactly with the WGS84
  transverse Mercator projection (Krueger series to n^6, Karney 2011; sub-millimetre inside a
  zone and well beyond it). The datum's own easting/northing is used when the robot sent it,
  otherwise it is projected from the datum lat/lon.
- ``"enu"``: x/y are east/north in the local tangent plane at the datum (the sim's world frame,
  a UM982 fix anchor). Converted exactly through ECEF: a point on the ellipsoid is projected
  orthogonally onto the plane tangent to the WGS84 ellipsoid at the datum. To first order this
  is east = dlon * N * cos(lat0), north = dlat * M with the prime-vertical (N) and meridional (M)
  radii at the datum latitude, but unlike that it keeps the curvature of parallels (2 m at 5 km).

In both frames ``bearing_deg`` rotates the map axes: it is the angle of the map's +X axis from
east (UTM: grid east), counter-clockwise positive, so 0 means +X east / +Y north and 90 means
+X north / +Y west. A datum without a frame is ``"enu"`` (what every datum was before the
frame was sent).
"""

import math
from typing import Optional, Tuple

FRAME_ENU = "enu"
FRAME_UTM = "utm"
FRAMES = (FRAME_ENU, FRAME_UTM)

# WGS84
WGS84_A = 6378137.0
WGS84_F = 1.0 / 298.257223563
WGS84_E2 = WGS84_F * (2.0 - WGS84_F)
WGS84_B = WGS84_A * (1.0 - WGS84_F)

UTM_K0 = 0.9996
UTM_FALSE_EASTING = 500000.0
UTM_FALSE_NORTHING_SOUTH = 10000000.0

_N = WGS84_F / (2.0 - WGS84_F)
_N2 = _N * _N
_N3 = _N2 * _N
_N4 = _N3 * _N
_N5 = _N4 * _N
_N6 = _N5 * _N
# Rectifying radius.
_A_RECT = WGS84_A / (1.0 + _N) * (1.0 + _N2 / 4.0 + _N4 / 64.0 + _N6 / 256.0)
# Krueger series coefficients (Karney 2011, eqs. 35 and 36).
_ALPHA = (
    _N / 2 - 2 * _N2 / 3 + 5 * _N3 / 16 + 41 * _N4 / 180 - 127 * _N5 / 288 + 7891 * _N6 / 37800,
    13 * _N2 / 48 - 3 * _N3 / 5 + 557 * _N4 / 1440 + 281 * _N5 / 630 - 1983433 * _N6 / 1935360,
    61 * _N3 / 240 - 103 * _N4 / 140 + 15061 * _N5 / 26880 + 167603 * _N6 / 181440,
    49561 * _N4 / 161280 - 179 * _N5 / 168 + 6601661 * _N6 / 7257600,
    34729 * _N5 / 80640 - 3418889 * _N6 / 1995840,
    212378941 * _N6 / 319334400,
)
_BETA = (
    _N / 2 - 2 * _N2 / 3 + 37 * _N3 / 96 - _N4 / 360 - 81 * _N5 / 512 + 96199 * _N6 / 604800,
    _N2 / 48 + _N3 / 15 - 437 * _N4 / 1440 + 46 * _N5 / 105 - 1118711 * _N6 / 3870720,
    17 * _N3 / 480 - 37 * _N4 / 840 - 209 * _N5 / 4480 + 5569 * _N6 / 90720,
    4397 * _N4 / 161280 - 11 * _N5 / 504 - 830251 * _N6 / 7257600,
    4583 * _N5 / 161280 - 108847 * _N6 / 3991680,
    20648693 * _N6 / 638668800,
)
_E = math.sqrt(WGS84_E2)


def normalize_frame(frame: Optional[str]) -> str:
    """'utm' or 'enu'; None/empty (a datum from before frames were sent) is 'enu'."""
    if not frame:
        return FRAME_ENU
    f = str(frame).strip().lower()
    if f not in FRAMES:
        raise ValueError(f"Unknown datum frame {frame!r}; expected one of {FRAMES}")
    return f


def utm_zone_from_longitude(lon_deg: float) -> int:
    """Plain 6-degree UTM zone (no Norway/Svalbard exceptions), the robot's rule
    (sati_vda5050_client utm.hpp, sati_geo_pose UM982)."""
    lon = (lon_deg + 180.0) % 360.0 - 180.0
    return min(int((lon + 180.0) / 6.0) + 1, 60)


def _central_meridian_rad(zone: int) -> float:
    return math.radians(zone * 6.0 - 183.0)


def _conformal_tau(tau: float) -> float:
    """tan(conformal latitude) from tan(geodetic latitude) (Karney 2011, eq. 7-9)."""
    sigma = math.sinh(_E * math.atanh(_E * tau / math.hypot(1.0, tau)))
    return tau * math.hypot(1.0, sigma) - sigma * math.hypot(1.0, tau)


def _geodetic_tau(tau_p: float) -> float:
    """Inverse of _conformal_tau by Newton's method (Karney 2011, eq. 19-21)."""
    tau = tau_p / (1.0 - WGS84_E2)
    for _ in range(5):
        tau_i = _conformal_tau(tau)
        d_tau = (tau_p - tau_i) * (1.0 + (1.0 - WGS84_E2) * tau * tau) / (
            (1.0 - WGS84_E2) * math.hypot(1.0, tau_i) * math.hypot(1.0, tau))
        tau += d_tau
        if abs(d_tau) < 1e-14 * max(1.0, abs(tau)):
            break
    return tau


def latlon_to_utm(lat_deg: float, lon_deg: float, zone: int,
                  north: Optional[bool] = None) -> Tuple[float, float]:
    """WGS84 lat/lon -> UTM (easting, northing) in metres in the given zone.

    `north` selects the false northing (default: the point's own hemisphere). Points outside
    the zone are projected with the zone's central meridian (still exact to ~1 mm several
    degrees out).
    """
    if north is None:
        north = lat_deg >= 0.0
    phi = math.radians(lat_deg)
    lam = math.radians(lon_deg) - _central_meridian_rad(zone)
    lam = math.atan2(math.sin(lam), math.cos(lam))
    tau_p = _conformal_tau(math.tan(phi))
    xi_p = math.atan2(tau_p, math.cos(lam))
    eta_p = math.asinh(math.sin(lam) / math.hypot(tau_p, math.cos(lam)))
    xi = xi_p
    eta = eta_p
    for j, a in enumerate(_ALPHA, start=1):
        xi += a * math.sin(2 * j * xi_p) * math.cosh(2 * j * eta_p)
        eta += a * math.cos(2 * j * xi_p) * math.sinh(2 * j * eta_p)
    easting = UTM_FALSE_EASTING + UTM_K0 * _A_RECT * eta
    northing = UTM_K0 * _A_RECT * xi + (0.0 if north else UTM_FALSE_NORTHING_SOUTH)
    return easting, northing


def utm_to_latlon(easting: float, northing: float, zone: int,
                  north: bool) -> Tuple[float, float]:
    """UTM (easting, northing) in metres -> WGS84 (lat, lon) in degrees."""
    xi = (northing - (0.0 if north else UTM_FALSE_NORTHING_SOUTH)) / (UTM_K0 * _A_RECT)
    eta = (easting - UTM_FALSE_EASTING) / (UTM_K0 * _A_RECT)
    xi_p = xi
    eta_p = eta
    for j, b in enumerate(_BETA, start=1):
        xi_p -= b * math.sin(2 * j * xi) * math.cosh(2 * j * eta)
        eta_p -= b * math.cos(2 * j * xi) * math.sinh(2 * j * eta)
    sinh_eta = math.sinh(eta_p)
    cos_xi = math.cos(xi_p)
    tau_p = math.sin(xi_p) / math.hypot(sinh_eta, cos_xi)
    lam = math.atan2(sinh_eta, cos_xi)
    phi = math.atan(_geodetic_tau(tau_p))
    lon = math.degrees(lam + _central_meridian_rad(zone))
    lon = (lon + 180.0) % 360.0 - 180.0
    return math.degrees(phi), lon


def _ecef(lat_deg: float, lon_deg: float) -> Tuple[float, float, float]:
    phi = math.radians(lat_deg)
    lam = math.radians(lon_deg)
    sin_phi = math.sin(phi)
    n = WGS84_A / math.sqrt(1.0 - WGS84_E2 * sin_phi * sin_phi)
    return (n * math.cos(phi) * math.cos(lam), n * math.cos(phi) * math.sin(lam),
            n * (1.0 - WGS84_E2) * sin_phi)


def _enu_axes(lat_deg: float, lon_deg: float):
    phi = math.radians(lat_deg)
    lam = math.radians(lon_deg)
    sp, cp, sl, cl = math.sin(phi), math.cos(phi), math.sin(lam), math.cos(lam)
    east = (-sl, cl, 0.0)
    north = (-sp * cl, -sp * sl, cp)
    up = (cp * cl, cp * sl, sp)
    return east, north, up


def _ecef_to_latlon(x: float, y: float, z: float) -> Tuple[float, float]:
    lon = math.atan2(y, x)
    p = math.hypot(x, y)
    phi = math.atan2(z, p * (1.0 - WGS84_E2))
    for _ in range(10):
        sin_phi = math.sin(phi)
        n = WGS84_A / math.sqrt(1.0 - WGS84_E2 * sin_phi * sin_phi)
        # z + e2*N*sin(phi) over p is tan(phi) for the point's own normal.
        new_phi = math.atan2(z + WGS84_E2 * n * sin_phi, p)
        if abs(new_phi - phi) < 1e-15:
            phi = new_phi
            break
        phi = new_phi
    return math.degrees(phi), math.degrees(lon)


def latlon_to_enu(lat: float, lon: float, datum_lat: float,
                  datum_lon: float) -> Tuple[float, float]:
    """(east, north) of a point on the ellipsoid in the tangent plane at the datum."""
    p = _ecef(lat, lon)
    p0 = _ecef(datum_lat, datum_lon)
    d = (p[0] - p0[0], p[1] - p0[1], p[2] - p0[2])
    e, n, _ = _enu_axes(datum_lat, datum_lon)
    return (d[0] * e[0] + d[1] * e[1] + d[2] * e[2], d[0] * n[0] + d[1] * n[1] + d[2] * n[2])


def enu_to_latlon(east: float, north: float, datum_lat: float,
                  datum_lon: float) -> Tuple[float, float]:
    """Inverse of latlon_to_enu: the point on the ellipsoid right below/above (east, north)."""
    p0 = _ecef(datum_lat, datum_lon)
    e, n, u = _enu_axes(datum_lat, datum_lon)
    q = tuple(p0[i] + east * e[i] + north * n[i] for i in range(3))
    # Move q along the datum's up vector onto the ellipsoid: A t^2 + B t + C = 0.
    a2 = WGS84_A * WGS84_A
    b2 = WGS84_B * WGS84_B
    qa = (q[0] * q[0] + q[1] * q[1]) / a2 + q[2] * q[2] / b2 - 1.0
    qb = 2.0 * ((q[0] * u[0] + q[1] * u[1]) / a2 + q[2] * u[2] / b2)
    qc = (u[0] * u[0] + u[1] * u[1]) / a2 + u[2] * u[2] / b2
    # Root nearest 0, in the cancellation-free form (qb > 0 near the surface).
    t = -2.0 * qa / (qb + math.sqrt(qb * qb - 4.0 * qc * qa))
    return _ecef_to_latlon(q[0] + t * u[0], q[1] + t * u[1], q[2] + t * u[2])


def _rotate_to_map(east: float, north: float, bearing_deg: float) -> Tuple[float, float]:
    b = math.radians(bearing_deg)
    return (east * math.cos(b) + north * math.sin(b), -east * math.sin(b) + north * math.cos(b))


def _rotate_from_map(x: float, y: float, bearing_deg: float) -> Tuple[float, float]:
    b = math.radians(bearing_deg)
    return (x * math.cos(b) - y * math.sin(b), x * math.sin(b) + y * math.cos(b))


def _datum_utm(datum_lat: float, datum_lon: float, utm_zone: Optional[int],
               utm_north: Optional[bool], utm_easting: Optional[float],
               utm_northing: Optional[float]) -> Tuple[int, bool, float, float]:
    zone = int(utm_zone) if utm_zone else utm_zone_from_longitude(datum_lon)
    north = bool(utm_north) if utm_north is not None else datum_lat >= 0.0
    if utm_easting is not None and utm_northing is not None:
        return zone, north, float(utm_easting), float(utm_northing)
    e0, n0 = latlon_to_utm(datum_lat, datum_lon, zone, north)
    return zone, north, e0, n0


def gps_to_local(
    lat: float,
    lon: float,
    datum_lat: float,
    datum_lon: float,
    datum_bearing_deg: float = 0.0,
    frame: Optional[str] = FRAME_ENU,
    utm_zone: Optional[int] = None,
    utm_north: Optional[bool] = None,
    utm_easting: Optional[float] = None,
    utm_northing: Optional[float] = None,
) -> Tuple[float, float]:
    """Convert WGS84 lat/lon to the map's local (x, y) in metres.

    Args:
        lat, lon: Target position in degrees.
        datum_lat, datum_lon: Map-origin position in degrees.
        datum_bearing_deg: Angle of the map's +X axis from (grid) east, CCW, in degrees.
        frame: 'utm' or 'enu' (None = 'enu'); see the module docstring.
        utm_zone, utm_north: UTM zone and hemisphere of a 'utm' datum (default: the datum's own).
        utm_easting, utm_northing: The datum's exact UTM coordinates if known.
    """
    if normalize_frame(frame) == FRAME_UTM:
        zone, north, e0, n0 = _datum_utm(
            datum_lat, datum_lon, utm_zone, utm_north, utm_easting, utm_northing)
        e, n = latlon_to_utm(lat, lon, zone, north)
        return _rotate_to_map(e - e0, n - n0, datum_bearing_deg)
    east, north_m = latlon_to_enu(lat, lon, datum_lat, datum_lon)
    return _rotate_to_map(east, north_m, datum_bearing_deg)


def local_to_gps(
    x: float,
    y: float,
    datum_lat: float,
    datum_lon: float,
    datum_bearing_deg: float = 0.0,
    frame: Optional[str] = FRAME_ENU,
    utm_zone: Optional[int] = None,
    utm_north: Optional[bool] = None,
    utm_easting: Optional[float] = None,
    utm_northing: Optional[float] = None,
) -> Tuple[float, float]:
    """Inverse of gps_to_local: the map's local (x, y) in metres -> WGS84 (lat, lon) in degrees."""
    east, north_m = _rotate_from_map(x, y, datum_bearing_deg)
    if normalize_frame(frame) == FRAME_UTM:
        zone, north, e0, n0 = _datum_utm(
            datum_lat, datum_lon, utm_zone, utm_north, utm_easting, utm_northing)
        return utm_to_latlon(e0 + east, n0 + north_m, zone, north)
    return enu_to_latlon(east, north_m, datum_lat, datum_lon)
