"""Per-map ingest worker of the scene graph (integration.md, piece 3; review findings 3, 5, 6).

Pure and dependency-injected: no ArangoDB, MinIO, Postgres, MQTT or config import. It runs against
fakes now and against the real engine / store / graph_db later. One `SceneWorker` is the single
writer of one map; `step()` does one bounded unit of work and tells the caller's loop what to do
next (`StepResult.state`, `sleep_s`). It is synchronous: run it in a thread / executor, or call it
from a loop with an injected sleep (`run`).

Per step
    1. lifecycle: map DELETING or missing -> release, do nothing (never save, never recreate sg_*
       or the bucket). Checked again right before every save.
    2. pull a batch of nodes past the scan position. A scan starts at `cursor - replay_margin`
       (database clock), so a costmap that became visible late is found; nodes already folded
       (same key and same stored_ms) are skipped cheaply from an in-memory `seen` map.
    3. node + PNG -> Leaf (scene_adapter), skip reasons counted. A PNG that has not landed yet is
       "pending": not marked seen, so the margin retries it.
    4. one `engine.fold_many(leaves)` per batch; `MissingTiles` -> load tiles, retry once.
    5. publish = `store.save(...)`, rate limited (every N leaves / T seconds / on idle, never
       faster than a minimum interval). The version exists only as the return of save.
    6. `publish(version, dirty)` callback with the Dirty merged since the last save.
    7. the cursor advances (and is persisted) only after a successful save of everything folded up
       to it. A failed save keeps the folded state in memory and retries; a FENCED save (stale
       expected version / lost lock: `SaveFenced`) stops the worker for good (zombie writer).

OPEN QUESTION: where does the cursor live? `Dirty.watermark` is capture-time based and cannot be
the cursor (a late upload is stamped below it, review finding 3). The ingest cursor is
`(costmap_stored_ms, node key)` of the database clock. Suggestion: a `cursor` field in `sg_meta`,
written by the SAME save (atomic with the version; a restore cannot see data without its cursor).
Until `store.save` accepts it, `CursorStore` is a small protocol (`get/put`) with an in-memory
fake; the worker passes `cursor=` and `expected_version=` to `store.save` and, at start, takes the
LOWER of the cursor_store value and `store.meta(map_id)["cursor"]` (more replay is always safe,
the fold is idempotent).

Assumed (not in CIMBI's draft yet): `store.save(map_id, sg, dirty, *, expected_version, cursor)`
raising `SaveFenced` on mismatch; `store.meta()` carrying `version`, `generation`, `params_hash`
and optionally `cursor`; `engine` already loaded at `meta["version"]` by the caller.
"""
import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import (Any, Callable, Dict, Iterable, List, Mapping, Optional, Protocol, Sequence,
                    Tuple)

from packages.scene_graph.api import BBox, Dirty, MissingTiles

from . import scene_adapter, scene_clock, scene_leaf

log = logging.getLogger(__name__)

Cursor = Tuple[int, str]            # (costmap_stored_ms, node key): a total order, database clock
ZERO_CURSOR: Cursor = (0, "")

# worker states (StepResult.state)
WORKED = "worked"                   # something happened and more may be waiting: call again at once
IDLE = "idle"                       # caught up: sleep
BACKOFF = "backoff"                 # transient failure (source, save, tiles): sleep and retry
NOT_OWNER = "not_owner"             # another process holds the map's writer lock: sleep and retry
DELETING = "deleting"               # terminal: map is being deleted or gone
FENCED = "fenced"                   # terminal: save refused (stale version / lost lock)
NEEDS_REBUILD = "needs_rebuild"     # terminal for the worker: params hash / generation changed
TERMINAL = (DELETING, FENCED, NEEDS_REBUILD)

SKIP_PNG_PENDING = "png_pending"    # not a terminal skip: retried through the margin
SKIP_PNG_EXPIRED = "png_missing_expired"


class SaveFenced(Exception):
    """The store refused a save: expected previous version mismatch or the writer lock is lost."""


class CursorStore(Protocol):
    def get(self, map_id: str) -> Optional[Cursor]: ...
    def put(self, map_id: str, cursor: Cursor) -> None: ...


class MemoryCursorStore:
    """In-memory CursorStore (tests; also a fine default until the cursor lives in sg_meta)."""

    def __init__(self) -> None:
        self.values: Dict[str, Cursor] = {}

    def get(self, map_id: str) -> Optional[Cursor]:
        return self.values.get(map_id)

    def put(self, map_id: str, cursor: Cursor) -> None:
        self.values[map_id] = (int(cursor[0]), str(cursor[1]))


class Guard(Protocol):
    """Per-map single-writer lock and lifecycle view. `is_deleting` is True for a map that is
    DELETING or no longer exists."""
    def acquire(self, map_id: str) -> bool: ...
    def held(self, map_id: str) -> bool: ...
    def release(self, map_id: str) -> None: ...
    def is_deleting(self, map_id: str) -> bool: ...


@dataclass
class WorkerConfig:
    batch_size: int = 200                   # nodes pulled (and folded) per step
    replay_margin_ms: int = 60_000          # ~ the upload-buffer timeout
    save_every_leaves: int = 50
    save_every_s: float = 30.0
    min_save_interval_s: float = 5.0        # hard floor between saves, whatever else says "now"
    save_when_idle: bool = True             # save unsaved leaves once a step finds nothing new
    idle_sleep_s: float = 2.0
    backoff_s: float = 10.0
    footprint_retry_s: float = 300.0        # re-ask for a footprint that was unknown
    default_half_side: float = scene_adapter.DEFAULT_HALF_SIDE
    robot_fields: Sequence[str] = ("robot_name", "robot_id", "robot")   # node field holding the robot (unverified)
    expected_params_hash: Optional[str] = None
    expected_generation: Optional[int] = None


@dataclass
class SceneWorkerStats:
    folded: int = 0                         # leaves handed to fold_many
    duplicates: int = 0                     # re-reads skipped (already folded at this stored_ms)
    skipped: Counter = field(default_factory=Counter)     # terminal skip reasons
    pending: int = 0                        # nodes waiting for their PNG (gauge)
    batches: int = 0
    saves: int = 0
    save_failures: int = 0
    source_errors: int = 0
    missing_tiles: int = 0
    default_r_d: int = 0                    # leaves folded with the default footprint
    version: int = 0
    unsaved_leaves: int = 0                 # folded, not yet saved
    version_lag_s: float = 0.0              # age of the oldest unsaved fold


@dataclass
class StepResult:
    state: str
    folded: int = 0
    skipped: int = 0
    version: Optional[int] = None           # set when this step saved
    sleep_s: float = 0.0
    detail: str = ""


def merge_dirty(a: Optional[Dirty], b: Dirty) -> Dirty:
    """Union of two Dirty summaries (a save that failed is retried together with the next batch)."""
    if a is None:
        return b
    boxes = [x for x in (a.bbox, b.bbox) if x is not None]
    bbox = BBox(min(x.x0 for x in boxes), min(x.y0 for x in boxes),
                max(x.x1 for x in boxes), max(x.y1 for x in boxes)) if boxes else None
    changed = list(dict.fromkeys(list(a.changed) + list(b.changed)))
    retired = {k: list(v) for k, v in a.retired.items()}
    retired.update({k: list(v) for k, v in b.retired.items()})
    links = list(dict.fromkeys([tuple(l) for l in list(a.links) + list(b.links)]))
    return Dirty(bbox=bbox, changed=changed, retired=retired, links=links,
                 watermark=max(a.watermark, b.watermark))


class SceneWorker:
    def __init__(self, map_id: str, *, source: Any, fetch_png: Callable[[str, str, str], Optional[bytes]],
                 footprint: Callable[[str], Tuple[Any, Any]], engine: Any, store: Any,
                 cursor_store: CursorStore, guard: Guard, stats: Optional[SceneWorkerStats] = None,
                 config: Optional[WorkerConfig] = None, now_ms: Callable[[], int],
                 publish: Optional[Callable[[int, Dirty], None]] = None,
                 load_tiles: Optional[Callable[[Any, Iterable], None]] = None,
                 on_needs_rebuild: Optional[Callable[[str], None]] = None,
                 clock: Optional[scene_clock.SessionClock] = None) -> None:
        self.map_id, self.source, self.fetch_png, self.footprint = map_id, source, fetch_png, footprint
        self.engine, self.store, self.cursor_store, self.guard = engine, store, cursor_store, guard
        self.stats = stats if stats is not None else SceneWorkerStats()
        self.config = config or WorkerConfig()
        self.now_ms, self.publish = now_ms, publish
        self.load_tiles, self.on_needs_rebuild = load_tiles, on_needs_rebuild
        self.clock = clock if clock is not None else scene_clock.SessionClock()
        self.state = ""                     # last terminal state, "" while running
        self.rebuild_reason = ""
        self._started = False
        self._cursor: Cursor = ZERO_CURSOR           # committed (durable) cursor
        self._candidate: Cursor = ZERO_CURSOR        # furthest processed, waiting for a save
        self._scan: Optional[Cursor] = None          # position inside a multi-page scan
        self._seen: Dict[str, int] = {}              # key -> stored_ms already handled terminally
        self._pending: Dict[str, int] = {}           # key -> stored_ms, PNG not landed
        self._expected_version = 0
        self._dirty: Optional[Dirty] = None          # folded, not yet saved
        self._last_save_ms: Optional[int] = None
        self._oldest_unsaved_ms: Optional[int] = None
        self._new_stamps: Dict[str, int] = {}        # key -> stamp to keep on the node before the fold
        self._footprints: Dict[str, Tuple[float, bool, int]] = {}   # robot -> (r_d, known, asked_ms)

    # ------------------------------------------------------------------ public

    @property
    def cursor(self) -> Cursor:
        return self._cursor

    def close(self) -> None:
        try:
            self.guard.release(self.map_id)
        except Exception:
            log.exception("scene worker %s: release failed", self.map_id)

    def run(self, sleep: Callable[[float], None], max_steps: Optional[int] = None) -> StepResult:
        """Loop `step()` until a terminal state (or `max_steps`); returns the last result."""
        n, res = 0, StepResult(IDLE)
        while max_steps is None or n < max_steps:
            res = self.step()
            n += 1
            if res.state in TERMINAL:
                break
            if res.sleep_s > 0:
                sleep(res.sleep_s)
        return res

    def step(self) -> StepResult:
        if self.state in TERMINAL:
            return StepResult(self.state)
        # 1. lifecycle first: nothing below may run for a map that is going away.
        if self._deleting():
            return self._terminal(DELETING, "map deleting or missing")
        if not self._started:
            early = self._start()
            if early is not None:
                return early
        elif not self.guard.held(self.map_id):
            return self._terminal(FENCED, "writer lock lost")
        cfg = self.config
        # a failed save left folded state in memory: retry it before pulling more
        if self._dirty is not None and self.stats.save_failures and self._due(idle=False, force=True):
            res = self._save()
            if res is not None:
                return res
        # 2. pull
        since = self._scan if self._scan is not None else (
            max(0, self._cursor[0] - cfg.replay_margin_ms), "")
        try:
            nodes = list(self.source.costmap_nodes_since(self.map_id, since[0], since[1], cfg.batch_size))
        except Exception as exc:
            self.stats.source_errors += 1
            log.warning("scene worker %s: source failed: %s", self.map_id, exc)
            return StepResult(BACKOFF, sleep_s=cfg.backoff_s, detail=f"source: {exc}")
        more = len(nodes) >= cfg.batch_size
        self._scan = self._order(nodes[-1]) if (more and nodes) else None
        # 3. adapt
        leaves, handled, skipped = [], [], 0
        self._new_stamps = {}
        for node in nodes:
            outcome = self._adapt(node)
            if outcome == "duplicate":
                continue
            if outcome == "pending":
                continue
            handled.append(node)
            if isinstance(outcome, str):
                skipped += 1
                self.stats.skipped[outcome] += 1
            else:
                leaves.append(outcome)
        self._expire_pending()
        # 4. fold once per batch
        folded = 0
        if leaves:
            err = self._keep_stamps()
            if err is None:
                err = self._fold(leaves)
            if err is not None:
                self._scan = None      # re-read from the margin once the cause is gone
                return err
            folded = len(leaves)
            self.stats.batches += 1
            self.stats.folded += folded
            self.stats.unsaved_leaves += folded
            if self._oldest_unsaved_ms is None:
                self._oldest_unsaved_ms = self.now_ms()
        for node in handled:
            key, ms = str(node.get("_key", "")), int(node.get("costmap_stored_ms") or 0)
            self._seen[key] = ms
            self._candidate = max(self._candidate, (ms, key))
        # 5-7. save (rate limited), then advance the cursor
        idle = not more and not folded and not skipped      # a re-read of the margin alone is idle
        saved: Optional[int] = None
        if self._dirty is not None and self._due(idle=idle):
            res = self._save()
            if res is not None:
                res.folded, res.skipped = folded, skipped
                return res
            saved = self.stats.version
        elif self._dirty is None:
            self._commit()      # nothing unsaved: terminal skips alone move the cursor
        self._refresh_gauges()
        if more or folded or skipped:       # progress: call again at once (the next step may idle-save)
            state, sleep = WORKED, 0.0
        else:
            state, sleep = IDLE, cfg.idle_sleep_s
        return StepResult(state, folded=folded, skipped=skipped, version=saved, sleep_s=sleep)

    # ------------------------------------------------------------------ internals

    def _deleting(self) -> bool:
        try:
            return bool(self.guard.is_deleting(self.map_id))
        except Exception:
            log.exception("scene worker %s: lifecycle check failed", self.map_id)
            return True     # unknown lifecycle: do not write

    def _terminal(self, state: str, detail: str = "") -> StepResult:
        self.state = state
        self.close()
        if state == NEEDS_REBUILD and self.on_needs_rebuild:
            try:
                self.on_needs_rebuild(detail)
            except Exception:
                log.exception("scene worker %s: on_needs_rebuild failed", self.map_id)
        return StepResult(state, detail=detail)

    def _start(self) -> Optional[StepResult]:
        if not self.guard.acquire(self.map_id):
            return StepResult(NOT_OWNER, sleep_s=self.config.backoff_s)
        try:
            meta = dict(self.store.meta(self.map_id) or {})
        except Exception as exc:
            self.guard.release(self.map_id)
            return StepResult(BACKOFF, sleep_s=self.config.backoff_s, detail=f"meta: {exc}")
        cfg = self.config
        if cfg.expected_params_hash is not None and meta.get("params_hash") not in (None, cfg.expected_params_hash):
            self.rebuild_reason = "params_hash"
            return self._terminal(NEEDS_REBUILD, "params_hash")
        if cfg.expected_generation is not None and meta.get("generation") not in (None, cfg.expected_generation):
            self.rebuild_reason = "generation"
            return self._terminal(NEEDS_REBUILD, "generation")
        version = int(meta.get("version") or 0)
        have = getattr(self.engine, "version", None)
        if have is not None and int(have) != version:
            self.rebuild_reason = "engine_version"
            return self._terminal(NEEDS_REBUILD, f"engine_version {have} != stored {version}")
        stored = [c for c in (self._cursor_of(self.cursor_store.get(self.map_id)),
                              self._cursor_of(meta.get("cursor"))) if c is not None]
        self._cursor = min(stored) if stored else ZERO_CURSOR
        self._candidate = self._cursor
        self._expected_version = version
        self.stats.version = version
        self._started = True
        return None

    @staticmethod
    def _cursor_of(raw: Any) -> Optional[Cursor]:
        if raw is None:
            return None
        try:
            return int(raw[0]), str(raw[1])
        except (TypeError, ValueError, IndexError):
            return None

    @staticmethod
    def _order(node: Mapping[str, Any]) -> Cursor:
        return int(node.get("costmap_stored_ms") or 0), str(node.get("_key", ""))

    def _robot_half_side(self, node: Mapping[str, Any]) -> Tuple[float, bool]:
        robot = next((str(node[f]) for f in self.config.robot_fields if node.get(f)), "")
        now = self.now_ms()
        hit = self._footprints.get(robot)
        if hit and (hit[1] or now - hit[2] < self.config.footprint_retry_s * 1000):
            return hit[0], hit[1]
        try:
            length, width = self.footprint(robot)
        except Exception:
            length, width = -1, -1
        r, known = scene_adapter.half_side(length, width, self.config.default_half_side)
        self._footprints[robot] = (r, known, now)
        return r, known

    def _adapt(self, node: Mapping[str, Any]):
        """Leaf, a terminal skip reason (str), "duplicate" or "pending"."""
        key, ms = str(node.get("_key", "")), int(node.get("costmap_stored_ms") or 0)
        if self._seen.get(key) == ms:
            self.stats.duplicates += 1
            return "duplicate"
        layer = scene_leaf.role_layers(node.get("costmap")).get(scene_leaf.ROLE_OCCUPANCY)
        if not layer:
            return scene_adapter.SKIP_NO_LAYER
        try:
            png = self.fetch_png(self.map_id, key, layer)
        except Exception as exc:
            log.warning("scene worker %s: png fetch of %s failed: %s", self.map_id, key, exc)
            png = None
        if png is None:
            self._pending[key] = ms
            return "pending"
        self._pending.pop(key, None)
        r_d, known = self._robot_half_side(node)
        info: Dict[str, Any] = {}
        leaf, reason = scene_adapter.to_leaf(node, png, r_d=r_d, layer=layer, clock=self.clock,
                                             stamp_info=info)
        if leaf is None:
            return reason or "unknown"
        if info.get("source") != scene_leaf.STAMP_STORED:
            self._new_stamps[key] = int(leaf.stamp_ms)
        if not known:
            self.stats.default_r_d += 1
        return leaf

    def _expire_pending(self) -> None:
        """A PNG that has not landed within the margin of the cursor will not be re-read any more."""
        floor = self._cursor[0] - self.config.replay_margin_ms
        for key in [k for k, ms in self._pending.items() if ms < floor]:
            del self._pending[key]
            self.stats.skipped[SKIP_PNG_EXPIRED] += 1
        self.stats.pending = len(self._pending)
        for key in [k for k, ms in self._seen.items() if ms < floor]:
            del self._seen[key]

    def _keep_stamps(self) -> Optional[StepResult]:
        """Persist, before folding, the stamps this batch's leaves are folded under (first write
        wins on the node). Written BEFORE the fold on purpose: a crash after a save and before a
        later stamp write would let a replay recompute a different stamp; written first, a failed
        fold or save just re-uses the stored one. A failed write means no fold."""
        stamps, self._new_stamps = self._new_stamps, {}
        write = getattr(self.source, "set_scene_stamps", None)
        if not stamps or write is None:
            return None
        try:
            write(self.map_id, stamps)
        except Exception as exc:
            self.stats.source_errors += 1
            log.warning("scene worker %s: keeping stamps failed: %s", self.map_id, exc)
            self._scan = None
            return StepResult(BACKOFF, sleep_s=self.config.backoff_s, detail=f"stamps: {exc}")
        return None

    def _fold(self, leaves: List[Any]) -> Optional[StepResult]:
        for attempt in (0, 1):
            try:
                dirty = self.engine.fold_many(leaves)
                self._dirty = merge_dirty(self._dirty, dirty)
                return None
            except MissingTiles as exc:
                self.stats.missing_tiles += 1
                log.warning("scene worker %s: missing tiles %s", self.map_id, exc.tiles)
                if attempt == 1 or self.load_tiles is None:
                    break
                try:
                    self.load_tiles(self.engine, exc.tiles)
                except Exception:
                    log.exception("scene worker %s: tile load failed", self.map_id)
                    break
            except Exception as exc:
                log.exception("scene worker %s: fold failed", self.map_id)
                return StepResult(BACKOFF, sleep_s=self.config.backoff_s, detail=f"fold: {exc}")
        return StepResult(BACKOFF, sleep_s=self.config.backoff_s, detail="missing tiles")

    def _due(self, idle: bool, force: bool = False) -> bool:
        cfg, now = self.config, self.now_ms()
        if self._last_save_ms is not None and now - self._last_save_ms < cfg.min_save_interval_s * 1000:
            return False
        if force:
            return True
        if self.stats.unsaved_leaves >= cfg.save_every_leaves:
            return True
        oldest = self._oldest_unsaved_ms
        if oldest is not None and now - oldest >= cfg.save_every_s * 1000:
            return True
        return idle and cfg.save_when_idle and self.stats.unsaved_leaves > 0

    def _save(self) -> Optional[StepResult]:
        """Save everything folded. None on success; a StepResult on failure / fence / lifecycle."""
        if self._deleting():
            return self._terminal(DELETING, "map deleting before save")
        if not self.guard.held(self.map_id):
            return self._terminal(FENCED, "writer lock lost")
        target = self._candidate
        try:
            version = self.store.save(self.map_id, self.engine, self._dirty,
                                      expected_version=self._expected_version, cursor=target)
        except SaveFenced as exc:
            return self._terminal(FENCED, str(exc) or "save fenced")
        except Exception as exc:
            self.stats.save_failures += 1
            self._last_save_ms = self.now_ms()
            log.warning("scene worker %s: save failed: %s", self.map_id, exc)
            return StepResult(BACKOFF, sleep_s=self.config.backoff_s, detail=f"save: {exc}")
        dirty, self._dirty = self._dirty, None
        self._expected_version = int(version)
        self._last_save_ms = self.now_ms()
        self._oldest_unsaved_ms = None
        self.stats.saves += 1
        self.stats.version = int(version)
        self.stats.unsaved_leaves = 0
        self.stats.save_failures = 0
        self._commit(target)
        if self.publish is not None and dirty is not None:
            try:
                self.publish(int(version), dirty)
            except Exception:
                log.exception("scene worker %s: publish callback failed", self.map_id)
        self._refresh_gauges()
        return None

    def _commit(self, cursor: Optional[Cursor] = None) -> None:
        cursor = cursor or self._candidate
        if cursor <= self._cursor:
            return
        self._cursor = cursor
        try:
            self.cursor_store.put(self.map_id, cursor)
        except Exception:
            log.exception("scene worker %s: cursor persist failed", self.map_id)

    def _refresh_gauges(self) -> None:
        self.stats.pending = len(self._pending)
        oldest = self._oldest_unsaved_ms
        self.stats.version_lag_s = 0.0 if oldest is None else max(0.0, (self.now_ms() - oldest) / 1000.0)
