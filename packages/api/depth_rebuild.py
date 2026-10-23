"""Scene graph and occupancy grid of a map from depth only: the operator's way to (re)create them
when the robot uploaded no costmap during topomap (docs/scene_graph/integration.md, "Backfill").

The scene-graph worker (graph-builder, packages/services/graph_builder/scene_leaf.py) already folds
the `occupancy_depth` layer when a node has no robot `occupancy` layer (LAYER_PRIORITY); the
occupancy grid (packages/api/occupancy_reads.py) is that fold's V plane. What was missing is the
writer: until now the layer was produced offline by tools/scene_graph_sim/depth_occupancy_backfill.py
+ write_occupancy_depth.py. This module is the same rule and the same record, run by the API:

    POST /api/v1/maps/{map}/scene-graph/rebuild-from-depth   {session_id?}  -> 202 job (200: same job runs)
    GET  /api/v1/maps/{map}/scene-graph/rebuild-from-depth                  -> {map_name, worker_enabled, job}
    POST /api/v1/maps/{map}/scene-graph/rebuild-from-depth/cancel           -> the job

Per node in scope (the map, or one of its sessions) that has a depth record and NO occupancy-role
layer (`occupancy` from the robot, or an earlier `occupancy_depth`): read its depth PNG, build the
200 x 200 @ 0.05 m window with packages.scene_graph.depth_occupancy.occupancy_from_depth (the window
centred on the node and aligned to the lattice, as the offline tool does), store the PNG at
`{node}/costmap/occupancy_depth.png` FIRST, then the record via `set_node_costmaps` (which stamps
`costmap_stored_ms`, the worker's ingest cursor, so the worker picks the node up by itself).
A robot costmap is never touched, and a node that has either layer is skipped: running it again
writes nothing new (idempotent), and an interrupted run is resumed by running it again.

Phases: `queued` (another map's job holds the one generation slot) -> `generating` (progress =
nodes handled / nodes to generate) -> `folding` (waits until the scene graph's stored cursor,
`sg_meta.cursor`, reaches the newest occupancy record in scope, or `fold_wait_s`) -> `succeeded`
| `failed` | `cancelled`. `fold.state`: caught_up | timeout | worker_off (SCENE_GRAPH_WORKER_ENABLED
off here) | map_disabled (the map's per-map switch is off) | not_needed (no occupancy record in scope).
A timeout or a switched-off worker is a warning, not a failure: the layers are written and the
worker folds them once it runs.

Guards: 404 map_not_found / session_not_found, 409 map_deleting, 409 session_active (an open,
unpaused MAPPING session in scope: the robot may still upload; pause or finish it first), 409
no_depth (no node in scope has a depth record), 409 job_active (a job with another scope runs on
this map; the same scope answers 200 with the running job instead). Errors are
`{"detail": {"code", "message", ...}}` like packages/api/reconstruction.py.

The registry is IN MEMORY (as packages/api/reloc_job.py): an API restart forgets the job (GET then
answers job null) and stops it; what was written stays written, and a new run continues. One job
per map; generation runs one map at a time (CPU and memory bound, numpy in a worker thread).
"""
import asyncio
import dataclasses
import datetime
import hashlib
import io
import logging
import math
import time
import uuid
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from fastapi import HTTPException

from packages.utils import map_sessions as ms

logger = logging.getLogger("ApiDelegationService.depth_rebuild")

LAYER = "occupancy_depth"
ROBOT_LAYER = "occupancy"
OCCUPANCY_LAYERS = (ROBOT_LAYER, LAYER)       # the occupancy role (scene_leaf.DEFAULT_ROLES)
RES = 0.05
SIZE = 200
VERSION = "depth_to_occupancy/2"              # tools/scene_graph_sim/depth_occupancy_backfill.py
ROBOT_HEIGHT_M = 0.5
FLOOR_TOLERANCE_M = 0.08
GENERATOR = {"version": VERSION, "robot_height_m": ROBOT_HEIGHT_M, "floor_tolerance_m": FLOOR_TOLERANCE_M,
             "floor": "per-leaf dominant plane near the base", "free": "camera-to-point rays and floor points",
             "unknown": "no depth evidence", "min_points": 1}

QUEUED, GENERATING, FOLDING = "queued", "generating", "folding"
SUCCEEDED, FAILED, CANCELLED = "succeeded", "failed", "cancelled"
ACTIVE = (QUEUED, GENERATING, FOLDING)

# skip reasons (per node, counted)
SKIP_HAS_COSTMAP = "has_costmap"              # the robot's own `occupancy` layer: always wins
SKIP_HAS_DEPTH_LAYER = "has_depth_layer"      # done before (idempotency)
SKIP_NO_DEPTH = "no_depth"
SKIP_OTHER_SESSION = "other_session"
# failure reasons (per node, counted; the job goes on)
FAIL_NO_POSE3D = "no_pose3d"                  # depth record without pose3d_map (robot sent no robot_pose3d)
FAIL_NO_PNG = "depth_png_missing"
FAIL_BAD_PNG = "depth_png_unreadable"
FAIL_STORE = "store_failed"
FAIL_ERROR = "error"

FOLD_WAIT_S = 600.0
FOLD_POLL_S = 2.0
MAP_CHECK_EVERY = 50                          # nodes between two map lifecycle checks
MAX_FAILURES_LISTED = 20


def _err(status: int, code: str, message: str, **extra: Any) -> HTTPException:
    return HTTPException(status, {"code": code, "message": message, **extra})


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# --- pure rules ---------------------------------------------------------------------------------

def node_session(node: Mapping[str, Any]) -> Optional[str]:
    """The session a node was recorded in: the node's own, else its depth records'."""
    sid = node.get("session_id")
    if sid:
        return str(sid)
    for rec in (node.get("depth") or {}).values():
        if isinstance(rec, Mapping) and rec.get("session_id"):
            return str(rec["session_id"])
    return None


def layers_of(node: Mapping[str, Any]) -> List[str]:
    """The stored costmap layer names of a node (`costmap_layers` from the listing, or `costmap`)."""
    if "costmap_layers" in node:
        return [str(x) for x in (node.get("costmap_layers") or [])]
    return [str(x) for x in (node.get("costmap") or {})]


def classify(node: Mapping[str, Any], session_id: Optional[str] = None) -> Optional[str]:
    """None when the node is to be generated, else its skip reason."""
    if session_id is not None and node_session(node) != str(session_id):
        return SKIP_OTHER_SESSION
    layers = layers_of(node)
    if ROBOT_LAYER in layers:
        return SKIP_HAS_COSTMAP
    if LAYER in layers:
        return SKIP_HAS_DEPTH_LAYER
    if not node.get("depth"):
        return SKIP_NO_DEPTH
    return None


def window_origin(x: float, y: float, res: float = RES, size: int = SIZE) -> Tuple[float, float]:
    """Cell (0, 0) of the node's window: centred on the pose, aligned to the lattice."""
    return (math.floor(x / res) - size // 2) * res, (math.floor(y / res) - size // 2) * res


def depth_cameras(node: Mapping[str, Any]) -> List[str]:
    """The node's depth cameras in the order they are tried (name order, as the offline tool)."""
    return sorted(k for k, v in (node.get("depth") or {}).items() if isinstance(v, Mapping))


def decode_depth(png: bytes):
    """u16 depth [rows, cols] of a PNG, or ValueError."""
    import numpy as np
    from PIL import Image
    try:
        with Image.open(io.BytesIO(png)) as im:
            arr = np.array(im)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"cannot decode: {exc}") from exc
    if arr.ndim != 2 or arr.size == 0:
        raise ValueError(f"not a single-channel image: shape {arr.shape}")
    return arr


def build_layer(node: Mapping[str, Any], camera: str, depth_png: bytes,
                job_id: Optional[str] = None) -> Tuple[bytes, Dict[str, Any]]:
    """(PNG bytes, `costmap.occupancy_depth` record) of one node from one camera's depth.
    Raises ValueError (reason in args[0]: FAIL_NO_POSE3D / FAIL_BAD_PNG) when it cannot be built."""
    import numpy as np
    from PIL import Image
    from packages.scene_graph.depth_occupancy import occupancy_from_depth
    rec = (node.get("depth") or {}).get(camera) or {}
    cam = rec.get("camera") or {}
    pose3d = rec.get("pose3d_map")
    if not isinstance(pose3d, Mapping) or not isinstance(cam.get("T_base_cam"), Mapping):
        raise ValueError(FAIL_NO_POSE3D)
    try:
        depth = decode_depth(depth_png)
    except ValueError as exc:
        raise ValueError(FAIL_BAD_PNG, str(exc)) from exc
    pose = node.get("pose") or {}
    px, py = float(pose.get("x", pose3d["x"])), float(pose.get("y", pose3d["y"]))
    ox, oy = window_origin(px, py)
    window = occupancy_from_depth(depth, float(rec.get("depth_scale") or 0.001), cam, cam["T_base_cam"], pose3d,
                                  (ox, oy), (SIZE, SIZE), RES, ROBOT_HEIGHT_M, FLOOR_TOLERANCE_M)
    buf = io.BytesIO()
    Image.fromarray(np.ascontiguousarray(window, dtype=np.uint8), mode="L").save(buf, format="PNG", optimize=True)
    png = buf.getvalue()
    record = {
        "layer": LAYER, "width": SIZE, "height": SIZE, "resolution": RES, "origin": {"x": ox, "y": oy, "yaw": 0.0},
        "frame": "map", "costmap_stamp_ms": rec.get("depth_stamp_ms"), "keyframe_stamp_ms": rec.get("rgb_stamp_ms"),
        "origin_map": {"x": ox, "y": oy, "yaw": 0.0}, "session_id": rec.get("session_id") or node.get("session_id"),
        "source": "depth", "camera": camera, "generator": dict(GENERATOR),
        "png_sha256": hashlib.sha256(png).hexdigest()}
    if job_id:
        record["job_id"] = job_id
    return png, record


def cursor_of(raw: Any) -> Optional[Tuple[int, str]]:
    """`sg_meta.cursor` ([ms, key]) as a tuple, or None (as scene_worker._cursor_of)."""
    if raw is None:
        return None
    try:
        return int(raw[0]), str(raw[1])
    except (TypeError, ValueError, IndexError):
        return None


def fold_target(nodes: Sequence[Mapping[str, Any]], session_id: Optional[str]) -> Optional[Tuple[int, str]]:
    """The newest `(costmap_stored_ms, key)` among the nodes in scope that have an occupancy-role layer:
    the scene graph holds them all once its cursor reaches it. None when there is none."""
    best = None
    for n in nodes:
        if session_id is not None and node_session(n) != str(session_id):
            continue
        if not any(layer in layers_of(n) for layer in OCCUPANCY_LAYERS):
            continue
        ms_ = n.get("costmap_stored_ms")
        if not isinstance(ms_, (int, float)) or isinstance(ms_, bool) or ms_ <= 0:
            continue
        item = (int(ms_), str(n.get("_key", "")))
        best = item if best is None or item > best else best
    return best


# --- the job ------------------------------------------------------------------------------------

@dataclasses.dataclass
class Job:
    job_id: str
    map_name: str
    session_id: Optional[str]
    requested_by: Optional[str] = None
    state: str = QUEUED
    requested_at: str = dataclasses.field(default_factory=_now_iso)
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    nodes_total: int = 0
    to_generate: int = 0
    done: int = 0                       # handled among to_generate (generated + failed)
    generated: int = 0
    skipped: Dict[str, int] = dataclasses.field(default_factory=dict)
    failed: Dict[str, int] = dataclasses.field(default_factory=dict)
    failures: List[Dict[str, str]] = dataclasses.field(default_factory=list)
    fold: Dict[str, Any] = dataclasses.field(default_factory=dict)
    warnings: List[str] = dataclasses.field(default_factory=list)
    error: Optional[Dict[str, Any]] = None
    cancel_requested: bool = False

    @property
    def active(self) -> bool:
        return self.state in ACTIVE

    def progress(self) -> float:
        if self.state in (SUCCEEDED,):
            return 1.0
        if not self.to_generate:
            return 1.0 if self.state == FOLDING else 0.0
        return min(1.0, self.done / self.to_generate)

    def fail(self, reason: str, key: str, detail: str = "") -> None:
        self.failed[reason] = self.failed.get(reason, 0) + 1
        if len(self.failures) < MAX_FAILURES_LISTED:
            self.failures.append({"node": key, "reason": reason, **({"detail": detail[:300]} if detail else {})})

    def view(self) -> Dict[str, Any]:
        return {"job_id": self.job_id, "map_name": self.map_name, "session_id": self.session_id,
                "state": self.state, "progress": round(self.progress(), 4),
                "requested_by": self.requested_by, "requested_at": self.requested_at,
                "started_at": self.started_at, "finished_at": self.finished_at,
                "nodes_total": self.nodes_total, "to_generate": self.to_generate, "done": self.done,
                "generated": self.generated, "skipped": dict(self.skipped),
                "failed": sum(self.failed.values()), "failed_by_reason": dict(self.failed),
                "failures": list(self.failures), "fold": dict(self.fold), "warnings": list(self.warnings),
                "error": self.error, "cancel_requested": self.cancel_requested}


class Sources:
    """The I/O the job needs. Production: `from_service`; tests pass fakes with the same methods.

    Sync (run in a worker thread): list_nodes(map) -> [doc], get_depth(map, node_id, camera) -> bytes|None,
    store_costmap(map, key, png) -> bool, set_costmap(map, key, record) -> bool, fold_cursor(map) -> raw|None.
    Async: map_row(map) -> (lifecycle, scene_graph_enabled)|None, sessions(map, session_id|None) -> [row]."""

    NODES_AQL = (
        "FOR d IN @@col RETURN {_key: d._key, node_id: d.node_id, pose: d.pose, depth: d.depth, "
        "session_id: d.session_id, costmap_stored_ms: d.costmap_stored_ms, "
        "costmap_layers: IS_OBJECT(d.costmap) ? ATTRIBUTES(d.costmap) : []}")
    MAP_SQL = ("SELECT lifecycle, status->'scene_graph_enabled' FROM mapobjectv1 "
               "WHERE name = %s AND lifecycle <> 'DELETED'")
    SESSIONS_SQL = ("SELECT session_id::text, purpose, paused_at, ended_at FROM map_sessions "
                    "WHERE map_name = %s")

    def __init__(self, db: Any, graph_db: Any, image_db: Any) -> None:
        self.db, self.graph_db, self.image_db = db, graph_db, image_db

    def list_nodes(self, map_name: str) -> List[Dict[str, Any]]:
        name = f"nodes_{map_name}"
        if not self.graph_db.db.has_collection(name):
            return []
        return list(self.graph_db.db.aql.execute(self.NODES_AQL, bind_vars={"@col": name}))

    def get_depth(self, map_name: str, node_id: str, camera: str) -> Optional[bytes]:
        return self.image_db.get_depth(node_id, camera, map_name)

    def store_costmap(self, map_name: str, key: str, png: bytes) -> bool:
        return bool(self.image_db.store_costmap(png, key, LAYER, map_name,
                                                metadata={"source": "depth", "layer": LAYER}))

    def set_costmap(self, map_name: str, key: str, record: Dict[str, Any]) -> bool:
        return bool(self.graph_db.set_node_costmaps(map_name, key, {LAYER: record}))

    def fold_cursor(self, map_name: str) -> Any:
        name = f"sg_meta_{map_name}"
        db = self.graph_db.db
        if not db.has_collection(name):
            return None
        doc = db.collection(name).get("meta")
        return None if doc is None else doc.get("cursor")

    async def map_row(self, map_name: str) -> Optional[Tuple[str, Any]]:
        async with self.db.connection() as conn:
            cur = await conn.execute(self.MAP_SQL, (map_name,))
            row = await cur.fetchone()
        return (row[0], row[1]) if row else None

    async def sessions(self, map_name: str, session_id: Optional[str]) -> List[Dict[str, Any]]:
        sql, args = self.SESSIONS_SQL, [map_name]
        if session_id is not None:
            sql += " AND session_id::text = %s"
            args.append(str(session_id))
        else:
            sql += " AND ended_at IS NULL"
        async with self.db.connection() as conn:
            cur = await conn.execute(sql, tuple(args))
            rows = await cur.fetchall()
        return [dict(zip(("session_id", "purpose", "paused_at", "ended_at"), r)) for r in rows]


def recording(row: Mapping[str, Any]) -> bool:
    """An open, unpaused mapping session: the robot may still be adding nodes and costmaps."""
    return ms.session_state(row) == "mapping"


class DepthRebuild:
    def __init__(self, sources: Any, worker_enabled: bool, *, fold_wait_s: float = FOLD_WAIT_S,
                 fold_poll_s: float = FOLD_POLL_S, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Any] = asyncio.sleep, to_thread: Callable[..., Any] = asyncio.to_thread):
        self.sources, self.worker_enabled = sources, bool(worker_enabled)
        self.fold_wait_s, self.fold_poll_s = fold_wait_s, fold_poll_s
        self._clock, self._sleep, self._to_thread = clock, sleep, to_thread
        self.jobs: Dict[str, Job] = {}                 # map -> its latest job
        self.tasks: Dict[str, asyncio.Task] = {}
        self._slot: Optional[asyncio.Semaphore] = None  # one map generates at a time

    def _semaphore(self) -> asyncio.Semaphore:
        if self._slot is None:
            self._slot = asyncio.Semaphore(1)
        return self._slot

    # --- requests ---

    async def _check_map(self, map_name: str) -> Any:
        row = await self.sources.map_row(map_name)
        if row is None:
            raise _err(404, "map_not_found", f"Map '{map_name}' not found")
        if row[0] == "DELETING":
            raise _err(409, "map_deleting", f"Map '{map_name}' is being deleted")
        return row[1]

    async def start(self, map_name: str, session_id: Optional[str] = None,
                    requested_by: Optional[str] = None) -> Tuple[int, Dict[str, Any]]:
        """(HTTP status, job view): 202 a new job, 200 the job of the same scope already running."""
        session_id = str(session_id) if session_id else None
        running = self.jobs.get(map_name)
        if running is not None and running.active:
            if running.session_id == session_id:
                return 200, running.view()
            raise _err(409, "job_active", "A rebuild from depth of this map is already running",
                       job=running.view())
        enabled = await self._check_map(map_name)
        rows = await self.sources.sessions(map_name, session_id)
        if session_id is not None and not rows:
            raise _err(404, "session_not_found", f"Session '{session_id}' not found on map '{map_name}'")
        live = [r for r in rows if recording(r)]
        if live:
            raise _err(409, "session_active",
                       "A mapping session is still recording on this map: pause or finish it first",
                       sessions=[str(r["session_id"]) for r in live])
        nodes = await self._to_thread(self.sources.list_nodes, map_name)
        in_scope = [n for n in nodes if session_id is None or node_session(n) == session_id]
        if not any(n.get("depth") for n in in_scope):
            raise _err(409, "no_depth", "No node in scope has a depth image: nothing to build the "
                       "occupancy from")
        running = self.jobs.get(map_name)        # a concurrent start won while we read
        if running is not None and running.active:
            if running.session_id == session_id:
                return 200, running.view()
            raise _err(409, "job_active", "A rebuild from depth of this map is already running",
                       job=running.view())
        job = Job(str(uuid.uuid4()), map_name, session_id, requested_by)
        if not self.worker_enabled:
            job.warnings.append("The scene-graph worker is off on this server: the layers are written, "
                                "the scene graph and occupancy grid are built once it is on")
        elif enabled is False:
            job.warnings.append("The scene graph is switched off for this map: the layers are written, "
                                "the scene graph is built once it is switched on")
        self.jobs[map_name] = job
        self.tasks[map_name] = asyncio.get_running_loop().create_task(self._run(job, nodes, enabled))
        logger.info("Rebuild from depth %s: map %s session %s queued", job.job_id, map_name, session_id)
        return 202, job.view()

    def status(self, map_name: str) -> Dict[str, Any]:
        job = self.jobs.get(map_name)
        return {"map_name": map_name, "worker_enabled": self.worker_enabled,
                "job": job.view() if job else None}

    def cancel(self, map_name: str) -> Dict[str, Any]:
        job = self.jobs.get(map_name)
        if job is None or not job.active:
            raise _err(404, "no_active_job", f"No rebuild from depth runs on map '{map_name}'")
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
        logger.info("Rebuild from depth %s (map %s) %s: generated %d, skipped %s, failed %s", job.job_id,
                    job.map_name, state, job.generated, job.skipped, job.failed)

    async def _run(self, job: Job, nodes: List[Dict[str, Any]], enabled: Any) -> None:
        try:
            async with self._semaphore():
                if job.cancel_requested:
                    return self._end(job, CANCELLED)
                job.state, job.started_at = GENERATING, _now_iso()
                ok = await self._generate(job, nodes)
            if not ok:
                return
            if job.cancel_requested:
                return self._end(job, CANCELLED)
            if job.to_generate and job.generated == 0:
                return self._end(job, FAILED, {"reason": "nothing_written",
                                               "message": "No node's occupancy could be built from depth "
                                                          "(see failures)"})
            await self._fold(job, enabled)
        except asyncio.CancelledError:
            if job.active:
                self._end(job, CANCELLED, {"reason": "stopped", "message": "the API stopped"})
            raise
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
            self._end(job, FAILED, {"reason": detail.get("code", "error"), "message": detail.get("message")})
        except Exception as exc:  # noqa: BLE001  never raises into the loop
            logger.exception("Rebuild from depth %s (map %s) failed", job.job_id, job.map_name)
            self._end(job, FAILED, {"reason": "error", "message": f"{type(exc).__name__}: {exc}"[:2000]})

    async def _generate(self, job: Job, nodes: List[Dict[str, Any]]) -> bool:
        """Write the layer on every node to generate. False when the job ended here."""
        todo = []
        job.nodes_total = len(nodes)
        for n in nodes:
            reason = classify(n, job.session_id)
            if reason is None:
                todo.append(n)
            elif reason != SKIP_OTHER_SESSION:
                job.skipped[reason] = job.skipped.get(reason, 0) + 1
        job.to_generate = len(todo)
        for i, node in enumerate(todo):
            if job.cancel_requested:
                self._end(job, CANCELLED)
                return False
            if i and i % MAP_CHECK_EVERY == 0:
                await self._check_map(job.map_name)
            await self._to_thread(self._one, job, node)
            job.done += 1
        return True

    def _one(self, job: Job, node: Mapping[str, Any]) -> None:
        """Generate and write one node (worker thread). Failures are counted on the job."""
        key = str(node.get("_key"))
        node_id = str(node.get("node_id") or key)
        reason, detail = FAIL_ERROR, "no depth camera record"
        try:
            for camera in depth_cameras(node):
                if not isinstance(node["depth"][camera].get("pose3d_map"), Mapping):
                    reason, detail = FAIL_NO_POSE3D, f"camera {camera}"
                    continue                # checked before the download: nothing to build from
                png = self.sources.get_depth(job.map_name, node_id, camera)
                if png is None:
                    reason, detail = FAIL_NO_PNG, f"camera {camera}"
                    continue
                try:
                    layer_png, record = build_layer(node, camera, png, job.job_id)
                except ValueError as exc:
                    reason = exc.args[0] if exc.args and exc.args[0] in (FAIL_NO_POSE3D, FAIL_BAD_PNG) else FAIL_ERROR
                    detail = str(exc.args[1]) if len(exc.args) > 1 else ""
                    continue
                # the PNG first: the record makes the worker fetch it
                if not self.sources.store_costmap(job.map_name, key, layer_png):
                    return job.fail(FAIL_STORE, key, "storing the PNG failed")
                if not self.sources.set_costmap(job.map_name, key, record):
                    return job.fail(FAIL_STORE, key, "writing the node record failed")
                job.generated += 1
                return None
        except Exception as exc:  # noqa: BLE001
            logger.warning("Rebuild from depth %s: node %s failed: %s", job.job_id, key, exc)
            return job.fail(FAIL_ERROR, key, f"{type(exc).__name__}: {exc}")
        return job.fail(reason, key, detail)

    async def _fold(self, job: Job, enabled: Any) -> None:
        """Wait (bounded) for the scene graph to hold every occupancy record in scope."""
        job.state = FOLDING
        nodes = await self._to_thread(self.sources.list_nodes, job.map_name)
        target = fold_target(nodes, job.session_id)
        job.fold = {"target": list(target) if target else None, "cursor": None}
        if target is None:
            job.fold["state"] = "not_needed"
            return self._end(job, SUCCEEDED)
        if not self.worker_enabled:
            job.fold["state"] = "worker_off"
            return self._end(job, SUCCEEDED)
        if enabled is False:
            job.fold["state"] = "map_disabled"
            return self._end(job, SUCCEEDED)
        deadline = self._clock() + self.fold_wait_s
        while True:
            try:
                cursor = cursor_of(await self._to_thread(self.sources.fold_cursor, job.map_name))
            except Exception as exc:  # noqa: BLE001  a read failure is retried until the deadline
                logger.warning("Rebuild from depth %s: scene graph cursor read failed: %s", job.job_id, exc)
                cursor = None
            job.fold["cursor"] = list(cursor) if cursor else None
            if cursor is not None and cursor >= target:
                job.fold["state"] = "caught_up"
                return self._end(job, SUCCEEDED)
            if job.cancel_requested:
                # the layers are written; the worker folds them anyway: only the wait is cancelled
                job.fold["state"] = "not_waited"
                return self._end(job, CANCELLED)
            if self._clock() >= deadline:
                job.fold["state"] = "timeout"
                job.warnings.append("The scene graph has not caught up yet; it keeps folding in the background")
                return self._end(job, SUCCEEDED)
            job.fold["state"] = "waiting"
            await self._sleep(self.fold_poll_s)


def create(db: Any, graph_db: Any, image_db: Any, worker_enabled: bool) -> DepthRebuild:
    return DepthRebuild(Sources(db, graph_db, image_db), worker_enabled)
