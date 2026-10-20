"""Runs one `SceneWorker` per map inside graph-builder, outside the MQTT path and `_upload_lock`
(integration.md, decision 3).

The runner's loop runs in one thread (`asyncio.to_thread(runner.run, ...)`): it never blocks the
event loop or the upload handlers. The single writer per map is the worker's guard (Postgres
advisory lock), not this runner: several graph-builder replicas may run it and each map still has
one writer.

Execution. `RunnerConfig.threads == 0` (the default here): the loop itself calls `worker.step()`,
one due map after another (a slow step delays every other map by its length). `threads >= 1`: the
steps run on a dedicated, bounded pool owned by this runner (not the event loop's default executor,
not shared with anything else), and the loop only schedules and collects. At most one step per map
is in flight (a worker is single-threaded by contract); at most `threads` steps run at once and none
is queued behind them, so the next one is picked when a thread frees up: the due map that has been
waiting longest first (earliest due time, ties rotating round-robin), which is what keeps a map that
is always WORKED from outrunning the rest. A map whose engine is not in memory (a cold load, the
slowest thing a step can do) is admitted only while it leaves one thread free for maps that are
already loaded (`threads - 1` cold steps at once, at least 1).

Memory. The workers' engines are the memory. When more than `max_resident` engines (or more than
`max_resident_bytes`, as the workers report `engine.nbytes`) are held, the least recently ACTIVE idle
worker (`evictable()`, quiet for `evict_idle_s`) is told to `evict()`: it saves what is unsaved and
drops its engine, which the next leaves reload. A map with unsaved folds is saved before it is
dropped, never dropped; if every engine is active the budget is exceeded and reported
(`over_budget`), nothing is torn down.

Fairness (inline mode): a pass gives every due map exactly one `step()` (one batch), starting one
map further along each pass (round-robin). A map must be missing from the list in two consecutive discoveries before its
worker is dropped. A listing that FAILED (raised) is "unknown", not empty: the known set is kept, no miss is
counted, `discover_errors` counts it and it is logged once per outage.

Terminal worker states are not final for the map: the worker is closed (its lock released) and
the map is retried after a pause, so a map that was deleted and re-created, a lost lock or a
rebuilt generation recovers without a restart. A change into a terminal state is logged at
warning and reported once as a `fold_failed` event (`stage` = the state) and through `on_status`;
`needs_rebuild` (a params / generation change the worker could not rebuild by itself) is retried
slowly. The rebuild into a new generation itself is the worker's.

`main.py` starts this (through scene_host) only when `SCENE_GRAPH_WORKER_ENABLED` is on (default
off). A worker's own `sleep_s` is honoured for the next visit (it grows while a map is idle);
`idle_s` is used only when the worker gives none.
"""
import logging
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from . import scene_worker as sw

log = logging.getLogger("GraphBuilderService.scene_runner")


@dataclass
class RunnerConfig:
    discover_s: float = 60.0            # how often the map list is refreshed
    idle_s: float = 2.0                 # a caught-up map is visited again after this (when the worker names no sleep)
    backoff_s: float = 10.0             # transient failure, lock held elsewhere, build errors
    terminal_retry_s: float = 60.0      # after deleting / fenced
    rebuild_retry_s: float = 300.0      # after needs_rebuild
    max_maps: int = 64                  # workers kept in memory at once (peak memory bound)
    skip_event_interval_s: float = 3600.0   # per map: how often MAP.SCENE_GRAPH_DISABLED_SKIPPED repeats
    slow_step_s: float = 0.0            # warn when one step of a map takes longer (0: off)
    not_owner_retry_s: float = 30.0     # a map whose lock another replica holds is asked for this rarely
    threads: int = 0                    # step pool size (0: steps run inline on the runner's thread)
    max_resident: int = 0               # engines kept in memory at once (0: no cap)
    max_resident_bytes: int = 0         # ... and their total engine.nbytes (0: no cap)
    evict_idle_s: float = 60.0          # an engine is evicted only after its map was quiet this long
    over_budget_log_s: float = 60.0     # how often "everything resident is active" is logged
    mem_stats_log_s: float = 60.0       # one INFO line (RSS, cgroup, engines) this often while an engine is active (0: off)


class SceneRunner:
    def __init__(self, list_maps: Callable[[], Iterable[str]],
                 make_worker: Callable[[str], "sw.SceneWorker"],
                 config: Optional[RunnerConfig] = None,
                 now: Callable[[], float] = time.monotonic,
                 on_status: Optional[Callable[[str, str], None]] = None,
                 skip_reason: Optional[Callable[[str], Optional[str]]] = None,
                 on_event: Optional[Callable[[str, str, Dict[str, Any]], None]] = None,
                 prefetch: Any = None, memory: Any = None, load_gate: Any = None) -> None:
        self.list_maps, self.make_worker = list_maps, make_worker
        self.memory = memory                # scene_memory.MemoryGuard (or None): sampled every tick, reported in snapshot
        self.load_gate = load_gate          # scene_memory.LoadGate (marker state is reported in snapshot)
        self._mem_logged: Optional[float] = None
        self.prefetch = prefetch            # shared PNG prefetch pool: snapshotted, closed with the runner
        self.config = config or RunnerConfig()
        self.now, self.on_status = now, on_status
        self.skip_reason, self.on_event = skip_reason, on_event     # reason or None; (map, kind, payload)
        self._absent: Dict[str, int] = {}
        self.discover_errors = 0
        self._discover_failing = False
        self._skip_reported: Dict[str, float] = {}
        self._capped: set = set()           # maps already reported as starved by max_maps
        self._rr = 0
        self._workers: Dict[str, Any] = {}
        self._due: Dict[str, float] = {}
        self._known: List[str] = []
        self._discovered_at: Optional[float] = None
        self.last_state: Dict[str, str] = {}
        self._last_step_at: Dict[str, float] = {}
        self.loops = 0
        self._pool: Optional[ThreadPoolExecutor] = None
        self._inflight: Dict[str, Tuple[Future, str, bool]] = {}    # map -> (future, "step" | "evict", cold)
        self._drop_after: Set[str] = set()      # dropped while a step ran: closed when it ends
        self._active_at: Dict[str, float] = {}  # map -> last time it did work (eviction order)
        self._queue_depth = 0                   # due maps that found no free thread at the last tick
        self.evictions = 0
        self.over_budget = False
        self._over_logged: Optional[float] = None

    @property
    def workers(self) -> Dict[str, Any]:
        return self._workers

    def tick(self, should_stop: Optional[Callable[[], bool]] = None) -> int:
        """One pass: inline mode steps every due map once (the start rotating) and returns how many were
        stepped; pooled mode collects finished steps, dispatches due maps onto free threads (earliest due
        first) and returns how many steps were collected. `should_stop` is checked between maps."""
        if self.config.threads > 0:
            return self._tick_pooled(should_stop)
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
        self._enforce_budget(self.now(), inline=True)
        self._memory_tick(self.now())
        return stepped

    def run(self, sleep: Callable[[float], None], should_stop: Callable[[], bool],
            tick_sleep_s: float = 0.5) -> None:
        try:
            while not should_stop():
                if self.tick(should_stop) == 0:
                    if self._inflight:
                        # steps are running: wake when one ends (or a map becomes due)
                        wait([f for f, _k, _c in self._inflight.values()], timeout=tick_sleep_s,
                             return_when=FIRST_COMPLETED)
                    else:
                        sleep(tick_sleep_s)
        finally:
            self.close()

    def snapshot(self) -> Dict[str, Any]:
        """Runner state for /health: {maps: {map: {state, last_step_age_s, resident_bytes, ...}}, pool,
        resident, prefetch}."""
        now = self.now()
        maps: Dict[str, Any] = {}
        resident, total, unknown = 0, 0, 0
        for map_id in list(self._known):
            at = self._last_step_at.get(map_id)
            settled = getattr(self._workers.get(map_id), "settled", None)
            entry: Dict[str, Any] = {"state": self.last_state.get(map_id),
                                     "settled": bool(settled()) if callable(settled)
                                     and map_id not in self._inflight else False,
                                     "last_step_age_s": None if at is None else round(max(0.0, now - at), 1)}
            worker = self._workers.get(map_id)
            stats = getattr(worker, "stats", None)
            if stats is not None:
                entry.update(version=getattr(stats, "version", None), pending=getattr(stats, "pending", None),
                             buffered=getattr(stats, "buffered_leaves", None),
                             version_lag_s=round(getattr(stats, "version_lag_s", 0.0), 1),
                             skipped=dict(getattr(stats, "skipped", {}) or {}),
                             resident=bool(getattr(stats, "resident", False)),
                             resident_bytes=getattr(stats, "resident_bytes", None),
                             canvas=getattr(stats, "canvas", None),
                             loads=getattr(stats, "loads", 0), evictions=getattr(stats, "evictions", 0),
                             prefetch_hits=getattr(stats, "prefetch_hits", 0),
                             already_folded=getattr(stats, "already_folded", 0),
                             default_r_d=getattr(stats, "default_r_d", 0),
                             views_built=getattr(stats, "views_built", 0),
                             views_missing=getattr(stats, "views_missing", 0),
                             view_failures=getattr(stats, "view_failures", 0))
                if getattr(stats, "resident", False):
                    resident += 1
                    nb = getattr(stats, "resident_bytes", None)
                    total += int(nb or 0)
                    unknown += nb is None
            maps[map_id] = entry
        out: Dict[str, Any] = {
            "maps": maps, "workers": len(self._workers), "discover_errors": self.discover_errors,
            "pool": {"threads": self.config.threads, "inflight": len(self._inflight),
                     "queue_depth": self._queue_depth,
                     "cold_inflight": sum(1 for _f, _k, cold in self._inflight.values() if cold)},
            "resident": {"engines": resident, "bytes": total, "bytes_unknown": unknown,
                         "max_engines": self.config.max_resident,
                         "max_bytes": self.config.max_resident_bytes,
                         "evictions": self.evictions, "over_budget": self.over_budget}}
        out["memory"] = self.memory_snapshot()
        if self.prefetch is not None:
            snap = getattr(self.prefetch, "snapshot", None)
            if snap is not None:
                try:
                    out["prefetch"] = snap()
                except Exception:  # noqa: BLE001
                    log.exception("scene runner: prefetch snapshot failed")
        return out

    def close(self) -> None:
        """Stop the pool (running steps finish: a worker is never closed under its step), then close every
        worker. The runner can be run again afterwards (the pool is created on demand)."""
        pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=True)
        self._inflight.clear()
        self._drop_after.clear()
        for map_id in list(self._workers):
            self._drop(map_id)
        if self.prefetch is not None:
            try:
                self.prefetch.close()
            except Exception:  # noqa: BLE001
                log.exception("scene runner: closing the prefetch pool failed")

    # ------------------------------------------------------------------ internals

    def _discover(self, t: float) -> None:
        if self._discovered_at is not None and t - self._discovered_at < self.config.discover_s:
            return
        try:
            maps = sorted({m for m in self.list_maps() if isinstance(m, str) and m})
        except Exception as exc:  # noqa: BLE001  unknown is not empty: keep everything, count no miss
            self.discover_errors += 1
            if not self._discover_failing:
                self._discover_failing = True
                log.warning("scene runner: listing maps failed (%s); keeping the known maps", exc)
            self._discovered_at = t
            return
        if self._discover_failing:
            self._discover_failing = False
            log.info("scene runner: map listing recovered")
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
            self._last_step_at.pop(gone, None)
            self._capped.discard(gone)
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

    def _worker_for(self, map_id: str, t: float) -> Optional[Any]:
        """The map's worker, built on first use; None (and the map retried later) when the max_maps cap
        or a failing factory keeps it from starting."""
        cfg = self.config
        worker = self._workers.get(map_id)
        if worker is not None:
            return worker
        # a worker that does not hold its map's lock (NOT_OWNER) holds no engine: not counted
        active = sum(1 for m in self._workers if self.last_state.get(m) != sw.NOT_OWNER)
        if active >= cfg.max_maps:
            self._due[map_id] = t + cfg.backoff_s
            if map_id not in self._capped:       # once per map, not once per retry
                self._capped.add(map_id)
                log.warning("scene runner %s: not started, %d workers already running "
                            "(max_maps)", map_id, active)
                self._report(map_id, "fold_failed", {
                    "stage": "max_maps", "consecutive": 1,
                    "detail": f"{active} workers running (max_maps {cfg.max_maps}): "
                              f"this map is not ingested"})
            return None
        try:
            worker = self._workers[map_id] = self.make_worker(map_id)
            self._capped.discard(map_id)
            self._active_at[map_id] = self.now()
        except Exception:  # noqa: BLE001
            log.exception("scene runner %s: building the worker failed", map_id)
            self._due[map_id] = t + cfg.backoff_s
            return None
        return worker

    def _step_map(self, map_id: str, t: float) -> None:
        """Inline mode: one step of one map, on this thread."""
        worker = self._worker_for(map_id, t)
        if worker is None:
            return
        started = self.now()
        try:
            result = worker.step()
        except Exception:  # noqa: BLE001
            log.exception("scene runner %s: step failed", map_id)
            self._drop(map_id)
            self._due[map_id] = t + self.config.backoff_s
            return
        self._apply(map_id, t, worker, result, self.now() - started)

    def _apply(self, map_id: str, t: float, worker: Any, result: Any, took: float) -> None:
        """What a finished step means for the map: state transitions, events, when it is due next."""
        cfg = self.config
        self._last_step_at[map_id] = self.now()
        if cfg.slow_step_s and took > cfg.slow_step_s:
            log.warning("scene runner %s: step took %.1fs (%d leaves folded)", map_id, took,
                        getattr(result, "folded", 0))
        state = result.state
        if state == sw.WORKED or getattr(result, "folded", 0) or getattr(result, "version", None) is not None:
            self._active_at[map_id] = self.now()
        if self.last_state.get(map_id) != state:
            self.last_state[map_id] = state
            if state in sw.TERMINAL:
                log.warning("scene runner %s: worker stopped: %s (%s)", map_id, state,
                            getattr(result, "detail", ""))
                self._report(map_id, "fold_failed", {
                    "stage": state, "consecutive": 1, "detail": str(getattr(result, "detail", ""))[:300]})
                if self.on_status is not None:
                    try:
                        self.on_status(map_id, state)
                    except Exception:  # noqa: BLE001
                        log.exception("scene runner %s: status callback failed", map_id)
        if state == sw.WORKED:
            self._due[map_id] = t
        elif state == sw.IDLE:
            self._due[map_id] = t + (getattr(result, "sleep_s", 0.0) or cfg.idle_s)
        elif state == sw.NOT_OWNER:
            self._due[map_id] = t + max(cfg.not_owner_retry_s, getattr(result, "sleep_s", 0.0) or 0.0)
        elif state == sw.BACKOFF:
            self._due[map_id] = t + max(cfg.backoff_s, getattr(result, "sleep_s", 0.0) or 0.0)
        else:                                   # terminal: release the lock, retry later
            self._drop(map_id)
            self._due[map_id] = t + (cfg.rebuild_retry_s if state == sw.NEEDS_REBUILD
                                     else cfg.terminal_retry_s)

    # ---- pooled mode

    def _ensure_pool(self) -> ThreadPoolExecutor:
        if self._pool is None:
            self._pool = ThreadPoolExecutor(max(1, self.config.threads), thread_name_prefix="scene-step")
        return self._pool

    def _tick_pooled(self, should_stop: Optional[Callable[[], bool]]) -> int:
        cfg = self.config
        collected = self._collect()
        t = self.now()
        self._discover(t)
        known = list(self._known)
        if known:
            k = self._rr % len(known)
            known = known[k:] + known[:k]
        self._rr += 1
        # earliest due first (stable: ties keep the rotating order), so the map that has waited longest
        # runs next and a map that is always WORKED cannot outrun the rest
        ready = sorted((m for m in known if m not in self._inflight and self._due.get(m, 0.0) <= t),
                       key=lambda m: self._due.get(m, 0.0))
        free = cfg.threads - len(self._inflight)
        cold_cap = max(1, cfg.threads - 1)
        cold_now = sum(1 for _f, _k, cold in self._inflight.values() if cold)
        waiting = 0
        for map_id in ready:
            if should_stop is not None and should_stop():
                break
            if free <= 0:
                waiting += 1
                continue
            worker = self._worker_for(map_id, t)
            if worker is None:
                continue
            cold = bool(getattr(worker, "cold", False))
            if cold and cold_now >= cold_cap:
                waiting += 1
                continue
            try:
                future = self._ensure_pool().submit(self._run_step, worker)
            except RuntimeError:                # pool shut down under us (close)
                break
            self._inflight[map_id] = (future, "step", cold)
            free -= 1
            cold_now += cold
        self._queue_depth = waiting
        self._enforce_budget(t, inline=False)
        self._memory_tick(t)
        return collected

    @staticmethod
    def _run_step(worker: Any) -> Tuple[Any, float]:
        started = time.monotonic()
        return worker.step(), time.monotonic() - started

    @staticmethod
    def _run_evict(worker: Any) -> bool:
        return bool(worker.evict())

    def _collect(self) -> int:
        """Apply every finished step / eviction; returns how many steps ended."""
        done = [m for m, (f, _k, _c) in self._inflight.items() if f.done()]
        steps = 0
        for map_id in done:
            future, kind, _cold = self._inflight.pop(map_id)
            t = self.now()
            deferred = map_id in self._drop_after
            self._drop_after.discard(map_id)
            worker = self._workers.get(map_id)
            exc = future.exception()
            if deferred:                        # dropped while it ran: its result no longer matters
                self._drop(map_id)
                continue
            if kind == "evict":
                if exc is not None:
                    log.error("scene runner %s: evicting the engine failed", map_id, exc_info=exc)
                elif future.result():
                    self.evictions += 1
                continue
            steps += 1
            if exc is not None:
                log.error("scene runner %s: step failed", map_id, exc_info=exc)
                self._drop(map_id)
                self._due[map_id] = t + self.config.backoff_s
                continue
            result, took = future.result()
            if worker is not None:
                self._apply(map_id, t, worker, result, took)
        return steps

    # ---- memory budget

    def _enforce_budget(self, t: float, inline: bool) -> None:
        cfg = self.config
        if cfg.max_resident <= 0 and cfg.max_resident_bytes <= 0:
            return
        resident, total = [], 0
        for map_id, worker in self._workers.items():
            stats = getattr(worker, "stats", None)
            if stats is not None and getattr(stats, "resident", False):
                nb = int(getattr(stats, "resident_bytes", None) or 0)
                resident.append((map_id, worker, nb))
                total += nb
        count = len(resident)
        # engines already being evicted still count until the eviction is collected: do not stack more
        pending = sum(1 for _f, kind, _c in self._inflight.values() if kind == "evict")

        def over(n: int, b: int) -> bool:
            return ((cfg.max_resident > 0 and n > cfg.max_resident)
                    or (cfg.max_resident_bytes > 0 and b > cfg.max_resident_bytes))

        if not over(count, total):
            self.over_budget = False
            return
        if pending:
            return
        candidates = [(self._active_at.get(m, 0.0), m, w, nb) for m, w, nb in resident
                      if m not in self._inflight and t - self._active_at.get(m, 0.0) >= cfg.evict_idle_s
                      and getattr(w, "evictable", lambda: False)()]
        candidates.sort(key=lambda c: c[0])
        n, b = count, total
        for _at, map_id, worker, nb in candidates:
            if not over(n, b):
                break
            n, b = n - 1, b - nb
            if inline:
                try:
                    if worker.evict():
                        self.evictions += 1
                except Exception:  # noqa: BLE001
                    log.exception("scene runner %s: evicting the engine failed", map_id)
            else:
                try:
                    self._inflight[map_id] = (self._ensure_pool().submit(self._run_evict, worker),
                                              "evict", False)
                except RuntimeError:
                    break
        self.over_budget = over(n, b)
        if self.over_budget and (self._over_logged is None or t - self._over_logged >= cfg.over_budget_log_s):
            self._over_logged = t
            log.warning("scene runner: %d engines / %d bytes resident, over budget (max %d / %d bytes) and "
                        "no idle engine to evict", n, b, cfg.max_resident, cfg.max_resident_bytes)

    # ---- memory report

    def memory_snapshot(self) -> Dict[str, Any]:
        """RSS, cgroup memory.current / max, oom counters, the guard's verdict, the load marker, and per resident
        engine its nbytes and canvas (H, W)."""
        from . import scene_memory
        out: Dict[str, Any] = (self.memory.snapshot() if self.memory is not None
                               else scene_memory.process_stats())
        engines: Dict[str, Any] = {}
        for map_id, worker in list(self._workers.items()):
            st = getattr(worker, "stats", None)
            if st is not None and getattr(st, "resident", False):
                engines[map_id] = {"nbytes": getattr(st, "resident_bytes", None),
                                   "canvas": getattr(st, "canvas", None)}
        out["engines"] = engines
        marker = getattr(self.load_gate, "marker", None)
        if marker is not None:
            out["load_marker"] = marker.snapshot()
        return out

    def _memory_tick(self, t: float) -> None:
        if self.memory is not None:
            try:
                self.memory.check()
            except Exception:  # noqa: BLE001
                log.exception("scene runner: memory guard check failed")
        every = self.config.mem_stats_log_s
        if not every or (self._mem_logged is not None and t - self._mem_logged < every):
            return
        snap = self.memory_snapshot()
        if not snap["engines"]:
            return
        self._mem_logged = t

        def mb(v: Any) -> str:
            return "n/a" if v is None else f"{v / 1048576:.0f}MB"

        def eng(e: Dict[str, Any]) -> str:
            c = e.get("canvas")
            return f"{mb(e['nbytes'])} {c[0]}x{c[1]}" if c else mb(e["nbytes"])

        log.info("scene runner: memory rss=%s cgroup=%s/%s oom=%s oom_kill=%s paused=%s engines=%s",
                 mb(snap.get("rss_bytes")), mb(snap.get("cgroup_current_bytes")), mb(snap.get("cgroup_max_bytes")),
                 snap.get("oom"), snap.get("oom_kill"), snap.get("paused", False),
                 {m: eng(e) for m, e in snap["engines"].items()})

    def _report(self, map_id: str, kind: str, payload: Dict[str, Any]) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(map_id, kind, payload)
        except Exception:  # noqa: BLE001
            log.exception("scene runner %s: event failed", map_id)

    def _drop(self, map_id: str) -> None:
        if map_id in self._inflight:
            self._drop_after.add(map_id)        # a step is running: it is closed when that step ends
            return
        worker = self._workers.pop(map_id, None)
        self._active_at.pop(map_id, None)
        if worker is None:
            return
        try:
            worker.close()
        except Exception:  # noqa: BLE001
            log.exception("scene runner %s: closing the worker failed", map_id)
