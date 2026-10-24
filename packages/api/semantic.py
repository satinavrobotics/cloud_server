"""Semantic layer gateway (docs/semantic/design.md, contract cloud_semantic/CONTRACT.md).

The same shape as the 3D reconstruction gateway (packages/api/reconstruction.py, whose module
docstring describes the job states, the dispatcher, the callbacks and the finish steps) and it
reuses that module: the job row, the MinIO helpers, the callback token, the stale digest, the
dispatcher and the callback handlers are inherited; this module only changes what differs.

    POST   /api/v1/maps/{map}/semantic              start()     202 + the job
    GET    /api/v1/maps/{map}/semantic              status()    result (+ stale) and job
    POST   /api/v1/maps/{map}/semantic/cancel       cancel()
    DELETE /api/v1/maps/{map}/semantic              delete()    204
    GET    /api/v1/maps/{map}/semantic/files/{name} open_file() (streamed by main.py)
    POST   /internal/semantic/jobs/{id}/progress|finish|fail

Differences from reconstruction:
  * Independent of it: a semantic job never waits for, or needs, a reconstruction.
  * The service delivers four files (instances.json, labels.bin, dynamic.zip, meta.json). Finish
    verifies them in staging (sizes; instances.json / meta.json parse; labels.bin header and
    size; dynamic.zip magic), copies them server-side to `map-{id}/semantic/{job}/` and commits.
    Nothing is derived here.
  * Table `map_semantics`, dispatcher lock `semantic_dispatcher`, events MAP.SEMANTIC_*.
  * Nodes are thinned here: every `every`-th node (capture order) goes into the manifest, with
    `every` chosen so a job has at most SEMANTIC_MAX_FRAMES camera frames unless the request sets
    it. The manifest then says `every: 1`.
  * Limitation: like reconstruction, only nodes with a `depth.{camera}` record are used, also for
    depth_source "da3" (which does not need the depth values; a node without the record is skipped).
  * Open vocabulary is not available yet (OPEN_VOCABULARY_AVAILABLE): POST answers 422.
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

from packages.api import reconstruction as rc
from packages.api.entrypoint import advisory_lock_key
from packages.api.reconstruction import (ACTIVE, CANCELLED, FAILED, FINALIZING, QUEUED, RUNNING,
                                         SUCCEEDED, Frame, Job, JobActive, ObjectStore,
                                         Presigner, _err, _error, _float, _int, _iso, _param,
                                         _ts, _utcnow, callback_token, digest_nodes,
                                         input_digest, map_crs, rgb_key, depth_key,
                                         staging_key, stale_reason)
from packages.api.semantic_client import SemanticClient
from packages.events.codes import EventCode, Source
from packages.events.emit import Event

logger = logging.getLogger("ApiDelegationService.semantic")

# --- constants ---------------------------------------------------------------------------------

# name -> (file, content type): every file of a result, as the client reads them
FILES: Dict[str, Tuple[str, str]] = {
    "instances": ("instances.json", "application/json"),
    "labels": ("labels.bin", "application/octet-stream"),
    "dynamic": ("dynamic.zip", "application/zip"),
    "meta": ("meta.json", "application/json"),
}
FILE_BY_NAME = {f: (k, ct) for k, (f, ct) in FILES.items()}
RESULT_PREFIX = "semantic/"
LOCK_NAME = "semantic_dispatcher"
TABLE = "map_semantics"
MANIFEST_VERSION = 1

OPEN_VOCABULARY_AVAILABLE = False
VOCABULARIES = ("closed", "open")
DEPTH_SOURCES = ("sensor", "da3")
PARAM_RANGES = {"every": (1, 50), "max_depth_m": (0.5, 65.0), "min_voxels": (5, 500)}

MAX_INSTANCES_BYTES = 32 << 20
MAX_LABEL_VOXELS = 5_000_000
LABELS_MAGIC = b"SEML"
LABELS_HEADER = struct.Struct("<4sIIf")          # magic, version, N, voxel_m  (16 bytes)
LABELS_BYTES_PER_VOXEL = 6 + 1 + 2               # int16[3] cm, uint8 class, uint16 instance
ZIP_MAGICS = (b"PK\x03\x04", b"PK\x05\x06")      # a zip with entries / an empty zip


# --- config ------------------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class SemConfig(rc.ReconConfig):
    staging_bucket: str = "sem-staging"
    max_frames: int = 400
    min_voxels: int = 15

    @classmethod
    def from_env(cls) -> "SemConfig":
        from packages import config as c
        return cls(
            service_url=c.SEMANTIC_SERVICE_URL, service_key=c.SEMANTIC_SERVICE_KEY,
            callback_secret=c.SEMANTIC_CALLBACK_SECRET,
            callback_base_url=c.SEMANTIC_CALLBACK_BASE_URL,
            minio_endpoint=c.SEMANTIC_MINIO_ENDPOINT, minio_secure=c.SEMANTIC_MINIO_SECURE,
            staging_bucket=c.SEMANTIC_STAGING_BUCKET, url_expiry_s=c.SEMANTIC_URL_EXPIRY_S,
            job_timeout_s=c.SEMANTIC_JOB_TIMEOUT_S, queue_timeout_s=c.SEMANTIC_QUEUE_TIMEOUT_S,
            max_inflight=max(1, c.SEMANTIC_MAX_INFLIGHT), max_depth_m=c.SEMANTIC_MAX_DEPTH_M,
            max_frames=max(1, c.SEMANTIC_MAX_FRAMES))

    def default_params(self) -> Dict[str, Any]:
        return {"vocabulary": "closed", "depth_source": "sensor",
                "max_depth_m": self.max_depth_m, "min_voxels": self.min_voxels}


# --- pure helpers ------------------------------------------------------------------------------

class StartBody(pydantic.BaseModel):
    """POST body (all optional): the job parameters a user may override."""
    vocabulary: Optional[str] = None
    depth_source: Optional[str] = None
    every: Optional[int] = None
    max_depth_m: Optional[float] = None
    min_voxels: Optional[int] = None

    class Config:
        extra = pydantic.Extra.forbid

    @pydantic.validator("vocabulary")
    def _vocabulary(cls, value):  # noqa: N805 - pydantic v1 validator
        if value is None:
            return value
        if value not in VOCABULARIES:
            raise ValueError(f"must be one of {', '.join(VOCABULARIES)}")
        if value == "open" and not OPEN_VOCABULARY_AVAILABLE:
            raise ValueError("open vocabulary is not available yet")
        return value

    @pydantic.validator("depth_source")
    def _depth_source(cls, value):  # noqa: N805
        if value is not None and value not in DEPTH_SOURCES:
            raise ValueError(f"must be one of {', '.join(DEPTH_SOURCES)}")
        return value

    @pydantic.validator("every", "max_depth_m", "min_voxels")
    def _in_range(cls, value, field):  # noqa: N805
        if value is None:
            return value
        lo, hi = PARAM_RANGES[field.name]
        if not (lo <= value <= hi):
            raise ValueError(f"must be between {lo} and {hi}")
        return value


def job_params(config: SemConfig, body: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """The job's params: the gateway's defaults and the body's overrides (`every` only when the
    body sets it: otherwise it is chosen at dispatch). 422 on a bad body."""
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
    return params


def choose_every(frames: Sequence[Frame], params: Mapping[str, Any], max_frames: int) -> int:
    """The node stride: the request's `every`, else the smallest that keeps the camera frames at
    most `max_frames`."""
    if params.get("every"):
        return int(params["every"])
    total = sum(len(f.cameras) for f in frames)
    return max(1, math.ceil(total / max(1, max_frames)))


def build_manifest(*, job_id: str, attempt: int, map_name: str, map_type: Optional[str],
                   crs: Optional[Dict[str, Any]], params: Mapping[str, Any],
                   frames: Sequence[Frame], map_bucket: str, staging_bucket: str,
                   presign_get: Callable[[str, str], str],
                   presign_put: Callable[[str, str], str], callback_base_url: str,
                   token: str, now: datetime.datetime, expiry_s: int) -> Dict[str, Any]:
    """The POST /jobs body (CONTRACT.md). `frames` are already thinned: `every` is 1."""
    nodes = []
    for f in frames:
        cameras = []
        for name in sorted(f.cameras):
            rec = f.cameras[name]
            cam = {"name": name, "params": dict(rec["camera"]),
                   "depth_scale": float(rec.get("depth_scale") or 0.001),
                   "rgb_url": presign_get(map_bucket, rgb_key(f.node_id, name)),
                   "depth_url": presign_get(map_bucket, depth_key(f.node_id, name))}
            if isinstance(rec.get("pose3d_map"), Mapping):
                cam["pose3d"] = dict(rec["pose3d_map"])
            cameras.append(cam)
        nodes.append({"node_id": f.node_id, "pose": {"x": f.x, "y": f.y, "yaw": f.yaw},
                      "cameras": cameras})
    send = {k: params[k] for k in ("vocabulary", "depth_source", "max_depth_m", "min_voxels")
            if params.get(k) is not None}
    send["every"] = 1
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
    return f"{base_url.rstrip('/')}/internal/semantic/jobs/{job_id}"


def file_url(map_name: str, file: str, job_id: str) -> str:
    return (f"/api/v1/maps/{urllib.parse.quote(map_name, safe='')}/semantic/files/"
            f"{file}?v={job_id}")


def semantic_view(job: Job, stale: Optional[bool], reason: Optional[Dict[str, int]]
                  ) -> Dict[str, Any]:
    """The map's current result (`semantic` in the status)."""
    result, inputs = job.result or {}, job.inputs or {}
    files = {}
    for name, info in ((job.artifacts or {}).get("files") or {}).items():
        file = FILES.get(name, (name, None))[0]
        files[name] = {"name": file, "bytes": info.get("bytes"),
                       "url": file_url(job.map_name, file, job.job_id)}
    return {"job_id": job.job_id, "finished_at": _iso(job.finished_at),
            "params": {k: job.params.get(k) for k in ("vocabulary", "depth_source", "every",
                                                      "max_depth_m", "min_voxels")},
            "instances": result.get("instances"), "label_voxels": result.get("label_voxels"),
            "nodes_with_depth": inputs.get("nodes_with_depth"),
            "nodes_used": inputs.get("nodes_used"), "every": inputs.get("every"),
            "frames_total": result.get("frames_total", inputs.get("frames")),
            "frames_used": result.get("frames_used"),
            "dynamic_frames": result.get("dynamic_frames"),
            "depth_source": result.get("depth_source"), "vocabulary": result.get("vocabulary"),
            "duration_s": result.get("duration_s"),
            "stale": stale, "stale_reason": reason if stale else None, "files": files}


def _discriminator(map_name: str, job_id: str, state: str) -> str:
    return f"map:{map_name}:semantic:{job_id}:{state}"


def started_event(job: Job, now: datetime.datetime, attempt: int) -> Event:
    return Event(EventCode.MAP_SEMANTIC_STARTED, now, source=Source.SEMANTIC,
                 discriminator=_discriminator(job.map_name, job.job_id, "started"),
                 payload={"map_name": job.map_name, "job_id": job.job_id,
                          "params": job.params, "attempt": attempt,
                          "nodes_with_depth": (job.inputs or {}).get("nodes_with_depth")})


def finished_event(job: Job, result: Mapping[str, Any], now: datetime.datetime) -> Event:
    return Event(EventCode.MAP_SEMANTIC_FINISHED, now, source=Source.SEMANTIC,
                 discriminator=_discriminator(job.map_name, job.job_id, SUCCEEDED),
                 payload={"map_name": job.map_name, "job_id": job.job_id,
                          "instances": _int(result.get("instances")),
                          "label_voxels": _int(result.get("label_voxels")),
                          "frames_used": _int(result.get("frames_used")),
                          "dynamic_frames": _int(result.get("dynamic_frames")),
                          "depth_source": (str(result["depth_source"])
                                           if result.get("depth_source") is not None else None),
                          "duration_s": _float(result.get("duration_s"))})


def failed_event(job: Job, state: str, error: Mapping[str, Any],
                 now: datetime.datetime) -> Event:
    return Event(EventCode.MAP_SEMANTIC_FAILED, now, source=Source.SEMANTIC,
                 discriminator=_discriminator(job.map_name, job.job_id, state),
                 payload={"map_name": job.map_name, "job_id": job.job_id,
                          "reason": str(error.get("reason") or "error"),
                          "stage": error.get("stage"), "message": error.get("message")})


# --- Postgres ----------------------------------------------------------------------------------

def _sql(statement: str) -> str:
    """A reconstruction statement on map_semantics (same columns, same indexes' semantics)."""
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
    """map_semantics on the API's PostgresDatabase: reconstruction's repository with its
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
    """In MapDeleter.request()'s transaction: cancel the map's active semantic job. Returns
    [(job_id, old_state, attempts)] for after_map_delete_marked()."""
    await cursor.execute(MAP_DELETE_CANCEL_SQL, (map_id, json.dumps(_error(
        "map_deleting", "the map is being deleted"))))
    return [(str(r[0]), r[1], int(r[2] or 0)) for r in await cursor.fetchall()]


# --- the gateway -------------------------------------------------------------------------------

async def _no_top_view(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    raise RuntimeError("the semantic layer has no top view")


class SemanticGateway(rc.ReconstructionGateway):
    """Everything the routes, callbacks and dispatcher do (see the module docstring). Takes the
    same collaborators as ReconstructionGateway: `repo` (PgRepo; tests: a fake), `depth_nodes`,
    `objects`, `presigner`, `client`, `config` (SemConfig)."""

    def __init__(self, repo: Any, depth_nodes: Callable[[str], Tuple[int, List[Dict]]],
                 objects: Any, presigner: Any, client: Optional[SemanticClient],
                 config: SemConfig, **kwargs: Any):
        super().__init__(repo, depth_nodes, objects, presigner, client, config,
                         derive_top_view=_no_top_view, **kwargs)

    # --- helpers --------------------------------------------------------------------------------
    def _require_configured(self) -> None:
        if not self.config.configured or self.client is None:
            raise _err(503, "not_configured",
                       "The semantic service is not configured "
                       "(SEMANTIC_SERVICE_URL, _SERVICE_KEY, _CALLBACK_SECRET)")

    async def _end(self, job: Job, state: str, error: Mapping[str, Any], *,
                   attempts: Optional[int] = None, states: Sequence[str] = ACTIVE) -> bool:
        """Job -> failed | cancelled with `error`, MAP.SEMANTIC_FAILED, staging removed."""
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
        existing = await self.repo.active(map_name)
        if existing is not None:
            raise _err(409, "job_active", "A semantic job of this map is already active",
                       job=existing.view())
        _total, frames = await self._frames(map_name)
        if not frames:
            raise _err(409, "no_depth", f"Map '{map_name}' has no nodes with depth "
                       "(recorded before depth capture)")
        if len(frames) > rc.MAX_NODES:
            raise _err(422, "too_many_nodes",
                       f"{len(frames)} nodes with depth; at most {rc.MAX_NODES} per job")
        try:
            job = await self.repo.insert(str(uuid.uuid4()), map_name, params, requested_by)
        except JobActive as exc:
            raise _err(409, "job_active", "A semantic job of this map is already active",
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
        sem = None
        if current is not None:
            stale, reason = None, None
            try:
                _total, frames = await self._frames_cached(map_name)
                stale = input_digest(frames) != (current.inputs or {}).get("digest")
                reason = stale_reason((current.inputs or {}).get("nodes"), frames)
            except Exception as exc:  # noqa: BLE001 - ArangoDB down: stale unknown
                logger.warning("Map %s: stale check failed: %s", map_name, exc)
            sem = semantic_view(current, stale, reason)
        return {"map_name": map_name, "configured": self.config.configured,
                "semantic": sem, "job": job.view() if job else None}

    async def cancel(self, map_name: str) -> Dict[str, Any]:
        await self._map(map_name)
        job = await self.repo.active(map_name)
        if job is None:
            raise _err(404, "no_active_job", f"Map '{map_name}' has no active semantic job")
        return (await self._cancel_job(job)).view()

    async def open_file(self, map_name: str, file: str) -> Dict[str, Any]:
        """Where the current result's `file` is: {bucket, key, bytes, content_type, job_id}."""
        if file not in FILE_BY_NAME:
            raise _err(404, "file_not_found", f"No semantic file '{file}'")
        await self._map(map_name)
        current = await self.repo.current(map_name)
        if current is None or not current.artifacts:
            raise _err(404, "no_semantic", f"Map '{map_name}' has no semantic layer")
        name, content_type = FILE_BY_NAME[file]
        info = (current.artifacts.get("files") or {}).get(name)
        if not info:
            raise _err(404, "file_not_found", f"No semantic file '{file}'")
        return {"bucket": current.artifacts["bucket"], "key": info["key"],
                "bytes": info.get("bytes"), "content_type": info.get("content_type")
                or content_type, "job_id": current.job_id, "file": file}

    # --- callbacks ------------------------------------------------------------------------------
    def authorize(self, job_id: str, authorization: Optional[str]) -> None:
        if not self.config.callback_secret:
            raise _err(503, "not_configured", "SEMANTIC_CALLBACK_SECRET is not set")
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
        if sizes["instances"] > MAX_INSTANCES_BYTES:
            return "instances.json too large", None
        raw = self.objects.read(bucket, staging_key(job_id, attempt, "instances.json"),
                                MAX_INSTANCES_BYTES)
        try:
            instances = json.loads(raw)
        except ValueError as exc:
            return f"instances.json does not parse: {exc}", None
        if not isinstance(instances, list):
            return "instances.json is not a list", None
        head = self.objects.read(bucket, staging_key(job_id, attempt, "labels.bin"),
                                 LABELS_HEADER.size - 1)
        if len(head) < LABELS_HEADER.size:
            return "labels.bin: shorter than its header", None
        magic, version, count, voxel = LABELS_HEADER.unpack(head[:LABELS_HEADER.size])
        if magic != LABELS_MAGIC or version != 1:
            return "labels.bin: not a SEML v1 file", None
        if count > MAX_LABEL_VOXELS or not voxel > 0:
            return f"labels.bin: implausible header (N={count}, voxel={voxel})", None
        want = LABELS_HEADER.size + count * LABELS_BYTES_PER_VOXEL
        if want != sizes["labels"]:
            return f"labels.bin: {sizes['labels']} bytes, its header says {want}", None
        magic = self.objects.read(bucket, staging_key(job_id, attempt, "dynamic.zip"), 3)
        if magic[:4] not in ZIP_MAGICS:
            return "dynamic.zip: not a zip file", None
        return None, meta

    async def _finalize_steps(self, job: Job, attempt: int, result: Dict[str, Any],
                              outputs: Any, meta: Dict[str, Any]) -> None:
        """Copy the four files to the map bucket, commit (module docstring)."""
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
        logger.info("Job %s (map %s) succeeded: %s instances, %s label voxels", job.job_id,
                    job.map_name, result.get("instances"), result.get("label_voxels"))
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
        every = choose_every(frames, job.params, self.config.max_frames)
        used = frames[::every]
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
                      "nodes_used": len(used), "every": every, "frames": n_frames,
                      "digest": input_digest(frames), "nodes": digest_nodes(frames)}
            changed = await self.repo.update(job.job_id, {
                "state": RUNNING, "attempts": attempt, "dispatched_at": now,
                "last_contact_at": now, "next_try_at": None, "stage": QUEUED, "progress": 0.0,
                "frames_done": 0, "frames_total": n_frames, "inputs": inputs,
                "error": None}, states=(QUEUED,), attempts=job.attempts)
            if not changed:  # cancelled while we were sending
                await self._service_cancel(job.job_id)
                return False
            logger.info("Job %s (map %s) sent, attempt %d, %d frames (every %d)", job.job_id,
                        job.map_name, attempt, n_frames, every)
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
            map_type=spec.get("type"), crs=map_crs(spec), params=job.params, frames=frames,
            map_bucket=self.objects.bucket_for(job.map_name),
            staging_bucket=cfg.staging_bucket,
            presign_get=lambda b, k: self.presigner.get(b, k, cfg.url_expiry_s),
            presign_put=lambda b, k: self.presigner.put(b, k, cfg.url_expiry_s),
            callback_base_url=cfg.callback_base_url,
            token=callback_token(cfg.callback_secret, job.job_id), now=now,
            expiry_s=cfg.url_expiry_s)

    # --- lifecycle ------------------------------------------------------------------------------
    async def prepare(self) -> None:
        """Leader start: the staging bucket and its expiry rule; remove every `semantic/{job}/`
        prefix whose job is neither running nor succeeded. Never raises."""
        try:
            await asyncio.to_thread(self.objects.ensure_staging, self.config.staging_bucket)
            self._staging_ready = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Staging bucket %s not ready: %s", self.config.staging_bucket, exc)
        try:
            keep = await self.repo.keep_files()
            for bucket, job_id in await asyncio.to_thread(self.objects.result_jobs):
                if job_id not in keep:
                    n = await asyncio.to_thread(self.objects.remove_prefix, bucket,
                                                result_prefix(job_id))
                    logger.info("Removed %d orphaned semantic file(s) %s/%s", n, bucket,
                                result_prefix(job_id))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Semantic orphan sweep failed: %s", exc)

    def start_dispatcher(self) -> None:
        if not self.config.configured or self._task is not None:
            return
        try:
            self._wake = asyncio.Event()
            self._task = asyncio.get_running_loop().create_task(
                self._loop(), name="api.semantic.dispatcher")
        except Exception:  # noqa: BLE001
            logger.exception("Could not start the semantic dispatcher")

    async def _loop(self) -> None:
        while True:
            conn = None
            try:
                conn = await self.repo.leader_connection()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("Semantic dispatcher: no lock connection: %s", exc)
            if conn is None:
                await asyncio.sleep(rc.TICK_S)
                continue
            try:
                logger.info("Semantic dispatcher running (service %s)", self.config.service_url)
                await self.prepare()
                while not getattr(conn, "closed", False):
                    if not self._staging_ready:
                        await self.prepare()
                    try:
                        await self.tick()
                    except asyncio.CancelledError:
                        raise
                    except Exception:  # noqa: BLE001
                        logger.exception("Semantic dispatcher pass failed")
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
        """MAP.SEMANTIC_FAILED (map_deleting) for each job the delete cancelled and a
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
                   config: Optional[SemConfig] = None) -> SemanticGateway:
    """The API's gateway (server.py). Without the service configured it still serves GET and
    the files; POST is 503 and no dispatcher runs."""
    config = config or SemConfig.from_env()
    presigner = Presigner(config.minio_endpoint, minio_access_key or "", minio_secret_key or "",
                          config.minio_secure)
    client = (SemanticClient(config.service_url, config.service_key)
              if config.configured else None)
    return SemanticGateway(PgRepo(db), rc.depth_node_reader(graph_db),
                           ObjectStore(image_db.client, image_db._bucket_name),
                           presigner, client, config)
