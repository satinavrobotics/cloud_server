"""Mesh gateway (docs/mesh/design.md, contract cloud_mesh/CONTRACT.md).

The same shape as the semantic layer gateway (packages/api/semantic.py) and, through it, the 3D
reconstruction gateway (packages/api/reconstruction.py, whose module docstring describes the job
states, the dispatcher, the callbacks and the finish steps): the job row, the MinIO helpers, the
callback token, the stale digest, the dispatcher and the callback handlers are inherited; this
module only changes what differs.

    POST   /api/v1/maps/{map}/mesh              start()     202 + the job
    GET    /api/v1/maps/{map}/mesh              status()    result (+ stale) and job
    POST   /api/v1/maps/{map}/mesh/cancel       cancel()
    DELETE /api/v1/maps/{map}/mesh              delete()    204
    GET    /api/v1/maps/{map}/mesh/files/{name} open_file() (streamed by main.py)
    POST   /internal/mesh/jobs/{id}/progress|finish|fail

Differences from semantic:
  * The service builds ONE mapping session of the map (a map is several sessions that disagree in
    pose and lighting): `params.session` is "auto" (default, the service picks) or a session id.
    So every manifest node carries `session_id` (the Arango node document's, "unknown" when the
    node has none) and the gateway does the thinning per session.
  * The service delivers two files (model.glb, meta.json). Finish verifies them in staging
    (sizes; model.glb is a glTF 2 binary whose header length is the size; meta.json parses),
    copies them server-side to `map-{id}/mesh/{job}/` and commits. Nothing is derived here.
  * Table `map_meshes`, dispatcher lock `mesh_dispatcher`, events MAP.MESH_*.
  * Thinning: for each session, every `every`-th node (capture order) goes into the manifest,
    with `every` chosen so the session has at most `max_frames` camera frames (the request's, at
    most MESH_MAX_FRAMES). An explicit session sends only that session's nodes. The manifest then
    carries `max_frames` for the service's own cap.
  * Limitation: like reconstruction, only nodes with a `depth.{camera}` record are used.
"""

import asyncio
import dataclasses
import datetime
import json
import logging
import math
import struct
import urllib.parse
import uuid
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import pydantic
from fastapi import HTTPException

from packages.api import person_removal as pr
from packages.api import reconstruction as rc
from packages.api.entrypoint import advisory_lock_key
from packages.api.mesh_client import MeshClient
from packages.api.reconstruction import (ACTIVE, CANCELLED, FAILED, FINALIZING, QUEUED, RUNNING,
                                         SUCCEEDED, Frame, Job, JobActive, ObjectStore,
                                         Presigner, _err, _error, _float, _int, _iso, _param,
                                         _ts, _utcnow, callback_token, digest_nodes,
                                         input_digest, map_crs, rgb_key, depth_key,
                                         staging_key, stale_reason)
from packages.events.codes import EventCode, Source
from packages.events.emit import Event

logger = logging.getLogger("ApiDelegationService.mesh")

# --- constants ---------------------------------------------------------------------------------

# name -> (file, content type): every file of a result, as the client reads them
FILES: Dict[str, Tuple[str, str]] = {
    "mesh": ("model.glb", "model/gltf-binary"),
    "meta": ("meta.json", "application/json"),
}
FILE_BY_NAME = {f: (k, ct) for k, (f, ct) in FILES.items()}
RESULT_PREFIX = "mesh/"
LOCK_NAME = "mesh_dispatcher"
TABLE = "map_meshes"
MANIFEST_VERSION = 1

QUALITIES = ("fast", "standard")
ATLAS_SIZES = (1024, 2048, 4096, 8192)
# the service's own ranges (cloud_mesh/mesh_service/config.py)
PARAM_RANGES = {"max_frames": (10, 2000), "triangles": (10_000, 1_000_000),
                "splat_steps": (500, 100_000)}
PARAM_KEYS = ("session", "quality", "max_frames", "triangles", "atlas", "splat_steps",
              "texture_opt")
UNKNOWN_SESSION = "unknown"

MAX_GLB_BYTES = 1 << 30
GLB_MAGIC = b"glTF"
GLB_HEADER = struct.Struct("<4sII")              # magic, version (2), total length  (12 bytes)


# --- config ------------------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class MeshConfig(rc.ReconConfig):
    staging_bucket: str = "mesh-staging"
    max_depth_m: float = 6.0
    max_frames: int = 400

    @classmethod
    def from_env(cls) -> "MeshConfig":
        from packages import config as c
        return cls(
            service_url=c.MESH_SERVICE_URL, service_key=c.MESH_SERVICE_KEY,
            callback_secret=c.MESH_CALLBACK_SECRET,
            callback_base_url=c.MESH_CALLBACK_BASE_URL,
            minio_endpoint=c.MESH_MINIO_ENDPOINT, minio_secure=c.MESH_MINIO_SECURE,
            staging_bucket=c.MESH_STAGING_BUCKET, url_expiry_s=c.MESH_URL_EXPIRY_S,
            job_timeout_s=c.MESH_JOB_TIMEOUT_S, queue_timeout_s=c.MESH_QUEUE_TIMEOUT_S,
            max_inflight=max(1, c.MESH_MAX_INFLIGHT), max_depth_m=c.MESH_MAX_DEPTH_M,
            max_frames=max(1, c.MESH_MAX_FRAMES))

    def default_params(self) -> Dict[str, Any]:
        return {"session": "auto", "quality": "standard", "max_frames": self.max_frames,
                "triangles": 300_000, "atlas": 4096, "texture_opt": True}


# --- pure helpers ------------------------------------------------------------------------------

class StartBody(pydantic.BaseModel):
    """POST body (all optional): the job parameters a user may override."""
    session: Optional[str] = None
    quality: Optional[str] = None
    max_frames: Optional[int] = None
    triangles: Optional[int] = None
    atlas: Optional[int] = None
    splat_steps: Optional[int] = None
    texture_opt: Optional[bool] = None
    # person removal for this run (docs/reconstruction/person_removal.md); null: the map's / server's setting
    person_removal: Optional[bool] = None

    class Config:
        extra = pydantic.Extra.forbid

    @pydantic.validator("session")
    def _session(cls, value):  # noqa: N805 - pydantic v1 validator
        if value is None:
            return value
        if not value.strip() or len(value) > 128 or any(ord(c) < 32 or ord(c) == 127
                                                        for c in value):
            raise ValueError("must be 'auto' or a session id (1-128 printable characters)")
        return value

    @pydantic.validator("quality")
    def _quality(cls, value):  # noqa: N805
        if value is not None and value not in QUALITIES:
            raise ValueError(f"must be one of {', '.join(QUALITIES)}")
        return value

    @pydantic.validator("atlas")
    def _atlas(cls, value):  # noqa: N805
        if value is not None and value not in ATLAS_SIZES:
            raise ValueError(f"must be one of {', '.join(str(a) for a in ATLAS_SIZES)}")
        return value

    @pydantic.validator("max_frames", "triangles", "splat_steps")
    def _in_range(cls, value, field):  # noqa: N805
        if value is None:
            return value
        lo, hi = PARAM_RANGES[field.name]
        if not (lo <= value <= hi):
            raise ValueError(f"must be between {lo} and {hi}")
        return value


def job_params(config: MeshConfig, body: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """The job's params: the gateway's defaults and the body's overrides (`max_frames` is capped
    at the config's; `splat_steps` only when the body sets it). 422 on a bad body."""
    if body is None:
        body = {}
    if not isinstance(body, Mapping):
        raise HTTPException(422, [{"loc": ["body"], "msg": "expected a JSON object",
                                   "type": "type_error.dict"}])
    try:
        parsed = StartBody.parse_obj(dict(body))
    except pydantic.ValidationError as exc:
        raise HTTPException(422, [{"loc": ["body", *e["loc"]], "msg": e["msg"],
                                   "type": e["type"]} for e in exc.errors()])
    params = config.default_params()
    params.update({k: v for k, v in parsed.dict().items() if v is not None})
    params["max_frames"] = min(int(params["max_frames"]), config.max_frames)
    return params


def node_session(frame: Frame) -> str:
    return frame.session_id or UNKNOWN_SESSION


def session_counts(frames: Sequence[Frame]) -> Dict[str, int]:
    """session id -> number of nodes with depth."""
    counts: Dict[str, int] = {}
    for f in frames:
        counts[node_session(f)] = counts.get(node_session(f), 0) + 1
    return counts


def select_frames(frames: Sequence[Frame], params: Mapping[str, Any], max_frames: int
                  ) -> Tuple[List[Frame], Dict[str, Dict[str, int]]]:
    """The manifest's nodes (capture order kept) and, per session, {nodes, used, every}: an
    explicit `params.session` keeps only that session; each kept session is thinned by the
    smallest `every` that keeps its camera frames at most `max_frames`."""
    wanted = params.get("session") or "auto"
    pool = [f for f in frames if wanted == "auto" or node_session(f) == wanted]
    by_session: Dict[str, List[Frame]] = {}
    for f in pool:
        by_session.setdefault(node_session(f), []).append(f)
    keep, info = set(), {}
    for sid, fs in by_session.items():
        total = sum(len(f.cameras) for f in fs)
        every = max(1, math.ceil(total / max(1, max_frames)))
        chosen = fs[::every]
        keep.update(f.node_id for f in chosen)
        info[sid] = {"nodes": len(fs), "used": len(chosen), "every": every}
    return [f for f in pool if f.node_id in keep], info


def build_manifest(*, job_id: str, attempt: int, map_name: str, map_type: Optional[str],
                   crs: Optional[Dict[str, Any]], params: Mapping[str, Any],
                   frames: Sequence[Frame], map_bucket: str, staging_bucket: str,
                   presign_get: Callable[[str, str], str],
                   presign_put: Callable[[str, str], str], callback_base_url: str,
                   token: str, now: datetime.datetime, expiry_s: int,
                   person_removal: bool = False) -> Dict[str, Any]:
    """The POST /jobs body (CONTRACT.md). `frames` are already thinned. Like the semantic
    manifest, with `session_id` on every node. With `person_removal`, a frame with people gets
    the masked depth and a `mask_url` (person_removal.manifest_camera)."""
    nodes = []
    for f in frames:
        cameras = []
        for name in sorted(f.cameras):
            rec = f.cameras[name]
            cam = {"name": name, "params": dict(rec["camera"]),
                   "depth_scale": float(rec.get("depth_scale") or 0.001),
                   "rgb_url": presign_get(map_bucket, rgb_key(f.node_id, name)),
                   "depth_url": presign_get(map_bucket, depth_key(f.node_id, name))}
            pr.manifest_camera(cam, f.node_id, name, f.person_masks.get(name), person_removal,
                               presign_get, map_bucket)
            if isinstance(rec.get("pose3d_map"), Mapping):
                cam["pose3d"] = dict(rec["pose3d_map"])
            cameras.append(cam)
        nodes.append({"node_id": f.node_id, "session_id": node_session(f),
                      "pose": {"x": f.x, "y": f.y, "yaw": f.yaw}, "cameras": cameras})
    send = {k: params[k] for k in PARAM_KEYS if params.get(k) is not None}
    outputs = {name: {"url": presign_put(staging_bucket, staging_key(job_id, attempt, file)),
                      "content_type": ct} for name, (file, ct) in FILES.items()}
    return {
        "manifest_version": MANIFEST_VERSION,
        "job_id": job_id, "attempt": attempt,
        "created_at": _iso(now),
        "expires_at": _iso(now + datetime.timedelta(seconds=expiry_s)),
        "map": {"name": map_name, "type": map_type or "local", "crs": crs},
        "params": send,
        "nodes": nodes,
        "outputs": outputs,
        "callback": {"base_url": callback_url(callback_base_url, job_id), "token": token},
    }


def result_prefix(job_id: str) -> str:
    return f"{RESULT_PREFIX}{job_id}/"


def callback_url(base_url: str, job_id: str) -> str:
    return f"{base_url.rstrip('/')}/internal/mesh/jobs/{job_id}"


def file_url(map_name: str, file: str, job_id: str) -> str:
    return (f"/api/v1/maps/{urllib.parse.quote(map_name, safe='')}/mesh/files/"
            f"{file}?v={job_id}")


def mesh_view(job: Job, stale: Optional[bool], reason: Optional[Dict[str, int]]
              ) -> Dict[str, Any]:
    """The map's current result (`mesh` in the status)."""
    result, inputs = job.result or {}, job.inputs or {}
    files = {}
    for name, info in ((job.artifacts or {}).get("files") or {}).items():
        file = FILES.get(name, (name, None))[0]
        files[name] = {"name": file, "bytes": info.get("bytes"),
                       "url": file_url(job.map_name, file, job.job_id)}
    return {"job_id": job.job_id, "finished_at": _iso(job.finished_at),
            "params": {k: job.params.get(k) for k in PARAM_KEYS},
            "session": result.get("session"),
            "sessions_rejected": result.get("sessions_rejected"),
            "triangles": result.get("triangles"), "bounds": result.get("bounds"),
            "poses_source": result.get("poses_source"), "warnings": result.get("warnings"),
            "nodes_with_depth": inputs.get("nodes_with_depth"),
            "nodes_used": inputs.get("nodes_used"), "sessions": inputs.get("sessions"),
            "frames_total": result.get("frames_total", inputs.get("frames")),
            "frames_used": result.get("frames_used"), "duration_s": result.get("duration_s"),
            "stale": stale, "stale_reason": reason if stale else None, "files": files,
            "person_removal": inputs.get("person_removal")}


def glb_problem(head: bytes, size: int) -> Optional[str]:
    """Why `head` (the first bytes) of a `size`-byte file is not a glTF 2 binary, else None:
    magic "glTF", version 2, and the header's total length equal to the file size."""
    if len(head) < GLB_HEADER.size:
        return "shorter than its header"
    magic, version, length = GLB_HEADER.unpack(head[:GLB_HEADER.size])
    if magic != GLB_MAGIC:
        return "not a glTF binary (bad magic)"
    if version != 2:
        return f"glTF version {version}, expected 2"
    if length != size:
        return f"{size} bytes, its header says {length}"
    if size > MAX_GLB_BYTES:
        return "too large"
    return None


def _discriminator(map_name: str, job_id: str, state: str) -> str:
    return f"map:{map_name}:mesh:{job_id}:{state}"


def _text(value: Any) -> Optional[str]:
    return str(value) if value is not None else None


def started_event(job: Job, now: datetime.datetime, attempt: int) -> Event:
    return Event(EventCode.MAP_MESH_STARTED, now, source=Source.MESH,
                 discriminator=_discriminator(job.map_name, job.job_id, "started"),
                 payload={"map_name": job.map_name, "job_id": job.job_id,
                          "params": job.params, "attempt": attempt,
                          "nodes_with_depth": (job.inputs or {}).get("nodes_with_depth")})


def finished_event(job: Job, result: Mapping[str, Any], now: datetime.datetime) -> Event:
    return Event(EventCode.MAP_MESH_FINISHED, now, source=Source.MESH,
                 discriminator=_discriminator(job.map_name, job.job_id, SUCCEEDED),
                 payload={"map_name": job.map_name, "job_id": job.job_id,
                          "session": _text(result.get("session")),
                          "triangles": _int(result.get("triangles")),
                          "frames_used": _int(result.get("frames_used")),
                          "poses_source": _text(result.get("poses_source")),
                          "duration_s": _float(result.get("duration_s"))})


def failed_event(job: Job, state: str, error: Mapping[str, Any],
                 now: datetime.datetime) -> Event:
    return Event(EventCode.MAP_MESH_FAILED, now, source=Source.MESH,
                 discriminator=_discriminator(job.map_name, job.job_id, state),
                 payload={"map_name": job.map_name, "job_id": job.job_id,
                          "reason": str(error.get("reason") or "error"),
                          "stage": error.get("stage"), "message": error.get("message")})


# --- Postgres ----------------------------------------------------------------------------------

def _sql(statement: str) -> str:
    """A reconstruction statement on map_meshes (same columns, same indexes' semantics)."""
    return statement.replace(rc.TABLE, TABLE)


INSERT_SQL = _sql(rc.INSERT_SQL)
GET_SQL = _sql(rc.GET_SQL)
ACTIVE_SQL = _sql(rc.ACTIVE_SQL)
CURRENT_SQL = _sql(rc.CURRENT_SQL)
LATEST_SQL = _sql(rc.LATEST_SQL)
BY_STATE_SQL = _sql(rc.BY_STATE_SQL)
NEXT_QUEUED_SQL = _sql(rc.NEXT_QUEUED_SQL)
RUNNING_COUNT_SQL = _sql(rc.RUNNING_COUNT_SQL)
KEEP_FILES_SQL = _sql(rc.KEEP_FILES_SQL)
SUPERSEDE_SQL = _sql(rc.SUPERSEDE_SQL)
SUCCEED_SQL = _sql(rc.SUCCEED_SQL)
CLAIM_SQL = _sql(rc.CLAIM_SQL)
MAP_DELETE_CANCEL_SQL = _sql(rc.MAP_DELETE_CANCEL_SQL)


class PgRepo(rc.PgRepo):
    """map_meshes on the API's PostgresDatabase: reconstruction's repository with its
    statements pointed at this table and its own dispatcher lock."""

    async def insert(self, job_id: str, map_name: str, params: Mapping[str, Any],
                     requested_by: Optional[str]) -> Job:
        import psycopg
        try:
            async with self._db.connection() as conn:
                await conn.execute(INSERT_SQL, (uuid.UUID(job_id), map_name, requested_by,
                                                json.dumps(dict(params))))
        except psycopg.errors.UniqueViolation:
            raise JobActive(await self.active(map_name))
        return await self.get(job_id)

    async def get(self, job_id: str) -> Optional[Job]:
        try:
            key = uuid.UUID(str(job_id))
        except ValueError:
            return None
        return await self._one(GET_SQL, (key,))

    async def active(self, map_name: str) -> Optional[Job]:
        return await self._one(ACTIVE_SQL, (map_name,))

    async def current(self, map_name: str) -> Optional[Job]:
        return await self._one(CURRENT_SQL, (map_name,))

    async def latest(self, map_name: str) -> Optional[Job]:
        return await self._one(LATEST_SQL, (map_name,))

    async def by_state(self, state: str) -> List[Job]:
        return await self._all(BY_STATE_SQL, (state,))

    async def next_queued(self, now: datetime.datetime) -> Optional[Job]:
        return await self._one(NEXT_QUEUED_SQL, (now,))

    async def running_count(self) -> int:
        async with self._db.connection() as conn:
            cur = await conn.execute(RUNNING_COUNT_SQL)
            return int((await cur.fetchone())[0])

    async def keep_files(self) -> set:
        async with self._db.connection() as conn:
            cur = await conn.execute(KEEP_FILES_SQL)
            return {str(r[0]) for r in await cur.fetchall()}

    async def update(self, job_id: str, fields: Mapping[str, Any], *,
                     states: Sequence[str] = ACTIVE, attempts: Optional[int] = None,
                     event: Optional[Event] = None) -> bool:
        bad = set(fields) - rc.UPDATABLE
        if bad:
            raise ValueError(f"not updatable: {sorted(bad)}")
        sets = ", ".join(f"{c} = %s" + ("::jsonb" if c in rc.JSON_COLUMNS else "")
                         for c in fields)
        sql = f"UPDATE {TABLE} SET {sets} WHERE job_id = %s AND state = ANY(%s)"
        params: List[Any] = [_param(c, v) for c, v in fields.items()]
        params += [uuid.UUID(job_id), list(states)]
        if attempts is not None:
            sql += " AND attempts = %s"
            params.append(attempts)
        async with self._db.connection() as conn:
            cur = await conn.execute(sql, params)
            changed = cur.rowcount == 1
            if changed and event is not None:
                await rc._emit_safe(conn, event)
        return changed

    async def claim_finalize(self, job_id: str, attempt: int, now: datetime.datetime,
                             stale_before: datetime.datetime) -> bool:
        async with self._db.connection() as conn:
            cur = await conn.execute(CLAIM_SQL, (now, uuid.UUID(job_id), attempt, stale_before))
            return cur.rowcount == 1

    async def commit_success(self, job: Job, attempt: int, result: Mapping[str, Any],
                             artifacts: Mapping[str, Any], now: datetime.datetime,
                             event: Event) -> Tuple[str, List[Dict[str, Any]]]:
        async with self._db.connection() as conn:
            cur = await conn.execute(rc.MAP_LOCK_SQL, (job.map_name,))
            row = await cur.fetchone()
            if row is None or row[0] == "DELETING":
                return "map_deleting", []
            cur = await conn.execute(GET_SQL + " FOR UPDATE", (uuid.UUID(job.job_id),))
            mine = await cur.fetchone()
            if mine is None or Job.from_row(mine).state != RUNNING \
                    or Job.from_row(mine).attempts != attempt:
                return "gone", []
            cur = await conn.execute(SUPERSEDE_SQL, (job.map_name, uuid.UUID(job.job_id)))
            old = [r[1] or {} for r in await cur.fetchall()]
            cur = await conn.execute(SUCCEED_SQL, (json.dumps(dict(result)),
                                                   json.dumps(dict(artifacts)), now, now,
                                                   uuid.UUID(job.job_id), attempt))
            if cur.rowcount != 1:
                raise RuntimeError("succeed update matched no row")
            await rc._emit_safe(conn, event)
        return "ok", old

    async def supersede_current(self, map_name: str) -> List[Dict[str, Any]]:
        async with self._db.connection() as conn:
            cur = await conn.execute(SUPERSEDE_SQL, (map_name, uuid.UUID(int=0)))
            return [r[1] or {} for r in await cur.fetchall()]

    async def leader_connection(self) -> Any:
        conn = await self._db.dedicated_connection()
        try:
            cur = await conn.execute("SELECT pg_try_advisory_lock(%s)",
                                     (advisory_lock_key(LOCK_NAME),))
            if (await cur.fetchone())[0]:
                return conn
        except Exception:  # noqa: BLE001
            await conn.close()
            raise
        await conn.close()
        return None


async def mark_map_deleting(cursor: Any, map_id: str) -> List[Tuple[str, str, int]]:
    """In MapDeleter.request()'s transaction: cancel the map's active mesh job. Returns
    [(job_id, old_state, attempts)] for after_map_delete_marked()."""
    await cursor.execute(MAP_DELETE_CANCEL_SQL, (map_id, json.dumps(_error(
        "map_deleting", "the map is being deleted"))))
    return [(str(r[0]), r[1], int(r[2] or 0)) for r in await cursor.fetchall()]


async def _no_top_view(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    raise RuntimeError("the mesh service has no top view")


class MeshGateway(rc.ReconstructionGateway):
    """Everything the routes, callbacks and dispatcher do (see the module docstring). Takes the
    same collaborators as ReconstructionGateway: `repo` (PgRepo; tests: a fake), `depth_nodes`,
    `objects`, `presigner`, `client`, `config` (MeshConfig)."""

    def __init__(self, repo: Any, depth_nodes: Callable[[str], Tuple[int, List[Dict]]],
                 objects: Any, presigner: Any, client: Optional[MeshClient],
                 config: MeshConfig, **kwargs: Any):
        super().__init__(repo, depth_nodes, objects, presigner, client, config,
                         derive_top_view=_no_top_view, **kwargs)

    # --- helpers --------------------------------------------------------------------------------
    def _require_configured(self) -> None:
        if not self.config.configured or self.client is None:
            raise _err(503, "not_configured",
                       "The mesh service is not configured "
                       "(MESH_SERVICE_URL, _SERVICE_KEY, _CALLBACK_SECRET)")

    async def _end(self, job: Job, state: str, error: Mapping[str, Any], *,
                   attempts: Optional[int] = None, states: Sequence[str] = ACTIVE) -> bool:
        """Job -> failed | cancelled with `error`, MAP.MESH_FAILED, staging removed."""
        now = self._now()
        changed = await self.repo.update(
            job.job_id, {"state": state, "error": dict(error), "finished_at": now,
                         "next_try_at": None},
            states=states, attempts=attempts, event=failed_event(job, state, error, now))
        if changed:
            logger.info("Job %s (map %s): %s (%s)", job.job_id, job.map_name, state,
                        error.get("reason"))
            await self._cleanup_staging(job.job_id)
        return changed

    # --- client routes --------------------------------------------------------------------------
    async def start(self, map_name: str, body: Optional[Mapping[str, Any]],
                    requested_by: Optional[str] = None) -> Dict[str, Any]:
        self._require_configured()
        params = job_params(self.config, body)
        await self._map(map_name)
        await self._resolve_person_removal(map_name, params)
        existing = await self.repo.active(map_name)
        if existing is not None:
            raise _err(409, "job_active", "A mesh job of this map is already active",
                       job=existing.view())
        _total, frames = await self._frames(map_name)
        if not frames:
            raise _err(409, "no_depth", f"Map '{map_name}' has no nodes with depth "
                       "(recorded before depth capture)")
        if len(frames) > rc.MAX_NODES:
            raise _err(422, "too_many_nodes",
                       f"{len(frames)} nodes with depth; at most {rc.MAX_NODES} per job")
        sessions = session_counts(frames)
        if params["session"] != "auto" and params["session"] not in sessions:
            raise _err(422, "unknown_session",
                       f"Map '{map_name}' has no nodes with depth of session "
                       f"'{params['session']}'", sessions=sessions)
        try:
            job = await self.repo.insert(str(uuid.uuid4()), map_name, params, requested_by)
        except JobActive as exc:
            raise _err(409, "job_active", "A mesh job of this map is already active",
                       job=exc.job.view() if exc.job else None)
        logger.info("Job %s (map %s) queued, params %s", job.job_id, map_name, params)
        self.wake()
        return job.view()

    async def status(self, map_name: str) -> Dict[str, Any]:
        await self._map(map_name)
        current = await self.repo.current(map_name)
        active = await self.repo.active(map_name)
        job = active
        if job is None:
            latest = await self.repo.latest(map_name)
            if latest is not None and latest.state in (FAILED, CANCELLED) and (
                    current is None or _ts(latest.requested_at) > _ts(current.requested_at)):
                job = latest
        view = None
        if current is not None:
            stale, reason = None, None
            try:
                _total, frames = await self._frames_cached(map_name)
                stale = input_digest(frames) != (current.inputs or {}).get("digest")
                reason = stale_reason((current.inputs or {}).get("nodes"), frames)
            except Exception as exc:  # noqa: BLE001 - ArangoDB down: stale unknown
                logger.warning("Map %s: stale check failed: %s", map_name, exc)
            view = mesh_view(current, stale, reason)
        return {"map_name": map_name, "configured": self.config.configured,
                "mesh": view, "job": job.view() if job else None}

    async def cancel(self, map_name: str) -> Dict[str, Any]:
        await self._map(map_name)
        job = await self.repo.active(map_name)
        if job is None:
            raise _err(404, "no_active_job", f"Map '{map_name}' has no active mesh job")
        return (await self._cancel_job(job)).view()

    async def open_file(self, map_name: str, file: str) -> Dict[str, Any]:
        """Where the current result's `file` is: {bucket, key, bytes, content_type, job_id}."""
        if file not in FILE_BY_NAME:
            raise _err(404, "file_not_found", f"No mesh file '{file}'")
        await self._map(map_name)
        current = await self.repo.current(map_name)
        if current is None or not current.artifacts:
            raise _err(404, "no_mesh", f"Map '{map_name}' has no mesh")
        name, content_type = FILE_BY_NAME[file]
        info = (current.artifacts.get("files") or {}).get(name)
        if not info:
            raise _err(404, "file_not_found", f"No mesh file '{file}'")
        return {"bucket": current.artifacts["bucket"], "key": info["key"],
                "bytes": info.get("bytes"), "content_type": info.get("content_type")
                or content_type, "job_id": current.job_id, "file": file}

    # --- callbacks ------------------------------------------------------------------------------
    def authorize(self, job_id: str, authorization: Optional[str]) -> None:
        if not self.config.callback_secret:
            raise _err(503, "not_configured", "MESH_CALLBACK_SECRET is not set")
        super().authorize(job_id, authorization)

    async def _apply_progress(self, job: Job, body: Mapping[str, Any]) -> None:
        now = self._now()
        fields: Dict[str, Any] = {"last_contact_at": now}
        if body.get("stage") is not None:
            fields["stage"] = str(body["stage"])[:64]
        progress = _float(body.get("progress"))
        if progress is not None:
            fields["progress"] = min(1.0, max(0.0, progress))
        for key in ("frames_done", "frames_total"):
            if _int(body.get(key)) is not None:
                fields[key] = _int(body.get(key))
        event = None
        if job.started_at is None and body.get("stage") not in (None, QUEUED):
            fields["started_at"] = now
            event = started_event(job, now, job.attempts)
        await self.repo.update(job.job_id, fields, states=(RUNNING,), attempts=job.attempts,
                               event=event)

    def _verify(self, job_id: str, attempt: int,
                outputs: Any) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        """(why the staged outputs are unusable, None) or (None, meta.json) (blocking)."""
        if not isinstance(outputs, Mapping):
            return "finish without outputs", None
        bucket = self.config.staging_bucket
        sizes = {}
        for name, (file, _ct) in FILES.items():
            out = outputs.get(name)
            if not isinstance(out, Mapping) or _int(out.get("bytes")) is None:
                return f"output {name} not reported", None
            size = self.objects.size(bucket, staging_key(job_id, attempt, file))
            if size is None:
                return f"{file} missing in staging", None
            if size != int(out["bytes"]):
                return f"{file}: {size} bytes staged, {out['bytes']} reported", None
            sizes[name] = size
        raw = self.objects.read(bucket, staging_key(job_id, attempt, "meta.json"),
                                rc.MAX_META_BYTES)
        if len(raw) > rc.MAX_META_BYTES:
            return "meta.json too large", None
        try:
            meta = json.loads(raw)
        except ValueError as exc:
            return f"meta.json does not parse: {exc}", None
        if not isinstance(meta, dict):
            return "meta.json is not an object", None
        head = self.objects.read(bucket, staging_key(job_id, attempt, "model.glb"),
                                 GLB_HEADER.size - 1)
        problem = glb_problem(head, sizes["mesh"])
        if problem:
            return f"model.glb: {problem}", None
        return None, meta

    async def _finalize_steps(self, job: Job, attempt: int, result: Dict[str, Any],
                              outputs: Any, meta: Dict[str, Any]) -> None:
        """Copy the two files to the map bucket, commit (module docstring)."""
        cfg = self.config
        bucket = self.objects.bucket_for(job.map_name)
        prefix = result_prefix(job.job_id)
        files: Dict[str, Dict[str, Any]] = {}

        try:
            if not await asyncio.to_thread(self.objects.bucket_exists, bucket):
                raise LookupError("the map bucket does not exist")
            for name, (file, ct) in FILES.items():
                await asyncio.to_thread(self.objects.copy, cfg.staging_bucket,
                                        staging_key(job.job_id, attempt, file), bucket,
                                        prefix + file)
                files[name] = {"key": prefix + file, "bytes": int(outputs[name]["bytes"]),
                               "sha256": outputs[name].get("sha256"), "content_type": ct}
        except LookupError as exc:
            await self._end(job, FAILED, _error("map_deleting", str(exc), FINALIZING),
                            states=(RUNNING,), attempts=attempt)
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("Job %s: storing the outputs failed", job.job_id)
            await self._remove_result({"bucket": bucket, "prefix": prefix})
            await self._end(job, FAILED, _error("error", f"storing the outputs: {exc}",
                                                FINALIZING), states=(RUNNING,), attempts=attempt)
            return
        artifacts = {"bucket": bucket, "prefix": prefix, "files": files}
        mine = await self.repo.get(job.job_id)
        if mine is not None and mine.cancel_requested and mine.state == RUNNING:
            await self._remove_result(artifacts)
            await self._end(job, CANCELLED, _error("cancelled", "cancelled by the user"),
                            states=(RUNNING,), attempts=attempt)
            return
        now = self._now()
        outcome, old = await self.repo.commit_success(job, attempt, result, artifacts, now,
                                                      finished_event(job, result, now))
        if outcome != "ok":
            mine = await self.repo.get(job.job_id)
            if not (mine is not None and mine.state == SUCCEEDED and mine.attempts == attempt):
                await self._remove_result(artifacts)  # (not when another finalizer won)
            if outcome == "map_deleting":
                await self._end(job, CANCELLED, _error("map_deleting",
                                                       "the map is being deleted", FINALIZING),
                                states=(RUNNING,), attempts=attempt)
            return
        logger.info("Job %s (map %s) succeeded: session %s, %s triangles", job.job_id,
                    job.map_name, result.get("session"), result.get("triangles"))
        for artifacts_old in old:
            await self._remove_result(artifacts_old)
        await self._cleanup_staging(job.job_id)
        self._stale_cache.pop(job.map_name, None)

    # --- dispatcher -----------------------------------------------------------------------------
    async def _dispatch(self, job: Job) -> bool:
        """Build a fresh manifest for `job` and POST it. True when the service took it."""
        row = await self.repo.map_row(job.map_name)
        if row is None or row[0] == "DELETING":
            await self._end(job, CANCELLED, _error("map_deleting", "the map is being deleted"),
                            states=(QUEUED,))
            return False
        spec = row[1] or {}
        try:
            total, frames = await self._frames(job.map_name)
        except Exception as exc:  # noqa: BLE001 - ArangoDB down: try again later
            await self._backoff(job, f"reading the map's nodes failed: {exc}")
            return False
        if not frames:
            await self._end(job, FAILED, _error("no_depth", "the map has no nodes with depth"),
                            states=(QUEUED,))
            return False
        if len(frames) > rc.MAX_NODES:
            await self._end(job, FAILED, _error(
                "rejected", f"{len(frames)} nodes with depth; at most {rc.MAX_NODES}"),
                states=(QUEUED,))
            return False
        sessions = session_counts(frames)
        wanted = job.params.get("session") or "auto"
        if wanted != "auto" and wanted not in sessions:
            await self._end(job, FAILED, _error(
                "unknown_session", f"the map has no nodes with depth of session '{wanted}'"),
                states=(QUEUED,))
            return False
        cap = min(int(job.params.get("max_frames") or self.config.max_frames),
                  self.config.max_frames)
        used, per_session = select_frames(frames, job.params, cap)
        attempt = job.attempts + 1
        now = self._now()
        try:
            manifest = await asyncio.to_thread(self._manifest, job, attempt, spec, used, now)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Job %s: building the manifest failed", job.job_id)
            await self._backoff(job, f"building the manifest failed: {exc}")
            return False
        try:
            status, body = await self.client.submit(manifest)
        except rc.ServiceUnreachable as exc:
            await self._backoff(job, str(exc))
            return False
        if status in (200, 202):
            n_frames = sum(len(f.cameras) for f in used)
            inputs = {"nodes_total": total, "nodes_with_depth": len(frames),
                      "nodes_used": len(used), "sessions": per_session, "frames": n_frames,
                      "digest": input_digest(frames), "nodes": digest_nodes(frames),
                      "person_removal": pr.summarize(used, bool(job.params.get("person_removal")))}
            changed = await self.repo.update(job.job_id, {
                "state": RUNNING, "attempts": attempt, "dispatched_at": now,
                "last_contact_at": now, "next_try_at": None, "stage": QUEUED, "progress": 0.0,
                "frames_done": 0, "frames_total": n_frames, "inputs": inputs,
                "error": None}, states=(QUEUED,), attempts=job.attempts)
            if not changed:  # cancelled while we were sending
                await self._service_cancel(job.job_id)
                return False
            logger.info("Job %s (map %s) sent, attempt %d, %d frames of %d session(s)",
                        job.job_id, job.map_name, attempt, n_frames, len(per_session))
            return True
        detail = body.get("detail") if isinstance(body, Mapping) else None
        message = f"HTTP {status}: {json.dumps(detail)[:500] if detail else ''}"
        if status == 409:  # the service saw a higher attempt: skip past it
            await self.repo.update(job.job_id, {"attempts": attempt, "next_try_at": now},
                                   states=(QUEUED,), attempts=job.attempts)
            return False
        if status == 429 or status >= 500:
            await self._backoff(job, message)
            return False
        await self._end(job, FAILED, _error("rejected", message), states=(QUEUED,))
        return False

    def _manifest(self, job: Job, attempt: int, spec: Mapping[str, Any],
                  frames: Sequence[Frame], now: datetime.datetime) -> Dict[str, Any]:
        cfg = self.config
        return build_manifest(
            job_id=job.job_id, attempt=attempt, map_name=job.map_name,
            map_type=spec.get("type"), crs=map_crs(spec),
            params={**job.params, "max_frames": min(int(job.params.get("max_frames")
                                                         or cfg.max_frames), cfg.max_frames)},
            frames=frames,
            map_bucket=self.objects.bucket_for(job.map_name),
            staging_bucket=cfg.staging_bucket,
            presign_get=lambda b, k: self.presigner.get(b, k, cfg.url_expiry_s),
            presign_put=lambda b, k: self.presigner.put(b, k, cfg.url_expiry_s),
            callback_base_url=cfg.callback_base_url,
            token=callback_token(cfg.callback_secret, job.job_id), now=now,
            expiry_s=cfg.url_expiry_s, person_removal=bool(job.params.get("person_removal")))

    # --- lifecycle ------------------------------------------------------------------------------
    async def prepare(self) -> None:
        """Leader start: the staging bucket and its expiry rule; remove every `mesh/{job}/`
        prefix whose job is neither running nor succeeded. Never raises."""
        try:
            await asyncio.to_thread(self.objects.ensure_staging, self.config.staging_bucket)
            self._staging_ready = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Staging bucket %s not ready: %s", self.config.staging_bucket, exc)
        try:
            keep = await self.repo.keep_files()
            swept = await asyncio.to_thread(self.objects.result_jobs, RESULT_PREFIX)
            for bucket, job_id in swept:
                if job_id not in keep:
                    n = await asyncio.to_thread(self.objects.remove_prefix, bucket,
                                                result_prefix(job_id))
                    logger.info("Removed %d orphaned mesh file(s) %s/%s", n, bucket,
                                result_prefix(job_id))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Mesh orphan sweep failed: %s", exc)

    def start_dispatcher(self) -> None:
        if not self.config.configured or self._task is not None:
            return
        try:
            self._wake = asyncio.Event()
            self._task = asyncio.get_running_loop().create_task(
                self._loop(), name="api.mesh.dispatcher")
        except Exception:  # noqa: BLE001
            logger.exception("Could not start the mesh dispatcher")

    async def _loop(self) -> None:
        while True:
            conn = None
            try:
                conn = await self.repo.leader_connection()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("Mesh dispatcher: no lock connection: %s", exc)
            if conn is None:
                await asyncio.sleep(rc.TICK_S)
                continue
            try:
                logger.info("Mesh dispatcher running (service %s)", self.config.service_url)
                await self.prepare()
                while not getattr(conn, "closed", False):
                    if not self._staging_ready:
                        await self.prepare()
                    try:
                        await self.tick()
                    except asyncio.CancelledError:
                        raise
                    except Exception:  # noqa: BLE001
                        logger.exception("Mesh dispatcher pass failed")
                    try:
                        await asyncio.wait_for(self._wake.wait(), rc.TICK_S)
                    except asyncio.TimeoutError:
                        pass
                    self._wake.clear()
            finally:
                try:
                    await conn.close()
                except Exception:  # noqa: BLE001
                    pass

    # --- map delete hook (after the delete's transaction committed) -----------------------------
    async def after_map_delete_marked(self, map_id: str,
                                      marked: Sequence[Tuple[str, str, int]]) -> None:
        """MAP.MESH_FAILED (map_deleting) for each job the delete cancelled and a
        best-effort cancel at the service. Never raises."""
        now = self._now()
        for job_id, old_state, attempts in marked or ():
            try:
                await self.repo.emit(failed_event(
                    Job(job_id, map_id, CANCELLED), CANCELLED,
                    _error("map_deleting", "the map is being deleted"), now))
                if old_state == RUNNING or attempts:
                    await self._service_cancel(job_id)
            except Exception:  # noqa: BLE001
                logger.exception("Job %s: map delete follow-up failed", job_id)
        self._stale_cache.pop(map_id, None)


def create_gateway(db: Any, graph_db: Any, image_db: Any,
                   minio_access_key: Optional[str], minio_secret_key: Optional[str],
                   config: Optional[MeshConfig] = None) -> MeshGateway:
    """The API's gateway (server.py). Without the service configured it still serves GET and
    the files; POST is 503 and no dispatcher runs."""
    config = config or MeshConfig.from_env()
    presigner = Presigner(config.minio_endpoint, minio_access_key or "", minio_secret_key or "",
                          config.minio_secure)
    client = (MeshClient(config.service_url, config.service_key)
              if config.configured else None)
    return MeshGateway(PgRepo(db), rc.depth_node_reader(graph_db),
                           ObjectStore(image_db.client, image_db._bucket_name),
                           presigner, client, config)
