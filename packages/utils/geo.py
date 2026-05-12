"""GPS ↔ local Cartesian coordinate conversion utilities.

Uses an equirectangular projection with a datum-bearing rotation.
Accurate to sub-metre for maps up to ~10 km across.
"""

import math
from typing import Tuple

EARTH_RADIUS_M = 6_371_000.0


def gps_to_local(
    lat: float,
    lon: float,
    datum_lat: float,
    datum_lon: float,
    datum_bearing_deg: float = 0.0,
) -> Tuple[float, float]:
    """Convert WGS84 lat/lon to local Cartesian (x, y) in metres.

    Args:
        lat: Target latitude in degrees.
        lon: Target longitude in degrees.
        datum_lat: Map-origin latitude in degrees.
        datum_lon: Map-origin longitude in degrees.
        datum_bearing_deg: Angle from map +X axis to true north, in degrees.

    Returns:
        (x, y) in metres in the map's local Cartesian frame.
    """
    dlat = math.radians(lat - datum_lat)
    dlon = math.radians(lon - datum_lon)
    east = dlon * EARTH_RADIUS_M * math.cos(math.radians(datum_lat))
    north = dlat * EARTH_RADIUS_M
    b = math.radians(datum_bearing_deg)
    x = east * math.cos(b) + north * math.sin(b)
    y = -east * math.sin(b) + north * math.cos(b)
    return x, y


def local_to_gps(
    x: float,
    y: float,
    datum_lat: float,
    datum_lon: float,
    datum_bearing_deg: float = 0.0,
) -> Tuple[float, float]:
    """Inverse of gps_to_local: local Cartesian (x, y) → WGS84 lat/lon.

    Args:
        x: Local x coordinate in metres.
        y: Local y coordinate in metres.
        datum_lat: Map-origin latitude in degrees.
        datum_lon: Map-origin longitude in degrees.
        datum_bearing_deg: Angle from map +X axis to true north, in degrees.

    Returns:
        (latitude, longitude) in degrees.
    """
    b = math.radians(datum_bearing_deg)
    east = x * math.cos(b) - y * math.sin(b)
    north = x * math.sin(b) + y * math.cos(b)
    dlat = north / EARTH_RADIUS_M
    dlon = east / (EARTH_RADIUS_M * math.cos(math.radians(datum_lat)))
    return datum_lat + math.degrees(dlat), datum_lon + math.degrees(dlon)
