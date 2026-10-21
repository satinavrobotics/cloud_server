"""Mesh gateway (packages/api/mesh.py), its routes, hooks and migration.

Reuses the fakes of the reconstruction gateway tests (in-memory repo, MinIO, presigner, service).
"""
import hashlib
import importlib.util
import json
import os
import pathlib
import struct
from unittest.mock import MagicMock, patch

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from packages.api import mesh as ms, reconstruction as rc  # noqa: E402
from packages.events.codes import EventCode  # noqa: E402
from packages.events.emit import build_row  # noqa: E402
from tests.unit.test_map_reconstruction_gateway import (  # noqa: E402
    SECRET, T0, FakeClient, FakeObjects, FakePresigner, FakeRepo, World as RcWorld, codes, node)

pytestmark = pytest.mark.unit

CFG = ms.MeshConfig(service_url="http://mesh:8011", service_key="k", callback_secret=SECRET,
                    callback_base_url="http://sati-cloud:8000", minio_endpoint="sati-cloud:9000")


def glb(size=40, version=2, length=None, magic=b"glTF"):
    """A glTF binary header + padding: `size` bytes whose header says `length` (default: size)."""
    return struct.pack("<4sII", magic, version, size if length is None else length) \
        + b"\0" * (size - 12)


def snode(key, x, session, created="2026-10-20T08:00:00", **kw):
    """A node with depth whose document carries `session_id`."""
    doc = node(key, x, 0.0, 0.0, created=created, **kw)
    doc["session_id"] = session
    return doc


class FakeMeshObjects(FakeObjects):
    def __init__(self):
        super().__init__()
        self.buckets = {"map-lab", "mesh-staging"}

    def result_jobs(self, prefix=rc.RESULT_PREFIX):
        return sorted({(b, k.split("/")[1]) for b, k in self.objects if k.startswith(prefix)})


class World(RcWorld):
    def __init__(self, nodes=None, config=CFG):
        super().__init__(nodes, config)
        self.objects = FakeMeshObjects()
        self.gw = ms.MeshGateway(self.repo, self._depth_nodes, self.objects,
                                 FakePresigner(), self.client, config, now=self.clock,
                                 monotonic=lambda: self.mono[0])

    def stage(self, job_id, attempt, model=None, meta=b'{"session": "s1"}'):
        data = {"mesh": model if model is not None else glb(), "meta": meta}
        outputs = {}
        for name, (file, _ct) in ms.FILES.items():
            self.objects.put("mesh-staging", rc.staging_key(job_id, attempt, file), data[name])
            outputs[name] = {"bytes": len(data[name]),
                             "sha256": hashlib.sha256(data[name]).hexdigest()}
        return outputs

    async def succeed(self):
        job = await self.running_job()
        outputs = self.stage(job.job_id, job.attempts)
        status, _ = await self.gw.on_finish(job.job_id, {
            "attempt": job.attempts, "outputs": outputs,
            "result": {"session": "s1", "triangles": 298000, "frames_used": 2,
                       "frames_total": 2, "poses_source": "colmap", "duration_s": 12.5}})
        assert status == 200
        await self.gw.settle()
        return self.repo.jobs[job.job_id]


class TestHelpers:
    def test_params_defaults_and_overrides(self):
        p = ms.job_params(CFG, None)
        assert p == {"session": "auto", "quality": "standard", "max_frames": 400,
                     "triangles": 300000, "atlas": 4096, "texture_opt": True}
        p = ms.job_params(CFG, {"session": "abc", "quality": "fast", "triangles": 50000,
                                "atlas": 2048, "splat_steps": 900, "texture_opt": False})
        assert (p["session"], p["quality"], p["triangles"], p["atlas"]) == ("abc", "fast", 50000, 2048)
        assert p["splat_steps"] == 900 and p["texture_opt"] is False

    def test_max_frames_is_capped_by_the_config(self):
        assert ms.job_params(CFG, {"max_frames": 1500})["max_frames"] == 400
        assert ms.job_params(CFG, {"max_frames": 120})["max_frames"] == 120

    @pytest.mark.parametrize("body", [
        {"quality": "ultra"}, {"atlas": 3000}, {"triangles": 5}, {"triangles": 2_000_000},
        {"max_frames": 1}, {"max_frames": 5000}, {"splat_steps": 10}, {"session": " "},
        {"session": "x" * 200}, {"texture_opt": "maybe"}, {"bogus": 1}, [1]])
    def test_params_rejected(self, body):
        with pytest.raises(HTTPException) as exc:
            ms.job_params(CFG, body)
        assert exc.value.status_code == 422

    def test_session_of_a_node(self):
        frames = rc.frames_from_nodes([
            snode("a", 0, "s-doc"),
            node("b", 1, 0, 0),                      # no document session: the depth record's
            {**node("c", 2, 0, 0), "depth": {"left": {"camera": {"fx": 1}}}}])
        assert [f.session_id for f in frames] == ["s-doc", "s1", None]
        assert [ms.node_session(f) for f in frames] == ["s-doc", "s1", "unknown"]
        assert ms.session_counts(frames) == {"s-doc": 1, "s1": 1, "unknown": 1}

    def test_select_frames_thins_each_session_on_its_own(self):
        nodes = ([snode(f"a{i}", i, "big", created=f"2026-10-20T08:00:{i:02d}") for i in range(10)]
                 + [snode(f"b{i}", i, "small", created=f"2026-10-20T09:00:{i:02d}")
                    for i in range(3)])
        frames = rc.frames_from_nodes(nodes)
        used, info = ms.select_frames(frames, {"session": "auto"}, 4)
        assert info == {"big": {"nodes": 10, "used": 4, "every": 3},
                        "small": {"nodes": 3, "used": 3, "every": 1}}
        assert [f.node_id for f in used] == ["a0", "a3", "a6", "a9", "b0", "b1", "b2"]
        used, info = ms.select_frames(frames, {"session": "small"}, 400)
        assert [f.node_id for f in used] == ["b0", "b1", "b2"] and list(info) == ["small"]

    def test_manifest_carries_session_id_per_node(self):
        frames = rc.frames_from_nodes([snode("n1", 0, "sess-1"), node("n2", 1, 0, 0),
                                       {**node("n3", 2, 0, 0), "depth": {"left": {
                                           "camera": {"fx": 1}}}}])
        m = ms.build_manifest(
            job_id="j1", attempt=2, map_name="lab", map_type=None, crs=None,
            params={"session": "auto", "quality": "fast", "max_frames": 100, "triangles": 300000,
                    "atlas": 4096, "texture_opt": True, "junk": 1},
            frames=frames, map_bucket="map-lab", staging_bucket="mesh-staging",
            presign_get=lambda b, k: f"get:{b}/{k}", presign_put=lambda b, k: f"put:{b}/{k}",
            callback_base_url="http://x:8000/", token="t", now=T0, expiry_s=60)
        assert [n["session_id"] for n in m["nodes"]] == ["sess-1", "s1", "unknown"]
        assert m["params"] == {"session": "auto", "quality": "fast", "max_frames": 100,
                               "triangles": 300000, "atlas": 4096, "texture_opt": True}
        assert set(m["outputs"]) == {"mesh", "meta"}
        assert m["outputs"]["mesh"]["url"] == "put:mesh-staging/j1/2/model.glb"
        assert m["outputs"]["mesh"]["content_type"] == "model/gltf-binary"
        assert m["outputs"]["meta"]["content_type"] == "application/json"
        assert m["callback"]["base_url"] == "http://x:8000/internal/mesh/jobs/j1"
        cam = m["nodes"][0]["cameras"][0]
        assert cam["rgb_url"].startswith("get:map-lab/n1/images/")
        assert "robot_pose" not in json.dumps(m)

    def test_token_is_shared_with_reconstruction(self):
        assert ms.callback_token(SECRET, "j") == rc.callback_token(SECRET, "j")

    def test_config(self):
        assert CFG.staging_bucket == "mesh-staging" and CFG.configured and CFG.max_frames == 400
        assert not ms.MeshConfig().configured

    def test_config_from_env(self):
        from packages import config as c
        with patch.multiple(c, MESH_SERVICE_URL="http://m", MESH_SERVICE_KEY="k",
                            MESH_CALLBACK_SECRET="s", MESH_MAX_FRAMES=0):
            cfg = ms.MeshConfig.from_env()
        assert cfg.configured and cfg.service_url == "http://m" and cfg.max_frames == 1
        assert cfg.staging_bucket == c.MESH_STAGING_BUCKET == "mesh-staging"
        assert c.MESH_JOB_TIMEOUT_S == 3600 and c.MESH_MAX_INFLIGHT == 1

    @pytest.mark.parametrize("head,size,why", [
        (glb(40), 40, None),
        (glb(40), 41, "its header says 40"),
        (glb(40, length=99), 40, "its header says 99"),
        (glb(40, version=1), 40, "version 1"),
        (glb(40, magic=b"PNG\x00"), 40, "bad magic"),
        (b"glTF\x02", 5, "shorter"),
        (b"", 0, "shorter")])
    def test_glb_problem(self, head, size, why):
        got = ms.glb_problem(head[:11] if why == "shorter" else head[:12], size)
        assert (got is None) if why is None else (why in got)


class TestWiring:
    def test_node_query_projects_the_session(self):
        from packages.topomap_dbs.graph_db.server import GraphDatabaseService as G
        assert "session_id: d.session_id" in G.DEPTH_NODES_AQL

    def test_diagnostics_know_the_container(self):
        from packages.services.server_diagnostics.checks import CAPABILITY_SERVICES
        assert CAPABILITY_SERVICES["mesh"] == (["mesh-service"], True)


class TestRoutesLogic:
    async def test_start_queues_and_refuses_a_second_job(self):
        w = World()
        job = await w.gw.start("lab", {"quality": "fast"}, "me")
        assert job["state"] == "queued" and job["params"]["quality"] == "fast"
        assert job["params"]["session"] == "auto"
        with pytest.raises(HTTPException) as exc:
            await w.gw.start("lab", None)
        assert exc.value.status_code == 409 and exc.value.detail["code"] == "job_active"

    async def test_start_errors(self):
        w = World(nodes=[node("n1", 0, 0, 0, depth=False)])
        with pytest.raises(HTTPException) as exc:
            await w.gw.start("lab", None)
        assert exc.value.detail["code"] == "no_depth"
        with pytest.raises(HTTPException) as exc:
            await w.gw.start("nope", None)
        assert exc.value.status_code == 404
        with pytest.raises(HTTPException) as exc:
            await World().gw.start("lab", {"quality": "x"})
        assert exc.value.status_code == 422

    async def test_unknown_session_is_refused_with_the_known_ones(self):
        w = World(nodes=[snode("a", 0, "s-a"), snode("b", 1, "s-b")])
        with pytest.raises(HTTPException) as exc:
            await w.gw.start("lab", {"session": "nope"})
        assert exc.value.status_code == 422 and exc.value.detail["code"] == "unknown_session"
        assert exc.value.detail["sessions"] == {"s-a": 1, "s-b": 1}
        assert (await w.gw.start("lab", {"session": "s-b"}))["state"] == "queued"

    async def test_not_configured_is_503(self):
        w = World(config=ms.MeshConfig())
        w.gw.client = None
        for call in (w.gw.start("lab", None),):
            with pytest.raises(HTTPException) as exc:
                await call
            assert exc.value.status_code == 503 and exc.value.detail["code"] == "not_configured"
            assert "MESH_SERVICE_URL" in exc.value.detail["message"]
        st = await w.gw.status("lab")          # reading still works
        assert st["configured"] is False and st["mesh"] is None and st["job"] is None
        w.gw.start_dispatcher()
        assert w.gw._task is None              # and no dispatcher runs

    async def test_independent_of_the_other_layers(self):
        w = World()
        job = await w.running_job()
        assert job.state == rc.RUNNING and len(w.client.submitted) == 1

    async def test_status_stale_and_cancel(self):
        w = World()
        done = await w.succeed()
        st = await w.gw.status("lab")
        view = st["mesh"]
        assert view["job_id"] == done.job_id and view["stale"] is False
        assert (view["session"], view["triangles"], view["poses_source"]) == ("s1", 298000,
                                                                              "colmap")
        assert set(view["files"]) == {"mesh", "meta"}
        assert view["files"]["mesh"]["url"].endswith(
            f"/mesh/files/model.glb?v={done.job_id}")
        assert view["files"]["mesh"]["bytes"] == 40
        w.nodes["lab"].append(node("n9", 5, 5, 0, created="2026-10-20T08:05:00"))
        w.mono[0] += 100
        st = await w.gw.status("lab")
        assert st["mesh"]["stale"] is True and st["mesh"]["stale_reason"]["new_nodes"] == 1
        with pytest.raises(HTTPException) as exc:
            await w.gw.cancel("lab")
        assert exc.value.detail["code"] == "no_active_job"
        await w.gw.start("lab", None)
        assert (await w.gw.cancel("lab"))["state"] == "cancelled"

    async def test_status_shows_a_failed_job_after_the_result(self):
        w = World()
        await w.succeed()
        job = await w.running_job()
        await w.gw.on_fail(job.job_id, {"attempt": 1, "reason": "no_usable_session",
                                        "message": "x", "stage": "poses"})
        st = await w.gw.status("lab")
        assert st["mesh"] is not None and st["job"]["state"] == "failed"
        assert st["job"]["error"]["reason"] == "no_usable_session"

    async def test_open_file_and_delete(self):
        w = World()
        with pytest.raises(HTTPException) as exc:
            await w.gw.open_file("lab", "model.glb")
        assert exc.value.detail["code"] == "no_mesh"
        done = await w.succeed()
        info = await w.gw.open_file("lab", "model.glb")
        assert info["key"] == f"mesh/{done.job_id}/model.glb" and info["bucket"] == "map-lab"
        assert info["content_type"] == "model/gltf-binary" and info["bytes"] == 40
        with pytest.raises(HTTPException) as exc:
            await w.gw.open_file("lab", "cloud.ply")
        assert exc.value.detail["code"] == "file_not_found"
        await w.gw.delete("lab")
        assert w.repo.jobs[done.job_id].state == rc.SUPERSEDED
        assert not any(k[1].startswith("mesh/") for k in w.objects.objects)


class TestManifestDispatch:
    async def test_session_id_and_per_session_thinning_reach_the_service(self):
        nodes = ([snode(f"a{i}", i, "big", created=f"2026-10-20T08:00:{i:02d}")
                  for i in range(10)]
                 + [snode(f"b{i}", i, "small", created=f"2026-10-20T09:00:{i:02d}")
                    for i in range(3)])
        w = World(nodes=nodes, config=ms.MeshConfig(**{**CFG.__dict__, "max_frames": 4}))
        await w.gw.start("lab", None)
        await w.gw.tick()
        m = w.client.submitted[0]
        assert [n["session_id"] for n in m["nodes"]] == ["big"] * 4 + ["small"] * 3
        assert m["params"]["max_frames"] == 4
        job = next(iter(w.repo.jobs.values()))
        assert job.inputs["nodes_used"] == 7 and job.inputs["nodes_with_depth"] == 13
        assert job.inputs["sessions"]["big"] == {"nodes": 10, "used": 4, "every": 3}
        assert job.frames_total == 7

    async def test_explicit_session_sends_only_its_nodes(self):
        w = World(nodes=[snode("a", 0, "s-a"), snode("b", 1, "s-b"), snode("c", 2, "s-b")])
        await w.gw.start("lab", {"session": "s-b", "quality": "fast"})
        await w.gw.tick()
        m = w.client.submitted[0]
        assert [n["node_id"] for n in m["nodes"]] == ["b", "c"]
        assert m["params"]["session"] == "s-b" and m["params"]["quality"] == "fast"

    async def test_session_that_vanished_fails_the_queued_job(self):
        w = World(nodes=[snode("a", 0, "s-a"), snode("b", 1, "s-b")])
        job = await w.gw.start("lab", {"session": "s-b"})
        w.nodes["lab"] = [snode("a", 0, "s-a")]
        await w.gw.tick()
        j = w.repo.jobs[job["job_id"]]
        assert j.state == rc.FAILED and j.error["reason"] == "unknown_session"

    async def test_rejected_by_the_service(self):
        w = World()
        w.client.submit_answer = (422, {"detail": {"code": "invalid_manifest"}})
        job = await w.gw.start("lab", None)
        await w.gw.tick()
        j = w.repo.jobs[job["job_id"]]
        assert j.state == rc.FAILED and j.error["reason"] == "rejected"
        assert EventCode.MAP_MESH_FAILED in codes(w.repo)


class TestCallbacks:
    def test_authorize(self):
        w = World()
        w.gw.authorize("j1", f"Bearer {ms.callback_token(SECRET, 'j1')}")
        for bad in (None, "Bearer nope", "Basic x"):
            with pytest.raises(HTTPException) as exc:
                w.gw.authorize("j1", bad)
            assert exc.value.status_code == 401
        w.gw.config = ms.MeshConfig()
        with pytest.raises(HTTPException) as exc:
            w.gw.authorize("j1", None)
        assert exc.value.status_code == 503

    async def test_progress_starts_with_a_mesh_event(self):
        w = World()
        job = await w.running_job()
        status, answer = await w.gw.on_progress(job.job_id, {
            "attempt": 1, "stage": "splat", "progress": 0.5, "frames_done": 1})
        assert (status, answer) == (200, {"action": "continue"})
        j = w.repo.jobs[job.job_id]
        assert j.stage == "splat" and j.started_at == T0 and j.progress == 0.5
        assert codes(w.repo) == [EventCode.MAP_MESH_STARTED]
        assert (await w.gw.on_progress(job.job_id, {"attempt": 9}))[0] == 410

    async def test_finish_copies_and_supersedes(self):
        w = World()
        first = await w.succeed()
        assert first.state == rc.SUCCEEDED
        for name, (file, _) in ms.FILES.items():
            assert ("map-lab", f"mesh/{first.job_id}/{file}") in w.objects.objects
        assert not [k for k in w.objects.objects if k[0] == "mesh-staging"]   # staging cleaned
        assert codes(w.repo)[-1] == EventCode.MAP_MESH_FINISHED
        second = await w.succeed()
        assert w.repo.jobs[first.job_id].state == rc.SUPERSEDED
        assert not any(k[1].startswith(f"mesh/{first.job_id}") for k in w.objects.objects)
        assert second.state == rc.SUCCEEDED

    @pytest.mark.parametrize("kwargs,message", [
        ({"model": glb(40)[:-1]}, "model.glb: 39 bytes, its header says 40"),
        ({"model": glb(40, length=44)}, "its header says 44"),
        ({"model": glb(40, version=1)}, "glTF version 1"),
        ({"model": glb(40, magic=b"PLY\n")}, "bad magic"),
        ({"model": b"glTF"}, "shorter"),
        ({"meta": b"{not json"}, "meta.json does not parse"),
        ({"meta": b"[1]"}, "not an object"),
    ])
    async def test_bad_output_keeps_the_previous_result(self, kwargs, message):
        w = World()
        old = await w.succeed()
        job = await w.running_job()
        outputs = w.stage(job.job_id, job.attempts, **kwargs)
        status, _ = await w.gw.on_finish(job.job_id, {"attempt": job.attempts,
                                                      "outputs": outputs, "result": {}})
        await w.gw.settle()
        assert status == 200
        j = w.repo.jobs[job.job_id]
        assert j.state == rc.FAILED and j.error["reason"] == "bad_output"
        assert message in j.error["message"]
        assert w.repo.jobs[old.job_id].state == rc.SUCCEEDED

    async def test_missing_or_wrong_sized_output(self):
        w = World()
        job = await w.running_job()
        outputs = w.stage(job.job_id, job.attempts)
        outputs["meta"]["bytes"] += 1
        await w.gw.on_finish(job.job_id, {"attempt": 1, "outputs": outputs, "result": {}})
        assert "reported" in w.repo.jobs[job.job_id].error["message"]
        w2 = World()
        job = await w2.running_job()
        await w2.gw.on_finish(job.job_id, {"attempt": 1, "outputs": {"meta": {"bytes": 1}}})
        assert "not reported" in w2.repo.jobs[job.job_id].error["message"]
        w3 = World()
        job = await w3.running_job()
        outputs = w3.stage(job.job_id, 1)
        del w3.objects.objects[("mesh-staging", rc.staging_key(job.job_id, 1, "model.glb"))]
        await w3.gw.on_finish(job.job_id, {"attempt": 1, "outputs": outputs, "result": {}})
        assert "model.glb missing in staging" in w3.repo.jobs[job.job_id].error["message"]

    async def test_finish_refused_for_a_deleting_map(self):
        w = World()
        job = await w.running_job()
        outputs = w.stage(job.job_id, 1)
        w.repo.maps["lab"] = ("DELETING", {})
        status, answer = await w.gw.on_finish(job.job_id, {"attempt": 1, "outputs": outputs})
        assert (status, answer) == (410, {"action": "stop"})
        assert w.repo.jobs[job.job_id].state == rc.CANCELLED

    async def test_fail_and_url_expired_retry_once(self):
        w = World()
        job = await w.running_job()
        await w.gw.on_fail(job.job_id, {"attempt": 1, "reason": "url_expired", "message": "x"})
        j = w.repo.jobs[job.job_id]
        assert j.state == rc.QUEUED
        await w.gw.tick()
        assert j.attempts == 2 and len(w.client.submitted) == 2
        await w.gw.on_fail(job.job_id, {"attempt": 2, "reason": "url_expired"})
        assert j.state == rc.FAILED
        assert codes(w.repo)[-1] == EventCode.MAP_MESH_FAILED

    async def test_cancel_while_running_then_finish_is_cancelled(self):
        w = World()
        job = await w.running_job()
        await w.gw.cancel("lab")
        assert w.client.cancelled == [job.job_id]
        outputs = w.stage(job.job_id, 1)
        status, _ = await w.gw.on_finish(job.job_id, {"attempt": 1, "outputs": outputs})
        assert status == 410 and w.repo.jobs[job.job_id].state == rc.CANCELLED


class TestDispatcher:
    async def test_poll_succeeded_runs_the_finish(self):
        w = World()
        job = await w.running_job()
        outputs = w.stage(job.job_id, 1)
        w.client.job_answer = (200, {"state": "succeeded", "attempt": 1, "outputs": outputs,
                                     "result": {"triangles": 2}})
        w.clock.advance(rc.POLL_AFTER_S + 1)
        await w.gw.tick()
        await w.gw.settle()
        assert w.repo.jobs[job.job_id].state == rc.SUCCEEDED

    async def test_prepare_sweeps_mesh_orphans_only(self):
        w = World()
        done = await w.succeed()
        w.objects.put("map-lab", "mesh/orphan/model.glb", b"x")
        w.objects.put("map-lab", "semantic/orphan/labels.bin", b"x")
        w.objects.put("map-lab", "reconstruction/orphan/cloud.ply", b"x")
        await w.gw.prepare()
        keys = {k[1] for k in w.objects.objects}
        assert "mesh/orphan/model.glb" not in keys
        assert "semantic/orphan/labels.bin" in keys
        assert "reconstruction/orphan/cloud.ply" in keys
        assert f"mesh/{done.job_id}/model.glb" in keys

    async def test_timeout(self):
        w = World()
        job = await w.running_job()
        w.clock.advance(CFG.job_timeout_s + 1)
        await w.gw.tick()
        assert w.repo.jobs[job.job_id].error["reason"] == "timeout"


class TestMapDelete:
    async def test_mark_map_deleting_sql(self):
        cursor = MagicMock()

        async def execute(sql, params):
            cursor.sql, cursor.params = sql, params

        async def fetchall():
            return [("j1", "running", 2)]
        cursor.execute, cursor.fetchall = execute, fetchall
        assert await ms.mark_map_deleting(cursor, "lab") == [("j1", "running", 2)]
        assert "map_meshes" in cursor.sql and "map_reconstructions" not in cursor.sql
        assert "map_deleting" in cursor.params[1]

    async def test_after_mark_cancels_at_the_service_and_writes_the_event(self):
        w = World()
        await w.gw.after_map_delete_marked("lab", [("j1", "running", 1), ("j2", "queued", 0)])
        assert w.client.cancelled == ["j1"]
        assert codes(w.repo) == [EventCode.MAP_MESH_FAILED] * 2

    def test_sql_targets_the_mesh_table(self):
        assert "map_meshes" in ms.GET_SQL and "map_reconstructions" not in ms.SUCCEED_SQL
        assert ms.LOCK_NAME == "mesh_dispatcher" and ms.TABLE == "map_meshes"
        from packages.api import map_delete
        assert map_delete.MESH_SQL == "DELETE FROM map_meshes WHERE map_name = %s"

    def test_events_build_rows(self):
        job = rc.Job("j1", "lab", rc.RUNNING, params={"session": "auto"})
        for ev in (ms.started_event(job, T0, 1),
                   ms.finished_event(job, {"session": "s1", "triangles": 4, "frames_used": 9,
                                           "poses_source": "colmap", "duration_s": 3}, T0),
                   ms.failed_event(job, rc.FAILED, {"reason": "gpu_oom"}, T0)):
            assert build_row(ev) is not None
        assert ms.started_event(job, T0, 1).discriminator == "map:lab:mesh:j1:started"


def _migration():
    path = (pathlib.Path(__file__).resolve().parents[2] / "packages/api/migrations/versions"
            / "20261021_01_map_meshes.py")
    spec = importlib.util.spec_from_file_location("m_mesh", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestMigration:
    def test_chain(self):
        mod = _migration()
        assert mod.revision == "20261021_01_map_meshes"
        assert mod.down_revision == "20261021_01_transfer_sync"

    def test_sql(self):
        up = _migration()._upgrade_sql()
        for col in rc.COLUMNS:
            assert f"\n  {col} " in up, col
        assert "map_meshes_one_active" in up and "WHERE state = 'succeeded'" in up
        assert "'graph_builder', 'reconstruction', 'semantic', 'mesh'" in up
        down = _migration()._downgrade_sql()
        assert "DROP TABLE IF EXISTS map_meshes" in down
        assert "'mesh'" not in down.split("CHECK")[1]
        assert "'semantic'" in down.split("CHECK")[1]


class TestHttp:
    @pytest.fixture
    def app(self):
        import packages.api.main as main
        w = World()
        svc = MagicMock()
        svc.mesh = w.gw
        with patch.object(main, "service", svc):
            yield main.app, w

    async def test_start_status_file_and_errors(self, app):
        app, w = app

        def new_client():
            return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")

        async with new_client() as c:
            r = await c.post("/api/v1/maps/lab/mesh", json={"quality": "fast"})
            assert r.status_code == 202 and r.json()["state"] == "queued"
            assert (await c.post("/api/v1/maps/lab/mesh")).status_code == 409
            assert (await c.post("/api/v1/maps/nope/mesh")).status_code == 404
            assert (await c.post("/api/v1/maps/lab/mesh",
                                 json={"atlas": 3})).status_code in (409, 422)
            assert (await c.get("/api/v1/maps/lab/mesh")).json()["job"]["state"] == "queued"
            assert (await c.get("/api/v1/maps/lab/mesh/files/meta.json")).status_code == 404
        await w.gw.tick()
        job = next(iter(w.repo.jobs.values()))
        outputs = w.stage(job.job_id, 1)
        async with new_client() as c:
            tok = ms.callback_token(SECRET, job.job_id)
            base = f"/internal/mesh/jobs/{job.job_id}"
            auth = {"Authorization": f"Bearer {tok}"}
            r = await c.post(base + "/progress", json={"attempt": 1, "stage": "splat"},
                             headers={"Authorization": "Bearer wrong"})
            assert r.status_code == 401
            r = await c.post(base + "/progress", json={"attempt": 1, "stage": "splat"},
                             headers=auth)
            assert r.status_code == 200 and r.json() == {"action": "continue"}
            r = await c.post(base + "/finish", json={"attempt": 1, "result": {"triangles": 7},
                                                     "outputs": outputs}, headers=auth)
            assert r.status_code == 200
            await w.gw.settle()
            r = await c.get(f"/api/v1/maps/lab/mesh/files/model.glb?v={job.job_id}")
            assert r.status_code == 200 and r.content == glb()
            assert r.headers["content-type"] == "model/gltf-binary"
            assert r.headers["etag"] == f'"{job.job_id}"'
            assert "immutable" in r.headers["cache-control"]
            assert r.headers["content-length"] == "40"
            r = await c.get("/api/v1/maps/lab/mesh/files/model.glb")
            assert r.headers["cache-control"] == "no-cache" and r.content[:4] == b"glTF"
            r = await c.get("/api/v1/maps/lab/mesh/files/model.glb",
                            headers={"If-None-Match": f'"{job.job_id}"'})
            assert r.status_code == 304
            r = await c.get("/api/v1/maps/lab/mesh/files/meta.json")
            assert r.json() == {"session": "s1"}
            assert (await c.post(base + "/other", json={})).status_code == 404
            assert (await c.get("/api/v1/maps/lab/mesh")).json()["mesh"]["triangles"] == 7
            assert (await c.delete("/api/v1/maps/lab/mesh")).status_code == 204
            assert (await c.post("/api/v1/maps/lab/mesh/cancel")).status_code == 404
            assert (await c.get("/api/v1/maps/lab/mesh/files/model.glb")).status_code == 404

    async def test_not_configured_route(self):
        import packages.api.main as main
        w = World(config=ms.MeshConfig())
        w.gw.client = None
        svc = MagicMock()
        svc.mesh = w.gw
        with patch.object(main, "service", svc):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),
                                         base_url="http://t") as c:
                r = await c.post("/api/v1/maps/lab/mesh")
                assert r.status_code == 503
                assert "not_configured" in r.text
                assert (await c.get("/api/v1/maps/lab/mesh")).json()["configured"] is False
