"""What graph-builder tells a robot about its topomap uploads (README "Robot ingest contract").

Per-node acks (`robot/<robot>/ingest_ack`, QoS 1, not retained), a retained high-water mark
(`robot/<robot>/ingest_state`, QoS 1) and the robot's own `robot/<robot>/spool_state`, kept for
the stats. Everything here is synchronous and thread-safe (one lock): it is called from the event
loop and from the node worker threads, and publishes through a callable that never blocks (paho
queues the message), so no ingest handler waits for the broker. Nothing raises into the caller
(`_send` logs and counts).

A bundle is a node and the parts its `parts` manifest declares, `(kind, sub)` with kind image /
depth / costmap and sub the camera or layer. A bundle with a manifest is tracked in memory in an
`_Entry` from the node's receipt: parts stored while the node is still in flight are recorded in
it, and the first ack goes out when the node is durable (`node_ok`). The final ack (complete) goes
out when the last declared part is stored (`part_stored`), or earlier when every part is either
stored or reported rejected (`part_rejected`), or `part_timeout_s` after the node was accepted
(`flush`: `resend: parts` for what never came; the entry stays so the resent parts still end in a
final ack, up to MAX_TIMEOUT_ACKS times). Entries are bounded (`max_trackers`, oldest evicted) and
lost with the process: the robot resends the node, and the duplicate path then reads the truth
from storage (server.py `_parts_truth`).

`ingest_state`: the highest session_node_id of a COMPLETE bundle of the robot's current run_id,
published (retained) on change, at most every `state_min_interval_s` per robot; `flush` publishes
the last value that was held back. A run_id change restarts it. Seeded once at startup from the
broker's retained messages (`seed_state`), merged with what this process already has: the same
run_id keeps the higher id, a different run keeps the local one (a completion of this process is
newer than anything retained).

"Latest run" of a robot: the run most recently seen on a node accepted in this process (run_ids are
unique per run, so a run seen before never becomes the latest again). A completion of any other run
(a late part or node of a superseded run) is still acked but does not touch `ingest_state`.
"""

import dataclasses
import json
import logging
import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Dict, List, Mapping, Optional, Set, Tuple

from packages.services.graph_builder import ingest

logger = logging.getLogger("GraphBuilderService.acks")

Part = Tuple[str, str]  # (kind, sub)
KINDS = ("image", "depth", "costmap")

ACCEPTED = "accepted"
DUPLICATE = "duplicate"
REJECTED = "rejected"
RESEND_NONE = "none"
RESEND_PARTS = "parts"
RESEND_NODE = "node"

# The robot resends nothing after these (README reason table).
TERMINAL_REASONS = frozenset({
    ingest.NO_SESSION, ingest.MAP_DELETING, ingest.MAP_MISSING, ingest.DATUM_CHANGED,
    ingest.RUN_ID_MISMATCH, ingest.INVALID_PAYLOAD})
# It may resend: the node (rejected node) or the part (rejected part).
RETRYABLE_REASONS = frozenset({
    ingest.SESSION_PAUSED, ingest.SESSION_UNPLACED, ingest.LOOKUP_FAILED, ingest.BUFFER_FULL,
    ingest.STORE_FAILED})

ACK_COALESCE_S = 1.0          # an identical ack of one (run, node) is sent at most this often
UNACCEPTED_TTL_FACTOR = 2     # a begun node never made durable is dropped after this x part_timeout_s
_RUNS_SEEN_CAP = 8            # runs remembered per robot (for "latest run")
MAX_TIMEOUT_ACKS = 3          # timeout acks per bundle, then the entry is dropped
_LAST_ACK_CAP = 5000
_ROBOT_CAP = 10000            # ingest_state / spool_state entries kept (one per robot)
_MQTT_ERR_NO_CONN = 4         # paho: QoS 1 is queued for the reconnect, not lost
_MQTT_ERR_QUEUE_SIZE = 15     # paho: the outgoing queue is at max_queued_messages


def valid_topic_level(name: Any) -> bool:
    """`name` is usable as one MQTT topic level (a non-empty string without / + # or NUL)."""
    return (isinstance(name, str) and bool(name)
            and not any(c in name for c in "/+#\x00"))


def ack_topic(robot_name: str) -> str:
    return f"robot/{robot_name}/ingest_ack"


def state_topic(robot_name: str) -> str:
    return f"robot/{robot_name}/ingest_state"


def mode_topic(robot_name: str) -> str:
    return f"robot/{robot_name}/ingest_mode"


def parse_parts(value: Any) -> Optional[List[Part]]:
    """The node_update's `parts` manifest as ordered, distinct (kind, sub) pairs. None: absent
    (or not a list); a list may be empty. Entries that are not {"kind": image|depth|costmap,
    "sub": non-empty string} are ignored, as are extra fields."""
    if not isinstance(value, list):
        return None
    parts: List[Part] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        kind, sub = item.get("kind"), item.get("sub")
        if kind in KINDS and isinstance(sub, str) and sub and (kind, sub) not in parts:
            parts.append((kind, sub))
    return parts


def _part_dicts(parts) -> List[Dict[str, str]]:
    return [{"kind": k, "sub": s} for k, s in sorted(parts)]


@dataclasses.dataclass
class _Entry:
    declared: List[Part]
    session_id: Optional[str]
    status: Optional[str] = None            # ACCEPTED / DUPLICATE once the node is durable
    stored: Set[Part] = dataclasses.field(default_factory=set)
    missing: Dict[Part, str] = dataclasses.field(default_factory=dict)  # rejected part -> reason
    since: float = 0.0                      # when the node was accepted / the last timeout ack
    timeouts: int = 0


@dataclasses.dataclass
class _State:
    run_id: Optional[str]
    highest: int
    dirty: bool = False
    published_at: float = float("-inf")


class AckManager:
    """See the module docstring. `publish(topic, payload, qos, retain)` returns paho's rc (or
    None when there is no client); `stats` is the service's counters dict."""

    def __init__(self, publish: Callable[[str, str, int, bool], Optional[int]],
                 stats: Dict[str, int], part_timeout_s: float = 120.0,
                 max_trackers: int = 20000, state_min_interval_s: float = 1.0,
                 clock: Callable[[], float] = time.monotonic,
                 wall_ms: Callable[[], int] = lambda: int(time.time() * 1000)):
        self._publish = publish
        self.stats = stats
        self.part_timeout_s = part_timeout_s
        self.max_trackers = max_trackers
        self.state_min_interval_s = state_min_interval_s
        self._clock = clock
        self._wall_ms = wall_ms
        self._lock = threading.Lock()
        # (robot, run_id, session_node_id) -> _Entry, least recently touched first
        self._entries: "OrderedDict[Tuple, _Entry]" = OrderedDict()
        self._last_ack: "OrderedDict[Tuple, Tuple[str, float]]" = OrderedDict()
        self._states: "OrderedDict[str, _State]" = OrderedDict()
        self._runs: "OrderedDict[str, List[Optional[str]]]" = OrderedDict()  # robot -> runs seen
        self._spool: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        # robot -> the register_map last published on ingest_mode (True / False / None = unknown)
        self._modes: "OrderedDict[str, Optional[bool]]" = OrderedDict()
        for name in ("acks_sent", "acks_failed", "acks_dropped", "acks_coalesced",
                     "acks_invalid_robot", "ack_trackers_evicted", "ack_timeouts",
                     "ingest_state_published", "ingest_mode_published"):
            stats.setdefault(name, 0)

    # --- events ----------------------------------------------------------------------------

    def begin(self, robot: Any, run_id: Optional[str], sid: Any, manifest: Optional[List[Part]],
              session_id: Optional[str]) -> None:
        """A node with a non-empty manifest was received (not stored yet): parts stored from now
        on are recorded for it. A second receipt (a resend) keeps what is known."""
        if not manifest:
            return
        with self._lock:
            key = (robot, run_id, sid)
            entry = self._entries.get(key)
            if entry is None:
                self._entries[key] = _Entry(list(manifest), session_id, since=self._clock())
                self._evict()
            else:
                entry.declared = list(manifest)
                entry.session_id = session_id
                self._entries.move_to_end(key)

    def node_ok(self, robot: Any, run_id: Optional[str], sid: Any, status: str,
                session_id: Optional[str], manifest: Optional[List[Part]],
                truth: Optional[Set[Part]] = None) -> None:
        """The node is durable (stored and marked complete, or found stored): the first ack.
        `truth`: the parts found in storage (the duplicate path)."""
        out: List[Tuple[str, str, bool]] = []
        with self._lock:
            key = (robot, run_id, sid)
            self._note_run(robot, run_id)
            if manifest is None:  # no manifest: parts cannot be judged
                self._entries.pop(key, None)
                self._ack(out, robot, run_id, sid, status, None, RESEND_NONE, (), (), None,
                          session_id)
            elif not manifest:
                self._entries.pop(key, None)
                self._ack(out, robot, run_id, sid, status, None, RESEND_NONE, (), (), True,
                          session_id)
                self._complete(out, robot, run_id, sid)
            else:
                entry = self._entries.get(key)
                if entry is None:
                    entry = self._entries[key] = _Entry(list(manifest), session_id)
                    self._evict()
                entry.declared = list(manifest)
                entry.session_id = session_id
                entry.status = status
                entry.since = self._clock()
                entry.missing.clear()
                if truth:
                    entry.stored |= set(truth)
                self._entries.move_to_end(key)
                pending = [p for p in entry.declared if p not in entry.stored]
                if not pending:
                    self._final(out, key, entry)
                else:
                    # A fresh node: its parts are on the way. A resent one: say what to resend.
                    self._ack(out, robot, run_id, sid, status, None,
                              RESEND_PARTS if status == DUPLICATE else RESEND_NONE,
                              entry.stored, pending, False, session_id)
        self._send(out)

    def node_failed(self, robot: Any, run_id: Optional[str], sid: Any, reason: str,
                    manifest: Optional[List[Part]], session_id: Optional[str]) -> None:
        """The node was not stored. Retryable reasons: resend the node."""
        out: List[Tuple[str, str, bool]] = []
        with self._lock:
            self._entries.pop((robot, run_id, sid), None)
            self._ack(out, robot, run_id, sid, REJECTED, reason,
                      RESEND_NODE if reason in RETRYABLE_REASONS else RESEND_NONE,
                      (), manifest or (), False, session_id)
        self._send(out)

    def node_abandoned(self, robot: Any, run_id: Optional[str], sid: Any,
                       manifest: Optional[List[Part]], session_id: Optional[str]) -> None:
        """The handler of a begun node ended abnormally (exception, cancellation). If the node was
        not acked yet (its tracker has no status) it fails, retryable: resend the node. Otherwise
        (acked, or no tracker) nothing is sent."""
        with self._lock:
            entry = self._entries.get((robot, run_id, sid))
            pending = entry is not None and entry.status is None
        if pending:
            self.node_failed(robot, run_id, sid, ingest.STORE_FAILED, manifest, session_id)

    def part_stored(self, robot: Any, run_id: Optional[str], sid: Any, kind: str,
                    sub: str) -> None:
        """A part is durable (MinIO and, for depth / costmap, the node's record)."""
        out: List[Tuple[str, str, bool]] = []
        with self._lock:
            key = (robot, run_id, sid)
            entry = self._entries.get(key)
            if entry is None:
                return
            entry.stored.add((kind, sub))
            entry.missing.pop((kind, sub), None)
            if entry.status is not None:
                self._evaluate(out, key, entry)
        self._send(out)

    def part_rejected(self, robot: Any, run_id: Optional[str], sid: Any, kind: str, sub: str,
                      reason: str, session_id: Optional[str] = None) -> None:
        """A part was not stored. Of an accepted bundle it is recorded (the final or timeout ack
        lists it); otherwise it is acked on its own."""
        out: List[Tuple[str, str, bool]] = []
        part = (kind, sub)
        with self._lock:
            key = (robot, run_id, sid)
            entry = self._entries.get(key)
            if entry is not None and part in entry.stored:
                return  # stored already (a resend that came too late)
            if entry is not None and entry.status is not None and part in entry.declared:
                entry.missing[part] = reason
                self._evaluate(out, key, entry)
            else:
                self._ack(out, robot, run_id, sid, REJECTED, reason,
                          RESEND_PARTS if reason in RETRYABLE_REASONS else RESEND_NONE,
                          (), [part], False, session_id)
        self._send(out)

    def flush(self) -> None:
        """Periodic: timeout acks for bundles whose parts did not all arrive, and the
        ingest_state values that were held back."""
        out: List[Tuple[str, str, bool]] = []
        with self._lock:
            now = self._clock()
            for key, entry in list(self._entries.items()):
                if entry.status is None:  # begun, never made durable nor failed: do not linger
                    if now - entry.since >= self.part_timeout_s * UNACCEPTED_TTL_FACTOR:
                        del self._entries[key]
                        self.stats["ack_trackers_evicted"] += 1
                    continue
                if now - entry.since < self.part_timeout_s:
                    continue
                pending = [p for p in entry.declared if p not in entry.stored]
                self.stats["ack_timeouts"] += 1
                if pending:
                    self._ack(out, key[0], key[1], key[2], entry.status, None, RESEND_PARTS,
                              entry.stored, pending, False, entry.session_id)
                entry.timeouts += 1
                entry.since = now
                entry.missing.clear()
                if not pending or entry.timeouts >= MAX_TIMEOUT_ACKS:
                    del self._entries[key]
            for robot, state in self._states.items():
                self._maybe_state(out, robot, state, now)
        self._send(out)

    # --- ingest_state / spool_state -------------------------------------------------------

    def seed_state(self, robot: Any, payload: Any) -> bool:
        """A retained ingest_state read at startup, merged with the local state: same run_id, the
        higher id; a different run, the local one stays (it is newer). Published again only if
        the merged value is higher than the seeded one, or the retained message was already
        overwritten by a lower local value. Returns whether the state took the seeded value."""
        highest = payload.get("highest_complete_id") if isinstance(payload, Mapping) else None
        if (not valid_topic_level(robot) or isinstance(highest, bool)
                or not isinstance(highest, int)):
            return False
        run_id = payload.get("run_id")
        with self._lock:
            run_id = None if run_id is None else str(run_id)
            state = self._states.get(robot)
            if state is None:
                self._states[robot] = _State(run_id, highest)
                self._cap(self._states)
                return True
            if state.run_id != run_id:
                return False
            local = state.highest
            if highest > local:
                state.highest = highest
                state.dirty = state.published_at > float("-inf")  # retained was overwritten lower
            elif local > highest:
                state.dirty = True
            return highest > local

    def seed_mode(self, robot: Any, payload: Any) -> None:
        """A retained ingest_mode read at startup: remembered as what the robot currently sees
        (nothing is published), so `note_mode` corrects a stale value, e.g. a `false` left by a
        mission that ended while this service was down."""
        if not valid_topic_level(robot) or not isinstance(payload, Mapping):
            return
        value = payload.get("register_map")
        with self._lock:
            if robot not in self._modes:
                self._modes[robot] = value if isinstance(value, bool) else None
                self._cap_modes()

    def note_mode(self, robot: Any, register_map: Optional[bool]) -> bool:
        """The register_map of the robot's active mission (None: no mission known / unknown).
        Publishes the retained `ingest_mode` only when it differs from what was last published,
        so it never flaps; the first note of an unseeded robot with None publishes nothing (the
        robot already reads a missing topic as unknown). The value is remembered only once the
        publish was accepted by the MQTT client (queued counts; no client / full queue / error /
        bad rc does not): otherwise the previous state is restored, so the next call retries.
        Never raises. Returns whether it published."""
        if register_map is not None and not isinstance(register_map, bool):
            register_map = None
        if not valid_topic_level(robot):
            return False
        out: List[Tuple[str, str, bool]] = []
        with self._lock:
            if robot not in self._modes and register_map is None:
                return False
            if self._modes.get(robot) == register_map and robot in self._modes:
                return False
            had = robot in self._modes
            previous = self._modes.get(robot)
            self._modes[robot] = register_map
            self._modes.move_to_end(robot)
            self._cap_modes()
            body = {"v": 1, "robot_name": robot, "ts": self._wall_ms()}
            if register_map is not None:
                body["register_map"] = register_map
            out.append((mode_topic(robot), json.dumps(body), True))
        accepted = self._send(out)
        if not all(accepted):
            with self._lock:  # not handed to MQTT: forget it, the next refresh retries
                if self._modes.get(robot) == register_map:
                    if had:
                        self._modes[robot] = previous
                    else:
                        self._modes.pop(robot, None)
            return False
        return True

    def mode_robots(self) -> List[str]:
        """Robots whose last published register_map is known (True / False)."""
        with self._lock:
            return [r for r, v in self._modes.items() if v is not None]

    def note_spool(self, robot: Any, payload: Any) -> bool:
        """The robot's retained spool_state, the latest per robot."""
        if not valid_topic_level(robot) or not isinstance(payload, Mapping):
            return False
        with self._lock:
            self._spool[robot] = {"run_id": payload.get("run_id"),
                                  "last_spooled_id": payload.get("last_spooled_id"),
                                  "depth": payload.get("depth"),
                                  "received_ms": self._wall_ms()}
            self._spool.move_to_end(robot)
            self._cap(self._spool)
        return True

    def snapshot(self) -> Dict[str, Any]:
        """For the stats: the ingest_state and spool_state per robot, the tracked bundles."""
        with self._lock:
            return {
                "ingest_state": {r: {"run_id": s.run_id, "highest_complete_id": s.highest}
                                 for r, s in self._states.items()},
                "spool_state": {r: dict(v) for r, v in self._spool.items()},
                "ack_trackers": len(self._entries),
            }

    def backlog(self, robot: Any, run_id: Optional[str] = None) -> Dict[str, Any]:
        """What the robot still has to upload, for the API's Finish gate (read-only).

        `depth` is the robot's own spool_state.depth (entries not yet complete or terminally
        rejected). It counts only for the robot's current run: `run_id` when given, else the
        latest run cloud-side (the latest accepted node's run, else the ingest_state's), else
        the spool_state's own; a spool_state run no accepted node carries yet (a restarted
        robot that already spooled) is the current run, one seen earlier but not the latest is
        stale. A spool_state of another run, or without an integer depth,
        is `known: false` (the gate cannot say; `pending` is false). `highest_complete_id` and
        `open_trackers` (bundles of that run waiting for parts) are liveness signals for the
        caller, not part of the gate."""
        with self._lock:
            runs = self._runs.get(robot) or []
            state = self._states.get(robot)
            spool = self._spool.get(robot)
            if run_id is None:
                spool_run = spool.get("run_id") if spool is not None else None
                if spool_run is not None and spool_run not in runs:
                    # a run no accepted node carries yet (the robot restarted and spooled): the
                    # spool's own run is current. One seen but not the latest is stale.
                    run_id = spool_run
                else:
                    run_id = runs[-1] if runs else (state.run_id if state is not None else None)
                    if run_id is None:
                        run_id = spool_run
            depth = spool.get("depth") if spool is not None else None
            known = (spool is not None and spool.get("run_id") == run_id
                     and isinstance(depth, int) and not isinstance(depth, bool) and depth >= 0)
            now_ms = self._wall_ms()
            return {
                "robot_name": robot,
                "run_id": run_id,
                "known": known,
                "pending": bool(known and depth > 0),
                "depth": depth if known else None,
                "last_spooled_id": spool.get("last_spooled_id") if known else None,
                "highest_complete_id": (state.highest if state is not None
                                        and state.run_id == run_id else None),
                "open_trackers": sum(1 for k in self._entries if k[0] == robot and k[1] == run_id),
                "spool_age_s": (round(max(0, now_ms - spool["received_ms"]) / 1000.0, 1)
                                if spool is not None else None),
            }

    # --- internals (call under the lock) --------------------------------------------------

    def _evict(self) -> None:
        while len(self._entries) > self.max_trackers:
            self._entries.popitem(last=False)
            self.stats["ack_trackers_evicted"] += 1

    def _note_run(self, robot: Any, run_id: Optional[str]) -> None:
        """A node was accepted: its run becomes the latest unless it was seen before."""
        if not valid_topic_level(robot):
            return
        runs = self._runs.setdefault(robot, [])
        if run_id not in runs:
            runs.append(run_id)
            del runs[:-_RUNS_SEEN_CAP]
        self._runs.move_to_end(robot)
        self._cap(self._runs)

    def _cap_modes(self) -> None:
        """Bound `_modes`, evicting unknown / True entries before a False one: forgetting a False
        would leave a stale retained `false` on the broker (silent loss of a mapping run)."""
        while len(self._modes) > _ROBOT_CAP:
            victim = next((r for r, v in self._modes.items() if v is not False),
                          next(iter(self._modes)))
            del self._modes[victim]

    @staticmethod
    def _cap(table: "OrderedDict") -> None:
        while len(table) > _ROBOT_CAP:
            table.popitem(last=False)

    def _evaluate(self, out, key, entry: _Entry) -> None:
        """An accepted bundle changed: send its final ack when no declared part is pending."""
        if any(p not in entry.stored and p not in entry.missing for p in entry.declared):
            return
        self._final(out, key, entry)

    def _final(self, out, key, entry: _Entry) -> None:
        robot, run_id, sid = key
        if not entry.missing:
            self._ack(out, robot, run_id, sid, entry.status, None, RESEND_NONE,
                      entry.declared, (), True, entry.session_id)
            del self._entries[key]
            self._complete(out, robot, run_id, sid)
            return
        missing = list(entry.missing)
        terminal = [r for r in entry.missing.values() if r in TERMINAL_REASONS]
        if terminal:  # the bundle is refused for good
            self._ack(out, robot, run_id, sid, REJECTED, terminal[0], RESEND_NONE,
                      entry.stored, missing, False, entry.session_id)
            del self._entries[key]
            return
        self._ack(out, robot, run_id, sid, entry.status, next(iter(entry.missing.values())),
                  RESEND_PARTS, entry.stored, missing, False, entry.session_id)
        entry.missing.clear()  # the resent parts are pending again
        entry.since = self._clock()

    def _ack(self, out, robot, run_id, sid, status, reason, resend, stored, missing, complete,
             session_id) -> None:
        if not valid_topic_level(robot):
            self.stats["acks_invalid_robot"] += 1
            logger.warning("Not acking a node of robot %r: not a topic level", robot)
            return
        body = {"v": 1, "robot_name": robot, "run_id": run_id, "session_node_id": sid,
                "status": status, "reason": reason, "resend": resend,
                "parts_stored": _part_dicts(stored), "parts_missing": _part_dicts(missing),
                "complete": complete, "session_id": session_id}
        try:
            signature = json.dumps(body, sort_keys=True)
        except (TypeError, ValueError):
            self.stats["acks_failed"] += 1
            return
        now = self._clock()
        key = (robot, run_id, sid)
        last = self._last_ack.get(key)
        if last is not None and last[0] == signature and now - last[1] < ACK_COALESCE_S:
            self.stats["acks_coalesced"] += 1
            return
        self._last_ack[key] = (signature, now)
        self._last_ack.move_to_end(key)
        while len(self._last_ack) > _LAST_ACK_CAP:
            self._last_ack.popitem(last=False)
        body["ts"] = self._wall_ms()
        out.append((ack_topic(robot), json.dumps(body), False))

    def _complete(self, out, robot, run_id, sid) -> None:
        """A bundle is complete: advance the robot's high-water mark."""
        if isinstance(sid, bool) or not isinstance(sid, int) or not valid_topic_level(robot):
            return
        latest = self._runs.get(robot)
        if latest and latest[-1] != run_id:  # a superseded run: acked, but no state
            return
        state = self._states.get(robot)
        if state is None or state.run_id != run_id:  # first, or a new run: restart
            state = self._states[robot] = _State(run_id, sid, dirty=True)
            self._cap(self._states)
        elif sid > state.highest:
            state.highest = sid
            state.dirty = True
        else:
            return
        self._maybe_state(out, robot, state, self._clock())

    def _maybe_state(self, out, robot, state: _State, now: float) -> None:
        if not state.dirty or now - state.published_at < self.state_min_interval_s:
            return
        state.dirty = False
        state.published_at = now
        out.append((state_topic(robot), json.dumps(
            {"v": 1, "robot_name": robot, "run_id": state.run_id,
             "highest_complete_id": state.highest, "ts": self._wall_ms()}), True))

    # --- publishing (outside the lock) ----------------------------------------------------

    def _send(self, out: List[Tuple[str, str, bool]]) -> List[bool]:
        """Hand the messages to MQTT (QoS 1). Never raises; failures are counted. Returns, per
        message, whether MQTT accepted it (rc 0, or NO_CONN where paho queues it)."""
        accepted: List[bool] = []
        for topic, body, retain in out:
            accepted.append(False)
            try:
                rc = self._publish(topic, body, 1, retain)
            except Exception as exc:  # noqa: BLE001 - an ack must never break ingest
                self.stats["acks_failed"] += 1
                logger.warning("Publishing to %s failed: %s", topic, exc)
                continue
            if rc == _MQTT_ERR_QUEUE_SIZE or rc is None:
                self.stats["acks_dropped"] += 1
                (logger.debug if rc is None else logger.warning)(
                    "Dropped a message for %s: %s", topic,
                    "no MQTT client" if rc is None else "outgoing queue full")
            elif rc not in (0, _MQTT_ERR_NO_CONN):  # NO_CONN: queued until the reconnect
                self.stats["acks_failed"] += 1
                logger.warning("Publishing to %s returned rc=%s", topic, rc)
            elif retain:
                accepted[-1] = True
                self.stats["ingest_mode_published" if topic.endswith("/ingest_mode")
                           else "ingest_state_published"] += 1
            else:
                accepted[-1] = True
                self.stats["acks_sent"] += 1
        return accepted
