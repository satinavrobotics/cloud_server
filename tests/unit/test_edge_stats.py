"""Per-edge traversal statistics of the topomap (groundwork for edge weights): the pure rules
(packages/utils/edge_stats.py), the storage (edge_stats_db), the two writers (mission-dispatch at
run end, the API when a mapping session finishes), GET /api/v1/maps/{id}/edge-stats and
scripts/backfill_edge_observations.py."""
import datetime
import os
import uuid
from types import SimpleNamespace

os.environ.setdefault("POSTGRES_PASSWORD", "test")

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

pytest.importorskip("psycopg")

import packages.api.main as main  # noqa: E402
from packages import config  # noqa: E402
from packages.api import edge_stats as api  # noqa: E402
from packages.api import maps  # noqa: E402
from packages.api.result_cache import ResultCache  # noqa: E402
from packages.controllers.mission import fleet_recorder as fr  # noqa: E402
from packages.utils import edge_stats as es  # noqa: E402
from packages.utils import edge_stats_db as store  # noqa: E402
from scripts import backfill_edge_observations as bf  # noqa: E402
from tests.unit import test_maps_m1 as m1  # noqa: E402
from tests.unit.fleet_recorder_fakes import FakeConnection, FakeDB, FakePool  # noqa: E402
from tests.unit.test_maps_m1 import fdb  # noqa: E402,F401
from tests.unit.test_run_legs import (  # noqa: E402
    State, at, feed, make_mission, msg, start)

pytestmark = pytest.mark.unit

UTC = datetime.timezone.utc
T0 = datetime.datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)


def secs(n):
    return T0 + datetime.timedelta(seconds=n)


def obs(i, duration=2.0, src="A", dst="B", robot="r1", map_id="yard", source="leg",
        distance=1.0, **extra):
    row = es._observation(map_id, src, dst, robot, source, f"k{i:03d}", secs(i), duration,
                          distance)
    row.update(extra)
    return row


# --- the rules -------------------------------------------------------------------------------

class TestRules:
    @pytest.mark.parametrize("duration, distance, reason", [
        (2.0, 1.0, None), (2.0, None, None), (0.0, 1.0, "duration"), (-1.0, 1.0, "duration"),
        (None, 1.0, "duration"), (3601.0, 1.0, "too_long"), (1.0, 2.5, "too_fast"),
        (1.0, 2.0, None), (0.0004, 1.0, "duration"), (0.0006, 0.0, None),
        (float("nan"), 1.0, "duration"), (float("inf"), 1.0, "duration"),
        (2.0, float("nan"), "distance"), (2.0, float("inf"), "distance")])
    def test_reject_reason(self, duration, distance, reason):
        assert es.reject_reason(duration, distance) == reason

    def test_thresholds_are_the_config(self):
        assert (config.EDGE_STATS_WINDOW, config.EDGE_STATS_MAX_SPEED_MPS,
                config.EDGE_STATS_MAX_DURATION_S) == (20, 2.0, 3600.0)
        assert config.EDGE_STATS_RETENTION_DAYS == 365

    def test_speed_rules_default_to_the_drive_stats_ones(self):
        # deliberately the same movement rules as drive-stats / path efficiency (env vars may override)
        assert config.EDGE_STATS_MAX_SPEED_MPS == config.DRIVE_STATS_MAX_SPEED_MS
        assert config.EDGE_STATS_MIN_SPEED_MPS == config.DRIVE_STATS_MIN_SPEED_MS

    def leg(self, **extra):
        leg = {"run_id": "run", "seq": 3, "robot_name": "r1", "map_id": "yard",
               "from_topomap_node": "A", "to_topomap_node": "B", "ended_at": secs(10),
               "duration_s": 4.0, "straight_m": 2.0, "recoveries": 1, "blocks": 2}
        leg.update(extra)
        return leg

    def test_leg_observation(self):
        o = es.leg_observation(self.leg())
        assert (o["from_node"], o["to_node"], o["source"], o["source_key"]) == \
            ("A", "B", "leg", "run:3")
        assert (o["duration_s"], o["speed_mps"], o["recoveries"], o["blocks"]) == (4.0, 0.5, 1, 2)

    @pytest.mark.parametrize("extra", [
        {"from_topomap_node": None}, {"to_topomap_node": ""}, {"to_topomap_node": "A"},
        {"map_id": None}, {"duration_s": 0.0}, {"duration_s": 0.1, "straight_m": 3.0}])
    def test_unusable_legs_are_dropped(self, extra):
        assert es.leg_observation(self.leg(**extra)) is None

    def test_a_duration_that_rounds_to_zero_is_rejected_not_stored_as_zero(self):
        assert es.leg_observation(self.leg(duration_s=0.0004, straight_m=0.0)) is None
        assert es.leg_observation(self.leg(duration_s=0.0006, straight_m=0.0))["duration_s"] == 0.001

    def test_the_distance_is_the_plan_length_when_the_leg_has_one(self):
        o = es.leg_observation(self.leg(straight_m=2.0, planned_m=3.0))
        assert (o["distance_m"], o["speed_mps"]) == (3.0, 0.75)
        o = es.leg_observation(self.leg(straight_m=2.0, planned_m=None))      # no plan length: the straight line
        assert (o["distance_m"], o["speed_mps"]) == (2.0, 0.5)
        o = es.leg_observation(self.leg(straight_m=2.0))                      # a row without the field (older caller)
        assert o["distance_m"] == 2.0

    def test_a_plan_length_that_makes_the_speed_impossible_is_rejected(self):
        assert es.leg_observation(self.leg(duration_s=0.1, straight_m=0.1, planned_m=3.0)) is None

    def test_a_leg_without_a_distance_keeps_its_time(self):
        o = es.leg_observation(self.leg(straight_m=None))
        assert o["duration_s"] == 4.0 and o["speed_mps"] is None

    def node(self, i, x, t, capture=None):
        return {"id": f"n{i}", "x": x, "y": 0.0, "time": None if t is None else secs(t),
                "capture": None if capture is None else secs(capture)}

    def test_session_observations_drop_bad_steps(self):
        nodes = [self.node(0, 0.0, 0), self.node(1, 1.0, 2), self.node(2, 2.0, 2),   # dt 0
                 self.node(3, 3.0, 4), self.node(4, 40.0, 5),                         # 37 m/s
                 self.node(5, 41.0, None), self.node(6, 42.0, 9)]                     # no time
        out = es.session_observations("yard", "sess", "r1", nodes)
        assert [(o["from_node"], o["to_node"]) for o in out] == [("n0", "n1"), ("n2", "n3")]
        assert [o["source_key"] for o in out] == ["sess:1", "sess:3"]   # the later node's index
        assert all(o["source"] == "mapping" and o["recoveries"] == 0 and o["blocks"] == 0
                   for o in out)
        assert out[0]["speed_mps"] == 0.5 and out[0]["observed_at"] == secs(2)

    def test_a_second_traversal_of_the_same_pair_is_its_own_observation(self):
        nodes = [self.node(0, 0.0, 0), self.node(1, 1.0, 2), self.node(0, 0.0, 4),
                 self.node(1, 1.0, 6)]                       # n0 -> n1 twice, n1 -> n0 once
        out = es.session_observations("yard", "s", "r1", nodes)
        assert [(o["from_node"], o["to_node"], o["source_key"]) for o in out] == [
            ("n0", "n1", "s:1"), ("n1", "n0", "s:2"), ("n0", "n1", "s:3")]
        assert es.session_observations("yard", "s", "r1", nodes) == out        # stable

    def test_a_slow_mapping_step_is_not_a_traversal_but_a_slow_leg_is(self):
        # 1 m in 600 s = 0.0017 m/s (a paused session): dropped; 1 m in 10 s = 0.1 m/s: kept
        nodes = [self.node(0, 0.0, 0), self.node(1, 1.0, 600), self.node(2, 2.0, 610)]
        out = es.session_observations("yard", "s", "r1", nodes)
        assert [(o["from_node"], o["to_node"]) for o in out] == [("n1", "n2")]
        assert es.leg_observation(self.leg(duration_s=600.0, straight_m=1.0)) is not None

    def test_session_observations_use_capture_time_when_both_nodes_have_it(self):
        nodes = [self.node(0, 0.0, 0, capture=100), self.node(1, 1.0, 1, capture=102)]
        [o] = es.session_observations("yard", "s", "r1", nodes)
        assert o["duration_s"] == 2.0 and o["observed_at"] == secs(102)


class TestStats:
    def test_median_p90_speed_and_sums(self):
        rows = [obs(i, duration=d, distance=d * 0.5, recoveries=i % 2, blocks=1)
                for i, d in enumerate([2, 4, 6, 8, 10])]
        [item] = es.group_stats(rows)
        assert (item["count"], item["median_s"], item["median_speed_mps"]) == (5, 6.0, 0.5)
        assert item["p90_s"] == pytest.approx(9.2)
        assert (item["recoveries"], item["blocks"]) == (2, 5)
        assert item["last_observed_at"] == secs(4).isoformat()
        assert item["sources"] == {"leg": 5, "mapping": 0}

    def test_the_window_is_exactly_the_newest_20(self):
        rows = [obs(i, duration=float(i + 1)) for i in range(25)]    # durations 1..25
        [item] = es.group_stats(rows)
        assert item["count"] == 20
        # durations 6..25 are in, 1..5 (the oldest) are out
        assert item["median_s"] == 15.5 and item["p90_s"] == pytest.approx(23.1)
        assert len(es.newest(rows)) == 20 and es.newest(rows)[-1]["duration_s"] == 6.0

    def test_the_window_is_per_edge_and_robot(self):
        rows = ([obs(i, robot="r1") for i in range(22)] + [obs(i, robot="r2") for i in range(3)]
                + [obs(i, dst="C") for i in range(2)])
        counts = {(i["from_node_id"], i["to_node_id"], i["robot"]): i["count"]
                  for i in es.group_stats(rows)}
        assert counts == {("A", "B", "r1"): 20, ("A", "B", "r2"): 3, ("A", "C", "r1"): 2}

    def test_pooled_groups_per_edge_over_all_robots(self):
        rows = ([obs(i, robot="r1", duration=1.0) for i in range(15)]
                + [obs(20 + i, robot="r2", duration=3.0) for i in range(10)])
        [item] = es.group_stats(rows, pooled=True)
        assert item["robot"] is None and item["count"] == 20             # the newest 20 of 25
        assert item["median_s"] == 2.0 and item["sources"] == {"leg": 20, "mapping": 0}
        assert len(es.group_stats(rows)) == 2                            # not pooled: per robot

    def test_observations_on_non_edges_are_ignored(self):
        edges = es.edge_set([{"from_node_id": "A", "to_node_id": "B"}])
        rows = [obs(0), obs(1, src="B", dst="A"), obs(2, dst="C")]     # B->A: the other direction
        assert [(o["from_node"], o["to_node"]) for o in es.on_edges(rows, edges)] == [("A", "B")]


# --- storage ---------------------------------------------------------------------------------

async def cursor_of(db):
    return FakeConnection(db).cursor()


class TestStore:
    async def test_insert_is_idempotent(self):
        db = FakeDB()
        cur = await cursor_of(db)
        rows = [obs(i) for i in range(3)]
        assert await store.store(cur, rows) == 3
        assert await store.store(cur, rows) == 0
        assert len(db.edge_obs) == 3

    async def test_a_batch_is_one_insert_statement(self):
        db = FakeDB()
        cur = await cursor_of(db)
        rows = [obs(i) for i in range(5)] + [obs(0)]                  # a repeat inside the batch
        assert await store.store(cur, rows) == 5
        assert db.statements.count(store.INSERT_SQL) == 1 and len(db.edge_obs) == 5
        assert await store.store(cur, []) == 0                        # nothing: no statement at all
        assert db.statements.count(store.INSERT_SQL) == 1

    def test_insert_params_are_one_array_per_column(self):
        params = store.insert_params([obs(0), obs(1, dst="C")])
        assert len(params) == len(store.COLUMNS) == len(store.COLUMN_TYPES)
        assert params[store.COLUMNS.index("to_node")] == ["B", "C"]
        assert store.INSERT_SQL.count("::") == len(store.COLUMNS) and "unnest" in store.INSERT_SQL

    async def test_store_keeps_every_raw_observation(self):
        db = FakeDB()
        cur = await cursor_of(db)
        assert await store.store(cur, [obs(i) for i in range(25)]
                                 + [obs(i, robot="r2") for i in range(2)]) == 27
        assert len(db.edge_obs) == 27                                  # nothing pruned
        assert await store.store(cur, [obs(0)]) == 0                   # a repeat: still idempotent
        assert store.INSERT_SQL in db.statements and len(db.statements) == 2   # no prune statement

    async def test_fetch_cuts_the_window_per_edge_and_robot(self):
        db = FakeDB()
        cur = await cursor_of(db)
        await store.store(cur, [obs(i) for i in range(25)] + [obs(i, robot="r2") for i in range(2)]
                          + [obs(0, dst="C")])
        rows = await store.fetch(cur, "yard")
        mine = sorted(r["source_key"] for r in rows if r["robot_name"] == "r1"
                      and r["to_node"] == "B")
        assert mine == [f"k{i:03d}" for i in range(5, 25)]             # the newest 20
        assert len([r for r in rows if r["robot_name"] == "r2"]) == 2
        assert len([r for r in rows if r["to_node"] == "C"]) == 1
        assert len(await store.fetch(cur, "yard", "r2")) == 2
        assert len(await store.fetch(cur, "yard", window=3)) == 3 + 2 + 1

    async def test_fetch_pooled_cuts_per_edge_across_robots(self):
        db = FakeDB()
        cur = await cursor_of(db)
        # r1 on the even seconds, r2 on the odd ones: the newest 20 of the pool are 30..49
        await store.store(cur, [obs(i, robot="r1" if i % 2 == 0 else "r2") for i in range(50)])
        rows = await store.fetch(cur, "yard", pooled=True)
        assert sorted(r["observed_at"] for r in rows) == [secs(i) for i in range(30, 50)]
        assert db.statements[-1] == store.SELECT_POOLED_SQL

    def test_select_sql_orders_like_newest(self):
        for sql in (store.SELECT_SQL, store.SELECT_POOLED_SQL):
            assert 'ORDER BY observed_at DESC, source COLLATE "C" DESC, ' \
                   'source_key COLLATE "C" DESC' in sql
        assert "PARTITION BY from_node, to_node, robot_name " in store.SELECT_SQL
        assert "PARTITION BY from_node, to_node " in store.SELECT_POOLED_SQL

    async def test_purge_old_deletes_only_rows_older_than_the_retention_in_batches(self):
        db = FakeDB()
        cur = await cursor_of(db)
        now = datetime.datetime.now(UTC)
        old = [dict(obs(i), observed_at=now - datetime.timedelta(days=400, seconds=i))
               for i in range(7)]
        young = [dict(obs(10 + i), observed_at=now - datetime.timedelta(days=364, hours=i))
                 for i in range(3)]
        await store.store(cur, old + young)
        conn = FakeConnection(db)
        assert await store.purge_old(conn, days=0) == 0                 # 0 = keep forever
        assert await store.purge_old(conn, batch=3) == 7                # batches of 3, 3, 1
        assert db.statements.count(store.PURGE_SQL) == 3
        assert sorted(k[5] for k in db.edge_obs) == ["k010", "k011", "k012"]

class TestRetentionPass:
    async def test_the_recorder_purges_old_observations_on_its_pool(self, tmp_path):
        db = FakeDB()
        rec = fr.FleetRecorder.__new__(fr.FleetRecorder)
        rec._pool = FakePool(db)
        old = dict(obs(0), observed_at=datetime.datetime.now(UTC) - datetime.timedelta(days=500))
        await store.store(await cursor_of(db), [old, obs(1, observed_at=datetime.datetime.now(UTC))])
        assert await rec.purge_edge_observations() == 1
        assert [k[5] for k in db.edge_obs] == ["k001"]
        db.unavailable = True
        assert await rec.purge_edge_observations() == 0                # logged, never raises


# --- writer 1: mission-dispatch at run end -----------------------------------------------------

async def finish_run(rec, mission, robot, t=30):
    mission.status.state = State.COMPLETED
    mission.status.passes_completed = 1
    rec._clock.now = at(t)
    rec.run_finished("r1", mission, robot)
    await rec.run_pending_ops()


class TestRunEndWriter:
    async def run(self, tmp_path):
        rec, db, mission, robot = await start(tmp_path)
        await feed(rec, mission, robot, msg(mission, 0, 0), msg(mission, 2, 3),
                   msg(mission, 4, 11), msg(mission, 6, 21))
        run_id = fr.run_uuid("m1", "abcd1234")
        db.events[(uuid.uuid4(), at(14))] = {"code": "NAV.RECOVERY_ENTERED", "run_id": run_id,
                                              "payload": {"leg_seq": 3}}
        db.events[(uuid.uuid4(), at(15))] = {"code": "NAV.GOAL_BLOCKED", "run_id": run_id,
                                              "payload": {"leg_seq": 3}}
        await finish_run(rec, mission, robot)
        return rec, db, run_id

    async def test_legs_between_topomap_nodes_become_observations(self, tmp_path):
        rec, db, run_id = await self.run(tmp_path)
        rows = sorted(db.edge_obs.values(), key=lambda r: r["from_node"])
        # the first leg starts at the pass start (no topomap node): no observation
        assert [(r["from_node"], r["to_node"], r["map_id"], r["robot_name"], r["source"])
                for r in rows] == [("A", "B", "map1", "r1", "leg"),
                                   ("B", "C", "map1", "r1", "leg")]
        assert [r["source_key"] for r in rows] == [f"{run_id}:2", f"{run_id}:3"]
        assert [r["duration_s"] for r in rows] == [8.0, 10.0]
        # recoveries / blocks come from the events tagged with the leg
        assert [(r["recoveries"], r["blocks"]) for r in rows] == [(0, 0), (1, 1)]
        # stage C: the legs reported driving the whole time, so moving == duration (nothing stopped, no gap above
        # 30 s); the recovery seconds are the EXITED durations (none here), and neither is subtracted from moving
        assert [(r["moving_s"], r["stopped_s"], r["recovery_s"]) for r in rows] == [
            (8.0, 0.0, 0.0), (10.0, 0.0, 0.0)]

    async def test_recording_twice_does_not_double_count(self, tmp_path):
        rec, db, run_id = await self.run(tmp_path)
        info = SimpleNamespace(run_id=run_id, robot_name="r1", map_id="map1")
        await rec._record_edge_observations(FakeConnection(db), info)
        assert len(db.edge_obs) == 2

    async def test_a_failure_never_costs_the_run(self, tmp_path):
        rec, db, mission, robot = await start(tmp_path)
        await feed(rec, mission, robot, msg(mission, 0, 0), msg(mission, 2, 3),
                   msg(mission, 4, 11))
        db.fail = lambda sql, params: RuntimeError("x") if sql == store.INSERT_SQL else None
        await finish_run(rec, mission, robot)
        [run] = db.runs.values()
        assert run["state"] == "COMPLETED" and run["summary_metrics"]["leg_count"] == 2
        assert db.edge_obs == {}

    @pytest.mark.parametrize("name", ["DeadlockDetected", "LockNotAvailable", "QueryCanceled",
                                      "SerializationFailure"])
    async def test_lock_and_cancel_errors_of_the_optional_writer_are_swallowed(
            self, tmp_path, name, caplog):
        from psycopg import errors
        rec, db, run_id = await self.run(tmp_path)
        info = SimpleNamespace(run_id=run_id, robot_name="r1", map_id="map1")
        db.edge_obs.clear()
        db.fail = lambda sql, params: getattr(errors, name)() if sql == store.INSERT_SQL else None
        with caplog.at_level("WARNING"):
            await rec._record_edge_observations(FakeConnection(db), info)    # does not raise
        assert db.edge_obs == {} and "edge observations" in caplog.text

    async def test_a_broken_connection_or_other_operational_error_still_raises(self, tmp_path):
        import psycopg
        rec, db, run_id = await self.run(tmp_path)
        info = SimpleNamespace(run_id=run_id, robot_name="r1", map_id="map1")
        db.fail = lambda sql, params: (psycopg.OperationalError("server closed the connection")
                                       if sql == store.INSERT_SQL else None)
        with pytest.raises(psycopg.OperationalError):
            await rec._record_edge_observations(FakeConnection(db), info)
        db.fail = lambda sql, params: (psycopg.errors.DeadlockDetected()
                                       if sql == store.INSERT_SQL else None)
        conn = FakeConnection(db)
        conn.broken = True
        with pytest.raises(psycopg.errors.DeadlockDetected):
            await rec._record_edge_observations(conn, info)

    async def test_a_run_without_topomap_legs_writes_nothing(self, tmp_path):
        rec, db, mission, robot = await start(tmp_path, mission=make_mission(planned=None))
        await feed(rec, mission, robot, msg(mission, 0, 0), msg(mission, 2, 3),
                   msg(mission, 4, 11))
        await finish_run(rec, mission, robot)
        assert db.edge_obs == {}


# --- writer 2: a mapping session finishes ------------------------------------------------------

def node_row(i, x, t):
    return {"id": f"n{i}", "session_node_id": i, "x": x, "y": 0.0, "yaw": 0.0,
            "timestamp": secs(t).isoformat(), "depth": [], "costmap": []}


class FakeGraph:
    def __init__(self, rows, edges):
        self.rows, self.edges, self.fail = rows, edges, False
        self.edges_fail = False

    def session_trace_nodes(self, map_id, session_id):
        if self.fail:
            raise RuntimeError("arango down")
        return list(self.rows)

    def get_edges(self, map_id):
        return [{"from_node_id": a, "to_node_id": b} for a, b in self.edges]

    def get_edges_strict(self, map_id):
        if self.edges_fail:
            raise RuntimeError("arango down")
        return self.get_edges(map_id)


def session(purpose="mapping", robot="r1"):
    return {"session_id": uuid.uuid4(), "map_name": "yard", "robot_name": robot,
            "purpose": purpose}


# n0-n1-n2-n3 laid down 2 s apart, 1 m apart; the graph connects n0-n1 and n1-n2 only
ROWS = [node_row(i, float(i), 2 * i) for i in range(4)]
EDGES = [("n0", "n1"), ("n1", "n0"), ("n1", "n2"), ("n2", "n1")]


class TestSessionWriter:
    async def test_only_observations_on_existing_edges_are_stored(self):
        db, graph, s = FakeDB(), FakeGraph(ROWS, EDGES), session()
        assert await api.record_session(FakePool(db), graph, s) == 2
        assert sorted((r["from_node"], r["to_node"]) for r in db.edge_obs.values()) == \
            [("n0", "n1"), ("n1", "n2")]                      # n2 -> n3 has no edge: ignored
        assert {r["source_key"] for r in db.edge_obs.values()} == {
            f"{s['session_id']}:1", f"{s['session_id']}:2"}

    async def test_finishing_twice_does_not_double_count(self):
        db, graph, s = FakeDB(), FakeGraph(ROWS, EDGES), session()
        await api.record_session(FakePool(db), graph, s)
        assert await api.record_session(FakePool(db), graph, s) == 0
        assert len(db.edge_obs) == 2

    async def test_operate_sessions_and_failures_store_nothing_and_never_raise(self):
        db, graph = FakeDB(), FakeGraph(ROWS, EDGES)
        assert await api.record_session(FakePool(db), graph, session("operate")) == 0
        graph.fail = True
        assert await api.record_session(FakePool(db), graph, session()) == 0
        assert db.edge_obs == {}

    async def test_an_arango_edge_error_stores_nothing_and_warns_and_a_retry_recovers(self, caplog):
        db, graph, s = FakeDB(), FakeGraph(ROWS, EDGES), session()
        graph.edges_fail = True                              # nodes readable, edges not
        with caplog.at_level("WARNING"):
            assert await api.record_session(FakePool(db), graph, s) == 0
        assert db.edge_obs == {} and "backfill" in caplog.text
        graph.edges_fail = False                             # nothing marked it as done
        assert await api.record_session(FakePool(db), graph, s) == 2

    async def test_the_hook_runs_in_the_background_and_a_failure_is_only_logged(self, caplog):
        db, graph, s = FakeDB(), FakeGraph(ROWS, EDGES), session()
        task = api.schedule_record_session(FakePool(db), graph, s)
        assert task in api._tasks and db.edge_obs == {}      # returned before it ran
        await task
        assert task not in api._tasks and len(db.edge_obs) == 2
        graph.fail = True
        with caplog.at_level("WARNING"):
            await api.schedule_record_session(FakePool(db), graph, session())
        assert len(db.edge_obs) == 2 and not api._tasks

    async def test_the_finish_action_hook_failure_does_not_fail_the_finish(self, fdb):  # noqa: F811
        fdb.add_map("yard")
        row = fdb.add_session("yard", "r1", "live", ended=False, purpose="mapping")

        async def hook(_s):
            raise RuntimeError("boom")
        out = await maps.session_action(None, "yard", str(row["session_id"]), "finish", m1.PUB,
                                        on_finished=hook)
        assert out["changed"] is True

    async def test_the_finish_action_calls_the_hook_once_after_a_change(self, fdb):  # noqa: F811
        fdb.add_map("yard")
        row = fdb.add_session("yard", "r1", "live", ended=False, purpose="mapping")
        seen = []

        async def hook(s):
            seen.append(str(s["session_id"]))
        sid = str(row["session_id"])
        await maps.session_action(None, "yard", sid, "finish", m1.PUB, on_finished=hook)
        await maps.session_action(None, "yard", sid, "finish", m1.PUB, on_finished=hook)
        assert seen == [sid]                                  # the repeat changed nothing


# --- GET /api/v1/maps/{map_id}/edge-stats -------------------------------------------------------

class FakeMaps:
    """maps.open_store over a FakeDB: a map `yard` exists, no other."""

    def __init__(self, db):
        self.db = db

    def __call__(self, _db, _publisher):
        outer = self

        class Ctx:
            async def __aenter__(self_inner):
                async def get_map(name):
                    return object() if name == "yard" else None
                return SimpleNamespace(get_map=get_map, cursor=FakeConnection(outer.db).cursor())

            async def __aexit__(self_inner, *exc):
                return False
        return Ctx()


@pytest.fixture
def seeded(monkeypatch):
    db = FakeDB()
    rows = ([obs(i, duration=float(i + 1), src="A", dst="B") for i in range(22)]
            + [obs(i, src="B", dst="C", robot="r2", source="mapping") for i in range(3)]
            + [obs(0, src="X", dst="Y")]                                  # no such edge
            + [obs(i, src="C", dst="D", map_id="other") for i in range(2)])
    for r in rows:                                       # as stored: no prune here
        db.edge_obs[tuple(r[c] for c in store.COLUMNS[:6])] = r
    monkeypatch.setattr(maps, "open_store", FakeMaps(db))
    monkeypatch.setattr(api, "CACHE", ResultCache(4, 60.0))
    graph = FakeGraph([], [("A", "B"), ("B", "C"), ("C", "D")])
    return db, graph


def put(db, *rows):
    for r in rows:
        db.edge_obs[tuple(r[c] for c in store.COLUMNS[:6])] = r


class TestRoute:
    async def test_contract_window_and_non_edges(self, seeded):
        db, graph = seeded
        out = await api.edge_stats(None, graph, "yard")
        assert {k: out[k] for k in ("map_id", "robot", "min_count", "window", "total", "limit",
                                    "offset", "next_offset")} == {
            "map_id": "yard", "robot": None, "min_count": 1, "window": 20, "total": 2,
            "limit": 100, "offset": 0, "next_offset": None}
        first, second = out["items"]
        assert set(first) == {"from_node_id", "to_node_id", "robot", "count", "median_s", "p90_s",
                              "median_speed_mps", "last_observed_at", "recoveries", "blocks",
                              "sources", "median_moving_s", "moving_speed_mps", "recovery_s",
                              "moving_count"}
        assert (first["from_node_id"], first["to_node_id"], first["robot"]) == ("A", "B", "r1")
        assert first["count"] == 20 and first["median_s"] == 12.5     # durations 3..22
        assert second["sources"] == {"leg": 0, "mapping": 3} and second["robot"] == "r2"

    async def test_filters_and_paging(self, seeded):
        db, graph = seeded
        assert (await api.edge_stats(None, graph, "yard", robot="r2"))["total"] == 1
        assert (await api.edge_stats(None, graph, "yard", min_count=4))["total"] == 1
        page = await api.edge_stats(None, graph, "yard", limit=1)
        assert len(page["items"]) == 1 and page["next_offset"] == 1 and page["total"] == 2
        last = await api.edge_stats(None, graph, "yard", limit=1, offset=1)
        assert last["items"][0]["robot"] == "r2" and last["next_offset"] is None
        assert (await api.edge_stats(None, graph, "yard", offset=5))["items"] == []

    @pytest.mark.parametrize("kw", [{"limit": 0}, {"limit": 201}, {"offset": -1},
                                    {"min_count": 0}])
    async def test_bad_query_is_422(self, seeded, kw):
        with pytest.raises(HTTPException) as exc:
            await api.edge_stats(None, seeded[1], "yard", **kw)
        assert exc.value.status_code == 422

    async def test_an_arango_error_is_503_not_an_empty_answer(self, seeded):
        seeded[1].edges_fail = True
        with pytest.raises(HTTPException) as exc:
            await api.edge_stats(None, seeded[1], "yard")
        assert exc.value.status_code == 503 and "ArangoDB" in exc.value.detail

    async def test_unknown_map_is_404(self, seeded):
        with pytest.raises(HTTPException) as exc:
            await api.edge_stats(None, seeded[1], "nope")
        assert exc.value.status_code == 404

    async def test_pooled_groups_across_robots_and_echoes_the_flag(self, seeded):
        db, graph = seeded
        # r2 drove A->B too: 3 newer, slower observations (durations 100 s)
        put(db, *[obs(30 + i, duration=100.0, src="A", dst="B", robot="r2") for i in range(3)])
        out = await api.edge_stats(None, graph, "yard", pooled=True)
        assert out["pooled"] is True and out["robot"] is None and out["total"] == 2
        ab, bc = out["items"]
        assert (ab["from_node_id"], ab["to_node_id"], ab["robot"]) == ("A", "B", None)
        # the newest 20 of all 25 rows: the 3 r2 rows + r1's newest 17 (durations 6..22, median of the 20 = 15.5)
        assert ab["count"] == 20 and ab["median_s"] == 15.5 and ab["sources"] == {"leg": 20, "mapping": 0}
        assert bc["robot"] is None and bc["count"] == 3
        per_robot = await api.edge_stats(None, graph, "yard")
        assert per_robot["pooled"] is False and per_robot["total"] == 3
        assert [i["robot"] for i in per_robot["items"]] == ["r1", "r2", "r2"]

    async def test_pooled_is_ignored_when_a_robot_is_given(self, seeded):
        out = await api.edge_stats(None, seeded[1], "yard", robot="r2", pooled=True)
        assert out["pooled"] is False and out["robot"] == "r2" and out["items"][0]["robot"] == "r2"

    async def test_the_grouped_list_is_cached_but_the_map_check_and_filters_are_not(self, seeded, monkeypatch):
        db, graph = seeded
        reads = []
        real = store.fetch

        async def counting(cursor, map_id, robot=None, pooled=False):
            reads.append((map_id, robot, pooled))
            return await real(cursor, map_id, robot, pooled)
        monkeypatch.setattr(store, "fetch", counting)
        await api.edge_stats(None, graph, "yard", limit=1)
        page2 = await api.edge_stats(None, graph, "yard", limit=1, offset=1)
        thin = await api.edge_stats(None, graph, "yard", min_count=4)
        assert len(reads) == 1                                  # one read for three requests
        assert page2["items"][0]["robot"] == "r2" and thin["total"] == 1
        await api.edge_stats(None, graph, "yard", pooled=True)  # another key
        await api.edge_stats(None, graph, "yard", robot="r1")
        assert len(reads) == 3 and reads[1:] == [("yard", None, True), ("yard", "r1", False)]
        with pytest.raises(HTTPException) as exc:               # 404 on every request
            await api.edge_stats(None, graph, "nope")
        assert exc.value.status_code == 404 and len(reads) == 3

    async def test_a_503_is_not_cached(self, seeded):
        db, graph = seeded
        graph.edges_fail = True
        with pytest.raises(HTTPException):
            await api.edge_stats(None, graph, "yard")
        graph.edges_fail = False
        assert (await api.edge_stats(None, graph, "yard"))["total"] == 2

    async def test_the_route_passes_through(self, seeded, monkeypatch):
        monkeypatch.setattr(main, "service", SimpleNamespace(database=None, graph_db=seeded[1]))
        out = await main.map_edge_stats("yard", robot="r1", min_count=1, limit=5, offset=0)
        assert out["total"] == 1 and out["limit"] == 5 and out["robot"] == "r1"
        pooled = await main.map_edge_stats("yard", robot=None, min_count=1, limit=5, offset=0,
                                           pooled=True)
        assert pooled["pooled"] is True and {i["robot"] for i in pooled["items"]} == {None}


# --- the backfill ------------------------------------------------------------------------------

class FakeSources:
    def __init__(self):
        self.leg_rows = [
            {"run_id": "run1", "seq": 2, "robot_name": "r1", "map_id": "yard",
             "from_topomap_node": "n0", "to_topomap_node": "n1", "ended_at": secs(100),
             "duration_s": 3.0, "straight_m": 1.0, "recoveries": 1, "blocks": 0},
            {"run_id": "run1", "seq": 3, "robot_name": "r1", "map_id": "yard",   # not an edge
             "from_topomap_node": "n1", "to_topomap_node": "n3", "ended_at": secs(110),
             "duration_s": 3.0, "straight_m": 2.0, "recoveries": 0, "blocks": 0},
            {"run_id": "run1", "seq": 4, "robot_name": "r1", "map_id": "yard",   # zero time
             "from_topomap_node": "n1", "to_topomap_node": "n2", "ended_at": secs(120),
             "duration_s": 0.0, "straight_m": 1.0, "recoveries": 0, "blocks": 0}]
        self.session_list = [session(), session("operate")]
        self.graph = FakeGraph(ROWS, EDGES)

    def legs(self):
        return iter(self.leg_rows)

    def sessions(self):
        return list(self.session_list)

    def count_fill(self):
        return 7

    def session_rows(self, map_id, session_id):
        return self.graph.session_trace_nodes(map_id, session_id)

    def edges(self, map_id):
        return self.graph.get_edges_strict(map_id)


class FakeWrite:
    def __init__(self, db):
        self.db, self.opened = db, 0

    def __call__(self):
        self.opened += 1
        outer = self

        class Ctx:
            def __enter__(self):
                return SyncCursor(outer.db)

            def __exit__(self, *exc):
                return False
        return Ctx()


class SyncCursor:
    """The synchronous face of the FakeDB cursor (the script uses a sync psycopg cursor)."""

    def __init__(self, db):
        self.db, self.rowcount = db, 0

    def execute(self, sql, params=None):
        cursor = FakeConnection(self.db).cursor()
        self.db.execute(cursor, sql, params)
        self.rowcount = cursor.rowcount


class TestBackfill:
    def test_collects_legs_and_sessions_on_edges_only(self):
        rows, counts = bf.collect(FakeSources(), now=secs(1000))
        assert sorted((o["source"], o["from_node"], o["to_node"]) for o in rows) == [
            ("leg", "n0", "n1"), ("mapping", "n0", "n1"), ("mapping", "n1", "n2")]
        assert counts["legs"] == 3 and counts["leg_unusable"] == 1       # the zero-time leg
        assert counts["non_edge"] == 2                  # leg n1->n3, mapping step n2->n3
        assert counts["sessions"] == 1                  # the operate session is skipped

    def test_dry_run_is_the_default_and_writes_nothing(self):
        db, write = FakeDB(), FakeWrite(FakeDB())
        out = bf.run(FakeSources(), write, now=secs(1000))
        assert out["apply"] is False and write.opened == 0 and db.edge_obs == {}

    def test_apply_is_idempotent_and_matches_the_live_writers(self):
        db = FakeDB()
        sources = FakeSources()
        out = bf.run(sources, FakeWrite(db), apply=True, now=secs(1000))
        assert out["inserted"] == 3 and len(db.edge_obs) == 3
        assert bf.run(sources, FakeWrite(db), apply=True, now=secs(1000))["inserted"] == 0
        assert len(db.edge_obs) == 3
        leg = db.edge_obs[("yard", "n0", "n1", "r1", "leg", "run1:2")]
        assert leg["recoveries"] == 1

    def test_fill_new_columns_only_fills_null_columns_of_leg_rows_and_is_idempotent(self):
        db = FakeDB()
        sources = FakeSources()
        bf.run(sources, FakeWrite(db), apply=True, now=secs(1000))
        key = ("yard", "n0", "n1", "r1", "leg", "run1:2")
        mapping = [k for k in db.edge_obs if k[4] == "mapping"][0]
        assert db.edge_obs[key]["stopped_s"] is None and db.edge_obs[key]["recovery_s"] is None
        # run_legs has stopped / recovery for the old leg (since 20261004) and no moving_s (recorded before stage C)
        db.legs[("run1", 2)] = {"stopped_s": 1.5, "recovery_s": 2.5, "moving_s": None}
        db.edge_obs[mapping]["stopped_s"] = None
        out = bf.run(sources, FakeWrite(db), apply=True, fill=True, now=secs(1000))
        assert out["inserted"] == 0 and out["filled"] == 1
        row = db.edge_obs[key]
        assert (row["stopped_s"], row["recovery_s"], row["moving_s"]) == (1.5, 2.5, None)
        assert db.edge_obs[mapping]["stopped_s"] is None                  # a mapping row is never touched
        # a value that is already there is not overwritten, and a second run changes nothing
        row["stopped_s"] = 9.0
        assert bf.run(sources, FakeWrite(db), apply=True, fill=True, now=secs(1000))["filled"] == 0
        assert row["stopped_s"] == 9.0

    def test_without_the_flag_nothing_is_filled_and_the_dry_run_only_counts(self):
        db = FakeDB()
        sources = FakeSources()
        bf.run(sources, FakeWrite(db), apply=True, now=secs(1000))
        db.legs[("run1", 2)] = {"stopped_s": 1.5, "recovery_s": 2.5, "moving_s": None}
        assert "filled" not in bf.run(sources, FakeWrite(db), apply=True, now=secs(1000))
        assert db.edge_obs[("yard", "n0", "n1", "r1", "leg", "run1:2")]["stopped_s"] is None
        dry = bf.run(sources, FakeWrite(db), fill=True, now=secs(1000))
        assert dry["to_fill"] == 7 and "filled" not in dry
        assert "UPDATE" in bf.FILL_SQL and bf.FILL_COUNT_SQL.startswith("SELECT count(*)")

    def test_keeps_every_observation_within_the_retention(self):
        sources = FakeSources()
        sources.leg_rows = [dict(sources.leg_rows[0], seq=i, ended_at=secs(i), duration_s=1.0 + i)
                            for i in range(1, 31)]
        sources.session_list = []
        rows, counts = bf.collect(sources, now=secs(1000))
        assert len(rows) == 30 and counts["to_store"] == 30 and counts["too_old"] == 0
        rows, counts = bf.collect(sources, now=T0 + datetime.timedelta(days=366))
        assert rows == [] and counts["too_old"] == 30                   # past EDGE_STATS_RETENTION_DAYS
