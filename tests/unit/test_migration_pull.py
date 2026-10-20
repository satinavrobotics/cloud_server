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


def test_stop_cancels_a_fetch_and_deletes_the_partial_file(tmp_path):
    from packages.migration.jobs import TransferError
    import pytest
    bundle = build(tmp_path, FakeSource({"Hall": a_map()}), {"Hall": ["definition"]})

    async def go():
        mgr = make_manager(tmp_path, remote(bundle, export_state="running"))     # the remote export never finishes
        job = mgr.start_pull("http://robot", {"Hall": ["definition"]})
        await asyncio.sleep(0.1)
        assert mgr.imports[job["id"]]["state"] == "fetching"
        mgr.stop_import(job["id"])
        for _ in range(50):
            if mgr._tasks[job["id"]].done():
                break
            await asyncio.sleep(0.02)
        assert mgr.imports[job["id"]]["state"] == "stopped" and mgr.imports[job["id"]]["error"] is None
        with pytest.raises(TransferError, match="nothing is running"):
            mgr.stop_import(job["id"])
        with pytest.raises(TransferError, match="no bundle to apply"):
            mgr.start_apply(job["id"])                                                # a fetch that was stopped has to be started again
        return mgr
    mgr = asyncio.new_event_loop().run_until_complete(go())
    assert list((tmp_path / "spool").iterdir()) == []


def test_stop_of_a_running_copy_only_raises_the_flag():
    mgr = TransferManager(None, None, None, "/tmp/x-sati-spool-test", {})
    mgr.imports["j"] = {"id": "j", "state": "applying", "progress": {"phase": "objects", "done": 3}, "touched": 0, "path": "p"}
    out = mgr.stop_import("j")
    assert mgr.imports["j"]["stop"] is True and out["progress"]["stopping"] is True and out["state"] == "applying"


# ---- send: this server asks another one to pull from it -----------------------------------------------------------------

def _sender(tmp_path, handler, monkeypatch, mine="111", urls=("http://100.116.20.49", "http://192.168.1.60")):
    import packages.migration.jobs as jobs
    mgr = make_manager(tmp_path, handler)
    mgr._instance_id, mgr._instance_read = mine, True
    monkeypatch.setattr(jobs, "own_urls", lambda port, client_host=None: list(urls))
    monkeypatch.setattr(jobs, "is_own_address", lambda host: host in ("127.0.0.1", "localhost", "100.116.20.49"))
    return mgr


def test_send_asks_the_destination_to_pull_and_tells_it_every_address_and_who_to_expect(tmp_path, monkeypatch):
    seen = {}

    def handler(request):
        if request.method == "GET" and request.url.path == "/api/v1/transfer/maps":
            return httpx.Response(200, json={"maps": [], "server": {"instance_id": "999", "name": "cloud"}})
        if request.method == "POST" and request.url.path == "/api/v1/transfer/imports/pull":
            seen["body"] = __import__("json").loads(request.content)
            return httpx.Response(202, json={"id": "r1", "state": "fetching", "origin": "x"})
        return httpx.Response(404)
    mgr = _sender(tmp_path, handler, monkeypatch)
    job = asyncio.new_event_loop().run_until_complete(mgr.outbound_start("http://100.85.3.47/", {"office_pi": ["definition", "images"]}, "192.168.1.60"))
    assert seen["body"] == {"source_urls": ["http://100.116.20.49", "http://192.168.1.60"], "expect_instance_id": "111",
                            "maps": {"office_pi": ["definition", "images"]}}
    assert job["id"] == "r1" and job["destination_url"] == "http://100.85.3.47"


def test_send_refuses_this_server_an_old_server_and_nothing_picked(tmp_path, monkeypatch):
    import pytest
    from packages.migration.jobs import TransferError
    same = _sender(tmp_path, lambda r: httpx.Response(200, json={"maps": [], "server": {"instance_id": "111"}}), monkeypatch)
    old = _sender(tmp_path, lambda r: httpx.Response(404), monkeypatch)
    loop = asyncio.new_event_loop()
    with pytest.raises(TransferError, match="this server itself"):
        loop.run_until_complete(same.outbound_start("http://100.85.3.47", {"a": []}))
    with pytest.raises(TransferError, match="this server itself"):
        loop.run_until_complete(same.outbound_start("http://100.116.20.49", {"a": []}))        # its own address, caught before any call
    with pytest.raises(TransferError, match="does not support"):
        loop.run_until_complete(old.outbound_start("http://100.85.3.47", {"a": []}))
    with pytest.raises(TransferError, match="at least one map"):
        loop.run_until_complete(same.outbound_start("http://100.85.3.47", {}))
    with pytest.raises(TransferError, match="http"):
        loop.run_until_complete(same.outbound_start("ftp://x", {"a": []}))


def test_the_destination_picks_the_address_that_really_is_the_sender(tmp_path):
    import pytest
    from packages.migration.jobs import TransferError

    def handler(request):
        host = request.url.host
        if host == "10.0.0.9":                      # reachable, but another robot with the same kind of address
            return httpx.Response(200, json={"server": {"instance_id": "other"}})
        if host == "100.116.20.49":
            return httpx.Response(200, json={"server": {"instance_id": "111"}})
        raise httpx.ConnectTimeout("no route")
    mgr = make_manager(tmp_path, handler)

    async def go(bases):
        async with httpx.AsyncClient(transport=mgr.http_transport) as c:
            return await mgr._choose_source(c, bases, "111")
    loop = asyncio.new_event_loop()
    assert loop.run_until_complete(go(["http://192.168.9.9", "http://10.0.0.9", "http://100.116.20.49"])) == "http://100.116.20.49"
    with pytest.raises(TransferError, match="none of the source's addresses"):
        loop.run_until_complete(go(["http://192.168.9.9", "http://10.0.0.9"]))


def test_only_the_expected_import_calls_are_relayed(tmp_path, monkeypatch):
    import pytest
    from packages.migration.jobs import TransferError
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        return httpx.Response(200, json={"id": "r1", "state": "planned"})
    mgr = _sender(tmp_path, handler, monkeypatch)
    loop = asyncio.new_event_loop()
    out = loop.run_until_complete(mgr.outbound_call("http://100.85.3.47", "POST", "r1", "apply"))
    assert out["destination_url"] == "http://100.85.3.47" and calls == [("POST", "/api/v1/transfer/imports/r1/apply")]
    loop.run_until_complete(mgr.outbound_call("http://100.85.3.47", "GET", "r1"))
    loop.run_until_complete(mgr.outbound_call("http://100.85.3.47", "DELETE", "r1"))
    for method, action in [("POST", "exports"), ("GET", "apply"), ("DELETE", "stop"), ("POST", "../../maps")]:
        with pytest.raises(TransferError, match="cannot be relayed"):
            loop.run_until_complete(mgr.outbound_call("http://100.85.3.47", method, "r1", action))
