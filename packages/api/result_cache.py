"""The small LRU + TTL + single-flight cache that the read endpoints share (drive-stats, mapping-stats,
run-stats series, map events, path efficiency, edge-stats). Keys are tuples whose first element is the
map id (`discard_map`)."""
import asyncio
import collections
import time
from typing import Any, Callable, Dict, Tuple


class ResultCache:
    """Small LRU with a TTL and single-flight computation (concurrent identical requests share one run)."""

    def __init__(self, entries: int, ttl_s: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.entries, self.ttl, self.clock = entries, ttl_s, clock
        self._done: "collections.OrderedDict[Any, Tuple[float, Any]]" = collections.OrderedDict()
        self._flight: Dict[Any, "asyncio.Future[Any]"] = {}

    def peek(self, key: Any) -> Any:
        hit = self._done.get(key)
        if hit is None or self.clock() - hit[0] > self.ttl:
            return None
        self._done.move_to_end(key)
        return hit[1]

    def discard_map(self, map_id: str) -> None:
        for k in [k for k in self._done if k[0] == map_id]:
            del self._done[k]

    async def get(self, key: Any, compute: Callable[[], Any]) -> Any:
        hit = self.peek(key)
        if hit is not None:
            return hit
        fut = self._flight.get(key)
        if fut is not None:
            return await asyncio.shield(fut)
        fut = asyncio.get_running_loop().create_future()
        self._flight[key] = fut
        try:
            value = await compute()
        except BaseException as exc:  # noqa: BLE001
            fut.set_exception(exc if isinstance(exc, Exception) else RuntimeError("cancelled"))
            fut.exception()              # mark retrieved: no "never retrieved" warning when nobody else waits
            raise
        else:
            self._done[key] = (self.clock(), value)
            while len(self._done) > self.entries:
                self._done.popitem(last=False)
            fut.set_result(value)
            return value
        finally:
            self._flight.pop(key, None)
