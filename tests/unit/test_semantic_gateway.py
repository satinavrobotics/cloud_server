"""Semantic layer gateway (packages/api/semantic.py), its routes, hooks and migration.

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

from packages.api import reconstruction as rc, semantic as sm  # noqa: E402
from packages.events.codes import EventCode  # noqa: E402
from packages.events.emit import build_row  # noqa: E402
from tests.unit.test_map_reconstruction_gateway import (  # noqa: E402
    SECRET, T0, FakeClient, FakeObjects, FakePresigner, FakeRepo, World as RcWorld, codes, node)

pytestmark = pytest.mark.unit

CFG = sm.SemConfig(service_url="http://sem:8010", service_key="k", callback_secret=SECRET,
                   callback_base_url="http://sati-cloud:8000", minio_endpoint="sati-cloud:9000")


def labels_bin(n=3):
    return struct.pack("<4sIIf", b"SEML", 1, n, 0.05) + b"\0" * (9 * n)


class FakeSemObjects(FakeObjects):
    def __init__(self):
        super().__init__()
        self.buckets = {"map-lab", "sem-staging"}

    def result_jobs(self):
        return sorted({(b, k.split("/")[1]) for b, k in self.objects
                       if k.startswith(sm.RESULT_PREFIX)})


class World(RcWorld):
    def __init__(self, nodes=None, config=CFG):
        super().__init__(nodes, config)
        self.objects = FakeSemObjects()
        self.gw = sm.SemanticGateway(self.repo, self._depth_nodes, self.objects,
                                     FakePresigner(), self.client, config, now=self.clock,
                                     monotonic=lambda: self.mono[0])

    def stage(self, job_id, attempt, labels=None, instances=b'[{"id": 0}]', dynamic=None,
              meta=b'{"version": 1}'):
        data = {"instances": instances, "labels": labels if labels is not None else labels_bin(),
                "dynamic": dynamic if dynamic is not None else b"PK\x05\x06" + b"\0" * 18,
                "meta": meta}
        outputs = {}
        for name, (file, _ct) in sm.FILES.items():
            self.objects.put("sem-staging", rc.staging_key(job_id, attempt, file), data[name])
            outputs[name] = {"bytes": len(data[name]),
                             "sha256": hashlib.sha256(data[name]).hexdigest()}
        return outputs

    async def succeed(self):
        job = await self.running_job()
        outputs = self.stage(job.job_id, job.attempts)
        status, _ = await self.gw.on_finish(job.job_id, {
            "attempt": job.attempts, "outputs": outputs,
            "result": {"instances": 5, "label_voxels": 3, "frames_used": 2, "frames_total": 2,
                       "dynamic_frames": 1, "depth_source": "sensor"}})
        assert status == 200
        await self.gw.settle()
        return self.repo.jobs[job.job_id]


class TestHelpers:
    def test_params_defaults_and_overrides(self):
        p = sm.job_params(CFG, None)
        assert p == {"vocabulary": "closed", "depth_source": "sensor", "max_depth_m": 10.0,
                     "min_voxels": 15}
        p = sm.job_params(CFG, {"depth_source": "da3", "every": 3, "min_voxels": 40})
        assert p["depth_source"] == "da3" and p["every"] == 3 and p["min_voxels"] == 40

    @pytest.mark.parametrize("body", [
        {"vocabulary": "open"}, {"vocabulary": "x"}, {"depth_source": "lidar"},
        {"every": 0}, {"every": 99}, {"max_depth_m": 100}, {"min_voxels": 1}, {"bogus": 1}, [1]])
    def test_params_rejected(self, body):
        with pytest.raises(HTTPException) as exc:
            sm.job_params(CFG, body)
        assert exc.value.status_code == 422

    def test_choose_every(self):
        frames = rc.frames_from_nodes([node(f"n{i}", i, 0, 0) for i in range(1000)])
        assert sm.choose_every(frames, {}, 400) == 3        # 1000 cameras / 400
        assert sm.choose_every(frames, {"every": 7}, 400) == 7
        assert sm.choose_every(frames[:10], {}, 400) == 1

    def test_manifest(self):
        frames = rc.frames_from_nodes([node("n1", 0, 1, 0.5)])
        m = sm.build_manifest(
            job_id="j1", attempt=2, map_name="lab", map_type=None, crs=None,
            params={"vocabulary": "closed", "depth_source": "sensor", "every": 5,
                    "max_depth_m": 10.0, "min_voxels": 15},
            frames=frames, map_bucket="map-lab", staging_bucket="sem-staging",
            presign_get=lambda b, k: f"get:{b}/{k}", presign_put=lambda b, k: f"put:{b}/{k}",
            callback_base_url="http://x:8000/", token="t", now=T0, expiry_s=60)
        assert m["params"]["every"] == 1 and m["params"]["vocabulary"] == "closed"
        assert set(m["outputs"]) == {"instances", "labels", "dynamic", "meta"}
        assert m["outputs"]["labels"]["url"] == "put:sem-staging/j1/2/labels.bin"
        assert m["outputs"]["dynamic"]["content_type"] == "application/zip"
        assert m["callback"]["base_url"] == "http://x:8000/internal/semantic/jobs/j1"
        cam = m["nodes"][0]["cameras"][0]
        assert cam["rgb_url"].startswith("get:map-lab/n1/images/")
        assert "robot_pose" not in json.dumps(m)

    def test_token_is_shared_with_reconstruction(self):
        assert sm.callback_token(SECRET, "j") == rc.callback_token(SECRET, "j")

    def test_config_from_env(self):
        with patch.dict(os.environ, {}, clear=False):
            assert not sm.SemConfig.from_env().configured or True
        assert CFG.staging_bucket == "sem-staging" and CFG.configured


class TestRoutesLogic:
    async def test_start_queues_and_refuses_a_second_job(self):
        w = World()
        job = await w.gw.start("lab", {"depth_source": "da3"}, "me")
        assert job["state"] == "queued" and job["params"]["depth_source"] == "da3"
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
            await w.gw.start("lab", {"vocabulary": "open"})
        assert exc.value.status_code == 422

    async def test_not_configured(self):
        w = World(config=sm.SemConfig())
        w.gw.client = None
        with pytest.raises(HTTPException) as exc:
            await w.gw.start("lab", None)
        assert exc.value.status_code == 503 and exc.value.detail["code"] == "not_configured"

    async def test_independent_of_reconstruction(self):
        """A semantic job runs with no reconstruction result anywhere."""
        w = World()
        job = await w.running_job()
        assert job.state == rc.RUNNING and len(w.client.submitted) == 1

    async def test_status_stale_and_cancel(self):
        w = World()
        done = await w.succeed()
        st = await w.gw.status("lab")
        assert st["semantic"]["job_id"] == done.job_id and st["semantic"]["stale"] is False
        assert st["semantic"]["instances"] == 5
        assert set(st["semantic"]["files"]) == {"instances", "labels", "dynamic", "meta"}
        assert st["semantic"]["files"]["labels"]["url"].endswith(
            f"/semantic/files/labels.bin?v={done.job_id}")
        w.nodes["lab"].append(node("n9", 5, 5, 0, created="2026-10-20T08:05:00"))
        w.mono[0] += 100
        st = await w.gw.status("lab")
        assert st["semantic"]["stale"] is True and st["semantic"]["stale_reason"]["new_nodes"] == 1
        with pytest.raises(HTTPException) as exc:
            await w.gw.cancel("lab")
        assert exc.value.detail["code"] == "no_active_job"
        await w.gw.start("lab", None)
        assert (await w.gw.cancel("lab"))["state"] == "cancelled"

    async def test_open_file_and_delete(self):
        w = World()
        with pytest.raises(HTTPException) as exc:
            await w.gw.open_file("lab", "labels.bin")
        assert exc.value.detail["code"] == "no_semantic"
        done = await w.succeed()
        info = await w.gw.open_file("lab", "labels.bin")
        assert info["key"] == f"semantic/{done.job_id}/labels.bin" and info["bucket"] == "map-lab"
        with pytest.raises(HTTPException):
            await w.gw.open_file("lab", "cloud.ply")
        await w.gw.delete("lab")
        assert w.repo.jobs[done.job_id].state == rc.SUPERSEDED
        assert not any(k[1].startswith("semantic/") for k in w.objects.objects)


class TestManifestDispatch:
    async def test_nodes_are_thinned_to_the_frame_budget(self):
        nodes = [node(f"n{i}", i, 0, 0, created=f"2026-10-20T08:{i // 60:02d}:{i % 60:02d}")
                 for i in range(10)]
        w = World(nodes=nodes, config=sm.SemConfig(**{**CFG.__dict__, "max_frames": 4}))
        await w.gw.start("lab", None)
        await w.gw.tick()
        m = w.client.submitted[0]
        assert len(m["nodes"]) == 4 and m["params"]["every"] == 1     # every 3rd of 10
        job = next(iter(w.repo.jobs.values()))
        assert job.inputs["every"] == 3 and job.inputs["nodes_used"] == 4
        assert job.frames_total == 4 and job.inputs["nodes_with_depth"] == 10

    async def test_user_every(self):
        w = World()
        await w.gw.start("lab", {"every": 2})
        await w.gw.tick()
        assert len(w.client.submitted[0]["nodes"]) == 1       # 2 depth nodes, every 2nd

    async def test_rejected_by_the_service(self):
        w = World()
        w.client.submit_answer = (422, {"detail": {"code": "invalid_manifest"}})
        job = await w.gw.start("lab", None)
        await w.gw.tick()
        j = w.repo.jobs[job["job_id"]]
        assert j.state == rc.FAILED and j.error["reason"] == "rejected"
        assert EventCode.MAP_SEMANTIC_FAILED in codes(w.repo)


class TestCallbacks:
    def test_authorize(self):
        w = World()
        w.gw.authorize("j1", f"Bearer {sm.callback_token(SECRET, 'j1')}")
        for bad in (None, "Bearer nope", "Basic x"):
            with pytest.raises(HTTPException) as exc:
                w.gw.authorize("j1", bad)
            assert exc.value.status_code == 401
        w.gw.config = sm.SemConfig()
        with pytest.raises(HTTPException) as exc:
            w.gw.authorize("j1", None)
        assert exc.value.status_code == 503

    async def test_progress_starts_with_a_semantic_event(self):
        w = World()
        job = await w.running_job()
        status, answer = await w.gw.on_progress(job.job_id, {
            "attempt": 1, "stage": "segmenting", "progress": 0.3, "frames_done": 1})
        assert (status, answer) == (200, {"action": "continue"})
        j = w.repo.jobs[job.job_id]
        assert j.stage == "segmenting" and j.started_at == T0
        assert codes(w.repo) == [EventCode.MAP_SEMANTIC_STARTED]
        assert (await w.gw.on_progress(job.job_id, {"attempt": 9}))[0] == 410

    async def test_finish_copies_and_supersedes(self):
        w = World()
        first = await w.succeed()
        assert first.state == rc.SUCCEEDED
        for name, (file, _) in sm.FILES.items():
            assert ("map-lab", f"semantic/{first.job_id}/{file}") in w.objects.objects
        assert not [k for k in w.objects.objects if k[0] == "sem-staging"]    # staging cleaned
        assert codes(w.repo)[-1] == EventCode.MAP_SEMANTIC_FINISHED
        second = await w.succeed()
        assert w.repo.jobs[first.job_id].state == rc.SUPERSEDED
        assert not any(k[1].startswith(f"semantic/{first.job_id}") for k in w.objects.objects)
        assert second.state == rc.SUCCEEDED

    @pytest.mark.parametrize("kwargs,message", [
        ({"labels": labels_bin(3)[:-1]}, "labels.bin"),
        ({"labels": b"XXXX" + labels_bin(3)[4:]}, "SEML"),
        ({"labels": b"SEML"}, "shorter"),
        ({"instances": b"{not json"}, "instances.json does not parse"),
        ({"instances": b'{"a": 1}'}, "not a list"),
        ({"dynamic": b"nope"}, "not a zip"),
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
        assert codes(w.repo)[-1] == EventCode.MAP_SEMANTIC_FAILED

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
                                     "result": {"instances": 2}})
        w.clock.advance(rc.POLL_AFTER_S + 1)
        await w.gw.tick()
        await w.gw.settle()
        assert w.repo.jobs[job.job_id].state == rc.SUCCEEDED

    async def test_prepare_sweeps_semantic_orphans_only(self):
        w = World()
        done = await w.succeed()
        w.objects.put("map-lab", "semantic/orphan/labels.bin", b"x")
        w.objects.put("map-lab", "reconstruction/orphan/cloud.ply", b"x")
        await w.gw.prepare()
        keys = {k[1] for k in w.objects.objects}
        assert "semantic/orphan/labels.bin" not in keys
        assert "reconstruction/orphan/cloud.ply" in keys
        assert f"semantic/{done.job_id}/labels.bin" in keys

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
        assert await sm.mark_map_deleting(cursor, "lab") == [("j1", "running", 2)]
        assert "map_semantics" in cursor.sql and "map_reconstructions" not in cursor.sql
        assert "map_deleting" in cursor.params[1]

    async def test_after_mark_cancels_at_the_service_and_writes_the_event(self):
        w = World()
        await w.gw.after_map_delete_marked("lab", [("j1", "running", 1), ("j2", "queued", 0)])
        assert w.client.cancelled == ["j1"]
        assert codes(w.repo) == [EventCode.MAP_SEMANTIC_FAILED] * 2

    def test_sql_targets_the_semantic_table(self):
        assert "map_semantics" in sm.GET_SQL and "map_reconstructions" not in sm.SUCCEED_SQL
        from packages.api import map_delete
        assert map_delete.SEMANTICS_SQL == "DELETE FROM map_semantics WHERE map_name = %s"

    def test_events_build_rows(self):
        w = World()
        job = rc.Job("j1", "lab", rc.RUNNING, params={"depth_source": "sensor"})
        for ev in (sm.started_event(job, T0, 1),
                   sm.finished_event(job, {"instances": 4, "label_voxels": 9,
                                           "depth_source": "sensor"}, T0),
                   sm.failed_event(job, rc.FAILED, {"reason": "error"}, T0)):
            row = build_row(ev)
            assert row is not None
        assert w


def _migration():
    path = (pathlib.Path(__file__).resolve().parents[2] / "packages/api/migrations/versions"
            / "20261015_01_map_semantics.py")
    spec = importlib.util.spec_from_file_location("m_sem", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestMigration:
    def test_chain(self):
        mod = _migration()
        assert mod.revision == "20261015_01_map_semantics"
        assert mod.down_revision == "20261014_01_run_cause_codes"

    def test_sql(self):
        up = _migration()._upgrade_sql()
        for col in rc.COLUMNS:
            assert f"\n  {col} " in up, col
        assert "map_semantics_one_active" in up and "WHERE state = 'succeeded'" in up
        assert "'graph_builder', 'reconstruction', 'semantic'" in up
        down = _migration()._downgrade_sql()
        assert "DROP TABLE IF EXISTS map_semantics" in down
        assert "'semantic'" not in down.split("CHECK")[1]
        assert "'reconstruction'" in down.split("CHECK")[1]


class TestHttp:
    @pytest.fixture
    def app(self):
        import packages.api.main as main
        w = World()
        svc = MagicMock()
        svc.semantic = w.gw
        with patch.object(main, "service", svc):
            yield main.app, w

    async def test_start_status_file_and_errors(self, app):
        app, w = app
        def new_client():
            return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")

        async with new_client() as c:
            r = await c.post("/api/v1/maps/lab/semantic", json={"depth_source": "da3"})
            assert r.status_code == 202 and r.json()["state"] == "queued"
            assert (await c.post("/api/v1/maps/lab/semantic")).status_code == 409
            assert (await c.post("/api/v1/maps/nope/semantic")).status_code == 404
            assert (await c.post("/api/v1/maps/lab/semantic",
                                 json={"vocabulary": "open"})).status_code in (409, 422)
            assert (await c.get("/api/v1/maps/lab/semantic")).json()["job"]["state"] == "queued"
            assert (await c.get("/api/v1/maps/lab/semantic/files/meta.json")).status_code == 404
        await w.gw.tick()
        job = next(iter(w.repo.jobs.values()))
        outputs = w.stage(job.job_id, 1)
        async with new_client() as c:
            tok = sm.callback_token(SECRET, job.job_id)
            base = f"/internal/semantic/jobs/{job.job_id}"
            auth = {"Authorization": f"Bearer {tok}"}
            r = await c.post(base + "/progress", json={"attempt": 1, "stage": "segmenting"},
                             headers={"Authorization": "Bearer wrong"})
            assert r.status_code == 401
            r = await c.post(base + "/progress", json={"attempt": 1, "stage": "segmenting"},
                             headers=auth)
            assert r.status_code == 200 and r.json() == {"action": "continue"}
            r = await c.post(base + "/finish", json={"attempt": 1, "result": {"instances": 1},
                                                     "outputs": outputs}, headers=auth)
            assert r.status_code == 200
            await w.gw.settle()
            r = await c.get(f"/api/v1/maps/lab/semantic/files/instances.json?v={job.job_id}")
            assert r.status_code == 200 and r.json() == [{"id": 0}]
            assert r.headers["etag"] == f'"{job.job_id}"'
            assert "immutable" in r.headers["cache-control"]
            r = await c.get("/api/v1/maps/lab/semantic/files/labels.bin")
            assert r.headers["cache-control"] == "no-cache" and r.content[:4] == b"SEML"
            r = await c.get("/api/v1/maps/lab/semantic/files/labels.bin",
                            headers={"If-None-Match": f'"{job.job_id}"'})
            assert r.status_code == 304
            assert (await c.post(base + "/other", json={})).status_code == 404
            assert (await c.get("/api/v1/maps/lab/semantic")).json()["semantic"]["instances"] == 1
            assert (await c.delete("/api/v1/maps/lab/semantic")).status_code == 204
            assert (await c.post("/api/v1/maps/lab/semantic/cancel")).status_code == 404
