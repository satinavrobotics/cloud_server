"""Server-side audit fixes: orchestrator client path quoting, topomap switch map rule, SLAM start
errors, concurrent snapshot reads and their budget, stored-map read sharing."""
import asyncio
import os
from types import SimpleNamespace

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

import httpx  # noqa: E402
import pytest  # noqa: E402

from packages.api import mapping_switch as msw  # noqa: E402
from packages.api import orchestrator_client as oc  # noqa: E402
from packages.api.orchestrator_maps import OrchestratorMaps  # noqa: E402

pytestmark = pytest.mark.unit


def _robot(name="r1"):
    return SimpleNamespace(name=name, ip_address="10.0.0.5", entrypoint_port=8080,
                           status=SimpleNamespace(online=True))


# --- orchestrator_client: path segments are quoted ----------------------------------------------

async def test_path_segments_are_quoted():
    seen = []

    def handler(request):
        seen.append(request.url.raw_path.decode())
        return httpx.Response(200, json={})

    def factory(timeout):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    client = oc.OrchestratorClient(_robot(), http_factory=factory)
    await client.get_map("a/b c?x#y")
    await client.patch_map("m 1", {"init_pos": None})
    await client.status("odd/name")
    await client.start("s p")
    await client.stop("s%p")
    assert seen == ["/maps/a%2Fb%20c%3Fx%23y", "/maps/m%201", "/services/odd%2Fname/status",
                    "/services/s%20p/start", "/services/s%25p/stop"]


# --- mapping switch ------------------------------------------------------------------------------

class Stub:
    """A client whose methods are given as attributes."""
    def __init__(self, **fns):
        self.__dict__.update(fns)


def _switch(client, **kw):
    return msw.MappingSwitch(client_factory=lambda robot: client, **kw)


async def test_topomap_switch_sends_the_map_only_in_relocalization():
    puts = []

    async def put(mode, map_name=None, wait=False, topomap=None):
        puts.append((mode, map_name, topomap))
        return {"mode": mode}

    client = Stub(put_localization=put)
    await msw.MappingSwitch._switch_topomap(
        client, {"mode": "slam", "map": "stale", "topomap": False}, True)
    await msw.MappingSwitch._switch_topomap(
        client, {"mode": "relocalization", "map": "cloud-x", "topomap": False}, True)
    assert puts == [("slam", None, True), ("relocalization", "cloud-x", True)]


async def test_start_slam_reports_a_failed_map_lookup_but_404_means_no_map():
    puts = []

    async def put(mode, map_name=None, wait=False, topomap=None):
        puts.append(mode)
        return {"mode": mode, "applied": True}

    async def loc():
        return {"mode": "odometry", "map": None}

    def getter(error):
        async def get_map(name):
            raise error
        return get_map

    client = Stub(get_localization=loc, put_localization=put,
                  get_map=getter(oc.OrchestratorError(oc.HTTP, "no such map", status=404)))
    res = await _switch(client)._start_slam(client, "r1", "yard", "cloud-yard")
    assert res.status == msw.SLAM_STARTED and puts == ["slam"]

    puts.clear()
    client.get_map = getter(oc.OrchestratorError(oc.HTTP, "boom", status=500))
    res = await _switch(client)._start_slam(client, "r1", "yard", "cloud-yard")
    assert res.status == msw.SLAM_FAILED and "boom" in res.warning and puts == []


async def test_restore_after_a_save_turns_the_topomap_off_in_the_same_put():
    puts = []

    async def put(mode, map_name=None, wait=False, topomap=None):
        puts.append((mode, map_name, topomap))
        return {}

    async def loc():
        return {"mode": "slam", "map": None, "topomap": True}

    client = Stub(get_localization=loc, put_localization=put)
    switch = _switch(client)
    switch._prev_intent["r1"] = {"mode": "odometry", "map": None}
    assert await switch._restore_intent(client, "r1") is None
    assert puts == [("odometry", None, False)]


async def test_restore_without_the_mapping_api_sends_no_topomap():
    puts = []

    async def put(mode, map_name=None, wait=False, topomap=None):
        puts.append((mode, map_name, topomap))
        return {}

    async def loc():
        return {"mode": "slam", "map": None}

    client = Stub(get_localization=loc, put_localization=put)
    assert await _switch(client)._restore_intent(client, "r1") is None
    assert puts == [("odometry", None, None)]


async def test_snapshot_status_calls_run_concurrently():
    async def loc():
        return {"mode": "odometry", "map": None}   # no `topomap`: both services via /services

    async def list_services():
        return [{"name": "topomap"}, {"name": "grid"}]

    async def status(name):
        await asyncio.sleep(0.2)
        return {"state": {"running": True}}

    client = Stub(get_localization=loc, list_services=list_services, status=status)
    loop = asyncio.get_event_loop()
    t0 = loop.time()
    snap = await _switch(client).snapshot(_robot(), fresh=True)
    assert loop.time() - t0 < 0.35
    assert snap.reachable is True and snap.services["topo"]["running"] is True
    assert snap.services["grid"]["running"] is True


async def test_snapshot_has_an_overall_budget():
    async def loc():
        await asyncio.sleep(5)

    client = Stub(get_localization=loc)
    snap = await _switch(client, fetch_budget_s=0.05).snapshot(_robot(), fresh=True)
    assert snap.reachable is False and "in time" in snap.error


async def test_invalidate_drops_the_last_snapshot_for_the_ws_path():
    async def loc():
        return {"mode": "odometry", "map": None, "topomap": False}

    client = Stub(get_localization=loc)
    switch = _switch(client)
    snap = await switch.snapshot(_robot(), fresh=True)
    assert switch.cached("r1") is snap
    switch.invalidate("r1")
    assert switch.cached("r1") is None


async def test_unknown_services_are_not_switched_and_failures_are_named():
    switch = _switch(Stub())
    assert await switch.start(_robot(), ["slam"]) == []

    async def boom():
        raise RuntimeError("x")

    client = Stub(get_localization=boom)
    actions = await _switch(client).start(_robot(), ["topo", "grid"])
    assert [(a["service"], a["ok"]) for a in actions] == [("topomap", False), ("grid", False)]


# --- OrchestratorMaps.stored ---------------------------------------------------------------------

class SlowMaps:
    def __init__(self, rows):
        self.rows, self.calls, self.gate = rows, 0, asyncio.Event()

    def factory(self, robot):
        owner = self

        class C:
            async def list_maps(self, cloud_map_id):
                owner.calls += 1
                rows = list(owner.rows)
                await owner.gate.wait()
                return rows
        return C()


ROWS = [{"name": "cloud-a", "valid": True},
        {"name": "x", "valid": True, "meta": {"cloud_map_id": "b"}},
        {"name": "cloud-c", "valid": False}]


async def test_stored_shares_one_read_between_concurrent_callers():
    fake = SlowMaps(ROWS)
    holder = OrchestratorMaps(client_factory=fake.factory)
    tasks = [asyncio.ensure_future(holder.stored(_robot())) for _ in range(4)]
    await asyncio.sleep(0)
    fake.gate.set()
    results = await asyncio.gather(*tasks)
    assert fake.calls == 1
    assert [e["cloud_map_id"] for e in results[0]] == ["a", "b"]
    assert all(r == results[0] for r in results)
    await holder.stored(_robot())
    assert fake.calls == 1          # cached


async def test_stored_read_overtaken_by_an_invalidate_is_not_cached():
    fake = SlowMaps(ROWS)
    holder = OrchestratorMaps(client_factory=fake.factory)
    task = asyncio.ensure_future(holder.stored(_robot()))
    await asyncio.sleep(0)
    holder.invalidate("r1")         # the read started before the change
    fake.gate.set()
    await task
    assert "r1" not in holder._stored
    assert fake.calls == 1
    await holder.stored(_robot())   # asks again
    assert fake.calls == 2
