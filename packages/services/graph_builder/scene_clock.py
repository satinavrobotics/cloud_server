"""Per-session clock-offset estimate for the scene-graph ingest worker
(docs/scene_graph/integration.md, "Review findings" item 4).

The fold keeps, per cell, the value with the newest capture time, and capture times are compared
across robots and sessions. Trusting a robot's own stamp makes the robot whose clock runs ahead
win every cell ("fastest clock wins"); an offline upload legitimately looks old, so a plain
receipt-age check cannot tell it from a behind clock. What can be told apart is the *delay*
`receipt - stamp` of each observation: it is `clock offset + transport/queue delay`, and the
delay part is >= 0. The least delayed observation of a session therefore approximates the
offset (plus the minimal latency):

    offset = min over the session's observations of (receipt - stamp)
    corrected stamp = min(stamp + offset, receipt)

* A live leaf in a session pins the offset, however many old leaves are uploaded with it.
* A session that only ever uploads late (all leaves delayed) gets an offset that is too large
  by that minimum delay: its stamps are moved later, but never beyond the receipt (clamp). That
  bias is bounded by the receipt, the same bound the fallback has.
* An ahead clock yields a negative delay, hence a negative offset: stamps are moved back.
* One wildly ahead stamp lowers the minimum for the rest of the session (the min is not
  robust). `suspect` exists so the worker can count and log those robots; stamps beyond
  `max_skew_s` of the receipt should be rejected by the caller before observing (leaf_stamp does).

Until `min_obs` observations are in, the offset is not trusted: `correct` reports it and the
caller falls back to the receipt time (never newer than receipt: conservative).

The minimum only decreases with more observations, so a leaf's corrected stamp may change
between two folds of the same leaf. The fold is idempotent per (cell, capture time, value), so
refolding with the better stamp is just a newer fold; the worker needs to do nothing special.

Pure: no I/O, no wall clock (time is the receipt times fed in), no config. State is bounded:
three numbers per session and a session map capped at `max_sessions` (LRU) and `ttl_ms`.
Not thread-safe: one worker loop owns it.
"""
from collections import OrderedDict
from typing import Dict, Optional, Tuple

DEFAULT_MIN_OBS = 3
DEFAULT_SUSPECT_S = 300.0
DEFAULT_MAX_SKEW_S = 24 * 3600.0
DEFAULT_MAX_SESSIONS = 4096
DEFAULT_TTL_S = 7 * 24 * 3600.0


class _State:
    __slots__ = ("min_delay", "n", "last", "last_receipt")

    def __init__(self) -> None:
        self.min_delay: Optional[int] = None
        self.n = 0
        self.last: Optional[Tuple[int, int]] = None   # last (stamp, receipt), to skip repeats
        self.last_receipt = 0


class SessionClock:
    def __init__(self, min_obs: int = DEFAULT_MIN_OBS, suspect_s: float = DEFAULT_SUSPECT_S,
                 max_skew_s: float = DEFAULT_MAX_SKEW_S,
                 max_sessions: int = DEFAULT_MAX_SESSIONS, ttl_s: float = DEFAULT_TTL_S) -> None:
        self.min_obs = max(1, int(min_obs))
        self.suspect_ms = int(suspect_s * 1000.0)
        self.max_skew_s = float(max_skew_s)
        self.max_sessions = max(1, int(max_sessions))
        self.ttl_ms = int(ttl_s * 1000.0)
        self._sessions: "OrderedDict[str, _State]" = OrderedDict()
        self._newest = 0

    def __len__(self) -> int:
        return len(self._sessions)

    def observe(self, session: str, stamp_ms: int, receipt_ms: int) -> None:
        """Feed one (robot stamp, receipt) pair of `session`. Refeeding the same pair as the
        last one (a re-read margin) is not counted twice."""
        stamp_ms, receipt_ms = int(stamp_ms), int(receipt_ms)
        state = self._sessions.get(session)
        if state is None:
            state = self._sessions[session] = _State()
        else:
            self._sessions.move_to_end(session)
        if state.last != (stamp_ms, receipt_ms):
            state.last = (stamp_ms, receipt_ms)
            state.n += 1
            delay = receipt_ms - stamp_ms
            if state.min_delay is None or delay < state.min_delay:
                state.min_delay = delay
        state.last_receipt = max(state.last_receipt, receipt_ms)
        self._newest = max(self._newest, receipt_ms)
        self._evict()

    def _evict(self) -> None:
        while len(self._sessions) > self.max_sessions:
            self._sessions.popitem(last=False)
        if self.ttl_ms > 0:
            horizon = self._newest - self.ttl_ms
            for key in [k for k, s in self._sessions.items() if s.last_receipt < horizon]:
                del self._sessions[key]

    def offset_ms(self, session: str) -> Optional[int]:
        """The estimated `receipt - stamp` offset, or None until `min_obs` observations."""
        state = self._sessions.get(session)
        if state is None or state.n < self.min_obs or state.min_delay is None:
            return None
        return state.min_delay

    def suspect(self, session: str) -> bool:
        """True when the trusted offset exceeds the bound: a robot with a bad clock."""
        offset = self.offset_ms(session)
        return offset is not None and abs(offset) > self.suspect_ms

    def correct(self, session: str, stamp_ms: int, receipt_ms: int) -> Tuple[int, bool]:
        """Observe, then `(stamp, trusted)`: with a trusted offset the corrected stamp
        `min(stamp + offset, receipt)`; else `(receipt, False)`."""
        self.observe(session, stamp_ms, receipt_ms)
        offset = self.offset_ms(session)
        if offset is None:
            return int(receipt_ms), False
        return min(int(stamp_ms) + offset, int(receipt_ms)), True

    def offsets(self) -> Dict[str, int]:
        """session -> trusted offset (ms), for a metric / log line."""
        return {k: s.min_delay for k, s in self._sessions.items()
                if s.n >= self.min_obs and s.min_delay is not None}
