"""Uploads waiting for their node (graph-builder): images, depth images and costmap layers.

A robot may send a node's image / depth / costmap before the node itself. Each such upload is
kept per node key `(robot_name, session_node_id)` and sub-key (camera or layer) until the node is
processed, then taken; a later upload with the same sub-key overwrites the earlier one.

`lock` (a threading.RLock, shared by the service's buffers) makes "is the node there yet? else
buffer" (event loop) and "the node is there now, take what was buffered" (the topology worker
thread) atomic with respect to each other: an upload is either stored directly against an
existing node or buffered and taken by the node's processing, never lost in between
(`put_unless`, and `take` under the same lock as the caller's mark-ready).

The buffer is bounded (`max_bytes` of the entries' `data` strings, `max_entries`): a new upload
that does not fit is not buffered (`put` returns False, `put_unless` returns DROPPED) and the
caller counts it; one overwriting the same sub-key is judged by the size it adds. The overflow
is logged once per OVERFLOW_LOG_INTERVAL_S, with the number dropped since.
"""

import datetime
import logging
import threading
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple, TypeVar

NodeKey = Tuple[str, Any]
T = TypeVar("T")

logger = logging.getLogger("GraphBuilderService.upload_buffer")

OVERFLOW_LOG_INTERVAL_S = 60.0
# put_unless: the upload did not fit and was not buffered.
DROPPED = object()


def _size(entry: Any) -> int:
    """Bytes an entry holds: its `data` string (base64 PNG / image) when it has one."""
    data = entry.get("data") if isinstance(entry, dict) else None
    return len(data) if isinstance(data, (str, bytes)) else 0


class NodeUploadBuffer:
    """node key -> sub-key -> (entry, buffered_at). `stats[stat]` counts the buffered entries;
    `bytes` is their total `_size` (None caps: unbounded).

    Mapping-style access (`in`, `[key]`, `len`, `== {}`) reads and writes the raw dict without
    counting (debugging and tests)."""

    def __init__(self, kind: str, stats: Dict[str, int], stat: str,
                 lock: Optional[Any] = None,
                 clock: Callable[[], datetime.datetime] = datetime.datetime.now,
                 log: logging.Logger = logger,
                 max_bytes: Optional[int] = None, max_entries: Optional[int] = None):
        self.kind = kind  # 'image' | 'depth' | 'costmap' (log messages)
        self.stats = stats
        self.stat = stat
        self.lock = lock if lock is not None else threading.RLock()
        self._clock = clock
        self._log = log
        self.entries: Dict[NodeKey, Dict[str, Tuple[Any, datetime.datetime]]] = {}
        self.max_bytes = max_bytes
        self.max_entries = max_entries
        self.bytes = 0
        self.overflowed = 0  # uploads not buffered because of the caps, ever
        self._overflow_unlogged = 0
        self._overflow_logged_at: Optional[datetime.datetime] = None

    # --- raw access ---------------------------------------------------------------------------

    def __contains__(self, key: object) -> bool:
        return key in self.entries

    def __getitem__(self, key: NodeKey) -> Dict[str, Tuple[Any, datetime.datetime]]:
        return self.entries[key]

    def __setitem__(self, key: NodeKey, value: Dict[str, Tuple[Any, datetime.datetime]]) -> None:
        with self.lock:
            self.bytes -= self._bytes_of(self.entries.get(key) or {})
            self.entries[key] = value
            self.bytes += self._bytes_of(value)

    def __iter__(self) -> Iterator[NodeKey]:
        return iter(self.entries)

    def __len__(self) -> int:
        return len(self.entries)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, NodeUploadBuffer):
            return self.entries == other.entries
        return self.entries == other

    __hash__ = None  # type: ignore[assignment]

    @staticmethod
    def _bytes_of(subs: Dict[str, Tuple[Any, datetime.datetime]]) -> int:
        return sum(_size(entry) for entry, _ in subs.values())

    def _count(self) -> int:
        return sum(len(subs) for subs in self.entries.values())

    # --- operations ---------------------------------------------------------------------------

    def put(self, key: NodeKey, sub: str, entry: Any) -> bool:
        """Buffer `entry` (overwriting the same sub-key's earlier one); False (not buffered,
        counted in `overflowed`) when that would exceed the caps."""
        with self.lock:
            subs = self.entries.get(key)
            old = subs.get(sub) if subs else None
            freed = _size(old[0]) if old else 0
            new_entries = 0 if old else 1
            if ((self.max_bytes is not None and self.bytes - freed + _size(entry) > self.max_bytes)
                    or (self.max_entries is not None
                        and self._count() + new_entries > self.max_entries)):
                self._overflow(key, sub)
                return False
            if subs is None:
                subs = self.entries[key] = {}
            if old is None:
                self.stats[self.stat] += 1
            subs[sub] = (entry, self._clock())
            self.bytes += _size(entry) - freed
            return True

    def _overflow(self, key: NodeKey, sub: str) -> None:
        self.overflowed += 1
        self._overflow_unlogged += 1
        now = self._clock()
        last = self._overflow_logged_at
        if last is None or (now - last).total_seconds() >= OVERFLOW_LOG_INTERVAL_S:
            self._overflow_logged_at = now
            self._log.warning(
                f"Buffered {self.kind} is full ({len(self.entries)} nodes, {self.bytes} bytes; "
                f"caps {self.max_entries} entries / {self.max_bytes} bytes): dropped "
                f"{self._overflow_unlogged} upload(s), the latest ({key[0]}, {key[1]}, {sub})")
            self._overflow_unlogged = 0

    def put_unless(self, key: NodeKey, sub: str, entry: Any,
                   target: Callable[[], Optional[T]]) -> Optional[T]:
        """`target()` under the lock: its value when not None (store directly, nothing is
        buffered), else buffer `entry` and return None, or DROPPED when it did not fit."""
        with self.lock:
            found = target()
            if found is not None:
                return found
            return None if self.put(key, sub, entry) else DROPPED

    def take(self, key: NodeKey, timeout: float,
             session_id: Optional[str] = None) -> List[Any]:
        """Remove and return the entries buffered for a node that are younger than `timeout`
        seconds and (when `session_id` is given) carry that `session_id`; the rest are logged
        and discarded."""
        with self.lock:
            subs = self.entries.pop(key, None)
            if not subs:
                return []
            self.stats[self.stat] -= len(subs)
            self.bytes -= self._bytes_of(subs)
        robot_name, session_node_id = key
        now = self._clock()
        taken = []
        for sub, (entry, at) in subs.items():
            age = (now - at).total_seconds()
            if age > timeout:
                self._log.warning(f"Buffered {self.kind} timed out: ({robot_name}, "
                                  f"{session_node_id}, {sub}), age={age:.1f}s")
            elif session_id is not None and entry.get("session_id") != session_id:
                self._log.warning(f"Buffered {self.kind} of ({robot_name}, {session_node_id}, "
                                  f"{sub}) is from another session; discarded")
            else:
                taken.append(entry)
        return taken

    def pop(self, key: NodeKey) -> int:
        """Discard what is buffered for a node (dropped); how many entries there were."""
        with self.lock:
            subs = self.entries.pop(key, None) or {}
            self.stats[self.stat] -= len(subs)
            self.bytes -= self._bytes_of(subs)
            return len(subs)

    def clear_robot(self, robot_name: str) -> int:
        """Discard everything buffered for a robot (its topomap session restarted); how many
        node keys were cleared."""
        with self.lock:
            keys = [k for k in self.entries if k[0] == robot_name]
            for key in keys:
                subs = self.entries.pop(key)
                self.stats[self.stat] -= len(subs)
                self.bytes -= self._bytes_of(subs)
            return len(keys)

    def cleanup(self, max_age: float) -> int:
        """Discard entries older than `max_age` seconds (periodic); how many."""
        now = self._clock()
        removed = 0
        with self.lock:
            for key in list(self.entries):
                subs = self.entries[key]
                for sub in [s for s, (_, at) in subs.items()
                            if (now - at).total_seconds() > max_age]:
                    self.bytes -= _size(subs[sub][0])
                    del subs[sub]
                    removed += 1
                    self._log.debug(f"Cleaned up old buffered {self.kind}: {key + (sub,)}")
                if not subs:
                    del self.entries[key]
            self.stats[self.stat] -= removed
        return removed
