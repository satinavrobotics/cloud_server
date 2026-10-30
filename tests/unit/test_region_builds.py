"""packages/utils/region_builds.py: marking region builds stale (the shared contract of the stats work packages)."""
import os

os.environ.setdefault("POSTGRES_PASSWORD", "test")

import pytest  # noqa: E402

from packages.utils import region_builds as rb  # noqa: E402

pytestmark = pytest.mark.unit


class Cur:
    def __init__(self, by_run=(), by_robot=()):
        self.by_run, self.by_robot, self.log, self.rows = by_run, by_robot, [], []

    async def execute(self, sql, params=None):
        self.log.append((sql, params))
        self.rows = [(m,) for m in (self.by_run if sql == rb.MAPS_OF_RUNS_SQL else self.by_robot)]

    async def fetchall(self):
        return self.rows


async def test_maps_of_runs_robot_and_explicit_are_merged_sorted_and_bumped_once():
    cur = Cur(by_run=["m2", "m1"], by_robot=["m1", "m3"])
    out = await rb.mark_stale(cur, run_ids=["r1"], robot_name="bot", map_ids=["m0", None])
    assert out == ["m0", "m1", "m2", "m3"]
    assert [s for s, _ in cur.log[-2:]] == [rb.BUMP_SQL, rb.STALE_SQL]
    assert cur.log[-2][1] == (out,) and cur.log[-1][1] == (out,)


async def test_nothing_affected_writes_nothing():
    cur = Cur()
    assert await rb.mark_stale(cur, run_ids=["r1"]) == []
    assert [s for s, _ in cur.log] == [rb.MAPS_OF_RUNS_SQL]
    assert await rb.mark_stale(Cur()) == []


def test_discard_caches_drops_the_maps_and_never_raises(monkeypatch):
    import importlib
    from packages.api.result_cache import ResultCache
    cache = ResultCache(4, 60.0)
    cache._done[("m1", 1)] = (cache.clock(), "x")
    cache._done[("m2", 1)] = (cache.clock(), "y")

    class Mod:
        CACHE = cache
    real = importlib.import_module

    def fake(name):
        if name == "packages.api.drive_stats":
            return Mod
        raise ImportError(name)
    monkeypatch.setattr(rb.importlib, "import_module", fake)
    rb.discard_caches(["m1"])
    assert list(cache._done) == [("m2", 1)]
    assert real is not None
