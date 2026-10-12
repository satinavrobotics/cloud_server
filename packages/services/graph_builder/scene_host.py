"""Hosts the scene-graph `SceneRunner` inside the graph-builder process (integration.md, decision 3).

`main.py` calls `start_scene_worker` only when `SCENE_GRAPH_WORKER_ENABLED` is true. Everything
that can fail (importing `packages.scene_graph`, building the store, opening the lock connection)
happens here, behind one `try`: a failure is logged with its reason and the service keeps running
without the worker. The runner runs in its own thread (the worker is synchronous); a crash inside
it is logged and the loop restarts it after a pause, so it never takes the process down.
`stop()` stops the thread, joins it, and closes the runner (every worker's advisory lock) and the
guard's connections.
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
        self._thread: Optional[threading.Thread] = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="scene-graph-worker", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.runner.run(self._stop.wait, self._stop.is_set)
            except Exception:  # noqa: BLE001
                log.exception("scene graph runner crashed; restarting in %.0fs", self.restart_pause_s)
                try:
                    self.runner.close()
                except Exception:  # noqa: BLE001
                    log.exception("scene graph runner: close after crash failed")
                self._stop.wait(self.restart_pause_s)

    def stop(self, timeout: float = JOIN_TIMEOUT_S) -> None:
        """Stop and join the runner thread, release the locks. Blocking: call it off the event loop."""
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
            if thread.is_alive():
                log.error("scene graph runner did not stop within %.0fs", timeout)
        for closer in (self.runner, self.guard):
            try:
                if closer is not None:
                    closer.close()
            except Exception:  # noqa: BLE001
                log.exception("scene graph host: close failed")
        self._thread = None


def params_dict(params: Any) -> dict:
    """The params as the store persists them (`EngineState.params`); the hash is taken over this."""
    return dict(robot_half_side=params.robot_half_side, lattice=params.lattice,
                place_mult=params.place_mult, sight_range=params.sight_range)


def expected_params_hash(params: Any) -> str:
    """The `params_hash` the store writes for a graph built with `params`."""
    from packages.scene_graph.store import PIPELINE
    from packages.scene_graph.store_codec import params_hash
    return params_hash(params_dict(params), PIPELINE)


def worker_config(params: Any) -> Any:
    """WorkerConfig from the SCENE_GRAPH_* settings of packages.config."""
    from packages import config
    from . import scene_worker
    return scene_worker.WorkerConfig(
        batch_min_leaves=max(1, config.SCENE_GRAPH_BATCH_MIN_LEAVES),
        batch_max_leaves=max(1, config.SCENE_GRAPH_BATCH_MAX_LEAVES),
        min_save_interval_s=config.SCENE_GRAPH_PUBLISH_INTERVAL_S,
        batch_budget_s=config.SCENE_GRAPH_BATCH_BUDGET_S,
        expected_params_hash=expected_params_hash(params))


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
    database = getattr(service, "database", None)
    sink = scene_events.make_event_sink(database, loop) if database is not None and loop is not None else None

    def make_worker(map_id: str) -> Any:
        meta = store.meta(map_id) or {}
        stored = int(meta.get("version") or 0)
        if stored and meta.get("params_hash") not in (None, cfg.expected_params_hash):
            engine = ChartGraph(params)         # params changed: the worker rebuilds a new generation
        else:
            engine = store.load(map_id) if stored else ChartGraph(params)
        return scene_worker.SceneWorker(
            map_id, source=service.graph_db, fetch_png=fetch_png, footprint=footprint, engine=engine,
            store=store, cursor_store=cursors, guard=guard, config=cfg,
            now_ms=lambda: int(time.time() * 1000),
            publish=scene_events.make_publish(service.update_publisher, loop, map_id),
            new_engine=lambda: ChartGraph(params), fence_errors=(StaleVersion,),
            on_event=(lambda kind, payload: sink(map_id, kind, payload)) if sink else None)

    switched_off: set = set()

    def list_maps() -> list:
        """MinIO buckets that are live rows of the maps registry: a bucket alone is not a map.
        The per-map switch is read in the same discovery (one query), for `skip_reason`."""
        maps = guard.registered_maps(service.image_db.list_maps())
        try:
            off = guard.disabled_maps(maps)
            switched_off.clear()
            switched_off.update(off)
        except Exception:  # noqa: BLE001  an unanswered question keeps the previous answer
            log.exception("scene graph: per-map switch lookup failed; keeping the previous answer")
        return maps

    def skip_reason(map_id: str) -> Optional[str]:
        return "map_not_enabled" if map_id in switched_off else None

    runner = scene_runner.SceneRunner(list_maps, make_worker, on_event=sink, skip_reason=skip_reason)
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
