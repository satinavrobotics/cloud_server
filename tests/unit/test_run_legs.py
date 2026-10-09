"""Run legs (mission/run analysis phases 1-2): leg rows from VDA5050 state messages, expected
time from the factsheet limits, recording levels, event tagging, run summary_metrics and the two
leg endpoints."""
import datetime
import json
import math
import uuid
from unittest.mock import patch

import httpx
import pytest

pytest.importorskip("psycopg")

import cloud_common.objects as api_objects  # noqa: E402
from cloud_common.objects import mission as mission_object  # noqa: E402
from packages.api import fleet_reads, main  # noqa: E402
from packages.controllers.mission import fleet_recorder as fr  # noqa: E402
from packages.events import schemas  # noqa: E402
from packages.events.codes import EventCode  # noqa: E402
from packages.utils import run_legs  # noqa: E402
from tests.unit.fleet_recorder_fakes import T0, make_recorder, state  # noqa: E402
from tests.unit.test_fleet_reads import FakeDb  # noqa: E402

pytestmark = pytest.mark.unit
State = mission_object.MissionStateV1
RUN = "abcd1234"


@pytest.fixture(autouse=True)
def _strict_payloads():
    previous = schemas.strict_validation()
    schemas.set_strict_validation(True)
    yield
    schemas.set_strict_validation(previous)


def at(seconds):
    return T0 + datetime.timedelta(seconds=seconds)


def make_mission(name="m1", run_id=RUN, planned=("A", "B", "C"), repeat=1,
                 points=((0, 0, 0.0), (3, 0, 0.0), (3, 4, math.pi / 2))):
    mission = api_objects.MissionObjectV1(
        name=name, robot="r1", status={}, timeout=600, repeat=repeat,
        planned_path=list(planned) if planned else None,
        mission_tree=[{"name": "go", "route": {"waypoints": [
            {"x": x, "y": y, "theta": th, "map_id": "map1"} for x, y, th in points]}}])
    mission.status.run_id = run_id
    return mission


def make_robot(speed=1.0, accel=0.5, angular=1.0):
    return api_objects.RobotObjectV1(
        name="r1", status={"factsheet": {"speed_max": speed, "acceleration_max": accel,
                                         "angular_speed_max": angular}})


def node(mission, idx, seq):
    return f"{mission.name}-r{mission.status.run_id}" + \
        (f"v{mission.status.order_rev}" if mission.status.order_rev else "") + f"-n{idx}-s{seq}"


def order(mission, idx=0):
    return node(mission, idx, 0).rsplit("-s", 1)[0]


def msg(mission, seq, t, idx=0, driving=True):
    return state(at(t), order_id=order(mission, idx),
                 last_node="" if seq is None else node(mission, idx, seq), driving=driving)


async def start(tmp_path, mission=None, robot=None, level=None):
    rec, db, clock = make_recorder(tmp_path, global_level=level)
    mission = mission or make_mission()
    robot = robot or make_robot()
    rec.run_started("r1", mission, robot, session_map="map1")
    await rec.run_pending_ops()
    return rec, db, mission, robot


async def feed(rec, mission, robot, *messages):
    for message in messages:
        rec.on_leg_state("r1", message, mission, robot)
    await rec.run_pending_ops()


# --- pure helpers --------------------------------------------------------------------------

class TestExpectedSeconds:
    def test_full_limits(self):
        # 4 m at 1 m/s + 1/0.5 + 1.0 rad at 2 rad/s
        assert run_legs.expected_seconds(4.0, 1.0, 1.0, 0.5, 2.0) == pytest.approx(6.5)

    def test_heading_is_wrapped_and_absolute(self):
        assert run_legs.expected_seconds(0.0, -3 * math.pi / 2, 1.0, 1.0, 1.0) == \
            pytest.approx(1.0 + math.pi / 2, abs=1e-3)

    def test_unknown_limits_drop_their_term(self):
        assert run_legs.expected_seconds(4.0, 1.0, 1.0, -1, -1) == pytest.approx(4.0)
        assert run_legs.expected_seconds(4.0, 1.0, -1, 0.5, 2.0) == pytest.approx(0.5)
        assert run_legs.expected_seconds(4.0, 1.0, 2.0, -1, 2.0) == pytest.approx(2.5)

    def test_nothing_known_is_none(self):
        assert run_legs.expected_seconds(4.0, 1.0, -1, -1, -1) is None
        assert run_legs.expected_seconds(None, None, 1.0, -1, -1) is None


class TestAggregate:
    def legs(self, durations, src="A", dst="B", **extra):
        return [{"run_id": i, "from_topomap_node": src, "to_topomap_node": dst,
                 "duration_s": d, "expected_s": 4.0, "recoveries": 0, **extra}
                for i, d in enumerate(durations)]

    def test_median_p90_ratio(self):
        [item] = run_legs.aggregate(self.legs([2, 4, 6, 8, 10]))
        assert item["count"] == 5 and item["median_s"] == 6.0
        assert item["p90_s"] == pytest.approx(9.2)        # 8 + 0.6 * 2
        assert item["expected_s"] == 4.0 and item["ratio"] == 1.5 and item["topomap"] is True

    def test_groups_and_orders_by_median_and_sums_recoveries(self):
        legs = self.legs([10, 12], "A", "B", recoveries=1, recovery_s=3.0) \
            + self.legs([1, 2, 3], "B", "C")
        a, b = run_legs.aggregate(legs)
        assert (a["from"], a["to"], a["count"], a["recoveries"], a["recovery_s"]) == \
            ("A", "B", 2, 2, 6.0)
        assert (b["from"], b["to"], b["median_s"]) == ("B", "C", 2.0)

    def test_falls_back_to_the_run_independent_vda_tail(self):
        legs = [{"run_id": i, "duration_s": 5.0, "from_vda_node": f"m-r{i}-n0-s0",
                 "to_vda_node": f"m-r{i}v1-n0-s2"} for i in ("a", "b")]
        [item] = run_legs.aggregate(legs)
        assert (item["from"], item["to"], item["count"], item["runs"], item["topomap"]) == \
            ("n0-s0", "n0-s2", 2, 2, False)
        assert item["expected_s"] is None and item["ratio"] is None

    def test_percentile_single(self):
        assert run_legs.percentile([3.0], 90) == 3.0 and run_legs.percentile([], 90) is None


# --- leg rows from state messages ----------------------------------------------------------

class TestLegRows:
    async def test_legs_from_a_state_sequence(self, tmp_path):
        rec, db, mission, robot = await start(tmp_path)
        await feed(rec, mission, robot,
                   msg(mission, None, 0),                  # order echoed, nothing reached
                   msg(mission, 0, 1),                     # the start node
                   msg(mission, 2, 5), msg(mission, 2, 6),   # waypoint 0 (twice: one reach)
                   msg(mission, 4, 12), msg(mission, 6, 20))
        rows = db.legs_of()
        assert [(r["seq"], r["from_topomap_node"], r["to_topomap_node"]) for r in rows] == \
            [(1, None, "A"), (2, "A", "B"), (3, "B", "C")]
        first, second, third = rows
        assert first["from_vda_node"] == node(mission, 0, 0) and \
            first["to_vda_node"] == node(mission, 0, 2)
        assert (first["started_at"], first["ended_at"]) == (at(1), at(5))
        assert first["duration_s"] == 4.0 and first["straight_m"] is None
        assert first["expected_s"] is None                 # no pose at the start node
        assert (second["duration_s"], second["straight_m"], second["planned_m"]) == (7.0, 3.0, 3.0)
        # 3 m / 1 m/s + 1 / 0.5 + 0 rad
        assert second["expected_s"] == pytest.approx(5.0)
        assert third["straight_m"] == 4.0
        assert third["expected_s"] == pytest.approx(4.0 + 2.0 + (math.pi / 2) / 1.0, abs=1e-3)
        assert all(r["mission_name"] == "m1" and r["robot_name"] == "r1" and
                   r["map_id"] == "map1" and r["pass_index"] == 0 for r in rows)
        assert second["received_ended_at"] is not None and second["order_rev"] == 0

    async def test_no_topomap_ids_without_a_matching_planned_path(self, tmp_path):
        mission = make_mission(planned=("A", "B"))        # 3 waypoints, 2 nodes: no mapping
        rec, db, mission, robot = await start(tmp_path, mission)
        await feed(rec, mission, robot, msg(mission, 0, 0), msg(mission, 2, 3), msg(mission, 4, 6))
        [first, second] = db.legs_of()
        assert second["to_topomap_node"] is None and second["to_vda_node"] == node(mission, 0, 4)

    async def test_missing_factsheet_limits_leave_expected_null(self, tmp_path):
        rec, db, mission, robot = await start(
            tmp_path, robot=api_objects.RobotObjectV1(name="r1", status={}))
        await feed(rec, mission, robot, msg(mission, 0, 0), msg(mission, 2, 3), msg(mission, 4, 6))
        assert [r["expected_s"] for r in db.legs_of()] == [None, None]
        assert db.legs_of()[1]["straight_m"] == 3.0

    async def test_partial_limits(self, tmp_path):
        rec, db, mission, robot = await start(tmp_path, robot=make_robot(1.0, -1, -1))
        await feed(rec, mission, robot, msg(mission, 0, 0), msg(mission, 2, 3), msg(mission, 4, 6))
        assert db.legs_of()[1]["expected_s"] == pytest.approx(3.0)

    async def test_stopped_time_is_the_non_driving_part(self, tmp_path):
        rec, db, mission, robot = await start(tmp_path)
        await feed(rec, mission, robot,
                   msg(mission, 0, 0), msg(mission, 2, 2),
                   msg(mission, 2, 4, driving=False), msg(mission, 2, 9, driving=True),
                   msg(mission, 4, 12))
        assert db.legs_of()[1]["duration_s"] == 10.0 and db.legs_of()[1]["stopped_s"] == 5.0

    async def test_repeat_passes(self, tmp_path):
        mission = make_mission(repeat=2)
        rec, db, mission, robot = await start(tmp_path, mission)
        await feed(rec, mission, robot, msg(mission, 0, 0), msg(mission, 2, 3),
                   msg(mission, 4, 6), msg(mission, 6, 9))
        # the dispatcher starts pass 2: a new run id for the status, passes_completed + 1
        mission.status.passes_completed = 1
        mission.status.run_id = "ffff0000"
        mission.status.order_rev = 0
        await feed(rec, mission, robot, msg(mission, 0, 20), msg(mission, 2, 22),
                   msg(mission, 4, 25))
        rows = db.legs_of()
        assert [(r["seq"], r["pass_index"]) for r in rows] == \
            [(1, 0), (2, 0), (3, 0), (4, 1), (5, 1)]
        assert rows[3]["from_vda_node"].endswith("-n0-s0") and rows[3]["duration_s"] == 2.0
        # the last node of pass 1 to the first of pass 2 is not a leg
        assert rows[3]["from_topomap_node"] is None and rows[3]["to_topomap_node"] == "A"

    async def test_a_reroute_resend_does_not_double_count(self, tmp_path):
        rec, db, mission, robot = await start(tmp_path)
        await feed(rec, mission, robot, msg(mission, 0, 0), msg(mission, 2, 3))
        # mid-leg towards waypoint 1 the route is rewritten: new revision, planned_path cleared
        mission.status.order_rev = 1
        mission.planned_path = None
        mission.status.applied_route_rev = mission.route_rev = 1
        await feed(rec, mission, robot,
                   msg(mission, 0, 8),                # the new order's start node: not a reach
                   msg(mission, 0, 9),                # echoed again
                   msg(mission, 2, 14))
        rows = db.legs_of()
        assert [r["seq"] for r in rows] == [1, 2]
        resumed = rows[1]
        # one leg from the node reached before the reroute, spanning it
        assert resumed["from_vda_node"] == node(make_mission(), 0, 2)
        assert resumed["to_vda_node"].endswith("v1-n0-s2") and resumed["order_rev"] == 1
        assert (resumed["started_at"], resumed["ended_at"]) == (at(3), at(14))
        assert resumed["from_topomap_node"] == "A" and resumed["to_topomap_node"] is None

    async def test_messages_of_a_cancelled_revision_are_ignored(self, tmp_path):
        rec, db, mission, robot = await start(tmp_path)
        await feed(rec, mission, robot, msg(mission, 0, 0), msg(mission, 2, 3))
        stale = msg(mission, 4, 5)
        mission.status.order_rev = 1
        await feed(rec, mission, robot, stale)
        assert len(db.legs_of()) == 1             # only the one before the revision changed

    async def test_dispatcher_restart_continues_the_numbering(self, tmp_path):
        rec, db, mission, robot = await start(tmp_path)
        await feed(rec, mission, robot, msg(mission, 0, 0), msg(mission, 2, 3), msg(mission, 4, 6))
        assert [r["seq"] for r in db.legs_of()] == [1, 2]
        # a new dispatcher process adopts the RUNNING run
        rec2, _, _ = make_recorder(tmp_path, db=db, name="spill2.jsonl")
        mission.status.start_timestamp = at(0)
        mission.status.run_id = RUN
        rec2.run_started("r1", mission, robot)
        await rec2.run_pending_ops()
        await feed(rec2, mission, robot, msg(mission, 6, 10), msg(mission, 6, 11))
        await feed(rec2, mission, robot)
        assert [r["seq"] for r in db.legs_of()] == [1, 2]   # first node seen only anchors
        rec2.on_leg_state("r1", msg(mission, 2, 20), mission, robot)
        await rec2.run_pending_ops()
        assert [r["seq"] for r in db.legs_of()] == [1, 2, 3]

    async def test_level_off_writes_nothing_events_only_and_full_do(self, tmp_path):
        for level, expected in (("off", 0), ("events_only", 2), ("full", 2)):
            rec, db, mission, robot = await start(tmp_path, level=level)
            await feed(rec, mission, robot, msg(mission, 0, 0), msg(mission, 2, 3),
                       msg(mission, 4, 6))
            assert len(db.legs_of()) == expected, level
            assert rec.leg_seq("r1") == (None if expected == 0 else 3)

    async def test_recording_failure_never_raises_into_dispatch(self, tmp_path):
        rec, db, mission, robot = await start(tmp_path)
        db.fail = lambda sql, params: fr and (
            Exception("boom") if sql == fr.INSERT_LEG_SQL else None)
        rec.on_leg_state("r1", object(), mission, robot)          # garbage message: swallowed
        assert rec.hook_errors == 1
        await feed(rec, mission, robot, msg(mission, 0, 0), msg(mission, 2, 3))
        assert db.legs_of() == [] and rec.op_failures == 1
        assert db.runs                                            # the run row is unaffected

    async def test_planned_path_is_copied_onto_the_run(self, tmp_path):
        rec, db, mission, robot = await start(tmp_path)
        [run] = db.runs.values()
        assert run["planned_path"] == ["A", "B", "C"]

    async def test_a_mission_without_a_run_records_no_legs(self, tmp_path):
        rec, db, clock = make_recorder(tmp_path)
        mission = make_mission()
        rec.on_leg_state("r1", msg(mission, 2, 3), mission, make_robot())
        await rec.run_pending_ops()
        assert db.legs_of() == []


# --- events tagged with the leg; run summary -----------------------------------------------

class TestEventsAndSummary:
    async def test_leg_seq_follows_the_leg_in_progress(self, tmp_path):
        rec, db, mission, robot = await start(tmp_path)
        assert rec.leg_seq("r1") is None
        await feed(rec, mission, robot, msg(mission, 0, 0))
        assert rec.leg_seq("r1") == 1
        await feed(rec, mission, robot, msg(mission, 2, 3))
        assert rec.leg_seq("r1") == 2
        rec.on_state("r1", msg(mission, 2, 4), robot)
        rec._put_latest(rec._tracks["r1"])
        latest = rec.queue.take_latest()["r1"]
        assert latest["state_msg"]["_dispatch"]["leg_seq"] == 2

    async def test_edge_blocked_carries_run_and_leg(self, tmp_path):
        rec, db, mission, robot = await start(tmp_path)
        await feed(rec, mission, robot, msg(mission, 0, 0), msg(mission, 2, 3))
        mission.status.blocked_edge, mission.status.blocked_node = "e1", "n1"
        rec.edge_blocked("r1", mission, at(4))
        [event] = [row for t, row in rec.queue.drain(1000)
                   if t == "fleet_events" and row["code"] == EventCode.MISSION_EDGE_BLOCKED.value]
        assert event["payload"]["leg_seq"] == 2
        assert str(event["run_id"]) == str(fr.run_uuid("m1", RUN))

    async def test_summary_metrics_and_leg_event_counts_on_run_end(self, tmp_path):
        rec, db, mission, robot = await start(tmp_path)
        await feed(rec, mission, robot, msg(mission, 0, 0), msg(mission, 2, 3),
                   msg(mission, 2, 5, driving=False), msg(mission, 4, 11), msg(mission, 6, 21))
        run_id = fr.run_uuid("m1", RUN)
        # what the API writes for the nav events, tagged with the leg
        for i, (code, payload, ts) in enumerate((
                ("NAV.RECOVERY_ENTERED", {"leg_seq": 3}, 14),
                ("NAV.RECOVERY_EXITED", {"leg_seq": 3, "duration_s": 4.0}, 18),
                ("NAV.GOAL_BLOCKED", {"leg_seq": 3, "cause": "X"}, 15),
                ("NAV.RECOVERY_ENTERED", {}, 16))):
            db.events[(uuid.uuid4(), at(ts))] = {"code": code, "run_id": run_id,
                                                 "payload": payload}
        mission.status.state = State.COMPLETED
        mission.status.passes_completed = 1
        rec._clock.now = at(30)
        rec.run_finished("r1", mission, robot)
        await rec.run_pending_ops()
        legs = db.legs_of()
        assert (legs[2]["recoveries"], legs[2]["recovery_s"], legs[2]["blocks"]) == (1, 4.0, 1)
        assert (legs[1]["recoveries"], legs[1]["blocks"]) == (0, 0)
        summary = db.runs[run_id]["summary_metrics"]
        assert summary["leg_count"] == 3 and summary["passes_completed"] == 1
        assert summary["distance_m"] == 7.0                   # 3 + 4 (first leg has none)
        assert summary["recovery_count"] == 1 and summary["time_recovery_s"] == 4.0
        assert summary["block_count"] == 1
        assert summary["duration_s"] == 30.0
        assert summary["time_moving_s"] == 15.0               # 21 s of legs, 6 s not driving
        assert summary["time_moving_s"] + summary["time_stopped_s"] == pytest.approx(30.0)
        assert summary["pass_durations_s"] == [{"pass": 0, "legs": 3, "duration_s": 21.0}]
        # expected time exists for the two legs with a pose: ratio = their actual / expected
        exp = legs[1]["expected_s"] + legs[2]["expected_s"]
        assert summary["expected_s"] == pytest.approx(exp, abs=0.01)
        assert summary["actual_vs_expected"] == pytest.approx(
            (legs[1]["duration_s"] + legs[2]["duration_s"]) / exp, abs=0.01)

    async def test_summary_of_a_run_without_legs(self, tmp_path):
        rec, db, mission, robot = await start(tmp_path)
        mission.status.state = State.CANCELED
        rec.run_finished("r1", mission, robot)
        await rec.run_pending_ops()
        [run] = db.runs.values()
        assert run["summary_metrics"]["leg_count"] == 0
        assert run["summary_metrics"]["expected_s"] is None and \
            run["summary_metrics"]["actual_vs_expected"] is None

    async def test_a_summary_failure_does_not_cost_the_run(self, tmp_path):
        rec, db, mission, robot = await start(tmp_path)
        db.fail = lambda sql, params: RuntimeError("x") if sql == fr.RUN_LEGS_SQL else None
        mission.status.state = State.COMPLETED
        rec.run_finished("r1", mission, robot)
        await rec.run_pending_ops()
        [run] = db.runs.values()
        assert run["state"] == "COMPLETED" and run.get("summary_metrics") is None


# --- endpoints -----------------------------------------------------------------------------

async def get(db, url):
    svc = type("Svc", (), {"database": db})()
    with patch.object(main, "service", svc):
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
            return await http.get(url)


def leg_row(run_id, seq, src, dst, duration, expected=4.0, recoveries=0, recovery_s=0.0,
            blocks=0):
    base = {"run_id": run_id, "seq": seq, "mission_name": "m1", "robot_name": "r1",
            "pass_index": 0, "order_rev": 0, "from_vda_node": f"m1-r{run_id}-n0-s{2 * seq - 2}",
            "to_vda_node": f"m1-r{run_id}-n0-s{2 * seq}", "from_topomap_node": src,
            "to_topomap_node": dst, "map_id": "map1", "started_at": at(0), "ended_at": at(1),
            "received_started_at": at(0), "received_ended_at": at(1), "duration_s": duration,
            "stopped_s": 0.0, "straight_m": 3.0, "planned_m": 3.0, "expected_s": expected,
            "recoveries": recoveries, "recovery_s": recovery_s, "blocks": blocks}
    return tuple(base[c] for c in fleet_reads.LEG_COLUMNS)


class TestTrack:
    """robot_track_ts: 1 Hz pose and speed rows at the track and full levels."""

    def tmsg(self, mission, t, seq=0, vel=(0.3, 0.4, 0.1), x=1.0):
        return state(at(t), order_id=order(mission), last_node=node(mission, 0, seq),
                     driving=True, velocity=vel, x=x)

    async def rows(self, tmp_path, level, messages):
        rec, db, mission, robot = await start(tmp_path, level=level)
        await feed(rec, mission, robot, *[self.tmsg(mission, *m) for m in messages])
        return [r for t, r in rec.queue.drain(100000) if t == "robot_track_ts"]

    async def test_one_row_per_second_with_speed_and_leg(self, tmp_path):
        rows = await self.rows(tmp_path, "track", [(0, 0), (0.4, 0), (1.0, 0), (1.5, 2), (2.2, 2)])
        columns = fr.tables.TRACK_COLUMNS
        got = [dict(zip(columns, r)) for r in rows]
        assert [g["ts"] for g in got] == [at(0), at(1.0), at(2.2)]
        assert got[0]["speed"] == pytest.approx(0.5) and got[0]["omega"] == pytest.approx(0.1)
        assert (got[0]["x"], got[0]["y"], got[0]["theta"], got[0]["map_id"]) == (1.0, 2.0, 0.5, "map1")
        assert got[0]["robot_name"] == "r1" and got[0]["run_id"] is not None
        assert [g["leg_seq"] for g in got] == [1, 1, 2]

    async def test_no_velocity_is_a_null_speed(self, tmp_path):
        [row] = await self.rows(tmp_path, "full", [(0, 0, None)])
        got = dict(zip(fr.tables.TRACK_COLUMNS, row))
        assert got["speed"] is None and got["omega"] is None

    async def test_levels(self, tmp_path):
        for level, expected in (("off", 0), ("events_only", 0), ("track", 1), ("full", 1)):
            assert len(await self.rows(tmp_path, level, [(0, 0)])) == expected, level

    async def test_legs_are_still_written_at_track(self, tmp_path):
        rec, db, mission, robot = await start(tmp_path, level="track")
        await feed(rec, mission, robot, msg(mission, 0, 0), msg(mission, 2, 3))
        assert len(db.legs_of()) == 1


class TestTrackEndpoint:
    run_id = uuid.uuid4()

    def respond(self, rows, session, run_map="map1"):
        run_id = self.run_id

        def respond(sql, params):
            if "FROM mission_runs" in sql:
                return [(run_id, "m1", "r1", None, run_map, None, "track", "COMPLETED", None,
                         None, 1, None, at(0), at(30), None, None, [], None)]
            if "FROM robot_track_ts" in sql:
                assert "run_id = %s" in sql and params[0] == "r1" and params[-1] == run_id
                return rows
            if "FROM map_sessions" in sql:
                assert params[:2] == ("r1", "map1")
                return [] if session is None else [session]
            return []
        return respond

    ROWS = [(at(0), 1.0, 2.0, 0.0, 0.5, 0.1, 1), (at(1), 2.0, 2.0, 0.0, None, None, None)]

    async def test_placed_session_converts_to_the_map_frame(self):
        session = (True, {"tx": 10.0, "ty": 0.0, "yaw": math.pi / 2})
        body = (await get(FakeDb(self.respond(self.ROWS, session)),
                          f"/api/v1/runs/{self.run_id}/track")).json()
        assert (body["frame"], body["map_id"], body["downsampled"]) == ("map", "map1", False)
        first, second = body["points"]
        assert (first["x"], first["y"], first["theta"]) == (8.0, 1.0, 1.571)
        assert (first["speed"], first["omega"], first["leg_seq"]) == (0.5, 0.1, 1)
        assert first["ts"] == "2026-09-24T12:00:00+00:00"
        assert (second["speed"], second["leg_seq"]) == (None, None)

    async def test_unplaced_or_no_session_stays_in_the_run_frame(self):
        for session in ((False, {"tx": 10.0, "ty": 0.0, "yaw": 0.0}), None):
            body = (await get(FakeDb(self.respond(self.ROWS, session)),
                              f"/api/v1/runs/{self.run_id}/track")).json()
            assert body["frame"] == "run" and body["points"][0]["x"] == 1.0

    async def test_empty_and_unknown(self):
        body = (await get(FakeDb(self.respond([], None)),
                          f"/api/v1/runs/{self.run_id}/track")).json()
        assert body["points"] == [] and body["frame"] == "run"
        assert (await get(FakeDb(), f"/api/v1/runs/{uuid.uuid4()}/track")).status_code == 404

    async def test_strided_above_the_cap(self):
        rows = [(at(i), float(i), 0.0, 0.0, 0.0, 0.0, 1) for i in range(10)]
        with patch.object(fleet_reads.config, "FLEET_TRACK_MAX_POINTS", 4):
            body = (await get(FakeDb(self.respond(rows, None)),
                              f"/api/v1/runs/{self.run_id}/track")).json()
        assert body["downsampled"] and len(body["points"]) <= 4
        assert body["points"][-1]["x"] == 9.0


class TestEndpoints:
    async def test_run_legs(self):
        run_id = uuid.uuid4()
        rows = [leg_row(run_id, 1, None, "A", 4.0, None), leg_row(run_id, 2, "A", "B", 7.0)]

        def respond(sql, params):
            if "FROM mission_runs" in sql:
                return [(run_id, "m1", "r1", None, None, None, "full", "COMPLETED", None, None,
                         1, None, at(0), at(30), None, None, [], None)]
            if "FROM run_legs" in sql:
                assert params == (run_id,) and "ORDER BY seq" in sql
                return rows
            return []
        body = (await get(FakeDb(respond), f"/api/v1/runs/{run_id}/legs")).json()
        assert body["run_id"] == str(run_id)
        assert [(l["seq"], l["from_topomap_node"], l["to_topomap_node"], l["duration_s"])
                for l in body["items"]] == [(1, None, "A", 4.0), (2, "A", "B", 7.0)]
        assert body["items"][0]["started_at"] == "2026-09-24T12:00:00+00:00"

    async def test_run_legs_unknown_run_is_404(self):
        response = await get(FakeDb(), f"/api/v1/runs/{uuid.uuid4()}/legs")
        assert response.status_code == 404

    async def test_mission_legs_aggregate(self):
        runs = [uuid.uuid4() for _ in range(3)]
        rows = []
        for i, (run_id, dur) in enumerate(zip(runs, (6.0, 10.0, 8.0))):
            rows.append(leg_row(run_id, 1, "A", "B", dur, recoveries=i % 2, recovery_s=2.0 * (i % 2)))
            rows.append(leg_row(run_id, 2, "B", "C", 2.0, expected=None))

        def respond(sql, params):
            if "FROM mission_runs" in sql:
                assert params == ("m1", "m1", "m1") and "archived_at IS NULL" in sql
                return [(r,) for r in runs]
            if "FROM run_legs" in sql:
                assert params[0] == runs
                return rows
            return []
        body = (await get(FakeDb(respond), "/api/v1/missions/m1/legs")).json()
        assert (body["mission"], body["runs"], body["legs"], body["truncated"]) == \
            ("m1", 3, 6, False)
        ab, bc = body["items"]
        assert (ab["from"], ab["to"], ab["topomap"], ab["count"]) == ("A", "B", True, 3)
        assert ab["median_s"] == 8.0 and ab["p90_s"] == 9.6 and ab["expected_s"] == 4.0
        assert ab["ratio"] == 2.0 and ab["recoveries"] == 1 and ab["recovery_s"] == 2.0
        assert (bc["median_s"], bc["expected_s"], bc["ratio"]) == (2.0, None, None)

    async def test_mission_without_runs_is_404(self):
        assert (await get(FakeDb(), "/api/v1/missions/nope/legs")).status_code == 404
