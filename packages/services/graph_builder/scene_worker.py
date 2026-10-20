"""Per-map ingest worker of the scene graph (integration.md, piece 3; review findings 3, 5, 6).

Pure and dependency-injected: no ArangoDB, MinIO, Postgres, MQTT or config import. It runs against
fakes now and against the real engine / store / graph_db later. One `SceneWorker` is the single
writer of one map; `step()` does one bounded unit of work and tells the caller's loop what to do
next (`StepResult.state`, `sleep_s`). It is synchronous: run it in a thread / executor, or call it
from a loop with an injected sleep (`run`).

Per step
    1. lifecycle: map DELETING or missing -> release, do nothing (never save, never recreate sg_*
       or the bucket); a lifecycle query that fails is a BACKOFF, not a verdict. The check is rate
       limited (`lifecycle_check_s`) except right before every save / generation commit.
    2. pull a batch of nodes past the scan position. A scan starts at `candidate - replay_margin`
       (database clock; `candidate` = furthest node handled), so a costmap that became visible
       late is found; nodes already folded (same key and same stored_ms) are skipped cheaply from
       an in-memory `seen` map.
    3. node + PNG -> Leaf (scene_adapter), skip reasons counted. A PNG that is confirmed missing
       (`fetch_png` returned None) is "pending": kept with its node, retried every `png_retry_s`
       and dropped after `png_expire_s` of WALL clock; while any is pending the persisted cursor
       stays below the oldest, so a restart re-reads it. A fetch that RAISES (MinIO down) is not a
       verdict: the step stops reading at that node and backs off, nothing past it is handled.
    4. one `engine.fold_many(leaves)` per batch; `MissingTiles` -> load tiles, retry once.
       `LeafError` (validation, nothing mutated): the leaf it names is quarantined at once and the
       rest folded (no key of the batch: bisected). Any other exception (`EngineFaulted`, a solver
       error: the engine is undefined): the engine is dropped, reloaded from the store at the live
       version (or from the saved pending generation of a rebuild) and the leaves since the
       durable cursor are read again; `load_fail_limit` faults in a row without a good fold fall
       into a rebuild, a fault inside that rebuild (or no way to rebuild) ends in NEEDS_REBUILD.
       `MemoryError` (nothing changed) is a backoff; `memory_retry_limit` in a row on the same batch
       bisect it and quarantine the leaf that still fails alone (like a poison leaf).
    5. publish = `store.save(...)`, rate limited (every N leaves / T seconds / on idle, never
       faster than a minimum interval). The version exists only as the return of save.
    6. `publish(version, dirty)` callback with the Dirty merged since the last save.
    7. the persisted cursor advances only after a successful save of everything folded up to it.
       A failed save keeps the folded state in memory and retries; a FENCED save (stale expected
       version / lost lock: `SaveFenced`) stops the worker for good (zombie writer).

Cursor: `Dirty.watermark` is capture-time based and cannot be the cursor (a late upload is stamped
below it, review finding 3). The ingest cursor is `(costmap_stored_ms, node key)` of the database
clock. It is written by the SAME `store.save` as the version (`sg_meta.cursor`: a restore cannot
see data without its cursor). At start the worker takes the LOWER of the `cursor_store` value and
`store.meta(map_id)["cursor"]` (more replay is always safe, the fold is idempotent). `CursorStore`
is a small protocol with an in-memory implementation (a second, process-local copy; the store's
meta is the durable one).

Batching (measured: a fold costs seconds and a larger batch amortises a fixed solve): adapted
leaves wait in a buffer and are folded in ONE `fold_many` once `batch_min_leaves` are waiting, or
once the oldest has waited `min_save_interval_s` (the publish interval; a trickle is never held
longer). A backlog (a fold slower than arrivals) is folded `batch_max_leaves` at a time, so the
worker never lags in small batches. The cursor only covers folded leaves; a buffered leaf is
re-read after a restart. A fold slower than `batch_budget_s` is logged (map, leaves, seconds).
Terminal skips wait in the buffer behind a leaf (they only order the cursor) and are counted once.

Idle polling: a step that finds nothing new sleeps `idle_sleep_s`, doubling per idle step up to
`idle_max_s`; any progress resets it. The worker's own `sleep_s` is what `SceneRunner` honours.

Params / generation: when the stored `params_hash` differs from `expected_params_hash` and the
store can `begin_generation` (and `new_engine` is given) the worker rebuilds in a new generation
from the first leaf (saves go to the pending generation with `generation=`, readers keep the old
one; the expected version stays the live one until `commit_generation`), then commits once caught
up. Without those it ends in NEEDS_REBUILD. A `scene_graph_needs_rebuild` request on the map's
status (guard.rebuild_request) starts the same rebuild. Saves are always made with the generation
the worker started in (a fence against another writer that committed a new one).

Engine: given loaded (`engine=`, optionally with the `loaded_version` it was loaded at) or built
lazily by `engine_factory(stored_version)` AFTER the writer lock is held, so a replica that does
not own the map holds no engine. A preloaded engine whose `loaded_version` differs from the stored
version (a load that raced the lock) is reloaded through the factory. `engine.version` is the
fold counter of the engine and is NOT compared with the store's snapshot version (they are
different counters).

Rebuild resume: a worker (re)starting finds a pending generation of the expected params and
continues it from its cursor. When a delete request exists too, only if the pending record's
`begun_ms` (store clock) is not before the request's `at` (it cannot hold a deleted leaf) and is
younger than `rebuild_max_age_s`; an old-format record (no `begun_ms`) is restarted. A mid-run
request always starts a fresh generation. A cursor-only persist moves the pending cursor of a
rebuild too (once the pending generation holds a saved state).

Rebuild order (canonical contract, handoff_laci_morse.md): a rebuild does not fold in scan order. Phase 1 scans
the nodes in (costmap_stored_ms, key) order WITHOUT fetching PNGs and keeps one small record per node (its stamp,
key, stored_ms and the node document); phase 2 sorts the records by (stamp_ms, key) and folds them in fixed
batches of `rebuild_batch` (48) leaves, fetching the PNGs only for the batch being built (plus the prefetch
pool's own bound). Memory bound: O(nodes) node documents (a few hundred bytes each, never a PNG or a decoded
window) + 8 bytes per node for the floor array; at most `rebuild_batch` decoded leaves are resident. Incremental
(non-rebuild) ingest is unchanged. The cursor stays `(costmap_stored_ms, key)`: what is persisted during phase 2
is the lowest stored_ms among the records not yet folded (everything below it is folded, so a resume rescans from
there); on resume the leaves the restored pending generation already holds are skipped by key (same stamp, pose,
footprint, view), so the resumed fold continues the same sorted sequence. A rebuild older than
`rebuild_max_age_s` still commits what is folded: the unfolded rest is dropped from the plan, the cursor is
that floor and the leaves above it that were folded are recognised by `seen`; the rest is folded incrementally.
A PNG that is missing when its turn comes is retried later and folds after its batch (the only departure from the
sorted order, as before).

Scale: the engine is the only large object a worker holds. With `lazy_load` it is loaded by the
first fold, not at start (an idle map never loads). `evict()` (called by the runner when its
resident-engine / byte budget is exceeded) saves what is unsaved and drops the engine of an IDLE
worker (nothing buffered, pending or unsaved); everything else (seen map, cursors, rebuild state)
is kept, so the next leaves reload it from the store at the version just saved and nothing is
re-folded. `PngPrefetch` (shared by all workers, bounded) fetches and decodes the PNGs of a page
on other threads while the worker adapts / folds.

Terminal states are logged at warning; `SceneRunner` reports them once per transition as a
`fold_failed` event (`stage` = the state).

Interface assumed of the store: `store.save(map_id, sg, dirty, *, expected_version, cursor[,
generation])` raising `SaveFenced` / the configured `fence_errors` on mismatch; `store.meta()`
carrying `version`, `generation`, `params_hash` and optionally `cursor`.
"""
import inspect
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from collections import Counter
from dataclasses import dataclass, field
from typing import (Any, Callable, Dict, Iterable, List, Mapping, Optional, Protocol, Sequence,
                    Tuple)

from packages.scene_graph import StateMismatch, TooOld
from packages.scene_graph.api import BBox, Dirty, EngineFaulted, LeafError, MissingTiles

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
NO_STAMP = 1 << 62                  # plan sort key of a node without a usable stamp (a skip: sorts last)
NEEDS_REBUILD = "needs_rebuild"     # terminal for the worker: params hash / generation changed
TERMINAL = (DELETING, FENCED, NEEDS_REBUILD)

SKIP_PNG_EXPIRED = "png_missing_expired"
SKIP_FOLD_ERROR = "fold_error"        # a leaf the engine cannot fold (bisected and quarantined)
# A leaf the stored state already holds (same key, stamp, pose, footprint and view) at a stored time
# not past the cursor that state was saved with: not folded again after a restart (no re-solve, no
# new version). Counted in `skipped`; not an error.
SKIP_ALREADY_FOLDED = "already_folded"


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
    DELETING or no longer exists, and RAISES when the lifecycle cannot be read (the worker backs
    off: an unanswered question is neither "deleting" nor "fine to write")."""
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
    idle_max_s: float = 0.0                 # idle sleep doubles up to this while nothing changes (0: constant)
    backoff_s: float = 10.0
    # How often the map's lifecycle / lock is looked up at the top of a step (one PG round trip
    # each). 0 = every step. Always looked up right before a save / generation commit.
    lifecycle_check_s: float = 0.0
    png_retry_s: float = 2.0                # a PNG that is not there yet is asked again this often ...
    png_expire_s: Optional[float] = None    # ... and given up on after this much wall clock (None: the margin)
    footprint_retry_s: float = 300.0        # re-ask for a footprint that was unknown
    default_half_side: float = scene_adapter.DEFAULT_HALF_SIDE
    robot_fields: Sequence[str] = ("robot_name",)    # node field holding the robot (graph-builder writes robot_name)
    expected_params_hash: Optional[str] = None
    expected_generation: Optional[int] = None
    # How often the map's `scene_graph_needs_rebuild` request is looked up (one cheap query per
    # map per interval; also once at start). 0 disables the check.
    rebuild_check_s: float = 30.0
    # Consecutive failures of `commit_generation` that are not a verdict (store unreachable, ...)
    # before the worker stops with FENCED (reported as an event; the runner retries later).
    commit_retries: int = 5
    # Caught-up steps with nothing folded and nothing saved before a rebuild is declared empty.
    empty_rebuild_confirmations: int = 2
    # batching (config.py SCENE_GRAPH_BATCH_*; the neutral defaults here fold every page whole)
    batch_min_leaves: int = 1               # fold once this many leaves wait ...
    batch_max_leaves: int = 1_000_000       # ... at most this many per fold_many ...
    # ... or once the oldest has waited min_save_interval_s (the publish interval)
    batch_budget_s: float = 0.0             # warn when one fold takes longer (0: off)
    # Replay: the full margin (`replay_margin_ms` below the furthest node) is re-read at start, after
    # a failure, and at most every `replay_recheck_s` (a costmap that became visible late). Every
    # other poll re-reads only `poll_margin_ms` (rows committed out of order within seconds).
    replay_recheck_s: float = 60.0
    poll_margin_ms: int = 5_000
    # Rebuild (finding 1/2): a rebuild is committed once its scan reached the head of the nodes; a
    # rebuild older than `rebuild_max_age_s` is committed at its next save even when the scan has
    # not (steady uploads, a slow fold). Folds inside a rebuild are fixed batches of `rebuild_batch`
    # leaves (no time-based flush) until the head is reached. A delete-triggered rebuild waits for
    # `rebuild_debounce_s` without a newer request (deletes in bursts coalesce).
    rebuild_max_age_s: float = 900.0
    rebuild_batch: int = 48
    rebuild_debounce_s: float = 30.0
    # engine load that fails deterministically this many times in a row falls into a rebuild (or
    # NEEDS_REBUILD when the worker cannot rebuild)
    load_fail_limit: int = 3
    # A leaf the engine rejects in validation (LeafError, nothing mutated) is quarantined at once: by the
    # key the error names, else by bisecting the batch. Any other exception from fold_many leaves the engine
    # in an undefined state (EngineFaulted): it is dropped and reloaded from the store; `load_fail_limit`
    # such faults in a row (no good fold between) fall into a rebuild, or end in NEEDS_REBUILD.
    # per step: stop reading nodes after this much time / this many PNG re-asks (0: unbounded)
    step_budget_s: float = 0.0
    png_retries_per_step: int = 20
    stats_log_s: float = 60.0               # one stats line per map this often (0: off)
    updated_event_interval_s: float = 60.0  # 'updated' events per map at most this often
    # a cursor-only persist (skip-only progress) when the cursor is this far ahead of the stored one
    cursor_persist_margin_ms: Optional[int] = None    # None: replay_margin_ms
    # Scale. lazy_load: the engine of a stored map (version > 0) is loaded by the first fold, not at start.
    # nbytes_refresh_s: `engine.nbytes` (a walk of its arrays) is recomputed at most this often.
    # memory_retry_limit: consecutive MemoryErrors of one batch before it is bisected / quarantined.
    # Restart replay: leaves the stored state already holds are skipped (SKIP_ALREADY_FOLDED) instead of
    # being folded again (False: fold the margin again, as before).
    skip_folded: bool = True
    # costmap_stored_ms is a leaf's receipt time only within this of the node's own time (scene_leaf.receipt_ms)
    receipt_window_s: float = scene_leaf.DEFAULT_RECEIPT_WINDOW_S
    lazy_load: bool = False
    nbytes_refresh_s: float = 10.0
    memory_retry_limit: int = 3
    # store.gc (the store's default keep) after this many saves of the map and once when it goes idle (0: never)
    gc_every_saves: int = 0


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
    buffered_leaves: int = 0                # adapted, waiting for a batch (gauge)
    last_fold_s: float = 0.0
    max_fold_s: float = 0.0
    slow_folds: int = 0                     # folds over batch_budget_s
    fold_failures: int = 0                  # consecutive (fold / tiles)
    rebuilds: int = 0
    resident: bool = False                  # an engine is held in memory (gauge)
    resident_bytes: Optional[int] = None    # engine.nbytes (None: the engine cannot say); 0 when not resident
    canvas: Optional[Tuple[int, int]] = None    # the engine's dense canvas (H, W) while resident
    gc_runs: int = 0                        # store.gc calls
    gc_deleted: int = 0                     # objects they removed
    memory_paused_steps: int = 0            # steps that pulled nothing because the memory guard was paused
    loads: int = 0                          # engines loaded through the factory
    evictions: int = 0                      # engines dropped to free memory
    prefetch_hits: int = 0                  # leaves whose PNG was fetched + decoded by the prefetch pool
    memory_isolations: int = 0              # batches bisected after repeated MemoryErrors
    already_folded: int = 0                 # leaves not folded again: the stored state holds them (restart replay)
    views_built: int = 0                    # leaves folded with a camera view (cover())
    views_missing: int = 0                  # ... without: the node has no depth record
    view_failures: int = 0                  # ... without: a depth record whose camera block is unusable


@dataclass
class StepResult:
    state: str
    folded: int = 0
    skipped: int = 0
    version: Optional[int] = None           # set when this step saved
    sleep_s: float = 0.0
    detail: str = ""
    settled: bool = False                   # IDLE and truly caught up: see SceneWorker.settled()


@dataclass
class RebuiltDirty(Dirty):
    """The publish after a generation switch: everything may have changed (`scene_events` reports
    it as truncated, i.e. "refetch the whole view"); `retired` is the old -> new key map."""
    full: bool = True


@dataclass
class _Entry:
    node: Mapping[str, Any]
    leaf: Any                               # None: a terminal skip, kept only to order the cursor


@dataclass
class _PlanRec:
    """One node of a rebuild's plan: sorted by (stamp, key); `persist`: the stamp is a fresh one the worker must keep."""
    stamp: int                              # epoch ms the leaf is folded under (NO_STAMP: none, adapt skips it)
    key: str
    ms: int                                 # costmap_stored_ms
    node: Mapping[str, Any]
    persist: bool = False


@dataclass
class _Pending:
    node: Mapping[str, Any]                 # kept to ask for its PNG again without re-reading the node
    ms: int                                 # costmap_stored_ms
    first_ms: int                           # wall clock (now_ms) when first found without its PNG
    tried_ms: int


class _FetchError(Exception):
    """`fetch_png` raised: the PNG's state is unknown (not "missing"). Never skips a leaf."""


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


class _Ticket:
    """One prefetched PNG: `result()` is (png | None, window | None, decode error | None) or raises what
    the fetch raised. `release()` (once; `result` does it) frees its slot in the shared bound."""
    __slots__ = ("future", "_owner", "_released")

    def __init__(self, future: Any, owner: "PngPrefetch") -> None:
        self.future, self._owner, self._released = future, owner, False

    def result(self) -> Tuple[Optional[bytes], Any, Optional[BaseException]]:
        try:
            return self.future.result()
        finally:
            self.release()

    def release(self) -> None:
        if not self._released:
            self._released = True
            if self.future.cancel():        # a task that has not started never runs
                self._owner._unqueued()
            self._owner._released()


class PngPrefetch:
    """A bounded pool, shared by every worker and separate from the step pool, that fetches (MinIO) and
    decodes the PNGs of a page of nodes while the worker adapts and folds the earlier ones. At most
    `max_pending` PNGs (queued, running or finished and not yet consumed) exist at once, so a page of
    200 windows of many maps cannot pile up in memory; `submit` returns None when the bound is reached
    and the worker fetches that leaf inline, as it did before. Starts its threads lazily and can be
    closed and used again (the runner is closed and restarted after a crash)."""

    def __init__(self, threads: int = 4, max_pending: int = 64) -> None:
        self.threads, self.max_pending = max(1, int(threads)), max(1, int(max_pending))
        self._lock = threading.Lock()
        self._pool: Optional[ThreadPoolExecutor] = None
        self._outstanding = 0
        self._queued = 0
        self.submitted = self.rejected = self.hits = 0

    def submit(self, fetch: Callable[[str, str, str], Optional[bytes]], map_id: str, key: str,
               layer: str) -> Optional[_Ticket]:
        with self._lock:
            if self._outstanding >= self.max_pending:
                self.rejected += 1
                return None
            if self._pool is None:
                self._pool = ThreadPoolExecutor(self.threads, thread_name_prefix="scene-prefetch")
            self._outstanding += 1
            self._queued += 1
            self.submitted += 1
            pool = self._pool
        try:
            future = pool.submit(self._task, fetch, map_id, key, layer)
        except RuntimeError:                # closed under us
            with self._lock:
                self._outstanding -= 1
                self._queued -= 1
            return None
        return _Ticket(future, self)

    def _task(self, fetch: Callable[[str, str, str], Optional[bytes]], map_id: str, key: str,
              layer: str) -> Tuple[Optional[bytes], Any, Optional[BaseException]]:
        with self._lock:
            self._queued -= 1
        png = fetch(map_id, key, layer)     # may raise: the worker sees it as a failed fetch of that leaf
        if png is None:
            return None, None, None
        try:
            return png, scene_adapter.occupancy_window(png), None
        except scene_adapter.PngError as exc:
            return png, None, exc

    def _released(self) -> None:
        with self._lock:
            self._outstanding -= 1

    def _unqueued(self) -> None:
        with self._lock:
            self._queued -= 1

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {"threads": self.threads, "max_pending": self.max_pending,
                    "outstanding": self._outstanding, "queue_depth": max(0, self._queued),
                    "submitted": self.submitted, "rejected": self.rejected, "hits": self.hits}

    def close(self) -> None:
        with self._lock:
            pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)


class SceneWorker:
    def __init__(self, map_id: str, *, source: Any, fetch_png: Callable[[str, str, str], Optional[bytes]],
                 footprint: Callable[[str], Tuple[Any, Any]], engine: Any = None, store: Any,
                 cursor_store: CursorStore, guard: Guard, stats: Optional[SceneWorkerStats] = None,
                 config: Optional[WorkerConfig] = None, now_ms: Callable[[], int],
                 publish: Optional[Callable[[int, Dirty], None]] = None,
                 load_tiles: Optional[Callable[[Any, Iterable], None]] = None,
                 on_needs_rebuild: Optional[Callable[[str], None]] = None,
                 clock: Optional[scene_clock.SessionClock] = None,
                 new_engine: Optional[Callable[[], Any]] = None,
                 on_event: Optional[Callable[[str, Dict[str, Any]], None]] = None,
                 fence_errors: Tuple[type, ...] = (),
                 engine_factory: Optional[Callable[[int], Any]] = None,
                 loaded_version: Optional[int] = None,
                 mono_ms: Optional[Callable[[], int]] = None,
                 prefetch: Optional[PngPrefetch] = None,
                 stored_leaves: Optional[Callable[[int], Optional[Mapping[str, tuple]]]] = None,
                 half_side: Optional[float] = None,
                 memory: Any = None, load_gate: Any = None) -> None:
        if engine is None and engine_factory is None:
            raise ValueError("SceneWorker needs an engine or an engine_factory")
        self.map_id, self.source, self.fetch_png, self.footprint = map_id, source, fetch_png, footprint
        self.engine, self.store, self.cursor_store, self.guard = engine, store, cursor_store, guard
        self.engine_factory, self._loaded_version = engine_factory, loaded_version
        self.prefetch = prefetch
        self.memory = memory                 # MemoryGuard (scene_memory): `.paused` stops intake, None: never
        self.load_gate = load_gate           # LoadGate: refuses an engine load (memory high / crash cooldown), keeps the marker
        self._saves_since_gc = 0
        self._load_refused = ""
        # the map's fixed robot half side (its spec's robot_half_side_m, set at creation): when given, every node is folded
        # with it and the live footprint is not read; None = the legacy per-robot footprint (a map from before the spec)
        self._half_side = half_side
        # `stored_leaves(version) -> {key: (stamp_ms, session, x, y, r_d, view)}` of a stored version (the
        # leaf segments of its manifest: no engine load); None = read them from the engine when it is loaded
        self.stored_leaves = stored_leaves
        self._known: Optional[Dict[str, tuple]] = None   # the leaves the state loaded at start holds (lazy)
        self._known_cursor: Cursor = ZERO_CURSOR         # the cursor that state was saved with
        self._known_version = 0
        self._known_off = True                           # no skipping: set by start, cleared when usable
        self._rebuild_known = False                      # a resumed pending generation: skip the leaves it holds
        # rebuild plan: phase 1 fills `_plan_recs` (key -> rec), sealing sorts it into `_plan`, phase 2 feeds it
        self._plan_recs: Optional[Dict[str, _PlanRec]] = None
        self._plan: Optional[List[_PlanRec]] = None
        self._plan_idx = 0
        self._plan_suffix: Optional[Any] = None          # array('q'): min stored_ms of _plan[i:]
        self._plan_min_ms: Optional[int] = None
        self._plan_persist: set = set()
        self._view_warned = False
        self._tickets: Dict[Tuple[str, int], _Ticket] = {}   # (key, stored_ms) -> prefetched PNG of this step
        self._memory_head: Optional[str] = None      # first leaf of the batch that last ran out of memory
        self._memory_streak = 0
        self._bytes_at_ms: Optional[int] = None      # when engine.nbytes was last computed
        self._bytes_stale = True
        self._bytes_warned = False
        self.stats = stats if stats is not None else SceneWorkerStats()
        self.config = config or WorkerConfig()
        self.now_ms, self.publish = now_ms, publish
        # every interval below (png expiry, save floor, lifecycle, debounce) is measured on this
        # clock: monotonic in production (`mono_ms`), the injected `now_ms` when none is given
        self._mono: Callable[[], int] = mono_ms if mono_ms is not None else now_ms
        self.load_tiles, self.on_needs_rebuild = load_tiles, on_needs_rebuild
        self.clock = clock if clock is not None else scene_clock.SessionClock()
        self.state = ""                     # last terminal state, "" while running
        self.rebuild_reason = ""
        self._started = False
        self._cursor: Cursor = ZERO_CURSOR           # committed (durable) cursor
        self._candidate: Cursor = ZERO_CURSOR        # furthest processed, waiting for a save
        self._scan: Optional[Cursor] = None          # position inside a multi-page scan
        self._dup_counted: Dict[str, int] = {}       # key -> stored_ms already counted in stats.duplicates
        self._seen: Dict[str, int] = {}              # key -> stored_ms already handled terminally
        self._pending: Dict[str, _Pending] = {}      # key -> node whose PNG is confirmed missing
        self._idle_streak = 0                        # consecutive idle steps (idle sleep backoff)
        self._lifecycle_ok_ms: Optional[int] = None  # when the lifecycle / lock was last confirmed
        self._expected_version = 0
        self._dirty: Optional[Dirty] = None          # folded, not yet saved
        self._last_save_ms: Optional[int] = None
        self._oldest_unsaved_ms: Optional[int] = None
        self._new_stamps: Dict[str, int] = {}        # key -> stamp to keep on the node before the fold
        self._footprints: Dict[str, Tuple[float, bool, int]] = {}   # robot -> (r_d, known, asked_ms)
        self.new_engine, self.on_event, self.fence_errors = new_engine, on_event, tuple(fence_errors)
        self._buf: List[_Entry] = []                 # adapted, in cursor order, waiting for a batch
        self._buffered: Dict[str, int] = {}          # key -> stored_ms of the leaves in _buf
        self._buf_since_ms: Optional[int] = None     # when the oldest waiting leaf arrived
        self._generation: Optional[int] = None       # generation this worker writes to
        self._rebuild_gen: Optional[int] = None      # pending generation being rebuilt
        self._rebuild_started_ms = 0
        self._rebuild_saved = False
        self._rebuild_event_reason = "needs_rebuild"
        self._rebuild_request: Optional[Mapping[str, Any]] = None   # the status key this rebuild answers
        self._commit_failures = 0                    # consecutive non-verdict commit_generation failures
        self._empty_confirms = 0                     # caught-up steps of a rebuild that saved nothing
        self._request_checked_ms: Optional[int] = None
        self._clear_failed_at: Optional[str] = None  # a committed request whose key could not be cleared
        self._watermark: Tuple[int, str] = (0, "")
        self._leaves_since_publish = 0
        self._fail_stage = ""
        self._fail_count = 0
        self._full_replay = True                     # re-read the whole margin on the next poll
        self._last_full_replay_ms = 0
        self._outage_since: Optional[int] = None     # PNG fetches raising since (excluded from expiry)
        self._load_failures = 0                      # consecutive deterministic engine-load failures
        self._load_gave_up = False
        self._quarantined: List[str] = []            # keys the last fold isolated as poison
        self._fault_streak = 0                       # consecutive folds that faulted the engine (reset by a good fold)
        self._fault_rebuilt = False                  # a fault already led to a rebuild that has not committed yet
        self._stored_cursor: Cursor = ZERO_CURSOR    # the cursor the store holds (sg_meta)
        self._resume_cursor: Optional[Cursor] = None # set by a rebuild that resumed a pending generation
        self._request_seen: Optional[str] = None     # `at` of the delete request being debounced
        self._request_seen_ms = 0
        self._warned_cannot_rebuild = False
        self._last_updated_event_ms: Optional[int] = None
        self._event_held: Optional[Tuple[int, Optional[int]]] = None   # an 'updated' event inside the throttle
        self._skip_logged: set = set()
        self._leaves_since_event = 0
        self._last_stats_ms: Optional[int] = None
        self._last_step_ms: Optional[int] = None     # when the worker last completed a step
        self.last_result_state = ""
        self._head = False                           # this step's scan reached the newest node

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
        try:
            res = self._step()
        finally:
            self._drop_tickets()
        self.last_result_state = res.state
        self._account()
        self._last_step_ms = self._mono()
        self._log_stats()
        return res

    def last_step_age_s(self) -> Optional[float]:
        return None if self._last_step_ms is None else max(0.0, (self._mono() - self._last_step_ms) / 1000.0)

    # ---- memory: residency, eviction

    @property
    def cold(self) -> bool:
        """No engine in memory: the next fold (or this step, for a worker that was never started) loads
        one. The runner admits only some of these at a time so loads never take every thread."""
        return self.engine is None and self.state not in TERMINAL

    def settled(self) -> bool:
        """Caught up for good: running, the last scan reached the newest node, and nothing is buffered,
        waiting for a PNG, prefetched, holding a stamp, folded-but-unsaved or mid-rebuild. An IDLE step
        alone does not mean this (a trickle leaf waits for its batch, a PNG is retried, a save is
        rate limited): use this to decide "everything ingested"."""
        return (self._started and self.state not in TERMINAL
                and self._head and not self._buf and not self._buffered and not self._pending
                and not self._tickets and not self._new_stamps and self._dirty is None
                and self.stats.unsaved_leaves == 0 and self._rebuild_gen is None
                and self._scan is None)

    def evictable(self) -> bool:
        """An engine is held and dropping it loses nothing but the load time: nothing is buffered, waiting
        for a PNG or holding a stamp to write, the worker runs, and a rebuild (if any) has saved state to
        reload (an unsaved fold is saved by `evict` first)."""
        return (self.engine is not None and self._started and self.state not in TERMINAL
                and not self._buf and not self._pending and not self._new_stamps
                and (self._rebuild_gen is None or self._rebuild_saved)
                and (self.engine_factory is not None or self._rebuild_gen is not None)
                and (self._rebuild_gen is not None or self._expected_version > 0)
                and not (self._load_gave_up or self._fault_streak))

    def evict(self) -> bool:
        """Free the engine: save what is unsaved (never drop an unsaved fold), then forget the engine and
        nothing else (`_seen`, cursors, rebuild state stay, so the replay margin is not folded again). The
        next fold reloads it from the store at the version just saved, or from the pending generation of
        a rebuild. False when it was not evictable or the save did not go through (nothing was dropped).
        Runs in the worker's own slot: never concurrently with a step."""
        if not self.evictable():
            return False
        if self._dirty is not None:
            res = self._save()
            if res is not None or self._dirty is not None:
                return False        # failed / fenced / deleting: the step reports it, the engine stays
        if self.state in TERMINAL or self.engine is None:
            return False
        self.engine, self._loaded_version = None, None
        self.stats.evictions += 1
        self._bytes_stale = True
        self._account(force=True)
        return True

    def _account(self, force: bool = False) -> None:
        """Residency gauges. `engine.nbytes` (feature-detected: a walk of the engine's arrays) is
        recomputed after a fold / load, at most every `nbytes_refresh_s`."""
        st, eng = self.stats, self.engine
        st.resident = eng is not None
        if eng is None:
            st.resident_bytes, st.canvas = 0, None
            return
        now = self._mono()
        if not force and not self._bytes_stale and st.resident_bytes is not None:
            return
        if (not force and self._bytes_at_ms is not None and st.resident_bytes is not None
                and now - self._bytes_at_ms < self.config.nbytes_refresh_s * 1000):
            return
        try:
            size = getattr(eng, "nbytes", None)
            st.resident_bytes = int(size) if size is not None else None
            shape = getattr(getattr(eng, "_V", None), "shape", None)
            st.canvas = (int(shape[0]), int(shape[1])) if shape is not None and len(shape) >= 2 else None
        except Exception:  # noqa: BLE001  a gauge: never the step's problem
            st.resident_bytes = None
        if st.resident_bytes is None and not self._bytes_warned:
            self._bytes_warned = True
            log.warning("scene worker %s: the engine reports no nbytes: only the engine count is budgeted",
                        self.map_id)
        self._bytes_at_ms, self._bytes_stale = now, False

    def _log_stats(self) -> None:
        every = self.config.stats_log_s
        if not every:
            return
        now = self._mono()
        if self._last_stats_ms is None:
            self._last_stats_ms = now
            return
        if now - self._last_stats_ms < every * 1000:
            return
        self._last_stats_ms = now
        st = self.stats
        log.info("scene worker %s: stats state=%s folded=%d skipped=%s pending=%d buffered=%d "
                 "version=%d lag=%.0fs saves=%d save_failures=%d source_errors=%d rebuilds=%d "
                 "rebuilding=%s resident=%s bytes=%s loads=%d evictions=%d prefetched=%d "
                 "already_folded=%d default_r_d=%d views=%d/%d/%d(ok/no_depth/bad)",
                 self.map_id, self.last_result_state, st.folded, dict(st.skipped),
                 len(self._pending), st.buffered_leaves, st.version, st.version_lag_s, st.saves,
                 st.save_failures, st.source_errors, st.rebuilds, self._rebuild_gen is not None,
                 st.resident, st.resident_bytes, st.loads, st.evictions, st.prefetch_hits,
                 st.already_folded, st.default_r_d, st.views_built, st.views_missing, st.view_failures)

    def _step(self) -> StepResult:
        if self.state in TERMINAL:
            return StepResult(self.state)
        cfg = self.config
        # 1. lifecycle first: nothing below may run for a map that is going away.
        res = self._lifecycle(force=not self._started, where="map deleting or missing")
        if res is not None:
            return res
        if not self._started:
            early = self._start()
            if early is not None:
                return early
        if self._started and self._poll_rebuild_request():
            return StepResult(WORKED)
        if self._event_held is not None:
            self._flush_updated_event()
        # a failed save left folded state in memory: retry it before pulling more
        if self._dirty is not None and self.stats.save_failures and self._due(idle=False, force=True):
            res = self._save()
            if res is not None:
                return res
        # 2. pull (not while a full batch is already waiting: fold that first)
        more = False
        t_step = self._mono()
        # memory guard: pull and prefetch nothing while the container is near its limit; what is buffered is still
        # folded and saved, and the durable cursor means nothing is lost (the next step after the resume re-reads)
        paused = self.memory is not None and bool(self.memory.paused)
        if paused:
            skipped, fetch_error = 0, None
            self.stats.memory_paused_steps += 1
        else:
            skipped, fetch_error = self._retry_pending()
        plan_mode = self._plan_mode()
        if paused:
            more = False
        elif plan_mode:
            self._plan_cut_overdue()
            if fetch_error is None:
                if self._leaf_count() >= self._batch_max():
                    more = True
                else:
                    if self._plan is None:
                        more, err = self._plan_scan()
                        if err is not None:
                            return err
                    if self._plan is not None:      # sealed (now or before): fold from the sorted plan
                        n_skipped, more, fetch_error = self._plan_feed(t_step)
                        skipped += n_skipped
        elif fetch_error is None and self._leaf_count() < self._batch_max():
            since = self._scan if self._scan is not None else (
                max(0, self._candidate[0] - self._margin_ms()), "")
            try:
                nodes = list(self.source.costmap_nodes_since(self.map_id, since[0], since[1], cfg.batch_size))
            except Exception as exc:
                self.stats.source_errors += 1
                self._full_replay = True
                log.warning("scene worker %s: source failed: %s", self.map_id, exc)
                return StepResult(BACKOFF, sleep_s=cfg.backoff_s, detail=f"source: {exc}")
            more = len(nodes) >= cfg.batch_size
            self._scan = self._order(nodes[-1]) if (more and nodes) else None
            # 3. adapt into the buffer (the page's PNGs are fetched + decoded by the prefetch pool meanwhile)
            self._prefetch(nodes)
            for i, node in enumerate(nodes):
                if (cfg.step_budget_s and i and self._mono() - t_step > cfg.step_budget_s * 1000):
                    # out of time for this step: continue behind the last node handled
                    self._scan, more = self._order(nodes[i - 1]), True
                    break
                try:
                    outcome = self._adapt(node)
                except _FetchError as exc:
                    # not a verdict on this leaf: stop here, nothing at or past it is handled, and
                    # the next step re-reads from the margin
                    fetch_error, self._scan, more = exc, None, False
                    break
                skipped += self._accept(node, outcome)
        elif fetch_error is None:
            more = True                       # a backlog waits behind the full batch
        if fetch_error is not None:
            self.stats.source_errors += 1
            self._scan = None
            self._full_replay = True
        if self._leaf_count() and self._buf_since_ms is None:
            self._buf_since_ms = self._mono()
        self._release_leading_skips()
        # a rebuild folds fixed batches; at the head of the nodes the remainder is flushed
        self._head = (not more) and fetch_error is None and not paused
        # 4. fold once per batch
        folded = 0
        self._quarantined = []
        batch = self._take_batch()
        if batch:
            err = self._keep_stamps()
            if err is None:
                err = self._fold([e.leaf for e in batch if e.leaf is not None])
            if err is not None:
                self._scan = None      # re-read from the margin once the cause is gone
                self._full_replay = True
                return err
            folded = len(batch)
            n_leaves = sum(1 for e in batch if e.leaf is not None) - len(self._quarantined)
            self._pop(batch)
            if self._quarantined:
                dead = set(self._quarantined)
                self.stats.skipped[SKIP_FOLD_ERROR] += len(dead)
            self.stats.batches += 1
            self.stats.folded += n_leaves
            self.stats.unsaved_leaves += n_leaves
            self._leaves_since_publish += n_leaves
            self._leaves_since_event += n_leaves
            if self._oldest_unsaved_ms is None and n_leaves:
                self._oldest_unsaved_ms = self._mono()
            self._release_leading_skips()
        # 5-7. save (rate limited), then advance the cursor
        idle = not more and not folded and not skipped      # a re-read of the margin alone is idle
        waiting = self._leaf_count() > 0
        saved: Optional[int] = None
        rebuilding = self._rebuild_gen is not None
        # a rebuild that reached the head (or is past its max age) saves what it folded, then commits
        force_commit = rebuilding and self._rebuild_overdue()
        # (not while leaves are still buffered: the generation would be committed without them)
        at_head = rebuilding and self._head and not self._pending and fetch_error is None and not waiting
        if self._dirty is not None and (self._due(idle=idle)
                                        or ((at_head or force_commit) and self._due(idle=False, force=True))):
            res = self._save()
            if res is not None:
                res.folded, res.skipped = folded, skipped
                return res
            saved = self.stats.version
        elif self._dirty is None:
            self._commit()      # nothing unsaved: terminal skips alone move the cursor
            res = self._persist_cursor()
            if res is not None:
                return res
        if rebuilding and self._dirty is None and fetch_error is None and (
                (at_head and (self._rebuild_saved or (not folded and not waiting)))
                or (force_commit and self._rebuild_saved)):
            res = self._commit_rebuild()
            if res is not None:
                return res
        self._refresh_gauges()
        if fetch_error is not None:
            return StepResult(BACKOFF, folded=folded, skipped=skipped, version=saved,
                              sleep_s=cfg.backoff_s, detail=f"png fetch: {fetch_error}")
        if more or folded or skipped:       # progress: call again at once (the next step may idle-save)
            self._idle_streak = 0
            state, sleep = WORKED, 0.0
        else:
            state = IDLE
            if self._saves_since_gc and cfg.gc_every_saves > 0 and self._dirty is None and not waiting:
                self._gc()
            cap = max(cfg.idle_max_s, cfg.idle_sleep_s)
            sleep = min(cap, cfg.idle_sleep_s * (2 ** min(self._idle_streak, 16)))
            self._idle_streak += 1
            if self._pending:                # a PNG is due again soon: do not oversleep it
                sleep = min(sleep, max(cfg.png_retry_s, 0.05))
            if waiting and self._buf_since_ms is not None:     # a trickle: wake when its wait is up
                left = self._batch_wait_s() - (self._mono() - self._buf_since_ms) / 1000.0
                sleep = min(sleep, max(left, 0.05))
        return StepResult(state, folded=folded, skipped=skipped, version=saved, sleep_s=sleep,
                          settled=state == IDLE and self.settled())

    # ------------------------------------------------------------------ internals

    # ---- rebuild plan (see "Rebuild order" in the module docstring)

    def _plan_mode(self) -> bool:
        return self._rebuild_gen is not None and self.config.rebuild_batch > 0

    def _plan_clear(self) -> None:
        self._plan_recs = self._plan = self._plan_suffix = self._plan_min_ms = None
        self._plan_idx = 0
        self._plan_persist.clear()

    def _plan_stamp(self, node: Mapping[str, Any]) -> Tuple[int, bool]:
        """(stamp ms, keep it), computed once per node in scan order (the session clock learns in that order, as
        it always did); (NO_STAMP, False) when the node has no usable one: it sorts last and the adapt step skips it with its reason."""
        layer = scene_leaf.role_layers(node.get("costmap")).get(scene_leaf.ROLE_OCCUPANCY)
        if not layer:
            return NO_STAMP, False
        try:
            got = scene_leaf.leaf_stamp(node, layer, clock=self.clock,
                                        receipt_window_s=self.config.receipt_window_s)
        except Exception:  # noqa: BLE001  a malformed node: adapt gives its reason
            return NO_STAMP, False
        if got is None:
            return NO_STAMP, False
        return int(got[0]), got[1] not in (scene_leaf.STAMP_STORED, scene_leaf.STAMP_PROVISIONAL)

    def _plan_scan(self) -> Tuple[bool, Optional[StepResult]]:
        """Phase 1: one page of nodes into the plan (no PNG). (more, BACKOFF result or None). The page that is
        not full seals the plan: it is sorted by (stamp, key) and phase 2 starts next step."""
        cfg = self.config
        since = self._scan if self._scan is not None else (
            max(0, self._candidate[0] - self._margin_ms()), "")
        try:
            nodes = list(self.source.costmap_nodes_since(self.map_id, since[0], since[1], cfg.batch_size))
        except Exception as exc:
            self.stats.source_errors += 1
            self._full_replay = True
            log.warning("scene worker %s: source failed: %s", self.map_id, exc)
            return False, StepResult(BACKOFF, sleep_s=cfg.backoff_s, detail=f"source: {exc}")
        more = len(nodes) >= cfg.batch_size
        self._scan = self._order(nodes[-1]) if (more and nodes) else None
        if self._plan_recs is None:
            self._plan_recs = {}
        for node in nodes:
            key, ms = str(node.get("_key", "")), int(node.get("costmap_stored_ms") or 0)
            have = self._plan_recs.get(key)
            if have is not None and have.ms >= ms:
                continue                      # a margin re-read
            stamp, persist = self._plan_stamp(node)
            self._plan_recs[key] = _PlanRec(stamp, key, ms, node, persist)
            if self._plan_min_ms is None or ms < self._plan_min_ms:
                self._plan_min_ms = ms
        if not more:
            self._plan_seal()
            return True, None
        return True, None

    def _plan_seal(self) -> None:
        """Sort the scanned records: the canonical fold order. `_plan_suffix[i]` = lowest stored_ms of
        records i.. (the cursor floor while they are unfolded)."""
        from array import array
        recs = sorted((self._plan_recs or {}).values(), key=lambda r: (r.stamp, r.key))
        suffix = array("q", bytes(8 * (len(recs) + 1)))
        low = 1 << 62
        suffix[len(recs)] = low
        for i in range(len(recs) - 1, -1, -1):
            low = min(low, recs[i].ms)
            suffix[i] = low
        self._plan, self._plan_suffix, self._plan_idx = recs, suffix, 0
        self._plan_recs = None
        self._scan = None

    def _plan_floor_ms(self) -> Optional[int]:
        """Lowest stored_ms among the plan's records that are not fed to the buffer yet (None: none)."""
        if self._plan is None:
            return self._plan_min_ms if self._plan_recs else None
        if self._plan_idx >= len(self._plan):
            return None
        return int(self._plan_suffix[self._plan_idx])

    def _plan_cut_overdue(self) -> None:
        """A rebuild past `rebuild_max_age_s` is committed at its next save with what it folded. Phase 1: the
        scan so far is the plan (a stored_ms prefix). Phase 2 (only once something was saved, as before): the
        unfed rest is dropped and the cursor stays at its floor; the folded leaves above it are in `seen`."""
        if not self._rebuild_overdue():
            return
        if self._plan is None:
            if self._plan_recs is not None and self._scan is not None:
                self._plan_seal()
            return
        if self._rebuild_saved and self._plan_idx < len(self._plan):
            floor = self._plan_floor_ms()
            self._plan_idx = len(self._plan)
            if floor is not None:
                self._candidate = min(self._candidate, (floor, ""))

    def _plan_feed(self, t_step: int) -> Tuple[int, bool, Optional[_FetchError]]:
        """Phase 2: adapt the next records in plan order into the buffer, up to one batch of leaves. The
        PNGs are fetched here (and prefetched a page ahead). (new terminal skips, more, fetch error)."""
        cfg, plan, skipped = self.config, self._plan, 0
        assert plan is not None
        handled = 0
        while self._plan_idx < len(plan):
            need = self._batch_max() - self._leaf_count()
            if need <= 0:
                break
            ahead = plan[self._plan_idx:self._plan_idx + max(need, cfg.batch_size)]
            self._prefetch([r.node for r in ahead])
            for rec in ahead[:need]:
                if cfg.step_budget_s and handled and self._mono() - t_step > cfg.step_budget_s * 1000:
                    return skipped, True, None
                node = rec.node
                if 0 < rec.stamp < NO_STAMP:
                    node = dict(node)
                    node[scene_leaf.STAMP_FIELD] = rec.stamp      # fold under the stamp the plan was sorted by
                    if rec.persist:
                        self._plan_persist.add(rec.key)
                try:
                    outcome = self._adapt(node)
                except _FetchError as exc:
                    return skipped, False, exc
                skipped += self._accept(node, outcome)
                self._plan_idx += 1
                handled += 1
        return skipped, self._plan_idx < len(plan), None

    def _margin_ms(self) -> int:
        """How far below the furthest node this poll starts. The whole replay margin is re-read at
        start, after a failure and every `replay_recheck_s`; other polls re-read a few seconds only,
        so an idle map does not re-read the last minute on every poll (finding 12)."""
        cfg, now = self.config, self._mono()
        if self._full_replay or now - self._last_full_replay_ms >= cfg.replay_recheck_s * 1000:
            self._full_replay, self._last_full_replay_ms = False, now
            return cfg.replay_margin_ms
        return min(cfg.poll_margin_ms, cfg.replay_margin_ms)

    def _batch_max(self) -> int:
        cfg = self.config
        if self._rebuild_gen is not None and cfg.rebuild_batch > 0:
            return min(cfg.batch_max_leaves, cfg.rebuild_batch)
        return cfg.batch_max_leaves

    def _rebuild_overdue(self) -> bool:
        cfg = self.config
        return (self._rebuild_gen is not None and cfg.rebuild_max_age_s > 0
                and self._mono() - self._rebuild_started_ms > cfg.rebuild_max_age_s * 1000)

    def _persist_cursor(self) -> Optional[StepResult]:
        """Skip-only progress (nothing to save) moves the cursor in memory only; make it durable
        once it is more than the persist margin ahead of the stored one. The store moves
        `meta.cursor` in place when only the cursor changed (same version), so this is cheap."""
        cfg = self.config
        rebuilding = self._rebuild_gen is not None
        # a pending generation moves its cursor the same way (store.save of an unchanged state), but only
        # once it holds a saved state: the store refuses to save an empty engine
        if (rebuilding and not self._rebuild_saved) or not self._started or self._expected_version <= 0:
            return None
        margin = cfg.cursor_persist_margin_ms if cfg.cursor_persist_margin_ms is not None \
            else cfg.replay_margin_ms
        target = self._durable(self._candidate)
        if target[0] - self._stored_cursor[0] <= margin:
            return None
        if self._last_save_ms is not None and self._mono() - self._last_save_ms < cfg.min_save_interval_s * 1000:
            return None
        if self.engine is None:
            return None
        res = self._lifecycle(force=True, where="map deleting before cursor persist")
        if res is not None:
            return res
        kwargs: Dict[str, Any] = {}
        gen = self._rebuild_gen if rebuilding else self._generation
        if gen is not None:
            kwargs["generation"] = gen
        try:
            empty = Dirty(bbox=None, changed=[], retired={}, links=[], watermark=self._watermark)
            version = self.store.save(self.map_id, self.engine, empty,
                                      expected_version=self._expected_version, cursor=target, **kwargs)
        except (SaveFenced,) + self.fence_errors as exc:
            return self._terminal(FENCED, str(exc) or "cursor persist fenced")
        except Exception as exc:  # noqa: BLE001  cheap, optional: the next attempt retries
            log.warning("scene worker %s: cursor persist failed: %s", self.map_id, exc)
            self._last_save_ms = self._mono()
            return None
        self._stored_cursor = target
        self._last_save_ms = self._mono()
        if not rebuilding and version is not None and int(version) != self._expected_version:
            self._expected_version = self.stats.version = int(version)    # (a pending save answers its own counter)
        return None

    def _lifecycle(self, force: bool, where: str) -> Optional[StepResult]:
        """The map's lifecycle and (once started) the writer lock. None = fine; DELETING / FENCED
        are terminal; a lifecycle query that FAILS is a BACKOFF, never a verdict. Rate limited to
        `lifecycle_check_s` unless `force` (saves, commits, start)."""
        now = self._mono()
        if (not force and self._lifecycle_ok_ms is not None
                and now - self._lifecycle_ok_ms < self.config.lifecycle_check_s * 1000):
            return None
        try:
            deleting = bool(self.guard.is_deleting(self.map_id))
        except Exception as exc:
            log.warning("scene worker %s: lifecycle check failed: %s", self.map_id, exc)
            if self._started and not self.guard.held(self.map_id):
                return self._terminal(FENCED, "writer lock lost")
            return StepResult(BACKOFF, sleep_s=self.config.backoff_s, detail=f"lifecycle: {exc}")
        if deleting:
            return self._terminal(DELETING, where)
        if self._started and not self.guard.held(self.map_id):
            return self._terminal(FENCED, "writer lock lost")
        self._lifecycle_ok_ms = now
        return None

    def _terminal(self, state: str, detail: str = "") -> StepResult:
        self.state = state
        log.warning("scene worker %s: stopped, %s: %s", self.map_id, state, detail)
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
        version = int(meta.get("version") or 0)
        self._generation = int(meta["generation"]) if meta.get("generation") is not None else None
        self._resume_cursor = None
        rebuilding = False
        request = self._read_rebuild_request(force=True)
        if cfg.expected_params_hash is not None and meta.get("params_hash") not in (None, cfg.expected_params_hash):
            self.rebuild_reason = "params_hash"
            if not self._can_rebuild():
                self._warn_cannot_rebuild("params_hash")
                return self._terminal(NEEDS_REBUILD, "params_hash")
            # a pending generation of the expected params is resumed from its cursor; with a delete
            # request only when it began after the request and is younger than rebuild_max_age_s
            err = self._begin_rebuild("needs_rebuild", request=None, resume_ok=True, covers=request)
            if err is not None:
                self.guard.release(self.map_id)
                return err
            rebuilding = True
            log.info("scene worker %s: params changed (%s -> %s): rebuilding in generation %d%s",
                     self.map_id, meta.get("params_hash"), cfg.expected_params_hash, self._rebuild_gen,
                     " (resumed)" if self._resume_cursor is not None else "")
        if cfg.expected_generation is not None and meta.get("generation") not in (None, cfg.expected_generation):
            self.rebuild_reason = "generation"
            return self._terminal(NEEDS_REBUILD, "generation")
        if request is not None:
            if rebuilding:
                self._rebuild_request = request     # this rebuild covers it; cleared at commit
            elif self._can_rebuild() and version > 0:
                err = self._begin_rebuild(str(request.get("reason") or "nodes_deleted"), request,
                                          resume_ok=True)
                if err is not None:
                    self.guard.release(self.map_id)
                    return err
                rebuilding = True
            elif version == 0:
                self._clear_request(request)        # nothing was ever saved: the first build is fresh
            else:
                self._warn_cannot_rebuild("rebuild request")
        lazy = (cfg.lazy_load and self.engine is None and self.engine_factory is not None
                and version > 0 and not rebuilding)
        if not rebuilding and not lazy:
            err = self._ensure_engine(version)
            if err is not None:
                if not self._load_gave_up:
                    self.guard.release(self.map_id)
                    return err
                # the stored graph cannot be loaded and retrying will not change that
                if self._can_rebuild() and version > 0:
                    err = self._begin_rebuild("load_failed")
                    if err is not None:
                        self.guard.release(self.map_id)
                        return err
                    rebuilding = True
                    self._load_gave_up, self._load_failures = False, 0
                else:
                    self.rebuild_reason = "load_failed"
                    return self._terminal(NEEDS_REBUILD, f"load_failed: {err.detail}")
        stored = [c for c in (self._cursor_of(self.cursor_store.get(self.map_id)),
                              self._cursor_of(meta.get("cursor"))) if c is not None]
        if rebuilding:
            self._cursor = self._resume_cursor or ZERO_CURSOR
        else:
            self._cursor = min(stored) if stored else ZERO_CURSOR
        self._stored_cursor = self._cursor_of(meta.get("cursor")) or ZERO_CURSOR
        # Leaves the stored state holds may be skipped while it is the state this worker folds into: not in a
        # rebuild (a new engine) and not after the engine was dropped (`_reset_fold_state`).
        self._known, self._known_version = None, version
        self._known_cursor = self._stored_cursor
        self._known_off = rebuilding or version <= 0 or not cfg.skip_folded
        if rebuilding and self._resume_cursor is not None:
            self._skip_held_by_pending()
        self._candidate = self._cursor
        self._expected_version = version
        self.stats.version = version
        self._started = True
        self._full_replay = True
        self._lifecycle_ok_ms = self._mono()
        return None

    def _warn_cannot_rebuild(self, what: str) -> None:
        if not self._warned_cannot_rebuild:
            self._warned_cannot_rebuild = True
            log.warning("scene worker %s: %s needs a rebuild but this worker cannot rebuild (no "
                        "new_engine / begin_generation)", self.map_id, what)

    # exceptions of the store that retrying cannot change (a corrupt / mismatched stored graph). ValueError
    # covers the engine's "state was made with other parameters" and json.JSONDecodeError (a subclass).
    _DETERMINISTIC = (StateMismatch, TooOld, FileNotFoundError, KeyError, ValueError, json.JSONDecodeError,
                      IndexError, AssertionError)

    @classmethod
    def _deterministic(cls, exc: BaseException) -> bool:
        return isinstance(exc, cls._DETERMINISTIC)

    def _ensure_engine(self, version: int) -> Optional[StepResult]:
        """The engine for the stored `version`, built now that the writer lock is held. A engine
        handed in is kept when it was loaded at this version (or says nothing about it); one that
        raced the lock (loaded at another version) is reloaded through the factory. A load that
        fails deterministically `load_fail_limit` times in a row sets `_load_gave_up`: the caller
        rebuilds (or stops with NEEDS_REBUILD) instead of loading the whole map every backoff."""
        if self.engine is not None and self._loaded_version in (None, version):
            return None
        if self.engine_factory is None:
            log.warning("scene worker %s: engine loaded at version %s, store is at %s and there is no "
                        "engine_factory to reload it", self.map_id, self._loaded_version, version)
            return StepResult(BACKOFF, sleep_s=self.config.backoff_s,
                              detail=f"engine at {self._loaded_version}, store at {version}")
        gate = self.load_gate
        if gate is not None:
            refused = gate.refusal(self.map_id)
            if refused is not None:
                reason, retry_s = refused
                if reason != self._load_refused:
                    self._load_refused = reason
                    log.warning("scene worker %s: engine load refused: %s", self.map_id, reason)
                return StepResult(BACKOFF, sleep_s=max(retry_s, self.config.backoff_s), detail=f"load refused: {reason}")
            self._load_refused = ""
            gate.begin(self.map_id)
        try:
            self.engine = self.engine_factory(version)
        except Exception as exc:
            if gate is not None:
                gate.done(self.map_id, False)
            log.exception("scene worker %s: loading the engine (version %s) failed", self.map_id, version)
            self._failed("load", f"{type(exc).__name__}: {exc}")
            if self._deterministic(exc):
                self._load_failures += 1
                if self._load_failures >= max(1, self.config.load_fail_limit):
                    self._load_gave_up = True
            else:
                self._load_failures = 0
            return StepResult(BACKOFF, sleep_s=self.config.backoff_s, detail=f"load: {exc}")
        if gate is not None:
            gate.done(self.map_id, True)
        self._loaded_version = version
        self._load_failures = 0
        self.stats.loads += 1
        self._bytes_stale = True
        self._recovered("load")
        return None

    # ---- rebuild requests (status.scene_graph_needs_rebuild, set by the API on node delete)

    def _can_rebuild(self) -> bool:
        return self.new_engine is not None and hasattr(self.store, "begin_generation")

    def _read_rebuild_request(self, force: bool = False) -> Optional[Mapping[str, Any]]:
        """The map's pending rebuild request ({at, reason, nodes}) or None. Asks the guard at most
        once per `rebuild_check_s` (always when `force`); a guard without the lookup, a failed
        query and a value without `at` all mean "none"."""
        look = getattr(self.guard, "rebuild_request", None)
        interval = self.config.rebuild_check_s
        if look is None or interval <= 0:
            return None
        now = self._mono()
        if (not force and self._request_checked_ms is not None
                and now - self._request_checked_ms < interval * 1000):
            return None
        self._request_checked_ms = now
        try:
            request = look(self.map_id)
        except Exception:
            log.exception("scene worker %s: rebuild request lookup failed", self.map_id)
            return None
        if isinstance(request, Mapping) and request.get("at"):
            return request
        return None

    def _clear_request(self, request: Mapping[str, Any]) -> None:
        """Remove the key if it still carries `request["at"]` (a newer delete keeps it)."""
        clear = getattr(self.guard, "clear_rebuild_request", None)
        at = str(request.get("at"))
        try:
            if clear is not None:
                clear(self.map_id, at)
            self._clear_failed_at = None
        except Exception:
            self._clear_failed_at = at
            log.exception("scene worker %s: clearing the rebuild request failed", self.map_id)

    def _poll_rebuild_request(self) -> bool:
        """Mid-run: act on a request. True when a rebuild generation was begun (state was reset).
        A request is acted on once it has been the newest for `rebuild_debounce_s` (a burst of
        deletes is one rebuild)."""
        if self._rebuild_gen is not None:
            return False                # a rebuild is running; its commit clears what it answers
        if self._clear_failed_at is not None:
            self._clear_request({"at": self._clear_failed_at})
        request = self._read_rebuild_request()
        if request is not None:
            at = str(request.get("at"))
            if at != self._request_seen:
                self._request_seen, self._request_seen_ms = at, self._mono()
        if request is None:
            return False
        if not self._can_rebuild():
            self._warn_cannot_rebuild("rebuild request")
            return False
        debounce = self.config.rebuild_debounce_s
        if debounce > 0 and self._mono() - self._request_seen_ms < debounce * 1000:
            # the request is looked up again after `rebuild_check_s` and acted on then
            return False
        if self._expected_version == 0:
            # nothing saved yet: drop the in-memory fold and start over, no generation needed
            self.engine = self.new_engine()
            self._reset_fold_state()
            self._clear_request(request)
            log.info("scene worker %s: rebuild request on a never-saved graph: restarted the fold", self.map_id)
            return True
        if self._begin_rebuild(str(request.get("reason") or "nodes_deleted"), request) is not None:
            return False                # begin_generation failed: asked again after the interval
        self._reset_fold_state()
        return True

    # -- store API probing (the store grows `fresh=`, `params_hash=`, meta()["pending"], load(pending=))

    @staticmethod
    def _params_of(fn: Any) -> frozenset:
        try:
            return frozenset(inspect.signature(fn).parameters)
        except (TypeError, ValueError):
            return frozenset()

    def _store_pending(self) -> Optional[Dict[str, Any]]:
        """The store's pending generation as {generation, version, params_hash, cursor}, or None.
        Reads `meta()["pending"]` (new API) and falls back to the raw doc / manifest (old API)."""
        try:
            meta = self.store.meta(self.map_id) or {}
            info = meta.get("pending")
            if not isinstance(info, Mapping):
                docs = getattr(self.store, "docs", None)
                raw = (docs.meta(self.map_id) or {}).get("pending") if docs is not None else None
                info = raw if isinstance(raw, Mapping) else None
            if info is None:
                return None
            info = dict(info)
            if info.get("params_hash") is None and info.get("manifest") and hasattr(self.store, "_raw_manifest"):
                man = self.store._raw_manifest(self.map_id, info["manifest"]) or {}
                info["params_hash"], info["cursor"] = man.get("params_hash"), man.get("cursor")
            return info
        except Exception as exc:  # noqa: BLE001
            log.warning("scene worker %s: pending generation lookup failed: %s", self.map_id, exc)
            return None

    def _load_pending(self) -> Any:
        """The engine restored from the pending generation, or None when the store cannot."""
        store = self.store
        try:
            if "pending" in self._params_of(getattr(store, "load", None)):
                return store.load(self.map_id, pending=True)
            loader = getattr(store, "load_pending", None)
            return loader(self.map_id) if loader is not None else None
        except Exception as exc:  # noqa: BLE001
            log.warning("scene worker %s: loading the pending generation failed: %s", self.map_id, exc)
            return None

    def _begin_generation(self, resume: bool) -> int:
        """Begin (resume=False: always fresh and EMPTY, discarding a pending one) or resume the
        pending generation. Feature-detects `fresh=` / `params_hash=`; on the old API a stale
        pending generation is cleared through the doc store before it is begun again."""
        begin = self.store.begin_generation
        sig = self._params_of(begin)
        kwargs: Dict[str, Any] = {}
        expected = self.config.expected_params_hash
        if "params_hash" in sig and expected is not None:
            kwargs["params_hash"] = expected
        if "rebuild_batch" in sig and self.config.rebuild_batch > 0:
            kwargs["rebuild_batch"] = int(self.config.rebuild_batch)
        if not resume:
            if "fresh" in sig:
                kwargs["fresh"] = True
            else:
                self._clear_pending_doc()
        return int(begin(self.map_id, **kwargs))

    def _clear_pending_doc(self) -> None:
        docs = getattr(self.store, "docs", None)
        if docs is None:
            return
        meta = docs.meta(self.map_id)
        if meta and meta.get("pending"):
            new = {k: v for k, v in meta.items() if k != "pending"}
            if not docs.cas_meta(self.map_id, meta["version"], new):
                raise SaveFenced("lost the compare-and-set clearing the pending generation")

    @staticmethod
    def _request_ms(request: Mapping[str, Any]) -> Optional[int]:
        """Epoch ms of a rebuild request's `at` (an ISO timestamp; naive means UTC), None when unreadable."""
        try:
            at = datetime.fromisoformat(str(request.get("at")))
        except (TypeError, ValueError):
            return None
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        return int(at.timestamp() * 1000)

    def _may_resume(self, pend: Mapping[str, Any], request: Optional[Mapping[str, Any]]) -> bool:
        """Whether the pending generation may be extended. Without a delete request: yes (a params
        rebuild that was interrupted). With one: only when the pending generation began (`begun_ms`, the
        store's clock) after the request was made, so it cannot hold a leaf deleted by it, and is younger
        than `rebuild_max_age_s`; an old-format pending record (no `begun_ms`) restarts."""
        if request is None:
            return True
        begun = pend.get("begun_ms")
        if begun is None:
            return False
        try:
            begun = int(begun)
        except (TypeError, ValueError):
            return False
        max_age = self.config.rebuild_max_age_s
        if max_age > 0 and self.now_ms() - begun >= max_age * 1000:
            return False
        at = self._request_ms(request)
        return at is not None and begun >= at

    def _begin_rebuild(self, reason: str, request: Optional[Mapping[str, Any]] = None,
                       resume_ok: bool = False, covers: Optional[Mapping[str, Any]] = None) -> Optional[StepResult]:
        """Begin a pending generation, switch to a fresh engine and announce it. None on success,
        a BACKOFF result when begin_generation failed (the caller decides about the lock).
        With `resume_ok` a pending generation of the expected params that holds a cursor is
        resumed (its engine restored, the scan continues from its cursor) when `_may_resume` allows
        it for the delete request (`request`, or `covers` for a params rebuild that answers one);
        otherwise the pending generation starts EMPTY (a stale one is never extended)."""
        engine, resumed = None, None
        if resume_ok:
            pend = self._store_pending()
            expected = self.config.expected_params_hash
            if (pend and expected is not None and pend.get("params_hash") == expected
                    and pend.get("cursor") is not None and self._cursor_of(pend.get("cursor")) is not None
                    and self._may_resume(pend, request if request is not None else covers)):
                engine = self._load_pending()
                if engine is not None:
                    resumed = self._cursor_of(pend.get("cursor"))
        try:
            gen = self._begin_generation(resume=resumed is not None)
        except Exception as exc:
            log.warning("scene worker %s: begin_generation failed: %s", self.map_id, exc)
            return StepResult(BACKOFF, sleep_s=self.config.backoff_s, detail=f"begin_generation: {exc}")
        # fold every leaf again, under the configured params, into the pending generation
        self.engine = engine if engine is not None else self.new_engine()
        self._resume_cursor = resumed
        self._rebuild_gen, self._rebuild_saved = gen, resumed is not None
        self._rebuild_started_ms = self._mono()
        self._empty_confirms = self._commit_failures = 0
        self._rebuild_event_reason = reason
        self._rebuild_request = request
        extra = {"resumed": True} if resumed is not None else {}
        self._event("rebuild_started", reason=reason, **extra)
        if request is not None:
            log.info("scene worker %s: rebuild requested (%s, %s nodes): generation %d",
                     self.map_id, reason, request.get("nodes"), gen)
        return None

    def _skip_held_by_pending(self) -> None:
        """A resumed (or reloaded) pending generation holds leaves in sorted-plan order, which is not a stored_ms
        prefix: leaves it already holds are skipped by key wherever they sit relative to the cursor."""
        self._known, self._known_off, self._rebuild_known = None, False, True
        self._known_cursor = (1 << 62, "")

    def _reset_fold_state(self) -> None:
        """Forget everything folded or buffered: the engine is new and every leaf is read again."""
        self._buf.clear()
        self._buffered.clear()
        self._seen.clear()
        self._dup_counted.clear()
        self._known, self._known_off = None, True      # the engine is new or rewound: fold everything again
        self._rebuild_known = False
        self._plan_clear()
        self._pending.clear()
        self._idle_streak = 0
        self._scan = None
        self._dirty = None
        self._oldest_unsaved_ms = None
        self._leaves_since_publish = 0
        self._cursor = self._candidate = ZERO_CURSOR
        self._buf_since_ms = None
        self._full_replay = True
        self._outage_since = None
        st = self.stats
        st.unsaved_leaves = st.save_failures = st.fold_failures = 0
        self._fail_stage, self._fail_count = "", 0
        self._refresh_gauges()

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
        if self._half_side is not None:
            return self._half_side, True
        robot = next((str(node[f]) for f in self.config.robot_fields if node.get(f)), "")
        now = self._mono()
        hit = self._footprints.get(robot)
        if hit and (hit[1] or now - hit[2] < self.config.footprint_retry_s * 1000):
            return hit[0], hit[1]
        try:
            length, width = self.footprint(robot)
        except Exception:
            length, width = -1, -1
        r, known = scene_adapter.half_side(length, width, self.config.default_half_side)
        self._footprints[robot] = (r, known, now)
        if not known:
            log.warning("scene worker %s: footprint of robot %r unknown (%s): folding its leaves with the "
                        "default r_d %.2f m (counted in default_r_d); asked again in %.0fs", self.map_id,
                        robot or "<node has no robot_name>", "no factsheet row or length/width -1",
                        r, self.config.footprint_retry_s)
        return r, known

    def _adapt(self, node: Mapping[str, Any], retry: bool = False):
        """Leaf, a terminal skip reason (str), "duplicate" or "pending". RAISES `_FetchError` when
        the PNG fetch itself failed (its state is unknown: nothing may be decided about the leaf)."""
        key, ms = str(node.get("_key", "")), int(node.get("costmap_stored_ms") or 0)
        if self._buffered.get(key) == ms:       # a margin re-read of an entry that waits for its batch
            return "duplicate"
        if key in self._pending and not retry:  # asked again by _retry_pending, on its own clock
            return "duplicate"
        if self._seen.get(key) == ms:
            if self._dup_counted.get(key) != ms:    # each node (key, stored_ms) counts once, not per poll
                self._dup_counted[key] = ms
                self.stats.duplicates += 1
            return "duplicate"
        layer = scene_leaf.role_layers(node.get("costmap")).get(scene_leaf.ROLE_OCCUPANCY)
        if not layer:
            return scene_adapter.SKIP_NO_LAYER
        if not retry and self._state_holds(node, None):
            return self._already_folded()
        window, bad_png = None, False
        ticket = None if retry else self._tickets.pop((key, ms), None)
        try:
            if ticket is not None:
                png, window, decode_error = ticket.result()
                bad_png = decode_error is not None
                if png is not None:
                    self.stats.prefetch_hits += 1
                    self.prefetch.hits += 1
            else:
                png = self.fetch_png(self.map_id, key, layer)
        except Exception as exc:
            log.warning("scene worker %s: png fetch of %s failed: %s", self.map_id, key, exc)
            if self._outage_since is None:
                self._outage_since = self._mono()
            raise _FetchError(f"{key}: {exc}") from exc
        self._fetch_recovered()
        if png is None:                          # confirmed missing: not there (yet)
            now = self._mono()
            held = self._pending.get(key)
            if held is None:
                self._pending[key] = _Pending(node, ms, now, now)
            else:
                held.tried_ms = now
            return "pending"
        self._pending.pop(key, None)
        r_d, known = self._robot_half_side(node)
        info: Dict[str, Any] = {}
        vinfo: Dict[str, Any] = {}
        leaf, reason = scene_adapter.to_leaf(node, png, r_d=r_d, layer=layer, clock=self.clock,
                                             stamp_info=info, window=window, bad_png=bad_png,
                                             receipt_window_s=self.config.receipt_window_s,
                                             view_info=vinfo)
        if leaf is None:
            return reason or "unknown"
        self._count_view(key, vinfo.get("view"))
        if not retry and self._state_holds(node, leaf):
            return self._already_folded()
        # Persist the stamp only when it is final. A stored stamp wins forever (first write wins on
        # the node), and the provisional one is the receipt time given to a session whose clock is
        # not trusted yet (< min_obs observations): keeping it would freeze the leaf at "receipt"
        # while the later leaves of the session are corrected. It is never newer than the receipt,
        # so not keeping it only lets a replay pick the better estimate.
        # (a rebuild plan injects its stamp as stored: `_plan_persist` names the ones it computed fresh)
        fresh = key in self._plan_persist
        self._plan_persist.discard(key)
        if fresh or info.get("source") not in (scene_leaf.STAMP_STORED, scene_leaf.STAMP_PROVISIONAL):
            self._new_stamps[key] = int(leaf.stamp_ms)
        if not known:
            self.stats.default_r_d += 1
        return leaf

    # ---- restart replay: leaves the stored state already holds

    def _known_leaves(self) -> Optional[Dict[str, tuple]]:
        """`{key: (stamp_ms, session, x, y, r_d, view)}` of the state this worker started from (read once,
        lazily), or None when skipping is off or the leaves cannot be read (then everything is folded)."""
        if self._known_off or (self._rebuild_gen is not None and not self._rebuild_known):
            return None
        if self._known is None:
            index = None
            try:
                if self.stored_leaves is not None and self._known_version > 0 and self._rebuild_gen is None:
                    index = self.stored_leaves(self._known_version)
                elif self.engine is not None and hasattr(self.engine, "export_state"):
                    index = {r.key: (int(r.t), str(r.session), float(r.x), float(r.y), float(r.r_d),
                                     None if r.view is None else tuple(float(v) for v in r.view))
                             for r in getattr(self.engine, "export_state_lazy", self.engine.export_state)().leaves}
            except Exception as exc:  # noqa: BLE001 - an optimisation only
                log.warning("scene worker %s: stored leaves unreadable (%s: %s): the replay folds them "
                            "again", self.map_id, type(exc).__name__, exc)
            if index is None:
                self._known_off = True
                return None
            self._known = dict(index)
        return self._known

    def _state_holds(self, node: Mapping[str, Any], leaf: Any) -> bool:
        """True when the stored state already holds this leaf: its key is there with the same stamp, session,
        pose, footprint and view, and the node was stored at or before the cursor that state was saved with
        (so the layer it holds is the one that was folded: a costmap that is stored again gets a newer
        `costmap_stored_ms`). With `leaf` None the leaf is not built yet: the stamp is the node's kept
        `scene_stamp_ms`, the rest comes from the node, and a node without a kept stamp is not decided here."""
        key = str(node.get("_key", ""))
        if (int(node.get("costmap_stored_ms") or 0), key) > self._known_cursor:
            return False
        known = self._known_leaves()
        rec = known.get(key) if known is not None else None
        if rec is None:
            return False
        if leaf is not None:
            return rec == (int(leaf.stamp_ms), str(leaf.session), float(leaf.x), float(leaf.y),
                           float(leaf.r_d), None if leaf.view is None else
                           (float(leaf.view.x), float(leaf.view.y), float(leaf.view.yaw),
                            float(leaf.view.fov), float(leaf.view.rng)))
        kept = node.get(scene_leaf.STAMP_FIELD)
        if not isinstance(kept, (int, float)) or isinstance(kept, bool) or int(kept) != rec[0]:
            return False
        pose = node.get("pose") or {}
        stored = node.get("costmap") or {}
        layer = scene_leaf.role_layers(stored).get(scene_leaf.ROLE_OCCUPANCY)
        record = stored.get(layer) if layer else None
        session = str(node.get("session_id") or (record or {}).get("session_id") or "")
        try:
            same_pose = (float(pose["x"]), float(pose["y"])) == (rec[2], rec[3])
        except (KeyError, TypeError, ValueError):
            return False
        if not same_pose or session != rec[1] or float(self._robot_half_side(node)[0]) != rec[4]:
            return False
        view, _ = scene_adapter.build_view(node, key, float(pose["x"]), float(pose["y"]), float(pose.get("yaw", 0.0)))
        return rec[5] == (None if view is None else (float(view.x), float(view.y), float(view.yaw),
                                                     float(view.fov), float(view.rng)))

    def _already_folded(self) -> str:
        self.stats.already_folded += 1
        return SKIP_ALREADY_FOLDED

    def _count_view(self, key: str, state: Optional[str]) -> None:
        st = self.stats
        if state == scene_adapter.VIEW_OK:
            st.views_built += 1
        elif state == scene_adapter.VIEW_NO_DEPTH:
            st.views_missing += 1
        elif state == scene_adapter.VIEW_BAD_CAMERA:
            st.view_failures += 1
            if not self._view_warned:
                self._view_warned = True
                log.warning("scene worker %s: node %s has a depth record but no usable camera block: its "
                            "leaf is folded without a view (cover() ignores it); further ones are only "
                            "counted (view_failures)", self.map_id, key)

    def _would_fetch(self, node: Mapping[str, Any]) -> Optional[Tuple[str, int, str]]:
        """(key, stored_ms, layer) when `_adapt` would fetch this node's PNG, else None."""
        key, ms = str(node.get("_key", "")), int(node.get("costmap_stored_ms") or 0)
        if self._buffered.get(key) == ms or key in self._pending or self._seen.get(key) == ms:
            return None
        layer = scene_leaf.role_layers(node.get("costmap")).get(scene_leaf.ROLE_OCCUPANCY)
        return (key, ms, layer) if layer else None

    def _prefetch(self, nodes: Sequence[Mapping[str, Any]]) -> None:
        """Hand the PNGs of this page to the prefetch pool, in order, while its bound has room. What it
        does not take is fetched inline by `_adapt`, exactly as without a pool."""
        if self.prefetch is None:
            return
        for node in nodes:
            want = self._would_fetch(node)
            if want is None:
                continue
            key, ms, layer = want
            if (key, ms) in self._tickets:
                continue
            ticket = self.prefetch.submit(self.fetch_png, self.map_id, key, layer)
            if ticket is None:
                break
            self._tickets[(key, ms)] = ticket

    def _drop_tickets(self) -> None:
        """Tickets the step did not consume (it stopped early): their slots go back to the pool."""
        if self._tickets:
            tickets, self._tickets = self._tickets, {}
            for ticket in tickets.values():
                ticket.release()

    def _fetch_recovered(self) -> None:
        """A fetch answered again: the time it was failing does not count towards a pending
        PNG's expiry (its wait is shifted by the outage)."""
        if self._outage_since is None:
            return
        gap = max(0, self._mono() - self._outage_since)
        self._outage_since = None
        for held in self._pending.values():
            held.first_ms += gap

    def _accept(self, node: Mapping[str, Any], outcome: Any) -> int:
        """Buffer an adapt outcome. Returns 1 when it was a NEW terminal skip (progress), else 0."""
        if outcome in ("duplicate", "pending"):
            return 0
        key, ms = str(node.get("_key", "")), int(node.get("costmap_stored_ms") or 0)
        self._buffered[key] = ms      # leaves and skips alike: a margin re-read must not queue it again
        if isinstance(outcome, str):
            self.stats.skipped[outcome] += 1
            if outcome != SKIP_ALREADY_FOLDED and outcome not in self._skip_logged:
                self._skip_logged.add(outcome)       # the first of each reason; the rest is counted
                log.warning("scene worker %s: node %s skipped (%s); further skips of this reason are only "
                            "counted (stats skipped)", self.map_id, key, outcome)
            self._buf.append(_Entry(node, None))
            return 1
        self._buf.append(_Entry(node, outcome))
        return 0

    def _retry_pending(self) -> Tuple[int, Optional[_FetchError]]:
        """Ask again for PNGs that were missing; give up on those older than `png_expire_s` of
        MONOTONIC wall clock, not counting a MinIO outage (stored time is no clock: a backlog moves
        through it in seconds). One last fetch is made before expiring. At most
        `png_retries_per_step` are asked per step. An expired node is routed through the buffer
        as a terminal skip, so the cursor never passes leaves still waiting there. Returns (new
        terminal skips, the fetch error that stopped the retries or None)."""
        cfg, skipped, error, asked = self.config, 0, None, 0
        if self._pending:
            now = self._mono()
            expire_ms = (cfg.png_expire_s if cfg.png_expire_s is not None
                         else cfg.replay_margin_ms / 1000.0) * 1000.0
            cap = cfg.png_retries_per_step
            for key, held in list(self._pending.items()):
                if self._outage_since is None and now - held.first_ms > expire_ms:
                    held_node = held.node
                    waited = (now - held.first_ms) / 1000.0
                    try:
                        outcome = self._adapt(held_node, retry=True)      # one last look
                    except _FetchError as exc:
                        error = exc
                        break
                    if outcome == "pending":
                        del self._pending[key]
                        log.warning("scene worker %s: node %s: PNG still missing after %.0fs, giving up",
                                    self.map_id, key, waited)
                        skipped += self._accept(held_node, SKIP_PNG_EXPIRED)
                    else:
                        skipped += self._accept(held_node, outcome)
                    continue
                if now - held.tried_ms < cfg.png_retry_s * 1000:
                    continue
                if cap and asked >= cap:
                    break
                asked += 1
                try:
                    outcome = self._adapt(held.node, retry=True)
                except _FetchError as exc:
                    error = exc
                    break
                skipped += self._accept(held.node, outcome)
        self.stats.pending = len(self._pending)
        floor = self._candidate[0] - cfg.replay_margin_ms
        # (not while a rebuild plan is being folded: the candidate runs ahead of the unfolded records there)
        for key in [k for k, ms in self._seen.items() if ms < floor and self._plan is None]:
            del self._seen[key]
            self._dup_counted.pop(key, None)
        for key in [k for k in self._dup_counted if k not in self._seen]:
            del self._dup_counted[key]
        return skipped, error

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
            self._new_stamps = {**stamps, **self._new_stamps}   # the leaves stay buffered: keep theirs
            self._scan = None
            return StepResult(BACKOFF, sleep_s=self.config.backoff_s, detail=f"stamps: {exc}")
        return None

    # ---- batching

    def _leaf_count(self) -> int:
        return sum(1 for e in self._buf if e.leaf is not None)

    def _batch_wait_s(self) -> float:
        return float(self.config.min_save_interval_s)

    def _take_batch(self) -> List[_Entry]:
        """The entries (leaves and the skips between them) of the next fold, or []: fewer than
        batch_min_leaves wait and the oldest has not waited the publish interval yet."""
        cfg, n = self.config, self._leaf_count()
        if not n:
            return []
        waited_s = 0.0 if self._buf_since_ms is None else (self._mono() - self._buf_since_ms) / 1000.0
        if self._rebuild_gen is not None and cfg.rebuild_batch > 0:
            # canonical rebuild: fixed batches, no time-based flush; the remainder at the head
            if n < cfg.rebuild_batch and not self._head and not self._rebuild_overdue():
                return []
            take = max(1, cfg.rebuild_batch)
        else:
            if n < max(1, cfg.batch_min_leaves) and waited_s < self._batch_wait_s():
                return []
            take = max(1, cfg.batch_max_leaves)
        out = []
        for entry in self._buf:
            out.append(entry)
            if entry.leaf is not None:
                take -= 1
                if take == 0:
                    break
        return out

    def _handled(self, node: Mapping[str, Any]) -> None:
        key, ms = str(node.get("_key", "")), int(node.get("costmap_stored_ms") or 0)
        self._seen[key] = ms
        self._candidate = max(self._candidate, (ms, key))

    def _pop(self, batch: List[_Entry]) -> None:
        del self._buf[:len(batch)]
        for entry in batch:
            self._handled(entry.node)
            self._buffered.pop(str(entry.node.get("_key", "")), None)
        self._buf_since_ms = self._mono() if self._leaf_count() else None

    def _release_leading_skips(self) -> None:
        """Terminal skips in front of the first waiting leaf (or all of them) are done: they move the cursor."""
        n = 0
        while n < len(self._buf) and self._buf[n].leaf is None:
            n += 1
        if n:
            for entry in self._buf[:n]:
                self._handled(entry.node)
                self._buffered.pop(str(entry.node.get("_key", "")), None)
            del self._buf[:n]

    # ---- events

    def _event(self, kind: str, **payload: Any) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(kind, payload)
        except Exception:
            log.exception("scene worker %s: event %s failed", self.map_id, kind)

    def _failed(self, stage: str, detail: str) -> None:
        """Count a failure of `stage` (fold | tiles | save); report the 1st, 2nd, 4th, 8th ... in a row."""
        if stage == self._fail_stage:
            self._fail_count += 1
        else:
            self._fail_stage, self._fail_count = stage, 1
        if stage in ("fold", "tiles"):
            self.stats.fold_failures = self._fail_count
        n = self._fail_count
        if n & (n - 1) == 0:
            self._event("fold_failed", stage=stage, detail=detail[:300], consecutive=n)

    def _recovered(self, *stages: str) -> None:
        if self._fail_stage in stages:
            self._fail_stage, self._fail_count = "", 0
            if "fold" in stages:
                self.stats.fold_failures = 0

    # ---- fold

    def _fold(self, leaves: List[Any]) -> Optional[StepResult]:
        cfg = self.config
        if self.engine is None:
            # dropped after a fault and the reload failed then: load it again before folding
            res = self._reload_engine()
            if res is not None:
                return res
        t0 = self._mono()
        failure = self._try_fold(leaves)
        if failure is None:
            return self._folded(len(leaves), t0)
        kind, exc = failure
        if kind == "tiles":
            self._failed("tiles", "missing tiles")
            return StepResult(BACKOFF, sleep_s=cfg.backoff_s, detail="missing tiles")
        if kind == "leaf":
            # LeafError: raised before anything was mutated, the engine is bit-identical: the leaf is
            # the cause, quarantine it and fold the rest
            return self._quarantine_leaves(leaves, exc, t0)
        if kind == "memory":
            return self._out_of_memory(leaves, exc)
        return self._engine_fault(exc)

    def _out_of_memory(self, leaves: List[Any], exc: BaseException) -> StepResult:
        """MemoryError of a fold: nothing changed, so a backoff retries it. The SAME batch (same first leaf)
        failing `memory_retry_limit` times in a row is not retried forever: it is bisected (smaller folds
        need less memory) and a leaf that still runs out of memory alone is quarantined like a poison leaf."""
        cfg = self.config
        head = str(getattr(leaves[0], "key", "")) if leaves else ""
        self._memory_streak = self._memory_streak + 1 if head == self._memory_head else 1
        self._memory_head = head
        if self._memory_streak >= max(1, cfg.memory_retry_limit) and len(leaves) > 0:
            self._memory_head, self._memory_streak = None, 0
            self.stats.memory_isolations += 1
            log.error("scene worker %s: fold of %d leaves ran out of memory %d times: bisecting",
                      self.map_id, len(leaves), cfg.memory_retry_limit, exc_info=exc)
            self._failed("fold", f"MemoryError: {exc}")
            return self._isolate(list(leaves), exc, bisect_memory=True)
        log.error("scene worker %s: fold ran out of memory (nothing changed)", self.map_id, exc_info=exc)
        self._failed("fold", f"MemoryError: {exc}")
        return StepResult(BACKOFF, sleep_s=cfg.backoff_s, detail=f"fold: {exc!r}")

    def _folded(self, n: int, t0: int) -> None:
        self._fold_timed(n, (self._mono() - t0) / 1000.0)
        self._fault_streak = 0
        self._memory_head, self._memory_streak = None, 0
        self._bytes_stale = True
        self._recovered("fold", "tiles")
        return None

    def _try_fold(self, leaves: List[Any]) -> Optional[Tuple[str, Optional[BaseException]]]:
        """One fold_many (tiles loaded and retried once). None on success, else ("tiles", None),
        ("leaf", LeafError: nothing mutated), ("memory", MemoryError: nothing changed) or
        ("fault", exc: EngineFaulted or anything raised after the engine began to mutate)."""
        for attempt in (0, 1):
            try:
                dirty = self.engine.fold_many(leaves)
                self._dirty = merge_dirty(self._dirty, dirty)
                self._watermark = tuple(getattr(dirty, "watermark", self._watermark))
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
            except LeafError as exc:
                return "leaf", exc
            except MemoryError as exc:
                return "memory", exc
            except Exception as exc:
                return "fault", exc
        return "tiles", None

    def _quarantine(self, leaf: Any, exc: BaseException) -> None:
        key = str(getattr(leaf, "key", "?"))
        self._quarantined.append(key)
        log.error("scene worker %s: leaf %s cannot be folded, quarantined: %s", self.map_id, key, exc)
        self._event("fold_failed", stage="poison_leaf", detail=f"{key}: {exc}"[:300], consecutive=1)

    def _quarantine_leaves(self, leaves: List[Any], exc: BaseException, t0: int) -> Optional[StepResult]:
        """`exc` is a LeafError of the batch. The leaf it names (`exc.key`) is quarantined (skip reason
        `fold_error`, an event, the cursor moves past it) and the rest folded; the next LeafError of the
        rest names the next one. A key that matches no leaf of the batch falls back to bisecting."""
        remaining = list(leaves)
        while True:
            key = getattr(exc, "key", None)
            bad = next((l for l in remaining if key is not None and str(getattr(l, "key", None)) == str(key)), None)
            if bad is None:
                return self._isolate(remaining, exc)
            remaining = [l for l in remaining if l is not bad]
            self._quarantine(bad, exc)
            if not remaining:
                return self._folded(0, t0)
            failure = self._try_fold(remaining)
            if failure is None:
                return self._folded(len(remaining), t0)
            kind, exc = failure
            if kind == "tiles":
                self._failed("tiles", "missing tiles")
                return StepResult(BACKOFF, sleep_s=self.config.backoff_s, detail="missing tiles")
            if kind == "memory":
                return self._out_of_memory(remaining, exc)
            if kind == "fault":
                return self._engine_fault(exc)

    def _isolate(self, leaves: List[Any], first: BaseException,
                 bisect_memory: bool = False) -> Optional[StepResult]:
        """A LeafError that names no leaf of the batch: bisect (the engine is untouched by every
        LeafError, so the halves can be retried) until the leaf that is rejected is alone; it is
        quarantined. A fault met on the way is the engine's (not a leaf's): handled as such."""
        bad: List[Tuple[Any, BaseException]] = []
        fault: List[BaseException] = []

        def walk(chunk: List[Any]) -> Optional[str]:
            if fault:
                return None
            failure = self._try_fold(chunk)
            if failure is None:
                return None
            kind, exc = failure
            if kind == "tiles":
                return "tiles"
            if kind == "memory" and not bisect_memory:
                return "memory"
            if kind == "fault":
                fault.append(exc)
                return None
            if len(chunk) == 1:
                bad.append((chunk[0], exc))
                return None
            mid = len(chunk) // 2
            return walk(chunk[:mid]) or walk(chunk[mid:])

        t0 = self._mono()
        stop = walk(list(leaves))
        for leaf, exc in bad:
            self._quarantine(leaf, exc)
        if fault:
            return self._engine_fault(fault[0])
        if stop is not None:
            self._failed("fold" if stop == "memory" else "tiles", f"{stop}")
            return StepResult(BACKOFF, sleep_s=self.config.backoff_s, detail=f"fold: {stop}")
        if not bad:
            # the batch folded in pieces (the rejection was not tied to one leaf): carry on
            log.warning("scene worker %s: batch of %d folded in pieces after %s", self.map_id,
                        len(leaves), first)
        return self._folded(len(leaves) - len(bad), t0)

    # ---- a faulted engine

    def _engine_fault(self, exc: BaseException) -> StepResult:
        """fold_many raised after (or while) mutating: the engine's state is undefined (EngineFaulted
        for every call after it). Drop it, rewind to the last durable cursor and reload it from the
        store. `load_fail_limit` faults in a row without a good fold between: a rebuild, or, when a
        rebuild already ran for this fault or none is possible, NEEDS_REBUILD (never a loop)."""
        cfg = self.config
        self._fault_streak += 1
        self.stats.fold_failures = self._fault_streak
        cause = exc.cause if isinstance(exc, EngineFaulted) and exc.cause is not None else exc
        detail = f"{type(cause).__name__}: {cause}"
        log.error("scene worker %s: engine faulted (%d in a row): %s", self.map_id, self._fault_streak,
                  detail, exc_info=exc)
        self._event("fold_failed", stage="engine_faulted", detail=detail[:300], consecutive=self._fault_streak)
        self._drop_engine()
        if self._fault_streak >= max(1, cfg.load_fail_limit):
            return self._fault_give_up(detail)
        return self._reload_engine() or StepResult(BACKOFF, sleep_s=cfg.backoff_s,
                                                   detail=f"fold: engine faulted, reloaded: {detail}")

    def _drop_engine(self) -> None:
        """Forget the engine and everything folded into it that the store does not hold: the leaves are
        read again from the durable cursor (`_cursor` only moved past leaves that were saved)."""
        keep = self._cursor
        self.engine, self._loaded_version = None, None
        self._reset_fold_state()
        self._cursor = self._candidate = keep

    def _reload_engine(self) -> Optional[StepResult]:
        """A new engine from the store: the live version, or the saved pending generation of a rebuild.
        None when the engine is ready; a StepResult when it could not be (the next fold tries again)."""
        if self._rebuild_gen is not None:
            if not self._rebuild_saved:
                self.engine = self.new_engine()      # nothing of this generation was ever saved
                self._cursor = self._candidate = ZERO_CURSOR
                return None
            pend = self._store_pending() or {}
            engine = self._load_pending() if pend.get("generation") in (None, self._rebuild_gen) else None
            if engine is None:
                return self._restart_rebuild("engine faulted and the pending generation could not be reloaded")
            self.engine = engine
            self._cursor = self._candidate = self._cursor_of(pend.get("cursor")) or ZERO_CURSOR
            self._skip_held_by_pending()
            return None
        if self.engine_factory is None:
            return self._fault_give_up("no engine_factory to reload it")
        err = self._ensure_engine(self._expected_version)
        if err is None:
            return None
        if self._load_gave_up:
            self._load_gave_up, self._load_failures = False, 0
            return self._fault_give_up("the stored graph cannot be loaded")
        return err

    def _fault_give_up(self, detail: str) -> StepResult:
        if self._rebuild_gen is not None or self._fault_rebuilt or not self._can_rebuild():
            self.rebuild_reason = "engine_faulted"
            if not self._can_rebuild():
                self._warn_cannot_rebuild("engine fault")
            return self._terminal(NEEDS_REBUILD, f"engine_faulted: {detail}")
        err = self._begin_rebuild("engine_faulted")
        if err is not None:
            return err
        self._fault_rebuilt = True
        self._fault_streak = 0
        self._reset_fold_state()
        return StepResult(WORKED, detail=f"engine faulted: rebuilding ({detail})")

    def _fold_timed(self, leaves: int, seconds: float) -> None:
        st = self.stats
        st.last_fold_s = seconds
        st.max_fold_s = max(st.max_fold_s, seconds)
        budget = self.config.batch_budget_s
        if budget and seconds > budget:
            st.slow_folds += 1
            log.warning("scene worker %s: fold of %d leaves took %.1fs (budget %.1fs)",
                        self.map_id, leaves, seconds, budget)

    def _due(self, idle: bool, force: bool = False) -> bool:
        cfg, now = self.config, self._mono()
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
        res = self._lifecycle(force=True, where="map deleting before save")
        if res is not None:
            return res
        target = self._durable(self._candidate)
        kwargs: Dict[str, Any] = {}
        gen = self._rebuild_gen if self._rebuild_gen is not None else self._generation
        if gen is not None:
            kwargs["generation"] = gen
        try:
            version = self.store.save(self.map_id, self.engine, self._dirty,
                                      expected_version=self._expected_version, cursor=target, **kwargs)
        except (SaveFenced,) + self.fence_errors as exc:
            return self._terminal(FENCED, str(exc) or "save fenced")
        except ValueError as exc:
            # the store raises a plain ValueError for "generation is neither current nor begun" (and
            # for an empty engine): decide from the store's state, not from the message
            if self._generation_gone(gen):
                return self._terminal(FENCED, f"generation {gen} is no longer current or begun: {exc}")
            return self._save_failed(exc)
        except Exception as exc:
            return self._save_failed(exc)
        dirty, self._dirty = self._dirty, None
        moved = False        # the store keeps the version when only the cursor changed: nothing to announce then
        if self._rebuild_gen is None:
            moved = int(version) != self._expected_version
            self._expected_version = int(version)
            self.stats.version = int(version)
        # else: `version` is the pending generation's own counter. The compare-and-set of every
        # save is on the LIVE version, which a pending save does not change: keep expecting it
        # until commit_generation returns the new live one.
        self._last_save_ms = self._mono()
        self._stored_cursor = max(self._stored_cursor, target)
        self._oldest_unsaved_ms = None
        self.stats.saves += 1
        self.stats.unsaved_leaves = 0
        self.stats.save_failures = 0
        self._recovered("save")
        self._rebuild_saved = self._rebuild_saved or self._rebuild_gen is not None
        self._commit(target)
        if self._rebuild_gen is None:       # a pending generation is not readable yet: nothing to announce
            if moved:
                charts = len(getattr(dirty, "changed", None) or []) if dirty is not None else None
                self._event_updated(int(version), charts)
            self._leaves_since_publish = 0
        if self._rebuild_gen is None and moved and self.publish is not None and dirty is not None:
            try:
                self.publish(int(version), dirty)
            except Exception:
                log.exception("scene worker %s: publish callback failed", self.map_id)
        self._saves_since_gc += 1
        every = self.config.gc_every_saves
        if every > 0 and self._saves_since_gc >= every:
            self._gc()
        self._refresh_gauges()
        return None

    def _gc(self) -> None:
        """store.gc with its default keep, on the worker's own slot (no save of this map runs meanwhile, and the map's
        lock is held, so no other writer). Never for a version the store does not hold as live: the live version this
        worker expects and the pending generation's manifest are always kept (the store keeps `leased` and `pending`)."""
        self._saves_since_gc = 0
        gc = getattr(self.store, "gc", None)
        if gc is None or not self._started or self.state in TERMINAL:
            return
        try:
            meta = self.store.meta(self.map_id) or {}
            live = int(meta.get("version") or 0)
            if live <= 0 or live != self._expected_version:
                return                      # the store moved under us (fenced next step): collect nothing
            res = gc(self.map_id, leased=[live]) or {}
            self.stats.gc_runs += 1
            self.stats.gc_deleted += int(res.get("deleted") or 0)
        except Exception as exc:  # noqa: BLE001  housekeeping: never the step's problem
            log.warning("scene worker %s: store gc failed: %s", self.map_id, exc)

    def _event_updated(self, version: int, charts: Optional[int]) -> None:
        """'updated' events are at most one per `updated_event_interval_s` per map (the websocket
        publish is not throttled). A save inside the interval is not lost: it is held (latest version,
        charts and leaves summed) and emitted by `_flush_updated_event` once the interval is over."""
        held = self._event_held
        if held is not None and charts is not None:
            charts = (held[1] or 0) + charts
        self._event_held = (version, charts)
        self._flush_updated_event()

    def _flush_updated_event(self) -> None:
        held = self._event_held
        if held is None:
            return
        now, every = self._mono(), self.config.updated_event_interval_s
        last = self._last_updated_event_ms
        if every > 0 and last is not None and now - last < every * 1000:
            return
        self._event_held = None
        self._last_updated_event_ms = now
        skipped = {k: v for k, v in self.stats.skipped.items() if v}
        self._event("updated", version=held[0], leaves_folded=self._leaves_since_event,
                    charts_changed=held[1], skipped=skipped or None)
        self._leaves_since_event = 0

    def _generation_gone(self, gen: Optional[int]) -> bool:
        """After a ValueError from save: the generation written to is neither the store's current
        one nor (for a rebuild) the pending one. False when that cannot be established."""
        if gen is None:
            return False
        try:
            current = (self.store.meta(self.map_id) or {}).get("generation")
        except Exception:
            return False
        if current is None or int(gen) == int(current):
            return False                    # the current generation: the error is something else
        if int(gen) < int(current):
            return True                     # another writer committed a newer generation
        docs = getattr(self.store, "docs", None)    # `meta()` does not expose the pending one
        try:
            pending = (docs.meta(self.map_id) or {}).get("pending") if docs is not None else None
        except Exception:
            return False
        if docs is None:
            return False
        return not pending or int(pending.get("generation", -1)) != int(gen)

    def _save_failed(self, exc: Exception) -> StepResult:
        self.stats.save_failures += 1
        self._last_save_ms = self._mono()
        log.warning("scene worker %s: save failed: %s", self.map_id, exc)
        self._failed("save", f"{type(exc).__name__}: {exc}")
        return StepResult(BACKOFF, sleep_s=self.config.backoff_s, detail=f"save: {exc}")

    def _commit_rebuild(self) -> Optional[StepResult]:
        """Caught up in the pending generation: switch readers to it. None on success / nothing to switch yet."""
        if not self._rebuild_saved:
            return self._finish_empty_rebuild()
        self._empty_confirms = 0
        res = self._lifecycle(force=True, where="map deleting before generation commit")
        if res is not None:
            return res
        gen = self._rebuild_gen
        try:
            version, retired = self.store.commit_generation(self.map_id, gen)
        except (SaveFenced,) + self.fence_errors as exc:
            return self._terminal(FENCED, str(exc) or "commit fenced")
        except ValueError as exc:
            # "no such pending generation with a saved state": decide from the store's state
            verdict = self._pending_state(gen)
            if verdict == "superseded":
                return self._terminal(FENCED, f"generation {gen} was committed or superseded: {exc}")
            if verdict == "gone":
                return self._restart_rebuild(f"pending generation {gen} vanished: {exc}")
            return self._commit_failed(exc)
        except Exception as exc:
            return self._commit_failed(exc)
        self._commit_failures = 0
        self._fault_rebuilt = False
        self._recovered("save")
        self._generation, self._rebuild_gen, self._rebuild_saved = gen, None, False
        self._plan_clear()
        self._rebuild_known = False
        self._expected_version = int(version)
        self.stats.version = int(version)
        self.stats.rebuilds += 1
        self._event("rebuild_completed", reason=self._rebuild_event_reason, version=int(version),
                    duration_s=round(max(0, self._mono() - self._rebuild_started_ms) / 1000.0, 3))
        self._leaves_since_publish = 0
        request, self._rebuild_request = self._rebuild_request, None
        if request is not None:
            self._clear_request(request)
        self._last_updated_event_ms = self._mono()
        self._leaves_since_event = 0
        self._event("updated", version=int(version), leaves_folded=0, charts_changed=None)
        if self.publish is not None:
            try:
                self.publish(int(version), RebuiltDirty(bbox=None, changed=[], retired=dict(retired or {}),
                                                        links=[], watermark=self._watermark))
            except Exception:
                log.exception("scene worker %s: publish callback failed", self.map_id)
        return None

    def _pending_state(self, gen: Optional[int]) -> str:
        """After a ValueError from commit_generation: "superseded" (the live generation is already
        `gen` or newer: another writer got there), "gone" (the pending generation is no longer
        `gen`, or holds no saved state: begin again) or "unknown" (the store cannot say)."""
        try:
            meta = self.store.meta(self.map_id)
            docs = getattr(self.store, "docs", None)
            raw = docs.meta(self.map_id) if docs is not None else None
        except Exception:
            return "unknown"
        if not meta:
            return "gone"                   # the store was dropped under us
        if gen is not None and int(meta.get("generation") or 0) >= int(gen):
            return "superseded"
        if docs is None:
            return "unknown"
        pend = (raw or {}).get("pending")
        if not pend or int(pend.get("generation", -1)) != int(gen) or not pend.get("manifest"):
            return "gone"
        return "unknown"

    def _commit_failed(self, exc: Exception) -> StepResult:
        """A commit failure that is not a verdict: backoff, bounded, then a terminal FENCED."""
        self._commit_failures += 1
        log.warning("scene worker %s: generation commit failed (%d/%d): %s", self.map_id,
                    self._commit_failures, self.config.commit_retries, exc)
        self._failed("save", f"commit: {type(exc).__name__}: {exc}")
        if self._commit_failures >= max(1, self.config.commit_retries):
            return self._terminal(FENCED, f"generation commit failed {self._commit_failures} times: "
                                          f"{type(exc).__name__}: {exc}")
        return StepResult(BACKOFF, sleep_s=self.config.backoff_s, detail=f"commit: {exc}")

    def _restart_rebuild(self, why: str) -> StepResult:
        """The pending generation is gone: fold everything again into a new one (or, when the store
        was dropped, into a fresh first generation)."""
        log.warning("scene worker %s: %s: restarting the rebuild", self.map_id, why)
        self._commit_failures = 0
        reason = self._rebuild_event_reason
        request = self._rebuild_request
        try:
            has_state = bool(self.store.meta(self.map_id))
        except Exception as exc:
            return StepResult(BACKOFF, sleep_s=self.config.backoff_s, detail=f"meta: {exc}")
        if not has_state:
            self._rebuild_gen, self._rebuild_saved = None, False
            self._generation, self._expected_version = None, 0
            self.engine = self.new_engine()
            self._reset_fold_state()
            self.stats.version = 0
            return StepResult(WORKED, detail=why)
        err = self._begin_rebuild(reason, request)
        if err is not None:
            return err
        self._reset_fold_state()
        return StepResult(WORKED, detail=why)

    def _finish_empty_rebuild(self) -> Optional[StepResult]:
        """A rebuild that is caught up with no leaf to save: the map has nothing to fold (all its
        nodes were deleted, or none has a usable costmap). The store cannot commit a generation
        without state, so the stale graph is dropped (`store.drop`: a rebuildable index) and the
        request answered; without `drop` the request is only cleared."""
        self._empty_confirms += 1
        if self._empty_confirms < max(1, self.config.empty_rebuild_confirmations):
            return None
        res = self._lifecycle(force=True, where="map deleting before empty rebuild")
        if res is not None:
            return res
        drop = getattr(self.store, "drop", None)
        if drop is not None:
            try:
                drop(self.map_id)
            except Exception as exc:
                self._empty_confirms = 0
                return self._commit_failed(exc)
            self._generation, self._expected_version = None, 0
            self.stats.version = 0
        else:
            log.warning("scene worker %s: empty rebuild and the store cannot drop: request cleared, "
                        "the previous graph stays", self.map_id)
        self._commit_failures = self._empty_confirms = 0
        self._fault_rebuilt = False
        self._rebuild_gen = None
        self._plan_clear()
        self.stats.rebuilds += 1
        self._event("rebuild_completed", reason=self._rebuild_event_reason, version=0,
                    duration_s=round(max(0, self._mono() - self._rebuild_started_ms) / 1000.0, 3))
        self._leaves_since_publish = 0
        request, self._rebuild_request = self._rebuild_request, None
        if request is not None:
            self._clear_request(request)
        if drop is not None and self.publish is not None:
            try:
                self.publish(0, RebuiltDirty(bbox=None, changed=[], retired={}, links=[],
                                             watermark=self._watermark))
            except Exception:
                log.exception("scene worker %s: publish callback failed", self.map_id)
        return None

    def _durable(self, cursor: Cursor) -> Cursor:
        """The cursor that may be persisted: not past the oldest node whose PNG is still missing
        (a restart must read it again)."""
        floors = [p.ms for p in self._pending.values()]
        floors += list(self._buffered.values())      # adapted leaves (and skips) not folded yet
        plan_floor = self._plan_floor_ms()           # a rebuild's records not fed yet (folded out of stored order)
        if plan_floor is not None:
            floors.append(plan_floor)
        if floors:
            return min(cursor, (min(floors), ""))
        return cursor

    def _commit(self, cursor: Optional[Cursor] = None) -> None:
        cursor = self._durable(cursor or self._candidate)
        if cursor <= self._cursor:
            return
        self._cursor = cursor
        try:
            self.cursor_store.put(self.map_id, cursor)
        except Exception:
            log.exception("scene worker %s: cursor persist failed", self.map_id)

    def _refresh_gauges(self) -> None:
        self.stats.pending = len(self._pending)
        self.stats.buffered_leaves = self._leaf_count()
        oldest = self._oldest_unsaved_ms
        self.stats.version_lag_s = 0.0 if oldest is None else max(0.0, (self._mono() - oldest) / 1000.0)
