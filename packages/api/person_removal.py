"""Dynamic-object (person) removal for the 3D reconstruction and the mesh (docs/reconstruction/person_removal.md).

Per keyframe camera: RGB -> person mask (the optional person-mask service, packages/services/person_mask) -> grown by
`dilate_px` and fitted to the depth image -> the depth with those pixels set to 0 (= no data, handover §4). Both are
stored NEXT TO the raw data, which is never changed:

    map-{id}/{node}/person_mask/{camera}.png      u8, 255 = person (depth image size), only when a person was found
    map-{id}/{node}/depth_masked/{camera}.png     u16 mm, the raw depth with the person pixels zeroed (same condition)
    node document `person_mask.{camera}`          {version, model, dilate_px, persons, pixels, ratio, width, height,
                                                   at, job_id} (persons 0 / pixels 0: checked, nobody there)

The reconstruction and mesh manifests (reconstruction.build_manifest, mesh.build_manifest) use them when person removal
is ON for the job: a frame with people gets `depth_url` = the masked depth and an extra `mask_url` (services that know
it also drop the masked pixels from model depth and textures; an older service ignores the key and still gets the
masked sensor depth). OFF, or a frame never checked, sends the raw depth exactly as before. So turning it off is a
setting, redoing it is another run (force), and nothing is lost either way.

ON or OFF for a job: the run's `person_removal` (POST .../reconstruction|mesh body), else the map's setting
(PUT .../person-removal/settings), else the server default PERSON_REMOVAL_DEFAULT (profile / robot setting; false).

    GET  /api/v1/maps/{map}/person-removal            -> {map_name, available, settings, coverage, job}
    POST /api/v1/maps/{map}/person-removal            {session_id?, model?, dilate_px?, force?} -> 202 job (200: the
                                                         same job runs)
    POST /api/v1/maps/{map}/person-removal/cancel     -> the job
    PUT  /api/v1/maps/{map}/person-removal/settings   {enabled: bool|null, model: str|null, dilate_px: int|null}

The job follows packages/api/depth_rebuild.py: in memory (an API restart forgets it; what was written stays and a new
run skips it), one job per map, one map at a time, the same guards (404 map_not_found / session_not_found, 409
map_deleting / session_active / no_depth / job_active) plus 503 not_configured (no PERSON_MASK_SERVICE_URL). After a
job the service is asked to unload its model, so the memory is held only while a run goes on.
"""
import asyncio
import dataclasses
import datetime
import io
import json
import logging
import re
import time
import uuid
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import pydantic
from fastapi import HTTPException

from packages.api import depth_rebuild as dr

logger = logging.getLogger("ApiDelegationService.person_removal")

VERSION = 1                          # of the record and the derivation; a record of another version is "unchecked"
TABLE = "map_person_removal"
MAX_DILATE_PX = 64
MODEL_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

QUEUED, RUNNING = "queued", "running"
SUCCEEDED, FAILED, CANCELLED = "succeeded", "failed", "cancelled"
ACTIVE = (QUEUED, RUNNING)

# frame states in a manifest / the coverage
MASKED, CLEAR, UNCHECKED, OFF = "masked", "clear", "unchecked", "off"

SKIP_DONE = "done_before"
FAIL_RGB = "rgb_missing"
FAIL_DEPTH = "depth_missing"
FAIL_BAD = "unreadable"
FAIL_SEGMENT = "segment_failed"
FAIL_STORE = "store_failed"
FAIL_ERROR = "error"
MAP_CHECK_EVERY = 50
MAX_FAILURES_LISTED = 20

_err = dr._err


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# --- keys --------------------------------------------------------------------------------------------------------

def mask_key(node_id: str, camera: str) -> str:
    return f"{node_id}/person_mask/{camera}.png"


def masked_depth_key(node_id: str, camera: str) -> str:
    return f"{node_id}/depth_masked/{camera}.png"


# --- pure mask rules ---------------------------------------------------------------------------------------------

def decode_mask(png: bytes):
    """bool [H, W] of a mask PNG (any non-zero = person), or ValueError."""
    import numpy as np
    from PIL import Image
    try:
        with Image.open(io.BytesIO(png)) as im:
            arr = np.asarray(im.convert("L"))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"cannot decode the mask: {exc}") from exc
    return arr > 0


def dilate(mask, px: int):
    """The mask grown by `px` pixels in every direction (a (2px+1)^2 square; separable, numpy only)."""
    out = mask.copy()
    if px <= 0 or not mask.any():
        return out
    rows = mask.copy()
    for s in range(1, px + 1):
        rows[s:, :] |= mask[:-s, :]
        rows[:-s, :] |= mask[s:, :]
    out = rows.copy()
    for s in range(1, px + 1):
        out[:, s:] |= rows[:, :-s]
        out[:, :-s] |= rows[:, s:]
    return out


def fit_mask(mask, height: int, width: int):
    """The mask on the depth image's pixel grid. The RGB covers the same field of view as the depth (handover §5.3:
    the RGB is resized to the depth size), so a plain resize; conservative: a depth pixel touched by any person pixel
    is masked."""
    import numpy as np
    from PIL import Image
    if mask.shape == (height, width):
        return mask.copy()
    img = Image.fromarray(np.where(mask, 255, 0).astype(np.uint8), mode="L")
    return np.asarray(img.resize((width, height), Image.BILINEAR)) > 0


def apply_to_depth(depth, mask):
    """A copy of the u16 depth with the masked pixels set to 0 (no data)."""
    out = depth.copy()
    out[mask] = 0
    return out


def encode_png(arr) -> bytes:
    """PNG of a u8 (L) or u16 (16-bit grey) array."""
    import numpy as np
    from PIL import Image
    buf = io.BytesIO()
    if arr.dtype == np.bool_:
        arr = np.where(arr, 255, 0).astype(np.uint8)
    Image.fromarray(np.ascontiguousarray(arr)).save(buf, format="PNG", optimize=arr.dtype == np.uint8)
    return buf.getvalue()


@dataclasses.dataclass
class Derived:
    pixels: int
    width: int
    height: int
    mask_png: Optional[bytes] = None           # None when nobody is in the frame
    depth_png: Optional[bytes] = None

    @property
    def ratio(self) -> float:
        return round(self.pixels / max(1, self.width * self.height), 5)


def derive(mask_png: bytes, depth_png: bytes, dilate_px: int) -> Derived:
    """The stored products of one frame from the service's mask (RGB size) and the raw depth PNG. ValueError on an
    unreadable input."""
    import numpy as np
    depth = dr.decode_depth(depth_png)
    if depth.dtype != np.uint16:
        if depth.min() < 0 or depth.max() > 65535:
            raise ValueError(f"depth is not u16 millimetres: dtype {depth.dtype}")
        depth = depth.astype(np.uint16)
    h, w = depth.shape
    mask = decode_mask(mask_png)
    if not mask.any():
        return Derived(0, w, h)
    mask = dilate(fit_mask(mask, h, w), dilate_px)
    pixels = int(mask.sum())
    return Derived(pixels, w, h, encode_png(mask), encode_png(apply_to_depth(depth, mask)))


def record(derived: Derived, persons: int, model: str, dilate_px: int, job_id: Optional[str] = None) -> Dict[str, Any]:
    rec = {"version": VERSION, "model": model, "dilate_px": int(dilate_px), "persons": int(persons),
           "pixels": derived.pixels, "ratio": derived.ratio, "width": derived.width, "height": derived.height,
           "at": _now_iso()}
    if job_id:
        rec["job_id"] = job_id
    return rec


def frame_state(rec: Any) -> str:
    """masked | clear | unchecked, from a node's `person_mask.{camera}` record."""
    if not isinstance(rec, Mapping) or rec.get("version") != VERSION:
        return UNCHECKED
    return MASKED if int(rec.get("pixels") or 0) > 0 else CLEAR


def is_done(rec: Any, model: str, dilate_px: int) -> bool:
    """The frame was checked with this model and dilation already (a new run skips it unless forced)."""
    return frame_state(rec) != UNCHECKED and rec.get("model") == model and int(rec.get("dilate_px", -1)) == dilate_px


# --- manifests (reconstruction.build_manifest / mesh.build_manifest) ---------------------------------------------

def manifest_camera(cam: Dict[str, Any], node_id: str, camera: str, rec: Any, enabled: bool,
                    presign_get: Callable[[str, str], str], bucket: str) -> str:
    """Point one manifest camera at the masked depth (+ `mask_url`) when removal is on and the frame has people.
    Returns the frame's state (OFF when removal is off)."""
    if not enabled:
        return OFF
    state = frame_state(rec)
    if state == MASKED:
        cam["depth_url"] = presign_get(bucket, masked_depth_key(node_id, camera))
        cam["mask_url"] = presign_get(bucket, mask_key(node_id, camera))
    return state


def summarize(frames: Sequence[Any], enabled: bool) -> Dict[str, Any]:
    """The job's `inputs.person_removal`: {enabled, masked, clear, unchecked} over the frames' cameras."""
    out: Dict[str, Any] = {"enabled": bool(enabled), MASKED: 0, CLEAR: 0, UNCHECKED: 0}
    if not enabled:
        return out
    for f in frames:
        masks = getattr(f, "person_masks", None) or {}
        for camera in f.cameras:
            out[frame_state(masks.get(camera))] += 1
    return out


def strip_params(params: Mapping[str, Any]) -> Dict[str, Any]:
    """The job params without the gateway-only `person_removal` (the services never see it)."""
    return {k: v for k, v in params.items() if k != "person_removal"}


# --- settings (table map_person_removal) -------------------------------------------------------------------------

class _MaskParams(pydantic.BaseModel):
    model: Optional[str] = None             # null: the server's PERSON_REMOVAL_MODEL, else the service's default
    dilate_px: Optional[int] = None         # null: PERSON_REMOVAL_DILATE_PX

    class Config:
        extra = pydantic.Extra.forbid

    @pydantic.validator("model")
    def _model(cls, value):  # noqa: N805 - pydantic v1 validator
        if value is not None and not MODEL_NAME.match(value):
            raise ValueError("1-64 characters of A-Z a-z 0-9 _ . -")
        return value

    @pydantic.validator("dilate_px")
    def _dilate(cls, value):  # noqa: N805
        if value is not None and not 0 <= value <= MAX_DILATE_PX:
            raise ValueError(f"must be between 0 and {MAX_DILATE_PX}")
        return value


class SettingsBody(_MaskParams):
    enabled: Optional[bool] = None          # null: follow the server default


class RunBody(_MaskParams):
    session_id: Optional[str] = None
    force: bool = False

    @pydantic.validator("session_id")
    def _session(cls, value):  # noqa: N805
        if value is not None and (not value.strip() or len(value) > 128):
            raise ValueError("1-128 characters")
        return value


def parse(model: Any, body: Any):
    """The pydantic body or 422 in FastAPI's shape."""
    if body is None:
        body = {}
    if not isinstance(body, Mapping):
        raise HTTPException(422, [{"loc": ["body"], "msg": "expected a JSON object", "type": "type_error.dict"}])
    try:
        return model.parse_obj(dict(body))
    except pydantic.ValidationError as exc:
        raise HTTPException(422, [{"loc": ["body", *e["loc"]], "msg": e["msg"], "type": e["type"]}
                                  for e in exc.errors()])


@dataclasses.dataclass
class Defaults:
    enabled: bool = False
    model: str = ""
    dilate_px: int = 6


def effective(stored: Optional[Mapping[str, Any]], defaults: Defaults) -> Dict[str, Any]:
    """{enabled, model, dilate_px, source}: the map's own choice where it made one, else the server's."""
    stored = stored or {}
    enabled = stored.get("enabled")
    return {"enabled": bool(defaults.enabled if enabled is None else enabled),
            "source": "server" if enabled is None else "map",
            "model": stored.get("model") or defaults.model or None,
            "dilate_px": int(defaults.dilate_px if stored.get("dilate_px") is None else stored["dilate_px"])}


class SettingsStore:
    GET_SQL = f"SELECT settings FROM {TABLE} WHERE map_name = %s"
    UPSERT_SQL = (f"INSERT INTO {TABLE} (map_name, settings, updated_at) VALUES (%s, %s::jsonb, now()) "
                  "ON CONFLICT (map_name) DO UPDATE SET settings = EXCLUDED.settings, updated_at = now() "
                  "RETURNING settings")
    DELETE_SQL = f"DELETE FROM {TABLE} WHERE map_name = %s"

    def __init__(self, db: Any, defaults: Defaults):
        self.db, self.defaults = db, defaults

    async def get(self, map_name: str) -> Optional[Dict[str, Any]]:
        async with self.db.connection() as conn:
            cur = await conn.execute(self.GET_SQL, (map_name,))
            row = await cur.fetchone()
        return dict(row[0]) if row and row[0] else None

    async def put(self, map_name: str, body: SettingsBody) -> Dict[str, Any]:
        doc = body.dict()
        async with self.db.connection() as conn:
            if all(v is None for v in doc.values()):
                await conn.execute(self.DELETE_SQL, (map_name,))
                return {}
            cur = await conn.execute(self.UPSERT_SQL, (map_name, json.dumps(doc)))
            row = await cur.fetchone()
        return dict(row[0])

    async def effective(self, map_name: str) -> Dict[str, Any]:
        try:
            stored = await self.get(map_name)
        except Exception as exc:  # noqa: BLE001  (table missing before the migration, Postgres down): the default
            logger.warning("Person removal settings of map %s unreadable: %s", map_name, exc)
            stored = None
        return effective(stored, self.defaults)

    async def enabled_for(self, map_name: str) -> bool:
        """The gateways' policy hook (reconstruction / mesh `person_removal_policy`)."""
        return bool((await self.effective(map_name))["enabled"])


# --- the person-mask service ---------------------------------------------------------------------------------------

class ServiceError(Exception):
    """`fatal`: the job cannot go on (service down, model missing, auth); else only this frame failed."""

    def __init__(self, code: str, message: str, fatal: bool):
        super().__init__(message)
        self.code, self.message, self.fatal = code, message, fatal


class MaskClient:
    """Synchronous (the job calls it from a worker thread). Retries connection errors and 5xx (not 503) 3 times."""

    RETRIES = (1.0, 3.0, 10.0)

    def __init__(self, base_url: str, key: Optional[str], timeout_s: float = 120.0,
                 sleep: Callable[[float], None] = time.sleep, transport: Any = None):
        import httpx
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        self._client = httpx.Client(base_url=base_url.rstrip("/"), headers=headers, timeout=timeout_s,
                                    transport=transport)
        self._sleep = sleep

    def _call(self, method: str, path: str, **kw):
        import httpx
        last = ""
        for i in range(len(self.RETRIES) + 1):
            try:
                resp = self._client.request(method, path, **kw)
            except httpx.HTTPError as exc:
                last = f"{type(exc).__name__}: {exc}"
            else:
                if resp.status_code < 500 or resp.status_code == 503:
                    return resp
                last = f"HTTP {resp.status_code}"
            if i < len(self.RETRIES):
                self._sleep(self.RETRIES[i])
        raise ServiceError("service_unavailable", f"person-mask service unreachable: {last}", True)

    @staticmethod
    def _detail(resp) -> Tuple[str, str]:
        try:
            detail = resp.json().get("detail")
        except Exception:  # noqa: BLE001
            detail = None
        if isinstance(detail, Mapping):
            return str(detail.get("code") or "error"), str(detail.get("message") or "")
        return "error", f"HTTP {resp.status_code}"

    def health(self) -> Dict[str, Any]:
        resp = self._call("GET", "/health")
        if resp.status_code != 200:
            raise ServiceError("service_unavailable", f"person-mask service health: HTTP {resp.status_code}", True)
        return resp.json()

    def segment(self, image: bytes, model: Optional[str]) -> Tuple[bytes, int, str]:
        """(mask PNG, persons, model) of one image."""
        resp = self._call("POST", "/v1/segment", params={"model": model} if model else None, content=image,
                          headers={"Content-Type": "application/octet-stream"})
        if resp.status_code == 200:
            return resp.content, int(resp.headers.get("X-Person-Count") or 0), resp.headers.get("X-Model") or ""
        code, message = self._detail(resp)
        fatal = resp.status_code in (401, 403, 404, 503)
        raise ServiceError(code, message or f"HTTP {resp.status_code}", fatal)

    def unload(self) -> None:
        try:
            self._call("POST", "/v1/unload")
        except ServiceError as exc:
            logger.info("Person-mask unload: %s", exc.message)

    def close(self) -> None:
        self._client.close()


def resolve_model(health: Mapping[str, Any], wanted: Optional[str]) -> str:
    """The model name the service will use for `wanted` (None: its default). ServiceError(fatal) when it has no such
    model or the model's weights are not there."""
    models = {m.get("name"): m for m in health.get("models") or [] if isinstance(m, Mapping)}
    name = wanted or health.get("default_model")
    if name not in models:
        raise ServiceError("unknown_model", f"the person-mask service has no model '{name}' "
                           f"(it has: {', '.join(str(k) for k in models) or 'none'})", True)
    if models[name].get("present") is False:
        raise ServiceError("model_unavailable", f"the weights of model '{name}' are not mounted on the person-mask "
                           f"service ({models[name].get('path')})", True)
    return str(name)


# --- the job -----------------------------------------------------------------------------------------------------

@dataclasses.dataclass
class Job:
    job_id: str
    map_name: str
    session_id: Optional[str]
    model: Optional[str]                     # requested (None: the effective default)
    dilate_px: int
    force: bool = False
    requested_by: Optional[str] = None
    state: str = QUEUED
    model_used: Optional[str] = None
    requested_at: str = dataclasses.field(default_factory=_now_iso)
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    nodes_total: int = 0
    frames_total: int = 0                    # node-cameras to process
    done: int = 0
    masked: int = 0                          # frames with people (masks written)
    clear: int = 0                           # frames checked, nobody there
    persons: int = 0
    skipped: Dict[str, int] = dataclasses.field(default_factory=dict)
    failed: Dict[str, int] = dataclasses.field(default_factory=dict)
    failures: List[Dict[str, str]] = dataclasses.field(default_factory=list)
    seconds: float = 0.0                     # service time over the processed frames
    warnings: List[str] = dataclasses.field(default_factory=list)
    error: Optional[Dict[str, Any]] = None
    cancel_requested: bool = False

    @property
    def active(self) -> bool:
        return self.state in ACTIVE

    def progress(self) -> float:
        if self.state == SUCCEEDED:
            return 1.0
        return min(1.0, self.done / self.frames_total) if self.frames_total else 0.0

    def fail(self, reason: str, where: str, detail: str = "") -> None:
        self.failed[reason] = self.failed.get(reason, 0) + 1
        if len(self.failures) < MAX_FAILURES_LISTED:
            self.failures.append({"frame": where, "reason": reason, **({"detail": detail[:300]} if detail else {})})

    def view(self) -> Dict[str, Any]:
        processed = self.masked + self.clear
        return {"job_id": self.job_id, "map_name": self.map_name, "session_id": self.session_id,
                "state": self.state, "progress": round(self.progress(), 4), "model": self.model_used or self.model,
                "dilate_px": self.dilate_px, "force": self.force, "requested_by": self.requested_by,
                "requested_at": self.requested_at, "started_at": self.started_at, "finished_at": self.finished_at,
                "nodes_total": self.nodes_total, "frames_total": self.frames_total, "done": self.done,
                "masked": self.masked, "clear": self.clear, "persons": self.persons, "skipped": dict(self.skipped),
                "failed": sum(self.failed.values()), "failed_by_reason": dict(self.failed),
                "failures": list(self.failures),
                "seconds_per_frame": round(self.seconds / processed, 3) if processed else None,
                "warnings": list(self.warnings), "error": self.error, "cancel_requested": self.cancel_requested}


class Sources:
    """The job's I/O. Production: built from the API's clients; tests pass fakes with the same methods.

    Sync: list_nodes(map) -> [doc], get_rgb(map, node_id, camera), get_depth(map, node_id, camera) -> bytes|None,
    store(map, node_id, key, png) -> bool, set_records(map, key, {camera: record}) -> bool, coverage(map) -> dict.
    Async: map_row(map) -> (lifecycle, _)|None, sessions(map, session_id|None) -> [row]."""

    NODES_AQL = ("FOR d IN @@col FILTER d.depth != null RETURN {_key: d._key, node_id: d.node_id, depth: d.depth, "
                 "session_id: d.session_id, person_mask: d.person_mask}")
    COVERAGE_AQL = (
        "FOR d IN @@col FILTER IS_OBJECT(d.depth) FOR cam IN ATTRIBUTES(d.depth) "
        "LET m = IS_OBJECT(d.person_mask) ? d.person_mask[cam] : null "
        "LET ok = IS_OBJECT(m) AND m.version == @version "
        "COLLECT AGGREGATE frames = COUNT(1), checked = SUM(ok ? 1 : 0), "
        "masked = SUM(ok AND m.pixels > 0 ? 1 : 0) RETURN {frames, checked, masked}")

    def __init__(self, db: Any, graph_db: Any, image_db: Any) -> None:
        self.graph_db, self.image_db = graph_db, image_db
        self._dr = dr.Sources(db, graph_db, image_db)

    def list_nodes(self, map_name: str) -> List[Dict[str, Any]]:
        name = f"nodes_{map_name}"
        if not self.graph_db.db.has_collection(name):
            return []
        return list(self.graph_db.db.aql.execute(self.NODES_AQL, bind_vars={"@col": name}))

    def coverage(self, map_name: str) -> Dict[str, int]:
        name = f"nodes_{map_name}"
        if not self.graph_db.db.has_collection(name):
            return {"frames": 0, "checked": 0, "masked": 0}
        rows = list(self.graph_db.db.aql.execute(self.COVERAGE_AQL, bind_vars={"@col": name, "version": VERSION}))
        row = rows[0] if rows else {}
        return {k: int(row.get(k) or 0) for k in ("frames", "checked", "masked")}

    def get_rgb(self, map_name: str, node_id: str, camera: str) -> Optional[bytes]:
        return self.image_db.get_image(camera, node_id, map_name)

    def get_depth(self, map_name: str, node_id: str, camera: str) -> Optional[bytes]:
        return self.image_db.get_depth(node_id, camera, map_name)

    def store(self, map_name: str, node_id: str, key: str, png: bytes) -> bool:
        return bool(self.image_db.store_node_png(png, key, node_id, map_name, metadata={"source": "person_removal"}))

    def set_records(self, map_name: str, key: str, records: Dict[str, Dict[str, Any]]) -> bool:
        return bool(self.graph_db.set_node_person_masks(map_name, key, records))

    async def map_row(self, map_name: str):
        return await self._dr.map_row(map_name)

    async def sessions(self, map_name: str, session_id: Optional[str]):
        return await self._dr.sessions(map_name, session_id)


def frames_to_process(nodes: Sequence[Mapping[str, Any]], session_id: Optional[str], model: str, dilate_px: int,
                      force: bool) -> Tuple[List[Tuple[Mapping[str, Any], str]], Dict[str, int]]:
    """([(node, camera)] to segment, {skip reason: count})."""
    todo, skipped = [], {}
    for n in nodes:
        if session_id is not None and dr.node_session(n) != session_id:
            continue
        masks = n.get("person_mask") if isinstance(n.get("person_mask"), Mapping) else {}
        for camera in dr.depth_cameras(n):
            if not force and is_done(masks.get(camera), model, dilate_px):
                skipped[SKIP_DONE] = skipped.get(SKIP_DONE, 0) + 1
            else:
                todo.append((n, camera))
    return todo, skipped


class PersonRemoval:
    def __init__(self, sources: Any, client: Optional[MaskClient], settings: Any, *,
                 to_thread: Callable[..., Any] = asyncio.to_thread, unload_after: bool = True):
        self.sources, self.client, self.settings = sources, client, settings
        self._to_thread, self.unload_after = to_thread, unload_after
        self.jobs: Dict[str, Job] = {}
        self.tasks: Dict[str, asyncio.Task] = {}
        self._slot: Optional[asyncio.Semaphore] = None

    @property
    def available(self) -> bool:
        return self.client is not None

    def _semaphore(self) -> asyncio.Semaphore:
        if self._slot is None:
            self._slot = asyncio.Semaphore(1)
        return self._slot

    async def _check_map(self, map_name: str) -> None:
        row = await self.sources.map_row(map_name)
        if row is None:
            raise _err(404, "map_not_found", f"Map '{map_name}' not found")
        if row[0] == "DELETING":
            raise _err(409, "map_deleting", f"Map '{map_name}' is being deleted")

    # --- requests ---

    async def status(self, map_name: str) -> Dict[str, Any]:
        await self._check_map(map_name)
        job = self.jobs.get(map_name)
        coverage = None
        if job is None or not job.active:
            try:
                coverage = await self._to_thread(self.sources.coverage, map_name)
            except Exception as exc:  # noqa: BLE001  ArangoDB down: coverage unknown
                logger.warning("Person removal coverage of map %s: %s", map_name, exc)
        return {"map_name": map_name, "available": self.available,
                "settings": await self.settings.effective(map_name), "stored_settings": await self._stored(map_name),
                "coverage": coverage, "job": job.view() if job else None}

    async def _stored(self, map_name: str) -> Optional[Dict[str, Any]]:
        try:
            return await self.settings.get(map_name)
        except Exception:  # noqa: BLE001
            return None

    async def put_settings(self, map_name: str, body: Any) -> Dict[str, Any]:
        parsed = parse(SettingsBody, body)
        await self._check_map(map_name)
        await self.settings.put(map_name, parsed)
        return {"map_name": map_name, "settings": await self.settings.effective(map_name),
                "stored_settings": await self._stored(map_name)}

    async def start(self, map_name: str, body: Any, requested_by: Optional[str] = None) -> Tuple[int, Dict[str, Any]]:
        parsed = parse(RunBody, body)
        if self.client is None:
            raise _err(503, "not_configured", "Person removal is not set up on this server (PERSON_MASK_SERVICE_URL)")
        session_id = parsed.session_id
        running = self.jobs.get(map_name)
        if running is not None and running.active:
            if running.session_id == session_id:
                return 200, running.view()
            raise _err(409, "job_active", "Person removal already runs on this map", job=running.view())
        await self._check_map(map_name)
        rows = await self.sources.sessions(map_name, session_id)
        if session_id is not None and not rows:
            raise _err(404, "session_not_found", f"Session '{session_id}' not found on map '{map_name}'")
        live = [r for r in rows if dr.recording(r)]
        if live:
            raise _err(409, "session_active", "A mapping session is still recording on this map: pause or finish "
                       "it first", sessions=[str(r["session_id"]) for r in live])
        nodes = await self._to_thread(self.sources.list_nodes, map_name)
        if not any(session_id is None or dr.node_session(n) == session_id for n in nodes):
            raise _err(409, "no_depth", "No node in scope has a depth image: nothing to remove people from")
        running = self.jobs.get(map_name)
        if running is not None and running.active:
            if running.session_id == session_id:
                return 200, running.view()
            raise _err(409, "job_active", "Person removal already runs on this map", job=running.view())
        eff = await self.settings.effective(map_name)
        job = Job(str(uuid.uuid4()), map_name, session_id, parsed.model or eff["model"],
                  eff["dilate_px"] if parsed.dilate_px is None else parsed.dilate_px, parsed.force, requested_by)
        if not eff["enabled"]:
            job.warnings.append("Person removal is off for this map: the masks are written, but the 3D "
                                "reconstruction and mesh use them only once it is switched on")
        self.jobs[map_name] = job
        self.tasks[map_name] = asyncio.get_running_loop().create_task(self._run(job, nodes))
        logger.info("Person removal %s: map %s session %s queued", job.job_id, map_name, session_id)
        return 202, job.view()

    def cancel(self, map_name: str) -> Dict[str, Any]:
        job = self.jobs.get(map_name)
        if job is None or not job.active:
            raise _err(404, "no_active_job", f"No person removal runs on map '{map_name}'")
        job.cancel_requested = True
        return job.view()

    async def stop(self) -> None:
        for task in list(self.tasks.values()):
            task.cancel()
        for task in list(self.tasks.values()):
            try:
                await task
            except BaseException:  # noqa: BLE001
                pass

    # --- the run ---

    def _end(self, job: Job, state: str, error: Optional[Dict[str, Any]] = None) -> None:
        job.state, job.finished_at = state, _now_iso()
        if error is not None:
            job.error = error
        logger.info("Person removal %s (map %s) %s: masked %d, clear %d, skipped %s, failed %s", job.job_id,
                    job.map_name, state, job.masked, job.clear, job.skipped, job.failed)

    async def _run(self, job: Job, nodes: List[Dict[str, Any]]) -> None:
        try:
            async with self._semaphore():
                if job.cancel_requested:
                    return self._end(job, CANCELLED)
                job.state, job.started_at = RUNNING, _now_iso()
                try:
                    await self._process(job, nodes)
                finally:
                    if self.unload_after:
                        await self._to_thread(self.client.unload)
        except asyncio.CancelledError:
            if job.active:
                self._end(job, CANCELLED, {"reason": "stopped", "message": "the API stopped"})
            raise
        except ServiceError as exc:
            self._end(job, FAILED, {"reason": exc.code, "message": exc.message})
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
            self._end(job, FAILED, {"reason": detail.get("code", "error"), "message": detail.get("message")})
        except Exception as exc:  # noqa: BLE001  never raises into the loop
            logger.exception("Person removal %s (map %s) failed", job.job_id, job.map_name)
            self._end(job, FAILED, {"reason": "error", "message": f"{type(exc).__name__}: {exc}"[:2000]})

    async def _process(self, job: Job, nodes: List[Dict[str, Any]]) -> None:
        health = await self._to_thread(self.client.health)
        job.model_used = resolve_model(health, job.model)
        todo, job.skipped = frames_to_process(nodes, job.session_id, job.model_used, job.dilate_px, job.force)
        job.nodes_total, job.frames_total = len(nodes), len(todo)
        for i, (node, camera) in enumerate(todo):
            if job.cancel_requested:
                return self._end(job, CANCELLED)
            if i and i % MAP_CHECK_EVERY == 0:
                await self._check_map(job.map_name)
            await self._to_thread(self._one, job, node, camera)
            job.done += 1
        if job.frames_total and job.masked + job.clear == 0:
            return self._end(job, FAILED, {"reason": "nothing_written",
                                           "message": "No frame could be checked (see failures)"})
        return self._end(job, SUCCEEDED)

    def _one(self, job: Job, node: Mapping[str, Any], camera: str) -> None:
        """Segment and store one frame (worker thread). Frame failures are counted; a fatal service error raises."""
        key = str(node.get("_key"))
        node_id = str(node.get("node_id") or key)
        where = f"{node_id}/{camera}"
        try:
            rgb = self.sources.get_rgb(job.map_name, node_id, camera)
            if rgb is None:
                return job.fail(FAIL_RGB, where)
            depth = self.sources.get_depth(job.map_name, node_id, camera)
            if depth is None:
                return job.fail(FAIL_DEPTH, where)
            t0 = time.monotonic()
            try:
                mask_png, persons, model = self.client.segment(rgb, job.model_used)
            except ServiceError as exc:
                if exc.fatal:
                    raise
                return job.fail(FAIL_SEGMENT, where, exc.message)
            job.seconds += time.monotonic() - t0
            try:
                derived = derive(mask_png, depth, job.dilate_px)
            except ValueError as exc:
                return job.fail(FAIL_BAD, where, str(exc))
            if derived.pixels:
                # the PNGs first: the record is what makes a manifest use them
                if not (self.sources.store(job.map_name, node_id, mask_key(node_id, camera), derived.mask_png)
                        and self.sources.store(job.map_name, node_id, masked_depth_key(node_id, camera),
                                               derived.depth_png)):
                    return job.fail(FAIL_STORE, where, "storing a PNG failed")
            rec = record(derived, persons, model or job.model_used, job.dilate_px, job.job_id)
            if not self.sources.set_records(job.map_name, key, {camera: rec}):
                return job.fail(FAIL_STORE, where, "writing the node record failed")
            if derived.pixels:
                job.masked += 1
                job.persons += persons
            else:
                job.clear += 1
            return None
        except ServiceError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("Person removal %s: frame %s failed: %s", job.job_id, where, exc)
            return job.fail(FAIL_ERROR, where, f"{type(exc).__name__}: {exc}")


def create(db: Any, graph_db: Any, image_db: Any, cfg: Any) -> Tuple[PersonRemoval, SettingsStore]:
    """The API's controller and settings store from packages/config.py values."""
    defaults = Defaults(bool(cfg.PERSON_REMOVAL_DEFAULT), cfg.PERSON_REMOVAL_MODEL or "",
                        int(cfg.PERSON_REMOVAL_DILATE_PX))
    store = SettingsStore(db, defaults)
    client = (MaskClient(cfg.PERSON_MASK_SERVICE_URL, cfg.PERSON_MASK_SERVICE_KEY, cfg.PERSON_MASK_TIMEOUT_S)
              if cfg.PERSON_MASK_SERVICE_URL else None)
    return PersonRemoval(Sources(db, graph_db, image_db), client, store), store
