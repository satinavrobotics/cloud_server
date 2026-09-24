"""
Goal-following simulation for the dummy robot (``--mode goal``).

Pure logic, no MQTT: the robot feeds it VDA5050 orders / instant actions and calls
``step(dt)`` once per tick; ``to_state()`` renders the VDA5050 state it should
publish. Time only advances through ``step(dt)``, so tests drive it tick by tick.

What the mission dispatcher (packages/controllers/mission/server.py) reads from the
state, and what this module therefore guarantees:

* ``orderId`` echoes the accepted order; the dispatcher only processes a state whose
  orderId carries the current mission's prefix (``_on_client_message``).
* ``lastNodeId`` / ``lastNodeSequenceId`` name the last node *reached*, and always the
  same node (``update_mission_node_state`` cross-checks the "-s{seq}" suffix). A route
  mission node is COMPLETED when lastNodeSequenceId == 2 * len(waypoints); a move
  node when it is 2. The first node of a new order (the robot's own pose, seq 0) is
  treated as reached on acceptance, as VDA5050 requires it to be trivially reachable.
* ``actionStates``: an action mission node is COMPLETED when ``actionStates[0]`` is
  FINISHED (FAILED fails it), so order actions are listed first and in order, and are
  present from the moment the order is accepted (an empty list would IndexError the
  dispatcher). Instant-action states (cancelOrder, ...) are appended after them:
  ``handle_instant_action`` scans actionStates from the end and stops at the first
  non-instant actionType, so they must stay at the tail.
* cancelOrder: FINISHED once the robot has stopped and dropped the order (orderId is
  kept, as VDA5050 prescribes); FAILED ("noOrderToCancel") if there was nothing to
  cancel, which the dispatcher also treats as a completed cancel. Resends of the same
  actionId are not re-executed.
"""

import math
from typing import Dict, List

from packages.controllers.mission.vda5050_types import vda5050_types as types

Status = types.VDA5050ActionStatus

# Instant actions this robot acknowledges. Anything else is ignored rather than
# reported: an unknown actionType at the tail of actionStates would stop the
# dispatcher's reverse scan before it reached a cancelOrder state.
_ACKED_INSTANT_ACTIONS = set(types.VDA5050InstantActionType.values() +
                             types.NVInstantActionType.values())


class GoalFollower:
    def __init__(self, x: float = 0.0, y: float = 0.0, theta: float = 0.0,
                 speed: float = 1.0, action_duration: float = 1.0,
                 goal_tolerance: float = 0.05, map_id: str = "default"):
        self.x = x
        self.y = y
        self.theta = theta
        self.speed = speed
        self.action_duration = action_duration
        self.goal_tolerance = goal_tolerance
        self.map_id = map_id

        self.order_id = ""
        self.order_update_id = 0
        self.last_node_id = ""
        self.last_node_sequence_id = 0
        self.driving = False
        # Nodes/edges not yet traversed, in sequence order (released and horizon).
        self.pending_nodes: List[types.VDA5050Node] = []
        self.pending_edges: List[types.VDA5050Edge] = []
        # Order action states, in order; keyed separately for updates.
        self.action_states: List[types.VDA5050ActionState] = []
        # Node actions waiting to run / running, in execution order (actionIds). All of
        # them block driving, whatever their blockingType (minimal simulation).
        self._action_queue: List[str] = []
        self._action_elapsed = 0.0
        # Edge actions that run while the robot drives the edge (actionIds by edge).
        self._edge_actions: Dict[str, List[str]] = {}
        self.instant_action_states: List[types.VDA5050ActionState] = []
        # Set when a factsheetRequest arrives; the owner publishes and clears it.
        self.factsheet_requested = False

    # ------------------------------------------------------------------ orders

    @property
    def has_active_order(self) -> bool:
        """True while the order has nodes left (released or horizon) or unfinished actions."""
        return bool(self.pending_nodes) or \
            any(not s.actionStatus.done for s in self.action_states)

    @property
    def order_finished(self) -> bool:
        return bool(self.order_id) and not self.has_active_order

    def handle_order(self, order: types.VDA5050Order) -> str:
        """Accept a VDA5050 order. Returns 'new', 'update', 'duplicate' or 'rejected'."""
        if order.orderId == self.order_id:
            if order.orderUpdateId == self.order_update_id:
                return "duplicate"  # a resend of what we already run
            if order.orderUpdateId < self.order_update_id:
                return "rejected"   # stale update
            self._apply_update(order)
            return "update"
        self._start_new_order(order)
        return "new"

    def _start_new_order(self, order: types.VDA5050Order):
        # A different orderId preempts whatever was running (a real robot would
        # reject it while busy; the dummy prefers to follow the dispatcher).
        self.order_id = order.orderId
        self.order_update_id = order.orderUpdateId
        self.pending_nodes = sorted(order.nodes, key=lambda n: n.sequenceId)
        self.pending_edges = sorted(order.edges, key=lambda e: e.sequenceId)
        self.action_states = []
        self._action_queue = []
        self._action_elapsed = 0.0
        self._edge_actions = {}
        self.instant_action_states = []
        self._register_actions(self.pending_nodes, self.pending_edges)
        self.driving = False
        # The first node is where the robot already is: reached on acceptance.
        first = self.pending_nodes[0]
        if first.released:
            self._reach_node(first, move_to=False)

    def _apply_update(self, order: types.VDA5050Order):
        self.order_update_id = order.orderUpdateId
        # Everything up to the last node reached is history; the rest replaces the
        # previous horizon/base.
        self.pending_nodes = sorted(
            (n for n in order.nodes if n.sequenceId > self.last_node_sequence_id),
            key=lambda n: n.sequenceId)
        self.pending_edges = sorted(
            (e for e in order.edges if e.sequenceId > self.last_node_sequence_id),
            key=lambda e: e.sequenceId)
        self._register_actions(self.pending_nodes, self.pending_edges)

    def _register_actions(self, nodes: List[types.VDA5050Node],
                          edges: List[types.VDA5050Edge]):
        known = {s.actionId for s in self.action_states}
        for element in list(nodes) + list(edges):
            for action in element.actions:
                if action.actionId in known:
                    continue
                known.add(action.actionId)
                self.action_states.append(types.VDA5050ActionState(
                    actionId=action.actionId, actionType=action.actionType,
                    actionDescription=action.actionDescription,
                    actionStatus=Status.WAITING))
            if isinstance(element, types.VDA5050Edge) and element.actions:
                self._edge_actions[element.edgeId] = [a.actionId for a in element.actions]

    # ---------------------------------------------------------- instant actions

    def handle_instant_actions(self, actions: List[types.VDA5050Action]):
        for action in actions:
            if action.actionType not in _ACKED_INSTANT_ACTIONS:
                continue
            if any(s.actionId == action.actionId for s in self.instant_action_states):
                continue  # dispatcher resend of an action we already handled
            status, result = Status.FINISHED, ""
            if action.actionType == types.VDA5050InstantActionType.CANCEL_ORDER:
                if self.has_active_order:
                    self._cancel_order()
                else:
                    status, result = Status.FAILED, "noOrderToCancel"
            elif action.actionType == types.VDA5050InstantActionType.FACTSHEET_REQUEST:
                self.factsheet_requested = True
            self.instant_action_states.append(types.VDA5050ActionState(
                actionId=action.actionId, actionType=action.actionType,
                actionStatus=status, resultDescription=result))

    def _cancel_order(self):
        self.pending_nodes = []
        self.pending_edges = []
        self._action_queue = []
        self._action_elapsed = 0.0
        for state in self.action_states:
            if not state.actionStatus.done:
                state.actionStatus = Status.FAILED
                state.resultDescription = "cancelled"
        self.driving = False

    # -------------------------------------------------------------- simulation

    def _state_of(self, action_id: str) -> types.VDA5050ActionState:
        return next(s for s in self.action_states if s.actionId == action_id)

    def _reach_node(self, node: types.VDA5050Node, move_to: bool = True):
        if move_to and node.nodePosition is not None:
            self.x, self.y = node.nodePosition.x, node.nodePosition.y
            self.theta = node.nodePosition.theta
        self.last_node_id = node.nodeId
        self.last_node_sequence_id = node.sequenceId
        self.pending_nodes = [n for n in self.pending_nodes if n.sequenceId > node.sequenceId]
        for edge in self.pending_edges:
            if edge.sequenceId < node.sequenceId:
                self._finish_edge_actions(edge)
        self.pending_edges = [e for e in self.pending_edges if e.sequenceId > node.sequenceId]
        self._action_queue += [a.actionId for a in node.actions
                               if not self._state_of(a.actionId).actionStatus.done]

    def _finish_edge_actions(self, edge: types.VDA5050Edge):
        for action_id in self._edge_actions.pop(edge.edgeId, []):
            state = self._state_of(action_id)
            if not state.actionStatus.done:
                state.actionStatus = Status.FINISHED

    def _step_actions(self, dt: float) -> bool:
        """Advance the head node action. Returns True while node actions block driving."""
        if not self._action_queue:
            return False
        state = self._state_of(self._action_queue[0])
        if state.actionStatus == Status.WAITING:
            state.actionStatus = Status.RUNNING
            self._action_elapsed = 0.0
        else:
            self._action_elapsed += dt
            if self._action_elapsed >= self.action_duration:
                state.actionStatus = Status.FINISHED
                self._action_queue.pop(0)
        return True

    def step(self, dt: float):
        """Advance the simulation by dt seconds (at most one node reached per step)."""
        if self._step_actions(dt):
            self.driving = False
            return
        target = self.pending_nodes[0] if self.pending_nodes else None
        if target is None or not target.released:
            self.driving = False  # order done, or waiting at the end of the base
            return
        for edge in self.pending_edges:
            if edge.sequenceId < target.sequenceId:
                for action_id in self._edge_actions.get(edge.edgeId, []):
                    state = self._state_of(action_id)
                    if state.actionStatus == Status.WAITING:
                        state.actionStatus = Status.RUNNING
        if target.nodePosition is None:
            self._reach_node(target)
            self.driving = False
            return
        dx = target.nodePosition.x - self.x
        dy = target.nodePosition.y - self.y
        dist = math.hypot(dx, dy)
        travel = self.speed * dt
        if dist <= self.goal_tolerance or travel >= dist:
            self._reach_node(target)
            self.driving = False
            return
        self.theta = math.atan2(dy, dx)
        self.x += dx / dist * travel
        self.y += dy / dist * travel
        self.driving = True

    # ------------------------------------------------------------------ output

    @property
    def nav_reasoning(self) -> str:
        if self._action_queue:
            return f"Executing action {self._state_of(self._action_queue[0]).actionType}"
        if self.pending_nodes and self.pending_nodes[0].released:
            return f"Heading to node {self.pending_nodes[0].nodeId}"
        if self.order_id and self.pending_nodes:
            return "Waiting for the order's horizon to be released"
        if self.order_id:
            return f"Order {self.order_id} finished"
        return "Idle, waiting for an order"

    def to_state(self, header_id: int, timestamp: str = "", **extra) -> types.VDA5050State:
        """The VDA5050 state for the current simulation state; ``extra`` supplies the
        fields this module does not own (manufacturer, batteryState, information...)."""
        velocity = self.speed if self.driving else 0.0
        fields = dict(
            headerId=header_id,
            timestamp=timestamp,
            orderId=self.order_id,
            orderUpdateId=self.order_update_id,
            lastNodeId=self.last_node_id,
            lastNodeSequenceId=self.last_node_sequence_id,
            nodeStates=[n.to_node_state() for n in self.pending_nodes],
            edgeStates=[e.to_edge_state() for e in self.pending_edges],
            actionStates=[s.copy() for s in self.action_states + self.instant_action_states],
            driving=self.driving,
            agvPosition=types.VDA5050AgvPosition(
                positionInitialized=True, x=self.x, y=self.y, theta=self.theta,
                mapId=self.map_id),
            velocity=types.VDA5050Velocity(
                vx=velocity * math.cos(self.theta), vy=velocity * math.sin(self.theta),
                omega=0.0),
            batteryState=None,
        )
        fields.update(extra)
        return types.VDA5050State(**fields)
