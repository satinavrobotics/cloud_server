"""3D reconstruction R3: the gateway (packages/api/reconstruction.py), its routes and hooks.

docs/reconstruction/design.md §6, §8, §9, §11. The repository is an in-memory fake with the
PgRepo interface (its SQL runs against real Postgres in tests/integration/reconstruction);
MinIO, the presigner and the service are fakes too.
"""
import copy
import datetime
import hashlib
import importlib.util
import json
import math
import os
import pathlib
import urllib.parse
import uuid

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from packages.api import map_delete, reconstruction as rc  # noqa: E402
from packages.api.reconstruction_client import ReconstructionClient, ServiceUnreachable  # noqa
from packages.events.codes import EventCode  # noqa: E402
from packages.events.emit import build_row  # noqa: E402

pytestmark = pytest.mark.unit

T0 = datetime.datetime(2026, 10, 20, 9, 0, tzinfo=datetime.timezone.utc)
SECRET = "s3cret"
CFG = rc.ReconConfig(service_url="http://recon:8009", service_key="k",
                     callback_secret=SECRET, callback_base_url="http://sati-cloud:8000",
                     minio_endpoint="sati-cloud:9000")
CAMERA = {"frame_id": "camera", "width": 448, "height": 336, "fx": 300.0, "fy": 300.0,
          "cx": 224.0, "cy": 168.0, "d": [0, 0, 0, 0, 0], "depth_type": "z",
          "T_base_cam": {"x": 0.1, "y": 0, "z": 1.0, "qx": -0.5, "qy": 0.5, "qz": -0.5,
                         "qw": 0.5}}


def ply(n=2):
    """A valid cloud.ply (handover §7.1) with n vertices."""
    header = ("ply\nformat binary_little_endian 1.0\ncomment satinav map=lab\n"
              f"element vertex {n}\nproperty float x\nproperty float y\nproperty float z\n"
              "property uchar red\nproperty uchar green\nproperty uchar blue\n"
              "property ushort count\nend_header\n").encode()
    return header + b"\0" * (17 * n)


def node(key, x, y, yaw, depth=True, created="2026-10-20T08:00:00", pose3d=None):
    doc = {"_key": key, "node_id": key, "pose": {"x": x, "y": y, "yaw": yaw},
           "robot_pose": {"x": -99.0, "y": -99.0, "yaw": 3.0}, "created_at": created}
    if depth:
        rec = {"camera": CAMERA, "depth_scale": 0.001, "session_id": "s1"}
        if pose3d:
            rec["pose3d_map"] = pose3d
        doc["depth"] = {"left": rec}
    return doc


# --- fakes -------------------------------------------------------------------------------------

class FakeRepo:
    def __init__(self):
        self.jobs = {}
        self.maps = {"lab": ("ALIVE", {"type": "local"})}
        self.events = []

    async def map_row(self, name):
        return self.maps.get(name)

    async def insert(self, job_id, map_name, params, requested_by):
        if any(j.map_name == map_name and j.state in rc.ACTIVE for j in self.jobs.values()):
            raise rc.JobActive(await self.active(map_name))
        n = len(self.jobs)
        self.jobs[job_id] = rc.Job(job_id, map_name, rc.QUEUED, requested_at=T0
                                   + datetime.timedelta(seconds=n), requested_by=requested_by,
                                   params=dict(params), next_try_at=T0)
        return copy.deepcopy(self.jobs[job_id])

    async def get(self, job_id):
        j = self.jobs.get(str(job_id))
        return copy.deepcopy(j) if j else None

    def _find(self, map_name, states):
        found = sorted((j for j in self.jobs.values()
                        if j.map_name == map_name and j.state in states),
                       key=lambda j: j.requested_at)
        return copy.deepcopy(found[-1]) if found else None

    async def active(self, map_name):
        return self._find(map_name, rc.ACTIVE)

    async def current(self, map_name):
        return self._find(map_name, (rc.SUCCEEDED,))

    async def latest(self, map_name):
        return self._find(map_name, rc.ACTIVE + rc.TERMINAL)

    async def by_state(self, state):
        return [copy.deepcopy(j) for j in sorted(self.jobs.values(),
                                                 key=lambda j: j.requested_at)
                if j.state == state]

    async def next_queued(self, now):
        due = [j for j in await self.by_state(rc.QUEUED)
               if (j.next_try_at or j.requested_at) <= now]
        return due[0] if due else None

    async def running_count(self):
        return len(await self.by_state(rc.RUNNING))

    async def keep_files(self):
        return {j.job_id for j in self.jobs.values() if j.state in (rc.RUNNING, rc.SUCCEEDED)}

    async def update(self, job_id, fields, *, states=rc.ACTIVE, attempts=None, event=None):
        j = self.jobs.get(job_id)
        if j is None or j.state not in states or (attempts is not None
                                                  and j.attempts != attempts):
            return False
        assert not set(fields) - rc.UPDATABLE
        for k, v in fields.items():
            setattr(j, k, copy.deepcopy(v))
        if event is not None:
            self.events.append(event)
        return True

    async def commit_success(self, job, attempt, result, artifacts, now, event):
        row = self.maps.get(job.map_name)
        if row is None or row[0] == "DELETING":
            return "map_deleting", []
        mine = self.jobs.get(job.job_id)
        if mine is None or mine.state != rc.RUNNING or mine.attempts != attempt:
            return "gone", []
        old = []
        for j in self.jobs.values():
            if j.map_name == job.map_name and j.state == rc.SUCCEEDED:
                j.state = rc.SUPERSEDED
                (j.inputs or {}).pop("nodes", None)
                old.append(j.artifacts or {})
        mine.state, mine.result, mine.artifacts = rc.SUCCEEDED, dict(result), dict(artifacts)
        mine.finished_at, mine.progress, mine.stage = now, 1.0, "done"
        self.events.append(event)
        return "ok", old

    async def supersede_current(self, map_name):
        old = []
        for j in self.jobs.values():
            if j.map_name == map_name and j.state == rc.SUCCEEDED:
                j.state = rc.SUPERSEDED
                old.append(j.artifacts or {})
        return old

    async def emit(self, event):
        self.events.append(event)

    async def claim_finalize(self, job_id, attempt, now, stale_before):
        j = self.jobs.get(job_id)
        if j is None or j.state != rc.RUNNING or j.attempts != attempt:
            return False
        if j.stage == rc.FINALIZING and j.last_contact_at is not None \
                and j.last_contact_at >= stale_before:
            return False
        j.stage, j.last_contact_at, j.progress = rc.FINALIZING, now, max(j.progress, 0.95)
        return True


class FakeObjects:
    def __init__(self):
        self.objects = {}  # (bucket, key) -> bytes
        self.buckets = {"map-lab", "recon-staging"}
        self.removed = []
        self.fail_copy = False

    def bucket_for(self, name):
        return "map-" + name.lower().replace("_", "-")

    def put(self, bucket, key, data):
        self.objects[(bucket, key)] = data

    def size(self, bucket, key):
        d = self.objects.get((bucket, key))
        return None if d is None else len(d)

    def read(self, bucket, key, limit):
        return self.objects[(bucket, key)][:limit + 1]

    def bucket_exists(self, bucket):
        return bucket in self.buckets

    def download(self, bucket, key, path):
        with open(path, "wb") as f:
            f.write(self.objects[(bucket, key)])

    def upload_file(self, bucket, key, path, content_type):
        if bucket not in self.buckets:
            raise RuntimeError("NoSuchBucket")
        with open(path, "rb") as f:
            self.objects[(bucket, key)] = f.read()

    def put_bytes(self, bucket, key, data, content_type):
        if bucket not in self.buckets:
            raise RuntimeError("NoSuchBucket")
        self.objects[(bucket, key)] = data

    def copy(self, sb, sk, db, dk):
        if self.fail_copy:
            raise RuntimeError("copy failed")
        if db not in self.buckets:
            raise RuntimeError("NoSuchBucket")
        self.objects[(db, dk)] = self.objects[(sb, sk)]

    def remove_prefix(self, bucket, prefix):
        gone = [k for k in self.objects if k[0] == bucket and k[1].startswith(prefix)]
        for k in gone:
            del self.objects[k]
        self.removed.append((bucket, prefix))
        return len(gone)

    def ensure_staging(self, bucket, expire_days=1):
        self.buckets.add(bucket)

    def result_jobs(self):
        return sorted({(b, k.split("/")[1]) for b, k in self.objects
                       if k.startswith(rc.RESULT_PREFIX)})

    def stream(self, bucket, key):
        yield self.objects[(bucket, key)]


class FakePresigner:
    def __init__(self, endpoint="sati-cloud:9000"):
        self.endpoint = endpoint

    def get(self, bucket, key, expires_s):
        return f"http://{self.endpoint}/{bucket}/{key}?X-Amz-Expires={expires_s}&sig=get"

    def put(self, bucket, key, expires_s):
        return f"http://{self.endpoint}/{bucket}/{key}?X-Amz-Expires={expires_s}&sig=put"


class FakeClient:
    def __init__(self):
        self.submitted = []
        self.cancelled = []
        self.submit_answer = (202, {"state": "queued"})
        self.job_answer = (200, {"state": "running", "attempt": 1})

    async def submit(self, manifest):
        self.submitted.append(manifest)
        if isinstance(self.submit_answer, Exception):
            raise self.submit_answer
        return self.submit_answer

    async def get_job(self, job_id):
        if isinstance(self.job_answer, Exception):
            raise self.job_answer
        return self.job_answer

    async def cancel(self, job_id):
        self.cancelled.append(job_id)
        return 200, {}

    async def close(self):
        pass


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += datetime.timedelta(seconds=s)


class World:
    def __init__(self, nodes=None, config=CFG):
        self.repo, self.objects, self.client = FakeRepo(), FakeObjects(), FakeClient()
        self.clock = Clock()
        self.mono = [0.0]
        self.nodes = {"lab": list(nodes if nodes is not None else [
            node("n1", 0.0, 1.0, 0.0, created="2026-10-20T08:00:02"),
            node("n2", 1.0, 0.0, math.pi / 2, created="2026-10-20T08:00:01",
                 pose3d={"x": 1.0, "y": 0.0, "z": 0.05, "qx": 0, "qy": 0.04, "qz": 0.7,
                         "qw": 0.71}),
            node("n3", 2.0, 0.0, 0.0, depth=False)])}
        self.reads = 0
        self.derived = []          # the grid params of each top-view derivation
        self.relief = True         # the derivation also writes the relief files
        self.derive_error = None   # raise this from the derivation
        self.gw = rc.ReconstructionGateway(
            self.repo, self._depth_nodes, self.objects, FakePresigner(), self.client, config,
            now=self.clock, monotonic=lambda: self.mono[0], derive_top_view=self._derive)

    async def _derive(self, ply_path, out_dir, **grid):
        """Stands in for reconstruction_topview's child process (tested on its own)."""
        with open(ply_path, "rb") as f:
            assert f.read(4) == b"ply\n"
        self.derived.append(grid)
        if self.derive_error is not None:
            raise self.derive_error
        names = ["ortho.png", "height.png"]
        if getattr(self, "relief", True):
            names += ["relief_rgb.png", "relief_height.png"]
        for name in names:
            with open(pathlib.Path(out_dir) / name, "wb") as f:
                f.write(b"\x89PNG-" + name[:5].encode())
        return {"resolution_m": 0.05, "origin": {"x": -1.0, "y": -2.0}, "width": 3,
                "height": 4, "z_floor": grid["z_floor"], "clip_z": grid["clip_z"],
                "clip_abs": grid["z_floor"] + grid["clip_z"], "z_offset": -0.1,
                "z_scale": 0.01, "top_view": {"points": 2, "points_rastered": 2,
                                              "cells_filled": 2}}

    def _depth_nodes(self, name):
        self.reads += 1
        docs = self.nodes.get(name, [])
        return len(docs), [d for d in docs if "depth" in d]

    def stage(self, job_id, attempt, points=2, meta=b'{"version": 1}'):
        """What the service PUTs: cloud.ply + meta.json only (handover §7)."""
        outputs = {}
        for name in rc.SERVICE_FILES:
            file = rc.FILES[name][0]
            data = meta if name == "meta" else ply(points)
            self.objects.put("recon-staging", rc.staging_key(job_id, attempt, file), data)
            outputs[name] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        return outputs

    async def running_job(self):
        job = await self.gw.start("lab", None)
        await self.gw.tick()
        return self.repo.jobs[job["job_id"]]

    async def succeed(self):
        job = await self.running_job()
        outputs = self.stage(job.job_id, job.attempts)
        status, answer = await self.gw.on_finish(job.job_id, {
            "attempt": job.attempts, "result": {"points": 1000, "frames_used": 2,
                                                "voxel_m": 0.05}, "outputs": outputs})
        assert (status, answer) == (200, {"action": "continue"})
        await self.gw.settle()
        return self.repo.jobs[job.job_id]


def codes(repo):
    return [e.code for e in repo.events]


# --- pure helpers ------------------------------------------------------------------------------

class TestHelpers:
    def test_token_is_hmac_and_checked_in_constant_time(self):
        tok = rc.callback_token(SECRET, "j1")
        import base64
        import hmac as _h
        want = base64.urlsafe_b64encode(_h.new(b"s3cret", b"j1", hashlib.sha256).digest())
        assert tok == want.decode().rstrip("=")
        assert rc.check_token(SECRET, "j1", f"Bearer {tok}")
        assert not rc.check_token(SECRET, "j2", f"Bearer {tok}")
        assert not rc.check_token(SECRET, "j1", tok)
        assert not rc.check_token(SECRET, "j1", None)
        with patch.object(rc.hmac, "compare_digest", return_value=True) as cmp:
            assert rc.check_token(SECRET, "j1", "Bearer nope")
        cmp.assert_called_once()

    def test_backoff(self):
        assert [rc.backoff(n) for n in range(1, 7)] == [10, 30, 60, 120, 120, 120]

    def test_params(self):
        p = rc.job_params(CFG, {"voxel_m": 0.1})
        assert p["voxel_m"] == 0.1 and p["max_depth_m"] == 10.0 and p["clip_z"] == 2.3
        assert p["max_voxels"] == 10_000_000 and p["raster_max_px"] == 4096
        for bad in ({"voxel_m": 0.01}, {"max_depth_m": 70}, {"clip_z": -6}, {"other": 1}, [1]):
            with pytest.raises(HTTPException) as exc:
                rc.job_params(CFG, bad)
            assert exc.value.status_code == 422

    def test_frames_use_the_map_pose_only_nodes_with_depth_in_capture_order(self):
        w = World()
        _, docs = w._depth_nodes("lab")
        frames = rc.frames_from_nodes(docs)
        assert [f.node_id for f in frames] == ["n2", "n1"]
        assert (frames[1].x, frames[1].y) == (0.0, 1.0)  # pose, never robot_pose

    def test_digest_and_stale_reasons(self):
        a = rc.frames_from_nodes([node("a", 0, 0, 0), node("b", 1, 1, 0)])
        stored = rc.digest_nodes(a)
        assert rc.input_digest(a) == rc.input_digest(list(reversed(a)))
        assert rc.stale_reason(stored, a) == {"new_nodes": 0, "removed_nodes": 0,
                                              "moved_nodes": 0}
        b = rc.frames_from_nodes([node("a", 0.0004, 0, 0), node("b", 1.2, 1, 0),
                                  node("c", 5, 5, 0)])
        assert rc.input_digest(b) != rc.input_digest(a)
        assert rc.stale_reason(stored, b) == {"new_nodes": 1, "removed_nodes": 0,
                                              "moved_nodes": 1}
        assert rc.stale_reason(stored, b[:1])["removed_nodes"] == 1

    def test_crs(self):
        geo = {"type": "geo", "geo": {"utm_zone": 34, "utm_north": True, "origin_e": 352397.3,
                                      "origin_n": 5262357.8}}
        assert rc.map_crs(geo) == {"utm_zone": 34, "utm_north": True, "origin_e": 352397.3,
                                   "origin_n": 5262357.8}
        assert rc.map_crs({"type": "local"}) is None and rc.map_crs(None) is None

    def test_manifest(self):
        w = World()
        frames = rc.frames_from_nodes(w._depth_nodes("lab")[1])
        m = rc.build_manifest(
            job_id="j1", attempt=2, map_name="lab", map_type="local", crs=None,
            params={"voxel_m": 0.05}, frames=frames, map_bucket="map-lab",
            staging_bucket="recon-staging", presign_get=lambda b, k: f"G:{b}/{k}",
            presign_put=lambda b, k: f"P:{b}/{k}", callback_base_url="http://h:8000/",
            token="tok", now=T0, expiry_s=14400)
        assert m["manifest_version"] == 1 and m["attempt"] == 2
        assert m["expires_at"] == "2026-10-20T13:00:00Z" and m["map"]["crs"] is None
        assert [n["node_id"] for n in m["nodes"]] == ["n2", "n1"]
        n2 = m["nodes"][0]
        assert n2["pose"] == {"x": 1.0, "y": 0.0, "yaw": math.pi / 2}
        cam = n2["cameras"][0]
        assert cam["rgb_url"] == "G:map-lab/n2/images/left"
        assert cam["depth_url"] == "G:map-lab/n2/depth/left.png"
        assert cam["pose3d"]["z"] == 0.05 and cam["params"] == CAMERA
        assert "pose3d" not in m["nodes"][1]["cameras"][0]
        assert "robot_pose" not in json.dumps(m) and "-99" not in json.dumps(m)
        assert m["outputs"]["cloud"] == {"url": "P:recon-staging/j1/2/cloud.ply",
                                         "content_type": "application/octet-stream"}
        assert set(m["outputs"]) == {"cloud", "meta"}  # the top view is the cloud's
        assert m["callback"] == {"base_url": "http://h:8000/internal/reconstruction/jobs/j1",
                                 "token": "tok"}

    def test_presigner_signs_for_the_service_endpoint(self):
        p = rc.Presigner("sati-cloud:9000", "AKIA", "secret", False)
        url = urllib.parse.urlsplit(p.get("map-lab", "n1/depth/left.png", 60))
        assert url.netloc == "sati-cloud:9000" and url.path == "/map-lab/n1/depth/left.png"
        q = urllib.parse.parse_qs(url.query)
        assert q["X-Amz-Expires"] == ["60"] and "X-Amz-Signature" in q
        put = urllib.parse.urlsplit(p.put("recon-staging", "j/1/cloud.ply", 14400))
        assert put.netloc == "sati-cloud:9000"
        # (the region is fixed, so presigning makes no network call: this test has no MinIO)

    def test_config_from_env(self):
        with patch.multiple("packages.config", RECONSTRUCTION_SERVICE_URL=None):
            assert not rc.ReconConfig.from_env().configured
        assert CFG.configured
        assert not rc.ReconConfig(service_url="u", service_key="k").configured


# --- start / status / cancel / delete ---------------------------------------------------------

class TestRoutesLogic:
    async def test_start_queues_and_refuses_a_second_job(self):
        w = World()
        job = await w.gw.start("lab", {"voxel_m": 0.1})
        assert job["state"] == "queued" and job["params"]["voxel_m"] == 0.1
        with pytest.raises(HTTPException) as exc:
            await w.gw.start("lab", None)
        assert exc.value.status_code == 409 and exc.value.detail["code"] == "job_active"
        assert exc.value.detail["job"]["job_id"] == job["job_id"]

    @pytest.mark.parametrize("setup,status,code", [
        (lambda w: w.repo.maps.pop("lab"), 404, "map_not_found"),
        (lambda w: w.repo.maps.update(lab=("DELETING", {})), 409, "map_deleting"),
        (lambda w: w.nodes.update(lab=[node("a", 0, 0, 0, depth=False)]), 409, "no_depth"),
    ])
    async def test_start_errors(self, setup, status, code):
        w = World()
        setup(w)
        with pytest.raises(HTTPException) as exc:
            await w.gw.start("lab", None)
        assert (exc.value.status_code, exc.value.detail["code"]) == (status, code)

    async def test_start_node_limit(self):
        w = World()
        with patch.object(rc, "MAX_NODES", 1):
            with pytest.raises(HTTPException) as exc:
                await w.gw.start("lab", None)
        assert exc.value.status_code == 422

    async def test_not_configured(self):
        w = World(config=rc.ReconConfig())
        with pytest.raises(HTTPException) as exc:
            await w.gw.start("lab", None)
        assert exc.value.status_code == 503 and exc.value.detail["code"] == "not_configured"
        status = await w.gw.status("lab")  # GET still works
        assert status["configured"] is False and status["job"] is None
        await w.gw.tick()  # the dispatcher does nothing
        assert w.client.submitted == []

    async def test_status_current_stale_and_cached(self):
        w = World()
        done = await w.succeed()
        s = await w.gw.status("lab")
        assert s["job"] is None
        r = s["reconstruction"]
        assert r["job_id"] == done.job_id and r["stale"] is False and r["points"] == 1000
        assert r["nodes_total"] == 3 and r["nodes_with_depth"] == 2
        assert r["files"]["ortho"]["url"] == (f"/api/v1/maps/lab/reconstruction/files/"
                                              f"ortho.png?v={done.job_id}")
        w.nodes["lab"].append(node("n9", 9, 9, 0))
        reads = w.reads
        assert (await w.gw.status("lab"))["reconstruction"]["stale"] is False  # cached 10 s
        assert w.reads == reads
        w.mono[0] += rc.STALE_CACHE_S
        r = (await w.gw.status("lab"))["reconstruction"]
        assert r["stale"] is True and r["stale_reason"] == {"new_nodes": 1, "removed_nodes": 0,
                                                            "moved_nodes": 0}

    async def test_status_shows_a_failed_job_after_the_result(self):
        w = World()
        await w.succeed()
        job = await w.running_job()
        await w.gw.on_fail(job.job_id, {"attempt": 1, "reason": "error", "message": "boom"})
        s = await w.gw.status("lab")
        assert s["job"]["state"] == "failed" and s["job"]["error"]["message"] == "boom"
        assert s["reconstruction"] is not None  # the old result stays

    async def test_cancel_queued_and_running(self):
        w = World()
        job = await w.gw.start("lab", None)
        assert (await w.gw.cancel("lab"))["state"] == "cancelled"
        assert w.client.cancelled == []  # never sent
        assert codes(w.repo) == [EventCode.MAP_RECONSTRUCTION_FAILED]
        job = await w.running_job()
        view = await w.gw.cancel("lab")
        assert view["state"] == "running" and view["cancel_requested"]
        assert w.client.cancelled == [job.job_id]
        status, answer = await w.gw.on_progress(job.job_id, {"attempt": 1, "stage": "x"})
        assert (status, answer) == (200, {"action": "cancel"})
        await w.gw.on_fail(job.job_id, {"attempt": 1, "reason": "cancelled"})
        assert w.repo.jobs[job.job_id].state == "cancelled"
        with pytest.raises(HTTPException) as exc:
            await w.gw.cancel("lab")
        assert exc.value.status_code == 404

    async def test_cancel_grace(self):
        w = World()
        job = await w.running_job()
        await w.gw.cancel("lab")
        w.clock.advance(rc.CANCEL_GRACE_S - 1)
        await w.gw.tick()
        assert w.repo.jobs[job.job_id].state == "running"
        w.clock.advance(1)
        w.client.job_answer = (200, {"state": "running", "attempt": 1})
        await w.gw.tick()
        assert w.repo.jobs[job.job_id].state == "cancelled"

    async def test_delete_removes_the_result_and_cancels(self):
        w = World()
        done = await w.succeed()
        prefix = rc.result_prefix(done.job_id)
        assert w.objects.size("map-lab", prefix + "cloud.ply") == len(ply())
        job = await w.running_job()
        await w.gw.delete("lab")
        assert w.repo.jobs[done.job_id].state == "superseded"
        assert w.repo.jobs[job.job_id].state == "cancelled"
        assert job.job_id in w.client.cancelled
        assert w.objects.size("map-lab", prefix + "cloud.ply") is None
        assert (await w.gw.status("lab"))["reconstruction"] is None

    async def test_open_file(self):
        w = World()
        done = await w.succeed()
        info = await w.gw.open_file("lab", "ortho.png")
        assert info == {"bucket": "map-lab", "key": rc.result_prefix(done.job_id) + "ortho.png",
                        "bytes": 10, "content_type": "image/png", "job_id": done.job_id,
                        "file": "ortho.png"}
        for bad in ("x.png", "../meta.json"):
            with pytest.raises(HTTPException) as exc:
                await w.gw.open_file("lab", bad)
            assert exc.value.status_code == 404


# --- callbacks ---------------------------------------------------------------------------------

class TestCallbacks:
    def test_authorize(self):
        w = World()
        w.gw.authorize("j1", "Bearer " + rc.callback_token(SECRET, "j1"))
        with pytest.raises(HTTPException) as exc:
            w.gw.authorize("j1", "Bearer " + rc.callback_token(SECRET, "j2"))
        assert exc.value.status_code == 401
        with pytest.raises(HTTPException) as exc:
            World(config=rc.ReconConfig()).gw.authorize("j1", "Bearer x")
        assert exc.value.status_code == 503

    async def test_progress_starts_and_updates(self):
        w = World()
        job = await w.running_job()
        status, answer = await w.gw.on_progress(job.job_id, {
            "attempt": 1, "stage": "integrating", "progress": 0.43, "frames_done": 1,
            "frames_total": 2})
        assert (status, answer) == (200, {"action": "continue"})
        j = w.repo.jobs[job.job_id]
        assert (j.stage, j.progress, j.frames_done) == ("integrating", 0.43, 1)
        assert j.started_at == T0
        await w.gw.on_progress(job.job_id, {"attempt": 1, "stage": "filtering"})
        assert codes(w.repo) == [EventCode.MAP_RECONSTRUCTION_STARTED]  # once
        row = build_row(w.repo.events[0], strict=True)
        assert row["source"] == "reconstruction" and row["robot_name"] is None
        assert row["payload"]["nodes_with_depth"] == 2

    async def test_410_for_old_attempt_or_ended_job(self):
        w = World()
        job = await w.running_job()
        assert (await w.gw.on_progress(job.job_id, {"attempt": 0}))[0] == 410
        assert (await w.gw.on_finish(job.job_id, {"attempt": 2}))[0] == 410
        assert (await w.gw.on_progress(str(uuid.uuid4()), {"attempt": 1}))[0] == 410
        await w.gw.cancel("lab")
        await w.gw.on_fail(job.job_id, {"attempt": 1, "reason": "cancelled"})
        assert await w.gw.on_progress(job.job_id, {"attempt": 1}) == (410, {"action": "stop"})

    async def test_finish_copies_supersedes_and_deletes_the_old_prefix(self):
        w = World()
        first = await w.succeed()
        second = await w.succeed()
        assert w.repo.jobs[first.job_id].state == "superseded"
        assert w.repo.jobs[second.job_id].state == "succeeded"
        assert "nodes" not in (w.repo.jobs[first.job_id].inputs or {})
        assert ("map-lab", rc.result_prefix(first.job_id)) in w.objects.removed
        assert ("recon-staging", f"{second.job_id}/") in w.objects.removed
        files = w.repo.jobs[second.job_id].artifacts["files"]
        assert files["cloud"]["key"] == f"reconstruction/{second.job_id}/cloud.ply"
        assert not [k for k in w.objects.objects if k[0] == "recon-staging"]
        finished = [e for e in w.repo.events if e.code == EventCode.MAP_RECONSTRUCTION_FINISHED]
        assert len(finished) == 2
        assert build_row(finished[-1], strict=True)["payload"]["points"] == 1000
        # a repeated finish (the answer was lost) is fine
        assert (await w.gw.on_finish(second.job_id, {"attempt": 1}))[0] == 200

    @pytest.mark.parametrize("break_it,message", [
        (lambda w, j: w.objects.objects.pop(("recon-staging", f"{j}/1/cloud.ply")),
         "missing"),
        (lambda w, j: w.objects.put("recon-staging", f"{j}/1/cloud.ply", b"short"), "bytes"),
        (lambda w, j: w.objects.put("recon-staging", f"{j}/1/cloud.ply", ply(3)[:-17]),
         "header says"),
        (lambda w, j: w.objects.put("recon-staging", f"{j}/1/cloud.ply",
                                    b"PLY?" + ply(2)[4:]), "not a PLY"),
        (lambda w, j: w.objects.put("recon-staging", f"{j}/1/meta.json", b"{" + b"x" * 26),
         "parse"),
    ])
    async def test_bad_output_keeps_the_previous_result(self, break_it, message):
        w = World()
        old = await w.succeed()
        job = await w.running_job()
        outputs = w.stage(job.job_id, 1, meta=b'{"version": 1, "points": 5}')
        break_it(w, job.job_id)
        status, _ = await w.gw.on_finish(job.job_id, {"attempt": 1, "result": {},
                                                      "outputs": outputs})
        assert status == 200
        j = w.repo.jobs[job.job_id]
        assert j.state == "failed" and j.error["reason"] == "bad_output"
        assert message in j.error["message"]
        assert w.repo.jobs[old.job_id].state == "succeeded"

    async def test_finish_derives_the_top_view_from_the_cloud(self):
        w = World()
        done = await w.succeed()
        # z_floor: median over the frames' base z (n2 pose3d z 0.05, n1 without pose3d -> 0)
        assert done.inputs["z_floor"] == pytest.approx(0.025)
        assert w.derived == [{"z_floor": pytest.approx(0.025), "clip_z": 2.3, "voxel_m": 0.05,
                              "raster_max_px": 4096}]
        prefix = rc.result_prefix(done.job_id)
        files = done.artifacts["files"]
        assert set(files) == {"cloud", "ortho", "height", "relief_rgb", "relief_height", "meta"}
        for name, file in (("ortho", "ortho.png"), ("height", "height.png"),
                           ("relief_rgb", "relief_rgb.png"),
                           ("relief_height", "relief_height.png")):
            data = w.objects.objects[("map-lab", prefix + file)]
            assert files[name] == {"key": prefix + file, "bytes": len(data),
                                   "sha256": hashlib.sha256(data).hexdigest(),
                                   "content_type": "image/png"}
        meta = json.loads(w.objects.objects[("map-lab", prefix + "meta.json")])
        assert meta["version"] == 1  # the service's fields ...
        assert meta["resolution_m"] == 0.05 and meta["origin"] == {"x": -1.0, "y": -2.0}
        assert (meta["width"], meta["height"], meta["z_scale"]) == (3, 4, 0.01)  # ... + grid
        assert meta["clip_abs"] == pytest.approx(2.325) and "top_view" not in meta
        assert files["meta"]["bytes"] == len(w.objects.objects[("map-lab",
                                                                prefix + "meta.json")])
        assert done.result["top_view"]["cells_filled"] == 2
        status = (await w.gw.status("lab"))["reconstruction"]
        assert set(status["files"]) == {"cloud", "ortho", "height", "relief_rgb",
                                        "relief_height", "meta"}
        assert status["files"]["height"]["url"].endswith(f"height.png?v={done.job_id}")
        assert status["files"]["relief_rgb"]["url"].endswith(
            f"relief_rgb.png?v={done.job_id}")
        info = await w.gw.open_file("lab", "relief_height.png")
        assert info["key"] == prefix + "relief_height.png" and info["file"] == "relief_height.png"

    async def test_results_without_relief_files_omit_them(self):
        """Results made before the relief (or a derivation without it): no keys, no error."""
        w = World()
        w.relief = False
        done = await w.succeed()
        assert set(done.artifacts["files"]) == {"cloud", "ortho", "height", "meta"}
        status = (await w.gw.status("lab"))["reconstruction"]
        assert set(status["files"]) == {"cloud", "ortho", "height", "meta"}
        with pytest.raises(HTTPException) as exc:
            await w.gw.open_file("lab", "relief_rgb.png")
        assert exc.value.status_code == 404

    async def test_top_view_uses_the_job_params_and_the_voxel_the_service_used(self):
        w = World()
        job = await w.gw.start("lab", {"clip_z": 1.5, "voxel_m": 0.1})
        await w.gw.tick()
        outputs = w.stage(job["job_id"], 1)
        await w.gw.on_finish(job["job_id"], {"attempt": 1, "result": {"voxel_m": 0.15},
                                             "outputs": outputs})
        await w.gw.settle()
        assert w.derived[-1]["clip_z"] == 1.5 and w.derived[-1]["voxel_m"] == 0.15
        assert w.repo.jobs[job["job_id"]].state == "succeeded"

    @pytest.mark.parametrize("error,reason", [
        (rc.TopViewError("top_view_failed", "no cloud point below the clip height"),
         "top_view_failed"),
        (rc.TopViewError("bad_output", "cloud.ply: PLY truncated"), "bad_output"),
        (RuntimeError("MinIO down"), "error"),
    ])
    async def test_top_view_failure_fails_the_job_and_keeps_the_old_result(self, error, reason):
        w = World()
        old = await w.succeed()
        job = await w.running_job()
        outputs = w.stage(job.job_id, 1)
        w.derive_error = error
        assert (await w.gw.on_finish(job.job_id, {"attempt": 1, "outputs": outputs}))[0] == 200
        await w.gw.settle()
        j = w.repo.jobs[job.job_id]
        assert j.state == "failed" and j.error["reason"] == reason
        assert j.error["stage"] == "finalizing"
        assert not [k for k in w.objects.objects
                    if k[1].startswith(rc.result_prefix(job.job_id))]
        assert w.repo.jobs[old.job_id].state == "succeeded"
        assert not [k for k in w.objects.objects if k[0] == "recon-staging"]
        failed = [e for e in w.repo.events if e.code == EventCode.MAP_RECONSTRUCTION_FAILED]
        assert failed[-1].payload["reason"] == reason

    async def test_one_finalizer_per_attempt(self):
        import asyncio
        w = World()
        job = await w.running_job()
        outputs = w.stage(job.job_id, 1)
        gate, calls = asyncio.Event(), []
        inner = w._derive

        async def slow(ply_path, out_dir, **grid):
            calls.append(1)
            await gate.wait()
            return await inner(ply_path, out_dir, **grid)
        w.gw._derive = slow
        body = {"attempt": 1, "outputs": outputs, "result": {"points": 2}}
        assert await w.gw.on_finish(job.job_id, body) == (200, {"action": "continue"})
        await asyncio.sleep(0)
        j = w.repo.jobs[job.job_id]
        assert (j.state, j.stage) == ("running", "finalizing")
        # a repeated finish (the service retried) and the poll start nothing new
        assert await w.gw.on_finish(job.job_id, body) == (200, {"action": "continue"})
        w.clock.advance(rc.POLL_AFTER_S - 1)
        w.client.job_answer = AssertionError("must not poll a fresh finalizer")
        await w.gw.tick()
        # another worker (no in-process task) is refused by the claim while it is fresh
        other = rc.ReconstructionGateway(w.repo, w._depth_nodes, w.objects, FakePresigner(),
                                         w.client, CFG, now=w.clock, derive_top_view=slow)
        assert await other.on_finish(job.job_id, body) == (200, {"action": "continue"})
        assert not other._finalizers
        gate.set()
        await w.gw.settle()
        assert calls == [1] and w.repo.jobs[job.job_id].state == "succeeded"

    async def test_a_stale_finalize_claim_is_taken_over_by_the_poll(self):
        w = World()
        job = await w.running_job()
        outputs = w.stage(job.job_id, 1)
        j = w.repo.jobs[job.job_id]
        j.stage, j.last_contact_at = rc.FINALIZING, w.clock.t  # a worker died finalizing
        w.clock.advance(rc.FINALIZE_STALE_S + 1)
        w.client.job_answer = (200, {"state": "succeeded", "attempt": 1,
                                     "result": {"points": 2}, "outputs": outputs})
        await w.gw.tick()
        await w.gw.settle()
        assert w.repo.jobs[job.job_id].state == "succeeded" and len(w.derived) == 1

    async def test_cancel_while_finalizing_discards_the_result(self):
        import asyncio
        w = World()
        job = await w.running_job()
        outputs = w.stage(job.job_id, 1)
        gate, inner = asyncio.Event(), w._derive

        async def slow(ply_path, out_dir, **grid):
            await gate.wait()
            return await inner(ply_path, out_dir, **grid)
        w.gw._derive = slow
        await w.gw.on_finish(job.job_id, {"attempt": 1, "outputs": outputs})
        await asyncio.sleep(0)
        await w.gw.cancel("lab")
        gate.set()
        await w.gw.settle()
        assert w.repo.jobs[job.job_id].state == "cancelled"
        assert not [k for k in w.objects.objects if k[0] == "map-lab"]

    async def test_finish_refused_for_a_deleting_map(self):
        w = World()
        job = await w.running_job()
        outputs = w.stage(job.job_id, 1)
        w.repo.maps["lab"] = ("DELETING", {})
        status, answer = await w.gw.on_finish(job.job_id, {"attempt": 1, "outputs": outputs})
        assert (status, answer) == (410, {"action": "stop"})
        assert w.repo.jobs[job.job_id].error["reason"] == "map_deleting"
        assert not [k for k in w.objects.objects if k[0] == "map-lab"]

    async def test_finish_never_creates_the_map_bucket(self):
        w = World()
        job = await w.running_job()
        outputs = w.stage(job.job_id, 1)
        w.objects.buckets.discard("map-lab")
        assert (await w.gw.on_finish(job.job_id, {"attempt": 1, "outputs": outputs}))[0] == 200
        await w.gw.settle()
        assert "map-lab" not in w.objects.buckets
        assert w.repo.jobs[job.job_id].error["reason"] == "map_deleting"

    async def test_finish_after_cancel_is_cancelled(self):
        w = World()
        job = await w.running_job()
        outputs = w.stage(job.job_id, 1)
        await w.gw.cancel("lab")
        assert (await w.gw.on_finish(job.job_id, {"attempt": 1, "outputs": outputs}))[0] == 410
        assert w.repo.jobs[job.job_id].state == "cancelled"

    async def test_fail_and_auto_retry_once_for_url_expired(self):
        w = World()
        job = await w.running_job()
        await w.gw.on_fail(job.job_id, {"attempt": 1, "reason": "url_expired"})
        j = w.repo.jobs[job.job_id]
        assert j.state == "queued" and j.attempts == 1
        await w.gw.tick()
        assert w.repo.jobs[job.job_id].attempts == 2 and w.client.submitted[-1]["attempt"] == 2
        await w.gw.on_fail(job.job_id, {"attempt": 2, "reason": "url_expired", "stage": "x"})
        j = w.repo.jobs[job.job_id]
        assert j.state == "failed" and j.error["reason"] == "url_expired"
        failed = [e for e in w.repo.events if e.code == EventCode.MAP_RECONSTRUCTION_FAILED]
        assert len(failed) == 1 and failed[0].payload["reason"] == "url_expired"

    @pytest.mark.parametrize("reason", ["error", "crashed", "timeout", "no_points"])
    async def test_no_auto_retry(self, reason):
        w = World()
        job = await w.running_job()
        await w.gw.on_fail(job.job_id, {"attempt": 1, "reason": reason})
        assert w.repo.jobs[job.job_id].state == "failed"


# --- the dispatcher ----------------------------------------------------------------------------

class TestDispatcher:
    async def test_send_builds_a_fresh_manifest_and_runs(self):
        w = World()
        job = await w.gw.start("lab", None)
        await w.gw.tick()
        j = w.repo.jobs[job["job_id"]]
        assert j.state == "running" and j.attempts == 1 and j.dispatched_at == T0
        m = w.client.submitted[0]
        assert m["job_id"] == j.job_id and m["attempt"] == 1
        assert m["callback"]["token"] == rc.callback_token(SECRET, j.job_id)
        assert m["callback"]["base_url"] == ("http://sati-cloud:8000/internal/reconstruction/"
                                             f"jobs/{j.job_id}")
        assert urllib.parse.urlsplit(m["nodes"][0]["cameras"][0]["rgb_url"]).netloc \
            == "sati-cloud:9000"
        assert j.inputs["digest"] == rc.input_digest(rc.frames_from_nodes(
            w._depth_nodes("lab")[1]))
        assert j.inputs["nodes"] == [["n2", 1.0, 0.0, round(math.pi / 2, 4)],
                                     ["n1", 0.0, 1.0, 0.0]]

    async def test_max_inflight(self):
        w = World()
        w.repo.maps["yard"] = ("ALIVE", {"type": "local"})
        w.nodes["yard"] = [node("y1", 0, 0, 0)]
        await w.gw.start("lab", None)
        await w.gw.start("yard", None)
        await w.gw.tick()
        assert len(w.client.submitted) == 1 and await w.repo.running_count() == 1

    async def test_service_down_backoff_then_queue_timeout(self):
        w = World()
        w.client.submit_answer = ServiceUnreachable("refused")
        job = await w.gw.start("lab", None)
        await w.gw.tick()
        j = w.repo.jobs[job["job_id"]]
        assert j.state == "queued" and j.next_try_at == T0 + datetime.timedelta(seconds=10)
        view = (await w.gw.status("lab"))["job"]
        assert view["waiting_for_service"] is True and view["error"] is None
        await w.gw.tick()  # not due yet
        assert len(w.client.submitted) == 1
        w.clock.advance(10)
        w.client.submit_answer = (429, {"detail": {"code": "queue_full"}})
        await w.gw.tick()
        assert w.repo.jobs[j.job_id].next_try_at == w.clock.t + datetime.timedelta(seconds=30)
        w.clock.advance(CFG.queue_timeout_s)
        await w.gw.tick()
        j = w.repo.jobs[j.job_id]
        assert j.state == "failed" and j.error["reason"] == "service_unavailable"
        assert "queue_full" in j.error["message"]

    async def test_rejected_and_stale_attempt(self):
        w = World()
        w.client.submit_answer = (409, {"detail": {"code": "stale_attempt"}})
        job = await w.gw.start("lab", None)
        await w.gw.tick()
        assert w.repo.jobs[job["job_id"]].attempts == 1  # skipped past
        w.client.submit_answer = (422, {"detail": {"code": "invalid_manifest"}})
        await w.gw.tick()
        j = w.repo.jobs[job["job_id"]]
        assert w.client.submitted[-1]["attempt"] == 2
        assert j.state == "failed" and j.error["reason"] == "rejected"

    async def test_poll_after_silence(self):
        w = World()
        job = await w.running_job()
        w.clock.advance(rc.POLL_AFTER_S - 1)
        w.client.job_answer = AssertionError("must not poll yet")
        await w.gw.tick()
        w.clock.advance(1)
        w.client.job_answer = (200, {"state": "running", "attempt": 1, "stage": "writing",
                                     "progress": 0.9})
        await w.gw.tick()
        j = w.repo.jobs[job.job_id]
        assert j.stage == "writing" and j.last_contact_at == w.clock.t

    async def test_poll_succeeded_runs_the_finish(self):
        w = World()
        job = await w.running_job()
        outputs = w.stage(job.job_id, 1)
        w.clock.advance(rc.POLL_AFTER_S)
        w.client.job_answer = (200, {"state": "succeeded", "attempt": 1,
                                     "result": {"points": 7}, "outputs": outputs})
        await w.gw.tick()
        await w.gw.settle()
        assert w.repo.jobs[job.job_id].state == "succeeded"

    async def test_poll_failed(self):
        w = World()
        job = await w.running_job()
        w.clock.advance(rc.POLL_AFTER_S)
        w.client.job_answer = (200, {"state": "failed", "attempt": 1,
                                     "error": {"reason": "crashed", "stage": "integrating"}})
        await w.gw.tick()
        assert w.repo.jobs[job.job_id].error["reason"] == "crashed"

    async def test_poll_404_resubmits_once_then_lost(self):
        w = World()
        job = await w.running_job()
        w.clock.advance(rc.POLL_AFTER_S)
        w.client.job_answer = (404, {})
        await w.gw.tick()  # requeued, then sent again in the same pass
        j = w.repo.jobs[job.job_id]
        assert j.state == "running" and j.attempts == 2
        w.clock.advance(rc.POLL_AFTER_S)
        await w.gw.tick()
        j = w.repo.jobs[job.job_id]
        assert j.state == "failed" and j.error["reason"] == "lost"

    async def test_unreachable_for_5_minutes(self):
        w = World()
        job = await w.running_job()
        w.client.job_answer = ServiceUnreachable("down")
        w.clock.advance(rc.UNREACHABLE_S - 1)
        await w.gw.tick()
        assert w.repo.jobs[job.job_id].state == "running"
        w.clock.advance(1)
        await w.gw.tick()
        assert w.repo.jobs[job.job_id].error["reason"] == "service_unavailable"

    async def test_job_timeout(self):
        w = World()
        job = await w.running_job()
        w.client.job_answer = (200, {"state": "running", "attempt": 1})
        w.clock.advance(CFG.job_timeout_s)
        await w.gw.tick()
        j = w.repo.jobs[job.job_id]
        assert j.state == "failed" and j.error["reason"] == "timeout"
        assert w.client.cancelled == [job.job_id]

    async def test_map_gone_before_send(self):
        w = World()
        job = await w.gw.start("lab", None)
        w.repo.maps["lab"] = ("DELETING", {})
        await w.gw.tick()
        assert w.repo.jobs[job["job_id"]].state == "cancelled"
        assert w.client.submitted == []

    async def test_prepare_sweeps_orphans_only(self):
        w = World()
        done = await w.succeed()
        running = await w.running_job()
        for j in ("orphan", running.job_id):
            w.objects.put("map-lab", f"reconstruction/{j}/cloud.ply", b"x")
        await w.gw.prepare()
        assert ("map-lab", "reconstruction/orphan/") in w.objects.removed
        assert w.objects.size("map-lab", f"reconstruction/{done.job_id}/cloud.ply") == len(ply())
        assert w.objects.size("map-lab", f"reconstruction/{running.job_id}/cloud.ply") == 1


# --- map delete --------------------------------------------------------------------------------

class TestMapDelete:
    async def test_mark_map_deleting_sql(self):
        cursor = MagicMock()
        cursor.execute = AsyncMock()
        cursor.fetchall = AsyncMock(return_value=[(uuid.UUID(int=5), "running", 1)])
        out = await rc.mark_map_deleting(cursor, "lab")
        sql, params = cursor.execute.await_args.args
        assert sql == rc.MAP_DELETE_CANCEL_SQL and params[0] == "lab"
        assert json.loads(params[1])["reason"] == "map_deleting"
        assert out == [(str(uuid.UUID(int=5)), "running", 1)]

    async def test_after_mark_cancels_at_the_service_and_writes_the_event(self):
        w = World()
        await w.gw.after_map_delete_marked("lab", [("j1", "running", 1), ("j2", "queued", 0)])
        assert w.client.cancelled == ["j1"]
        assert [e.payload["reason"] for e in w.repo.events] == ["map_deleting"] * 2
        assert build_row(w.repo.events[0], strict=True)["payload"]["job_id"] == "j1"

    async def test_request_runs_the_hooks(self):
        from tests.unit.test_map_delete import FakeDb
        db = FakeDb()
        db.seed("lab")
        calls = []

        async def on_mark(cursor, map_id):
            calls.append(("mark", map_id))
            return [("j1", "running", 1)]

        async def after_mark(map_id, marked):
            calls.append(("after", map_id, marked))
            raise RuntimeError("never fails the delete")
        deleter = map_delete.MapDeleter(db, lambda m: True, lambda m: True, on_mark=on_mark,
                                        after_mark=after_mark)
        with patch.object(deleter, "start"):
            body = await deleter.request("lab")
        assert body["lifecycle"] == "DELETING"
        assert calls == [("mark", "lab"), ("after", "lab", [("j1", "running", 1)])]


# --- the migration -----------------------------------------------------------------------------

def _migration():
    path = (pathlib.Path(__file__).resolve().parents[2] / "packages/api/migrations/versions"
            / "20261003_01_map_reconstructions.py")
    spec = importlib.util.spec_from_file_location("m_recon", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestMigration:
    def test_chain(self):
        mod = _migration()
        assert mod.revision == "20261003_01_map_reconstructions"
        assert mod.down_revision == "20261002_01_drop_current_map"

    def test_sql(self):
        up = _migration()._upgrade_sql()
        for col in rc.COLUMNS:
            assert f"\n  {col} " in up, col
        assert "map_reconstructions_one_active" in up and "WHERE state = 'succeeded'" in up
        assert "'graph_builder', 'reconstruction'" in up
        down = _migration()._downgrade_sql()
        assert "DROP TABLE IF EXISTS map_reconstructions" in down
        assert "'reconstruction'" not in down.split("CHECK")[1]


# --- HTTP routes -------------------------------------------------------------------------------

class TestHttp:
    @pytest.fixture
    def app(self):
        import packages.api.main as main
        w = World()
        svc = MagicMock()
        svc.reconstruction = w.gw
        with patch.object(main, "service", svc):
            yield main.app, w

    async def _client(self, app):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")

    async def test_start_status_file_and_errors(self, app):
        app, w = app
        async with await self._client(app) as c:
            r = await c.post("/api/v1/maps/lab/reconstruction", json={"voxel_m": 0.1})
            assert r.status_code == 202 and r.json()["state"] == "queued"
            r = await c.post("/api/v1/maps/lab/reconstruction")
            assert r.status_code == 409 and r.json()["detail"]["code"] == "job_active"
            r = await c.post("/api/v1/maps/nope/reconstruction")
            assert r.status_code == 404
            r = await c.post("/api/v1/maps/lab/reconstruction", json={"voxel_m": 5})
            assert r.status_code in (409, 422)
            assert (await c.get("/api/v1/maps/lab/reconstruction")).json()["job"]["state"] \
                == "queued"
            assert (await c.get("/api/v1/maps/lab/reconstruction/files/meta.json")).status_code \
                == 404
        await w.gw.tick()
        job = next(iter(w.repo.jobs.values()))
        outputs = w.stage(job.job_id, 1, meta=b'{"version": 1}')
        async with await self._client(app) as c:
            tok = rc.callback_token(SECRET, job.job_id)
            base = f"/internal/reconstruction/jobs/{job.job_id}"
            r = await c.post(base + "/progress", json={"attempt": 1, "stage": "integrating"},
                             headers={"Authorization": "Bearer wrong"})
            assert r.status_code == 401
            r = await c.post(base + "/progress", json={"attempt": 1, "stage": "integrating"},
                             headers={"Authorization": f"Bearer {tok}"})
            assert r.status_code == 200 and r.json() == {"action": "continue"}
            r = await c.post(base + "/progress", json={"attempt": 7},
                             headers={"Authorization": f"Bearer {tok}"})
            assert r.status_code == 410 and r.json() == {"action": "stop"}
            r = await c.post(base + "/finish", json={"attempt": 1, "result": {"points": 3},
                                                     "outputs": outputs},
                             headers={"Authorization": f"Bearer {tok}"})
            assert r.status_code == 200
            await w.gw.settle()
            r = await c.get(f"/api/v1/maps/lab/reconstruction/files/meta.json?v={job.job_id}")
            assert r.status_code == 200 and r.json()["version"] == 1
            assert r.json()["width"] == 3 and r.json()["z_scale"] == 0.01
            assert r.headers["etag"] == f'"{job.job_id}"'
            assert "immutable" in r.headers["cache-control"]
            assert r.headers["content-type"].startswith("application/json")
            r = await c.get("/api/v1/maps/lab/reconstruction/files/cloud.ply")
            assert r.headers["cache-control"] == "no-cache"
            assert "attachment" in r.headers["content-disposition"]
            r = await c.get("/api/v1/maps/lab/reconstruction/files/cloud.ply",
                            headers={"If-None-Match": f'"{job.job_id}"'})
            assert r.status_code == 304
            assert (await c.post(base + "/other", json={})).status_code == 404
            assert (await c.delete("/api/v1/maps/lab/reconstruction")).status_code == 204
            assert (await c.post("/api/v1/maps/lab/reconstruction/cancel")).status_code == 404


# --- the service client ------------------------------------------------------------------------

class TestClient:
    async def test_bearer_and_errors(self):
        seen = []

        def handler(request):
            seen.append((request.method, request.url.path, request.headers["authorization"]))
            if request.url.path == "/jobs/gone":
                return httpx.Response(404, json={"detail": "nope"})
            return httpx.Response(202, json={"state": "queued"})
        client = ReconstructionClient("http://recon:8009/", "key",
                                      transport=httpx.MockTransport(handler))
        assert await client.submit({"job_id": "j"}) == (202, {"state": "queued"})
        assert (await client.get_job("gone"))[0] == 404
        await client.cancel("j")
        assert seen == [("POST", "/jobs", "Bearer key"), ("GET", "/jobs/gone", "Bearer key"),
                        ("POST", "/jobs/j/cancel", "Bearer key")]
        await client.close()

        def boom(request):
            raise httpx.ConnectError("refused")
        down = ReconstructionClient("http://recon:8009", "k", transport=httpx.MockTransport(boom))
        with pytest.raises(ServiceUnreachable):
            await down.get_job("j")
