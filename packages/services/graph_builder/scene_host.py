"""Hosts the scene-graph `SceneRunner` inside the graph-builder process (integration.md, decision 3).

`main.py` calls `start_scene_worker` only when `SCENE_GRAPH_WORKER_ENABLED` is true. Everything
that can fail (importing `packages.scene_graph`, building the store, opening the lock connection)
happens here, behind one `try`: a failure is logged with its reason and the service keeps running
without the worker. The runner runs in its own thread (the worker is synchronous); a crash inside
it is logged and the loop restarts it after a pause, so it never takes the process down.
`stop()` sets the stop flag and joins the thread; the thread itself closes the runner (every
worker's advisory lock) and the guard's connections when its loop ends, so they are never closed
from the caller's thread while a step may still be using them. Only when the thread is not alive
(never started, or already ended) does `stop()` close them itself.
"""
import logging
import threading
import time
from typing import Any, Callable, Optional

log = logging.getLogger("GraphBuilderService.scene_host")

RESTART_PAUSE_S = 10.0
JOIN_TIMEOUT_S = 30.0


def default_conninfo() -> str:
    from packages.config import (POSTGRES_DATABASE_HOST, POSTGRES_DATABASE_NAME, POSTGRES_DATABASE_PORT,
                                 POSTGRES_DATABASE_USERNAME, postgres_database_password)
    return (f"dbname={POSTGRES_DATABASE_NAME} user={POSTGRES_DATABASE_USERNAME} "
            f"host={POSTGRES_DATABASE_HOST} password={postgres_database_password()} "
            f"port={POSTGRES_DATABASE_PORT}")


class SceneWorkerHost:
    def __init__(self, runner: Any, guard: Any = None,
                 restart_pause_s: float = RESTART_PAUSE_S) -> None:
        self.runner, self.guard = runner, guard
        self.restart_pause_s = restart_pause_s
        self._stop = threading.Event()
        self._closed = threading.Lock()         # taken once: the final close runs once
        self._thread: Optional[threading.Thread] = None

    def state(self) -> dict:
        """For /health: whether the thread runs plus the runner's per-map view."""
        out = {"running": self.running}
        snap = getattr(self.runner, "snapshot", None)
        if snap is not None:
            try:
                out.update(snap())
            except Exception as exc:  # noqa: BLE001
                out["error"] = f"{type(exc).__name__}: {exc}"
        return out

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="scene-graph-worker", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    self.runner.run(self._stop.wait, self._stop.is_set)
                except Exception:  # noqa: BLE001
                    log.exception("scene graph runner crashed; restarting in %.0fs", self.restart_pause_s)
                    self._close_runner()
                    self._stop.wait(self.restart_pause_s)
        finally:
            self._close_all()

    def _close_runner(self) -> None:
        try:
            self.runner.close()
        except Exception:  # noqa: BLE001
            log.exception("scene graph runner: close failed")

    def _close_all(self) -> None:
        if not self._closed.acquire(blocking=False):
            return
        self._close_runner()
        try:
            if self.guard is not None:
                self.guard.close()
        except Exception:  # noqa: BLE001
            log.exception("scene graph host: guard close failed")

    def stop(self, timeout: float = JOIN_TIMEOUT_S) -> None:
        """Stop the runner thread; it releases the locks as it ends. Blocking: call it off the
        event loop. A thread that outlives `timeout` (a step in a long fold) keeps running to its
        next stop check and closes everything itself; nothing is closed under its feet here."""
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
            if thread.is_alive():
                log.error("scene graph runner did not stop within %.0fs; it closes the locks when "
                          "its current step ends", timeout)
                return
        self._close_all()       # no thread (never started / ended): idempotent
        self._thread = None


def params_dict(params: Any) -> dict:
    """The params as the store persists them (`EngineState.params`); the hash is taken over this."""
    return dict(robot_half_side=params.robot_half_side, lattice=params.lattice, chart_radius=params.chart_radius)


def expected_params_hash(params: Any) -> str:
    """The `params_hash` the store writes for a graph built with `params`."""
    from packages.scene_graph.store import SceneGraphStore
    return SceneGraphStore.params_hash(params)


def stored_leaf_index(store: Any, map_id: str, version: int) -> dict:
    """`{key: (stamp_ms, session, x, y, r_d, view)}` of the leaves stored with `version`: the leaf
    segments of its manifest, no tiles and no engine. The worker skips the leaves a restart would
    fold again. Raises when the store cannot say (the worker then folds the margin again)."""
    from packages.scene_graph.store import segment_records
    from packages.scene_graph.store_codec import unpack
    man = store.manifest(map_id, version)
    out: dict = {}
    for h in man.get("segments", []):
        raw = store.blobs.get(map_id, "g%d/l/%s" % (man["generation"], h))
        if raw is None:
            raise FileNotFoundError(f"leaf segment {h}")
        arr, meta = unpack(raw)
        for r in segment_records(arr, meta):
            out[r.key] = (int(r.t), str(r.session), float(r.x), float(r.y), float(r.r_d),
                          None if r.view is None else tuple(float(v) for v in r.view))
    return out


def worker_config(params: Any) -> Any:
    """WorkerConfig from the SCENE_GRAPH_* settings of packages.config."""
    from packages import config
    from . import scene_worker
    lo = max(1, config.SCENE_GRAPH_BATCH_MIN_LEAVES)
    hi = max(1, config.SCENE_GRAPH_BATCH_MAX_LEAVES)
    if lo > hi:
        log.warning("SCENE_GRAPH_BATCH_MIN_LEAVES (%d) > SCENE_GRAPH_BATCH_MAX_LEAVES (%d): "
                    "using %d for both", lo, hi, hi)
        lo = hi
    return scene_worker.WorkerConfig(
        batch_min_leaves=lo, batch_max_leaves=hi,
        save_every_leaves=config.SCENE_GRAPH_SAVE_EVERY_LEAVES, save_every_s=config.SCENE_GRAPH_SAVE_EVERY_S,
        replay_margin_ms=int(config.SCENE_GRAPH_REPLAY_MARGIN_S * 1000),
        receipt_window_s=float(config.SCENE_GRAPH_RECEIPT_WINDOW_S),
        skip_folded=bool(config.SCENE_GRAPH_SKIP_FOLDED),
        replay_recheck_s=config.SCENE_GRAPH_REPLAY_RECHECK_S,
        rebuild_max_age_s=config.SCENE_GRAPH_REBUILD_MAX_AGE_S,
        rebuild_debounce_s=config.SCENE_GRAPH_REBUILD_DEBOUNCE_S,
        rebuild_batch=config.SCENE_GRAPH_REBUILD_BATCH,
        load_fail_limit=config.SCENE_GRAPH_LOAD_FAIL_LIMIT,
        step_budget_s=config.SCENE_GRAPH_STEP_BUDGET_S,
        stats_log_s=config.SCENE_GRAPH_STATS_LOG_S,
        min_save_interval_s=config.SCENE_GRAPH_PUBLISH_INTERVAL_S,
        batch_budget_s=config.SCENE_GRAPH_BATCH_BUDGET_S,
        idle_sleep_s=config.SCENE_GRAPH_IDLE_S, idle_max_s=config.SCENE_GRAPH_IDLE_MAX_S,
        lifecycle_check_s=config.SCENE_GRAPH_LIFECYCLE_CHECK_S,
        lazy_load=config.SCENE_GRAPH_LAZY_LOAD,
        memory_retry_limit=max(1, config.SCENE_GRAPH_MEMORY_RETRY_LIMIT),
        expected_params_hash=expected_params_hash(params))


def runner_config(config: Any) -> Any:
    """RunnerConfig from the SCENE_GRAPH_* settings: map cap, the step pool, the memory budget."""
    from . import scene_runner
    return scene_runner.RunnerConfig(
        max_maps=config.SCENE_GRAPH_MAX_MAPS, slow_step_s=config.SCENE_GRAPH_SLOW_STEP_S,
        threads=max(1, int(config.SCENE_GRAPH_WORKER_THREADS)),
        max_resident=max(0, int(config.SCENE_GRAPH_MAX_RESIDENT_ENGINES)),
        max_resident_bytes=max(0, int(config.SCENE_GRAPH_MAX_RESIDENT_BYTES)),
        evict_idle_s=float(config.SCENE_GRAPH_EVICT_IDLE_S))


def build_host(service: Any, loop: Any, conninfo: Optional[str] = None) -> SceneWorkerHost:
    """Wire the real store, engine, guard and sources. May raise (ImportError, connection errors)."""
    from packages.scene_graph.api import SceneParams
    from packages.scene_graph.engine import ChartGraph
    from packages.scene_graph.store import SceneGraphStore, StaleVersion
    from . import scene_events, scene_guard, scene_runner, scene_sources, scene_worker

    params = SceneParams()
    store = SceneGraphStore.from_client(service.topomap_db)
    guard = scene_guard.PgGuard(scene_guard.connect_from_conninfo(conninfo or default_conninfo()))
    fetch_png = scene_sources.png_fetcher(service.image_db)
    footprint = scene_sources.PgFootprint(scene_guard.connect_from_conninfo(conninfo or default_conninfo()))
    cursors = scene_worker.MemoryCursorStore()
    cfg = worker_config(params)
    from packages import config as _config
    # one bounded PNG fetch + decode pool for every map (separate from the step pool)
    prefetch = (scene_worker.PngPrefetch(_config.SCENE_GRAPH_PREFETCH_THREADS,
                                         _config.SCENE_GRAPH_PREFETCH_QUEUE)
                if _config.SCENE_GRAPH_PREFETCH_THREADS > 0 else None)
    database = getattr(service, "database", None)
    sink = scene_events.make_event_sink(database, loop) if database is not None and loop is not None else None

    def make_worker(map_id: str) -> Any:
        # No engine here: the worker builds it through `engine_factory` once it HOLDS the map's
        # writer lock (a replica that does not own the map loads nothing), pinned to the stored
        # version it read under that lock. A params change never reaches the factory: the worker
        # starts a new generation with `new_engine` instead.
        def load(version: int) -> Any:
            return store.load(map_id, version) if version else ChartGraph(params)

        return scene_worker.SceneWorker(
            map_id, source=service.graph_db, fetch_png=fetch_png, footprint=footprint,
            engine_factory=load, store=store, cursor_store=cursors, guard=guard, config=cfg,
            now_ms=lambda: int(time.time() * 1000),
            mono_ms=lambda: int(time.monotonic() * 1000),
            stored_leaves=lambda version: stored_leaf_index(store, map_id, version),
            publish=scene_events.make_publish(service.update_publisher, loop, map_id),
            new_engine=lambda: ChartGraph(params), fence_errors=(StaleVersion,), prefetch=prefetch,
            on_event=(lambda kind, payload: sink(map_id, kind, payload)) if sink else None)

    switched_off: set = set()

    def list_maps() -> list:
        """MinIO buckets that are live rows of the maps registry: a bucket alone is not a map.
        The per-map switch is read in the same discovery (one query), for `skip_reason`."""
        # strict listing raises on a MinIO outage: the runner keeps its maps (unknown is not empty)
        lister = getattr(service.image_db, "list_maps_strict", None) or service.image_db.list_maps
        maps = guard.registered_maps(lister())
        try:
            off = guard.disabled_maps(maps)
            switched_off.clear()
            switched_off.update(off)
        except Exception:  # noqa: BLE001  an unanswered question keeps the previous answer
            log.exception("scene graph: per-map switch lookup failed; keeping the previous answer")
        return maps

    def skip_reason(map_id: str) -> Optional[str]:
        return "map_not_enabled" if map_id in switched_off else None

    runner = scene_runner.SceneRunner(
        list_maps, make_worker, on_event=sink, skip_reason=skip_reason, prefetch=prefetch,
        config=runner_config(_config))
    return SceneWorkerHost(runner, guard)


def start_scene_worker(service: Any, loop: Any, conninfo: Optional[str] = None,
                       build: Callable[..., SceneWorkerHost] = build_host) -> Optional[SceneWorkerHost]:
    """The started host, or None when it could not be built (logged; never raises)."""
    try:
        host = build(service, loop, conninfo)
        host.start()
    except Exception as exc:  # noqa: BLE001
        log.error("scene graph worker not started: %s: %s", type(exc).__name__, exc, exc_info=True)
        return None
    log.info("scene graph worker started")
    return host
