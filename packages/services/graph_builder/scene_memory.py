"""Memory safety of the scene-graph worker: a cgroup-aware high-water guard, the load marker that breaks a
restart loop, and the process / cgroup numbers for /stats.

* `MemoryGuard`: reads the container's cgroup memory (v2 `memory.current` / `memory.max`, v1 fallback; no limit
  or no cgroup: disabled, never pauses). Above `high` (fraction of the limit) it is *paused* until the usage
  falls below `low`. While paused the worker pulls and prefetches nothing new (folds of what is buffered go on,
  nothing is dropped: the cursor is durable), no further engine is loaded, and the registered `on_high` hooks run
  (the store shrinks its tile cache). One WARNING per transition, with the numbers.
* `LoadMarker`: a small JSON file written before an engine load and removed after it succeeded. A marker found
  at start belongs to a process that died while loading that map (probably the OOM killer): that map is not
  auto-loaded again until the marker is older than the cooldown.
* `process_stats()`: VmRSS, cgroup current / max, `memory.events` oom / oom_kill.

Nothing here touches the engine's maths or any scene-graph output.
"""
import json
import logging
import os
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

log = logging.getLogger("GraphBuilderService.scene_memory")

CGROUP_ROOT = "/sys/fs/cgroup"
PROC_STATUS = "/proc/self/status"
_NO_LIMIT = 1 << 60             # cgroup v1 reports a huge number for "unlimited"


def _read(path: str) -> Optional[str]:
    try:
        with open(path, "r") as fh:
            return fh.read().strip()
    except (OSError, ValueError):
        return None


def _int(text: Optional[str]) -> Optional[int]:
    try:
        return int(text) if text is not None else None
    except ValueError:
        return None


class CgroupReader:
    """Memory numbers of this container's cgroup. Every method answers None when the file is absent."""

    def __init__(self, root: str = CGROUP_ROOT, proc_status: str = PROC_STATUS) -> None:
        self.root, self.proc_status = root, proc_status

    def current(self) -> Optional[int]:
        v = _int(_read(os.path.join(self.root, "memory.current")))
        return v if v is not None else _int(_read(os.path.join(self.root, "memory", "memory.usage_in_bytes")))

    def limit(self) -> Optional[int]:
        raw = _read(os.path.join(self.root, "memory.max"))
        if raw is not None:
            return None if raw == "max" else _int(raw)
        v = _int(_read(os.path.join(self.root, "memory", "memory.limit_in_bytes")))
        return None if v is None or v >= _NO_LIMIT else v

    def events(self) -> Dict[str, int]:
        """`oom` and `oom_kill` counters (v2 memory.events; v1 memory.oom_control has oom_kill only)."""
        out: Dict[str, int] = {}
        raw = _read(os.path.join(self.root, "memory.events"))
        if raw is None:
            raw = _read(os.path.join(self.root, "memory", "memory.oom_control"))
        for line in (raw or "").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] in ("oom", "oom_kill"):
                n = _int(parts[1])
                if n is not None:
                    out[parts[0]] = n
        return out

    def rss(self) -> Optional[int]:
        raw = _read(self.proc_status)
        for line in (raw or "").splitlines():
            if line.startswith("VmRSS:"):
                parts = line.split()
                n = _int(parts[1]) if len(parts) > 1 else None
                return None if n is None else n * 1024
        return None


def process_stats(reader: Optional[CgroupReader] = None) -> Dict[str, Any]:
    """RSS and cgroup memory of this process (None for what the host does not expose)."""
    r = reader or CgroupReader()
    ev = r.events()
    return {"rss_bytes": r.rss(), "cgroup_current_bytes": r.current(), "cgroup_max_bytes": r.limit(),
            "oom": ev.get("oom"), "oom_kill": ev.get("oom_kill")}


def _mb(n: Optional[int]) -> str:
    return "n/a" if n is None else f"{n / 1048576:.0f}MB"


class MemoryGuard:
    def __init__(self, high: float = 0.80, low: float = 0.70, reader: Optional[CgroupReader] = None,
                 now: Callable[[], float] = time.monotonic, sample_s: float = 1.0,
                 on_high: Optional[Callable[[], None]] = None, on_low: Optional[Callable[[], None]] = None) -> None:
        self.high = high
        self.low = min(low, high)
        self.reader = reader or CgroupReader()
        self.now, self.sample_s = now, sample_s
        self.on_high, self.on_low = on_high, on_low
        self._lock = threading.Lock()
        self._paused = False
        self._since: Optional[float] = None
        self._sampled_at: Optional[float] = None
        self.transitions = 0
        self.last: Dict[str, Any] = {}

    @property
    def enabled(self) -> bool:
        return 0.0 < self.high <= 1.0

    @property
    def paused(self) -> bool:
        self.check()
        return self._paused

    def check(self) -> bool:
        """Sample (at most once per `sample_s`) and apply the hysteresis. True while paused."""
        if not self.enabled:
            return False
        t = self.now()
        with self._lock:
            if self._sampled_at is not None and t - self._sampled_at < self.sample_s:
                return self._paused
            self._sampled_at = t
            cur, lim = self.reader.current(), self.reader.limit()
            if cur is None or not lim:
                self.last = {"current": cur, "limit": lim}
                return self._paused         # no cgroup number: the guard stays off (or keeps its last verdict)
            frac = cur / lim
            self.last = {"current": cur, "limit": lim, "fraction": round(frac, 4)}
            flip = None
            if not self._paused and frac >= self.high:
                self._paused, self._since, flip = True, t, "high"
            elif self._paused and frac < self.low:
                self._paused, flip = False, "low"
            if flip is not None:
                self.transitions += 1
        if flip == "high":
            log.warning("memory guard: %s of %s used (%.0f%% >= %.0f%%): intake and prefetch paused, no engine "
                        "loads, tile cache shrunk", _mb(cur), _mb(lim), frac * 100, self.high * 100)
            self._hook(self.on_high)
        elif flip == "low":
            log.warning("memory guard: %s of %s used (%.0f%% < %.0f%%): resumed", _mb(cur), _mb(lim),
                        frac * 100, self.low * 100)
            self._hook(self.on_low)
        return self._paused

    @staticmethod
    def _hook(fn: Optional[Callable[[], None]]) -> None:
        if fn is not None:
            try:
                fn()
            except Exception:  # noqa: BLE001  a relief hook must never stop the worker
                log.exception("memory guard hook failed")

    def snapshot(self) -> Dict[str, Any]:
        out = dict(process_stats(self.reader))
        out.update(guard_enabled=self.enabled, paused=self._paused, high=self.high, low=self.low,
                   transitions=self.transitions)
        return out


class LoadMarker:
    """`begin(map)` before an engine load, `clear(map)` after it succeeded. A marker of a previous process
    (found at construction) blocks that map for `cooldown_s` from its timestamp."""

    def __init__(self, path: str, cooldown_s: float = 600.0, clock: Callable[[], float] = time.time) -> None:
        self.path, self.cooldown_s, self.clock = path, cooldown_s, clock
        self._lock = threading.Lock()
        self._live: Dict[str, float] = {}               # loads in flight in THIS process
        self.dead: Dict[str, float] = {}                # map -> timestamp: left by a process that died loading it
        self._logged: set = set()
        for map_id, ts in self._read().items():
            self.dead[map_id] = ts
            log.error("scene load marker: the previous process died while loading the engine of map %s "
                      "(marker %.0f s old): it is not auto-loaded again for %.0f s", map_id,
                      max(0.0, self.clock() - ts), cooldown_s)

    def _read(self) -> Dict[str, float]:
        raw = _read(self.path)
        if not raw:
            return {}
        try:
            doc = json.loads(raw)
            return {str(m): float(ts) for m, ts in dict(doc.get("maps", {})).items()}
        except (ValueError, TypeError, AttributeError):
            return {}

    def _write(self) -> None:
        try:
            if not self._live:
                try:
                    os.remove(self.path)
                except FileNotFoundError:
                    pass
                return
            tmp = self.path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump({"maps": self._live}, fh)
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("scene load marker: cannot write %s: %s", self.path, exc)

    def blocked_for(self, map_id: str) -> float:
        """Seconds left of the cooldown for `map_id` (0: free to load)."""
        ts = self.dead.get(map_id)
        if ts is None:
            return 0.0
        left = self.cooldown_s - (self.clock() - ts)
        if left <= 0:
            self.dead.pop(map_id, None)
            return 0.0
        return left

    def begin(self, map_id: str) -> None:
        with self._lock:
            self._live[map_id] = self.clock()
            self._write()

    def clear(self, map_id: str) -> None:
        with self._lock:
            self._live.pop(map_id, None)
            self.dead.pop(map_id, None)
            self._write()

    def snapshot(self) -> Dict[str, Any]:
        return {"blocked": {m: round(self.blocked_for(m), 1) for m in list(self.dead)},
                "loading": sorted(self._live)}


class LoadGate:
    """What the worker asks before an engine load: `refusal(map) -> (reason, retry_s)` or None; around the load,
    `begin` / `done` maintain the marker."""

    def __init__(self, guard: Optional[MemoryGuard] = None, marker: Optional[LoadMarker] = None,
                 retry_s: float = 10.0) -> None:
        self.guard, self.marker, self.retry_s = guard, marker, retry_s

    def refusal(self, map_id: str) -> Optional[Tuple[str, float]]:
        if self.marker is not None:
            left = self.marker.blocked_for(map_id)
            if left > 0:
                return (f"load cooldown after a crash while loading ({left:.0f}s left)", max(self.retry_s, min(left, 60.0)))
        if self.guard is not None and self.guard.paused:
            return ("memory high: no engine load", self.retry_s)
        return None

    def begin(self, map_id: str) -> None:
        if self.marker is not None:
            self.marker.begin(map_id)

    def done(self, map_id: str, ok: bool) -> None:
        """A failed load that returned normally was not a crash: the marker goes in both cases."""
        if self.marker is not None:
            self.marker.clear(map_id)


def trim_heap() -> None:
    """Give freed heap back to the OS (glibc); a no-op elsewhere."""
    try:
        import ctypes
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:  # noqa: BLE001
        pass
