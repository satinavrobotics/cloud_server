"""The "last position on this map" placement suggestion (docs/satinav-maps-redesign.md §14.3).

- map_sessions.last_position_suggestion: the math (the robot stood at the last pose, restarted
  and drove a bit: candidate map_T_session applied to the live pose is the true map pose);
- maps.placement_suggestions: source selection (unplace snapshot, state history, finished
  session), the empty and error cases, on the in-memory store;
- POST .../place accepting `source: "last_position"`;
- the dispatcher's _on_run_changed storing last_robot_pose / old_run_id in the placement patch.
"""
import datetime
import json
import math
import os
import random
import uuid

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

from unittest.mock import patch  # noqa: E402

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

import packages.controllers.mission.vda5050_types as types  # noqa: E402
from cloud_common.objects.robot import RobotObjectV1, RobotStatusV1  # noqa: E402
from packages.api import maps  # noqa: E402
from packages.utils import map_geo  # noqa: E402
from packages.utils import map_sessions as ms  # noqa: E402
from tests.unit import test_maps_m1 as m1  # noqa: E402
from tests.unit import test_maps_use_run_change as rc  # noqa: E402
from tests.unit.test_maps_m2 import ShimDb, ShimStore  # noqa: E402

pytestmark = pytest.mark.unit

T0 = m1.T0
MIN = datetime.timedelta(minutes=1)
OLD_T = {"tx": 10.0, "ty": -3.0, "yaw": 0.6}


# --- the math -----------------------------------------------------------------------------------

def _true_map_pose_after_driving(last_map, start, live):
    """Where the robot really is: it stood at `last_map` when the run began at `start` (run
    frame), then drove to `live` (run frame): the motion start -> live is applied to last_map."""
    start_t = {"tx": start["x"], "ty": start["y"], "yaw": start["theta"]}
    live_t = {"tx": live["x"], "ty": live["y"], "yaw": live["theta"]}
    rel = ms.compose(map_geo.invert_transform(start_t), live_t)
    last_t = {"tx": last_map["x"], "ty": last_map["y"], "yaw": last_map["yaw"]}
    t = ms.compose(last_t, rel)
    return t["tx"], t["ty"], t["yaw"]


class TestSuggestionMath:
    def test_round_trip_with_driving_since_the_restart(self):
        rng = random.Random(7)
        for _ in range(300):
            old = {"tx": rng.uniform(-50, 50), "ty": rng.uniform(-50, 50),
                   "yaw": rng.uniform(-math.pi, math.pi)}
            last = {"x": rng.uniform(-50, 50), "y": rng.uniform(-50, 50),
                    "theta": rng.uniform(-math.pi, math.pi)}
            start = {"x": rng.uniform(-3, 3), "y": rng.uniform(-3, 3),
                     "theta": rng.uniform(-math.pi, math.pi)}
            live = {"x": rng.uniform(-9, 9), "y": rng.uniform(-9, 9),
                    "theta": rng.uniform(-math.pi, math.pi)}
            got = ms.last_position_suggestion(old, last, start)
            x, y, yaw = map_geo.apply_pose(old, last["x"], last["y"], last["theta"])
            assert got["pose"] == pytest.approx({"x": x, "y": y, "yaw": yaw})
            assert got["robot_pose"] == start
            # the last map pose is the start pose through the candidate ...
            sx, sy, syaw = map_geo.apply_pose(got["map_T_session"], start["x"], start["y"],
                                              start["theta"])
            assert (sx, sy) == pytest.approx((x, y), abs=1e-6)
            assert ms.yaw_difference(syaw, yaw) < 1e-9
            # ... and the live pose through it is where the robot really is
            tx, ty, tyaw = _true_map_pose_after_driving(got["pose"], start, live)
            lx, ly, lyaw = map_geo.apply_pose(got["map_T_session"], live["x"], live["y"],
                                              live["theta"])
            assert (lx, ly) == pytest.approx((tx, ty), abs=1e-6)
            assert ms.yaw_difference(lyaw, tyaw) < 1e-9

    def test_unknown_run_start_is_the_odometry_origin(self):
        got = ms.last_position_suggestion(OLD_T, {"x": 1.0, "y": 2.0, "theta": 0.1})
        assert got["robot_pose"] == {"x": 0.0, "y": 0.0, "theta": 0.0}
        # with the robot at the origin the candidate IS the last map pose
        assert got["map_T_session"] == pytest.approx(
            {"tx": got["pose"]["x"], "ty": got["pose"]["y"], "yaw": got["pose"]["yaw"]})

    @pytest.mark.parametrize("old,last,start", [
        (None, {"x": 0, "y": 0, "theta": 0}, None),
        (OLD_T, None, None),
        (OLD_T, {"x": float("nan"), "y": 0, "theta": 0}, None),
        (OLD_T, {"x": 0, "y": 0}, None),
        ({"tx": 1.0, "ty": 2.0}, {"x": 0, "y": 0, "theta": 0}, None),
        ({"tx": float("inf"), "ty": 0.0, "yaw": 0.0}, {"x": 0, "y": 0, "theta": 0}, None),
        (OLD_T, {"x": 0, "y": 0, "theta": 0}, {"x": float("inf"), "y": 0, "theta": 0}),
    ])
    def test_unusable_input_gives_nothing(self, old, last, start):
        assert ms.last_position_suggestion(old, last, start) is None


# --- the endpoint -------------------------------------------------------------------------------

class SugStore(ShimStore):
    async def robot_state_pose(self, name, start=None, end=None, first=False):
        rows = [r for r in self.db.state_rows if r["robot"] == name
                and (start is None or r["ts"] >= start) and (end is None or r["ts"] < end)]
        if not rows:
            return None
        r = (min if first else max)(rows, key=lambda r: r["ts"])
        return {"ts": r["ts"], "x": r["x"], "y": r["y"], "theta": r["theta"]}

    async def robot_run_start(self, name):
        return self.db.run_starts.get(name)


class SugDb(ShimDb):
    def __init__(self):
        super().__init__()
        self.state_rows = []
        self.run_starts = {}   # robot -> (started_at, reason)

    def row(self, ts, x, y, theta, robot="r1"):
        self.state_rows.append({"robot": robot, "ts": ts, "x": x, "y": y, "theta": theta})

    def store(self, _db, _publisher_id):
        import contextlib
        import copy

        @contextlib.asynccontextmanager
        async def cm():
            snapshot = copy.deepcopy((self.maps, self.sessions))
            store = SugStore(self)
            try:
                yield store
            except BaseException:
                self.maps, self.sessions = snapshot
                raise
            self.events.extend(store.pending_events)
            self.notifies.extend(store.pending_notifies)
        return cm()


@pytest.fixture
def db():
    d = SugDb()
    with patch.object(maps, "open_store", d.store), patch.object(maps, "_utcnow", m1.Clock()):
        yield d


def _robot(db, name="r1"):
    db.robots[name] = RobotObjectV1(name=name, status=RobotStatusV1(
        online=True, state="IDLE", pose={"x": 0.0, "y": 0.0, "theta": 0.0}))


def _unplaced(db, placement=None, **extra):
    """An open, unplaced operate session of r1 on the local map `shed`, unplaced at T0 + 10 min."""
    db.maps.setdefault("shed", {"lifecycle": "ALIVE", "spec": {"type": "local"},
                                "status": {"state": "ready"}})
    base = {"placement": {"unplaced_reason": "run_changed",
                          "unplaced_at": (T0 + 10 * MIN).isoformat(), **(placement or {})},
            "aligned": False, "map_t_session": dict(OLD_T), "purpose": "operate",
            "started_at": T0}
    base.update(extra)
    return db.add_session("shed", "r1", "live", ended=False, **base)


async def _get(sid, map_name="shed"):
    return await maps.placement_suggestions(None, map_name, str(sid))


async def _status(coro):
    try:
        await coro
    except HTTPException as exc:
        return exc.status_code
    raise AssertionError("no HTTPException")


LAST = {"x": 4.0, "y": 1.0, "theta": 0.2}


class TestEndpoint:
    async def test_unplace_snapshot(self, db):
        s = _unplaced(db, {"last_robot_pose": LAST})
        db.row(T0 + 10 * MIN + datetime.timedelta(seconds=3), 0.5, 0.0, 0.1)   # run start
        out = await _get(s["session_id"])
        assert out["map_id"] == "shed" and out["session_id"] == str(s["session_id"])
        (sug,) = out["suggestions"]
        assert sug["source"] == "last_position" and sug["basis"] == "unplace_snapshot"
        assert sug["from_session_id"] == str(s["session_id"])
        assert sug["at"] == (T0 + 10 * MIN).isoformat()
        x, y, yaw = map_geo.apply_pose(OLD_T, 4.0, 1.0, 0.2)
        assert sug["pose"] == pytest.approx({"x": x, "y": y, "yaw": yaw})
        assert sug["robot_pose"] == {"x": 0.5, "y": 0.0, "theta": 0.1}
        assert set(sug["map_T_session"]) == {"tx", "ty", "yaw"}
        assert ms.placement_transform(sug["pose"], sug["robot_pose"]) == \
            pytest.approx(sug["map_T_session"])
        json.dumps(out)

    async def test_run_start_is_the_first_row_after_the_unplace_not_a_later_one(self, db):
        s = _unplaced(db, {"last_robot_pose": LAST})
        db.row(T0 + 9 * MIN, 99.0, 99.0, 0.0)               # old run
        db.row(T0 + 11 * MIN, 7.0, 7.0, 0.0)                # drove since
        db.row(T0 + 10 * MIN + datetime.timedelta(seconds=2), 0.1, 0.0, 0.0)
        (sug,) = (await _get(s["session_id"]))["suggestions"]
        assert sug["robot_pose"] == {"x": 0.1, "y": 0.0, "theta": 0.0}

    async def test_no_run_start_row_means_the_odometry_origin(self, db):
        s = _unplaced(db, {"last_robot_pose": LAST})
        (sug,) = (await _get(s["session_id"]))["suggestions"]
        assert sug["robot_pose"] == {"x": 0.0, "y": 0.0, "theta": 0.0}

    async def test_state_history_when_the_session_was_unplaced_before_the_snapshot(self, db):
        s = _unplaced(db)
        db.row(T0 + 9 * MIN, 1.0, 1.0, 0.0)
        db.row(T0 + 10 * MIN - datetime.timedelta(seconds=5), 4.0, 1.0, 0.2)   # last old row
        db.row(T0 + 10 * MIN + datetime.timedelta(seconds=4), 0.0, 0.0, 0.0)   # new run
        (sug,) = (await _get(s["session_id"]))["suggestions"]
        assert sug["basis"] == "state_history"
        assert sug["at"] == (T0 + 10 * MIN - datetime.timedelta(seconds=5)).isoformat()
        x, y, yaw = map_geo.apply_pose(OLD_T, 4.0, 1.0, 0.2)
        assert sug["pose"] == pytest.approx({"x": x, "y": y, "yaw": yaw})
        assert sug["from_session_id"] == str(s["session_id"])

    async def test_snapshot_wins_over_history_and_finished_session(self, db):
        old = db.add_session("shed", "r1", "live", ended=True, aligned=True,
                             started_at=T0, ended_at=T0 + 5 * MIN)
        db.row(T0 + MIN, 1.0, 1.0, 0.0)
        s = _unplaced(db, {"last_robot_pose": LAST})
        (sug,) = (await _get(s["session_id"]))["suggestions"]
        assert sug["basis"] == "unplace_snapshot" and old["session_id"] != s["session_id"]

    async def test_finished_session_fallback(self, db):
        old = db.add_session("shed", "r1", "live", ended=True, aligned=True,
                             started_at=T0, ended_at=T0 + 5 * MIN,
                             map_t_session={"tx": -2.0, "ty": 5.0, "yaw": -0.3})
        db.add_session("shed", "r2", "live", ended=True, aligned=True,        # another robot
                       started_at=T0 + MIN, ended_at=T0 + 6 * MIN)
        db.row(T0 + MIN, 1.0, 1.0, 0.0)
        db.row(T0 + 5 * MIN - datetime.timedelta(seconds=1), 2.0, 3.0, 0.4)    # its last row
        db.row(T0 + 8 * MIN, 0.0, 0.0, 0.0)                                    # after it ended
        db.run_starts["r1"] = (T0 + 7 * MIN, "run_changed")
        s = _unplaced(db, placement={"unplaced_at": None})   # nothing usable of its own
        s["placement"] = None
        db.sessions[-1]["placement"] = None
        db.sessions[-1]["map_t_session"] = None
        (sug,) = (await _get(s["session_id"]))["suggestions"]
        assert sug["basis"] == "finished_session"
        assert sug["from_session_id"] == str(old["session_id"])
        x, y, yaw = map_geo.apply_pose({"tx": -2.0, "ty": 5.0, "yaw": -0.3}, 2.0, 3.0, 0.4)
        assert sug["pose"] == pytest.approx({"x": x, "y": y, "yaw": yaw})
        assert sug["robot_pose"] == {"x": 0.0, "y": 0.0, "theta": 0.0}  # the row at 8 min

    async def test_finished_session_must_have_ended_placed(self, db):
        db.add_session("shed", "r1", "live", ended=True, aligned=False,
                       started_at=T0, ended_at=T0 + 5 * MIN)
        db.row(T0 + MIN, 1.0, 1.0, 0.0)
        s = _unplaced(db, placement={"unplaced_at": None})
        db.sessions[-1]["placement"] = None
        assert (await _get(s["session_id"]))["suggestions"] == []

    async def test_nothing_to_suggest(self, db):
        s = _unplaced(db)
        assert (await _get(s["session_id"]))["suggestions"] == []

    async def test_a_placed_session_has_none(self, db):
        s = _unplaced(db, {"last_robot_pose": LAST}, aligned=True)
        assert (await _get(s["session_id"]))["suggestions"] == []

    async def test_a_geo_map_has_none(self, db):
        db.add_map("geo1", type="geo", status={"state": "ready"},
                   geo=map_geo.geo_from_datum(m1.UTM_DATUM))
        s = db.add_session("geo1", "r1", "live", ended=False, aligned=False,
                           placement={"last_robot_pose": LAST})
        assert (await _get(s["session_id"], "geo1"))["suggestions"] == []

    async def test_errors(self, db):
        s = _unplaced(db, {"last_robot_pose": LAST})
        fin = db.add_session("shed", "r9", "live", ended=True)
        assert await _status(_get(s["session_id"], "nomap")) == 404
        assert await _status(_get(uuid.uuid4())) == 404
        assert await _status(_get("not-a-uuid")) == 404
        assert await _status(_get(fin["session_id"])) == 409
        db.add_map("other", type="local", status={"state": "ready"})
        assert await _status(_get(s["session_id"], "other")) == 404


class TestAccepting:
    async def test_place_records_the_source(self, db):
        _robot(db)
        s = _unplaced(db)
        body = {"pose": {"x": 1.0, "y": 2.0, "yaw": 0.3},
                "robot_pose": {"x": 0.0, "y": 0.0, "theta": 0.0}, "source": "last_position"}
        out = await maps.place_session(None, "shed", str(s["session_id"]), body, m1.PUB, "ann")
        assert out["session"]["placement"]["source"] == "last_position"
        assert out["session"]["aligned"] is True

    async def test_without_a_source_it_is_a_user_placement(self, db):
        _robot(db)
        s = _unplaced(db)
        body = {"pose": {"x": 1.0, "y": 2.0, "yaw": 0.3},
                "robot_pose": {"x": 0.0, "y": 0.0, "theta": 0.0}}
        out = await maps.place_session(None, "shed", str(s["session_id"]), body, m1.PUB)
        assert out["session"]["placement"]["source"] == "user"

    async def test_another_source_is_refused(self, db):
        _robot(db)
        s = _unplaced(db)
        body = {"pose": {"x": 1.0, "y": 2.0, "yaw": 0.3},
                "robot_pose": {"x": 0.0, "y": 0.0, "theta": 0.0}, "source": "session"}
        assert await _status(maps.place_session(None, "shed", str(s["session_id"]), body,
                                                m1.PUB)) == 422
        # "datum" is a source since maps §17, for geo maps only: a local map refuses it
        assert await _status(maps.place_session(None, "shed", str(s["session_id"]),
                                                dict(body, source="datum"), m1.PUB)) == 409


# --- the dispatcher -------------------------------------------------------------------------------

class TestDispatcherSnapshot:
    async def _unplace(self, pose, epoch=None):
        patches = []

        def unplace(params):
            patches.append(json.loads(params[0]))
            return [], 1

        db = rc.FakeDb([("UPDATE map_sessions SET aligned = false", unplace)])
        r = rc._robot(db)
        if pose is not None:
            r._robot_object.status.pose.x, r._robot_object.status.pose.y, \
                r._robot_object.status.pose.theta = pose
        r._run_epoch = epoch
        msg = types.VDA5050Connection(headerId=1, timestamp="t", state="ONLINE")
        await r._on_connection_message(msg)   # baseline
        await r._on_connection_message(msg)   # header id did not advance: a new run
        assert len(patches) == 1
        return patches[0]

    async def test_last_pose_and_old_run_are_stored(self):
        epoch = uuid.uuid4()
        patch_ = await self._unplace((4.0, 1.0, 0.2), epoch)
        assert patch_["unplaced_reason"] == "run_changed"
        assert patch_["last_robot_pose"] == {"x": 4.0, "y": 1.0, "theta": 0.2}
        assert patch_["old_run_id"] == str(epoch)

    async def test_a_state_message_run_change_uses_the_old_pose(self):
        patches = []
        db = rc.FakeDb([("UPDATE map_sessions SET aligned = false",
                         lambda p: (patches.append(json.loads(p[0])) or [], 1))])
        r = rc._robot(db)
        r._robot_object.status.pose.x = 4.0
        for hid in (10, 11, 0):    # 0: the client restarted; its first pose is elsewhere
            await r._on_state_message(types.VDA5050State(
                headerId=hid, timestamp="", nodeStates=[], edgeStates=[], errors=[],
                agvPosition={"x": 0.0 if hid == 0 else 4.0, "y": 0.0, "theta": 0.0,
                             "positionInitialized": True, "mapId": "m"}))
        assert len(patches) == 1 and patches[0]["last_robot_pose"]["x"] == 4.0

    async def test_a_pose_that_is_not_finite_is_left_out(self):
        patch_ = await self._unplace((float("nan"), 0.0, 0.0))
        assert "last_robot_pose" not in patch_ and "old_run_id" not in patch_
