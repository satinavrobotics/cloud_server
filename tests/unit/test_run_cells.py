"""packages/api/run_cells.py (the per-run cell summary and its worker) and packages/utils/run_cells_db.py, with fakes
and small synthetic runs (docs/region_stats_2026-10-27.md 6.1, 6.2, 8)."""
import asyncio
import contextlib
import datetime
import json
import os
import uuid

os.environ.setdefault("POSTGRES_PASSWORD", "test")

import numpy as np  # noqa: E402
import pytest  # noqa: E402

pytest.importorskip("psycopg")

from cloud_common.objects.robot import RobotTypeIdentifierV1  # noqa: E402
from packages import config  # noqa: E402
from packages.api import run_cells as rc  # noqa: E402
from packages.utils import cell_raster as cr  # noqa: E402
from packages.utils import run_cells_db as rdb  # noqa: E402

pytestmark = pytest.mark.unit

T0 = datetime.datetime(2026, 10, 20, 12, 0, tzinfo=datetime.timezone.utc)
E0 = T0.timestamp()
FS = RobotTypeIdentifierV1(agv_class="AMR", length=0.8, width=0.5, speed_max=1.2)


def track(xs, y=0.05, t0=E0, dt=1.5, session="s1", epoch="e1", map_id="m1"):
    return [(t0 + i * dt, x, y, session, epoch, map_id) for i, x in enumerate(xs)]


def data(track_rows=(), legs=(), recoveries=(), blocks=(), map_id="m1"):
    return rc.RunData(uuid.UUID(int=7), "r1", map_id, T0, T0 + datetime.timedelta(minutes=5),
                      list(track_rows), list(recoveries), list(blocks), list(legs))


NODES = {"m1": {"a": (0.0, 0.05), "b": (4.0, 0.05)}}


def nodes(map_id):
    return NODES.get(map_id, {})


def cell_totals(row):
    return {"len_m": float(row.len_m.sum()), "time_s": float(row.time_s.sum()),
            "stopped_s": float(row.stopped_s.sum()), "recovery_s": float(row.recovery_s.sum()),
            "blocks": int(row.blocks.sum())}


class TestSummariseRows:
    def test_array_totals_equal_segment_totals(self):
        xs = [0.0, 0.5, 1.0, 1.0, 1.0, 1.5, 2.0]          # two good moves, a stop, two more
        d = data(track(xs), recoveries=[(E0 + 6.0, 3.0)])
        [row] = rc.summarise_rows(d, nodes, FS)
        assert row.source == "track" and row.map_id == "m1" and row.robot_class == "AMR|0.80x0.50|1.20"
        assert row.speed_max == pytest.approx(1.2) and row.algo_version == config.RUN_CELLS_ALGO_VERSION
        got = cell_totals(row)
        assert got["len_m"] == pytest.approx(row.totals["len_m"]) == pytest.approx(2.0)
        assert got["time_s"] == pytest.approx(row.totals["time_s"]) == pytest.approx(9.0)
        assert got["stopped_s"] == pytest.approx(row.totals["stopped_s"]) == pytest.approx(3.0)
        assert got["recovery_s"] == pytest.approx(row.totals["recovery_s"]) == pytest.approx(3.0)
        assert row.totals["segments"] == 6 and row.totals["rejected"] == 0
        assert np.all(np.diff(row.key) > 0)                # sorted, distinct
        assert row.frames == [["s1", "e1"]]

    def test_matches_the_raw_drive_stats_tiles(self):
        from packages.api import drive_stats as ds
        rows = track([0.0, 0.4, 0.8, 0.8, 1.2, 1.2, 1.6])
        [row] = rc.summarise_rows(data(rows), nodes, FS)
        loaded = ds.Loaded()
        loaded.track_rows = [("run", r[0], r[1], r[2], r[3], r[4]) for r in rows]
        tiles, _, _, _ = ds.compute(loaded, lambda: {}, ds.Params())
        assert np.array_equal(tiles.key, row.key)
        assert np.allclose(tiles.sums[0], row.len_m) and np.allclose(tiles.sums[1], row.time_s)
        assert np.allclose(tiles.sums[2], row.recovery_s) and np.allclose(tiles.sums[3], row.stopped_s)

    def test_two_frame_groups_do_not_join(self):
        rows = track([0.0, 0.5, 1.0]) + track([5.0, 5.5, 6.0], t0=E0 + 4.0, session="s2", epoch="e2")
        [row] = rc.summarise_rows(data(rows), nodes, FS)
        assert row.totals["segments"] == 4 and row.totals["len_m"] == pytest.approx(2.0)
        assert row.frames == [["s1", "e1"], ["s2", "e2"]]

    def test_recoveries_spread_over_the_segments_they_overlap(self):
        d = data(track([0.0, 0.5, 1.0, 1.5]), recoveries=[(E0 + 3.0, 2.0)])      # E0+1 .. E0+3
        [row] = rc.summarise_rows(d, nodes, FS)
        assert row.totals["recovery_s"] == pytest.approx(2.0) and row.recovery_s.sum() == pytest.approx(2.0)
        assert row.totals["recovery_unattributed_s"] == 0.0

    def test_blocks_are_placed_on_the_cells_and_counted_once(self):
        d = data(track([0.0, 0.5, 1.0, 1.5]), blocks=[(E0 + 2.25, None), (E0 + 2.3, 4), (E0 + 900, None)])
        [row] = rc.summarise_rows(d, nodes, FS)
        assert row.blocks.sum() == 2 and row.totals["blocks"] == 2 and row.totals["blocks_unattributed"] == 1
        ax, _ = cr.key_cells(row.key[row.blocks > 0])
        assert set(ax.tolist()) <= {7, 8}                                # x about 0.75

    def test_track_preferred_over_legs(self):
        leg = (1, "a", "b", 10.0, 0.0, 0.0, E0, E0 + 10, "m1")
        [row] = rc.summarise_rows(data(track([0.0, 0.5, 1.0]), legs=[leg]), nodes, FS)
        assert row.source == "track" and row.totals["len_m"] == pytest.approx(1.0)

    def test_legs_source_and_leg_blocks(self):
        legs = [(1, "a", "b", 10.0, 2.0, 1.0, E0, E0 + 10, "m1"), (2, "b", "zz", 5.0, 0.0, 0.0, E0 + 10, E0 + 15, "m1")]
        d = data(legs=legs, blocks=[(E0 + 3, 1), (E0 + 100, None)])
        [row] = rc.summarise_rows(d, nodes, FS)
        assert row.source == "legs" and row.totals["legs_skipped"] == 1
        assert row.totals["len_m"] == pytest.approx(4.0) and row.totals["stopped_s"] == pytest.approx(2.0)
        assert row.totals["blocks"] == 1 and row.totals["blocks_unattributed"] == 1
        assert row.blocks.sum() == 1 and cell_totals(row)["time_s"] == pytest.approx(10.0)

    def test_empty_run_gets_an_empty_row(self):
        [row] = rc.summarise_rows(data(blocks=[(E0, None)]), nodes, None)
        assert row.source == "empty" and len(row) == 0 and row.robot_class == "" and row.speed_max is None
        assert row.totals["blocks_unattributed"] == 1
        assert rc.summarise_rows(data(map_id=None), nodes, None) == []

    def test_per_map_split(self):
        rows = track([0.0, 0.5, 1.0]) + track([30.0, 30.5, 31.0], t0=E0 + 4.0, session="s2", map_id="m2")
        out = rc.summarise_rows(data(rows), nodes, FS)
        assert [r.map_id for r in out] == ["m1", "m2"]
        assert all(r.totals["len_m"] == pytest.approx(1.0) for r in out)

    def test_pose_jump_and_gap_are_rejected_not_counted(self):
        rows = track([0.0, 0.5, 50.0, 50.5])                 # 49.5 m in 1.5 s
        [row] = rc.summarise_rows(data(rows), nodes, FS)
        assert row.totals["rejected"] == 1 and row.totals["len_m"] == pytest.approx(1.0)


class Cur:
    """Fake async cursor of rdb.insert / fetch: records the statement, answers a canned fetch."""

    def __init__(self, rows=()):
        self.calls, self.rows, self.rowcount = [], list(rows), 1

    async def execute(self, sql, params=None):
        self.calls.append((sql, params))

    async def fetchall(self):
        return self.rows


def a_row(**kw):
    key = cr.cell_key(np.array([3, 4, 4]), np.array([1, 1, 2]))
    base = dict(run_id=uuid.UUID(int=1), map_id="m1", robot_name="r1", robot_class="AMR|0.80x0.50|1.20", speed_max=1.2,
                started_at=T0, ended_at=T0, source="track", algo_version=1, cell_m=0.1,
                params={"min_speed": 0.05}, frames=[["s", "e"]], key=key, len_m=np.array([0.1, 0.2, 0.3]),
                time_s=np.array([1.0, 2.0, 3.0]), stopped_s=np.zeros(3), recovery_s=np.array([0.0, 0.5, 0.0]),
                blocks=np.array([0, 1, 0]), totals={"len_m": 0.6})
    base.update(kw)
    return rdb.CellRow(**base)


class TestRunCellsDb:
    async def test_encode_decode_round_trip(self):
        row = a_row()
        params = rdb.encode(row)
        assert params[12] == [3, 4, 4] and params[13] == [1, 1, 2] and params[-1] == "m1"
        stored = [params[i] for i in range(len(rdb.COLUMNS) - 1)] + [T0]
        back = rdb.decode(stored)
        assert np.array_equal(back.key, row.key) and np.allclose(back.len_m, row.len_m)
        assert np.array_equal(back.blocks, row.blocks) and back.totals == row.totals and back.frames == row.frames
        assert back.speed_max == 1.2 and back.created_at == T0

    async def test_insert_is_an_upsert_guarded_by_the_map(self):
        cur = Cur()
        assert await rdb.insert(cur, a_row()) is True
        sql = cur.calls[0][0]
        assert "ON CONFLICT (run_id, map_id) DO UPDATE" in sql and "lifecycle NOT IN ('DELETING', 'DELETED')" in sql
        cur.rowcount = 0                                   # the map is gone: nothing written
        assert await rdb.insert(cur, a_row()) is False

    async def test_fetch_filters(self):
        cur = Cur()
        await rdb.fetch(cur, "m1", T0, None, "r1", "AMR", limit=5, algo_version=1)
        sql, args = cur.calls[0]
        assert "started_at >= %s" in sql and "robot_name = %s" in sql and "robot_class = %s" in sql
        assert "algo_version = %s" in sql and sql.endswith("LIMIT %s")
        assert args == ("m1", 1, T0, "r1", "AMR", 5)


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class TestWorker:
    def worker(self, monkeypatch, pending, builds=None, stale=None):
        clock = FakeClock()
        w = rc.RunCellsWorker(object(), object(), build=builds, stale_maps=stale, clock=clock)
        calls = {"pending": 0, "summarised": [], "failing": set()}

        async def fake_pending(limit):
            calls["pending"] += 1
            blocked = w.retry_blocked()
            return [p for p in pending if p[0] not in blocked and p[0] not in calls["summarised"]][:limit]

        async def fake_factsheets(db):
            return {}

        async def fake_summarise(db, graph, robots, run_id):
            if run_id in calls["failing"]:
                raise RuntimeError("boom")
            calls["summarised"].append(run_id)
            return [a_row(run_id=run_id, map_id=dict(pending)[run_id])]

        async def fake_store(db, rows):
            return len(rows)
        w.pending = fake_pending
        monkeypatch.setattr(rc, "factsheets", fake_factsheets)
        monkeypatch.setattr(rc, "summarise_run", fake_summarise)
        monkeypatch.setattr(rc, "store", fake_store)
        return w, clock, calls

    async def test_summarises_and_debounces_the_map_build(self, monkeypatch):
        built = []

        async def build(map_id):
            built.append(map_id)
        w, clock, calls = self.worker(monkeypatch, [("r1", "m1"), ("r2", "m1"), ("r3", "m2")], builds=build)
        out = await w.step()
        assert out == {"summarised": 3, "failed": 0, "built": 0} and built == []     # debounce not over
        clock.now += config.REGION_STATS_DEBOUNCE_S - 1
        assert (await w.step())["built"] == 0
        clock.now += 2
        assert (await w.step())["built"] == 2 and sorted(built) == ["m1", "m2"]
        assert (await w.step())["built"] == 0 and w.dirty == {}                      # nothing dirty any more

    async def test_failing_run_is_skipped_until_the_retry_time_and_never_blocks_the_batch(self, monkeypatch):
        w, clock, calls = self.worker(monkeypatch, [("bad", "m1"), ("ok", "m1")])
        calls["failing"].add("bad")
        out = await w.step()
        assert out["summarised"] == 1 and out["failed"] == 1 and calls["summarised"] == ["ok"]
        assert "bad" in w.failed
        clock.now += config.RUN_CELLS_RETRY_S - 1
        await w.step()
        assert "bad" in w.failed and "bad" not in calls["summarised"]
        clock.now += 2
        calls["failing"].clear()
        await w.step()
        assert "bad" not in w.failed and "bad" in calls["summarised"]

    async def test_stale_maps_are_built_like_dirty_ones(self, monkeypatch):
        built = []

        async def build(map_id):
            built.append(map_id)

        async def stale():
            return ["m9"]
        w, clock, _ = self.worker(monkeypatch, [], builds=build, stale=stale)
        await w.step()
        clock.now += config.REGION_STATS_DEBOUNCE_S + 1
        await w.step()
        assert built == ["m9"]

    async def test_a_failed_build_is_retried_after_the_debounce(self, monkeypatch):
        attempts = []

        async def build(map_id):
            attempts.append(map_id)
            if len(attempts) == 1:
                raise RuntimeError("no scene graph read")
        w, clock, _ = self.worker(monkeypatch, [("r1", "m1")], builds=build)
        await w.step()
        clock.now += config.REGION_STATS_DEBOUNCE_S + 1
        await w.step()
        assert attempts == ["m1"] and "m1" in w.dirty
        clock.now += config.REGION_STATS_DEBOUNCE_S + 1
        await w.step()
        assert attempts == ["m1", "m1"] and w.dirty == {}


class LockConn:
    def __init__(self, server):
        self.server, self.closed = server, False

    async def execute(self, sql, params=None):
        assert sql.startswith("SELECT pg_try_advisory_lock") or sql == "SELECT 1", sql
        if sql == "SELECT 1":
            return _One((1,))
        key = params[0]
        self.server.setdefault(key, self)
        return _One((self.server[key] is self,))

    async def close(self):
        self.closed = True


class _One:
    def __init__(self, row):
        self.row = row

    async def fetchone(self):
        return self.row


class TestElection:
    async def test_only_the_lock_holder_runs_passes(self, monkeypatch):
        server = {}

        class Db:
            async def dedicated_connection(self):
                return LockConn(server)
        ticks = []
        a, b = rc.RunCellsWorker(Db(), object()), rc.RunCellsWorker(Db(), object())
        for w, name in ((a, "a"), (b, "b")):
            async def step(name=name):
                ticks.append(name)
            monkeypatch.setattr(w, "step", step)
            w.start()
        try:
            await a.election.step()
            await b.election.step()
            await a.election.step()
            await b.election.step()
            assert a.election.holding and not b.election.holding
            assert ticks == ["a"]                    # the first step acquires; the second runs the pass
        finally:
            await a.stop()
            await b.stop()

    async def test_disabled_worker_does_not_start(self, monkeypatch):
        monkeypatch.setattr(config, "RUN_CELLS_ENABLED", False)
        w = rc.RunCellsWorker(object(), object())
        w.start()
        assert w.election is None


class TestLoadRun:
    class Cursor:
        def __init__(self, track_rows):
            self.sql, self.rows, self.track_rows = [], [], track_rows

        async def execute(self, sql, params=None):
            self.sql.append(sql)
            if sql == rc.RUN_SQL:
                self.rows = [(uuid.UUID(int=1), "r1", "m1", T0, T0 + datetime.timedelta(minutes=1))]
            elif sql == rc.TRACK_SQL:
                self.rows = self.track_rows
            elif sql == rc.EVENTS_SQL:
                self.rows = [("NAV.RECOVERY_EXITED", E0 + 5, 2.0, None), ("NAV.RECOVERY_EXITED", E0 + 6, None, None),
                             ("NAV.GOAL_BLOCKED", E0 + 3, None, None), ("MISSION.EDGE_BLOCKED", E0 + 4, None, 2)]
            else:
                self.rows = []

        async def fetchone(self):
            return self.rows[0] if self.rows else None

        async def fetchall(self):
            return self.rows

    async def test_legs_are_read_only_without_a_track(self):
        cur = self.Cursor([(E0, 1.0, 2.0, None, None, "m1")])
        d = await rc.load_run(cur, uuid.UUID(int=1))
        assert rc.LEGS_SQL not in cur.sql and len(d.track) == 1
        assert d.recoveries == [(E0 + 5, 2.0)] and d.blocks == [(E0 + 3, None), (E0 + 4, 2)]
        cur = self.Cursor([])
        await rc.load_run(cur, uuid.UUID(int=1))
        assert rc.LEGS_SQL in cur.sql
