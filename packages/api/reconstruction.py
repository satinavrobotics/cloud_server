"""3D reconstruction gateway (docs/reconstruction/design.md §6, §8, §9; step R3).

The reconstruction itself runs in an external service (its own repo, any host; contract in
docs/reconstruction/handover.md). This module is cloud_server's side: it owns the job (Postgres
`map_reconstructions`), sends it with a manifest of presigned MinIO URLs, receives progress and
the result through HMAC-authenticated callbacks, verifies and commits the result, and serves it.

    POST   /api/v1/maps/{map}/reconstruction              start()     202 + the job
    GET    /api/v1/maps/{map}/reconstruction              status()    result (+ stale) and job
    POST   /api/v1/maps/{map}/reconstruction/cancel       cancel()
    DELETE /api/v1/maps/{map}/reconstruction              delete()    204
    GET    /api/v1/maps/{map}/reconstruction/files/{name} open_file() (streamed by main.py)
    POST   /internal/reconstruction/jobs/{id}/progress|finish|fail   on_progress/finish/fail

Job states (§8.2): queued -> running -> succeeded -> superseded; running -> failed | cancelled;
queued -> failed | cancelled. At most one queued/running and one succeeded job per map (partial
unique indexes). A failed or cancelled job never touches the previous result.

Dispatcher (§6.5): one task in the API process (one per cluster: advisory lock
`reconstruction_dispatcher`), every TICK_S seconds and on each POST:
  1. expire: cancel grace (60 s after a cancel with no word from the service -> cancelled), job
     timeout (running > JOB_TIMEOUT_S -> best-effort service cancel, failed `timeout`), queue
     timeout (queued > QUEUE_TIMEOUT_S since it was requested or last sent -> failed
     `service_unavailable`);
  2. poll: a running job silent for POLL_AFTER_S -> GET /jobs/{id}; `succeeded` / `failed` /
     `cancelled` are applied like the callbacks, 404 (or an older attempt) = lost -> one resubmit,
     unreachable for UNREACHABLE_S -> failed `service_unavailable`;
  3. send: while fewer than MAX_INFLIGHT are running, the oldest due queued job gets a fresh
     manifest (built now, so its URLs are fresh) and POST /jobs. 2xx -> running; connection
     error / timeout / 5xx / 429 -> stays queued with backoff 10 s, 30 s, 1 min, then 2 min;
     409 (stale attempt) -> resent with the next attempt; any other 4xx -> failed `rejected`.
  `url_expired` and `lost` are retried once automatically (a new attempt of the same job).

Callbacks (§6.3): `Authorization: Bearer base64url(HMAC-SHA256(CALLBACK_SECRET, job_id))`,
recomputed and compared in constant time (401). 200 {"action": "continue" | "cancel"}; 410
{"action": "stop"} when the job is no longer running or the attempt is old.

Finish (§6.4, §7.2, §8.2): the service delivers only `cloud.ply` + `meta.json`. Each is stat'ed
in the staging bucket (size = reported bytes, meta.json parses, the PLY header is the §7.1 vertex
layout and its size matches N; else failed `bad_output`), the map must exist and not be
DELETING. The job is then CLAIMED (stage `finalizing`, so a repeated finish or the poll does not
start a second one) and the rest runs in a background task, the callback answering at once:
cloud.ply is copied server-side to `map-{id}/reconstruction/{job}/`, downloaded to a temp file,
and the top view (ortho.png, height.png) is derived from it in a child process
(reconstruction_topview.py: chunked, memory-capped, off the event loop); the rasters and the
merged meta.json (the service's fields + the grid) are stored next to it. A failed derivation
fails the job (`top_view_failed`, or `bad_output` for a malformed PLY): a result is committed
only with all four files. Then one transaction (the map row locked FOR SHARE) supersedes the old
result, marks this one succeeded and writes MAP.RECONSTRUCTION_FINISHED; after it, the old
result's prefix and the staging prefix go. While finalizing, `last_contact_at` is refreshed
every HEARTBEAT_S; a claim older than FINALIZE_STALE_S (a crashed worker) may be taken over by
the poll. The map bucket is never created here: a copy into a deleted map fails
(`map_deleting`).

Stale (§8.3): `inputs.digest` = SHA-256 over the sorted (node_id, x, y, yaw, cameras) of the
nodes with depth when the manifest was built; GET recomputes it (cached STALE_CACHE_S per map).

Map delete (packages/api/map_delete.py): `mark_map_deleting()` cancels the active job in the
delete's own transaction; `after_map_delete_marked()` then sends the service a best-effort
cancel and writes MAP.RECONSTRUCTION_FAILED (`map_deleting`); the delete saga's finish removes
the rows, the bucket delete removes the files.

Frames (§3): only the ArangoDB node `pose` (map frame) and `depth.{camera}.pose3d_map` go into
the manifest; `robot_pose` never does.
"""

import asyncio
import base64
import dataclasses
import datetime
import hashlib
import hmac
import io
import json
import logging
import os
import statistics
import tempfile
import time
import urllib.parse
import uuid
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import pydantic
from fastapi import HTTPException

from packages.api.entrypoint import advisory_lock_key
from packages.api.reconstruction_client import ReconstructionClient, ServiceUnreachable
from packages.api.reconstruction_topview import (MAX_HEADER, PlyError, TopViewError,
                                                 derive_in_subprocess, ply_size,
                                                 read_ply_header)
from packages.events.codes import EventCode, Source
from packages.events.emit import Event, emit

logger = logging.getLogger("ApiDelegationService.reconstruction")

# --- constants ---------------------------------------------------------------------------------

QUEUED, RUNNING, SUCCEEDED = "queued", "running", "succeeded"
FAILED, CANCELLED, SUPERSEDED = "failed", "cancelled", "superseded"
ACTIVE = (QUEUED, RUNNING)
TERMINAL = (SUCCEEDED, FAILED, CANCELLED, SUPERSEDED)

# name -> (file, content type): every file of a result (§8.4), as the client reads them
FILES: Dict[str, Tuple[str, str]] = {
    "cloud": ("cloud.ply", "application/octet-stream"),
    "ortho": ("ortho.png", "image/png"),
    "height": ("height.png", "image/png"),
    "relief_rgb": ("relief_rgb.png", "image/png"),
    "relief_height": ("relief_height.png", "image/png"),
    "meta": ("meta.json", "application/json"),
}
FILE_BY_NAME = {f: (k, ct) for k, (f, ct) in FILES.items()}
# what the service PUTs (handover §2.1, §7); ortho/height are derived here (§7.2)
SERVICE_FILES = ("cloud", "meta")
DERIVED_FILES = ("ortho", "height", "relief_rgb", "relief_height")
FINALIZING = "finalizing"
HEARTBEAT_S = 15.0

MANIFEST_VERSION = 1
MAX_NODES = 20000
MAX_META_BYTES = 1 << 20
TICK_S = 5.0
POLL_AFTER_S = 60.0
UNREACHABLE_S = 300.0
CANCEL_GRACE_S = 60.0
STALE_CACHE_S = 10.0
AUTO_RETRY_MAX_ATTEMPTS = 2   # url_expired / lost: resubmit while attempts < 2
FINALIZE_STALE_S = POLL_AFTER_S
BACKOFF_S = (10.0, 30.0, 60.0)
BACKOFF_MAX_S = 120.0
LOCK_NAME = "reconstruction_dispatcher"
RESULT_PREFIX = "reconstruction/"

PARAM_RANGES = {"voxel_m": (0.02, 0.5), "max_depth_m": (0.5, 65.0), "clip_z": (-5.0, 20.0)}
FIXED_PARAMS = {"edge_rel": 0.05, "min_neighbours": 2, "max_voxels": 10_000_000,
                "raster_max_px": 4096}


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(ts: Optional[datetime.datetime]) -> Optional[str]:
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=datetime.timezone.utc)
    return ts.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _ts(ts: Optional[datetime.datetime]) -> float:
    if ts is None:
        return float("-inf")
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=datetime.timezone.utc)
    return ts.timestamp()


def _err(status: int, code: str, message: str, **extra: Any) -> HTTPException:
    return HTTPException(status, {"code": code, "message": message, **extra})


# --- config ------------------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class ReconConfig:
    service_url: Optional[str] = None
    service_key: Optional[str] = None
    callback_secret: Optional[str] = None
    callback_base_url: str = "http://localhost:8000"
    minio_endpoint: str = "localhost:9000"
    minio_secure: bool = False
    staging_bucket: str = "recon-staging"
    url_expiry_s: int = 14400
    job_timeout_s: int = 3600
    queue_timeout_s: int = 1800
    max_inflight: int = 1
    voxel_m: float = 0.05
    max_depth_m: float = 10.0
    clip_z: float = 2.3
    relief_res_m: float = 0.10
    relief_max_cells: int = 4_000_000
    topview_mem_mb: int = 1024
    topview_timeout_s: int = 600
    work_dir: Optional[str] = None

    @property
    def configured(self) -> bool:
        return bool(self.service_url and self.service_key and self.callback_secret)

    @classmethod
    def from_env(cls) -> "ReconConfig":
        from packages import config as c
        return cls(
            service_url=c.RECONSTRUCTION_SERVICE_URL, service_key=c.RECONSTRUCTION_SERVICE_KEY,
            callback_secret=c.RECONSTRUCTION_CALLBACK_SECRET,
            callback_base_url=c.RECONSTRUCTION_CALLBACK_BASE_URL,
            minio_endpoint=c.RECONSTRUCTION_MINIO_ENDPOINT,
            minio_secure=c.RECONSTRUCTION_MINIO_SECURE,
            staging_bucket=c.RECONSTRUCTION_STAGING_BUCKET,
            url_expiry_s=c.RECONSTRUCTION_URL_EXPIRY_S,
            job_timeout_s=c.RECONSTRUCTION_JOB_TIMEOUT_S,
            queue_timeout_s=c.RECONSTRUCTION_QUEUE_TIMEOUT_S,
            max_inflight=max(1, c.RECONSTRUCTION_MAX_INFLIGHT),
            voxel_m=c.RECONSTRUCTION_VOXEL_M, max_depth_m=c.RECONSTRUCTION_MAX_DEPTH_M,
            clip_z=c.RECONSTRUCTION_CLIP_Z,
            relief_res_m=c.RECONSTRUCTION_RELIEF_RES_M,
            relief_max_cells=c.RECONSTRUCTION_RELIEF_MAX_CELLS,
            topview_mem_mb=c.RECONSTRUCTION_TOPVIEW_MEM_MB,
            topview_timeout_s=c.RECONSTRUCTION_TOPVIEW_TIMEOUT_S,
            work_dir=c.RECONSTRUCTION_WORK_DIR)

    def default_params(self) -> Dict[str, Any]:
        return {"voxel_m": self.voxel_m, "max_depth_m": self.max_depth_m,
                "clip_z": self.clip_z}


# --- pure helpers ------------------------------------------------------------------------------

def callback_token(secret: str, job_id: str) -> str:
    """base64url(HMAC-SHA256(secret, job_id)), unpadded (§6.3). Nothing is stored."""
    mac = hmac.new(secret.encode("utf-8"), str(job_id).encode("utf-8"), hashlib.sha256)
    return base64.urlsafe_b64encode(mac.digest()).decode("ascii").rstrip("=")


def check_token(secret: str, job_id: str, authorization: Optional[str]) -> bool:
    """Constant-time check of `Authorization: Bearer <token>` for `job_id`."""
    if not authorization or not authorization.startswith("Bearer "):
        return False
    given = authorization[len("Bearer "):].strip().encode("utf-8")
    return hmac.compare_digest(given, callback_token(secret, job_id).encode("utf-8"))


def backoff(tries: int) -> float:
    """Seconds before the next send after `tries` failed sends (1-based)."""
    if tries <= 0:
        return 0.0
    return BACKOFF_S[tries - 1] if tries <= len(BACKOFF_S) else BACKOFF_MAX_S


class StartBody(pydantic.BaseModel):
    """POST body (all optional): the job parameters a user may override."""
    voxel_m: Optional[float] = None
    max_depth_m: Optional[float] = None
    clip_z: Optional[float] = None

    class Config:
        extra = pydantic.Extra.forbid

    @pydantic.validator("voxel_m", "max_depth_m", "clip_z")
    def _in_range(cls, value, field):  # noqa: N805 - pydantic v1 validator
        if value is None:
            return value
        lo, hi = PARAM_RANGES[field.name]
        if not (lo <= value <= hi):
            raise ValueError(f"must be between {lo} and {hi}")
        return value


def job_params(config: ReconConfig, body: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """The job's params: the gateway's defaults, the body's overrides, the fixed ones. 422 on a
    bad body (unknown field, out of range)."""
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
    params.update(FIXED_PARAMS)
    return params


@dataclasses.dataclass(frozen=True)
class Frame:
    """A node with depth, as the manifest and the digest see it (map frame only)."""
    node_id: str
    x: float
    y: float
    yaw: float
    created_at: str
    cameras: Dict[str, Dict[str, Any]]  # camera -> the node's depth.{camera} record


def frames_from_nodes(docs: Iterable[Mapping[str, Any]]) -> List[Frame]:
    """ArangoDB node documents -> frames: nodes with at least one usable `depth.{camera}`,
    sorted by capture time (created_at, then id). Uses `pose` (map frame), never robot_pose."""
    frames = []
    for doc in docs:
        depth = doc.get("depth")
        pose = doc.get("pose") or {}
        if not isinstance(depth, Mapping) or pose.get("x") is None or pose.get("y") is None:
            continue
        cams = {str(name): dict(rec) for name, rec in depth.items()
                if isinstance(rec, Mapping) and isinstance(rec.get("camera"), Mapping)}
        if not cams:
            continue
        node_id = str(doc.get("node_id") or doc.get("_key"))
        frames.append(Frame(node_id, float(pose["x"]), float(pose["y"]),
                            float(pose.get("yaw") or 0.0), str(doc.get("created_at") or ""),
                            cams))
    frames.sort(key=lambda f: (f.created_at, f.node_id))
    return frames


def digest_nodes(frames: Sequence[Frame]) -> List[List[Any]]:
    """[[node_id, x, y, yaw]] rounded as the digest rounds them (stored as inputs.nodes)."""
    return [[f.node_id, round(f.x, 3), round(f.y, 3), round(f.yaw, 4)] for f in frames]


def input_digest(frames: Sequence[Frame]) -> str:
    """§8.3: SHA-256 over the sorted (node_id, round(x,3), round(y,3), round(yaw,4),
    sorted(depth cameras)) of the nodes with depth."""
    items = sorted([f.node_id, round(f.x, 3), round(f.y, 3), round(f.yaw, 4),
                    sorted(f.cameras)] for f in frames)
    return hashlib.sha256(json.dumps(items, separators=(",", ":")).encode()).hexdigest()


def floor_z(frames: Sequence[Frame]) -> float:
    """§7.2 z_floor: the median base z over the manifest's frames (node-cameras): `pose3d_map.z`,
    0 for a frame without pose3d. Taken when the manifest is built (stored as inputs.z_floor)."""
    zs = []
    for f in frames:
        for rec in f.cameras.values():
            pose3d = rec.get("pose3d_map")
            z = _float(pose3d.get("z")) if isinstance(pose3d, Mapping) else None
            zs.append(z if z is not None and z == z else 0.0)
    return float(statistics.median(zs)) if zs else 0.0


def stale_reason(old_nodes: Optional[Sequence[Sequence[Any]]],
                 frames: Sequence[Frame]) -> Dict[str, int]:
    """{new_nodes, removed_nodes, moved_nodes} between the stored inputs.nodes and now."""
    old = {str(n[0]): tuple(n[1:4]) for n in (old_nodes or [])}
    now = {n[0]: tuple(n[1:4]) for n in digest_nodes(frames)}
    return {"new_nodes": len(now.keys() - old.keys()),
            "removed_nodes": len(old.keys() - now.keys()),
            "moved_nodes": sum(1 for k in now.keys() & old.keys() if now[k] != old[k])}


def map_crs(spec: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    """The manifest's `map.crs`: the geo map's UTM zone and origin; None for a local map."""
    spec = spec or {}
    geo = spec.get("geo") if spec.get("type") == "geo" else None
    if not isinstance(geo, Mapping) or geo.get("utm_zone") is None:
        return None
    return {"utm_zone": int(geo["utm_zone"]), "utm_north": bool(geo.get("utm_north", True)),
            "origin_e": float(geo.get("origin_e") or 0.0),
            "origin_n": float(geo.get("origin_n") or 0.0)}


def rgb_key(node_id: str, camera: str) -> str:
    return f"{node_id}/images/{camera}"


def depth_key(node_id: str, camera: str) -> str:
    return f"{node_id}/depth/{camera}.png"


def staging_key(job_id: str, attempt: int, file: str) -> str:
    return f"{job_id}/{attempt}/{file}"


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def result_prefix(job_id: str) -> str:
    return f"{RESULT_PREFIX}{job_id}/"


def build_manifest(*, job_id: str, attempt: int, map_name: str, map_type: Optional[str],
                   crs: Optional[Dict[str, Any]], params: Mapping[str, Any],
                   frames: Sequence[Frame], map_bucket: str, staging_bucket: str,
                   presign_get: Callable[[str, str], str],
                   presign_put: Callable[[str, str], str], callback_base_url: str,
                   token: str, now: datetime.datetime, expiry_s: int) -> Dict[str, Any]:
    """The POST /jobs body (handover §2.1). `presign_get(bucket, key)` / `presign_put(...)`
    return the URLs; everything else is pure."""
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
    outputs = {name: {"url": presign_put(staging_bucket,
                                         staging_key(job_id, attempt, FILES[name][0])),
                      "content_type": FILES[name][1]} for name in SERVICE_FILES}
    return {
        "manifest_version": MANIFEST_VERSION,
        "job_id": job_id, "attempt": attempt,
        "created_at": _iso(now),
        "expires_at": _iso(now + datetime.timedelta(seconds=expiry_s)),
        "map": {"name": map_name, "type": map_type or "local", "crs": crs},
        "params": dict(params),
        "nodes": nodes,
        "outputs": outputs,
        "callback": {"base_url": callback_url(callback_base_url, job_id), "token": token},
    }


def callback_url(base_url: str, job_id: str) -> str:
    return f"{base_url.rstrip('/')}/internal/reconstruction/jobs/{job_id}"


def file_url(map_name: str, file: str, job_id: str) -> str:
    return (f"/api/v1/maps/{urllib.parse.quote(map_name, safe='')}/reconstruction/files/"
            f"{file}?v={job_id}")


# --- the job row -------------------------------------------------------------------------------

COLUMNS = ("job_id", "map_name", "state", "requested_at", "requested_by", "attempts",
           "next_try_at", "dispatched_at", "started_at", "finished_at", "last_contact_at",
           "cancel_requested", "cancel_requested_at", "stage", "progress", "frames_done",
           "frames_total", "params", "inputs", "result", "artifacts", "error")
JSON_COLUMNS = frozenset({"params", "inputs", "result", "artifacts", "error"})


@dataclasses.dataclass
class Job:
    job_id: str
    map_name: str
    state: str
    requested_at: Optional[datetime.datetime] = None
    requested_by: Optional[str] = None
    attempts: int = 0
    next_try_at: Optional[datetime.datetime] = None
    dispatched_at: Optional[datetime.datetime] = None
    started_at: Optional[datetime.datetime] = None
    finished_at: Optional[datetime.datetime] = None
    last_contact_at: Optional[datetime.datetime] = None
    cancel_requested: bool = False
    cancel_requested_at: Optional[datetime.datetime] = None
    stage: Optional[str] = None
    progress: float = 0.0
    frames_done: Optional[int] = None
    frames_total: Optional[int] = None
    params: Dict[str, Any] = dataclasses.field(default_factory=dict)
    inputs: Optional[Dict[str, Any]] = None
    result: Optional[Dict[str, Any]] = None
    artifacts: Optional[Dict[str, Any]] = None
    error: Optional[Dict[str, Any]] = None

    @classmethod
    def from_row(cls, row: Sequence[Any]) -> "Job":
        values = dict(zip(COLUMNS, row))
        values["job_id"] = str(values["job_id"])
        values["attempts"] = int(values.get("attempts") or 0)
        values["progress"] = float(values.get("progress") or 0.0)
        values["params"] = values.get("params") or {}
        return cls(**values)

    @property
    def waiting_for_service(self) -> bool:
        return (self.state == QUEUED and isinstance(self.error, Mapping)
                and self.error.get("reason") == "service_unavailable")

    def view(self) -> Dict[str, Any]:
        """The job as the client sees it (§9.1 `job`)."""
        return {"job_id": self.job_id, "map_name": self.map_name, "state": self.state,
                "stage": self.stage, "progress": round(float(self.progress), 4),
                "frames_done": self.frames_done, "frames_total": self.frames_total,
                "attempts": self.attempts, "params": self.params,
                "requested_at": _iso(self.requested_at), "requested_by": self.requested_by,
                "started_at": _iso(self.started_at), "finished_at": _iso(self.finished_at),
                "cancel_requested": self.cancel_requested,
                "waiting_for_service": self.waiting_for_service,
                "error": None if self.state == QUEUED else self.error}


def reconstruction_view(job: Job, stale: Optional[bool],
                        reason: Optional[Dict[str, int]]) -> Dict[str, Any]:
    """The map's current result (§9.1 `reconstruction`)."""
    result, inputs = job.result or {}, job.inputs or {}
    files = {}
    for name, info in ((job.artifacts or {}).get("files") or {}).items():
        file = FILES.get(name, (name, None))[0]
        files[name] = {"name": file, "bytes": info.get("bytes"),
                       "url": file_url(job.map_name, file, job.job_id)}
    return {"job_id": job.job_id, "finished_at": _iso(job.finished_at),
            "params": {k: job.params.get(k) for k in ("voxel_m", "max_depth_m", "clip_z")},
            "points": result.get("points"),
            "nodes_total": inputs.get("nodes_total"),
            "nodes_with_depth": inputs.get("nodes_with_depth"),
            "nodes_used": result.get("nodes_used", result.get("frames_used")),
            "frames_total": result.get("frames_total", inputs.get("frames")),
            "frames_used": result.get("frames_used"),
            "frames_skipped": result.get("frames_skipped") or {},
            "voxel_m": result.get("voxel_m"),
            "bounds3d": result.get("bounds3d"),
            "duration_s": result.get("duration_s"),
            "stale": stale, "stale_reason": reason if stale else None,
            "files": files}


def _discriminator(map_name: str, job_id: str, state: str) -> str:
    """Part of the MAP.RECONSTRUCTION_* event_id (stored data: never change the format)."""
    return f"map:{map_name}:reconstruction:{job_id}:{state}"


def started_event(job: Job, now: datetime.datetime, attempt: int) -> Event:
    return Event(EventCode.MAP_RECONSTRUCTION_STARTED, now, source=Source.RECONSTRUCTION,
                 discriminator=_discriminator(job.map_name, job.job_id, "started"),
                 payload={"map_name": job.map_name, "job_id": job.job_id,
                          "params": job.params, "attempt": attempt,
                          "nodes_with_depth": (job.inputs or {}).get("nodes_with_depth")})


def finished_event(job: Job, result: Mapping[str, Any], now: datetime.datetime) -> Event:
    skipped = result.get("frames_skipped")
    return Event(EventCode.MAP_RECONSTRUCTION_FINISHED, now, source=Source.RECONSTRUCTION,
                 discriminator=_discriminator(job.map_name, job.job_id, SUCCEEDED),
                 payload={"map_name": job.map_name, "job_id": job.job_id,
                          "points": _int(result.get("points")),
                          "nodes_used": _int(result.get("nodes_used",
                                                        result.get("frames_used"))),
                          "frames_skipped": ({str(k): int(v) for k, v in skipped.items()
                                              if isinstance(v, (int, float))}
                                             if isinstance(skipped, Mapping) else None),
                          "voxel_m": _float(result.get("voxel_m")),
                          "duration_s": _float(result.get("duration_s"))})


def failed_event(job: Job, state: str, error: Mapping[str, Any],
                 now: datetime.datetime) -> Event:
    return Event(EventCode.MAP_RECONSTRUCTION_FAILED, now, source=Source.RECONSTRUCTION,
                 discriminator=_discriminator(job.map_name, job.job_id, state),
                 payload={"map_name": job.map_name, "job_id": job.job_id,
                          "reason": str(error.get("reason") or "error"),
                          "stage": error.get("stage"), "message": error.get("message")})


def _int(v: Any) -> Optional[int]:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _float(v: Any) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _error(reason: str, message: Optional[str] = None, stage: Optional[str] = None,
           **extra: Any) -> Dict[str, Any]:
    return {"reason": reason, "stage": stage,
            "message": (message[:2000] if isinstance(message, str) else message), **extra}


# --- Postgres ----------------------------------------------------------------------------------

TABLE = "map_reconstructions"
SELECT = f"SELECT {', '.join(COLUMNS)} FROM {TABLE}"
INSERT_SQL = (f"INSERT INTO {TABLE} (job_id, map_name, state, requested_by, params, "
              "next_try_at) VALUES (%s, %s, 'queued', %s, %s::jsonb, now())")
GET_SQL = f"{SELECT} WHERE job_id = %s"
ACTIVE_SQL = f"{SELECT} WHERE map_name = %s AND state IN ('queued', 'running')"
CURRENT_SQL = f"{SELECT} WHERE map_name = %s AND state = 'succeeded'"
LATEST_SQL = f"{SELECT} WHERE map_name = %s ORDER BY requested_at DESC LIMIT 1"
BY_STATE_SQL = f"{SELECT} WHERE state = %s ORDER BY requested_at"
NEXT_QUEUED_SQL = (f"{SELECT} WHERE state = 'queued' AND coalesce(next_try_at, requested_at) "
                   "<= %s ORDER BY requested_at LIMIT 1")
RUNNING_COUNT_SQL = f"SELECT count(*) FROM {TABLE} WHERE state = 'running'"
KEEP_FILES_SQL = f"SELECT job_id FROM {TABLE} WHERE state IN ('running', 'succeeded')"
MAP_SQL = ("SELECT lifecycle, spec FROM mapobjectv1 WHERE name = %s "
           "AND lifecycle <> 'DELETED'")
MAP_LOCK_SQL = ("SELECT lifecycle FROM mapobjectv1 WHERE name = %s "
                "AND lifecycle <> 'DELETED' FOR SHARE")
SUPERSEDE_SQL = (f"UPDATE {TABLE} SET state = 'superseded', inputs = inputs - 'nodes' "
                 "WHERE map_name = %s AND state = 'succeeded' AND job_id <> %s "
                 "RETURNING job_id, artifacts")
SUCCEED_SQL = (f"UPDATE {TABLE} SET state = 'succeeded', result = %s::jsonb, "
               "artifacts = %s::jsonb, finished_at = %s, last_contact_at = %s, progress = 1, "
               "stage = 'done', error = NULL "
               "WHERE job_id = %s AND state = 'running' AND attempts = %s")
# Finish: the one finalizer of a running attempt (a stale claim = a crashed worker, retaken)
CLAIM_SQL = (f"UPDATE {TABLE} SET stage = 'finalizing', last_contact_at = %s, "
             "progress = GREATEST(progress, 0.95) WHERE job_id = %s AND state = 'running' "
             "AND attempts = %s AND (stage IS DISTINCT FROM 'finalizing' "
             "OR last_contact_at IS NULL OR last_contact_at < %s)")
# Map delete (packages/api/map_delete.py::MapDeleter.request, in its transaction): the active job
# is cancelled; the old state says whether the service may still be working on it.
MAP_DELETE_CANCEL_SQL = (
    f"WITH a AS (SELECT job_id, state, attempts FROM {TABLE} WHERE map_name = %s "
    "AND state IN ('queued', 'running') FOR UPDATE) "
    f"UPDATE {TABLE} r SET state = 'cancelled', cancel_requested = true, "
    "cancel_requested_at = coalesce(r.cancel_requested_at, now()), finished_at = now(), "
    "error = %s::jsonb FROM a WHERE r.job_id = a.job_id "
    "RETURNING r.job_id, a.state, a.attempts")
UPDATABLE = frozenset(COLUMNS) - {"job_id", "map_name", "requested_at", "requested_by"}


class JobActive(Exception):
    def __init__(self, job: Optional[Job]):
        super().__init__("a reconstruction job is already active")
        self.job = job


def _param(column: str, value: Any) -> Any:
    return json.dumps(value) if column in JSON_COLUMNS and value is not None else value


class PgRepo:
    """map_reconstructions on the API's PostgresDatabase (pooled connections: one transaction
    each, committed on a clean exit)."""

    def __init__(self, db: Any):
        self._db = db

    async def _one(self, sql: str, params: Sequence[Any] = ()) -> Optional[Job]:
        async with self._db.connection() as conn:
            cur = await conn.execute(sql, params)
            row = await cur.fetchone()
        return Job.from_row(row) if row else None

    async def _all(self, sql: str, params: Sequence[Any] = ()) -> List[Job]:
        async with self._db.connection() as conn:
            cur = await conn.execute(sql, params)
            return [Job.from_row(r) for r in await cur.fetchall()]

    async def map_row(self, name: str) -> Optional[Tuple[str, Dict[str, Any]]]:
        async with self._db.connection() as conn:
            cur = await conn.execute(MAP_SQL, (name,))
            row = await cur.fetchone()
        return (row[0], row[1] or {}) if row else None

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
        """Set `fields` on the job if it is in `states` (and at `attempts`); `event` in the same
        transaction (savepoint). True when the row changed."""
        bad = set(fields) - UPDATABLE
        if bad:
            raise ValueError(f"not updatable: {sorted(bad)}")
        sets = ", ".join(f"{c} = %s" + ("::jsonb" if c in JSON_COLUMNS else "") for c in fields)
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
                await _emit_safe(conn, event)
        return changed

    async def claim_finalize(self, job_id: str, attempt: int, now: datetime.datetime,
                             stale_before: datetime.datetime) -> bool:
        """Stage -> finalizing unless another finalizer holds a fresh claim. True = ours."""
        async with self._db.connection() as conn:
            cur = await conn.execute(CLAIM_SQL, (now, uuid.UUID(job_id), attempt, stale_before))
            return cur.rowcount == 1

    async def commit_success(self, job: Job, attempt: int, result: Mapping[str, Any],
                             artifacts: Mapping[str, Any], now: datetime.datetime,
                             event: Event) -> Tuple[str, List[Dict[str, Any]]]:
        """One transaction (§8.2): the map row FOR SHARE (a concurrent delete's mark waits or
        is seen), old result superseded, this job succeeded, the event. ('ok', superseded
        artifacts) | ('map_deleting', []) | ('gone', []) when the job is no longer running at
        `attempt` (nothing changed)."""
        async with self._db.connection() as conn:
            cur = await conn.execute(MAP_LOCK_SQL, (job.map_name,))
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
            await _emit_safe(conn, event)
        return "ok", old

    async def supersede_current(self, map_name: str) -> List[Dict[str, Any]]:
        """DELETE .../reconstruction: the current result becomes superseded; its artifacts."""
        async with self._db.connection() as conn:
            cur = await conn.execute(SUPERSEDE_SQL, (map_name, uuid.UUID(int=0)))
            return [r[1] or {} for r in await cur.fetchall()]

    async def emit(self, event: Event) -> None:
        async with self._db.connection() as conn:
            await _emit_safe(conn, event)

    async def leader_connection(self) -> Any:
        """A dedicated connection holding the dispatcher lock, or None if another worker
        holds it."""
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


async def _emit_safe(conn: Any, event: Event) -> None:
    """An event in a savepoint: a failing event write is logged, never fails the change."""
    try:
        async with conn.transaction():
            await emit(conn, event)
    except Exception:  # noqa: BLE001
        logger.exception("Could not write %s", event.code.value)


async def mark_map_deleting(cursor: Any, map_id: str) -> List[Tuple[str, str, int]]:
    """In MapDeleter.request()'s transaction: cancel the map's active job (§6.6). Returns
    [(job_id, old_state, attempts)] for after_map_delete_marked()."""
    await cursor.execute(MAP_DELETE_CANCEL_SQL, (map_id, json.dumps(_error(
        "map_deleting", "the map is being deleted"))))
    return [(str(r[0]), r[1], int(r[2] or 0)) for r in await cursor.fetchall()]


# --- MinIO -------------------------------------------------------------------------------------

class Presigner:
    """Presigns for the host the SERVICE reaches MinIO at (RECONSTRUCTION_MINIO_ENDPOINT, §6.7):
    SigV4 signs the Host header, so a URL cannot be rewritten after signing. A fixed region
    means presigning makes no network call."""

    def __init__(self, endpoint: str, access_key: str, secret_key: str, secure: bool,
                 region: str = "us-east-1"):
        from minio import Minio
        self.endpoint = endpoint
        self._client = Minio(endpoint, access_key=access_key, secret_key=secret_key,
                             secure=secure, region=region)

    def get(self, bucket: str, key: str, expires_s: int) -> str:
        return self._client.presigned_get_object(
            bucket, key, expires=datetime.timedelta(seconds=expires_s))

    def put(self, bucket: str, key: str, expires_s: int) -> str:
        return self._client.presigned_put_object(
            bucket, key, expires=datetime.timedelta(seconds=expires_s))


class ObjectStore:
    """The blocking MinIO operations of the gateway, on the API's own MinIO client."""

    def __init__(self, client: Any, bucket_for: Callable[[str], str]):
        self.client = client
        self.bucket_for = bucket_for

    def ensure_staging(self, bucket: str, expire_days: int = 1) -> None:
        """The staging bucket with a lifecycle rule that expires every object after a day."""
        from minio.commonconfig import ENABLED, Filter
        from minio.lifecycleconfig import Expiration, LifecycleConfig, Rule
        from minio.error import S3Error
        try:
            if not self.client.bucket_exists(bucket):
                self.client.make_bucket(bucket)
        except S3Error as exc:
            if exc.code not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
                raise
        self.client.set_bucket_lifecycle(bucket, LifecycleConfig([Rule(
            ENABLED, rule_filter=Filter(prefix=""), rule_id="recon-staging-expiry",
            expiration=Expiration(days=expire_days))]))

    def size(self, bucket: str, key: str) -> Optional[int]:
        from minio.error import S3Error
        try:
            return int(self.client.stat_object(bucket, key).size)
        except S3Error as exc:
            if exc.code in ("NoSuchKey", "NoSuchBucket", "NoSuchObject"):
                return None
            raise

    def read(self, bucket: str, key: str, limit: int) -> bytes:
        response = self.client.get_object(bucket, key)
        try:
            return response.read(limit + 1)
        finally:
            response.close()
            response.release_conn()

    def download(self, bucket: str, key: str, path: str) -> None:
        """Stream the object into `path` (bounded memory)."""
        self.client.fget_object(bucket, key, path)

    def upload_file(self, bucket: str, key: str, path: str, content_type: str) -> None:
        self.client.fput_object(bucket, key, path, content_type=content_type)

    def put_bytes(self, bucket: str, key: str, data: bytes, content_type: str) -> None:
        self.client.put_object(bucket, key, io.BytesIO(data), len(data),
                               content_type=content_type)

    def bucket_exists(self, bucket: str) -> bool:
        try:
            return bool(self.client.bucket_exists(bucket))
        except ValueError:  # an invalid bucket name: it cannot exist
            return False

    def copy(self, src_bucket: str, src_key: str, dst_bucket: str, dst_key: str) -> None:
        from minio.commonconfig import CopySource
        self.client.copy_object(dst_bucket, dst_key, CopySource(src_bucket, src_key))

    def remove_prefix(self, bucket: str, prefix: str) -> int:
        from minio.deleteobjects import DeleteObject
        from minio.error import S3Error
        try:
            names = [o.object_name for o in self.client.list_objects(bucket, prefix=prefix,
                                                                     recursive=True)]
        except S3Error as exc:
            if exc.code == "NoSuchBucket":
                return 0
            raise
        if names:
            errors = list(self.client.remove_objects(bucket, [DeleteObject(n) for n in names]))
            if errors:
                raise RuntimeError(f"removing {bucket}/{prefix}: {errors[:3]}")
        return len(names)

    def result_jobs(self) -> List[Tuple[str, str]]:
        """(map bucket, job id) of every `reconstruction/{job}/` prefix in the map buckets."""
        out = []
        for b in self.client.list_buckets():
            if not b.name.startswith("map-"):
                continue
            for obj in self.client.list_objects(b.name, prefix=RESULT_PREFIX):
                parts = obj.object_name.split("/")
                if len(parts) >= 2 and parts[1]:
                    out.append((b.name, parts[1]))
        return out

    def stream(self, bucket: str, key: str, chunk: int = 256 * 1024):
        """A generator over the object's bytes (StreamingResponse runs it in a thread)."""
        response = self.client.get_object(bucket, key)
        try:
            for data in response.stream(chunk):
                yield data
        finally:
            response.close()
            response.release_conn()


# --- the gateway -------------------------------------------------------------------------------

class ReconstructionGateway:
    """Everything the routes, callbacks and dispatcher do. `repo` is PgRepo (tests: a fake),
    `depth_nodes(map)` returns (node count, node docs with depth) from ArangoDB, `objects` an
    ObjectStore, `presigner` a Presigner, `client` a ReconstructionClient (None when not
    configured). Blocking calls (ArangoDB, MinIO) run in a thread. `derive_top_view(ply, out_dir,
    **grid params)` makes ortho.png + height.png (default: reconstruction_topview's child
    process)."""

    def __init__(self, repo: Any, depth_nodes: Callable[[str], Tuple[int, List[Dict]]],
                 objects: Any, presigner: Any, client: Optional[ReconstructionClient],
                 config: ReconConfig, *,
                 now: Callable[[], datetime.datetime] = _utcnow,
                 monotonic: Callable[[], float] = time.monotonic,
                 derive_top_view: Optional[Callable[..., Any]] = None):
        self.repo = repo
        self._depth_nodes = depth_nodes
        self.objects = objects
        self.presigner = presigner
        self.client = client
        self.config = config
        self._now = now
        self._mono = monotonic
        self._derive = derive_top_view or self._derive_in_subprocess
        self._finalizers: Dict[str, asyncio.Task] = {}
        self._stale_cache: Dict[str, Tuple[float, Tuple[int, List[Frame]]]] = {}
        self._task: Optional[asyncio.Task] = None
        self._wake: Optional[asyncio.Event] = None
        self._staging_ready = False

    # --- helpers --------------------------------------------------------------------------------
    async def _frames(self, map_name: str) -> Tuple[int, List[Frame]]:
        total, docs = await asyncio.to_thread(self._depth_nodes, map_name)
        return int(total), frames_from_nodes(docs)

    async def _frames_cached(self, map_name: str) -> Tuple[int, List[Frame]]:
        hit = self._stale_cache.get(map_name)
        if hit is not None and self._mono() < hit[0]:
            return hit[1]
        value = await self._frames(map_name)
        self._stale_cache[map_name] = (self._mono() + STALE_CACHE_S, value)
        return value

    async def _map(self, map_name: str) -> Dict[str, Any]:
        row = await self.repo.map_row(map_name)
        if row is None:
            raise _err(404, "map_not_found", f"Map '{map_name}' not found")
        if row[0] == "DELETING":
            raise _err(409, "map_deleting", f"Map '{map_name}' is being deleted")
        return row[1] or {}

    def _require_configured(self) -> None:
        if not self.config.configured or self.client is None:
            raise _err(503, "not_configured",
                       "The reconstruction service is not configured "
                       "(RECONSTRUCTION_SERVICE_URL, _SERVICE_KEY, _CALLBACK_SECRET)")

    def wake(self) -> None:
        if self._wake is not None:
            self._wake.set()

    async def _cleanup_staging(self, job_id: str) -> None:
        try:
            await asyncio.to_thread(self.objects.remove_prefix, self.config.staging_bucket,
                                    f"{job_id}/")
        except Exception as exc:  # noqa: BLE001 - the 1-day expiry rule removes it anyway
            logger.warning("Job %s: could not clean its staging prefix: %s", job_id, exc)

    async def _remove_result(self, artifacts: Mapping[str, Any]) -> None:
        bucket, prefix = artifacts.get("bucket"), artifacts.get("prefix")
        if not bucket or not prefix:
            return
        try:
            await asyncio.to_thread(self.objects.remove_prefix, bucket, prefix)
        except Exception as exc:  # noqa: BLE001 - the startup sweep retries it
            logger.warning("Could not remove old reconstruction %s/%s: %s", bucket, prefix, exc)

    async def _service_cancel(self, job_id: str) -> None:
        if self.client is None:
            return
        try:
            await self.client.cancel(job_id)
        except Exception as exc:  # noqa: BLE001 - best effort; callbacks/poll finish it
            logger.info("Job %s: cancel at the service failed: %s", job_id, exc)

    async def _end(self, job: Job, state: str, error: Mapping[str, Any], *,
                   attempts: Optional[int] = None, states: Sequence[str] = ACTIVE) -> bool:
        """Job -> failed | cancelled with `error`, MAP.RECONSTRUCTION_FAILED, staging removed."""
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
            raise _err(409, "job_active", "A reconstruction of this map is already active",
                       job=existing.view())
        _total, frames = await self._frames(map_name)
        if not frames:
            raise _err(409, "no_depth", f"Map '{map_name}' has no nodes with depth "
                       "(recorded before depth capture)")
        if len(frames) > MAX_NODES:
            raise _err(422, "too_many_nodes",
                       f"{len(frames)} nodes with depth; at most {MAX_NODES} per job")
        try:
            job = await self.repo.insert(str(uuid.uuid4()), map_name, params, requested_by)
        except JobActive as exc:
            raise _err(409, "job_active", "A reconstruction of this map is already active",
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
        recon = None
        if current is not None:
            stale, reason = None, None
            try:
                _total, frames = await self._frames_cached(map_name)
                stale = input_digest(frames) != (current.inputs or {}).get("digest")
                reason = stale_reason((current.inputs or {}).get("nodes"), frames)
            except Exception as exc:  # noqa: BLE001 - ArangoDB down: stale unknown
                logger.warning("Map %s: stale check failed: %s", map_name, exc)
            recon = reconstruction_view(current, stale, reason)
        return {"map_name": map_name, "configured": self.config.configured,
                "reconstruction": recon, "job": job.view() if job else None}

    async def cancel(self, map_name: str) -> Dict[str, Any]:
        await self._map(map_name)
        job = await self.repo.active(map_name)
        if job is None:
            raise _err(404, "no_active_job", f"Map '{map_name}' has no active reconstruction")
        return (await self._cancel_job(job)).view()

    async def _cancel_job(self, job: Job) -> Job:
        now = self._now()
        if job.state == QUEUED:
            await self._end(job, CANCELLED, _error("cancelled", "cancelled by the user"),
                            states=(QUEUED,))
            if job.attempts:  # a resubmit was pending: the service may still know the job
                await self._service_cancel(job.job_id)
        else:
            await self.repo.update(job.job_id, {"cancel_requested": True,
                                                "cancel_requested_at": now},
                                   states=(RUNNING,))
            await self._service_cancel(job.job_id)
        return (await self.repo.get(job.job_id)) or job

    async def delete(self, map_name: str) -> None:
        await self._map(map_name)
        active = await self.repo.active(map_name)
        if active is not None:
            if active.state == RUNNING:
                await self._end(active, CANCELLED, _error("cancelled", "result deleted"),
                                states=(RUNNING,))
                await self._service_cancel(active.job_id)
            else:
                await self._cancel_job(active)
        for artifacts in await self.repo.supersede_current(map_name):
            await self._remove_result(artifacts)
        self._stale_cache.pop(map_name, None)

    async def open_file(self, map_name: str, file: str) -> Dict[str, Any]:
        """Where the current result's `file` is: {bucket, key, bytes, content_type, job_id}."""
        if file not in FILE_BY_NAME:
            raise _err(404, "file_not_found", f"No reconstruction file '{file}'")
        await self._map(map_name)
        current = await self.repo.current(map_name)
        if current is None or not current.artifacts:
            raise _err(404, "no_reconstruction", f"Map '{map_name}' has no reconstruction")
        name, content_type = FILE_BY_NAME[file]
        info = (current.artifacts.get("files") or {}).get(name)
        if not info:
            raise _err(404, "file_not_found", f"No reconstruction file '{file}'")
        return {"bucket": current.artifacts["bucket"], "key": info["key"],
                "bytes": info.get("bytes"), "content_type": info.get("content_type")
                or content_type, "job_id": current.job_id, "file": file}

    # --- callbacks ------------------------------------------------------------------------------
    def authorize(self, job_id: str, authorization: Optional[str]) -> None:
        if not self.config.callback_secret:
            raise _err(503, "not_configured", "RECONSTRUCTION_CALLBACK_SECRET is not set")
        if not check_token(self.config.callback_secret, job_id, authorization):
            raise HTTPException(401, "invalid callback token")

    async def _callback_job(self, job_id: str, attempt: int) -> Optional[Job]:
        """The job if it is running at `attempt`, else None (-> 410)."""
        job = await self.repo.get(job_id)
        if job is None or job.state != RUNNING or job.attempts != attempt:
            return None
        return job

    async def on_progress(self, job_id: str, body: Mapping[str, Any]) -> Tuple[int, Dict]:
        attempt = _int(body.get("attempt"))
        job = await self._callback_job(job_id, attempt)
        if job is None:
            return 410, {"action": "stop"}
        await self._apply_progress(job, body)
        return 200, {"action": "cancel" if job.cancel_requested else "continue"}

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

    async def on_finish(self, job_id: str, body: Mapping[str, Any]) -> Tuple[int, Dict]:
        attempt = _int(body.get("attempt"))
        job = await self.repo.get(job_id)
        if job is not None and job.state == SUCCEEDED and job.attempts == attempt:
            return 200, {"action": "continue"}  # a repeated finish (our answer was lost)
        job = await self._callback_job(job_id, attempt)
        if job is None:
            return 410, {"action": "stop"}
        return await self._finish(job, attempt, body.get("result"), body.get("outputs"))

    async def _finish(self, job: Job, attempt: int, result: Any,
                      outputs: Any) -> Tuple[int, Dict]:
        if job.cancel_requested:
            await self._end(job, CANCELLED, _error("cancelled", "cancelled by the user"),
                            states=(RUNNING,), attempts=attempt)
            return 410, {"action": "stop"}
        live = self._finalizers.get(job.job_id)
        if live is not None and not live.done():
            return 200, {"action": "continue"}  # this worker is finalizing it already
        problem, meta = await asyncio.to_thread(self._verify, job.job_id, attempt, outputs)
        if problem:
            await self._end(job, FAILED, _error("bad_output", problem, "finish"),
                            states=(RUNNING,), attempts=attempt)
            return 200, {"action": "continue"}
        row = await self.repo.map_row(job.map_name)
        if row is None or row[0] == "DELETING":
            await self._end(job, CANCELLED if row is not None else FAILED,
                            _error("map_deleting", "the map is being deleted", "finish"),
                            states=(RUNNING,), attempts=attempt)
            return 410, {"action": "stop"}
        now = self._now()
        stale = now - datetime.timedelta(seconds=FINALIZE_STALE_S)
        if not await self.repo.claim_finalize(job.job_id, attempt, now, stale):
            return 200, {"action": "continue"}  # another worker is finalizing it
        result = dict(result) if isinstance(result, Mapping) else {}
        task = asyncio.get_running_loop().create_task(
            self._finalize(job, attempt, result, outputs, meta),
            name=f"api.reconstruction.finalize.{job.job_id}")
        self._finalizers[job.job_id] = task
        task.add_done_callback(lambda t, j=job.job_id: self._finalizers.pop(j, None)
                               if self._finalizers.get(j) is t else None)
        return 200, {"action": "continue"}

    async def settle(self) -> None:
        """Wait for the running finalizations (tests; shutdown cancels them instead)."""
        while self._finalizers:
            await asyncio.gather(*list(self._finalizers.values()), return_exceptions=True)

    async def _heartbeat(self, job: Job, attempt: int) -> None:
        """Keep the finalize claim fresh (and the poll away) while the top view is made."""
        while True:
            await asyncio.sleep(HEARTBEAT_S)
            try:
                await self.repo.update(job.job_id, {"last_contact_at": self._now()},
                                       states=(RUNNING,), attempts=attempt)
            except Exception as exc:  # noqa: BLE001 - the next beat retries
                logger.warning("Job %s: finalize heartbeat failed: %s", job.job_id, exc)

    async def _derive_in_subprocess(self, ply: str, out_dir: str, **grid: Any) -> Dict[str, Any]:
        return await derive_in_subprocess(ply, out_dir, mem_mb=self.config.topview_mem_mb,
                                          timeout_s=self.config.topview_timeout_s,
                                          relief_res_m=self.config.relief_res_m,
                                          relief_max_cells=self.config.relief_max_cells, **grid)

    async def _finalize(self, job: Job, attempt: int, result: Dict[str, Any], outputs: Any,
                        meta: Dict[str, Any]) -> None:
        """Copy cloud.ply, derive and store the top view and meta.json, commit (module doc)."""
        beat = asyncio.get_running_loop().create_task(self._heartbeat(job, attempt))
        try:
            await self._finalize_steps(job, attempt, result, outputs, meta)
        except asyncio.CancelledError:
            raise  # shutdown: the job stays running/finalizing; the poll finishes it later
        except Exception:  # noqa: BLE001
            logger.exception("Job %s: finalizing failed", job.job_id)
        finally:
            beat.cancel()

    async def _finalize_steps(self, job: Job, attempt: int, result: Dict[str, Any],
                              outputs: Any, meta: Dict[str, Any]) -> None:
        cfg = self.config
        bucket = self.objects.bucket_for(job.map_name)
        prefix = result_prefix(job.job_id)
        files: Dict[str, Dict[str, Any]] = {}
        cloud_key = staging_key(job.job_id, attempt, FILES["cloud"][0])

        async def fail(reason: str, message: str) -> None:
            await self._remove_result({"bucket": bucket, "prefix": prefix})
            await self._end(job, FAILED, _error(reason, message, FINALIZING),
                            states=(RUNNING,), attempts=attempt)

        try:
            if not await asyncio.to_thread(self.objects.bucket_exists, bucket):
                raise LookupError("the map bucket does not exist")
            await asyncio.to_thread(self.objects.copy, cfg.staging_bucket, cloud_key, bucket,
                                    prefix + FILES["cloud"][0])
            files["cloud"] = {"key": prefix + FILES["cloud"][0],
                              "bytes": int(outputs["cloud"]["bytes"]),
                              "sha256": outputs["cloud"].get("sha256"),
                              "content_type": FILES["cloud"][1]}
            with tempfile.TemporaryDirectory(prefix="recon-", dir=cfg.work_dir) as tmp:
                ply = os.path.join(tmp, "cloud.ply")
                await asyncio.to_thread(self.objects.download, cfg.staging_bucket, cloud_key,
                                        ply)
                grid = await self._derive(ply, tmp, **self._grid_params(job, result))
                for name in DERIVED_FILES:
                    file, ct = FILES[name]
                    path = os.path.join(tmp, file)
                    if name.startswith("relief_") and not os.path.exists(path):
                        continue  # (a custom derive without the relief; the real one writes it)
                    await asyncio.to_thread(self.objects.upload_file, bucket, prefix + file,
                                            path, ct)
                    files[name] = {"key": prefix + file, "bytes": os.path.getsize(path),
                                   "sha256": await asyncio.to_thread(_sha256_file, path),
                                   "content_type": ct}
            data = json.dumps({**meta, **{k: v for k, v in grid.items() if k != "top_view"}},
                              separators=(",", ":")).encode("utf-8")
            file, ct = FILES["meta"]
            await asyncio.to_thread(self.objects.put_bytes, bucket, prefix + file, data, ct)
            files["meta"] = {"key": prefix + file, "bytes": len(data),
                             "sha256": hashlib.sha256(data).hexdigest(), "content_type": ct}
        except TopViewError as exc:
            logger.warning("Job %s: top view failed (%s): %s", job.job_id, exc.reason,
                           exc.message)
            await fail(exc.reason, exc.message)
            return
        except LookupError as exc:
            await self._end(job, FAILED, _error("map_deleting", str(exc), FINALIZING),
                            states=(RUNNING,), attempts=attempt)
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("Job %s: storing the outputs failed", job.job_id)
            await fail("error", f"storing the outputs: {exc}")
            return
        result = {**result, "top_view": grid.get("top_view")}
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
        logger.info("Job %s (map %s) succeeded: %s points, top view %sx%s", job.job_id,
                    job.map_name, result.get("points"), grid.get("width"), grid.get("height"))
        for artifacts_old in old:
            await self._remove_result(artifacts_old)
        await self._cleanup_staging(job.job_id)
        self._stale_cache.pop(job.map_name, None)

    def _grid_params(self, job: Job, result: Mapping[str, Any]) -> Dict[str, Any]:
        """The §7.2 inputs, all the cloud's own: z_floor from the manifest's poses
        (inputs.z_floor), clip_z / raster_max_px from the job, voxel_m as the service used it
        (result.voxel_m, else the job's)."""
        params = job.params or {}
        voxel = _float(result.get("voxel_m"))
        if voxel is None or not voxel > 0:
            voxel = _float(params.get("voxel_m")) or self.config.voxel_m
        return {"z_floor": _float((job.inputs or {}).get("z_floor")) or 0.0,
                "clip_z": _float(params.get("clip_z"))
                if params.get("clip_z") is not None else self.config.clip_z,
                "voxel_m": voxel,
                "raster_max_px": _int(params.get("raster_max_px"))
                or FIXED_PARAMS["raster_max_px"]}

    def _verify(self, job_id: str, attempt: int,
                outputs: Any) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        """(why the staged outputs are unusable, None) or (None, meta.json) (blocking)."""
        if not isinstance(outputs, Mapping):
            return "finish without outputs", None
        bucket = self.config.staging_bucket
        sizes = {}
        for name in SERVICE_FILES:
            file = FILES[name][0]
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
                                MAX_META_BYTES)
        if len(raw) > MAX_META_BYTES:
            return "meta.json too large", None
        try:
            meta = json.loads(raw)
        except ValueError as exc:
            return f"meta.json does not parse: {exc}", None
        if not isinstance(meta, dict):
            return "meta.json is not an object", None
        head = self.objects.read(bucket, staging_key(job_id, attempt, "cloud.ply"),
                                 MAX_HEADER + 16)
        try:
            header_len, count, props = read_ply_header(head)
        except PlyError as exc:
            return f"cloud.ply: {exc}", None
        want = ply_size(header_len, count, props)
        if want != sizes["cloud"]:
            return f"cloud.ply: {sizes['cloud']} bytes, its header says {want}", None
        return None, meta

    async def on_fail(self, job_id: str, body: Mapping[str, Any]) -> Tuple[int, Dict]:
        attempt = _int(body.get("attempt"))
        job = await self._callback_job(job_id, attempt)
        if job is None:
            return 410, {"action": "stop"}
        await self._fail(job, attempt, _error(str(body.get("reason") or "error")[:64],
                                              body.get("message"), body.get("stage")))
        return 200, {"action": "continue"}

    async def _fail(self, job: Job, attempt: int, error: Dict[str, Any]) -> None:
        reason = error["reason"]
        if job.cancel_requested or reason == "cancelled":
            await self._end(job, CANCELLED, {**error, "reason": "cancelled"},
                            states=(RUNNING,), attempts=attempt)
        elif reason in ("url_expired", "lost") and job.attempts < AUTO_RETRY_MAX_ATTEMPTS:
            await self._requeue(job, attempt, reason, error.get("message"))
        else:
            await self._end(job, FAILED, error, states=(RUNNING,), attempts=attempt)

    async def _requeue(self, job: Job, attempt: int, reason: str,
                       message: Optional[str]) -> None:
        """One automatic retry (a new attempt with fresh URLs) after url_expired / lost."""
        changed = await self.repo.update(
            job.job_id, {"state": QUEUED, "next_try_at": self._now(), "stage": None,
                         "progress": 0.0, "frames_done": None, "error": None},
            states=(RUNNING,), attempts=attempt)
        if changed:
            logger.info("Job %s: %s (%s); resubmitting", job.job_id, reason, message)
            await self._cleanup_staging(job.job_id)
            self.wake()

    # --- dispatcher -----------------------------------------------------------------------------
    async def tick(self) -> None:
        """One dispatcher pass: expire, poll, send (module docstring)."""
        await self._expire()
        await self._poll()
        await self._send()

    async def _expire(self) -> None:
        now = self._now()
        for job in await self.repo.by_state(RUNNING):
            if job.cancel_requested and job.cancel_requested_at is not None and \
                    (now - job.cancel_requested_at).total_seconds() >= CANCEL_GRACE_S:
                await self._end(job, CANCELLED, _error("cancelled", "cancelled by the user"),
                                states=(RUNNING,), attempts=job.attempts)
            elif job.dispatched_at is not None and \
                    (now - job.dispatched_at).total_seconds() >= self.config.job_timeout_s:
                await self._service_cancel(job.job_id)
                await self._end(job, FAILED, _error(
                    "timeout", f"running for more than {self.config.job_timeout_s} s",
                    job.stage), states=(RUNNING,), attempts=job.attempts)
        for job in await self.repo.by_state(QUEUED):
            since = job.dispatched_at or job.requested_at
            if since is not None and \
                    (now - since).total_seconds() >= self.config.queue_timeout_s:
                last = (job.error or {}).get("message")
                await self._end(job, FAILED, _error(
                    "service_unavailable",
                    f"not accepted by the service within {self.config.queue_timeout_s} s"
                    + (f" (last: {last})" if last else "")), states=(QUEUED,))

    async def _poll(self) -> None:
        if self.client is None:
            return
        now = self._now()
        for job in await self.repo.by_state(RUNNING):
            last = job.last_contact_at or job.dispatched_at or now
            if (now - last).total_seconds() < POLL_AFTER_S:
                continue
            try:
                status, body = await self.client.get_job(job.job_id)
            except ServiceUnreachable as exc:
                status, body = None, {"error": str(exc)}
            if status == 404 or (status == 200 and _int(body.get("attempt")) is not None
                                 and _int(body.get("attempt")) < job.attempts):
                await self._lost(job)
                continue
            if status != 200:
                if (now - last).total_seconds() >= UNREACHABLE_S:
                    await self._end(job, FAILED, _error(
                        "service_unavailable",
                        f"no contact with the service for {int(UNREACHABLE_S)} s "
                        f"({status or body.get('error')})", job.stage),
                        states=(RUNNING,), attempts=job.attempts)
                continue
            if job.cancel_requested:
                await self._service_cancel(job.job_id)
            state = body.get("state")
            if state in (QUEUED, RUNNING):
                await self._apply_progress(job, body)
            elif state == SUCCEEDED:
                await self._finish(job, job.attempts, body.get("result"), body.get("outputs"))
            elif state in (FAILED, CANCELLED):
                err = body.get("error") if isinstance(body.get("error"), Mapping) else {}
                await self._fail(job, job.attempts, _error(
                    str(err.get("reason") or ("cancelled" if state == CANCELLED else "error")),
                    err.get("message"), err.get("stage")))

    async def _lost(self, job: Job) -> None:
        if job.attempts < AUTO_RETRY_MAX_ATTEMPTS and not job.cancel_requested:
            await self._requeue(job, job.attempts, "lost", "the service does not know the job")
        elif job.cancel_requested:
            await self._end(job, CANCELLED, _error("cancelled", "cancelled by the user"),
                            states=(RUNNING,), attempts=job.attempts)
        else:
            await self._end(job, FAILED, _error("lost", "the service lost the job again"),
                            states=(RUNNING,), attempts=job.attempts)

    async def _send(self) -> None:
        if not self.config.configured or self.client is None:
            return
        running = await self.repo.running_count()
        tried = set()
        for _ in range(10):
            if running >= self.config.max_inflight:
                return
            job = await self.repo.next_queued(self._now())
            if job is None or job.job_id in tried:  # one try per job and pass
                return
            tried.add(job.job_id)
            if await self._dispatch(job):
                running += 1

    async def _dispatch(self, job: Job) -> bool:
        """Build a fresh manifest for `job` and POST it. True when the service took it."""
        cfg = self.config
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
        if len(frames) > MAX_NODES:
            await self._end(job, FAILED, _error(
                "rejected", f"{len(frames)} nodes with depth; at most {MAX_NODES}"),
                states=(QUEUED,))
            return False
        attempt = job.attempts + 1
        now = self._now()
        try:
            manifest = await asyncio.to_thread(self._manifest, job, attempt, spec, frames, now)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Job %s: building the manifest failed", job.job_id)
            await self._backoff(job, f"building the manifest failed: {exc}")
            return False
        try:
            status, body = await self.client.submit(manifest)
        except ServiceUnreachable as exc:
            await self._backoff(job, str(exc))
            return False
        if status in (200, 202):
            inputs = {"nodes_total": total, "nodes_with_depth": len(frames),
                      "frames": sum(len(f.cameras) for f in frames),
                      "digest": input_digest(frames), "nodes": digest_nodes(frames),
                      "z_floor": floor_z(frames)}
            changed = await self.repo.update(job.job_id, {
                "state": RUNNING, "attempts": attempt, "dispatched_at": now,
                "last_contact_at": now, "next_try_at": None, "stage": QUEUED, "progress": 0.0,
                "frames_done": 0, "frames_total": inputs["frames"], "inputs": inputs,
                "error": None}, states=(QUEUED,), attempts=job.attempts)
            if not changed:  # cancelled while we were sending
                await self._service_cancel(job.job_id)
                return False
            logger.info("Job %s (map %s) sent, attempt %d, %d frames", job.job_id,
                        job.map_name, attempt, inputs["frames"])
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

    async def _backoff(self, job: Job, message: str) -> None:
        tries = int((job.error or {}).get("tries") or 0) + 1
        await self.repo.update(job.job_id, {
            "next_try_at": self._now() + datetime.timedelta(seconds=backoff(tries)),
            "error": _error("service_unavailable", message, tries=tries)},
            states=(QUEUED,), attempts=job.attempts)
        logger.info("Job %s: not sent (%s); retry %d in %.0f s", job.job_id, message, tries,
                    backoff(tries))

    # --- lifecycle ------------------------------------------------------------------------------
    async def prepare(self) -> None:
        """Leader start (§8.2, §8.4): the staging bucket and its expiry rule; remove every
        `reconstruction/{job}/` prefix whose job is neither running nor succeeded (a crash
        between copy and commit, or a downgrade). Never raises."""
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
                    logger.info("Removed %d orphaned reconstruction file(s) %s/%s", n,
                                bucket, result_prefix(job_id))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Reconstruction orphan sweep failed: %s", exc)

    def start_dispatcher(self) -> None:
        """Start the dispatcher loop (only when configured). Never raises."""
        if not self.config.configured or self._task is not None:
            return
        try:
            self._wake = asyncio.Event()
            self._task = asyncio.get_running_loop().create_task(
                self._loop(), name="api.reconstruction.dispatcher")
        except Exception:  # noqa: BLE001
            logger.exception("Could not start the reconstruction dispatcher")

    async def _loop(self) -> None:
        while True:
            conn = None
            try:
                conn = await self.repo.leader_connection()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("Reconstruction dispatcher: no lock connection: %s", exc)
            if conn is None:
                await asyncio.sleep(TICK_S)
                continue
            try:
                logger.info("Reconstruction dispatcher running (service %s)",
                            self.config.service_url)
                await self.prepare()
                while not getattr(conn, "closed", False):
                    if not self._staging_ready:
                        await self.prepare()
                    try:
                        await self.tick()
                    except asyncio.CancelledError:
                        raise
                    except Exception:  # noqa: BLE001
                        logger.exception("Reconstruction dispatcher pass failed")
                    try:
                        await asyncio.wait_for(self._wake.wait(), TICK_S)
                    except asyncio.TimeoutError:
                        pass
                    self._wake.clear()
            finally:
                try:
                    await conn.close()
                except Exception:  # noqa: BLE001
                    pass

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except BaseException:  # noqa: BLE001
                pass
        for task in list(self._finalizers.values()):  # the poll finishes them after a restart
            task.cancel()
        if self._finalizers:
            await asyncio.gather(*list(self._finalizers.values()), return_exceptions=True)
        if self.client is not None:
            await self.client.close()

    # --- map delete hook (after the delete's transaction committed) -----------------------------
    async def after_map_delete_marked(self, map_id: str,
                                      marked: Sequence[Tuple[str, str, int]]) -> None:
        """MAP.RECONSTRUCTION_FAILED (map_deleting) for each job the delete cancelled and a
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


def depth_node_reader(graph_db: Any) -> Callable[[str], Tuple[int, List[Dict[str, Any]]]]:
    """(node count, node docs with depth) of a map from the API's GraphDatabaseService."""
    def read(map_name: str) -> Tuple[int, List[Dict[str, Any]]]:
        return graph_db.depth_nodes(map_name)
    return read


def create_gateway(db: Any, graph_db: Any, image_db: Any,
                   minio_access_key: Optional[str], minio_secret_key: Optional[str],
                   config: Optional[ReconConfig] = None) -> ReconstructionGateway:
    """The API's gateway (server.py). Without the service configured it still serves GET and
    the files; POST is 503 and no dispatcher runs."""
    config = config or ReconConfig.from_env()
    presigner = Presigner(config.minio_endpoint, minio_access_key or "", minio_secret_key or "",
                          config.minio_secure)
    client = (ReconstructionClient(config.service_url, config.service_key)
              if config.configured else None)
    return ReconstructionGateway(PgRepo(db), depth_node_reader(graph_db),
                                 ObjectStore(image_db.client, image_db._bucket_name),
                                 presigner, client, config)
