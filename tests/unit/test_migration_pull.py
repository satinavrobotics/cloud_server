"""Data upload: this server pulls a bundle from another one as a background job."""
import asyncio

import httpx

from packages.migration.jobs import TransferManager
from tests.unit.test_migration_bundle_export_plan import FakeSource, a_map, build


def make_manager(tmp_path, handler):
    mgr = TransferManager(None, None, None, str(tmp_path / "spool"), {"name": "cloud"})
    mgr.http_transport = httpx.MockTransport(handler)
    mgr.poll_s = 0.01

    async def target_maps():
        return {"Hall": {"lifecycle": "ALIVE", "imported_from": None}}
    mgr.target_maps = target_maps
    return mgr


def remote(bundle_path, export_state="done", calls=None):
    data = open(bundle_path, "rb").read()

    def handler(request):
        if calls is not None:
            calls.append((request.method, request.url.path))
        path = request.url.path
        if request.method == "POST" and path == "/api/v1/transfer/exports":
            return httpx.Response(202, json={"id": "e1", "state": "running"})
        if path == "/api/v1/transfer/exports/e1" and request.method == "GET":
            return httpx.Response(200, json={"id": "e1", "state": export_state, "size": len(data), "error": "boom"})
        if path.endswith("/bundle"):
            return httpx.Response(200, content=data)
        if request.method == "DELETE":
            return httpx.Response(200, json={"success": True})
        return httpx.Response(404)
    return handler


async def finish(mgr, job):
    for _ in range(100):
        if mgr.imports[job["id"]]["state"] not in ("fetching",):
            return mgr.imports[job["id"]]
        await asyncio.sleep(0.05)
    raise AssertionError("pull did not finish")


def test_pull_fetches_plans_and_cleans_up(tmp_path):
    bundle = build(tmp_path, FakeSource({"Hall": a_map()}), {"Hall": ["definition", "images"]})
    calls = []

    async def go():
        mgr = make_manager(tmp_path, remote(bundle, calls=calls))
        job = mgr.start_pull("http://robot/", {"Hall": ["definition", "images"]})
        assert job["state"] == "fetching"
        done = await finish(mgr, job)
        return mgr, done
    mgr, done = asyncio.new_event_loop().run_until_complete(go())
    assert done["state"] == "planned" and done["plan"]["ok"]
    assert done["plan"]["maps"][0]["action"] == "rename"            # "Hall" exists on this server
    assert ("DELETE", "/api/v1/transfer/exports/e1") in calls       # the source is told it can drop the export
    assert [f for f in (tmp_path / "spool").iterdir() if f.name.endswith(".part")] == []


def test_pull_reports_a_failed_remote_export(tmp_path):
    bundle = build(tmp_path, FakeSource({"Hall": a_map()}), {"Hall": []})

    async def go():
        mgr = make_manager(tmp_path, remote(bundle, export_state="failed"))
        return await finish(mgr, mgr.start_pull("http://robot", {"Hall": []}))
    done = asyncio.new_event_loop().run_until_complete(go())
    assert done["state"] == "failed" and "boom" in done["error"]
    assert list((tmp_path / "spool").iterdir()) == []


def test_pull_rejects_bad_input(tmp_path):
    import pytest
    from packages.migration.jobs import TransferError
    mgr = make_manager(tmp_path, lambda r: httpx.Response(404))

    async def go():
        with pytest.raises(TransferError):
            mgr.start_pull("ftp://x", {"a": []})
        with pytest.raises(TransferError):
            mgr.start_pull("http://x", {})
        with pytest.raises(TransferError, match="does not support"):
            await _remote_404(mgr)
    asyncio.new_event_loop().run_until_complete(go())


async def _remote_404(mgr):
    mgr.http_transport = httpx.MockTransport(lambda r: httpx.Response(404))
    return await mgr.remote_maps("http://x", False)


def test_remote_listing_says_when_the_source_is_this_very_server(tmp_path):
    mgr = make_manager(tmp_path, lambda r: httpx.Response(200, json={"maps": [], "parts": [], "server": {"instance_id": "999"}}))

    async def go(mine):
        mgr._instance_id, mgr._instance_read = mine, True
        return await mgr.remote_maps("http://robot", False)
    loop = asyncio.new_event_loop()
    assert loop.run_until_complete(go("999"))["is_self"] is True
    assert loop.run_until_complete(go("111"))["is_self"] is False
    assert loop.run_until_complete(go(None))["is_self"] is False         # without an own id nothing is claimed
