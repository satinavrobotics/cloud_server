"""Row builders shared by the telemetry_ingest tests."""

import datetime

from packages.events.codes import EventCode
from packages.events.emit import Event

UTC = datetime.timezone.utc
T0 = datetime.datetime(2026, 9, 24, 12, 0, 0, tzinfo=UTC)


def event(i: int, robot: str = "r1", code: EventCode = EventCode.ROBOT_STATE_CHANGED) -> Event:
    ts = T0 + datetime.timedelta(seconds=i)
    if code is EventCode.TELEMETRY_RECORDING_CHANGED:
        return Event(code, ts, robot_name=robot, discriminator=f"robot:{robot}",
                     payload={"old_level": "events_only", "new_level": "off",
                              "scope": "robot", "scope_id": robot, "actor": "tester"})
    return Event(code, ts, robot_name=robot, payload={"old": "IDLE", "new": f"S{i}"})


def state_row(i: int, robot: str = "r1", **extra):
    row = {"ts": T0 + datetime.timedelta(seconds=i), "robot_name": robot,
           "x": float(i), "y": 2.0, "yaw": 0.5, "battery": 80.0, "state": "DRIVING",
           "driving": True}
    row.update(extra)
    return row


def diag_row(i: int, robot: str = "r1", **extra):
    row = {"ts": T0 + datetime.timedelta(seconds=i), "robot_name": robot,
           "cpu": 10.0, "temp_max": 60.0, "gnss_fix": "RTK_FIXED", "gnss_sats": 18}
    row.update(extra)
    return row
