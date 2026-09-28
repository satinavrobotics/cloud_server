"""Unit tests for packages/utils/geo.py: GPS <-> local conversions in the 'utm' and 'enu' frames.

Golden values were computed with pyproj 3.7 (PROJ etmerc for UTM, EPSG:4979->4978 ECEF for the
tangent plane, Geod(WGS84) for geodesics) in a throwaway python:3.12-slim container; pyproj is
not a dependency. The same tables are in sati-client __tests__/utils/mapTransform.test.ts, so
client and server provably agree.
"""

import math

import pytest

from packages.utils.geo import (
    enu_to_latlon, gps_to_local, latlon_to_enu, latlon_to_utm, local_to_gps, normalize_frame,
    utm_to_latlon, utm_zone_from_longitude,
)

# (lat, lon, zone, north, easting, northing)
UTM_POINTS = [
    (47.47946, 19.03238, 34, True, 351756.484938, 5260323.440888),
    (47.37, 8.54, 32, True, 465270.4231, 5246384.775982),
    (-33.8688, 151.2093, 56, False, 334368.633648, 6250948.345385),
    (-23.5505, -46.6333, 23, False, 333287.915102, 7394588.318558),
    (64.1466, -21.9426, 27, True, 454138.376516, 7113689.868974),
    (0.5, 3.1, 31, True, 511127.081121, 55265.121879),
    (47.5, 23.9, 34, True, 718400.91214, 5264806.435385),
    (47.47946, 19.03238, 33, True, 803789.272495, 5266332.230924),
    (-0.3, 36.8, 37, False, 255137.836182, 9966816.359553),
]

BUDAPEST_GRID_CASES = [
    (100.0, 0.0, 47.4794827626, 19.033706484),
    (70.710678, 70.710678, 47.480111977, 19.0332942265),
    (0.0, 100.0, 47.4803592694, 19.0323464115),
    (-70.710678, 70.710678, 47.4800797743, 19.0314182734),
    (-100.0, 0.0, 47.4794372221, 19.0310535176),
    (-70.710678, -70.710678, 47.4788080159, 19.0314657961),
    (-0.0, -100.0, 47.4785607305, 19.0324135869),
    (70.710678, -70.710678, 47.4788402172, 19.033341704),
    (1000.0, 0.0, 47.4796869358, 19.0456449132),
    (707.106781, 707.106781, 47.4859794536, 19.0415232823),
    (0.0, 1000.0, 47.4884526866, 19.0320440417),
    (-707.106781, 707.106781, 47.4856573621, 19.0227617162),
    (-1000.0, 0.0, 47.4792315305, 19.0191152498),
    (-707.106781, -707.106781, 47.4729398429, 19.0232389785),
    (-0.0, -1000.0, 47.4704672974, 19.0327157953),
    (707.106781, -707.106781, 47.4732617918, 19.0419960231),
    (5000.0, 0.0, 47.4805793417, 19.0987061736),
    (3535.533906, 3535.533906, 47.5120502225, 19.0781190362),
    (0.0, 5000.0, 47.5244232734, 19.0306985768),
    (-3535.533906, 3535.533906, 47.5104383395, 18.9842659591),
    (-5000.0, 0.0, 47.4783023171, 18.9660579006),
    (-3535.533906, -3535.533906, 47.4468521888, 18.9866974831),
    (-0.0, -5000.0, 47.4344963271, 19.034057349),
    (3535.533906, -3535.533906, 47.4484605084, 19.0804375216),
]

ENU_CASES = [
    (47.47946, 19.03238, 100.0, 0, 47.4803594407, 19.03238, 0.0, 100.0),
    (47.47946, 19.03238, 100.0, 30, 47.4802389366, 19.0330433778, 50.0, 86.60254),
    (47.47946, 19.03238, 100.0, 90, 47.4794599923, 19.0337067359, 100.0, -0.0),
    (47.47946, 19.03238, 100.0, 135, 47.4788239954, 19.0333181327, 70.710678, -70.710678),
    (47.47946, 19.03238, 100.0, 200, 47.4786148011, 19.0319262369, -34.202014, -93.969262),
    (47.47946, 19.03238, 100.0, 270, 47.4794599923, 19.0310532641, -100.0, -0.0),
    (47.47946, 19.03238, 100.0, 315, 47.4800959968, 19.0314418447, -70.710678, 70.710678),
    (47.47946, 19.03238, 1000.0, 0, 47.4884544011, 19.03238, 0.0, 999.999996),
    (47.47946, 19.03238, 1000.0, 30, 47.4872491888, 19.0390146604, 499.999998, 866.0254),
    (47.47946, 19.03238, 1000.0, 90, 47.4794592325, 19.0456473594, 999.999996, 0.0),
    (47.47946, 19.03238, 1000.0, 135, 47.4730996057, 19.0417603078, 707.106778, -707.106778),
    (47.47946, 19.03238, 1000.0, 200, 47.471007925, 19.0278430234, -342.020142, -939.692617),
    (47.47946, 19.03238, 1000.0, 270, 47.4794592325, 19.0191126406, -999.999996, 0.0),
    (47.47946, 19.03238, 1000.0, 315, 47.4858196197, 19.0229974279, -707.106778, 707.106778),
    (47.47946, 19.03238, 5000.0, 0, 47.5244318638, 19.03238, 0.0, 4999.999487),
    (47.47946, 19.03238, 5000.0, 30, 47.5184019955, 19.0655729317, 2499.999744, 4330.126575),
    (47.47946, 19.03238, 5000.0, 90, 47.4794408119, 19.0987167813, 4999.99949, 0.0),
    (47.47946, 19.03238, 5000.0, 135, 47.4476502895, 19.0792589117, 3535.533545, -3535.533543),
    (47.47946, 19.03238, 5000.0, 200, 47.4371977061, 19.0097096543, -1710.100542, -4698.462622),
    (47.47946, 19.03238, 5000.0, 270, 47.4794408119, 18.9660432187, -4999.99949, 0.0),
    (47.47946, 19.03238, 5000.0, 315, 47.5112503451, 18.9854444807, -3535.533545, 3535.533543),
    (-33.8688, 151.2093, 5000.0, 0, -33.823722309, 151.2093, -0.0, 4999.999484),
    (-33.8688, 151.2093, 5000.0, 30, -33.8297586343, 151.236306974, 2499.999744, 4330.126573),
    (-33.8688, 151.2093, 5000.0, 90, -33.8687881534, 151.2633385226, 4999.999489, -0.0),
    (-33.8688, 151.2093, 5000.0, 135, -33.9006686145, 151.2475252184, 3535.533544, -3535.533542),
    (-33.8688, 151.2093, 5000.0, 200, -33.9111574856, 151.1908085988, -1710.100541, -4698.462619),
    (-33.8688, 151.2093, 5000.0, 270, -33.8687881534, 151.1552614774, -4999.999489, -0.0),
    (-33.8688, 151.2093, 5000.0, 315, -33.8369193737, 151.1711031849, -3535.533544, 3535.533542),
]

BUDAPEST = (47.47946, 19.03238)
BUDAPEST_UTM = (351756.484938, 5260323.440888)  # zone 34N
# BUDAPEST_GRID_CASES: (x, y, lat, lon): a point at UTM grid offset (x, y) from the Budapest
# datum, 100 m / 1 km / 5 km away in 8 directions, and where it really is.
# ENU_CASES: (lat0, lon0, distance, azimuth, lat, lon, east, north): the point `distance` metres
# along the geodesic at `azimuth` from the datum, and its exact tangent-plane east/north.


def _metres(lat1, lon1, lat2, lon2):
    """Distance between two nearby points (for mm-level differences)."""
    dn = math.radians(lat2 - lat1) * 6_367_000.0
    de = math.radians(lon2 - lon1) * 6_389_000.0 * math.cos(math.radians(lat1))
    return math.hypot(dn, de)


@pytest.mark.unit
class TestUtmProjection:
    @pytest.mark.parametrize("lat,lon,zone,north,easting,northing", UTM_POINTS)
    def test_forward_matches_pyproj(self, lat, lon, zone, north, easting, northing):
        e, n = latlon_to_utm(lat, lon, zone, north)
        assert abs(e - easting) < 1e-4
        assert abs(n - northing) < 1e-4

    @pytest.mark.parametrize("lat,lon,zone,north,easting,northing", UTM_POINTS)
    def test_inverse_matches_pyproj(self, lat, lon, zone, north, easting, northing):
        la, lo = utm_to_latlon(easting, northing, zone, north)
        assert _metres(lat, lon, la, lo) < 1e-4

    @pytest.mark.parametrize("lat,lon", [(47.47946, 19.03238), (-33.8688, 151.2093),
                                         (0.0, 0.0), (71.0, 25.8), (-54.8, -68.3)])
    def test_round_trip(self, lat, lon):
        zone = utm_zone_from_longitude(lon)
        e, n = latlon_to_utm(lat, lon, zone)
        la, lo = utm_to_latlon(e, n, zone, lat >= 0.0)
        assert _metres(lat, lon, la, lo) < 1e-6

    def test_zone_rule(self):
        assert utm_zone_from_longitude(19.03238) == 34
        assert utm_zone_from_longitude(-180.0) == 1
        assert utm_zone_from_longitude(179.999) == 60
        assert utm_zone_from_longitude(151.2093) == 56


@pytest.mark.unit
class TestUtmFrame:
    """Robot-style local coordinates: UTM grid offsets from the datum."""

    @pytest.mark.parametrize("x,y,lat,lon", BUDAPEST_GRID_CASES)
    def test_local_to_gps_exact(self, x, y, lat, lon):
        la, lo = local_to_gps(x, y, *BUDAPEST, 0.0, frame="utm", utm_zone=34, utm_north=True)
        assert _metres(lat, lon, la, lo) < 0.01

    @pytest.mark.parametrize("x,y,lat,lon", BUDAPEST_GRID_CASES)
    def test_gps_to_local_exact(self, x, y, lat, lon):
        lx, ly = gps_to_local(lat, lon, *BUDAPEST, 0.0, frame="utm", utm_zone=34, utm_north=True)
        assert math.hypot(lx - x, ly - y) < 0.01

    @pytest.mark.parametrize("x,y,lat,lon", BUDAPEST_GRID_CASES)
    def test_datum_easting_northing_used(self, x, y, lat, lon):
        """With the robot's exact datum E/N the result is the same (no round trip needed)."""
        la, lo = local_to_gps(x, y, *BUDAPEST, 0.0, frame="utm", utm_zone=34, utm_north=True,
                              utm_easting=BUDAPEST_UTM[0], utm_northing=BUDAPEST_UTM[1])
        assert _metres(lat, lon, la, lo) < 0.01

    def test_zone_defaults_to_datum_zone(self):
        x, y, lat, lon = BUDAPEST_GRID_CASES[-1]
        la, lo = local_to_gps(x, y, *BUDAPEST, frame="utm")
        assert _metres(lat, lon, la, lo) < 0.01

    def test_old_equirectangular_was_off_by_the_convergence(self):
        """The bug this frame fixes: treating grid offsets as east/north misses by ~27 m at 1 km."""
        x, y, lat, lon = BUDAPEST_GRID_CASES[8]  # 1 km grid east
        la, lo = local_to_gps(x, y, *BUDAPEST, frame="enu")
        assert 20.0 < _metres(lat, lon, la, lo) < 30.0

    @pytest.mark.parametrize("bearing", [0.0, 12.5, 90.0, -45.0])
    def test_bearing_rotates_grid_offsets(self, bearing):
        x, y, lat, lon = BUDAPEST_GRID_CASES[17]
        b = math.radians(bearing)
        mx = x * math.cos(b) + y * math.sin(b)
        my = -x * math.sin(b) + y * math.cos(b)
        la, lo = local_to_gps(mx, my, *BUDAPEST, bearing, frame="utm")
        assert _metres(lat, lon, la, lo) < 0.01

    def test_southern_hemisphere_round_trip(self):
        datum = (-33.8688, 151.2093)
        for x, y in [(0.0, 0.0), (1234.5, -678.9), (-5000.0, 5000.0)]:
            lat, lon = local_to_gps(x, y, *datum, 7.0, frame="utm", utm_zone=56, utm_north=False)
            x2, y2 = gps_to_local(lat, lon, *datum, 7.0, frame="utm", utm_zone=56, utm_north=False)
            assert math.hypot(x2 - x, y2 - y) < 1e-6


@pytest.mark.unit
class TestEnuFrame:
    """Sim-style local coordinates: east/north in the tangent plane at the datum."""

    @pytest.mark.parametrize("lat0,lon0,dist,az,lat,lon,east,north", ENU_CASES)
    def test_matches_pyproj_ecef(self, lat0, lon0, dist, az, lat, lon, east, north):
        x, y = gps_to_local(lat, lon, lat0, lon0, 0.0, frame="enu")
        assert math.hypot(x - east, y - north) < 1e-3
        la, lo = local_to_gps(east, north, lat0, lon0, 0.0, frame="enu")
        assert _metres(lat, lon, la, lo) < 1e-3

    @pytest.mark.parametrize("lat0,lon0,dist,az,lat,lon,east,north", ENU_CASES)
    def test_close_to_geodesic(self, lat0, lon0, dist, az, lat, lon, east, north):
        """A geodesic of length d at azimuth az lands at d*(sin az, cos az) in the plane."""
        x, y = gps_to_local(lat, lon, lat0, lon0, frame="enu")
        gx, gy = dist * math.sin(math.radians(az)), dist * math.cos(math.radians(az))
        limit = 0.01 if dist <= 100.0 else 0.5
        assert math.hypot(x - gx, y - gy) < limit

    def test_missing_frame_is_enu(self):
        lat0, lon0, _, _, lat, lon, east, north = ENU_CASES[2]
        assert gps_to_local(lat, lon, lat0, lon0, 0.0, None) == \
            gps_to_local(lat, lon, lat0, lon0, 0.0, "enu")
        x, y = gps_to_local(lat, lon, lat0, lon0)
        assert math.hypot(x - east, y - north) < 1e-3

    def test_origin_maps_to_zero(self):
        assert latlon_to_enu(*BUDAPEST, *BUDAPEST) == (0.0, 0.0)
        la, lo = enu_to_latlon(0.0, 0.0, *BUDAPEST)
        assert _metres(*BUDAPEST, la, lo) < 1e-9

    def test_bearing_convention(self):
        """bearing = angle of +X from east, CCW: 90 means +X north and +Y west."""
        lat0, lon0, _, _, lat_n, lon_n, _, north = ENU_CASES[0]  # 100 m due north
        x, y = gps_to_local(lat_n, lon_n, lat0, lon0, 90.0)
        assert abs(x - north) < 1e-3 and abs(y) < 1e-3
        lat0, lon0, _, _, lat_e, lon_e, east, _ = ENU_CASES[2]  # 100 m due east
        x, y = gps_to_local(lat_e, lon_e, lat0, lon0, 90.0)
        assert abs(x) < 1e-3 and abs(y + east) < 1e-3  # east is -Y

    @pytest.mark.parametrize("x,y,bearing", [
        (0.0, 0.0, 0.0), (100.0, 200.0, 0.0), (-50.0, 75.0, 30.0), (4000.0, -3000.0, 90.0)])
    @pytest.mark.parametrize("datum", [BUDAPEST, (-33.8688, 151.2093), (64.1466, -21.9426)])
    def test_local_round_trip(self, x, y, bearing, datum):
        lat, lon = local_to_gps(x, y, *datum, bearing)
        x2, y2 = gps_to_local(lat, lon, *datum, bearing)
        assert math.hypot(x2 - x, y2 - y) < 1e-6


@pytest.mark.unit
class TestFrameNames:
    def test_normalize(self):
        assert normalize_frame(None) == "enu"
        assert normalize_frame("") == "enu"
        assert normalize_frame("UTM") == "utm"
        assert normalize_frame("enu") == "enu"

    def test_unknown_frame_rejected(self):
        with pytest.raises(ValueError):
            gps_to_local(47.0, 8.0, 47.0, 8.0, frame="lla")
