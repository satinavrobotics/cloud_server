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

Plan of a leg (utils/plan_length.py): the leg between waypoints k-1 and k of ONE route node has the planner's
trajectory entry (k-1, k) of that node when it has one; `planned_m` is its polyline length when the entry's ends are
the two poses the robot was sent to (within EFFICIENCY_STALE_TOL_M; `plan_source` "trajectory"), the straight line
when they are not (a reroute: the leg starts at a waypoint of the old route; "stale") and the straight line when
there is no entry (first leg of an order, across route nodes, no trajectory; "straight"). A leg without a straight
line (it starts at the robot's own position) has no plan: both NULL. `predicted_s` is the trajectory entry's own (the planner's
region cost model, docs/planner_region_stats_2026-10-27.md 7), copied the way `planned_m` is: only from an entry that matches
(`plan_source` "trajectory"); `cost_model` is then the route node's. Both NULL otherwise. `from_chart` / `to_chart` are the
`Pose2D.chart_key` of the two waypoints and `graph_version` the route node's (None for a base node, a route
without them).

Time of a leg (stage C of the edge statistics). `duration_s` is end.ts - start.ts, split by the robot's own reports:

- `stopped_s`: the intervals between two state messages that follow a message with `driving` false; one interval
  counts at most MAX_INTERVAL_S (a silent robot is not "stopped"). `driving` missing (None) counts as driving.
- `silent_s`: the part of every interval (whatever the previous message said about driving) above MAX_INTERVAL_S,
  i.e. the time the tracker knows nothing about (no messages: a link or dispatcher gap). max(0, dt - MAX_INTERVAL_S)
  per interval, so an interval is split into at most MAX_INTERVAL_S of stopped (if not driving) and the rest silent.
- `moving_s` = max(0, duration_s - stopped_s - silent_s): the time with messages that said the robot was driving.
- Recovery time (run_legs.recovery_s, from the NAV.RECOVERY_EXITED events, filled at run end) is NOT subtracted: it
  overlaps stopped time and a recovery maneuver may report driving true, so subtracting it would count it twice.
Both stopped_s and silent_s restart with the leg (the anchor moving to the next route node of the tree resets them).
"""
import dataclasses
import datetime
import math
from typing import Any, Dict, List, Optional, Tuple

from packages import config
from packages.controllers.mission import order_ids
from packages.utils import plan_length, run_legs

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
    chart: Optional[str] = None          # the waypoint's chart_key (None for a base node)
    stopped_s: float = 0.0               # non-driving time since this node was reached
    silent_s: float = 0.0                # time in gaps above MAX_INTERVAL_S since this node was reached
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
    plan_source: Optional[str] = None    # plan_length.SOURCES; None: no straight line, so no plan
    from_chart: Optional[str] = None
    to_chart: Optional[str] = None
    graph_version: Optional[int] = None
    predicted_s: Optional[float] = None  # the plan's region-cost prediction for the leg (trajectory entry, plan_source "trajectory")
    cost_model: Optional[str] = None     # `model_hash:stamp` behind predicted_s
    silent_s: float = 0.0                # see the module docstring: moving_s = duration - stopped - silent
    moving_s: Optional[float] = None


def _dist(a: Any, b: Any) -> float:
    return math.hypot(b.x - a.x, b.y - a.y)


@dataclasses.dataclass
class RouteInfo:
    """One route node of the mission's tree, as the tracker needs it."""
    offset: int                                         # of its first waypoint in the concatenated route
    waypoints: List[Any]
    legs: Dict[Tuple[int, int], List[List[float]]]      # plan trajectory entries by (from, to) waypoint index
    graph_version: Optional[int] = None
    predicted: Dict[Tuple[int, int], float] = dataclasses.field(default_factory=dict)   # trajectory entries' predicted_s
    cost_model: Optional[str] = None


def route_table(mission: Any) -> Tuple[Dict[int, RouteInfo], Optional[List[str]]]:
    """({tree index: RouteInfo}, planned_path or None when it does not line up with the waypoints)."""
    table: Dict[int, RouteInfo] = {}
    offset = 0
    for idx, node in enumerate(mission.mission_tree):
        route = getattr(node, "route", None)
        if route is not None:       # (node.type rebuilds a dict on every call)
            legs = {(t.from_index, t.to_index): t.points for t in route.trajectory or []}
            predicted = {(t.from_index, t.to_index): t.predicted_s for t in route.trajectory or []
                         if getattr(t, "predicted_s", None) is not None}
            table[idx] = RouteInfo(offset, list(route.waypoints), legs,
                                   getattr(route, "graph_version", None), predicted, getattr(route, "cost_model", None))
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
        self._routes: Optional[Tuple[Tuple[int, int], Tuple[Dict[int, RouteInfo], Optional[List[str]]]]] = None

    def _route_table(self, mission: Any, order_rev: int) -> Tuple[Dict[int, RouteInfo], Optional[List[str]]]:
        """route_table(mission), rebuilt only when the order or the route revision changed."""
        key = (order_rev, int(getattr(mission, "route_rev", 0) or 0))
        if self._routes is None or self._routes[0] != key:
            self._routes = (key, route_table(mission))
        return self._routes[1]

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
            if 0 < dt:
                if not self._prev[1]:
                    self.anchor.stopped_s += min(dt, MAX_INTERVAL_S)
                self.anchor.silent_s += max(0.0, dt - MAX_INTERVAL_S)
        driving = getattr(message, "driving", None)
        self._prev = (ts, True if driving is None else bool(driving))

        node_id = message.lastNodeId or ""
        if not order_ids.is_node_of(prefix, node_id):
            # Not progress in this order yet; the order's echo marks where the pass began.
            order_id = message.orderId or ""
            if self.anchor is None and order_ids.is_order_of(prefix, order_id):
                idx = order_ids.order_node_index(order_id)
                if idx in self._route_table(mission, status.order_rev)[0]:
                    self.anchor = Reach(f"{prefix}-n{idx}-s0", 0, idx, ts, received,
                                        status.order_rev, virtual=True)
            return []
        if node_id == self.last_node:
            return []
        self.last_node = node_id

        table, planned = self._route_table(mission, status.order_rev)
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
                                                  stopped_s=0.0, silent_s=0.0, node_idx=idx)
            return []

        info = table[idx]
        offset, waypoints = info.offset, info.waypoints
        wp = seq // 2 - 1
        if not 0 <= wp < len(waypoints):
            return []
        reach = Reach(node_id, seq, idx, ts, received, status.order_rev, pose=waypoints[wp],
                      wp=wp, topomap=planned[offset + wp] if planned else None,
                      chart=getattr(waypoints[wp], "chart_key", None))
        anchor, self.anchor = self.anchor, reach
        if anchor is None:
            return []
        return [self._leg(anchor, reach, limits, run_legs.leg_map_id(waypoints[wp].map_id, default_map), info)]

    @staticmethod
    def _plan(start: Reach, end: Reach, straight: Optional[float], route: Optional[RouteInfo]
              ) -> Tuple[Optional[float], Optional[str]]:
        """(planned_m, plan_source) of a leg: see the module docstring."""
        points, a, b = None, None, None
        if (route is not None and start.pose is not None and end.pose is not None and start.wp is not None
                and end.wp == start.wp + 1 and order_ids.node_index(start.vda_node) == end.node_idx):
            points = route.legs.get((start.wp, end.wp))
            a, b = (start.pose.x, start.pose.y), (end.pose.x, end.pose.y)
        return plan_length.plan_of(points, a, b, straight, config.EFFICIENCY_STALE_TOL_M)

    def _leg(self, start: Reach, end: Reach, limits: Any, map_id: Optional[str],
             route: Optional[RouteInfo] = None) -> Leg:
        self.n_legs += 1
        straight = heading = None
        if start.pose is not None and end.pose is not None:
            straight = _dist(start.pose, end.pose)
            heading = end.pose.theta - start.pose.theta
        planned, source = self._plan(start, end, straight, route)
        predicted = route.predicted.get((start.wp, end.wp)) if route is not None and source == plan_length.SOURCE_TRAJECTORY else None
        expected = run_legs.expected_seconds(
            planned, heading, getattr(limits, "speed_max", None),
            getattr(limits, "acceleration_max", None),
            getattr(limits, "angular_speed_max", None)) if planned is not None else None
        duration = max(0.0, (end.ts - start.ts).total_seconds())
        stopped = min(start.stopped_s, duration)
        silent = min(start.silent_s, duration)
        return Leg(
            seq=self.n_legs, pass_index=self.pass_index or 0, order_rev=end.order_rev,
            from_vda_node=start.vda_node, to_vda_node=end.vda_node,
            from_topomap_node=start.topomap, to_topomap_node=end.topomap, map_id=map_id,
            started_at=start.ts, ended_at=end.ts, received_started_at=start.received,
            received_ended_at=end.received, duration_s=round(duration, 3),
            stopped_s=round(stopped, 3), silent_s=round(silent, 3),
            moving_s=round(max(0.0, duration - stopped - silent), 3),
            straight_m=None if straight is None else round(straight, 3),
            planned_m=None if planned is None else round(planned, 3),
            expected_s=expected, plan_source=source, from_chart=start.chart, to_chart=end.chart,
            graph_version=route.graph_version if route is not None else None,
            predicted_s=None if predicted is None else round(float(predicted), 3),
            cost_model=route.cost_model if predicted is not None else None)
