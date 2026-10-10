"""Recording level per mission and the one-off raise for the next run:

- policy: next-run level > mission level > robot > site > global > default; unset changes nothing;
- the fleet recorder: a run records at the mission's level (run row, events, legs/track gating),
  the one-off raise is consumed exactly once when a run starts and recorded on the run row, and
  the robot follows its normal level again once the run is over;
- the API: the mission accepts and returns both fields, POST/DELETE .../recording/next-run.
"""
import datetime
import json
from types import SimpleNamespace
from unittest.mock import patch

import pydantic
import pytest
from fastapi import HTTPException

pytest.importorskip("psycopg")

import cloud_common.objects as api_objects  # noqa: E402
from cloud_common.objects import mission as mission_object  # noqa: E402
from packages.events.schemas import RecordingLevel  # noqa: E402
from packages.telemetry_ingest import tables  # noqa: E402
from packages.telemetry_ingest.policy import PolicySources, RecordingPolicy, resolve  # noqa: E402
from tests.unit.fleet_recorder_fakes import RefusedError, make_recorder  # noqa: E402
from tests.unit.test_fleet_recorder_policy import _mission, _robot, _settings  # noqa: E402

pytestmark = pytest.mark.unit
State = mission_object.MissionStateV1
Level = RecordingLevel


def _m(name="m1", level=None, next_run=None):
    mission = _mission(name)
    mission.telemetry_recording = level
    mission.telemetry_recording_next_run = next_run
    return mission


# --- policy ---------------------------------------------------------------------------------

class TestResolution:
    def test_order(self):
        assert resolve("track", "off", "full") is Level.TRACK                  # robot
        assert resolve(None, "off", "full", mission_level="events_only") is Level.EVENTS_ONLY
        assert resolve("track", "off", "full", mission_level="full") is Level.FULL
        assert resolve("track", "off", "full", mission_level="events_only",
                       next_run_level="off") is Level.OFF                      # one-off wins

    def test_unset_changes_nothing(self):
        assert resolve("track", "off", "full") is resolve("track", "off", "full", None, None)
        assert resolve() is Level.EVENTS_ONLY

    def test_policy_run_level_and_inherited(self):
        policy = RecordingPolicy(sources=PolicySources(robot_levels={"r1": "track"},
                                                       global_level="full"))
        assert policy.level_for("r1") is Level.TRACK
        policy.set_run_level("r1", Level.OFF)
        assert policy.level_for("r1") is Level.OFF
        assert not policy.allows(tables.EVENTS_TABLE, "r1")
        assert policy.level_for("r2") is Level.FULL                            # other robots
        assert policy.inherited_level("r1") is Level.TRACK                     # sources alone
        assert policy.inherited_level("r1", "events_only") is Level.EVENTS_ONLY
        policy.set_run_level("r1", None)
        assert policy.level_for("r1") is Level.TRACK

    def test_run_level_survives_a_reload_and_goes_with_a_deleted_robot(self):
        policy = RecordingPolicy(sources=PolicySources())
        policy.set_run_level("r1", Level.FULL)
        policy.replace_sources(PolicySources(global_level="off"))
        assert policy.level_for("r1") is Level.FULL
        policy.forget_robot("r1")
        assert policy.level_for("r1") is Level.OFF


# --- the mission fields ---------------------------------------------------------------------

class TestMissionFields:
    def test_default_unset(self):
        mission = _mission("m1")
        assert mission.telemetry_recording is None
        assert mission.telemetry_recording_next_run is None

    @pytest.mark.parametrize("level", ["full", "track", "events_only", "off"])
    def test_accepts_each_level(self, level):
        mission = _m(level=level, next_run=level)
        spec = json.loads(mission.spec.json())
        assert spec["telemetry_recording"] == spec["telemetry_recording_next_run"] == level

    @pytest.mark.parametrize("field", ["telemetry_recording", "telemetry_recording_next_run"])
    @pytest.mark.parametrize("bad", ["FULL", "debug", "", 3])
    def test_rejects_unknown_levels(self, field, bad):
        with pytest.raises(pydantic.ValidationError):
            api_objects.MissionObjectV1(
                name="m", robot="r1", status={}, **{field: bad},
                mission_tree=[{"name": "go", "route": {"waypoints": [
                    {"x": 1.0, "y": 1.0, "theta": 0.0}]}}])

    def test_level_is_editable_the_next_run_is_not(self):
        assert "telemetry_recording" in mission_object.EDITABLE_SPEC_FIELDS
        assert "telemetry_recording_next_run" not in mission_object.EDITABLE_SPEC_FIELDS


# --- the fleet recorder ---------------------------------------------------------------------

async def _go(rec, mission, robot, finish=True, robot_name="r1"):
    rec.run_started(robot_name, mission, robot)
    if finish:
        mission.status.state = State.COMPLETED
        rec.run_finished(robot_name, mission, robot)
    await rec.run_pending_ops()


def _row(db, name):
    (row,) = [r for r in db.runs.values() if r["mission_name"] == name]
    return row


def _run_events(db, row):
    return sorted(r["code"] for r in db.events.values() if r["run_id"] == row["run_id"])


async def test_mission_level_beats_robot_site_and_global(tmp_path):
    rec, db, _ = make_recorder(tmp_path, global_level="full")
    robot = _robot("track")
    rec.on_robot_object(robot)
    await _go(rec, _m("a", level="events_only"), robot)
    assert _row(db, "a")["recording_level"] == "events_only"
    await _go(rec, _m("b"), robot)                                  # unset: the robot's level
    assert _row(db, "b")["recording_level"] == "track"


async def test_unset_changes_nothing(tmp_path):
    rec, db, _ = make_recorder(tmp_path, global_level="off")
    robot = _robot()
    rec.run_started("r1", _m("a"), robot)
    assert rec.policy.snapshot()["run_levels"] == {}
    rec.on_settings_object(_settings("full"))     # a settings change still applies mid-run
    assert rec.policy.level_for("r1") is Level.FULL
    await rec.run_pending_ops()
    assert _row(db, "a")["recording_level"] == "off"   # fixed when the run started


async def test_mission_level_governs_the_robot_during_the_run_only(tmp_path):
    rec, db, _ = make_recorder(tmp_path, global_level="events_only")
    robot = _robot()
    mission = _m("a", level="off")
    rec.run_started("r1", mission, robot)
    assert rec.policy.level_for("r1") is Level.OFF
    assert not rec.policy.allows(tables.EVENTS_TABLE, "r1")
    mission.status.state = State.COMPLETED
    rec.run_finished("r1", mission, robot)
    assert rec.policy.level_for("r1") is Level.EVENTS_ONLY           # back to the sources
    await rec.run_pending_ops()
    assert _row(db, "a")["recording_level"] == "off"
    assert _run_events(db, _row(db, "a")) == []                       # off: no run events


async def test_mission_level_raises_above_the_robot(tmp_path):
    rec, db, _ = make_recorder(tmp_path, global_level="events_only")
    rec.run_started("r1", _m("a", level="track"), _robot())
    assert rec.policy.allows(tables.TRACK_TABLE, "r1")
    await rec.run_pending_ops()
    assert _row(db, "a")["recording_level"] == "track"


async def test_next_run_beats_the_mission_level_and_is_consumed_once(tmp_path):
    rec, db, _ = make_recorder(tmp_path, global_level="events_only")
    robot = _robot()
    db.next_run["a"] = "full"
    first = _m("a", level="off", next_run="full")
    await _go(rec, first, robot, finish=False)
    row = _row(db, "a")
    assert row["recording_level"] == "full"                           # what was actually used
    assert db.next_run == {}                                          # cleared
    assert rec.policy.level_for("r1") is Level.FULL
    assert "MISSION.RUN_STARTED" in _run_events(db, row)
    first.status.state = State.COMPLETED
    rec.run_finished("r1", first, robot)
    await rec.run_pending_ops()
    assert rec.policy.level_for("r1") is Level.EVENTS_ONLY
    # the next run is back at the mission level
    rec.run_started("r1", _m("a2", level="off"), robot)
    await rec.run_pending_ops()
    assert _row(db, "a2")["recording_level"] == "off"


async def test_two_starts_cannot_both_use_the_raise(tmp_path):
    rec, db, _ = make_recorder(tmp_path, global_level="events_only")
    db.next_run["same"] = "full"
    second = _m("same")
    second.status.run_id = "other"          # another run of the mission: its own run row
    rec.run_started("r1", _m("same"), _robot())
    rec.run_started("r2", second, _robot(name="r2"))
    await rec.run_pending_ops()
    levels = sorted(r["recording_level"] for r in db.runs.values())
    assert levels == ["events_only", "full"]


async def test_a_refused_event_retry_uses_the_raise_once(tmp_path):
    rec, db, _ = make_recorder(tmp_path, global_level="events_only")
    db.next_run["a"] = "full"
    refused = []

    def fail(sql, params):
        if "fleet_events" in sql and not refused:
            refused.append(sql)
            return RefusedError("boom")
        return None
    db.fail = fail
    rec.run_started("r1", _m("a"), _robot())
    await rec.run_pending_ops()
    # the first attempt rolled back (the raise with it); the retry used it
    row = _row(db, "a")
    assert refused and row["recording_level"] == "full" and db.next_run == {}


async def test_one_off_off_silences_run_events(tmp_path):
    rec, db, _ = make_recorder(tmp_path, global_level="full")
    db.next_run["a"] = "off"
    await _go(rec, _m("a"), _robot())
    row = _row(db, "a")
    assert row["recording_level"] == "off"
    assert _run_events(db, row) == []


async def test_a_resumed_run_keeps_its_stored_level(tmp_path):
    rec, db, _ = make_recorder(tmp_path, global_level="events_only")
    robot = _robot()
    db.next_run["a"] = "full"
    mission = _m("a")
    rec.run_started("r1", mission, robot)
    await rec.run_pending_ops()
    # a dispatcher restart: a new recorder resumes the same mission
    rec2, _, _ = make_recorder(tmp_path, db=db, global_level="events_only", name="spill2.jsonl")
    mission.status.start_timestamp = datetime.datetime(2026, 9, 24, 12, 0,
                                                       tzinfo=datetime.timezone.utc)
    rec2.run_started("r1", mission, robot)
    await rec2.run_pending_ops()
    assert rec2.policy.level_for("r1") is Level.FULL
    assert len(db.runs) == 1


# --- the API --------------------------------------------------------------------------------

class _Db:
    def __init__(self, mission):
        self.row = mission
        self.writes = []

    async def get_object(self, cls, name):
        return cls(name=name, lifecycle=self.row.lifecycle, status=self.row.status.dict(),
                   **json.loads(self.row.spec.json()))

    async def update_spec_fields(self, cls, name, fields, publisher_id):
        self.writes.append(fields)
        self.row = cls(name=name, lifecycle=self.row.lifecycle, status=self.row.status.dict(),
                       **{**json.loads(self.row.spec.json()), **fields})


def _api(mission):
    import packages.api.main as api_main
    db = _Db(mission)
    return api_main, db, patch.object(api_main, "service", SimpleNamespace(database=db))


async def test_api_set_and_cancel_next_run():
    api_main, db, ctx = _api(_m("m1", level="track"))
    with ctx:
        out = await api_main.set_mission_next_run_recording(
            "m1", api_main.MissionNextRunRecording(level="full"))
        assert out == {"mission": "m1", "telemetry_recording": "track",
                       "telemetry_recording_next_run": "full"}
        got = await api_main.get_mission("m1")
        assert got["telemetry_recording_next_run"] == "full"
        out = await api_main.cancel_mission_next_run_recording("m1")
        assert out["cancelled"] is True and out["telemetry_recording_next_run"] is None
        again = await api_main.cancel_mission_next_run_recording("m1")
        assert again["cancelled"] is False
    assert db.writes == [{"telemetry_recording_next_run": "full"},
                         {"telemetry_recording_next_run": None}]


async def test_api_next_run_needs_a_pending_mission():
    mission = _m("m1")
    mission.status.state = State.RUNNING
    api_main, db, ctx = _api(mission)
    with ctx, pytest.raises(HTTPException) as err:
        await api_main.set_mission_next_run_recording(
            "m1", api_main.MissionNextRunRecording(level="full"))
    assert err.value.status_code == 409 and db.writes == []


def test_api_next_run_body_validates_the_level():
    import packages.api.main as api_main
    with pytest.raises(pydantic.ValidationError):
        api_main.MissionNextRunRecording(level="debug")
    with pytest.raises(pydantic.ValidationError):
        api_main.MissionNextRunRecording()


async def test_api_put_level_validated_and_next_run_not_editable():
    api_main, db, ctx = _api(_m("m1"))
    with ctx:
        out = await api_main.update_mission("m1", {"telemetry_recording": "off"})
        assert out["telemetry_recording"] == "off"
        out = await api_main.update_mission("m1", {"telemetry_recording": None})
        assert out["telemetry_recording"] is None
        with pytest.raises(HTTPException) as err:
            await api_main.update_mission("m1", {"telemetry_recording": "debug"})
        assert err.value.status_code == 400
        with pytest.raises(HTTPException) as err:
            await api_main.update_mission("m1", {"telemetry_recording_next_run": "full"})
        assert err.value.status_code == 400
