"""Leg detection from a robot's VDA5050 state messages (one tracker per run).

A leg is one move from one reached node to the next: it ends when the robot reports a new
lastNodeId with a sequence id above 0 on a route node of the current order, and starts when
the previous one was reported. Everything is stamped with the robot's header time.

Node ids are "{prefix}-n{tree node}-s{seq}" (order_ids): sequence 0 is the padding node at the
robot's position when the order starts, sequence 2k+2 is waypoint k of the route node.

- Sequence 0 never ends a leg. It only starts one (the first of a pass or of a non-route
  detour) and otherwise keeps the open leg: a reroute resends the order under a new revision
  and the robot reports the new base node again, which must not cut or double the leg that was
  interrupted. A base node of the *next* route node of the tree (a new order, same pass)
  restarts the leg's clock but keeps where it started.
- The same lastNodeId twice is one reach.
- A new pass starts from scratch (new run id, so new node ids).
- A node the robot reported before this tracker saw the order (a dispatcher restart) only
  becomes the start of the next leg.

Topomap node ids: planner go-tos have exactly one waypoint per planned_path node, in order
(mission_planner `waypoints[i]` <-> `path[i]`), so waypoint i of the concatenated route nodes
is planned_path[i] when the two have the same length. Any other mission, or a mission whose
route was rewritten (planned_path cleared), has no topomap ids: its legs are recorded with VDA
ids only.
"""
import dataclasses
import datetime
import math
from typing import Any, Dict, List, Optional, Tuple

from packages.controllers.mission import order_ids
from packages.utils import run_legs

# One state message interval is only counted as stopped time up to this long (a silent robot
# is not "stopped").
MAX_INTERVAL_S = 30.0


@dataclasses.dataclass
class Reach:
    """A node the robot reported (or the implied start of a pass)."""
    vda_node: str
    seq: int
    node_idx: int
    ts: datetime.datetime
    received: datetime.datetime
    order_rev: int
    pose: Optional[Any] = None           # waypoint Pose2D, map frame; None for a base node
    wp: Optional[int] = None             # waypoint index inside its route node
    topomap: Optional[str] = None
    stopped_s: float = 0.0               # non-driving time since this node was reached
    virtual: bool = False


@dataclasses.dataclass
class Leg:
    seq: int
    pass_index: int
    order_rev: int
    from_vda_node: Optional[str]
    to_vda_node: str
    from_topomap_node: Optional[str]
    to_topomap_node: Optional[str]
    map_id: Optional[str]
    started_at: datetime.datetime
    ended_at: datetime.datetime
    received_started_at: datetime.datetime
    received_ended_at: datetime.datetime
    duration_s: float
    stopped_s: float
    straight_m: Optional[float]
    planned_m: Optional[float]
    expected_s: Optional[float]


def _dist(a: Any, b: Any) -> float:
    return math.hypot(b.x - a.x, b.y - a.y)


def route_table(mission: Any) -> Tuple[Dict[int, Tuple[int, List[Any]]], Optional[List[str]]]:
    """({tree index: (offset into the concatenated route, waypoints)}, planned_path or None
    when it does not line up with the waypoints)."""
    table: Dict[int, Tuple[int, List[Any]]] = {}
    offset = 0
    for idx, node in enumerate(mission.mission_tree):
        route = getattr(node, "route", None)
        if route is not None:       # (node.type rebuilds a dict on every call)
            table[idx] = (offset, list(route.waypoints))
            offset += len(route.waypoints)
    planned = list(mission.planned_path or [])
    return table, (planned if planned and len(planned) == offset else None)


class LegTracker:
    def __init__(self) -> None:
        self.n_legs = 0
        self.pass_index: Optional[int] = None
        self.anchor: Optional[Reach] = None
        self.last_node: Optional[str] = None
        self._prev: Optional[Tuple[datetime.datetime, bool]] = None   # (ts, driving)

    @property
    def current_seq(self) -> Optional[int]:
        """Number of the leg in progress (1-based, relative to this tracker)."""
        return self.n_legs + 1 if self.anchor is not None else None

    def _reset(self) -> None:
        self.anchor = None
        self.last_node = None
        self._prev = None

    def observe(self, *, message: Any, mission: Any, ts: datetime.datetime,
                received: datetime.datetime, limits: Any = None,
                default_map: Optional[str] = None) -> List[Leg]:
        """One state message of the run's mission; the legs it completes (usually none)."""
        status = mission.status
        if self.pass_index != status.passes_completed:
            self._reset()
            self.pass_index = status.passes_completed
        prefix = order_ids.run_prefix(str(mission.name), status.run_id, status.order_rev)

        # Time the robot spent not driving since the previous message, on the open leg.
        if self._prev is not None and self.anchor is not None:
            dt = (ts - self._prev[0]).total_seconds()
            if 0 < dt and not self._prev[1]:
                self.anchor.stopped_s += min(dt, MAX_INTERVAL_S)
        driving = getattr(message, "driving", None)
        self._prev = (ts, True if driving is None else bool(driving))

        node_id = message.lastNodeId or ""
        if not order_ids.is_node_of(prefix, node_id):
            # Not progress in this order yet; the order's echo marks where the pass began.
            order_id = message.orderId or ""
            if self.anchor is None and order_ids.is_order_of(prefix, order_id):
                idx = order_ids.order_node_index(order_id)
                if idx in route_table(mission)[0]:
                    self.anchor = Reach(f"{prefix}-n{idx}-s0", 0, idx, ts, received,
                                        status.order_rev, virtual=True)
            return []
        if node_id == self.last_node:
            return []
        self.last_node = node_id

        table, planned = route_table(mission)
        idx, seq = order_ids.node_index(node_id), order_ids.node_sequence(node_id)
        if idx not in table:
            self.anchor = None         # an action or a move: the next route starts afresh
            return []
        if seq is None:
            return []
        if seq == 0:
            anchor = self.anchor
            if anchor is None or anchor.virtual:
                self.anchor = Reach(node_id, 0, idx, ts, received, status.order_rev)
            elif anchor.node_idx != idx:
                # The next route node of the tree: the robot is at the node it just reached.
                self.anchor = dataclasses.replace(anchor, ts=ts, received=received,
                                                  stopped_s=0.0, node_idx=idx)
            return []

        offset, waypoints = table[idx]
        wp = seq // 2 - 1
        if not 0 <= wp < len(waypoints):
            return []
        reach = Reach(node_id, seq, idx, ts, received, status.order_rev, pose=waypoints[wp],
                      wp=wp, topomap=planned[offset + wp] if planned else None)
        anchor, self.anchor = self.anchor, reach
        if anchor is None:
            return []
        return [self._leg(anchor, reach, limits, default_map or waypoints[wp].map_id or None)]

    def _leg(self, start: Reach, end: Reach, limits: Any, map_id: Optional[str]) -> Leg:
        self.n_legs += 1
        straight = planned = heading = None
        if start.pose is not None and end.pose is not None:
            straight = _dist(start.pose, end.pose)
            planned = straight
            heading = end.pose.theta - start.pose.theta
        expected = run_legs.expected_seconds(
            planned, heading, getattr(limits, "speed_max", None),
            getattr(limits, "acceleration_max", None),
            getattr(limits, "angular_speed_max", None)) if planned is not None else None
        duration = max(0.0, (end.ts - start.ts).total_seconds())
        return Leg(
            seq=self.n_legs, pass_index=self.pass_index or 0, order_rev=end.order_rev,
            from_vda_node=start.vda_node, to_vda_node=end.vda_node,
            from_topomap_node=start.topomap, to_topomap_node=end.topomap, map_id=map_id,
            started_at=start.ts, ended_at=end.ts, received_started_at=start.received,
            received_ended_at=end.received, duration_s=round(duration, 3),
            stopped_s=round(min(start.stopped_s + 0.0, duration), 3),
            straight_m=None if straight is None else round(straight, 3),
            planned_m=None if planned is None else round(planned, 3),
            expected_s=expected)
