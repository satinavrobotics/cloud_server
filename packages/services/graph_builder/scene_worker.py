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

Terminal states are logged at warning; `SceneRunner` reports them once per transition as a
`fold_failed` event (`stage` = the state).

Interface assumed of the store: `store.save(map_id, sg, dirty, *, expected_version, cursor[,
generation])` raising `SaveFenced` / the configured `fence_errors` on mismatch; `store.meta()`
carrying `version`, `generation`, `params_hash` and optionally `cursor`.
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


@dataclass
class StepResult:
    state: str
    folded: int = 0
    skipped: int = 0
    version: Optional[int] = None           # set when this step saved
    sleep_s: float = 0.0
    detail: str = ""


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
                 loaded_version: Optional[int] = None) -> None:
        if engine is None and engine_factory is None:
            raise ValueError("SceneWorker needs an engine or an engine_factory")
        self.map_id, self.source, self.fetch_png, self.footprint = map_id, source, fetch_png, footprint
        self.engine, self.store, self.cursor_store, self.guard = engine, store, cursor_store, guard
        self.engine_factory, self._loaded_version = engine_factory, loaded_version
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
        # a failed save left folded state in memory: retry it before pulling more
        if self._dirty is not None and self.stats.save_failures and self._due(idle=False, force=True):
            res = self._save()
            if res is not None:
                return res
        # 2. pull (not while a full batch is already waiting: fold that first)
        more = False
        skipped, fetch_error = self._retry_pending()
        if fetch_error is None and self._leaf_count() < cfg.batch_max_leaves:
            since = self._scan if self._scan is not None else (
                max(0, self._candidate[0] - cfg.replay_margin_ms), "")
            try:
                nodes = list(self.source.costmap_nodes_since(self.map_id, since[0], since[1], cfg.batch_size))
            except Exception as exc:
                self.stats.source_errors += 1
                log.warning("scene worker %s: source failed: %s", self.map_id, exc)
                return StepResult(BACKOFF, sleep_s=cfg.backoff_s, detail=f"source: {exc}")
            more = len(nodes) >= cfg.batch_size
            self._scan = self._order(nodes[-1]) if (more and nodes) else None
            # 3. adapt into the buffer
            for node in nodes:
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
        if self._leaf_count() and self._buf_since_ms is None:
            self._buf_since_ms = self.now_ms()
        self._release_leading_skips()
        # 4. fold once per batch
        folded = 0
        batch = self._take_batch()
        if batch:
            err = self._keep_stamps()
            if err is None:
                err = self._fold([e.leaf for e in batch])
            if err is not None:
                self._scan = None      # re-read from the margin once the cause is gone
                return err
            folded = len(batch)
            self._pop(batch)
            self.stats.batches += 1
            self.stats.folded += folded
            self.stats.unsaved_leaves += folded
            self._leaves_since_publish += folded
            if self._oldest_unsaved_ms is None:
                self._oldest_unsaved_ms = self.now_ms()
            self._release_leading_skips()
        # 5-7. save (rate limited), then advance the cursor
        idle = not more and not folded and not skipped      # a re-read of the margin alone is idle
        waiting = self._leaf_count() > 0
        saved: Optional[int] = None
        if self._dirty is not None and self._due(idle=idle):
            res = self._save()
            if res is not None:
                res.folded, res.skipped = folded, skipped
                return res
            saved = self.stats.version
        elif self._dirty is None:
            self._commit()      # nothing unsaved: terminal skips alone move the cursor
        if (self._rebuild_gen is not None and not more and not folded and not waiting
                and self._dirty is None and fetch_error is None and not self._pending):
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
            cap = max(cfg.idle_max_s, cfg.idle_sleep_s)
            sleep = min(cap, cfg.idle_sleep_s * (2 ** min(self._idle_streak, 16)))
            self._idle_streak += 1
            if self._pending:                # a PNG is due again soon: do not oversleep it
                sleep = min(sleep, max(cfg.png_retry_s, 0.05))
            if waiting and self._buf_since_ms is not None:     # a trickle: wake when its wait is up
                left = self._batch_wait_s() - (self.now_ms() - self._buf_since_ms) / 1000.0
                sleep = min(sleep, max(left, 0.05))
        return StepResult(state, folded=folded, skipped=skipped, version=saved, sleep_s=sleep)

    # ------------------------------------------------------------------ internals

    def _lifecycle(self, force: bool, where: str) -> Optional[StepResult]:
        """The map's lifecycle and (once started) the writer lock. None = fine; DELETING / FENCED
        are terminal; a lifecycle query that FAILS is a BACKOFF, never a verdict. Rate limited to
        `lifecycle_check_s` unless `force` (saves, commits, start)."""
        now = self.now_ms()
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
        rebuilding = False
        if cfg.expected_params_hash is not None and meta.get("params_hash") not in (None, cfg.expected_params_hash):
            self.rebuild_reason = "params_hash"
            if not self._can_rebuild():
                return self._terminal(NEEDS_REBUILD, "params_hash")
            err = self._begin_rebuild("needs_rebuild")
            if err is not None:
                self.guard.release(self.map_id)
                return err
            rebuilding = True
            log.info("scene worker %s: params changed (%s -> %s): rebuilding in generation %d",
                     self.map_id, meta.get("params_hash"), cfg.expected_params_hash, self._rebuild_gen)
        if cfg.expected_generation is not None and meta.get("generation") not in (None, cfg.expected_generation):
            self.rebuild_reason = "generation"
            return self._terminal(NEEDS_REBUILD, "generation")
        request = self._read_rebuild_request(force=True)
        if request is not None:
            if rebuilding:
                self._rebuild_request = request     # this rebuild covers it; cleared at commit
            elif self._can_rebuild() and version > 0:
                err = self._begin_rebuild(str(request.get("reason") or "nodes_deleted"), request)
                if err is not None:
                    self.guard.release(self.map_id)
                    return err
                rebuilding = True
            elif version == 0:
                self._clear_request(request)        # nothing was ever saved: the first build is fresh
        if not rebuilding:
            err = self._ensure_engine(version)
            if err is not None:
                self.guard.release(self.map_id)
                return err
        stored = [c for c in (self._cursor_of(self.cursor_store.get(self.map_id)),
                              self._cursor_of(meta.get("cursor"))) if c is not None]
        self._cursor = ZERO_CURSOR if rebuilding else (min(stored) if stored else ZERO_CURSOR)
        self._candidate = self._cursor
        self._expected_version = version
        self.stats.version = version
        self._started = True
        self._lifecycle_ok_ms = self.now_ms()
        return None

    def _ensure_engine(self, version: int) -> Optional[StepResult]:
        """The engine for the stored `version`, built now that the writer lock is held. A engine
        handed in is kept when it was loaded at this version (or says nothing about it); one that
        raced the lock (loaded at another version) is reloaded through the factory."""
        if self.engine is not None and self._loaded_version in (None, version):
            return None
        if self.engine_factory is None:
            log.warning("scene worker %s: engine loaded at version %s, store is at %s and there is no "
                        "engine_factory to reload it", self.map_id, self._loaded_version, version)
            return StepResult(BACKOFF, sleep_s=self.config.backoff_s,
                              detail=f"engine at {self._loaded_version}, store at {version}")
        try:
            self.engine = self.engine_factory(version)
        except Exception as exc:
            log.exception("scene worker %s: loading the engine (version %s) failed", self.map_id, version)
            self._failed("load", f"{type(exc).__name__}: {exc}")
            return StepResult(BACKOFF, sleep_s=self.config.backoff_s, detail=f"load: {exc}")
        self._loaded_version = version
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
        now = self.now_ms()
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
        """Mid-run: act on a request. True when a rebuild generation was begun (state was reset)."""
        if self._rebuild_gen is not None:
            return False                # a rebuild is running; its commit clears what it answers
        if self._clear_failed_at is not None:
            self._clear_request({"at": self._clear_failed_at})
        request = self._read_rebuild_request()
        if request is None or not self._can_rebuild():
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

    def _begin_rebuild(self, reason: str, request: Optional[Mapping[str, Any]] = None) -> Optional[StepResult]:
        """Begin a pending generation, switch to a fresh engine and announce it. None on success,
        a BACKOFF result when begin_generation failed (the caller decides about the lock)."""
        try:
            gen = int(self.store.begin_generation(self.map_id))
        except Exception as exc:
            log.warning("scene worker %s: begin_generation failed: %s", self.map_id, exc)
            return StepResult(BACKOFF, sleep_s=self.config.backoff_s, detail=f"begin_generation: {exc}")
        # fold every leaf again, under the configured params, into the pending generation
        self.engine = self.new_engine()
        self._rebuild_gen, self._rebuild_saved = gen, False
        self._rebuild_started_ms = self.now_ms()
        self._empty_confirms = self._commit_failures = 0
        self._rebuild_event_reason = reason
        self._rebuild_request = request
        self._event("rebuild_started", reason=reason)
        if request is not None:
            log.info("scene worker %s: rebuild requested (%s, %s nodes): generation %d",
                     self.map_id, reason, request.get("nodes"), gen)
        return None

    def _reset_fold_state(self) -> None:
        """Forget everything folded or buffered: the engine is new and every leaf is read again."""
        self._buf.clear()
        self._buffered.clear()
        self._seen.clear()
        self._pending.clear()
        self._idle_streak = 0
        self._scan = None
        self._dirty = None
        self._oldest_unsaved_ms = None
        self._leaves_since_publish = 0
        self._cursor = self._candidate = ZERO_CURSOR
        self._buf_since_ms = None
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

    def _adapt(self, node: Mapping[str, Any], retry: bool = False):
        """Leaf, a terminal skip reason (str), "duplicate" or "pending". RAISES `_FetchError` when
        the PNG fetch itself failed (its state is unknown: nothing may be decided about the leaf)."""
        key, ms = str(node.get("_key", "")), int(node.get("costmap_stored_ms") or 0)
        if self._buffered.get(key) == ms:       # a margin re-read of an entry that waits for its batch
            return "duplicate"
        if key in self._pending and not retry:  # asked again by _retry_pending, on its own clock
            return "duplicate"
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
            raise _FetchError(f"{key}: {exc}") from exc
        if png is None:                          # confirmed missing: not there (yet)
            now = self.now_ms()
            held = self._pending.get(key)
            if held is None:
                self._pending[key] = _Pending(node, ms, now, now)
            else:
                held.tried_ms = now
            return "pending"
        self._pending.pop(key, None)
        r_d, known = self._robot_half_side(node)
        info: Dict[str, Any] = {}
        leaf, reason = scene_adapter.to_leaf(node, png, r_d=r_d, layer=layer, clock=self.clock,
                                             stamp_info=info)
        if leaf is None:
            return reason or "unknown"
        # Persist the stamp only when it is final. A stored stamp wins forever (first write wins on
        # the node), and the provisional one is the receipt time given to a session whose clock is
        # not trusted yet (< min_obs observations): keeping it would freeze the leaf at "receipt"
        # while the later leaves of the session are corrected. It is never newer than the receipt,
        # so not keeping it only lets a replay pick the better estimate.
        if info.get("source") not in (scene_leaf.STAMP_STORED, scene_leaf.STAMP_PROVISIONAL):
            self._new_stamps[key] = int(leaf.stamp_ms)
        if not known:
            self.stats.default_r_d += 1
        return leaf

    def _accept(self, node: Mapping[str, Any], outcome: Any) -> int:
        """Buffer an adapt outcome. Returns 1 when it was a NEW terminal skip (progress), else 0."""
        if outcome in ("duplicate", "pending"):
            return 0
        key, ms = str(node.get("_key", "")), int(node.get("costmap_stored_ms") or 0)
        self._buffered[key] = ms      # leaves and skips alike: a margin re-read must not queue it again
        if isinstance(outcome, str):
            self.stats.skipped[outcome] += 1
            self._buf.append(_Entry(node, None))
            return 1
        self._buf.append(_Entry(node, outcome))
        return 0

    def _retry_pending(self) -> Tuple[int, Optional[_FetchError]]:
        """Ask again for PNGs that were missing; give up on those older than `png_expire_s` of
        WALL clock (stored time is no clock: a backlog moves through it in seconds). Returns
        (new terminal skips, the fetch error that stopped the retries or None)."""
        cfg, skipped, error = self.config, 0, None
        if self._pending:
            now = self.now_ms()
            expire_ms = (cfg.png_expire_s if cfg.png_expire_s is not None
                         else cfg.replay_margin_ms / 1000.0) * 1000.0
            for key, held in list(self._pending.items()):
                if now - held.first_ms > expire_ms:
                    del self._pending[key]
                    self.stats.skipped[SKIP_PNG_EXPIRED] += 1
                    log.warning("scene worker %s: node %s: PNG still missing after %.0fs, giving up",
                                self.map_id, key, (now - held.first_ms) / 1000.0)
                    self._handled(held.node)
                    continue
                if now - held.tried_ms < cfg.png_retry_s * 1000:
                    continue
                try:
                    outcome = self._adapt(held.node, retry=True)
                except _FetchError as exc:
                    error = exc
                    break
                skipped += self._accept(held.node, outcome)
        self.stats.pending = len(self._pending)
        floor = self._candidate[0] - cfg.replay_margin_ms
        for key in [k for k, ms in self._seen.items() if ms < floor]:
            del self._seen[key]
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
        waited_s = 0.0 if self._buf_since_ms is None else (self.now_ms() - self._buf_since_ms) / 1000.0
        if n < max(1, cfg.batch_min_leaves) and waited_s < self._batch_wait_s():
            return []
        take, out = max(1, cfg.batch_max_leaves), []
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
        self._buf_since_ms = self.now_ms() if self._leaf_count() else None

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
        t0 = self.now_ms()
        for attempt in (0, 1):
            try:
                dirty = self.engine.fold_many(leaves)
                self._dirty = merge_dirty(self._dirty, dirty)
                self._watermark = tuple(getattr(dirty, "watermark", self._watermark))
                self._fold_timed(len(leaves), (self.now_ms() - t0) / 1000.0)
                self._recovered("fold", "tiles")
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
                self._failed("fold", f"{type(exc).__name__}: {exc}")
                return StepResult(BACKOFF, sleep_s=cfg.backoff_s, detail=f"fold: {exc}")
        self._failed("tiles", "missing tiles")
        return StepResult(BACKOFF, sleep_s=cfg.backoff_s, detail="missing tiles")

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
        if self._rebuild_gen is None:
            self._expected_version = int(version)
            self.stats.version = int(version)
        # else: `version` is the pending generation's own counter. The compare-and-set of every
        # save is on the LIVE version, which a pending save does not change: keep expecting it
        # until commit_generation returns the new live one.
        self._last_save_ms = self.now_ms()
        self._oldest_unsaved_ms = None
        self.stats.saves += 1
        self.stats.unsaved_leaves = 0
        self.stats.save_failures = 0
        self._recovered("save")
        self._rebuild_saved = self._rebuild_saved or self._rebuild_gen is not None
        self._commit(target)
        if self._rebuild_gen is None:       # a pending generation is not readable yet: nothing to announce
            charts = len(getattr(dirty, "changed", None) or []) if dirty is not None else None
            self._event("updated", version=int(version), leaves_folded=self._leaves_since_publish,
                        charts_changed=charts)
            self._leaves_since_publish = 0
        if self._rebuild_gen is None and self.publish is not None and dirty is not None:
            try:
                self.publish(int(version), dirty)
            except Exception:
                log.exception("scene worker %s: publish callback failed", self.map_id)
        self._refresh_gauges()
        return None

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
        self._last_save_ms = self.now_ms()
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
        except self.fence_errors as exc:
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
        self._recovered("save")
        self._generation, self._rebuild_gen, self._rebuild_saved = gen, None, False
        self._expected_version = int(version)
        self.stats.version = int(version)
        self.stats.rebuilds += 1
        self._event("rebuild_completed", reason=self._rebuild_event_reason, version=int(version),
                    duration_s=round(max(0, self.now_ms() - self._rebuild_started_ms) / 1000.0, 3))
        self._leaves_since_publish = 0
        request, self._rebuild_request = self._rebuild_request, None
        if request is not None:
            self._clear_request(request)
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
        self._rebuild_gen = None
        self.stats.rebuilds += 1
        self._event("rebuild_completed", reason=self._rebuild_event_reason, version=0,
                    duration_s=round(max(0, self.now_ms() - self._rebuild_started_ms) / 1000.0, 3))
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
        if self._pending:
            return min(cursor, (min(p.ms for p in self._pending.values()), ""))
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
        self.stats.version_lag_s = 0.0 if oldest is None else max(0.0, (self.now_ms() - oldest) / 1000.0)
