"""Scene graph + occupancy grid from depth only (packages/api/depth_rebuild.py) and its routes.

Sources are in-memory fakes with the production `Sources` interface; the depth -> occupancy rule
itself is tested in tests/unit/scene_graph/test_depth_occupancy.py.
"""
import asyncio
import hashlib
import io
import os
from types import SimpleNamespace
from unittest.mock import patch

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

import httpx  # noqa: E402
import numpy as np  # noqa: E402
import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from PIL import Image  # noqa: E402

from packages.api import depth_rebuild as dr  # noqa: E402

pytestmark = pytest.mark.unit

CAMERA = {"width": 448, "height": 336, "fx": 174.0, "fy": 174.0, "cx": 224.0, "cy": 168.0,
          "valid_range_m": [0.2, 10.0],
          "T_base_cam": {"x": 0.117, "y": 0.033, "z": 0.26, "qx": -0.4987, "qy": 0.5014, "qz": -0.5041, "qw": 0.4958}}
POSE3D = {"x": 1.0, "y": 2.0, "z": -0.25, "qx": 0.0, "qy": 0.0, "qz": 0.0, "qw": 1.0}


def depth_png(value_mm=1500):
    """A u16 PNG: everything at `value_mm` in front of the camera (a wall)."""
    arr = np.full((336, 448), value_mm, np.uint16)
    arr[200:, :] = 900                      # some floor-ish closer values in the lower half
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="PNG")
    return buf.getvalue()


def node(key, session="s1", depth=True, layers=(), pose3d=True, stored=None):
    doc = {"_key": key, "node_id": key, "pose": {"x": 1.0, "y": 2.0, "yaw": 0.0}, "session_id": session,
           "costmap_layers": list(layers), "costmap_stored_ms": stored}
    if depth:
        rec = {"camera": CAMERA, "depth_scale": 0.001, "session_id": session, "depth_stamp_ms": 1000,
               "rgb_stamp_ms": 990}
        if pose3d:
            rec["pose3d_map"] = POSE3D
        doc["depth"] = {"left": rec}
    return doc


class FakeSources:
    def __init__(self, nodes, sessions=(), map_row=("ALIVE", None)):
        self.nodes = {n["_key"]: n for n in nodes}
        self.depth = {(k, "left"): depth_png() for k in self.nodes}
        self.sess = list(sessions)
        self.row = map_row
        self.objects, self.calls = {}, []
        self.cursor = None
        self.clock_ms = 10_000

    def list_nodes(self, map_name):
        return [dict(n) for n in self.nodes.values()]

    def get_depth(self, map_name, node_id, camera):
        return self.depth.get((node_id, camera))

    def store_costmap(self, map_name, key, png):
        self.calls.append(("png", key))
        self.objects[key] = png
        return True

    def set_costmap(self, map_name, key, record):
        self.calls.append(("record", key))
        n = self.nodes[key]
        n["costmap_layers"] = sorted(set(n["costmap_layers"]) | {dr.LAYER})
        n.setdefault("records", {})[dr.LAYER] = record
        self.clock_ms += 1
        n["costmap_stored_ms"] = self.clock_ms
        return True

    def fold_cursor(self, map_name):
        return self.cursor

    async def map_row(self, map_name):
        return self.row if map_name == "lab" else None

    async def sessions(self, map_name, session_id):
        rows = [r for r in self.sess if session_id is None or r["session_id"] == session_id]
        return rows if session_id is not None else [r for r in rows if r.get("ended_at") is None]


def finished(sid="s1"):
    return {"session_id": sid, "purpose": "mapping", "paused_at": None, "ended_at": "2026-10-22T10:00:00"}


def make(src, worker=True, **kw):
    clock = {"t": 0.0}

    async def sleep(s):
        clock["t"] += s
        await asyncio.sleep(0)

    async def inline(fn, *a):           # deterministic: no real threads
        return fn(*a)

    return dr.DepthRebuild(src, worker, clock=lambda: clock["t"], sleep=sleep, to_thread=inline,
                           fold_wait_s=kw.get("fold_wait_s", 10.0), fold_poll_s=1.0)


async def wait(job_ctl, map_name="lab"):
    task = job_ctl.tasks[map_name]
    await asyncio.wait_for(task, 10)
    return job_ctl.status(map_name)["job"]


# --- pure rules ---

def test_classify():
    assert dr.classify(node("a")) is None
    assert dr.classify(node("a", layers=["occupancy"])) == dr.SKIP_HAS_COSTMAP
    assert dr.classify(node("a", layers=["occupancy", "occupancy_depth"])) == dr.SKIP_HAS_COSTMAP
    assert dr.classify(node("a", layers=["occupancy_depth"])) == dr.SKIP_HAS_DEPTH_LAYER
    assert dr.classify(node("a", layers=["person"])) is None          # a non-occupancy layer does not count
    assert dr.classify(node("a", depth=False)) == dr.SKIP_NO_DEPTH
    assert dr.classify(node("a", session="s2"), "s1") == dr.SKIP_OTHER_SESSION
    assert dr.classify({"_key": "a", "costmap": {"occupancy": {}}, "depth": {"x": {}}}) == dr.SKIP_HAS_COSTMAP


def test_node_session_falls_back_to_the_depth_record():
    n = node("a", session=None)
    n["depth"]["left"]["session_id"] = "s9"
    assert dr.node_session(n) == "s9"


def test_window_origin_is_centred_and_on_the_lattice():
    ox, oy = dr.window_origin(1.02, -2.51)
    assert ox == pytest.approx((20 - 100) * 0.05) and oy == pytest.approx((-51 - 100) * 0.05)


def test_build_layer_record_and_png():
    png, rec = dr.build_layer(node("a"), "left", depth_png(), "job1")
    win = np.array(Image.open(io.BytesIO(png)))
    assert win.shape == (200, 200) and win.dtype == np.uint8
    assert set(np.unique(win).tolist()) <= {0, 100, 255} and (win == 100).any() and (win == 0).any()
    assert rec["layer"] == "occupancy_depth" and rec["source"] == "depth" and rec["frame"] == "map"
    assert rec["width"] == rec["height"] == 200 and rec["resolution"] == 0.05
    assert rec["origin_map"] == rec["origin"] and rec["origin"]["yaw"] == 0.0
    assert rec["png_sha256"] == hashlib.sha256(png).hexdigest()
    assert rec["costmap_stamp_ms"] == 1000 and rec["keyframe_stamp_ms"] == 990 and rec["session_id"] == "s1"
    assert rec["generator"]["version"] == dr.VERSION and rec["job_id"] == "job1"


def test_build_layer_refuses_without_pose3d_or_with_a_bad_png():
    with pytest.raises(ValueError) as e:
        dr.build_layer(node("a", pose3d=False), "left", depth_png())
    assert e.value.args[0] == dr.FAIL_NO_POSE3D
    with pytest.raises(ValueError) as e:
        dr.build_layer(node("a"), "left", b"not a png")
    assert e.value.args[0] == dr.FAIL_BAD_PNG


def test_fold_target_and_cursor():
    nodes = [node("a", layers=["occupancy"], stored=5), node("b", layers=["occupancy_depth"], stored=9),
             node("c", stored=99), node("d", session="s2", layers=["occupancy"], stored=50)]
    assert dr.fold_target(nodes, None) == (50, "d")
    assert dr.fold_target(nodes, "s1") == (9, "b")
    assert dr.fold_target([node("c")], None) is None
    assert dr.cursor_of([9, "b"]) == (9, "b") and dr.cursor_of(None) is None and dr.cursor_of("x") is None


# --- the job ---

async def test_generates_only_nodes_without_costmap_png_before_record_then_waits_for_the_fold():
    src = FakeSources([node("a"), node("b", layers=["occupancy"], stored=5), node("c", depth=False),
                       node("d", layers=["occupancy_depth"], stored=6)], sessions=[finished()])
    ctl = make(src)
    status, view = await ctl.start("lab", requested_by="op")
    assert status == 202 and view["state"] in dr.ACTIVE and view["requested_by"] == "op"

    async def fold_later():
        while ctl.jobs["lab"].state != dr.FOLDING or ctl.jobs["lab"].fold.get("state") != "waiting":
            await asyncio.sleep(0)
        src.cursor = [src.nodes["a"]["costmap_stored_ms"], "a"]
    await asyncio.gather(fold_later(), wait(ctl))
    job = ctl.status("lab")["job"]
    assert job["state"] == dr.SUCCEEDED and job["progress"] == 1.0
    assert job["generated"] == 1 and job["to_generate"] == 1 and job["failed"] == 0
    assert job["skipped"] == {"has_costmap": 1, "no_depth": 1, "has_depth_layer": 1}
    assert src.calls == [("png", "a"), ("record", "a")]                 # PNG first, robot costmap untouched
    assert job["fold"]["state"] == "caught_up" and job["fold"]["target"] == [src.nodes["a"]["costmap_stored_ms"], "a"]
    rec = src.nodes["a"]["records"]["occupancy_depth"]
    assert rec["png_sha256"] == hashlib.sha256(src.objects["a"]).hexdigest()


async def test_second_run_writes_nothing():
    src = FakeSources([node("a")])
    src.cursor = [10 ** 12, "z"]
    ctl = make(src)
    await ctl.start("lab")
    assert (await wait(ctl))["generated"] == 1
    src.calls.clear()
    status, _ = await ctl.start("lab")
    assert status == 202
    job = await wait(ctl)
    assert job["state"] == dr.SUCCEEDED and job["generated"] == 0 and job["to_generate"] == 0
    assert job["skipped"] == {"has_depth_layer": 1} and src.calls == []


async def test_session_scope_only_touches_that_session():
    src = FakeSources([node("a", session="s1"), node("b", session="s2")], sessions=[finished("s1"), finished("s2")])
    src.cursor = [10 ** 12, "z"]
    ctl = make(src)
    await ctl.start("lab", session_id="s2")
    job = await wait(ctl)
    assert job["session_id"] == "s2" and job["generated"] == 1 and src.calls == [("png", "b"), ("record", "b")]


async def test_fold_timeout_is_a_warning_and_worker_off_skips_the_wait():
    src = FakeSources([node("a")])
    ctl = make(src, fold_wait_s=3.0)
    await ctl.start("lab")
    job = await wait(ctl)
    assert job["state"] == dr.SUCCEEDED and job["fold"]["state"] == "timeout" and job["warnings"]

    src2 = FakeSources([node("a")])
    ctl2 = make(src2, worker=False)
    _, view = await ctl2.start("lab")
    assert view["warnings"]
    job2 = await wait(ctl2)
    assert job2["state"] == dr.SUCCEEDED and job2["fold"]["state"] == "worker_off" and job2["generated"] == 1


async def test_map_switched_off_is_reported():
    src = FakeSources([node("a")], map_row=("ALIVE", False))
    ctl = make(src)
    await ctl.start("lab")
    job = await wait(ctl)
    assert job["fold"]["state"] == "map_disabled" and job["generated"] == 1


async def test_per_node_failures_are_counted_and_all_failing_fails_the_job():
    src = FakeSources([node("a", pose3d=False), node("b")])
    src.depth.pop(("b", "left"))
    ctl = make(src)
    await ctl.start("lab")
    job = await wait(ctl)
    assert job["state"] == dr.FAILED and job["error"]["reason"] == "nothing_written"
    assert job["failed_by_reason"] == {"no_pose3d": 1, "depth_png_missing": 1}
    assert {f["node"] for f in job["failures"]} == {"a", "b"} and src.calls == []

    src = FakeSources([node("a", pose3d=False), node("b")])
    src.cursor = [10 ** 12, "z"]
    ctl = make(src)
    await ctl.start("lab")
    job = await wait(ctl)
    assert job["state"] == dr.SUCCEEDED and job["generated"] == 1 and job["failed"] == 1


async def test_a_failed_write_is_counted():
    src = FakeSources([node("a")])
    src.store_costmap = lambda m, k, p: False
    ctl = make(src)
    await ctl.start("lab")
    job = await wait(ctl)
    assert job["state"] == dr.FAILED and job["failed_by_reason"] == {"store_failed": 1}


async def test_guards():
    ctl = make(FakeSources([node("a")]))
    with pytest.raises(HTTPException) as e:
        await ctl.start("nope")
    assert e.value.status_code == 404 and e.value.detail["code"] == "map_not_found"

    ctl = make(FakeSources([node("a")], map_row=("DELETING", None)))
    with pytest.raises(HTTPException) as e:
        await ctl.start("lab")
    assert e.value.status_code == 409 and e.value.detail["code"] == "map_deleting"

    live = {"session_id": "s1", "purpose": "mapping", "paused_at": None, "ended_at": None}
    ctl = make(FakeSources([node("a")], sessions=[live]))
    with pytest.raises(HTTPException) as e:
        await ctl.start("lab")
    assert e.value.status_code == 409 and e.value.detail["code"] == "session_active"
    assert e.value.detail["sessions"] == ["s1"]

    paused = dict(live, paused_at="2026-10-22T10:00:00")
    operating = dict(live, session_id="s2", purpose="operate")
    ctl = make(FakeSources([node("a")], sessions=[paused, operating]))
    assert (await ctl.start("lab"))[0] == 202            # a paused mapping or an operate session does not block
    await wait(ctl)

    ctl = make(FakeSources([node("a")], sessions=[finished("s1")]))
    with pytest.raises(HTTPException) as e:
        await ctl.start("lab", session_id="s9")
    assert e.value.status_code == 404 and e.value.detail["code"] == "session_not_found"

    ctl = make(FakeSources([node("a", depth=False)]))
    with pytest.raises(HTTPException) as e:
        await ctl.start("lab")
    assert e.value.status_code == 409 and e.value.detail["code"] == "no_depth"


async def test_same_scope_returns_the_running_job_other_scope_conflicts_and_cancel():
    src = FakeSources([node("a"), node("b")], sessions=[finished("s1")])
    ctl = make(src)
    status, first = await ctl.start("lab")
    status2, again = await ctl.start("lab")
    assert status == 202 and status2 == 200 and again["job_id"] == first["job_id"]
    with pytest.raises(HTTPException) as e:
        await ctl.start("lab", session_id="s1")
    assert e.value.status_code == 409 and e.value.detail["code"] == "job_active"
    assert ctl.cancel("lab")["cancel_requested"] is True
    job = await wait(ctl)
    assert job["state"] == dr.CANCELLED and job["generated"] == 0
    with pytest.raises(HTTPException) as e:
        ctl.cancel("lab")
    assert e.value.status_code == 404 and e.value.detail["code"] == "no_active_job"


async def test_status_without_a_job():
    assert make(FakeSources([])).status("lab") == {"map_name": "lab", "worker_enabled": True, "job": None}


# --- routes ---

async def test_routes():
    import packages.api.main as main
    src = FakeSources([node("a")])
    src.cursor = [10 ** 12, "z"]
    ctl = make(src)
    with patch.object(main, "service", SimpleNamespace()), patch.object(main, "_depth_rebuild_jobs", ctl):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://t") as client:
            base = "/api/v1/maps/lab/scene-graph/rebuild-from-depth"
            r = await client.post(base)
            assert r.status_code == 202 and r.json()["map_name"] == "lab"
            r2 = await client.post(base, json={})
            assert r2.status_code == 200 and r2.json()["job_id"] == r.json()["job_id"]
            await wait(ctl)
            r = await client.get(base)
            assert r.status_code == 200 and r.json()["job"]["state"] == "succeeded"
            r = await client.post(base + "/cancel")
            assert r.status_code == 404 and r.json()["detail"]["code"] == "no_active_job"
            r = await client.post("/api/v1/maps/nope/scene-graph/rebuild-from-depth", json={"session_id": "s1"})
            assert r.status_code == 404 and r.json()["detail"]["code"] == "map_not_found"
