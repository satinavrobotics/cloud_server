"""The planner's snapshot cache (packages/services/mission_planner/region_snapshots.py): keyed by (map, graph version, class, stamp,
model hash), read through region_stats_load and built by region_cost.build_snapshot; used only for the version it was read for;
a placeholder speed_max reads nothing; a failing read is the neutral "load_error". Plus the server's mode gate (`_region_for`)."""
import asyncio
from types import SimpleNamespace as NS

import pytest

from packages import config
from packages.services.mission_planner import region_snapshots as rs
from packages.utils import region_cost as rc
from packages.utils import region_stats_load as load

from .test_region_cost import A, B, CLS, KEYS, stat, stats

pytestmark = pytest.mark.unit

FS = NS(agv_class="CARRIER", length=0.5, width=0.5, speed_max=0.24, angular_speed_max=0.5)
PLACEHOLDER = NS(agv_class="CARRIER", length=-1, width=-1, speed_max=1.0, angular_speed_max=-1)


class Conn:
    """A stand-in for `database.connection()`; `reads` counts the cursors opened, `rows` is what a read returns."""

    def __init__(self):
        self.opened = 0

    def __call__(self):
        return self

    async def __aenter__(self):
        self.opened += 1
        return self

    async def __aexit__(self, *exc):
        return False

    def cursor(self):
        return self


@pytest.fixture
def world(monkeypatch):
    """A fake region_stats table: world.table[(version, class)] -> RegionStats; world.reads counts `aload` calls."""
    w = NS(table={}, reads=[], fail=None)

    async def aload(cur, map_id, version, cls=load.POOLED):
        w.reads.append((map_id, version, cls))
        if w.fail is not None:
            raise w.fail
        return w.table.get((version, cls)) or load.RegionStats(map_id, version, cls, {}, None, None, None)
    monkeypatch.setattr(load, "aload", aload)
    return w


def fill(world, version=3, stamp="st1", rows=(A, B)):
    world.table[(version, CLS)] = stats(list(rows), version=version, stamp=stamp)
    world.table[(version, "*")] = stats(list(rows), cls="*", version=version, stamp=stamp)


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_a_snapshot_is_built_from_the_stats_of_the_version(world):
    fill(world)
    snaps = rs.RegionSnapshots(Conn(), Clock())
    got = run(snaps.get("m", 3, KEYS, FS))
    assert got.status == "ok" and got.graph_version == 3 and got.robot_class == CLS and got.stamp == "st1"
    assert got.rho_of("A") > 0 and got.model_hash == rc.CostModel.from_config().model_hash
    assert world.reads == [("m", 3, CLS), ("m", 3, "*")]


def test_cache_hit_within_the_ttl_reads_nothing(world):
    fill(world)
    clock = Clock()
    snaps = rs.RegionSnapshots(Conn(), clock, ttl_s=30)
    first = run(snaps.get("m", 3, KEYS, FS))
    clock.t += 29
    assert run(snaps.get("m", 3, KEYS, FS)) is first
    assert len(world.reads) == 2


def test_after_the_ttl_the_same_stamp_reuses_the_built_snapshot(world):
    fill(world)
    clock = Clock()
    snaps = rs.RegionSnapshots(Conn(), clock, ttl_s=30)
    first = run(snaps.get("m", 3, KEYS, FS))
    clock.t += 31
    assert run(snaps.get("m", 3, KEYS, FS)) is first
    assert len(world.reads) == 4 and snaps.builds == 1                  # re-read, not rebuilt


def test_a_new_stamp_is_a_new_snapshot_and_the_old_one_stays_cached(world):
    fill(world, stamp="st1")
    clock = Clock()
    snaps = rs.RegionSnapshots(Conn(), clock, ttl_s=30)
    first = run(snaps.get("m", 3, KEYS, FS))
    fill(world, stamp="st2", rows=(stat("A", 2, 10.0, 30.0), B))
    clock.t += 31
    second = run(snaps.get("m", 3, KEYS, FS))
    assert second.stamp == "st2" and second is not first and second.rho_of("A") != first.rho_of("A")
    assert snaps.stats()["entries"] == 2 and first.key != second.key


def test_each_version_class_and_model_has_its_own_snapshot(world, monkeypatch):
    fill(world, version=3)
    fill(world, version=4)
    snaps = rs.RegionSnapshots(Conn(), Clock())
    s3, s4 = run(snaps.get("m", 3, KEYS, FS)), run(snaps.get("m", 4, KEYS, FS))
    assert (s3.graph_version, s4.graph_version) == (3, 4) and s3 is not s4
    monkeypatch.setattr(config, "REGION_COST_N0", 9.0)                  # another model: another hash, another entry
    s3b = run(snaps.get("m", 3, KEYS, FS))
    assert s3b.model_hash != s3.model_hash and s3b.key != s3.key


def test_stats_of_another_version_are_neutral_with_a_reason(world):
    """The rows are asked for by the planned version, so a table that only has another version's gives no_build_for_version; a
    build of another version handed back (a stale cache row) is version_mismatch."""
    fill(world, version=2)
    snaps = rs.RegionSnapshots(Conn(), Clock())
    got = run(snaps.get("m", 3, KEYS, FS))
    assert got.status == "no_build_for_version" and not got.active and got.rho_max == 0
    world.table[(5, CLS)] = stats([A, B], version=4)
    world.table[(5, "*")] = stats([A, B], cls="*", version=4)
    assert run(snaps.get("m", 5, KEYS, FS)).status == "version_mismatch"


def test_a_placeholder_speed_reads_nothing(world):
    fill(world)
    snaps = rs.RegionSnapshots(Conn(), Clock())
    got = run(snaps.get("m", 3, KEYS, PLACEHOLDER))
    assert got.status == "no_speed_max" and world.reads == []
    assert run(snaps.get("m", 3, KEYS, None)).status == "no_speed_max" and world.reads == []


def test_the_robot_class_comes_from_the_factsheet(world):
    fill(world)
    other = NS(agv_class="TUGGER", length=1.0, width=0.8, speed_max=0.5)
    snaps = rs.RegionSnapshots(Conn(), Clock())
    run(snaps.get("m", 3, KEYS, other))
    assert world.reads[0] == ("m", 3, "TUGGER|1.00x0.80")


def test_a_failing_read_is_load_error_and_not_cached(world):
    fill(world)
    world.fail = RuntimeError("db down")
    snaps = rs.RegionSnapshots(Conn(), Clock())
    got = run(snaps.get("m", 3, KEYS, FS))
    assert got.status == "load_error" and not got.active
    world.fail = None
    assert run(snaps.get("m", 3, KEYS, FS)).status == "ok"


def test_a_straddled_rebuild_is_read_again(world):
    fill(world, stamp="st1")
    calls = []
    real = load.aload

    async def flaky(cur, map_id, version, cls=load.POOLED):
        got = await real(cur, map_id, version, cls)
        calls.append(cls)
        if len(calls) == 2:                              # the pool of the first attempt is from the next build
            return stats([A, B], cls="*", version=3, stamp="st0")
        return got
    load.aload = flaky
    try:
        snaps = rs.RegionSnapshots(Conn(), Clock())
        assert run(snaps.get("m", 3, KEYS, FS)).status == "ok"
        assert len(calls) == 4
    finally:
        load.aload = real


def test_lru_is_bounded(world):
    for v in range(1, 6):
        fill(world, version=v)
    snaps = rs.RegionSnapshots(Conn(), Clock(), entries=3)
    for v in range(1, 6):
        run(snaps.get("m", v, KEYS, FS))
    assert snaps.stats()["entries"] == 3


# ---- the server's mode gate -------------------------------------------------------------------------------------
class FakeGraph:
    keys = KEYS

    def region_arg(self, region):
        return None


def service(snaps):
    from packages.services.mission_planner.server import MissionPlannerService
    svc = MissionPlannerService.__new__(MissionPlannerService)
    svc._region_snaps = snaps
    svc.logger = NS(warning=lambda *a, **k: None, info=lambda *a, **k: None)
    return svc


def test_mode_off_does_no_work(world, monkeypatch):
    fill(world)
    monkeypatch.setattr(config, "REGION_COST_MODE", "off")
    monkeypatch.setattr(config, "REGION_COST_MAPS", ("m",))
    conn = Conn()
    svc = service(rs.RegionSnapshots(conn, Clock()))
    robot = NS(status=NS(factsheet=FS))
    assert run(svc._region_for("m", 3, FakeGraph(), robot)) == (None, "off")
    assert world.reads == [] and conn.opened == 0


def test_a_map_outside_the_allowlist_does_no_work(world, monkeypatch):
    fill(world)
    monkeypatch.setattr(config, "REGION_COST_MODE", "on")
    monkeypatch.setattr(config, "REGION_COST_MAPS", ())
    svc = service(rs.RegionSnapshots(Conn(), Clock()))
    assert run(svc._region_for("m", 3, FakeGraph(), NS(status=NS(factsheet=FS)))) == (None, "off")
    monkeypatch.setattr(config, "REGION_COST_MAPS", ("other",))
    assert run(svc._region_for("m", 3, FakeGraph(), NS(status=NS(factsheet=FS)))) == (None, "off")
    assert world.reads == []


@pytest.mark.parametrize("mode", ["shadow", "on"])
def test_shadow_and_on_load_the_snapshot_for_listed_maps(world, monkeypatch, mode):
    fill(world)
    monkeypatch.setattr(config, "REGION_COST_MODE", mode)
    monkeypatch.setattr(config, "REGION_COST_MAPS", ("m",))
    svc = service(rs.RegionSnapshots(Conn(), Clock()))
    snap, got = run(svc._region_for("m", 3, FakeGraph(), NS(status=NS(factsheet=FS))))
    assert got == mode and snap.status == "ok" and snap.graph_version == 3


def test_a_graph_that_cannot_price_gets_no_snapshot(world, monkeypatch):
    fill(world)
    monkeypatch.setattr(config, "REGION_COST_MODE", "on")
    monkeypatch.setattr(config, "REGION_COST_MAPS", ("*",))
    svc = service(rs.RegionSnapshots(Conn(), Clock()))
    assert run(svc._region_for("m", 3, NS(keys=KEYS), NS(status=NS(factsheet=FS)))) == (None, "off")
    assert world.reads == []
