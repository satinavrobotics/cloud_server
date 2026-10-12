"""Runs one `SceneWorker` per map inside graph-builder, outside the MQTT path and `_upload_lock`
(integration.md, decision 3).

Synchronous and single-threaded: one pass (`tick`) visits the maps that are due, in turn, and
calls `worker.step()` once; a worker's own result decides when its map is visited next. Meant
to run in one thread (`asyncio.to_thread(runner.run, ...)`), so a slow re-solve of one map
delays the others but never blocks the event loop or the upload handlers. The single writer per
map is the worker's guard (Postgres advisory lock), not this runner: several graph-builder
replicas may run it and each map still has one writer.

Terminal worker states are not final for the map: the worker is closed (its lock released) and
the map is retried after a pause, so a map that was deleted and re-created, a lost lock or a
rebuilt generation recovers without a restart. `needs_rebuild` is reported once through
`on_status` and retried slowly (the rebuild itself is not implemented here).

Nothing in this file is wired into `main.py` yet: it is enabled behind
`SCENE_GRAPH_WORKER_ENABLED` (default off) once the engine and store exist.
"""
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional

from . import scene_worker as sw

log = logging.getLogger("GraphBuilderService.scene_runner")


@dataclass
class RunnerConfig:
    discover_s: float = 60.0            # how often the map list is refreshed
    idle_s: float = 2.0                 # a caught-up map is visited again after this
    backoff_s: float = 10.0             # transient failure, lock held elsewhere, build errors
    terminal_retry_s: float = 60.0      # after deleting / fenced
    rebuild_retry_s: float = 300.0      # after needs_rebuild
    max_maps: int = 64                  # workers kept in memory at once (peak memory bound)


class SceneRunner:
    def __init__(self, list_maps: Callable[[], Iterable[str]],
                 make_worker: Callable[[str], "sw.SceneWorker"],
                 config: Optional[RunnerConfig] = None,
                 now: Callable[[], float] = time.monotonic,
                 on_status: Optional[Callable[[str, str], None]] = None) -> None:
        self.list_maps, self.make_worker = list_maps, make_worker
        self.config = config or RunnerConfig()
        self.now, self.on_status = now, on_status
        self._workers: Dict[str, Any] = {}
        self._due: Dict[str, float] = {}
        self._known: List[str] = []
        self._discovered_at: Optional[float] = None
        self.last_state: Dict[str, str] = {}

    @property
    def workers(self) -> Dict[str, Any]:
        return self._workers

    def tick(self) -> int:
        """One pass over the maps that are due; returns how many were stepped."""
        t = self.now()
        self._discover(t)
        stepped = 0
        for map_id in list(self._known):
            if self._due.get(map_id, 0.0) > t:
                continue
            self._step_map(map_id, t)
            stepped += 1
        return stepped

    def run(self, sleep: Callable[[float], None], should_stop: Callable[[], bool],
            tick_sleep_s: float = 0.5) -> None:
        try:
            while not should_stop():
                if self.tick() == 0:
                    sleep(tick_sleep_s)
        finally:
            self.close()

    def close(self) -> None:
        for map_id in list(self._workers):
            self._drop(map_id)

    # ------------------------------------------------------------------ internals

    def _discover(self, t: float) -> None:
        if self._discovered_at is not None and t - self._discovered_at < self.config.discover_s:
            return
        try:
            maps = sorted(set(self.list_maps()))
        except Exception:  # noqa: BLE001
            log.exception("scene runner: listing maps failed")
            maps = self._known
        self._discovered_at = t
        for gone in set(self._known) - set(maps):
            self._drop(gone)
            self._due.pop(gone, None)
            self.last_state.pop(gone, None)
        self._known = maps

    def _step_map(self, map_id: str, t: float) -> None:
        cfg = self.config
        worker = self._workers.get(map_id)
        if worker is None:
            if len(self._workers) >= cfg.max_maps:
                self._due[map_id] = t + cfg.backoff_s
                return
            try:
                worker = self._workers[map_id] = self.make_worker(map_id)
            except Exception:  # noqa: BLE001
                log.exception("scene runner %s: building the worker failed", map_id)
                self._due[map_id] = t + cfg.backoff_s
                return
        try:
            result = worker.step()
        except Exception:  # noqa: BLE001
            log.exception("scene runner %s: step failed", map_id)
            self._drop(map_id)
            self._due[map_id] = t + cfg.backoff_s
            return
        state = result.state
        if self.last_state.get(map_id) != state:
            self.last_state[map_id] = state
            if self.on_status is not None and state in sw.TERMINAL:
                try:
                    self.on_status(map_id, state)
                except Exception:  # noqa: BLE001
                    log.exception("scene runner %s: status callback failed", map_id)
        if state == sw.WORKED:
            self._due[map_id] = t
        elif state == sw.IDLE:
            self._due[map_id] = t + cfg.idle_s
        elif state in (sw.BACKOFF, sw.NOT_OWNER):
            self._due[map_id] = t + max(cfg.backoff_s, getattr(result, "sleep_s", 0.0) or 0.0)
        else:                                   # terminal: release the lock, retry later
            self._drop(map_id)
            self._due[map_id] = t + (cfg.rebuild_retry_s if state == sw.NEEDS_REBUILD
                                     else cfg.terminal_retry_s)

    def _drop(self, map_id: str) -> None:
        worker = self._workers.pop(map_id, None)
        if worker is None:
            return
        try:
            worker.close()
        except Exception:  # noqa: BLE001
            log.exception("scene runner %s: closing the worker failed", map_id)
