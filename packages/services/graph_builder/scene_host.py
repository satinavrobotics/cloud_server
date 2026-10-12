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


def build_host(service: Any, loop: Any, conninfo: Optional[str] = None) -> SceneWorkerHost:
    """Wire the real store, engine, guard and sources. May raise (ImportError, connection errors)."""
    from packages.scene_graph.api import SceneParams        # noqa: F401
    from packages.scene_graph.engine import ChartGraph
    from packages.scene_graph.store import SceneGraphStore
    from . import scene_events, scene_guard, scene_runner, scene_sources, scene_worker

    store = SceneGraphStore.from_client(service.topomap_db)
    guard = scene_guard.PgGuard(scene_guard.connect_from_conninfo(conninfo or default_conninfo()))
    fetch_png = scene_sources.png_fetcher(service.image_db)
    footprint = scene_sources.PgFootprint(scene_guard.connect_from_conninfo(conninfo or default_conninfo()))
    cursors = scene_worker.MemoryCursorStore()

    def make_worker(map_id: str) -> Any:
        stored = int((store.meta(map_id) or {}).get("version") or 0)
        engine = store.load(map_id) if stored else ChartGraph(SceneParams())
        return scene_worker.SceneWorker(
            map_id, source=service.graph_db, fetch_png=fetch_png, footprint=footprint, engine=engine,
            store=store, cursor_store=cursors, guard=guard,
            now_ms=lambda: int(time.time() * 1000),
            publish=scene_events.make_publish(service.update_publisher, loop, map_id))

    runner = scene_runner.SceneRunner(service.image_db.list_maps, make_worker)
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
