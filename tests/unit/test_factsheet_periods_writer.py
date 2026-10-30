"""The recorder writes a robot_factsheet_periods row only when a received factsheet's figures changed (utils/factsheet_periods.py)."""
import datetime

import pytest

pytest.importorskip("psycopg")

from packages.controllers.mission.vda5050_types import vda5050_types as types  # noqa: E402
from packages.utils import factsheet_periods as fp  # noqa: E402
from tests.unit.fleet_recorder_fakes import T0, make_recorder  # noqa: E402

pytestmark = pytest.mark.unit


def factsheet(ts=T0, *, speed=0.24, angular=1.0, accel=None, length=0.8, width=0.5, agv="CARRIER"):
    return types.VDA5050Factsheet(
        headerId=1, timestamp=ts.isoformat() if ts is not None else "",
        typeSpecification=types.VDA5050TypeSpecification(agvClass=agv),
        physicalParameters=types.VDA5050PhysicalParameters(
            speedMax=speed, angularSpeedMax=angular, accelerationMax=accel, length=length, width=width))


async def receive(rec, message, robot="r1"):
    rec.on_factsheet(robot, message)
    await rec.run_pending_ops()


def rows(db, robot="r1"):
    return [v for k, v in sorted(db.periods.items()) if k[0] == robot]


async def test_first_factsheet_records_a_period_at_its_header_time(tmp_path):
    rec, db, clock = make_recorder(tmp_path)
    clock.advance(3600)
    await receive(rec, factsheet(T0))
    (row,) = rows(db)
    assert row["valid_from"] == T0 and row["source"] == "factsheet"
    assert (row["agv_class"], row["speed_max"], row["angular_speed_max"], row["length"], row["width"]) == \
        ("CARRIER", 0.24, 1.0, 0.8, 0.5)
    assert row["acceleration_max"] is None


async def test_same_values_twice_and_retained_redelivery_write_nothing(tmp_path):
    rec, db, clock = make_recorder(tmp_path)
    await receive(rec, factsheet(T0))
    clock.advance(600)
    await receive(rec, factsheet(T0 + datetime.timedelta(seconds=600)))     # same figures, new header
    await receive(rec, factsheet(T0))                                       # retained message re-delivered on reconnect
    assert len(rows(db)) == 1


async def test_a_change_adds_a_period(tmp_path):
    rec, db, clock = make_recorder(tmp_path)
    await receive(rec, factsheet(T0, speed=0.24))
    later = T0 + datetime.timedelta(days=2)
    clock.advance(3 * 86400)
    await receive(rec, factsheet(later, speed=0.36))
    first, second = rows(db)
    assert (first["speed_max"], second["speed_max"], second["valid_from"]) == (0.24, 0.36, later)
    clock.advance(10)
    await receive(rec, factsheet(later, speed=0.36))
    assert len(rows(db)) == 2


async def test_missing_or_nonsense_values_keep_the_stored_ones(tmp_path):
    rec, db, clock = make_recorder(tmp_path)
    await receive(rec, factsheet(T0, accel=0.5))
    clock.advance(60)
    # no angular speed, acceleration -1 and a negative width: the dispatcher keeps what it stored, so nothing changed
    await receive(rec, factsheet(clock(), angular=None, accel=-1, width=-1))
    assert len(rows(db)) == 1


async def test_placeholder_factsheet_is_recorded_with_unknown_dimensions_as_null(tmp_path):
    rec, db, _ = make_recorder(tmp_path)
    await receive(rec, factsheet(T0, speed=1.0, angular=None, length=-1, width=-1))
    (row,) = rows(db)
    assert row["speed_max"] == 1.0 and row["length"] is None and row["width"] is None


async def test_future_header_time_uses_the_received_time(tmp_path):
    rec, db, clock = make_recorder(tmp_path)
    await receive(rec, factsheet(clock() + datetime.timedelta(hours=5)))
    assert rows(db)[0]["valid_from"] == clock()
    await receive(rec, factsheet(clock() + datetime.timedelta(seconds=30), speed=0.5))   # inside the 60 s tolerance
    assert rows(db)[1]["valid_from"] == clock() + datetime.timedelta(seconds=30)


async def test_header_time_not_after_the_latest_period_uses_the_received_time(tmp_path):
    rec, db, clock = make_recorder(tmp_path)
    await receive(rec, factsheet(T0))
    clock.advance(100)
    await receive(rec, factsheet(T0 - datetime.timedelta(days=1), speed=0.5))
    assert rows(db)[1]["valid_from"] == clock()


def test_differs_compares_after_the_rounding_noise():
    base = {"agv_class": "C", "speed_max": 0.24, "angular_speed_max": None, "acceleration_max": None, "length": 1.0, "width": 0.5}
    assert not fp.differs(base, dict(base, speed_max=0.24 + 1e-12))
    assert fp.differs(base, dict(base, angular_speed_max=1.0))
    assert fp.differs(None, base)
