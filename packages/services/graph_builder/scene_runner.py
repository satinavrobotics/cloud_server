"""Runs one `SceneWorker` per map inside graph-builder, outside the MQTT path and `_upload_lock`
(integration.md, decision 3).

Synchronous and single-threaded: one pass (`tick`) visits the maps that are due, in turn, and
calls `worker.step()` once; a worker's own result decides when its map is visited next. Meant
to run in one thread (`asyncio.to_thread(runner.run, ...)`), so a slow re-solve of one map
delays the others but never blocks the event loop or the upload handlers. The single writer per
map is the worker's guard (Postgres advisory lock), not this runner: several graph-builder
replicas may run it and each map still has one writer.

Fairness: a pass gives every due map exactly one `step()` (one batch), starting one map further
along each pass (round-robin), so one slow map delays the others by at most one of its steps and
never starves them. A map must be missing from the list in two consecutive discoveries before its
worker is dropped (`image_db.list_maps` swallows MinIO errors and then returns []).

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
    skip_event_interval_s: float = 3600.0   # per map: how often MAP.SCENE_GRAPH_DISABLED_SKIPPED repeats
    slow_step_s: float = 0.0            # warn when one step of a map takes longer (0: off)


class SceneRunner:
    def __init__(self, list_maps: Callable[[], Iterable[str]],
                 make_worker: Callable[[str], "sw.SceneWorker"],
                 config: Optional[RunnerConfig] = None,
                 now: Callable[[], float] = time.monotonic,
                 on_status: Optional[Callable[[str, str], None]] = None,
                 skip_reason: Optional[Callable[[str], Optional[str]]] = None,
                 on_event: Optional[Callable[[str, str, Dict[str, Any]], None]] = None) -> None:
        self.list_maps, self.make_worker = list_maps, make_worker
        self.config = config or RunnerConfig()
        self.now, self.on_status = now, on_status
        self.skip_reason, self.on_event = skip_reason, on_event     # reason or None; (map, kind, payload)
        self._absent: Dict[str, int] = {}
        self._skip_reported: Dict[str, float] = {}
        self._rr = 0
        self._workers: Dict[str, Any] = {}
        self._due: Dict[str, float] = {}
        self._known: List[str] = []
        self._discovered_at: Optional[float] = None
        self.last_state: Dict[str, str] = {}

    @property
    def workers(self) -> Dict[str, Any]:
        return self._workers

    def tick(self, should_stop: Optional[Callable[[], bool]] = None) -> int:
        """One pass over the maps that are due, one step each, the start rotating; returns how many
        were stepped. `should_stop` is checked between maps (a step may take seconds)."""
        t = self.now()
        self._discover(t)
        known = list(self._known)
        if known:
            k = self._rr % len(known)
            known = known[k:] + known[:k]
        self._rr += 1
        stepped = 0
        for map_id in known:
            if should_stop is not None and should_stop():
                break
            if self._due.get(map_id, 0.0) > t:
                continue
            self._step_map(map_id, t)
            stepped += 1
        return stepped

    def run(self, sleep: Callable[[float], None], should_stop: Callable[[], bool],
            tick_sleep_s: float = 0.5) -> None:
        try:
            while not should_stop():
                if self.tick(should_stop) == 0:
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
            maps = sorted({m for m in self.list_maps() if isinstance(m, str) and m})
        except Exception:  # noqa: BLE001
            log.exception("scene runner: listing maps failed")
            maps = self._known
        self._discovered_at = t
        maps = self._apply_skips(maps, t)
        keep = set(maps)
        for gone in set(self._known) - set(maps):
            self._absent[gone] = self._absent.get(gone, 0) + 1
            if self._absent[gone] < 2:      # one empty / failed listing must not drop a worker
                keep.add(gone)
                continue
            self._absent.pop(gone, None)
            self._drop(gone)
            self._due.pop(gone, None)
            self.last_state.pop(gone, None)
        for present in maps:
            self._absent.pop(present, None)
        self._known = sorted(keep)

    def _apply_skips(self, maps: List[str], t: float) -> List[str]:
        """Maps the scene graph is not enabled for are not stepped; each is reported (rate limited)."""
        if self.skip_reason is None:
            return maps
        run = []
        for map_id in maps:
            try:
                reason = self.skip_reason(map_id)
            except Exception:  # noqa: BLE001
                log.exception("scene runner %s: skip check failed", map_id)
                reason = None
            if not reason:
                run.append(map_id)
                continue
            last = self._skip_reported.get(map_id)
            if self.on_event is not None and (last is None or t - last >= self.config.skip_event_interval_s):
                self._skip_reported[map_id] = t
                try:
                    self.on_event(map_id, "skipped", {"reason": reason})
                except Exception:  # noqa: BLE001
                    log.exception("scene runner %s: event failed", map_id)
        return run

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
        started = self.now()
        try:
            result = worker.step()
        except Exception:  # noqa: BLE001
            log.exception("scene runner %s: step failed", map_id)
            self._drop(map_id)
            self._due[map_id] = t + cfg.backoff_s
            return
        took = self.now() - started
        if cfg.slow_step_s and took > cfg.slow_step_s:
            log.warning("scene runner %s: step took %.1fs (%d leaves folded)", map_id, took,
                        getattr(result, "folded", 0))
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
