"""Incremental transfer: the destination says what it holds, the source leaves those bytes out of the bundle."""
import asyncio
import json

import httpx

from packages.migration import bundle as B
from packages.migration import jobs
from packages.migration.apply import apply_import
from packages.migration.jobs import TransferManager
from packages.migration.plan import plan_import
from tests.unit.test_migration_apply import FakeStore, run
from tests.unit.test_migration_bundle_export_plan import Target, build_sel, with_runs_and_bags

SEL = {"maps": {"Hall": ["definition", "images"]}, "rosbags": [{"robot": "peacock", "bag_id": "bag-1"}]}
HAVE = {"maps": {"Hall": {"n1/images/a": 8, "n1/depth/left.png": 7}}, "rosbags": {"peacock/bag-1": 10}}


def test_what_the_destination_holds_is_named_but_not_sent(tmp_path):
    full = B.BundleReader(build_sel(tmp_path, with_runs_and_bags(), SEL))
    (tmp_path / "d").mkdir()
    delta = B.BundleReader(build_sel(tmp_path / "d", with_runs_and_bags(), SEL, HAVE))
    assert full.has("maps/Hall/objects/n1/images/a") and not delta.has("maps/Hall/objects/n1/images/a")
    assert not delta.has("rosbags/peacock/bag-1/data") and delta.has("rosbags/peacock/bag-1/meta.json")
    listed = {o["key"]: o for o in delta.iter_ndjson("maps/Hall/objects.ndjson")}
    assert listed["n1/images/a"]["present"] and "sha256" not in listed["n1/images/a"]
    assert delta.verify() == []
    plan = plan_import(delta, Target())
    assert plan["ok"] and plan["rosbags"][0]["action"] == "have"
    assert plan["totals"]["object_bytes"] == 0 and plan["totals"]["rosbag_bytes"] == 0
    assert plan["totals"]["present_bytes"] == 8 + 7 + 10
    assert plan_import(full, Target())["totals"]["object_bytes"] == 15 + 10     # the recording counts too


def test_a_different_size_is_sent_again(tmp_path):
    have = {"maps": {"Hall": {"n1/images/a": 3}}, "rosbags": {"peacock/bag-1": 3}}
    r = B.BundleReader(build_sel(tmp_path, with_runs_and_bags(), SEL, have))
    assert r.has("maps/Hall/objects/n1/images/a") and r.has("rosbags/peacock/bag-1/data")


def test_apply_skips_what_is_there_and_refuses_what_has_since_gone(tmp_path):
    reader = B.BundleReader(build_sel(tmp_path, with_runs_and_bags(), SEL, HAVE))
    plan = plan_import(reader, Target())
    store = FakeStore()
    store.objects = {"Hall": {"n1/images/a": b"jpegdata", "n1/depth/left.png": b"pngdata"}}
    store.bags = {("peacock", "bag-1"): (b"ROSBAGDATA", {})}
    report = run(apply_import(reader, plan, store, "i"))
    assert report["ok"] and report["maps"][0]["objects_skipped"] == 2 and report["rosbags"][0]["skipped"]
    gone = FakeStore()
    again = run(apply_import(reader, plan, gone, "i2"))
    assert not again["ok"]
    assert "gone now" in again["maps"][0]["error"] and "gone now" in again["rosbags"][0]["error"]
    assert not gone.bags and "n1/images/a" not in gone.objects.get("Hall", {})


class FakeTarget:
    """What jobs.ServiceTarget reports about this server's storage."""
    def __init__(self, *_):
        pass

    async def object_sizes(self, name):
        return {"n1/images/a": 8} if name == "Hall-Pi" else {}

    async def rosbag_size(self, robot, bag_id):
        return 10 if (robot, bag_id) == ("peacock", "bag-1") else None


def pull_manager(tmp_path, monkeypatch, existing, bundle, seen):
    monkeypatch.setattr(jobs, "ServiceTarget", FakeTarget)
    data = open(bundle, "rb").read()

    def handler(request):
        path = request.url.path
        if request.method == "GET" and path == "/api/v1/transfer/maps":
            return httpx.Response(200, json={"server": {"instance_id": "src-1", "name": "Pi"}})
        if request.method == "POST" and path == "/api/v1/transfer/exports":
            seen.append(json.loads(request.content))
            return httpx.Response(202, json={"id": "e1", "state": "running"})
        if path == "/api/v1/transfer/exports/e1":
            return httpx.Response(200, json={"id": "e1", "state": "done", "size": len(data)})
        if path.endswith("/bundle"):
            return httpx.Response(200, content=data)
        return httpx.Response(200, json={"success": True})
    mgr = TransferManager(None, None, None, str(tmp_path / "spool"), {"name": "cloud"})
    mgr.http_transport = httpx.MockTransport(handler)
    mgr.poll_s = 0.01

    async def target_maps():
        return existing
    mgr.target_maps = target_maps
    return mgr


async def pulled(mgr, selection, **kw):
    job = mgr.start_pull("http://pi", selection, **kw)
    for _ in range(100):
        if mgr.imports[job["id"]]["state"] != "fetching":
            return mgr.imports[job["id"]]
        await asyncio.sleep(0.05)
    raise AssertionError("pull did not finish")


def test_a_pull_tells_the_source_what_it_already_holds(tmp_path, monkeypatch):
    bundle = build_sel(tmp_path, with_runs_and_bags(), SEL, HAVE)
    seen = []
    existing = {"Hall-Pi": {"lifecycle": "ALIVE", "imported_from": {"source": "src-1", "map": "Hall"}}}
    mgr = pull_manager(tmp_path, monkeypatch, existing, bundle, seen)

    done = asyncio.new_event_loop().run_until_complete(pulled(mgr, SEL))
    assert done["state"] == "planned" and done["delta"] is True
    assert seen[0]["have"] == {"maps": {"Hall": {"n1/images/a": 8}}, "rosbags": {"peacock/bag-1": 10}}
    assert seen[0]["maps"] == SEL["maps"]


def test_a_map_from_another_source_or_a_full_pull_sends_no_list(tmp_path, monkeypatch):
    bundle = build_sel(tmp_path, with_runs_and_bags(), {"maps": SEL["maps"]})
    seen = []
    other = {"Hall": {"lifecycle": "ALIVE", "imported_from": {"source": "someone-else", "map": "Hall"}}}
    loop = asyncio.new_event_loop()
    mgr = pull_manager(tmp_path, monkeypatch, other, bundle, seen)
    loop.run_until_complete(pulled(mgr, {"maps": SEL["maps"]}))
    assert "have" not in seen[0]
    existing = {"Hall-Pi": {"lifecycle": "ALIVE", "imported_from": {"source": "src-1", "map": "Hall"}}}
    mgr = pull_manager(tmp_path, monkeypatch, existing, bundle, seen)
    loop.run_until_complete(pulled(mgr, {"maps": SEL["maps"]}, delta=False))
    assert "have" not in seen[1]
