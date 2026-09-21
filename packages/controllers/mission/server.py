"""
SPDX-FileCopyrightText: NVIDIA CORPORATION & AFFILIATES
Copyright (c) 2021-2024 NVIDIA CORPORATION & AFFILIATES. All rights reserved.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

SPDX-License-Identifier: Apache-2.0
"""

# This repository implements data types and logic specified in the VDA5050 protocol, which is
# specified here https://github.com/VDA5050/VDA5050/blob/main/VDA5050_EN.md
import asyncio
import datetime
import json
import logging
import re
import requests
import time
import uuid
import sys
from typing import Any, Dict, List, Optional, Set, Tuple, Union, cast
from collections import OrderedDict

import pydantic

from packages.utils.mqtt_client import MQTTClient
from packages.controllers.mission import behavior_tree
from packages.controllers.mission import order_ids
import packages.controllers.mission.vda5050_types as types
from packages.database.postgres import PostgresDatabase
from packages.utils import metrics
import cloud_common.objects as api_objects
import cloud_common.objects.mission as mission_object
import cloud_common.objects.robot as robot_object
from cloud_common.objects.detection_results import DetectedObject
from cloud_common.objects.mission import EDITABLE_SPEC_FIELDS

import importlib

module_name = "internal_packages.push_data.telemetry_sender"
try:
    importlib.util.find_spec(module_name)
except ModuleNotFoundError:
    module_name = "packages.utils.telemetry_sender"

module = importlib.import_module(module_name)
TelemetrySender = getattr(module, "TelemetrySender")

# How long to wait in seconds before trying to reconnect to the mqtt broker
MQTT_RECONNECT_PERIOD = 0.5
# How long to wait in seconds before trying to reconnect to the mission database
DATABASE_RECONNECT_PERIOD = 0.5

class WaitElapsed(pydantic.BaseModel):
    """Posted to a robot's own message queue when a "wait" action node's timer runs out.

    Going through the queue (rather than letting the timer task touch the mission
    itself) keeps every mission mutation on the one message loop, in order with the
    robot's state messages. `key` ties it to the wait that started the timer, so a
    timer that outlived its mission (or its pass) is recognised and dropped."""
    key: Tuple[str, str, Optional[str], int, int]


RobotMessage = Union[api_objects.RobotObjectV1,
                     api_objects.MissionObjectV1,
                     types.VDA5050State,
                     types.VDA5050Factsheet,
                     types.RobotDatum,
                     WaitElapsed]


class ClientMessage(pydantic.BaseModel):
    name: str
    # TODO: perhaps do OOP to handle typing of payload too;
    # currently default is any
    payload: Any


class ClientStatusMessage(ClientMessage):
    name: str
    payload: types.VDA5050State


class ClientFactsheetMessage(ClientMessage):
    name: str
    payload: types.VDA5050Factsheet


class ClientDatumMessage(ClientMessage):
    name: str
    payload: types.RobotDatum


def vda5050_errors_to_status_dict(errors: List[types.VDA5050Error]) -> Dict[str, str]:
    """Mirror a VDA5050 state message's errors[] onto the RobotStatusV1.errors dict.

    Keyed by errorType (falling back to a positional key for the rare untyped
    error) so distinct errors don't collide under a single value. A plain
    snapshot, not an accumulating log: the robot re-sends its currently-active
    errors every state message, so the caller should overwrite status.errors
    with this each time rather than merge it — an error absent from a new
    message has genuinely cleared.
    """
    return {
        (error.errorType or f"error_{idx}"): error.errorDescription
        for idx, error in enumerate(errors)
    }


# VDA5050 error types that mean "this robot's mission can't make forward progress
# right now" — mirrors sati-client's utils/robotStatus.ts NAVIGATION_READINESS_ERROR_TYPES
# so client and server agree on the same definition of "nav ready".
NAVIGATION_READINESS_ERROR_TYPES = {"navigationNotReadyError", "poseHealthNotReadyError"}


class Robot:
    """Manages the mission state of a particular robot"""

    # An instant action the robot never reports FINISHED is resent on every state
    # message; give up after this many attempts. See handle_instant_action().
    MAX_INSTANT_ACTION_RESENDS = 20
    # Consecutive state messages whose orderId doesn't match the current mission
    # before we stop resending and fail the mission. See _on_client_message().
    MAX_ORDER_MISMATCHES = 40
    # How many finished mission names to remember for re-queue suppression. Only
    # needs to outlive the watcher echo of our own terminal write, so this is
    # generous; it exists so a long-lived robot doesn't grow the set unboundedly.
    MAX_FINISHED_MISSIONS_TRACKED = 256

    def __init__(self, name: str, db: PostgresDatabase, client: MQTTClient,
                 prefix: str, server: "RobotServer"):
        self._logger = logging.getLogger("Isaac Mission Dispatch")
        self._name = name
        self._mqtt_prefix = prefix
        self._messages: asyncio.Queue[RobotMessage] = asyncio.Queue()
        self._database = db
        self._robot_object: Optional[api_objects.RobotObjectV1] = None
        self._detection_results_object: Optional[api_objects.DetectionResultsObjectV1] = None
        # Try to get existing detected objects.
        # This will be awaited later or handled directly. In this legacy block, it was synchronous.
        # Since PostgresDatabase methods are async, we can't await in __init__.
        # For now, we initialize to None and will handle it appropriately.
        self._detection_results_object = None
        self._missions: OrderedDict[str,
                                    api_objects.MissionObjectV1] = OrderedDict()
        self._current_mission: Optional[api_objects.MissionObjectV1] = None
        self._current_instant_actions: OrderedDict[str,
                                                   types.VDA5050Action] = OrderedDict()
        # Resend attempts per outstanding instant action, so an action the robot
        # never reports FINISHED (e.g. it rejects cancelOrder with "no active order
        # running") is eventually abandoned instead of being resent on every state
        # message forever. See handle_instant_action().
        self._instant_action_resends: Dict[str, int] = {}
        # Consecutive robot-state messages carrying an orderId that isn't the current
        # mission's. Bounded in _on_client_message() so a robot that never adopts our
        # order fails the mission instead of spinning silently.
        self._order_mismatch_count: int = 0
        # Names of missions this controller has already run to a terminal state.
        # get_next_mission() drops a finished mission from _missions, but the object
        # stays ALIVE in the database, so our own terminal-status write echoes back
        # through the watcher -- and that echo can be a snapshot taken *before* the
        # terminal status landed, so _on_mission_change()'s state.done check sees
        # PENDING/RUNNING and re-queues a mission we already ran. Remembering what we
        # finished is the only check that doesn't depend on winning that race.
        # Insertion-ordered so the oldest entries can be evicted past
        # MAX_FINISHED_MISSIONS_TRACKED.
        self._finished_missions: "OrderedDict[str, None]" = OrderedDict()
        self._mqtt_client = client
        self._robot_online_task: Optional[asyncio.Task[Any]] = None
        self._mission_timeout_task: Optional[asyncio.Task[Any]] = None
        self._robot_server = server
        self._alive = True
        self._header_id = 0
        self._current_behavior_tree: Optional[behavior_tree.MissionBehaviorTree] = None
        # The timer of the "wait" action node that is currently running, and the
        # key that its WaitElapsed message will carry (see WaitElapsed).
        self._wait_task: Optional[asyncio.Task[Any]] = None
        self._wait_key: Optional[Tuple[str, str, Optional[str], int, int]] = None
        # Completion (mission name + run id + pass) whose then_run mission has been
        # created already, so a completion seen twice chains only once.
        self._chained_completion: Optional[str] = None
        # Missions whose spec edit was refused because they were already dispatched,
        # so the log says so once rather than on every status echo.
        self._ignored_spec_edits: Set[str] = set()
        self._updating_mission_from_api: bool = False
        self._charging_mission_received: bool = False
        self.last_node_seq_id: int = -1

        if self._robot_server.push_telemetry:
            self._telemetry = metrics.Telemetry()
            self._telemetry_client = TelemetrySender(
                self._robot_server.telemetry_env)
        # To calculate the durition of a robot state
        self._cur_robot_state_timestamp = datetime.datetime.now()
        asyncio.get_event_loop().create_task(self.run())

    async def _try_start_mission(self):
        # Schedule a new mission if we aren't doing anything and there is one in the queue
        if self._current_mission is None and self._missions:
            self._current_mission = next(iter(self._missions.values()))
            # Fresh mission, fresh mismatch budget -- the previous mission's leftover
            # count must not shorten this one's grace period.
            self._order_mismatch_count = 0

        # Cant start a new mission if there is no mission
        if self._current_mission is None:
            self.debug("Could not find a new mission to run")
            return
        # Cant start a new mission if there is no robot object
        if self._robot_object is None or \
                self._robot_object.lifecycle is not api_objects.object.ObjectLifecycleV1.ALIVE:
            return
        # Skip missions that were already canceled before they started
        if self._current_mission.needs_canceled:
            self.mission_info("Mission already flagged for cancel before dispatch — canceling immediately")
            self._set_mission_state(mission_object.MissionStateV1.CANCELED)
            await self.get_next_mission()
            return
        # Withhold dispatch while the robot can't actually receive an order — offline
        # or not navigation-ready. The mission stays PENDING; _on_client_message()
        # retries this once the robot's online/error status changes.
        hold_reason = self._dispatch_hold_reason()
        if hold_reason is not None:
            if not self._current_mission.status.held or \
                    self._current_mission.status.held_reason != hold_reason:
                self._current_mission.status.held = True
                self._current_mission.status.held_reason = hold_reason
                self.mission_info(f"Holding mission dispatch: {hold_reason}")
                asyncio.ensure_future(self._database.update_status(
                    api_objects.MissionObjectV1, self._current_mission.name,
                    self._current_mission.status, uuid.uuid4()))
            return
        if self._current_mission.status.held:
            self._current_mission.status.held = False
            self._current_mission.status.held_reason = None
            self.mission_info("Robot ready — releasing held mission")
            asyncio.ensure_future(self._database.update_status(
                api_objects.MissionObjectV1, self._current_mission.name,
                self._current_mission.status, uuid.uuid4()))
        # The run id must exist (and be persisted) before the first order goes out.
        if not await self._assign_run_id():
            return
        # Initialize behavior tree
        self._current_behavior_tree = behavior_tree.MissionBehaviorTree(
            self._current_mission)
        if not self._current_behavior_tree.create_behavior_tree():
            # In case the mission is not set correctly
            self._current_mission.status.failure_reason = \
                self._current_behavior_tree.failure_reason
            self._set_mission_state(mission_object.MissionStateV1.FAILED)
            await self.get_next_mission()
            return

        self.update_mission_from_behavior_tree()
        self._arm_mission_timeout()
        await self._send_order()

    def _dispatch_hold_reason(self) -> Optional[str]:
        """None if the robot can receive a dispatched order right now; otherwise a
        human-readable reason dispatch should be withheld."""
        if self._robot_object is None or not self._robot_object.status.online:
            return "Robot is offline"
        if NAVIGATION_READINESS_ERROR_TYPES & self._robot_object.status.errors.keys():
            return "Robot navigation is not ready"
        return None

    def _has_outstanding_cancel(self) -> bool:
        """True while a cancelOrder we sent has not yet been reported FINISHED (or
        abandoned) -- see handle_instant_action() for how entries leave the dict."""
        return any(a.actionType == types.VDA5050InstantActionType.CANCEL_ORDER
                   for a in self._current_instant_actions.values())

    async def _send_cancel_order(self, action_id: str):
        """Send a VDA5050 cancelOrder and track it in _current_instant_actions, so
        _has_outstanding_cancel() sees it and handle_instant_action() resends it
        until the robot reports it FINISHED (or it is abandoned)."""
        instant_action = types.VDA5050Action(
            actionType=types.VDA5050InstantActionType.CANCEL_ORDER,
            actionId=action_id)
        await self._send_instant_action(instant_action)
        self._current_instant_actions[action_id] = instant_action

    async def _handle_force_cancel(self, message: api_objects.RobotObjectV1):
        """Operator escape hatch, independent of mission tracking (see
        RobotSpecV1.needs_order_cancel's doc comment).

        Level-triggered on the flag rather than on its rising edge: the request may
        already be True the first time this dispatcher sees the robot (it was down
        or restarting when the operator asked -- exactly when the hatch is needed),
        and an edge check against _robot_object would then never fire nor clear it.
        The clear is persisted *before* sending so a stale still-True echo of the
        API's write can at worst cost a redundant clear, and one cancel at a time
        (same rule as the explicit-cancel path) keeps such echoes from minting a
        second cancelOrder while the first is outstanding."""
        if not message.needs_order_cancel:
            return
        message.needs_order_cancel = False
        await self._database.update_spec(
            api_objects.RobotObjectV1, message.name, message.spec, uuid.uuid4())
        if self._has_outstanding_cancel():
            self.info("Force-cancel requested, but a cancelOrder is already "
                      "outstanding; not sending another")
            return
        action_id = f"force-cancel-instantaction-n{self._header_id}"
        self.info(f"Force-cancel requested: sending {action_id}")
        await self._send_cancel_order(action_id)

    async def _send_instant_action(self, instant_action: types.VDA5050Action):
        instant_actions = types.VDA5050InstantActions(
            headerId=self._header_id,
            timestamp=datetime.datetime.now().isoformat(),
            instantActions=[instant_action])
        self._mqtt_client.publish(f"{self._mqtt_prefix}/{self._name}/instantActions",
                                  instant_actions.json())
        self._header_id += 1

    def _order_prefix(self) -> str:
        """Prefix of every order/node id generated for the current mission's run and
        revision (see order_ids)."""
        status = self._current_mission.status
        return order_ids.run_prefix(str(self._current_mission.name),
                                    status.run_id, status.order_rev)

    async def _persist_current_mission_status(self):
        await self._database.update_status(
            api_objects.MissionObjectV1, self._current_mission.name,
            self._current_mission.status, uuid.uuid4())

    async def _assign_run_id(self) -> bool:
        """Give a mission that is about to be dispatched for the first time its run
        id, and persist it *before* any order carrying it is sent.

        Awaited rather than fire-and-forget: if the dispatcher restarted between
        publishing the first order and the write landing, the mission would resume
        with no run_id and come back under different ids than the robot holds.

        A mission that already has a start_timestamp but no run_id was running before
        run_id existed; it keeps its legacy ids (see order_ids.run_prefix).

        Returns False if the id could not be persisted; nothing has been sent and
        the mission stays PENDING for the next attempt.
        """
        status = self._current_mission.status
        if status.run_id is not None or status.start_timestamp is not None:
            return True
        status.run_id = uuid.uuid4().hex[:8]
        try:
            await self._persist_current_mission_status()
        except Exception as err:  # pylint: disable=broad-except
            status.run_id = None
            self.warning(f"[{self._current_mission.name}] Could not persist run id "
                         f"({err}); not dispatching yet")
            return False
        self.mission_info(f"Run id {status.run_id}")
        return True

    async def _bump_order_rev(self) -> bool:
        """Move the current run to a new order revision, and persist it *before* the
        resend, so a cancelled node that is resent with new content goes out under a
        new orderId (the robot still holds the cancelled order's id in its state).

        A legacy mission (no run_id) keeps its ids. Returns False if the revision
        could not be persisted; the caller must not resend and should retry.
        """
        status = self._current_mission.status
        if status.run_id is None:
            return True
        status.order_rev += 1
        try:
            await self._persist_current_mission_status()
        except Exception as err:  # pylint: disable=broad-except
            status.order_rev -= 1
            self.warning(f"[{self._current_mission.name}] Could not persist order "
                         f"revision ({err}); not resending yet")
            return False
        self.mission_info(f"Order revision {status.order_rev}")
        return True

    async def _send_order(self):
        if self._robot_object is None or self._robot_object.lifecycle \
            not in [api_objects.object.ObjectLifecycleV1.ALIVE,
                    api_objects.object.ObjectLifecycleV1.PENDING_DELETE]:
            return
        if self._current_mission is None or self._current_behavior_tree is None:
            return

        if self._current_behavior_tree.current_node is None:
            self.mission_info("No available order to be sent")
            return
        if isinstance(self._current_behavior_tree.current_node,
                      behavior_tree.MissionLeafNode):
            idx = self._current_behavior_tree.current_node.idx
            mission_node = self._current_mission.mission_tree[idx]

            # Notify node does not send an order to robot, everything is handled in Dispatch
            if mission_node.type == mission_object.MissionNodeType.NOTIFY and \
                    mission_node.notify is not None:
                self._process_notify_node(mission_node)
                return

            # A wait is a timer the dispatcher runs itself; the robot has no order for it.
            if mission_node.type == mission_object.MissionNodeType.ACTION and \
                    mission_node.action is not None and \
                    mission_node.action.action_type == mission_object.WAIT_ACTION_TYPE:
                self._start_wait(mission_node)
                return

            if mission_node.type == mission_object.MissionNodeType.ROUTE and \
                    mission_node.route is not None:
                order = types.VDA5050Order.from_route(mission_node.route, self._robot_object,
                                                      self._order_prefix(), idx)
                self.mission_info("Sending mission route node "
                                  f"{mission_node.name}")

            elif mission_node.type == mission_object.MissionNodeType.MOVE and \
                    mission_node.move is not None:
                order = types.VDA5050Order.from_move(mission_node.move, self._robot_object,
                                                     self._order_prefix(), idx)
                self.mission_info("Sending mission move node "
                                  f"{mission_node.name}")

            elif mission_node.type == mission_object.MissionNodeType.ACTION and \
                    mission_node.action is not None:
                order = types.VDA5050Order.from_action(mission_node.action, self._robot_object,
                                                       self._order_prefix(), idx)
                self.mission_info("Sending mission action node "
                                  f"{mission_node.name}")

            order.headerId = self._header_id
            self._header_id += 1
            order.timestamp = datetime.datetime.now().isoformat()

            self._mqtt_client.publish(
                f"{self._mqtt_prefix}/{self._name}/order", order.json())
            self.set_mission_node_state(f"{mission_node.name}",
                                        mission_object.MissionStateV1.RUNNING)

    def _update_mission_from_api(self, mission: api_objects.MissionObjectV1,
                                 message: api_objects.MissionObjectV1) -> bool:
        cancel_current_node = False
        # From POST /mission/{name}/cancel endpoint
        if mission.needs_canceled != message.needs_canceled:
            self.info(
                f"Cancel a {mission.status.state} mission [{message.name}]")
            mission.needs_canceled = message.needs_canceled
            return cancel_current_node

        # From DELETE /mission/{name} endpoint
        if mission.lifecycle != message.lifecycle:
            self.info(
                f"{mission.status.state} mission lifecycle is changed to {message.lifecycle}")
            mission.lifecycle = message.lifecycle
            return cancel_current_node

        # From POST /mission/{name}/update endpoint
        if message.update_nodes:
            self.info(
                f"Update mission nodes: {list(message.update_nodes.keys())}")
            for node_name, route in message.update_nodes.items():
                for n in mission.mission_tree:
                    if n.name == node_name:
                        n.route = route
                        if mission.status.node_status[node_name].state is \
                                mission_object.MissionStateV1.RUNNING:
                            # Cancel current node
                            cancel_current_node = True
                        break
        return cancel_current_node

    async def _on_mission_change(self, message: api_objects.MissionObjectV1):
        # A mission being deleted that isn't in our queue is one we already ran (or
        # never had): forget it so its name can be reused by a genuinely new mission,
        # and stop -- a deleted object is not work to queue. Falling through here
        # would re-queue it on any echo whose status hadn't caught up yet, which is
        # the very re-dispatch this method exists to prevent. A delete for a mission
        # still in _missions is handled by delete_pending_mission() below.
        if message.lifecycle is not api_objects.object.ObjectLifecycleV1.ALIVE and \
                message.name not in self._missions:
            self._finished_missions.pop(message.name, None)
            return

        # If this is a new mission, add it to the queue
        if message.name not in self._missions:
            # A mission name reused after its previous run finished (deleted and
            # re-created, rather than left alone) must not be blocked by
            # _finished_missions below just because the delete's own lifecycle
            # change event hasn't reached us yet -- delete and re-create are two
            # independent writes the watcher delivers with no ordering guarantee
            # against each other (2026-09-15 field incident: an operator deleted
            # a completed mission and immediately re-created it under the same
            # name; the re-create's PENDING echo arrived first, got swallowed
            # here, and the mission sat PENDING forever with no error anywhere
            # -- deleting it *again* was the only fix, and only by luck of
            # timing). A message this fresh -- PENDING, never dispatched -- is
            # unambiguously a new mission, never the stale echo _finished_missions
            # exists to catch (that echo is always of a mission that reached
            # RUNNING at some point, so it always carries a start_timestamp; see
            # the true stale-echo case below, which this does not weaken).
            looks_freshly_created = (
                message.status.state == mission_object.MissionStateV1.PENDING and
                message.status.start_timestamp is None)
            if looks_freshly_created:
                self._finished_missions.pop(message.name, None)
            # Neither check below is redundant. _finished_missions covers what
            # *this* controller ran, without trusting the echoed status (see the
            # field's own declaration for why that status can lie -- this is the
            # true stale-echo case: our own completion write hasn't propagated
            # yet, so the echo still reports the mission RUNNING, not PENDING,
            # and so is never "freshly created" above); state.done covers
            # missions already terminal in the database that we never ran
            # ourselves, e.g. after a restart.
            if message.name in self._finished_missions:
                self.debug(f"Ignoring already-finished mission [{message.name}] "
                           f"(echo reports {message.status.state}) -- not re-queueing")
                return
            if message.status.state.done:
                self.debug(f"Ignoring terminal mission [{message.name}] "
                           f"({message.status.state}) -- not re-queueing")
                return
            self.info(f"Received a new mission [{message.name}]")
            self._missions[message.name] = message
            if self._current_mission is None:
                await self._try_start_mission()
        else:  # If we've seen this mission, update it
            # An edit that moved a mission that has not been dispatched to another robot:
            # that robot's dispatcher queues it (the server routes by `robot`), so this one
            # lets go of it.
            dispatched = self._current_mission is not None and \
                self._current_mission.name == message.name and \
                self._current_behavior_tree is not None
            if message.robot != self._name and not dispatched:
                self.info(f"Mission [{message.name}] was moved to robot {message.robot}")
                del self._missions[message.name]
                if self._current_mission is not None and self._current_mission.name == message.name:
                    self._current_mission = None
                    await self._try_start_mission()
                return
            if self._current_mission is not None and self._current_mission.name == message.name:
                # A held mission has not been dispatched, so an operator's spec edit can
                # still take effect; once the behavior tree exists the orders are on
                # their way and the edit can only be ignored.
                self._apply_spec_edit(self._current_mission, message,
                                      dispatched=self._current_behavior_tree is not None)
                self.info(f"Update a RUNNING mission [{message.name}]")
                cancel_node_from_api = self._update_mission_from_api(
                    self._current_mission, message)
                # Delete/Cancel a running mission
                if self._current_mission.lifecycle == \
                        api_objects.object.ObjectLifecycleV1.PENDING_DELETE:
                    self._current_mission.needs_canceled = True

                if self._wait_task is not None and self._current_mission.needs_canceled:
                    # Nothing is running on the robot during a wait, so there is no
                    # order to cancel: end the mission here.
                    self.mission_info("Cancelled during a wait")
                    self._set_mission_state(mission_object.MissionStateV1.CANCELED)
                    self._set_robot_idle_after_mission()
                    await self.get_next_mission()
                    return

                if self._current_mission.needs_canceled or cancel_node_from_api:
                    # One cancel at a time. This branch runs on *every* change event
                    # for the running mission -- including the watcher echo of each
                    # status write we make per robot state message -- so minting a
                    # fresh actionId here each time flooded the robot with a new
                    # cancelOrder per state message until the cancel completed
                    # (23k+ distinct cancel actions observed for one mission, each
                    # rejected by the robot as "cancel already in progress"). The
                    # outstanding one is resent by handle_instant_action() anyway.
                    if self._has_outstanding_cancel():
                        self.debug("cancelOrder already outstanding; not sending another")
                        return
                    self.info("Cancelling current node...")
                    action_id = f"{self._order_prefix()}-instantaction-n{self._header_id}"
                    self.mission_info(f"Send cancel order action {action_id}")
                    await self._send_cancel_order(action_id)
                return

            self.info(f"Update a PENDING mission [{message.name}]")
            self._apply_spec_edit(self._missions[message.name], message, dispatched=False)
            self._update_mission_from_api(
                self._missions[message.name], message)
            # Delete a queued mission
            if await self._robot_server.delete_pending_mission(message):
                del self._missions[message.name]
            # Cancel a queued mission
            elif message.needs_canceled:
                self._missions[message.name].status.state = mission_object.MissionStateV1.CANCELED
                await self._database.update_status(api_objects.MissionObjectV1, self._missions[message.name].name, self._missions[message.name].status, uuid.uuid4())
                del self._missions[message.name]

    async def _on_robot_change(self, message: api_objects.RobotObjectV1):
        if self._robot_object is None:
            # Create robot object
            self.info("Created robot")
            self._robot_object = message

            self._header_id = 0
            self._robot_online_task = \
                asyncio.get_event_loop().create_task(self._check_robot_online())

            if (not self._robot_server.disable_request_factsheet
                    and self._robot_object.status.factsheet.agv_class == ""):
                action_id = f"instantaction-n{self._header_id}"
                factsheet_action_type = types.VDA5050InstantActionType.FACTSHEET_REQUEST
                instant_action = types.VDA5050Action(
                    actionType=factsheet_action_type, actionId=action_id)
                self.info(
                    f"FACTSHEET INFO: Sending {factsheet_action_type.value} action.")
                await self._send_instant_action(instant_action)
                self._current_instant_actions[action_id] = instant_action

            await self._handle_force_cancel(message)
            await self._try_start_mission()
        else:
            # Delete robot update
            if message.lifecycle == api_objects.object.ObjectLifecycleV1.PENDING_DELETE:
                if message.status.state == api_objects.robot.RobotStateV1.ON_TASK:
                    # Set mission to failure
                    self._set_mission_state(
                        mission_object.MissionStateV1.FAILED)
                # Set the state of the robot to DELETE for RobotServer to delete
                # on the server and database side.
                self.debug(
                    "Robot is idle and delete request received, deleting robot.")
                await self._delete_robot_object()

            # Re-request factsheet if not yet received (robot may have restarted MQTT)
            if (not self._robot_server.disable_request_factsheet and
                    self._robot_object.status.factsheet.agv_class == ""):
                action_id = f"instantaction-n{self._header_id}"
                factsheet_action_type = types.VDA5050InstantActionType.FACTSHEET_REQUEST
                instant_action = types.VDA5050Action(
                    actionType=factsheet_action_type, actionId=action_id)
                self.info(
                    f"FACTSHEET INFO: Re-sending {factsheet_action_type.value} action.")
                await self._send_instant_action(instant_action)
                self._current_instant_actions[action_id] = instant_action

            # Teleop update
            if (message.switch_teleop and
                    self._robot_object.status.state != robot_object.RobotStateV1.TELEOP) or \
                    (not message.switch_teleop and
                     self._robot_object.status.state == robot_object.RobotStateV1.TELEOP):
                action_id = f"instantaction-n{self._header_id}"
                action_type = types.NVInstantActionType.START_TELEOP \
                    if message.switch_teleop else types.NVInstantActionType.STOP_TELEOP
                instant_action = types.VDA5050Action(
                    actionType=action_type, actionId=action_id)
                self.mission_info(f"Sending {action_type.value} action.")
                await self._send_instant_action(instant_action)
                self._current_instant_actions[action_id] = instant_action

            await self._handle_force_cancel(message)

            # Robot object update
            self._robot_object = message

    async def _check_robot_online(self):
        if self._robot_object is None:
            return
        try:
            await asyncio.sleep(self._robot_object.heartbeat_timeout.total_seconds())
            self.info("Robot Offline")
            self._robot_object.status.recording_state = None
            self._robot_object.status.nav_reasoning = None
            if not self._robot_object.status.online:
                await self._database.update_status(api_objects.RobotObjectV1, self._robot_object.name, self._robot_object.status, uuid.uuid4())
                return
            self._robot_object.status.online = False
            if self._robot_object.lifecycle is not api_objects.object.ObjectLifecycleV1.DELETED:
                await self._database.update_status(api_objects.RobotObjectV1, self._robot_object.name, self._robot_object.status, uuid.uuid4())
        except asyncio.CancelledError:
            self.debug("Cancelled robot online check.")

    async def handle_instant_action(self, message: types.VDA5050State):
        # Handle instant actions
        updated_instant_action_ids = []
        finished_instant_actions = []
        for action_state in message.actionStates[::-1]:
            # Iterate through all the appended instant actions
            if action_state.actionType not in (types.VDA5050InstantActionType.values() +
                                               types.NVInstantActionType.values()):
                break
            if action_state.actionId in self._current_instant_actions.keys():
                if action_state.actionStatus == types.VDA5050ActionStatus.FINISHED:
                    # Update current instant aciton dict
                    finished_instant_actions.append(
                        self._current_instant_actions.pop(action_state.actionId))
                    self._instant_action_resends.pop(action_state.actionId, None)
                    self.mission_info(
                        f"Finished instant action:\n {finished_instant_actions[-1]}")
                elif action_state.actionStatus == types.VDA5050ActionStatus.FAILED:
                    # FAILED is as terminal as FINISHED: the robot will never move
                    # this action again, so keeping it here only blocks
                    # _has_outstanding_cancel() forever and keeps it in the resend
                    # loop. A FAILED cancelOrder specifically means "no order to
                    # cancel" (VDA5050 noOrderToCancel) -- the robot has nothing of
                    # this mission left running, which is the outcome a cancel was
                    # after, so it counts as a completed cancel for the mission.
                    failed = self._current_instant_actions.pop(action_state.actionId)
                    self._instant_action_resends.pop(action_state.actionId, None)
                    if failed.actionType == types.VDA5050InstantActionType.CANCEL_ORDER:
                        self.mission_info(
                            f"cancelOrder {action_state.actionId} reported FAILED "
                            "(no order to cancel) -- treating mission as cancelled")
                        finished_instant_actions.append(failed)
                    else:
                        self.warning(
                            f"Instant action {failed.actionType} {action_state.actionId} "
                            f"reported FAILED by robot: {action_state.resultDescription}")
                updated_instant_action_ids.append(action_state.actionId)

        # Resend instant actions if they are not in the feedback message. An action is
        # only cleared above when the robot reports it FINISHED, so a robot that never
        # acknowledges one (it may reject the action outright, e.g. cancelOrder when it
        # has no active order) would otherwise be resent on every single state message
        # indefinitely -- previously observed as ~4.6M resends in 25 minutes. Give up
        # after MAX_INSTANT_ACTION_RESENDS attempts.
        give_up: List[str] = []
        for action_id, instant_action in self._current_instant_actions.items():
            if action_id not in updated_instant_action_ids:
                attempts = self._instant_action_resends.get(action_id, 0) + 1
                if attempts > self.MAX_INSTANT_ACTION_RESENDS:
                    self.warning(
                        f"Abandoning {instant_action.actionType} instant action "
                        f"{action_id} -- unacknowledged after "
                        f"{self.MAX_INSTANT_ACTION_RESENDS} resends")
                    give_up.append(action_id)
                    continue
                self._instant_action_resends[action_id] = attempts
                # Resend instant action
                await self._send_instant_action(instant_action)
                self.mission_info(
                    f"Resend {instant_action.actionType} instant action "
                    f"({attempts}/{self.MAX_INSTANT_ACTION_RESENDS}).")
        for action_id in give_up:
            self._current_instant_actions.pop(action_id, None)
            self._instant_action_resends.pop(action_id, None)
        return finished_instant_actions
    async def _process_datum_message(self, msg: types.RobotDatum) -> None:
        """Persist robot datum and auto-seed the current map's datum if it has none."""
        self._robot_object.datum.latitude = msg.latitude
        self._robot_object.datum.longitude = msg.longitude
        self._robot_object.datum.bearing_deg = msg.bearing_deg
        await self._database.update_spec(
            api_objects.RobotObjectV1, self._name, self._robot_object.spec, uuid.uuid4()
        )
        current_map = self._robot_object.current_map
        if current_map:
            try:
                map_obj = await self._database.get_object(api_objects.MapObjectV1, current_map)
                if map_obj and map_obj.datum_latitude is None:
                    map_obj.datum_latitude = msg.latitude
                    map_obj.datum_longitude = msg.longitude
                    map_obj.datum_bearing_deg = msg.bearing_deg
                    await self._database.update_spec(
                        api_objects.MapObjectV1, current_map, map_obj.spec, uuid.uuid4()
                    )
                    self.info(f"Auto-seeded datum for map '{current_map}' from robot datum.")
            except Exception as e:
                self.warning(f"Failed to auto-seed datum for map '{current_map}': {e}")

    async def _on_client_message(self, message: types.VDA5050State):
        self.debug(f"[{message.orderId}] Got feedback")
        # If we have a robot, Update it with the details from the message
        if self._robot_object is not None:
            # Check if the current task to verify if robot is online still exists
            if self._robot_online_task is not None:
                # Cancel to replace with another task to update the online checking time
                self._robot_online_task.cancel()
            self._robot_online_task = \
                asyncio.get_event_loop().create_task(self._check_robot_online())
            if message.agvPosition:
                self._robot_object.status.pose.x = message.agvPosition.x
                self._robot_object.status.pose.y = message.agvPosition.y
                self._robot_object.status.pose.theta = message.agvPosition.theta
                self._robot_object.status.pose.map_id = message.agvPosition.mapId
            if message.batteryState:
                self._robot_object.status.battery_level = message.batteryState.batteryCharge
                if message.batteryState.charging and not self._robot_object.status.state.running:
                    self._set_robot_state(
                        robot_object.RobotStateV1.CHARGING)
                    self._charging_mission_received = False
                elif (self._robot_object.status.state == robot_object.RobotStateV1.CHARGING and
                      not message.batteryState.charging):
                    self._set_robot_state(
                        robot_object.RobotStateV1.IDLE)

            if self._robot_server.mission_ctrl_url:
                request_map = (not self._robot_object.status.pose.map_id
                               and self._robot_object.status.state.can_deploy_map)
                send_charging_mission = (self._robot_object.battery.recommended_minimum
                                         and (self._robot_object.status.battery_level <=
                                              self._robot_object.battery.recommended_minimum)
                                         and not self._robot_object.status.state.running
                                         and not self._charging_mission_received)
                if request_map or send_charging_mission:
                    # Check mission control health
                    try:
                        health_response = requests.get(
                            self._robot_server.mission_ctrl_url + "/api/v1/health")
                        if health_response.status_code == 200:
                            # Send map request
                            if request_map:
                                response = requests.post(
                                    self._robot_server.mission_ctrl_url + "/api/v1/push_map",
                                    params={"robot_name": self._name})
                                if response.status_code == 200:
                                    self._set_robot_state(
                                        robot_object.RobotStateV1.MAP_DEPLOYMENT)
                                    logging.debug(
                                        "Map loading request posted successfully for robot %s",
                                        self._name)
                                else:
                                    logging.warning(
                                        "Failed to post map loading request for robot %s ",
                                        self._name)
                            if send_charging_mission:
                                response = requests.post(
                                    self._robot_server.mission_ctrl_url+"/api/v1/mission/charging",
                                    params={"robot_name": self._name})
                                if response.status_code == 200:
                                    logging.debug(
                                        "Charging mission posted successfully for robot %s",
                                        self._name)
                                    self._charging_mission_received = True
                                else:
                                    logging.warning(
                                        "Failed to post charging mission for robot %s ",
                                        self._name)
                    except requests.exceptions.ConnectionError as err:
                        # Service doesn't exist, handle accordingly
                        logging.warning(
                            "Connection error occurred: \n %s", err)
                    except requests.exceptions.HTTPError as http_err:
                        logging.warning("HTTP error occurred: \n %s", http_err)
                    except requests.exceptions.Timeout as timeout_err:
                        logging.warning(
                            "Timeout error occurred: \n %s", timeout_err)
            if not self._robot_object.status.online:
                self.info("Robot Online")
            self._robot_object.status.online = True
            # Single pass over the VDA5050 information[] array. Each infoType is an
            # independent slot keyed by type (last entry wins on the rare duplicate),
            # so we collect them once instead of re-scanning the list per field.
            info_by_type: Dict[str, str] = {
                i.infoType: i.infoDescription for i in (message.information or [])}

            if "user_info" in info_by_type:
                self._robot_object.status.info_messages = \
                    json.loads(info_by_type["user_info"])

            # deviation_range is a server-tracked value kept alongside the user_info
            # payload. Write it *after* the user_info replacement above so it is not
            # clobbered when a robot reports agvPosition and user_info in the same
            # state message.
            if message.agvPosition is not None:
                if self._robot_object.status.info_messages is None:
                    self._robot_object.status.info_messages = {}
                self._robot_object.status.info_messages["deviation_range"] = \
                    message.agvPosition.deviationRange

            # Recording state: prefer the information[] entry, else the legacy
            # top-level recordingState field.
            recording_info = info_by_type.get(
                "recordingState", message.recordingState)
            if recording_info is not None:
                self._robot_object.status.recording_state = recording_info

            # Navigation reasoning narration (infoType="navReasoning"): the robot
            # re-sends its latest operator-facing line in every ~1Hz state message.
            # It is level-triggered, so we keep the last known line and treat only a
            # *changed* description as a new event (log it once); an unchanged line is
            # a heartbeat. An absent entry leaves the last line in place — MQTT is
            # QoS 0, so a single message missing it must not erase the narration.
            nav_reasoning = info_by_type.get("navReasoning")
            if nav_reasoning is not None:
                if nav_reasoning != self._robot_object.status.nav_reasoning:
                    self.info(f"Nav reasoning: {nav_reasoning}")
                self._robot_object.status.nav_reasoning = nav_reasoning

            self._robot_object.status.errors = vda5050_errors_to_status_dict(message.errors)
            # Robot's online/error status just changed — retry dispatch of a mission
            # that was being withheld for that reason. No-op if still not ready, and
            # never touches an already-dispatched mission (held is only ever set on a
            # not-yet-dispatched PENDING mission).
            if self._current_mission is not None and self._current_mission.status.held:
                await self._try_start_mission()
            # Update robot unique ID
            self._robot_object.status.hardware_version = \
                robot_object.RobotHardwareVersionV1(manufacturer=message.manufacturer,
                                                    serial_number=message.serialNumber)
            if self._robot_object.lifecycle is not api_objects.object.ObjectLifecycleV1.DELETED:
                await self._database.update_status(api_objects.RobotObjectV1, self._robot_object.name, self._robot_object.status, uuid.uuid4())

            # Update object detection results if necessary
            for action_state in message.actionStates:
                if (action_state.actionStatus == types.VDA5050ActionStatus.FINISHED and
                        action_state.actionType == types.NVActionType.GET_OBJECTS and
                        self.robot_object is not None):
                    if self._detection_results_object is None:
                        self._detection_results_object = api_objects.DetectionResultsObjectV1(
                            name=self.robot_object.name)
                        await self._database.create_object(
                            self._detection_results_object, uuid.uuid4())
                    self._detection_results_object.status.detected_objects = \
                        [DetectedObject(**item) for item in json.loads(
                            action_state.resultDescription)]

                    await self._database.update_status(
                        api_objects.DetectionResultsObjectV1, self._detection_results_object.name, self._detection_results_object.status, uuid.uuid4())
                    self.info(
                        "Updated object detector information in mission database.")

        finished_instant_actions = await self.handle_instant_action(message)
        self.update_robot_state(finished_instant_actions)

        # Make sure there is a mission to update
        if self._current_mission is None or self._current_behavior_tree is None:
            return

        # In case mission failed due to timeout
        if self._current_mission.status.state.done:
            if not self._will_run_another_pass():
                self._set_robot_idle_after_mission()
            await self.get_next_mission()
            return

        # During a wait the robot has no order of ours to report (a mission or pass that
        # starts with one still has the previous order's id on its state), so a
        # mismatch is expected and must neither be counted nor trigger a resend.
        if self._wait_key is not None and \
                not order_ids.is_order_of(self._order_prefix(), message.orderId):
            return

        # If the order doesn't match, ignore it
        if not order_ids.is_order_of(self._order_prefix(), message.orderId):
            self._order_mismatch_count += 1
            self.info(f"[{self._current_mission.name}] Got message from another mission order: "
                      f"{message.orderId} "
                      f"({self._order_mismatch_count}/{self.MAX_ORDER_MISMATCHES})")
            # Normally the robot adopts our order within a message or two and this
            # self-corrects. If it never does -- e.g. it dropped the order without
            # telling us -- resending forever leaves the mission RUNNING and the robot
            # reported ON_TASK while it sits still, with nothing surfaced to the
            # operator. Fail the mission instead so the state is visible and the queue
            # can move on.
            if self._order_mismatch_count >= self.MAX_ORDER_MISMATCHES:
                self.warning(
                    f"[{self._current_mission.name}] Robot never adopted our order after "
                    f"{self.MAX_ORDER_MISMATCHES} state messages (still reporting "
                    f"{message.orderId}) -- failing mission")
                self._current_mission.status.failure_reason = \
                    ("Robot did not accept the dispatched order "
                     f"(still reporting {message.orderId})")
                self._set_mission_state(mission_object.MissionStateV1.FAILED)
                self._order_mismatch_count = 0
                self._set_robot_idle_after_mission()
                await self.get_next_mission()
                return
            await self._send_order()
            return
        self._order_mismatch_count = 0

        prev_child_node = self._current_behavior_tree.current_node.name
        self.update_mission_state(message, finished_instant_actions)

        # Resend node requested by the user
        if self._updating_mission_from_api:
            # The robot cancelled this node's order to take the new content, and still
            # holds its orderId; resending under the same id would be a different
            # order with an id the robot has already seen. If the revision can't be
            # persisted, leave the flag set and retry on the next state message.
            if not await self._bump_order_rev():
                return
            self.mission_info(f"Resend the updated mission node {prev_child_node}: "
                              f"{self._current_behavior_tree.current_node.name}")
            await self._send_order()
            self._updating_mission_from_api = False

        # If current node is updated, then send a new order
        if prev_child_node != self._current_behavior_tree.current_node.name:
            self.mission_info(f"Update node from {prev_child_node} to "
                              f"{self._current_behavior_tree.current_node.name}")
            await self._send_order()

        if self._current_mission.status.state.done:
            await self.post_mission_completion()

    async def _on_client_factsheet(self, message: types.VDA5050Factsheet):
        if self._robot_object is not None:
            self._robot_object.status.factsheet.agv_class = message.typeSpecification.agvClass
            self._robot_object.status.factsheet.speed_max = message.physicalParameters.speedMax
            # Footprint in metres, both optional in VDA5050; a missing value stays "unknown" (-1).
            physical = message.physicalParameters
            if physical.length is not None and physical.length > 0:
                self._robot_object.status.factsheet.length = physical.length
            if physical.width is not None and physical.width > 0:
                self._robot_object.status.factsheet.width = physical.width
            if physical.heightMax is not None and physical.heightMax > 0:
                self._robot_object.status.factsheet.height = physical.heightMax

            # Store custom actions from factsheet
            if message.actions:
                self._robot_object.status.factsheet.custom_actions = [
                    robot_object.CustomActionV1(
                        action_type=action.actionType,
                        action_description=action.actionDescription,
                        action_parameters=[
                            robot_object.CustomActionParameterV1(key=param.key, value=param.value or "")
                            for param in action.actionParameters
                        ],
                        blocking_type=action.blockingType.value,
                        icon_hint=action.iconHint
                    )
                    for action in message.actions
                ]
                self.info(f"Stored {len(message.actions)} custom actions from factsheet")

            await self._database.update_status(api_objects.RobotObjectV1, self._robot_object.name, self._robot_object.status, uuid.uuid4())

    async def post_mission_completion(self):
        # Delete a completed/failure mission
        if self._current_mission is None:
            return
        await self._robot_server.delete_pending_mission(self._current_mission)
        # Set robot to idle -- unless the mission is about to run its next pass, in
        # which case the robot never stops being on task.
        if not self._will_run_another_pass():
            self._set_robot_idle_after_mission()
        await self.get_next_mission()

    def _remember_finished(self, name: str) -> None:
        """Record that this controller has run `name` to completion, evicting the
        oldest entry once the set outgrows MAX_FINISHED_MISSIONS_TRACKED."""
        self._finished_missions[name] = None
        self._finished_missions.move_to_end(name)
        while len(self._finished_missions) > self.MAX_FINISHED_MISSIONS_TRACKED:
            self._finished_missions.popitem(last=False)

    def _will_run_another_pass(self) -> bool:
        """Whether the current mission has just completed a pass and has more to run.

        A cancelled (or deleted) mission never does: cancelling during any pass ends
        the whole repeat."""
        mission = self._current_mission
        if mission is None or mission.status.state != mission_object.MissionStateV1.COMPLETED:
            return False
        if mission.needs_canceled or \
                mission.lifecycle is not api_objects.object.ObjectLifecycleV1.ALIVE:
            return False
        return mission.repeat == 0 or mission.status.passes_completed + 1 < mission.repeat

    async def _start_next_pass(self) -> bool:
        """Run the current (just completed) mission again, in place, as its next pass.

        The mission object stays the same, so the operator sees one mission with a lap
        counter rather than a pile of copies; what changes is the run id, so the new
        pass's VDA5050 order/node ids can never collide with the previous pass's (the
        robot may still hold those). The reset is persisted before anything is sent
        (the same rule as _assign_run_id). Returns False if the pass could not be
        started; the mission then simply finishes as COMPLETED.
        """
        mission = self._current_mission
        assert mission is not None
        self._cancel_mission_timeout()
        self._cancel_wait()
        status = mission.status.copy(deep=True)
        status.passes_completed += 1
        status.state = mission_object.MissionStateV1.PENDING
        status.node_status = {name: mission_object.MissionNodeStatusV1()
                              for name in status.node_status}
        status.current_node = 0
        status.task_status = {}
        status.end_timestamp = None
        status.failure_reason = None
        status.failure_category = None
        status.blocked = False
        status.blocked_node = None
        status.blocked_edge = None
        status.blocked_waypoint_index = None
        status.block_reason = None
        status.held = False
        status.held_reason = None
        status.run_id = uuid.uuid4().hex[:8]
        status.order_rev = 0
        try:
            await self._database.update_status(
                api_objects.MissionObjectV1, mission.name, status, uuid.uuid4())
        except Exception as err:  # pylint: disable=broad-except
            self.warning(f"[{mission.name}] Could not persist the next pass ({err}); "
                         "finishing the mission instead")
            return False
        mission.status = status
        self._order_mismatch_count = 0
        self.last_node_seq_id = -1
        self.mission_info(f"Starting pass {status.passes_completed + 1}"
                          f"{'' if mission.repeat == 0 else f' of {mission.repeat}'}"
                          f", run id {status.run_id}")
        self._current_behavior_tree = behavior_tree.MissionBehaviorTree(mission)
        if not self._current_behavior_tree.create_behavior_tree():
            mission.status.failure_reason = self._current_behavior_tree.failure_reason
            self._set_mission_state(mission_object.MissionStateV1.FAILED)
            return False
        self.update_mission_from_behavior_tree()
        self._arm_mission_timeout()
        await self._send_order()
        return True

    async def _chain_then_run(self, finished: api_objects.MissionObjectV1):
        """Start the mission named by `finished.then_run`, as a copy of it.

        A copy rather than the template itself, because the template is usually a
        mission that has already run. Never fails the finished mission: a template that
        is missing or belongs to another robot is logged and skipped."""
        if not finished.then_run:
            return
        key = f"{finished.name}:{finished.status.run_id}"
        if key == self._chained_completion:
            return
        self._chained_completion = key
        try:
            template = await self._database.get_object(
                api_objects.MissionObjectV1, finished.then_run)
            if template.robot != finished.robot:
                self.warning(f"[{finished.name}] then_run mission {finished.then_run} is for "
                             f"robot {template.robot}, not {finished.robot}; not chaining")
                return
            chained = api_objects.MissionObjectV1(
                name=f"{template.name}-run-{int(time.time() * 1000)}",
                robot=template.robot,
                mission_tree=[node.copy(deep=True) for node in template.mission_tree],
                timeout=template.timeout,
                mode=template.mode,
                register_map=template.register_map,
                repeat=template.repeat,
                then_run=template.then_run,
                status=mission_object.MissionStatusV1(),
                lifecycle=api_objects.object.ObjectLifecycleV1.ALIVE)
            await self._database.create_object(chained, uuid.uuid4())
            self.mission_info(f"Chained mission {chained.name} from {template.name}")
        except Exception as err:  # pylint: disable=broad-except
            self.warning(f"[{finished.name}] Could not chain then_run mission "
                         f"{finished.then_run}: {err}")

    async def get_next_mission(self):
        if self._current_mission is None:
            return
        if self._current_mission.status.state == mission_object.MissionStateV1.COMPLETED and \
                not self._current_mission.needs_canceled and \
                self._current_mission.lifecycle is api_objects.object.ObjectLifecycleV1.ALIVE:
            if self._will_run_another_pass() and await self._start_next_pass():
                return
            if self._current_mission.status.state == mission_object.MissionStateV1.COMPLETED:
                # The last pass is a finished pass too: "lap 3 / 3" reads passes_completed.
                final = self._current_mission
                final.status.passes_completed += 1
                try:
                    await self._database.update_status(
                        api_objects.MissionObjectV1, final.name, final.status, uuid.uuid4())
                except Exception as err:  # pylint: disable=broad-except
                    self.warning(f"[{final.name}] Could not persist the pass count ({err})")
                await self._chain_then_run(final)
        self._cancel_wait()
        self._ignored_spec_edits.discard(self._current_mission.name)
        self._remember_finished(self._current_mission.name)
        del self._missions[self._current_mission.name]
        self._current_mission = None
        # Check to see if a robot is pending delete
        if self._robot_object is not None and \
                self._robot_object.lifecycle == \
                api_objects.object.ObjectLifecycleV1.PENDING_DELETE:
            await self._delete_robot_object()
        else:
            await self._try_start_mission()

    def _arm_mission_timeout(self):
        """(Re)start the mission timeout watchdog for the current mission.

        Any previously scheduled timeout is cancelled first. Used both on initial
        dispatch and when resuming after an edgeBlocked condition clears."""
        self._cancel_mission_timeout()
        if self._current_mission is None:
            return
        self._mission_timeout_task = asyncio.get_event_loop().create_task(
            self._wait_mission_timeout(
                self._current_mission.timeout.total_seconds(),
                self._current_mission.name))

    def _cancel_mission_timeout(self):
        """Cancel the mission timeout watchdog (e.g. while the mission is blocked and
        legitimately waiting for an operator reroute, so it is not failed as TIMEOUT)."""
        if self._mission_timeout_task is not None:
            self._mission_timeout_task.cancel()
            self._mission_timeout_task = None

    async def _wait_mission_timeout(self, timeout: float, name: str):
        await asyncio.sleep(timeout)
        # Check to see if the mission that launched this thread is still running
        if (self._current_mission is None) or (self._robot_object is None):
            return

        if name == self._current_mission.name and \
                self._current_mission.status.state == mission_object.MissionStateV1.RUNNING:
            # A mission blocked on an impassable edge is legitimately waiting for an
            # operator reroute — never fail it as a timeout. (The timeout task is also
            # cancelled on block; this guards the rare cancel/fire race.)
            if self._current_mission.status.blocked:
                return
            # In case there is no response from the client
            if await self._robot_server.delete_pending_mission(self._current_mission):
                return
            if self._current_mission.needs_canceled:
                self._set_mission_state(mission_object.MissionStateV1.CANCELED)
            else:
                self._current_mission.status.failure_reason = "Mission timed out"
                self._set_mission_state(mission_object.MissionStateV1.FAILED)
            # Tell the robot to actually abandon its order before moving on — without
            # this, a robot that never finished the order (e.g. stuck retrying/stalled
            # navigation, exactly what triggers this timeout in the first place) keeps
            # reporting the old orderId indefinitely. Nothing else here ever notices;
            # the mission object is already gone from _missions/_current_mission below,
            # so the normal needs_canceled-driven cancelOrder path (see the "Update a
            # RUNNING mission" branch above) never runs for it. The next dispatched
            # mission then gets rejected by the robot ("An order is running") and fails
            # the same way after MAX_ORDER_MISMATCHES — observed in practice as a
            # "zombie order" a rerun could not recover from short of manually
            # publishing a cancelOrder or restarting the robot's VDA5050 client.
            # One cancel at a time, same as the explicit-cancel path: on the
            # needs_canceled route a cancelOrder is usually already outstanding, and
            # handle_instant_action() keeps resending that one regardless of which
            # mission is current, so a second one here would only be a duplicate.
            if not self._has_outstanding_cancel():
                timeout_cancel_id = \
                    f"{self._order_prefix()}-timeout-cancel-n{self._header_id}"
                self.mission_info(
                    f"Sending cancelOrder {timeout_cancel_id} so the robot "
                    "abandons the timed-out order")
                await self._send_cancel_order(timeout_cancel_id)
            self._set_robot_idle_after_mission()
            await self.get_next_mission()

    async def _delete_robot_object(self):
        if self._robot_object is not None:
            self._robot_object.lifecycle = api_objects.object.ObjectLifecycleV1.DELETED
            self._alive = False
            if self._robot_online_task is not None:
                self._robot_online_task.cancel()
            await self._robot_server.delete_robot(self._name)

    @staticmethod
    def _sequence_id_from_node_id(node_id: str) -> Optional[int]:
        """Sequence id encoded in a node id we generated ("...-s{seq}"), else None."""
        return order_ids.node_sequence(node_id)

    def update_mission_node_state(self, message: types.VDA5050State,
                                  finished_instant_actions: List[types.VDA5050Action])\
            -> mission_object.MissionStateV1:
        # Update mission state from robot client
        if self._current_mission is None:
            return mission_object.MissionStateV1.PENDING
        mission_node_index = order_ids.order_node_index(message.orderId)
        current_mission_node = self._current_mission.mission_tree[mission_node_index]
        task_status = self._current_mission.status.task_status
        # lastNodeId/lastNodeSequenceId describe the last node the robot *reached*,
        # which lags the order it has accepted: right after we dispatch a new
        # mission's order the robot echoes the new orderId while still reporting the
        # previous mission's final node. Every node this mission generates is named
        # for its run ("{prefix}-n{node}-s{seq}", see order_ids -- the prefix carries
        # the mission name, run id and order revision, so a same-named earlier run or
        # a cancelled revision doesn't match either), so a lastNodeId not carrying
        # the current prefix is a leftover from the previous one and must not be
        # read as progress: its terminal sequence id satisfies the route-complete
        # test below and completes a brand-new mission on its very first state
        # message (observed: a mission COMPLETED 55ms after dispatch with the robot
        # still parked at the previous route's endpoint).
        #
        # Keyed on the run prefix rather than the node id, so that advancing
        # between mission_tree nodes *within* one mission still reads the previous
        # node's sequence id exactly as before.
        reached_node_in_current_mission = \
            order_ids.is_node_of(self._order_prefix(), message.lastNodeId)
        # A foreign lastNodeId means "this mission has reached nothing yet" -- which
        # also covers the empty lastNodeId the robot reports before its very first
        # order, since that matches no mission's prefix either.
        last_node_seq_id = \
            message.lastNodeSequenceId if reached_node_in_current_mission else 0
        # lastNodeId and lastNodeSequenceId must describe the same node. Every node
        # id we generate ends in "-s{sequenceId}", so cross-check the two: a robot
        # that reset only the id on accepting a new order (observed 2026-09-14:
        # lastNodeId=Test2-n1-s0 with lastNodeSequenceId=6 left over from the
        # previous route) passes the prefix guard above and would complete a route
        # of three waypoints on its first state message. The id is the field the
        # robot set for *this* order, so it wins.
        seq_from_node_id = self._sequence_id_from_node_id(message.lastNodeId)
        if reached_node_in_current_mission and seq_from_node_id is not None and \
                seq_from_node_id != last_node_seq_id:
            self.warning(
                f"[{self._current_mission.name}] lastNodeSequenceId "
                f"{last_node_seq_id} disagrees with lastNodeId "
                f"'{message.lastNodeId}'; using {seq_from_node_id} from the id")
            last_node_seq_id = seq_from_node_id
        current_order_node_id = \
            last_node_seq_id + 2 if reached_node_in_current_mission else 0

        node_state = self._current_mission.status.node_status[str(
            current_mission_node.name)].state
        if current_mission_node.type == mission_object.MissionNodeType.ROUTE and \
                current_mission_node.route is not None:
            # Find the index of the waypoint that the robot last reached
            # - Nodes are separated by 2 sequenceId, hence we divide by 2
            # - We also pad by an additional node in the beginning that is not in our
            #   waypoints, so we subtract 1
            # - This means that (lastNodeSequenceId = 2) -> (idx = 0)
            idx = last_node_seq_id // 2 - 1

            # For route nodes, task index corresponds to the last user-defined node reached
            # We assume that user-defined nodes will allowedDeviationXY = 0
            # Because we pad by an additional node in the beginning, we want to ignore
            # that node, so we enforce that idx >= 0.
            #
            # Assign `idx` directly rather than incrementing a separate counter (as this
            # used to: `0` on first reach, `+= 1` after) -- `idx` is already the exact,
            # correctly-computed waypoint index, so incrementing a shadow counter was both
            # redundant and, on the very first reach, wrong whenever idx happened to be
            # anything other than 0 (e.g. a mission resumed or rerouted partway through its
            # route). sati-client's utils/missionRouteProgress.ts now reads this value as
            # the authoritative "which waypoint" signal (see AUDIT_BACKLOG Z9 item 5), so it
            # needs to be correct on every reach, not just steady-state increments.
            if self.last_node_seq_id < last_node_seq_id and \
                    idx >= 0 and \
                    idx < len(current_mission_node.route.waypoints) and \
                    current_mission_node.route.waypoints[idx].allowedDeviationXY == 0:
                task_status[str(current_mission_node.name)] = idx
                asyncio.ensure_future(self._database.update_status(api_objects.MissionObjectV1, self._current_mission.name, self._current_mission.status, uuid.uuid4()))

            if current_order_node_id == current_mission_node.route.size * 2 + 2:
                node_state = mission_object.MissionStateV1.COMPLETED

        elif current_mission_node.type == mission_object.MissionNodeType.MOVE and \
                current_mission_node.move is not None and \
                current_order_node_id == 1 * 2 + 2:
            node_state = mission_object.MissionStateV1.COMPLETED
        # TODO(Nico): fix the action states index
        elif current_mission_node.type == mission_object.MissionNodeType.ACTION:
            if message.actionStates[0].actionStatus == types.VDA5050ActionStatus.FINISHED:
                node_state = mission_object.MissionStateV1.COMPLETED
            elif message.actionStates[0].actionStatus == types.VDA5050ActionStatus.FAILED:
                node_state = mission_object.MissionStateV1.FAILED
            # Check if this is a teleop action node
            elif message.actionStates[0].actionType == types.NVActionType.PAUSE_ORDER and \
                self._robot_object is not None and \
                    self._robot_object.status.state != robot_object.RobotStateV1.TELEOP:
                self._set_robot_state(robot_object.RobotStateV1.TELEOP)
                self.mission_info("Switch to teleop")
        # Check if there is an instant order cancellation feedback
        for finished_instant_action in finished_instant_actions:
            if finished_instant_action.actionType == types.VDA5050InstantActionType.CANCEL_ORDER:
                node_state = mission_object.MissionStateV1.CANCELED
                break

        # Save last node sequence id. Stores the current order's value (0 while the
        # robot is still reporting a previous order's node) so the waypoint-advance
        # test above compares like with like across an order change.
        self.last_node_seq_id = last_node_seq_id

        if self.get_mission_errors(message):
            self.warning("Fatal Errors present, failing mission")
            node_state = mission_object.MissionStateV1.FAILED
        # Set mission node state based on update from robot client message
        self.set_mission_node_state(str(current_mission_node.name), node_state)
        return node_state

    def _handle_edge_blocked(self, message: types.VDA5050State) -> bool:
        """Detect a robot-reported ``edgeBlocked`` WARNING and record it as a
        non-terminal block on the current mission.

        The robot emits this on ``state.errors[]`` when a topological edge is
        impassable after its local retries. It then stops, goes IDLE, and waits for
        the server/operator to send a new route. The mission deliberately stays
        RUNNING (the error is a WARNING, not FATAL); we only annotate it so an
        operator can see it and issue a reroute.

        Returns True while the mission is blocked, signalling the caller to skip
        further state/behavior-tree processing for this tick. When the robot stops
        reporting the block (the new order has been ingested and navigation resumed),
        any recorded block is cleared and False is returned.
        """
        if self._current_mission is None:
            return False
        status = self._current_mission.status
        blocked_error = next(
            (e for e in message.errors if e.errorType == "edgeBlocked"), None)

        if blocked_error is None:
            # No active block reported. If we had one recorded, the robot has
            # resumed (a new order cleared its error) — clear the server-side block.
            if status.blocked:
                self._clear_block(self._current_mission)
            return False

        # Resolve references. The nodeId encodes the mission_tree node and the
        # waypoint sequence as "{mission}-n{node_idx}-s{sequence}". edgeId may be
        # absent if the robot could not resolve it — tolerate that.
        blocked_edge = None
        blocked_node_name = None
        blocked_waypoint_index = None
        for ref in blocked_error.errorReferences:
            if ref.referenceKey in ("edgeId", "edge_id"):
                blocked_edge = ref.referenceValue
            elif ref.referenceKey in ("nodeId", "node_id"):
                node_idx = order_ids.node_index(ref.referenceValue)
                if node_idx is not None and \
                        node_idx < len(self._current_mission.mission_tree):
                    blocked_node_name = str(
                        self._current_mission.mission_tree[node_idx].name)
                seq = order_ids.node_sequence(ref.referenceValue)
                if seq is not None:
                    blocked_waypoint_index = seq // 2 - 1

        # Idempotency: the idle robot re-emits this WARNING in every state message,
        # so only act (log + persist) when the block is new or its target changed.
        if (status.blocked and status.blocked_node == blocked_node_name and
                status.blocked_edge == blocked_edge):
            return True

        status.blocked = True
        status.blocked_node = blocked_node_name
        status.blocked_edge = blocked_edge
        status.blocked_waypoint_index = blocked_waypoint_index
        status.block_reason = blocked_error.errorDescription
        if blocked_node_name is not None and blocked_node_name in status.node_status:
            status.node_status[blocked_node_name].error_msg = \
                blocked_error.errorDescription

        self.warning(
            f"Edge blocked: node={blocked_node_name} edge={blocked_edge} "
            f"reason={blocked_error.errorDescription!r}; mission stays RUNNING, "
            "awaiting reroute")

        # The robot has stopped and is IDLE; reflect that and stop the timeout from
        # failing a mission that is legitimately waiting for an operator reroute.
        self._set_robot_state(robot_object.RobotStateV1.IDLE)
        self._cancel_mission_timeout()

        asyncio.ensure_future(self._database.update_status(
            api_objects.MissionObjectV1, self._current_mission.name,
            status, uuid.uuid4()))
        return True

    def _clear_block(self, mission: api_objects.MissionObjectV1):
        """Clear a recorded edgeBlocked condition once the robot has resumed."""
        if not mission.status.blocked:
            return
        blocked_node = mission.status.blocked_node
        mission.status.blocked = False
        mission.status.blocked_node = None
        mission.status.blocked_edge = None
        mission.status.blocked_waypoint_index = None
        mission.status.block_reason = None
        if blocked_node is not None and blocked_node in mission.status.node_status:
            mission.status.node_status[blocked_node].error_msg = None
        self.mission_info("Edge block cleared; mission resuming")
        # Robot is moving again; restore ON_TASK and re-arm the mission timeout.
        self._set_robot_state(robot_object.RobotStateV1.ON_TASK)
        if mission is self._current_mission:
            self._arm_mission_timeout()
        asyncio.ensure_future(self._database.update_status(
            api_objects.MissionObjectV1, mission.name, mission.status, uuid.uuid4()))

    def get_mission_errors(self, message: types.VDA5050State):
        fatal_errors = False
        if len(message.errors) == 0:
            return False
        for error in message.errors:
            # Skip warnings
            if error.errorLevel != types.VDA5050ErrorLevel.FATAL:
                continue
            fatal_errors = True
            for error_reference in error.errorReferences:
                if error_reference.referenceKey in \
                        ["node_id", "nodeId", "action_id", "actionId"]:
                    mission_node_id = \
                        error_reference.referenceValue.rsplit(
                            "-n")[-1].rsplit("-s")[0]
                    try:
                        mission_node = int(mission_node_id)
                    except ValueError:
                        continue
                    if self._current_mission is not None and \
                            mission_node < len(self._current_mission.mission_tree):
                        (self._current_mission.status.node_status[
                            str(self._current_mission.mission_tree[mission_node].name)].error_msg) \
                            = error.errorDescription
                        self._current_mission.status.failure_reason = "\n".join(
                            error.errorDescription for error in message.errors)
        return fatal_errors

    def update_mission_from_behavior_tree(self):
        # update mission state from behavior tree
        if self._current_behavior_tree is None or self._current_mission is None:
            return
        # Record the old status and store the new status
        previous_mission_status = self._current_mission.status.copy(deep=True)
        # Update mission status
        self._current_behavior_tree.update()
        self._current_mission.status.current_node = self._current_behavior_tree.current_node.idx
        current_state = behavior_tree.tree2mission_state(
            self._current_behavior_tree.status)
        mission_state_updated = self._set_mission_state(current_state)
        # In case mission node status get updated but mission state remains the same
        if not mission_state_updated and previous_mission_status != self._current_mission.status:
            self.info(
                f"update mission node: {self._current_mission.status.current_node}")
            asyncio.ensure_future(self._database.update_status(api_objects.MissionObjectV1, self._current_mission.name, self._current_mission.status, uuid.uuid4()))

    def update_robot_state(self, finished_instant_actions: List[types.VDA5050Action]):
        """ Update robot states after teleop is finished

        Only the teleop instant actions matter here. Any other acknowledged instant
        action (cancelOrder, factsheetRequest, ...) says nothing about teleop and must
        not move the robot out of TELEOP: a robot that is still paused only leaves it
        through stopTeleop, and the dispatcher only sends that while it is in TELEOP.

        Args:
            finished_instant_actions (List[types.VDA5050Action]): All the completed instant actions
        """
        for finished_instant_action in finished_instant_actions:
            if finished_instant_action.actionType == types.NVInstantActionType.START_TELEOP:
                self._set_robot_state(robot_object.RobotStateV1.TELEOP)
                self.mission_info("Switch to teleop")
            elif finished_instant_action.actionType == types.NVInstantActionType.STOP_TELEOP:
                resume_robot_state = robot_object.RobotStateV1.ON_TASK \
                    if self._current_mission else robot_object.RobotStateV1.IDLE
                self._set_robot_state(resume_robot_state)
                self.mission_info("Stop teleop")

    def update_mission_state(self, message: types.VDA5050State,
                             finished_instant_actions: List):
        # Update mission state from both robot feedback and behavior tree
        # Do nothing if there is no mission
        if (self._current_mission is None) or (self._robot_object is None) or \
                (self._current_behavior_tree is None):
            return

        # Check for missionStatus published inside the VDA5050 informations array.
        # The robot firmware embeds lifecycle events here instead of a separate topic.
        mission_status = next(
            (i.infoDescription for i in (message.information or [])
             if i.infoType == "missionStatus"),
            None
        )

        if mission_status == "completed":
            # The robot reports this per order, and each mission_tree node gets its own
            # order, so it completes the node the order belongs to; the behavior tree
            # then decides whether that was the last one (a single-node mission ends
            # right here, as it always did).
            if self._complete_order_node(message):
                self.update_mission_from_behavior_tree()
            return
        if mission_status == "failed":
            # Populate failure_reason from the errors array before transitioning.
            self.get_mission_errors(message)
            self._set_mission_state(mission_object.MissionStateV1.FAILED)
            return
        if mission_status == "canceled":
            if self._current_mission.needs_canceled:
                self._set_mission_state(mission_object.MissionStateV1.CANCELED)
            else:
                self._updating_mission_from_api = True
            return

        # A robot that has hit an impassable edge reports an edgeBlocked WARNING and
        # waits IDLE (it does not emit missionStatus="failed"). Record the block and
        # stop here so a waiting mission is not churned or advanced; it resumes once
        # an operator reroute clears the block.
        if self._handle_edge_blocked(message):
            return

        # For "reached" (intermediate waypoint) and the no-info case, fall through
        # to the existing behavior-tree path so node-level tracking stays intact.
        node_state = self.update_mission_node_state(
            message, finished_instant_actions)
        if node_state == mission_object.MissionStateV1.CANCELED:
            if self._current_mission.needs_canceled:
                self._set_mission_state(mission_object.MissionStateV1.CANCELED)
            else:
                self._updating_mission_from_api = True
            return

        self.update_mission_from_behavior_tree()

    def _leaf_node_count(self) -> int:
        """How many leaf (order) nodes the current mission has."""
        assert self._current_mission is not None
        return sum(1 for node in self._current_mission.mission_tree
                   if node.type in (mission_object.MissionNodeType.ROUTE,
                                    mission_object.MissionNodeType.MOVE,
                                    mission_object.MissionNodeType.ACTION,
                                    mission_object.MissionNodeType.NOTIFY,
                                    mission_object.MissionNodeType.CONSTANT))

    def _complete_order_node(self, message: types.VDA5050State) -> bool:
        """Mark the node the message's order belongs to COMPLETED, for the robot's
        missionStatus "completed". Returns whether the behavior tree should be updated."""
        assert self._current_mission is not None
        try:
            node = self._current_mission.mission_tree[order_ids.order_node_index(message.orderId)]
        except (ValueError, IndexError):
            return False
        # In a mission with several nodes the previous order's "completed" can still be
        # on the robot's state right after the next order is accepted, and must not
        # complete that new node: a route only counts once the robot says it reached a
        # node of this run. A single-node mission has no earlier order to be stale.
        if self._leaf_node_count() > 1 and \
                node.type in (mission_object.MissionNodeType.ROUTE,
                              mission_object.MissionNodeType.MOVE) and \
                not order_ids.is_node_of(self._order_prefix(), message.lastNodeId):
            return False
        self.set_mission_node_state(str(node.name), mission_object.MissionStateV1.COMPLETED)
        return True

    def _apply_spec_edit(self, target: api_objects.MissionObjectV1,
                         message: api_objects.MissionObjectV1, dispatched: bool):
        """Copy an operator's spec edit (PUT /missions/{name}) onto the mission this
        dispatcher already loaded. Only a mission that has not been dispatched yet can
        take one: a dispatched mission's orders are already with the robot."""
        # A reroute rewrites a route of the dispatcher's copy of the tree only (the
        # database keeps the original and the request in update_nodes), so on a
        # dispatched mission a differing tree is expected and not an edit.
        changed = [field for field in EDITABLE_SPEC_FIELDS
                   if getattr(target, field) != getattr(message, field) and
                   not (dispatched and field == "mission_tree" and message.update_nodes)]
        if not changed:
            return
        if dispatched:
            if target.name not in self._ignored_spec_edits:
                self._ignored_spec_edits.add(target.name)
                self.warning(f"[{target.name}] Ignoring an edit of {changed}: the mission "
                             "has already been dispatched")
            return
        for field in changed:
            setattr(target, field, getattr(message, field))
        if "mission_tree" in changed:
            names = ["root"] + [str(node.name) for node in target.mission_tree]
            target.status.node_status = {
                name: target.status.node_status.get(name, mission_object.MissionNodeStatusV1())
                for name in names}
        self.info(f"Applied an edit of {changed} to mission [{target.name}]")

    def _start_wait(self, mission_node: mission_object.MissionNodeV1):
        """Start the timer of a "wait" action node. No order goes to the robot."""
        assert self._current_mission is not None and mission_node.action is not None
        status = self._current_mission.status
        seconds = float(mission_node.action.action_parameters["seconds"])
        key = (str(self._current_mission.name), str(mission_node.name),
               status.run_id, status.order_rev, status.passes_completed)
        # _send_order() runs again for a node whenever the robot's state does not match
        # yet; the timer already running for this node must not be restarted by that.
        if self._wait_key == key:
            return
        self._cancel_wait()
        self._wait_key = key
        self.mission_info(f"Waiting {seconds:g}s at node {mission_node.name}")
        self.set_mission_node_state(str(mission_node.name),
                                    mission_object.MissionStateV1.RUNNING)
        asyncio.ensure_future(self._persist_current_mission_status())
        self._wait_task = asyncio.get_event_loop().create_task(
            self._run_wait_timer(seconds, self._wait_key))

    async def _run_wait_timer(self, seconds: float,
                              key: Tuple[str, str, Optional[str], int, int]):
        await asyncio.sleep(seconds)
        await self._messages.put(WaitElapsed(key=key))

    def _cancel_wait(self):
        """Drop the running wait, if any. Safe from any path that leaves the mission."""
        if self._wait_task is not None and self._wait_task is not asyncio.current_task():
            self._wait_task.cancel()
        self._wait_task = None
        self._wait_key = None

    async def _on_wait_elapsed(self, message: WaitElapsed):
        if self._current_mission is None or self._wait_key != message.key or \
                self._current_behavior_tree is None:
            return
        self._wait_task = None
        self._wait_key = None
        node_name = message.key[1]
        self.set_mission_node_state(node_name, mission_object.MissionStateV1.COMPLETED)
        prev_child_node = self._current_behavior_tree.current_node.name
        self.update_mission_from_behavior_tree()
        if self._current_mission.status.state.done:
            await self.post_mission_completion()
        elif prev_child_node != self._current_behavior_tree.current_node.name:
            await self._send_order()

    async def run(self):
        while self._alive:
            try:
                message = await self._messages.get()
                # If this is a robot object
                if isinstance(message, api_objects.RobotObjectV1):
                    await self._on_robot_change(message)
                elif isinstance(message, api_objects.MissionObjectV1):
                    await self._on_mission_change(message)
                elif isinstance(message, types.VDA5050State):
                    await self._on_client_message(message)
                elif isinstance(message, types.VDA5050Factsheet):
                    await self._on_client_factsheet(message)
                elif isinstance(message, types.RobotDatum):
                    await self._process_datum_message(message)
                elif isinstance(message, WaitElapsed):
                    await self._on_wait_elapsed(message)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.warning(f"Unhandled exception in robot message loop: {e}")

    async def send_message(self, message):
        await self._messages.put(message)

    def info(self, message: str):
        self._logger.info(
            "[Isaac Mission Dispatch] | INFO: [%s] %s", self._name, message)

    def mission_info(self, message: str):
        if self._current_mission is not None:
            mission = "Mission ID - " + self._current_mission.name
        else:
            mission = "None"
        self._logger.info("[Isaac Mission Dispatch] | INFO: [%s] [%s] %s",
                          self._name, mission, message)

    def debug(self, message: str):
        self._logger.debug(
            "[Isaac Mission Dispatch] | DEBUG: [%s] %s", self._name, message)

    def warning(self, message: str):
        self._logger.warning(
            "[Isaac Mission Dispatch] | WARNING: [%s] %s", self._name, message)

    def _set_robot_state(self, state: robot_object.RobotStateV1):
        if self._robot_object is None or state == self._robot_object.status.state:
            return
        self.info(f"Robot state: {self._robot_object.status.state} -> {state}")
        if self._robot_server.push_telemetry:
            prev_state_timestamp = self._cur_robot_state_timestamp
            self._cur_robot_state_timestamp = datetime.datetime.now()
            duration = (self._cur_robot_state_timestamp -
                        prev_state_timestamp).total_seconds()
            robot_metrics = {
                f"{self._robot_object.status.state.value}.duration": duration}
            self._telemetry.add_kpi(
                self._robot_object.name, robot_metrics, metrics.Timeframe.ROBOT)
            self._telemetry_client.send_telemetry(self._telemetry.get_kpis_by_frequency(
                metrics.Timeframe.ROBOT))
        self._robot_object.status.state = state
        asyncio.ensure_future(self._database.update_status(api_objects.RobotObjectV1, self._robot_object.name, self._robot_object.status, uuid.uuid4()))

    def _set_robot_idle_after_mission(self):
        """The robot's state once a mission has ended -- unless it is teleoperated.

        A mission ending (completed, failed, cancelled, timed out) says nothing about
        teleop: the robot may still be paused by a pause_order or startTeleop, and only
        stopTeleop releases it. Dropping TELEOP here would make the dispatcher believe
        the robot is free, so it would never send that stopTeleop."""
        if self._robot_object is not None and \
                self._robot_object.status.state == robot_object.RobotStateV1.TELEOP:
            return
        self._set_robot_state(robot_object.RobotStateV1.IDLE)

    def _set_mission_state(self, state: mission_object.MissionStateV1):
        if self._current_mission is None or state == self._current_mission.status.state:
            return False
        self.mission_info(
            f"Mission state: {self._current_mission.status.state} -> {state}")
        self._current_mission.status.state = state
        self._current_mission.status.node_status["root"].state = state
        if state.done:
            # Terminal mission states are set here directly (e.g. on timeout or
            # cancel-before-ack), bypassing the leaf-node updates that normally
            # come from robot feedback (see update_mission_node_state). Propagate
            # to any node still RUNNING/PENDING so node_status doesn't disagree
            # with the mission-level state forever.
            for node_state in self._current_mission.status.node_status.values():
                if not node_state.state.done:
                    node_state.state = state
        if state == mission_object.MissionStateV1.RUNNING:
            # If the mission just moved to RUNNING, set the start timestamp
            if self._current_mission.status.start_timestamp is None:
                self._current_mission.status.start_timestamp = datetime.datetime.now()
                # A teleoperated (paused) robot stays TELEOP until stopTeleop.
                if self._robot_object is None or \
                        self._robot_object.status.state != robot_object.RobotStateV1.TELEOP:
                    self._set_robot_state(robot_object.RobotStateV1.ON_TASK)
                self.mission_info(
                    f"Mission started at {self._current_mission.status.start_timestamp}")
        elif state.done:
            self._current_mission.status.end_timestamp = datetime.datetime.now()
            # If the mission just moved to COMPLETED, record the end timestamp
            if state == mission_object.MissionStateV1.COMPLETED:
                self.mission_info(
                    f"Mission completed at {self._current_mission.status.end_timestamp}")
            # If the mission just moved to CANCELED, record the end timestamp
            elif state == mission_object.MissionStateV1.CANCELED:
                self.mission_info(
                    f"Mission cancelled at {self._current_mission.status.end_timestamp}")
            # If the mission just moved to FAILED, record the reason and end timestamp
            elif state == mission_object.MissionStateV1.FAILED:
                self.mission_info(
                    f"Mission failed at {self._current_mission.status.end_timestamp}")
                self.mission_info(
                    f"Failure reason: {self._current_mission.status.failure_reason}")

            if self._robot_server.push_telemetry:
                telem = {}  # type: Dict[str, Union[int, str]]
                telem[f"{state}"] = 1
                telem["mission_id"] = self._current_mission.name

                self._telemetry.add_kpi(
                    "mission_fate",
                    telem,
                    metrics.Timeframe.MISSION)
                self._telemetry_client.send_telemetry(
                    self._telemetry.get_kpis_by_frequency(
                        metrics.Timeframe.MISSION))
                self._telemetry.clear_frequency(metrics.Timeframe.MISSION)

        if self._current_mission.status.start_timestamp is not None and \
                self._current_mission.status.end_timestamp is not None:
            self.mission_info("Mission duration: "
                              f"""{self._current_mission.status.end_timestamp -
                                   self._current_mission.status.start_timestamp}""")
        asyncio.ensure_future(self._database.update_status(api_objects.MissionObjectV1, self._current_mission.name, self._current_mission.status, uuid.uuid4()))
        return True

    def set_mission_node_state(self, node_name: str, state: mission_object.MissionStateV1):
        if self._current_mission is None:
            return
        previous_state = self._current_mission.status.node_status[node_name].state
        if previous_state == state:
            return
        self.mission_info(f"Node {node_name}: {previous_state} -> {state}")
        self._current_mission.status.node_status[node_name].state = state

    def _process_notify_node(self, mission_node):
        self.set_mission_node_state(f"{mission_node.name}",
                                    mission_object.MissionStateV1.RUNNING)
        retries = 0
        while retries <= 3:
            response = requests.post(url=mission_node.notify.url,
                                     json=mission_node.notify.json_data,
                                     timeout=mission_node.notify.timeout)
            if response.status_code == 200:
                self.set_mission_node_state(f"{mission_node.name}",
                                            mission_object.MissionStateV1.COMPLETED)
                break
            elif response.status_code in [408, 425, 429, 500, 502, 503, 504]:
                self.mission_info(
                    f"Notify: {response.status_code} received, retrying")
                retries += 1
            else:
                self.set_mission_node_state(f"{mission_node.name}",
                                            mission_object.MissionStateV1.FAILED)
                break
        if retries > 3:
            self.set_mission_node_state(f"{mission_node.name}",
                                        mission_object.MissionStateV1.FAILED)

        # Since Notify does not send an order, there is no feedback from robot, so we
        # need to trigger update here
        self.update_mission_from_behavior_tree()

    @property
    def robot_object(self) -> Optional[robot_object.RobotObjectV1]:
        return self._robot_object


class RobotServer:
    """Handles sending missions to robots using the VDA5050 protocol"""

    def __init__(self, mqtt_host: str = "localhost", mqtt_port: int = 1883,
                 mqtt_transport: str = "tcp", mqtt_ws_path: Optional[str] = None,
                 mqtt_prefix: str = "uagv/v2/RobotCompany",
                 mqtt_username: Optional[str] = None,
                 mqtt_password: Optional[str] = None,
                 postgres_db: str = "mission",
                 postgres_user: str = "postgres",
                 postgres_password: str = "postgres",
                 postgres_host: str = "localhost",
                 postgres_port: int = 5432,
                 mission_ctrl_url: Optional[str] = None, push_telemetry: bool = False,
                 telemetry_env: str = "DEV", disable_request_factsheet: bool = False):
        """Initializes a RobotServer object by starting threads for mqtt and for the robot/mission
        database watchers
        Args:
            mqtt_host: The hostname for the mqtt client to connect to
            mqtt_port: The port for the mqtt client to connect to
            mqtt_prefix: The prefix to add to all VDA5050 mqtt topics
            databae_url: The url where the database REST API is hosted
        """
        self._logger = logging.getLogger("Isaac Mission Dispatch")

        # Save parameters to use later
        self._mqtt_prefix = mqtt_prefix

        # Connect to the db
        from packages.database.postgres import PostgresDatabase
        self._database = PostgresDatabase(
            dbname=postgres_db,
            user=postgres_user,
            password=postgres_password,
            host=postgres_host,
            port=postgres_port
        )

        # Create queues to propogate changes to the main thread
        self._event_loop = asyncio.get_event_loop()
        self._mission_changes: asyncio.Queue[api_objects.MissionObjectV1] = asyncio.Queue()
        self._robot_changes: asyncio.Queue[api_objects.RobotObjectV1] = asyncio.Queue()
        self._mqtt_messages: asyncio.Queue = asyncio.Queue()

        # Connect to MQTT
        self._mqtt_client = self._connect_to_mqtt(
            mqtt_host, mqtt_port, mqtt_transport, mqtt_ws_path,
            mqtt_username, mqtt_password
        )

        # The robot objects
        self._robots: Dict[str, Robot] = {}

        # Mission control
        self.mission_ctrl_url = mission_ctrl_url
        self.push_telemetry = push_telemetry
        self.disable_request_factsheet = disable_request_factsheet
        self.telemetry_env = telemetry_env

    def _enqueue(self, queue, obj):
        asyncio.run_coroutine_threadsafe(queue.put(obj), self._event_loop)

    def _mqtt_on_connect(self, client, userdata, flags, rc):
        client.subscribe(f"{self._mqtt_prefix}/+/state")
        client.subscribe(f"{self._mqtt_prefix}/+/factsheet")
        client.subscribe(f"{self._mqtt_prefix}/+/datum")

    def _mqtt_on_message(self, client, userdata, msg):
        state_match = re.match(f"{self._mqtt_prefix}/(.*)/state", msg.topic)
        factsheet_match = re.match(
            f"{self._mqtt_prefix}/(.*)/factsheet", msg.topic)
        datum_match = re.match(f"{self._mqtt_prefix}/(.*)/datum", msg.topic)
        try:
            if state_match:
                robot = state_match.groups()[0]
                pl = msg.payload
                self._enqueue(self._mqtt_messages, ClientStatusMessage(name=robot,
                                                                       payload=json.loads(pl)))
            elif factsheet_match:
                robot = factsheet_match.groups()[0]
                pl = msg.payload
                self._enqueue(self._mqtt_messages, ClientFactsheetMessage(name=robot,
                                                                          payload=json.loads(pl)))
            elif datum_match:
                robot = datum_match.groups()[0]
                pl = msg.payload
                self._enqueue(self._mqtt_messages, ClientDatumMessage(name=robot,
                                                                      payload=json.loads(pl)))
            else:
                self.warning(
                    f"Got message from unrecognized topic \"{msg.topic}\"")
                return
        except pydantic.ValidationError as e:
            self.warning(f"Validation error from client message:\n{e.errors()}")
        except Exception as e:
            self.warning(f"Error processing MQTT message: {e}")

    def _connect_to_mqtt(self, host: str, port: int, transport: str, ws_path: Optional[str],
                         username: Optional[str], password: Optional[str]) -> MQTTClient:
        client = MQTTClient(
            client_id="mission_dispatcher_backend",
            broker=host,
            port=port,
            transport=transport,
            ws_path=ws_path,
            username=username,
            password=password
        )
        
        # Register wildcard callbacks
        client.register_callback(f"{self._mqtt_prefix}/+/state", self._mqtt_on_message)
        client.register_callback(f"{self._mqtt_prefix}/+/factsheet", self._mqtt_on_message)
        client.register_callback(f"{self._mqtt_prefix}/+/datum", self._mqtt_on_message)
        
        client.connect()
        return client

    async def stop(self):
        loop = asyncio.get_event_loop()
        loop.stop()

    async def _watch_changes(self, object_class: Any, queue: asyncio.Queue):
        while True:
            try:
                publisher_id = uuid.uuid4()
                watcher_instance = await self._database.get_watcher(object_class, publisher_id)
                with watcher_instance:
                    async for update in watcher_instance.watch():
                        self.debug(f"Watch object update: {object_class.get_alias()}")
                        await queue.put(update)
            except Exception as err:
                self.warning(f"Exit: {err}")
                if hasattr(self._mqtt_client, 'disconnect'):
                    self._mqtt_client.disconnect()
                elif hasattr(self._mqtt_client, 'loop_stop'):
                    self._mqtt_client.loop_stop()
                asyncio.run_coroutine_threadsafe(self.stop(), self._event_loop)
                break

    async def _handle_robot_changes(self):
        while True:
            robot = await self._robot_changes.get()
            # Ignore deleted robot object
            if robot.lifecycle == \
                    api_objects.object.ObjectLifecycleV1.DELETED:
                continue
            # Robots being deleted may not have a name
            if hasattr(robot, "name"):
                if robot.name not in self._robots:
                    self.debug(f"Got robot from database {robot.name}")
                    self._robots[robot.name] = Robot(robot.name, self._database,
                                                     self._mqtt_client, self._mqtt_prefix, self)
                await self._robots[robot.name].send_message(robot)

    async def _handle_mission_changes(self):
        while True:
            mission = await self._mission_changes.get()

            # Ignore deleted mission object
            if mission.lifecycle == \
                    api_objects.object.ObjectLifecycleV1.DELETED:
                continue

            # Ignore missions that are already done
            if mission.status.state.done:
                # Delete completed mission
                await self.delete_pending_mission(mission)
                continue

            # Put the mission into the queue for the correct robot object
            if mission.robot not in self._robots:
                self.debug(f"Got new mission from database {mission.name}")
                self._robots[mission.robot] = Robot(mission.robot, self._database,
                                                    self._mqtt_client, self._mqtt_prefix, self)
            await self._robots[mission.robot].send_message(mission)

    async def _handle_mqtt_messages(self):
        while True:
            message = await self._mqtt_messages.get()
            if message.name not in self._robots:
                # Try to get the robot from the database
                try:
                    robot = await self._database.get_object(api_objects.RobotObjectV1, message.name)
                    self.debug(f"Got robot from database on MQTT message: {message.name}")
                    self._robots[message.name] = Robot(message.name, self._database,
                                                       self._mqtt_client, self._mqtt_prefix, self)
                    # Send the robot object to the robot handler
                    await self._robots[message.name].send_message(robot)
                except Exception as e:
                    self.warning(
                        f"Ignoring MQTT message from unknown robot \"{message.name}\": {e}")
                    continue
            await self._robots[message.name].send_message(message.payload)

    async def _run(self):
        await self._database.async_init()
        await asyncio.gather(
            self._watch_changes(api_objects.MissionObjectV1, self._mission_changes),
            self._watch_changes(api_objects.RobotObjectV1, self._robot_changes),
            self._handle_robot_changes(),
            self._handle_mission_changes(),
            self._handle_mqtt_messages()
        )

    async def delete_robot(self, robot_name: str):
        robot = self._robots[robot_name]
        if robot is not None:
            properties = robot.robot_object
            if properties is not None:
                await self._database.set_lifecycle(api_objects.RobotObjectV1, properties.name, api_objects.object.ObjectLifecycleV1.DELETED, uuid.uuid4())
                del self._robots[properties.name]

    async def delete_pending_mission(self, mission: api_objects.MissionObjectV1) -> bool:
        if mission.lifecycle == \
                api_objects.object.ObjectLifecycleV1.PENDING_DELETE:
            await self._database.set_lifecycle(api_objects.MissionObjectV1, mission.name, api_objects.object.ObjectLifecycleV1.DELETED, uuid.uuid4())
            self.info(f"Deleted mission {mission.name}")
            return True
        return False

    def run(self):
        # Start threads and corroutines
        # self._mqtt_client.loop_start() handles loop internally now
        self._event_loop.run_until_complete(self._run())

    def info(self, message: str):
        self._logger.info("[Isaac Mission Dispatch] | INFO: %s", message)

    def debug(self, message: str):
        self._logger.debug("[Isaac Mission Dispatch] | DEBUG: %s", message)

    def warning(self, message: str):
        self._logger.warning("[Isaac Mission Dispatch] | WARNING: %s", message)
