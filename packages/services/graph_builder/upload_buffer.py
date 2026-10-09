"""Uploads waiting for their node (graph-builder): images, depth images and costmap layers.

A robot may send a node's image / depth / costmap before the node itself. Each such upload is
kept per node key `(robot_name, session_node_id)` and sub-key (camera or layer) until the node is
processed, then taken; a later upload with the same sub-key overwrites the earlier one.

`lock` (a threading.RLock, shared by the service's buffers) makes "is the node there yet? else
buffer" (event loop) and "the node is there now, take what was buffered" (the topology worker
thread) atomic with respect to each other: an upload is either stored directly against an
existing node or buffered and taken by the node's processing, never lost in between
(`put_unless`, and `take` under the same lock as the caller's mark-ready).
"""

import datetime
import logging
import threading
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple, TypeVar

NodeKey = Tuple[str, Any]
T = TypeVar("T")

logger = logging.getLogger("GraphBuilderService.upload_buffer")


class NodeUploadBuffer:
    """node key -> sub-key -> (entry, buffered_at). `stats[stat]` counts the buffered entries.

    Mapping-style access (`in`, `[key]`, `len`, `== {}`) reads and writes the raw dict without
    counting (debugging and tests)."""

    def __init__(self, kind: str, stats: Dict[str, int], stat: str,
                 lock: Optional[Any] = None,
                 clock: Callable[[], datetime.datetime] = datetime.datetime.now,
                 log: logging.Logger = logger):
        self.kind = kind  # 'image' | 'depth' | 'costmap' (log messages)
        self.stats = stats
        self.stat = stat
        self.lock = lock if lock is not None else threading.RLock()
        self._clock = clock
        self._log = log
        self.entries: Dict[NodeKey, Dict[str, Tuple[Any, datetime.datetime]]] = {}

    # --- raw access ---------------------------------------------------------------------------

    def __contains__(self, key: object) -> bool:
        return key in self.entries

    def __getitem__(self, key: NodeKey) -> Dict[str, Tuple[Any, datetime.datetime]]:
        return self.entries[key]

    def __setitem__(self, key: NodeKey, value: Dict[str, Tuple[Any, datetime.datetime]]) -> None:
        self.entries[key] = value

    def __iter__(self) -> Iterator[NodeKey]:
        return iter(self.entries)

    def __len__(self) -> int:
        return len(self.entries)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, NodeUploadBuffer):
            return self.entries == other.entries
        return self.entries == other

    __hash__ = None  # type: ignore[assignment]

    # --- operations ---------------------------------------------------------------------------

    def put(self, key: NodeKey, sub: str, entry: Any) -> None:
        """Buffer `entry` (overwriting the same sub-key's earlier one)."""
        with self.lock:
            subs = self.entries.setdefault(key, {})
            if sub not in subs:
                self.stats[self.stat] += 1
            subs[sub] = (entry, self._clock())

    def put_unless(self, key: NodeKey, sub: str, entry: Any,
                   target: Callable[[], Optional[T]]) -> Optional[T]:
        """`target()` under the lock: its value when not None (store directly, nothing is
        buffered), else buffer `entry` and return None."""
        with self.lock:
            found = target()
            if found is not None:
                return found
            self.put(key, sub, entry)
            return None

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
            return len(subs)

    def clear_robot(self, robot_name: str) -> int:
        """Discard everything buffered for a robot (its topomap session restarted); how many
        node keys were cleared."""
        with self.lock:
            keys = [k for k in self.entries if k[0] == robot_name]
            for key in keys:
                self.stats[self.stat] -= len(self.entries.pop(key))
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
                    del subs[sub]
                    removed += 1
                    self._log.debug(f"Cleaned up old buffered {self.kind}: {key + (sub,)}")
                if not subs:
                    del self.entries[key]
            self.stats[self.stat] -= removed
        return removed
