"""Unit tests for packages/utils/geo.py — GPS ↔ local Cartesian conversions."""

import math
import pytest

from packages.utils.geo import gps_to_local, local_to_gps, EARTH_RADIUS_M


@pytest.mark.unit
class TestGpsToLocal:
    """Tests for gps_to_local()."""

    def test_origin_maps_to_zero(self):
        """The datum origin must map to (0, 0)."""
        x, y = gps_to_local(47.0, 8.0, datum_lat=47.0, datum_lon=8.0)
        assert abs(x) < 1e-6
        assert abs(y) < 1e-6

    def test_north_displacement(self):
        """A point due north of the origin should produce positive y, zero x (bearing=0)."""
        dlat_deg = 0.001  # ~111 m north
        x, y = gps_to_local(47.0 + dlat_deg, 8.0, datum_lat=47.0, datum_lon=8.0, datum_bearing_deg=0.0)
        expected = math.radians(dlat_deg) * EARTH_RADIUS_M
        assert abs(y - expected) < 0.01
        assert abs(x) < 0.01

    def test_east_displacement(self):
        """A point due east of the origin should produce positive x, zero y (bearing=0)."""
        dlon_deg = 0.001
        x, y = gps_to_local(47.0, 8.0 + dlon_deg, datum_lat=47.0, datum_lon=8.0, datum_bearing_deg=0.0)
        expected = math.radians(dlon_deg) * EARTH_RADIUS_M * math.cos(math.radians(47.0))
        assert abs(x - expected) < 0.01
        assert abs(y) < 0.01

    def test_bearing_90_swaps_axes(self):
        """With bearing=90° the map +X aligns with west, +Y with north.
        A point due north should now appear at (x>0, y≈0)."""
        dlat_deg = 0.001
        x, y = gps_to_local(47.0 + dlat_deg, 8.0, datum_lat=47.0, datum_lon=8.0, datum_bearing_deg=90.0)
        assert x > 0
        assert abs(y) < 0.1

    def test_known_displacement(self):
        """Regression test for a known Zurich-area displacement."""
        x, y = gps_to_local(47.3770, 8.5420, datum_lat=47.3769, datum_lon=8.5417, datum_bearing_deg=0.0)
        # ~22 m east, ~11 m north (approximate)
        assert 20 < x < 25
        assert 8 < y < 14

    def test_negative_displacement(self):
        """A point south-west of the origin should give negative x and y."""
        x, y = gps_to_local(46.999, 7.999, datum_lat=47.0, datum_lon=8.0, datum_bearing_deg=0.0)
        assert x < 0
        assert y < 0


@pytest.mark.unit
class TestLocalToGps:
    """Tests for local_to_gps()."""

    def test_zero_maps_to_origin(self):
        """(0, 0) must map back to the datum."""
        lat, lon = local_to_gps(0.0, 0.0, datum_lat=47.0, datum_lon=8.0)
        assert abs(lat - 47.0) < 1e-9
        assert abs(lon - 8.0) < 1e-9

    def test_positive_x_moves_east(self):
        """Positive x with bearing=0 should increase longitude."""
        lat, lon = local_to_gps(100.0, 0.0, datum_lat=47.0, datum_lon=8.0, datum_bearing_deg=0.0)
        assert lon > 8.0
        assert abs(lat - 47.0) < 1e-6

    def test_positive_y_moves_north(self):
        """Positive y with bearing=0 should increase latitude."""
        lat, lon = local_to_gps(0.0, 100.0, datum_lat=47.0, datum_lon=8.0, datum_bearing_deg=0.0)
        assert lat > 47.0
        assert abs(lon - 8.0) < 1e-6


@pytest.mark.unit
class TestRoundTrip:
    """Round-trip consistency tests between gps_to_local and local_to_gps."""

    @pytest.mark.parametrize("lat,lon,bearing", [
        (47.3769, 8.5417, 0.0),
        (51.5074, -0.1278, 45.0),
        (35.6762, 139.6503, 12.5),
        (-33.8688, 151.2093, 270.0),
    ])
    def test_gps_round_trip(self, lat, lon, bearing):
        """GPS → local → GPS must recover the original coordinates to <0.01 m (~1e-7 deg)."""
        datum_lat, datum_lon = lat - 0.005, lon - 0.005
        x, y = gps_to_local(lat, lon, datum_lat, datum_lon, bearing)
        lat2, lon2 = local_to_gps(x, y, datum_lat, datum_lon, bearing)
        assert abs(lat2 - lat) < 1e-7, f"Latitude drift: {abs(lat2 - lat)}"
        assert abs(lon2 - lon) < 1e-7, f"Longitude drift: {abs(lon2 - lon)}"

    @pytest.mark.parametrize("x,y,bearing", [
        (0.0, 0.0, 0.0),
        (100.0, 200.0, 0.0),
        (-50.0, 75.0, 30.0),
        (500.0, -300.0, 90.0),
    ])
    def test_local_round_trip(self, x, y, bearing):
        """Local → GPS → local must recover original coordinates to <0.001 m."""
        datum_lat, datum_lon = 47.0, 8.0
        lat, lon = local_to_gps(x, y, datum_lat, datum_lon, bearing)
        x2, y2 = gps_to_local(lat, lon, datum_lat, datum_lon, bearing)
        assert abs(x2 - x) < 1e-3, f"X drift: {abs(x2 - x)}"
        assert abs(y2 - y) < 1e-3, f"Y drift: {abs(y2 - y)}"
