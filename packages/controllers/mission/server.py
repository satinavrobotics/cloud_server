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
import enum
import hashlib
import json
import logging
import math
import re
import requests
import signal
import time
import time as _wall_time
import uuid
from typing import Any, Deque, Dict, List, Optional, Set, Tuple, Union
from collections import OrderedDict, deque

import fastapi
import psycopg
import psycopg_pool
import pydantic

from packages.utils.mqtt_client import MQTTClient
from packages.controllers.mission import battery
from packages.controllers.mission import behavior_tree
from packages.controllers.mission import fleet_recorder
from packages.controllers.mission import lifecycle
from packages.controllers.mission import order_ids
from packages.controllers.mission import order_policy
from packages.controllers.mission import planner_client
from packages.controllers.mission import run_change
from packages.events.codes import EventCode, Source
from packages.events.emit import Event, emit as emit_event
import packages.controllers.mission.vda5050_types as types
from packages.database.postgres import PostgresDatabase
from packages.utils import blocked_nodes, map_geo, map_sessions, metrics
import cloud_common.objects as api_objects
import cloud_common.objects.common as common_objects
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

# Outgoing headerIds start at HEADER_ID_RATE per second since HEADER_ID_EPOCH (not at 0), so a
# restarted dispatcher continues above the earlier process's (VDA5050: headerId counts up
# per topic; a robot may drop a lower one). uint32 holds ~34 years of this from the epoch;
# the dispatcher sends far less than HEADER_ID_RATE messages per second on one topic.
HEADER_ID_EPOCH = 1767225600      # 2026-01-01T00:00:00Z
HEADER_ID_RATE = 4
HEADER_ID_MAX = 2 ** 32 - 1


def initial_header_id(now: Optional[float] = None) -> int:
    """Where a new process's headerIds start (see HEADER_ID_RATE)."""
    seconds = (_wall_time.time() if now is None else now) - HEADER_ID_EPOCH
    return min(max(int(seconds * HEADER_ID_RATE), 0), HEADER_ID_MAX)


# Webhook calls (notify nodes, the charging hook) run in a worker thread so a slow endpoint
# never stalls the event loop shared by all robots. A notify node's spec timeout is capped;
# its retries wait these seconds before the 2nd, 3rd and 4th attempt.
NOTIFY_MAX_TIMEOUT_S = 30.0
NOTIFY_RETRY_BACKOFF_S = (1.0, 2.0, 4.0)
NOTIFY_RETRY_STATUSES = (408, 425, 429, 500, 502, 503, 504)
# The charging hook (--mission_ctrl_url): per-request timeout, and the least time between
# two attempts for one robot (state messages arrive far more often than that).
CHARGING_HOOK_TIMEOUT_S = 5.0
# A handler that fails the same way on every state message would log at state rate: the
# robot loop logs the first occurrence of each distinct exception (type + message) with its
# traceback, then at most one count summary per this many seconds. At most
# LOOP_ERROR_MAX_KINDS distinct kinds are tracked (the least recently logged is forgotten).
LOOP_ERROR_SUMMARY_INTERVAL_S = 60.0
LOOP_ERROR_MAX_KINDS = 32
CHARGING_HOOK_RETRY_S = 60.0

# Phase 0 tables dispatch will write (v2 §5.3). They come from the API's Alembic migration
# (20260924_01_phase0_core; robot_track_ts: 20261009_01_robot_track), so on startup dispatch waits until they exist.
DISPATCH_REQUIRED_TABLES = ("mission_runs", "fleet_events", "robot_state_ts", "robot_latest",
                            "robot_track_ts")

# How long the recording-only settings watcher waits before re-watching after a failure
SETTINGS_WATCH_RETRY_S = 5.0

# _watch_changes (missions, robots) restarts its watch after a failure: first retry after
# the minimum, doubling up to the maximum; a watch that delivered something starts over.
WATCH_CHANGES_RETRY_MIN_S = 1.0
WATCH_CHANGES_RETRY_MAX_S = 30.0

# An MQTT message from a robot the dispatcher does not know is looked up in the database once
# per this many seconds per name (and warned about once), not once per message.
UNKNOWN_ROBOT_TTL_S = 60.0

# A robot state of ON_TASK with no mission is set back to IDLE only this long after the robot's
# controller was created (a dispatcher restart re-queues a running mission first).
STALE_STATE_GRACE_S = 30.0
# Maps §14.13: a run-epoch check that failed (database) is retried after this long.
RUN_CHECK_RETRY_S = 30.0
# The run-epoch check runs inline on the robot's state loop: it is given this long (a pool
# stall can last 30 s), then it counts as failed and is retried after RUN_CHECK_RETRY_S.
RUN_CHECK_TIMEOUT_S = 5.0
# A current mission whose start failed (an exception before its tree existed) is started
# again on a robot state message at most this often, and failed after this many attempts.
START_RETRY_S = 5.0
MAX_START_ATTEMPTS = 5
# A failed status write (database restart, pool timeout) is retried with this back-off
# (doubling, capped) until it lands, the row is gone or the controller is shut down. A
# failure streak is a warning on its first failure and an error from the Nth on.
STATUS_WRITE_RETRY_MIN_S = 0.5
STATUS_WRITE_RETRY_MAX_S = 10.0
STATUS_WRITE_ERROR_AFTER = 5
# A status write failing with one of these is retried (the database or the network is
# unavailable); anything else (a serialization TypeError, a DataError) cannot succeed on a
# retry and is dropped after one logged attempt.
STATUS_WRITE_TRANSIENT_ERRORS = (psycopg.OperationalError, psycopg.InterfaceError,
                                 psycopg_pool.PoolTimeout, psycopg_pool.PoolClosed,
                                 OSError, asyncio.TimeoutError)
# How long a shutdown waits for the queued status writes to land.
STATUS_WRITE_FLUSH_S = 3.0
# Graceful shutdown: each step is bounded on its own so a slow one cannot starve the next;
# their sum stays under lifecycle.SHUTDOWN_TIMEOUT_S (which stop_grace_period must exceed).
SHUTDOWN_MQTT_S = 1.5
SHUTDOWN_ROBOTS_S = STATUS_WRITE_FLUSH_S + 1.0
SHUTDOWN_RECORDER_DRAIN_S = 2.5
SHUTDOWN_RECORDER_STOP_S = 2.0
SHUTDOWN_POOL_CLOSE_S = 1.5
SHUTDOWN_LOCK_S = 1.0
# The robot row (jsonb status + NOTIFY to every listener) is written at most this often for
# fields that change continuously (pose, battery, localization score, deviation range); a
# discrete change (online, state, errors, map, recording, ...) is written at once, and the
# newest continuous values always follow within this window (trailing write).
ROBOT_STATUS_MIN_WRITE_S = 1.0
# A newest-wins state message that replaced an unprocessed one is a warning the first time
# and then every this many times (it means the robot's loop or the broker queue is behind).
STATE_COALESCED_WARN_EVERY = 100
# Failure reason of the missions of a robot that is deleted.
ROBOT_DELETED_REASON = "Robot deleted"


# The robot skipped a blocked node its nodePolicy allowed it to skip (a WARNING with the
# nodeId). Reports of these types never fail a mission, even if a robot sends them FATAL.
NODE_SKIPPED = "nodeSkipped"
ADVISORY_ERROR_TYPES = frozenset({NODE_SKIPPED})
# VDA5050 errors by which a robot rejects an order it keeps not adopting (it goes on
# reporting its old orderId). With a reference to our order they fail it at once.
ORDER_REJECTION_ERROR_TYPES = frozenset({
    "orderError", "orderUpdateError", "validationError", "noRouteError",
    "orderNotAccepted"})
# Per-node notes (informations with a nodeId reference) kept on a mission, newest first.
MISSION_NODE_NOTES_MAX = 50
# Skipped nodes kept on a mission (oldest dropped first).
MISSION_SKIPPED_NODES_MAX = 100
# Node offsets that suggest a frame error: at least this many, mostly one way
# (|mean vector| / mean length), and on average at least this far.
OFFSET_SUSPECT_MIN_N = 5
OFFSET_SUSPECT_CONSISTENCY = 0.8
OFFSET_SUSPECT_MIN_M = 0.2

# A datum that moves less than this is GNSS jitter, not a change (map-location plan A).
DATUM_CHANGE_THRESHOLD_M = 1.0
DATUM_BEARING_THRESHOLD_DEG = 0.5


def _distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Equirectangular distance in metres (fine for the metres-to-kilometres of GNSS jitter)."""
    mean_lat = math.radians((lat1 + lat2) / 2.0)
    d_north = math.radians(lat2 - lat1) * 6371000.0
    d_east = math.radians(lon2 - lon1) * 6371000.0 * math.cos(mean_lat)
    return math.hypot(d_north, d_east)


def _datum_changed(old: Optional[robot_object.RobotDatumV1],
                   new: robot_object.RobotDatumV1) -> bool:
    """Did the datum change materially? Position beyond ~1 m, a different frame, or a
    different bearing. No previous position counts as a change."""
    if old is None or old.latitude is None or old.longitude is None:
        return True
    if new.latitude is None or new.longitude is None:
        return True
    if old.frame != new.frame:
        return True
    if _distance_m(old.latitude, old.longitude, new.latitude, new.longitude) \
            > DATUM_CHANGE_THRESHOLD_M:
        return True
    d_bearing = abs((new.bearing_deg - old.bearing_deg + 180.0) % 360.0 - 180.0)
    return d_bearing > DATUM_BEARING_THRESHOLD_DEG


# Approximate position (map-location plan B): a move below this, with source and fix quality
# unchanged and accuracy within APPROX_ACCURACY_REL_TOL, is not worth a status write, unless
# the stored copy is older than APPROX_POSITION_REFRESH_S (a parked robot must not look stale).
APPROX_POSITION_THRESHOLD_M = 5.0
APPROX_ACCURACY_REL_TOL = 0.2
APPROX_POSITION_REFRESH_S = 300.0


def _approx_position_changed(old: Optional[robot_object.RobotApproxPositionV1],
                             new: types.RobotApproxPosition,
                             now: Optional[datetime.datetime] = None) -> bool:
    """Is a new approximate position worth storing? No previous one, a different source or fix
    quality, an accuracy that moved by APPROX_ACCURACY_REL_TOL or more, a move beyond the
    threshold, or a stored copy older than APPROX_POSITION_REFRESH_S."""
    if old is None:
        return True
    if old.source != new.source or old.fix_quality != new.fix_quality:
        return True
    if (old.accuracy_m is None) != (new.accuracy_m is None):
        return True
    if old.accuracy_m is not None and abs(new.accuracy_m - old.accuracy_m) \
            >= APPROX_ACCURACY_REL_TOL * max(old.accuracy_m, new.accuracy_m, 1e-9):
        return True
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if old.stored_at is None or (now - old.stored_at).total_seconds() \
            >= APPROX_POSITION_REFRESH_S:
        return True
    return _distance_m(old.latitude, old.longitude, new.latitude, new.longitude) \
        >= APPROX_POSITION_THRESHOLD_M


class RouteRefused(Exception):
    """A route node that must not be sent (maps §14): its waypoints are on a map the robot is
    not placed on, not using, or its session could not be read. The node fails."""


class _SessionUnknown:
    """The robot's open session could not be read (Postgres down): route nodes on a map are
    refused, the run recorder gets no map."""


SESSION_UNKNOWN = _SessionUnknown()

class WaitElapsed(pydantic.BaseModel):
    """Posted to a robot's own message queue when a "wait" action node's timer runs out.

    Going through the queue (rather than letting the timer task touch the mission
    itself) keeps every mission mutation on the one message loop, in order with the
    robot's state messages. `key` ties it to the wait that started the timer, so a
    timer that outlived its mission (or its pass) is recognised and dropped."""
    key: Tuple[str, str, Optional[str], int, int]


class NotifyDone(pydantic.BaseModel):
    """Posted to a robot's own message queue when a "notify" node's task has finished, so the
    next node's order (or the mission's completion) goes out on the message loop, as after a
    wait. `key` is the notify's `_notify_key`: one that outlived its mission is dropped."""
    key: Tuple[str, str, Optional[str], int, int]


class ConnectionDelivery(pydantic.BaseModel):
    """A connection message as the Robot gets it, with how the broker delivered it
    (`retained`: from its retained store after a (re)subscribe, not a live publish)."""
    connection: types.VDA5050Connection
    retained: bool = False


class MissionTimeoutElapsed(pydantic.BaseModel):
    """Posted to a robot's own message queue when the mission timeout runs out, so the
    failure and the start of the next mission run on the message loop, not inside the timer
    task (which the next mission's own timer would otherwise cancel from under it). `token`
    is the timer's `_timeout_budget`: a timer that was paused, re-armed or cancelled since is
    recognised and dropped."""
    token: Tuple[str, float, float]


RobotMessage = Union[api_objects.RobotObjectV1,
                     api_objects.MissionObjectV1,
                     types.VDA5050State,
                     types.VDA5050Factsheet,
                     types.RobotDatum,
                     types.RobotApproxPosition,
                     types.VDA5050Connection,
                     ConnectionDelivery,
                     WaitElapsed,
                     NotifyDone,
                     MissionTimeoutElapsed]


class ClientMessage(pydantic.BaseModel):
    name: str
    # TODO: perhaps do OOP to handle typing of payload too;
    # currently default is any
    payload: Any


class ClientStatusMessage(ClientMessage):
    name: str
    payload: types.VDA5050State


class RawStateMessage:
    """A robot's state topic payload as received (bytes), parsed by the consumer of
    RobotServer._mqtt_messages: a state that a newer one replaces in the queue is never
    parsed, and the paho thread does not pay for pydantic at the robots' state rate."""

    __slots__ = ("name", "payload")

    def __init__(self, name: str, payload: Any):
        self.name = name
        self.payload = payload

    def parse(self) -> "ClientStatusMessage":
        return ClientStatusMessage(name=self.name, payload=json.loads(self.payload))


class ClientFactsheetMessage(ClientMessage):
    name: str
    payload: types.VDA5050Factsheet


class ClientDatumMessage(ClientMessage):
    name: str
    payload: types.RobotDatum


class ClientApproxPositionMessage(ClientMessage):
    name: str
    payload: types.RobotApproxPosition


class ClientConnectionMessage(ClientMessage):
    name: str
    payload: types.VDA5050Connection
    retained: bool = False


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


# VDA5050 readiness errorType -> fixed hold reason (never the description: it carries a
# changing counter). Nav keys mirror sati-client's utils/robotStatus.ts; base is server-side.
_NAV_NOT_READY = "Robot navigation is not ready"
READINESS_HOLD_REASONS = {
    "robotBaseNotReadyError": "Robot base is not responding",
    # The Odin is relocalizing, or refused the map (its description names the map). Before
    # the generic nav keys: a relocalizing robot usually reports poseHealthNotReadyError too.
    "relocalizationMapRejectedError": "Robot refused its relocalization map",
    "relocalizationNotReadyError": "Robot is relocalizing",
    "navigationNotReadyError": _NAV_NOT_READY,
    "poseHealthNotReadyError": _NAV_NOT_READY,
    "tfChainNotReadyError": _NAV_NOT_READY,
    # The robot's rescue: an operator goal ended our order; nothing is dispatched until
    # the operator hands the robot back (the robot then stops reporting it).
    "operatorTakeover": "An operator has taken over the robot",
}
OPERATOR_TAKEOVER = "operatorTakeover"


class _StatusRow:
    """Write state of one database row (table, name): a lock that serializes every status
    write of the row, the newest write requested but not yet started, and the task draining
    it. `users` counts what still refers to the entry so it can be dropped. Every request
    gets a sequence number when it is made; `written_seq` is the newest one that landed, so
    a request older than it (it holds an older status object) is never written after it."""

    def __init__(self):
        self.lock = asyncio.Lock()
        self.pending: Optional[Tuple[Any, str, Any, uuid.UUID, int]] = None
        self.seq = 0
        self.written_seq = 0
        self.task: Optional[asyncio.Task[Any]] = None
        self.users = 0


class _StateSlot:
    """A VDA5050 state message waiting in a message queue. A newer state of the same robot
    replaces `msg` in place (or empties it, when other messages were queued after it), so a
    consumer that is behind handles the newest state once instead of replaying stale ones."""

    __slots__ = ("msg",)

    def __init__(self, msg: Any):
        self.msg = msg


class CancelPurpose(str, enum.Enum):
    """What a cancelOrder the dispatcher sends is for; it decides what its completion
    means (Robot._act_on_resolved_cancels)."""
    MISSION = "mission"  # the mission is being cancelled
    REPLACE = "replace"  # the node's order makes way for a new revision (reroute, resume)
    STOP = "stop"        # the robot must drop the mission's order (timeout, force cancel)
    CLEAR = "clear"      # the robot must drop an order that is not the mission's (force
                         # cancel of a stray order the mission waits behind)


# What a cancelOrder needs the robot to be reporting (Robot._cancel_order).
OURS_RUN = "run"
OURS_SENT = "sent"
OURS_DISPATCHER = "dispatcher"
OURS_ANY = "any"

SEND_NODE = "send"
NEW_REVISION = "revision"


def _route_digest(route: mission_object.MissionRouteNodeV1) -> str:
    # Unset optional waypoint fields are left out, so adding one to Pose2D does not
    # change the digest of a route stored before it existed (a mission in flight at
    # deploy would otherwise read as rerouted and its progress be ignored).
    return hashlib.sha1(
        route.json(sort_keys=True, exclude_none=True).encode()).hexdigest()[:16]


# Precomputed once: handle_instant_action() and _order_action_state() test every action
# state of every robot state message against these.
_INSTANT_ACTION_TYPES = frozenset(types.VDA5050InstantActionType.values() +
                                  types.NVInstantActionType.values())
_NON_NODE_ACTION_TYPES = _INSTANT_ACTION_TYPES | {types.NODE_POLICY_ACTION_TYPE}


class Robot:
    """Manages the mission state of a particular robot"""

    # An instant action the robot never reports FINISHED is resent on the state
    # messages that lack it, with exponential back-off (base * 2^(resends-1), capped) so
    # a robot streaming state at 10 Hz does not burn the budget in seconds; give up after
    # this many attempts. See handle_instant_action().
    MAX_INSTANT_ACTION_RESENDS = 20
    # A cancel that only drops a dead mission's order (CancelPurpose.STOP) blocks the next
    # mission's send until it is acknowledged or abandoned: it gets a shorter budget.
    STOP_CANCEL_MAX_RESENDS = 6
    INSTANT_ACTION_RESEND_BASE_S = 1.0
    INSTANT_ACTION_RESEND_MAX_S = 8.0
    # Consecutive state messages whose orderId doesn't match the current mission
    # before we stop resending and fail the mission. See _on_client_message().
    MAX_ORDER_MISMATCHES = 40
    # An order the robot has not adopted yet is sent again with exponential back-off
    # (base * 2^resends, capped), not on every state message. See _resend_due().
    ORDER_RESEND_BASE_S = 1.0
    ORDER_RESEND_MAX_S = 8.0
    # At most this many identical resends of one order the robot has not adopted; the
    # mission then fails after MAX_ORDER_MISMATCHES state messages as before.
    ORDER_MAX_RESENDS = 3
    # The mismatch count alone is per state message, so a robot streaming state fast would
    # reach MAX_ORDER_MISMATCHES before the resends above are through. The give-up also
    # needs this long since the order was last (re)sent -- longer than the largest resend
    # interval, so the schedule always completes first and the last send gets an answer.
    ORDER_GIVE_UP_MIN_S = 10.0
    # A "canceled" about the current order while the robot still lists nodes is read as
    # left over from the previous order while it drives, or this soon after the order
    # went out; otherwise as the robot dropping the order (see update_mission_state).
    CANCELED_LEFTOVER_GRACE_S = 10.0
    # More new order revisions than this for one run within the window is churn: the
    # mission is failed instead of re-issued once more. See _bump_order_rev().
    ORDER_CHURN_MAX_REVISIONS = 5
    ORDER_CHURN_WINDOW_S = 60.0
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
        # Monotonic time of each outstanding action's last resend (back-off base).
        self._instant_action_resent_at: Dict[str, float] = {}
        # Blocked-node writes in flight; held so they are not garbage collected mid-write.
        self._blocked_node_tasks: Set[asyncio.Task[Any]] = set()
        # Fire-and-forget tasks (the charging hook): referenced here so they are not garbage
        # collected mid-flight; Robot.shutdown cancels what is left.
        self._background_tasks: Set[asyncio.Task[Any]] = set()
        # Robot-loop exceptions by (type, message) -> [repeats since last log, last log time]
        self._loop_errors: Dict[Tuple[str, str], List[Any]] = {}
        # Per-row status write serializers (see _queue_status_write) and their tasks.
        self._status_rows: Dict[Tuple[str, str], _StatusRow] = {}
        self._status_write_tasks: Set[asyncio.Task[Any]] = set()
        self._status_flush_tasks: Set[asyncio.Task[Any]] = set()
        # Robot-row write throttle (ROBOT_STATUS_MIN_WRITE_S): the discrete fields and the
        # time of the last write from the state loop, and the trailing-write timer.
        self._robot_written_sig: Optional[Tuple[Any, ...]] = None
        self._robot_written_at: float = -math.inf
        self._robot_write_timer: Optional[asyncio.TimerHandle] = None
        # Newest-wins state queueing (send_message): the unprocessed state slot, and the
        # slot that is the last item of _messages (None once anything else was queued after).
        self._state_slot: Optional[_StateSlot] = None
        self._queue_tail: Optional[_StateSlot] = None
        self._states_coalesced = 0
        # Consecutive robot-state messages carrying an orderId that isn't the current
        # mission's. Bounded in _on_client_message() so a robot that never adopts our
        # order fails the mission instead of spinning silently.
        self._order_mismatch_count: int = 0
        # Last malformed user_info payload and last unusable GET_OBJECTS result logged, and
        # the GET_OBJECTS actionIds already processed (FINISHED actionStates stay listed).
        self._bad_user_info: Optional[str] = None
        self._detection_actions_done: Set[str] = set()
        self._bad_detection_actions: Set[str] = set()
        # Foreign error references (an earlier order's) already logged, once each.
        self._foreign_error_refs: Set[str] = set()
        # errorTypes of unreferenced FATAL errors the robot reported before it
        # accepted the current mission's order (or while no mission ran). Such an
        # error is a leftover of an earlier run (e.g. a safety reflex latched while
        # the previous mission was cancelled), not a failure of this mission; it is
        # ignored until the robot reports it gone once. See _track_stale_fatal().
        self._stale_fatal_types: set = set()
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
        self._robot_online_task: Optional[Any] = None  # TimerHandle, then the offline Task
        self._mission_timeout_task: Optional[asyncio.Task[Any]] = None
        # The running timeout (mission, budget s, monotonic start), and one paused while
        # the robot is offline (mission, s left). See _pause_mission_timeout().
        self._timeout_budget: Optional[Tuple[str, float, float]] = None
        self._timeout_paused: Optional[Tuple[str, float]] = None
        # Start retries of a current mission whose start raised (see _retry_stalled_start).
        self._start_attempts = 0
        self._start_retry_at = 0.0  # monotonic
        # The frame the last route was converted into (see _route_in_robot_frame).
        self._last_route_frame: Optional[Dict[str, Any]] = None
        self._robot_server = server
        self._alive = True
        # VDA5050 headerIds count per topic; see _next_header_id().
        self._header_ids: Dict[str, int] = {}
        self._header_start = initial_header_id()
        # The header's manufacturer: the robot's own, once it has reported; until then the
        # topic's (the prefix is "interface/version/manufacturer").
        self._manufacturer = prefix.rsplit("/", 1)[-1] if "/" in prefix else ""
        # The last order published. Sending it again republishes it unchanged, so one
        # orderId never carries two contents (see _send_order). What it was built from
        # is stored with the mission (status.sent_order).
        self._sent_order: Optional[types.VDA5050Order] = None
        self._order_sent_at = 0.0  # monotonic, last send (resends too)
        self._order_first_sent_at = 0.0  # monotonic, first send of _sent_order
        self._order_resends = 0
        # An orderId an earlier dispatcher process may have sent with content this one
        # does not know: it is not sent again; the next send moves to a new revision.
        self._unknown_content_order_id: Optional[str] = None
        # A mission resumed after a restart waits for the robot's first state message,
        # which decides how it goes on (see _resume_from_state).
        self._resume_pending = False
        # The send the current node is owed, made once no cancelOrder of ours is in
        # flight (see _flush_pending_send): SEND_NODE, or NEW_REVISION after the robot
        # dropped the node's order to make way for new content.
        self._pending_send: Optional[str] = None
        # What each cancelOrder in flight is for, and the run it was sent in (see
        # _act_on_resolved_cancels), and the ones the robot has just resolved.
        self._cancel_purposes: Dict[str, Tuple[CancelPurpose, Optional[str]]] = {}
        self._resolved_cancels: List[Tuple[CancelPurpose, Optional[str], bool]] = []
        # Instant action ids carry it, so they never repeat an earlier process's.
        self._process_tag = uuid.uuid4().hex[:4]
        # The orderId of the robot's last state message.
        self._robot_order_id: Optional[str] = None
        # Whether that state message still listed nodes or edges: the robot is executing.
        self._robot_executing = False
        # A reroute's cancel held back until the robot has adopted the order version just
        # sent (see _replace_cancel_must_wait).
        self._deferred_replace_cancel = False
        # The orderId the robot dropped for a blocked node (None: not known, e.g. after a
        # restart): a "canceled" about it keeps the block (see update_mission_state).
        self._blocked_order_id: Optional[str] = None
        # Node reports of the current run already taken, by (kind, nodeId, infoType): the
        # robot repeats them in every state. A note's entry is the note (for last_seen).
        self._node_reports_seen: Dict[Tuple[str, str, str], Any] = {}
        # Monotonic times of the current run's recent order revisions (churn breaker).
        self._order_revisions: Deque[float] = deque()
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
        self._charging_mission_received: bool = False
        self._charging_hook_busy: bool = False
        self._charging_hook_next_at: float = 0.0
        self._notify_in_flight: bool = False
        self._notify_task: Optional[asyncio.Task[Any]] = None
        # Same key shape as _wait_key, while a notify node runs (a mismatch is expected
        # then, like during a wait) and until its NotifyDone is handled.
        self._notify_key: Optional[Tuple[str, str, Optional[str], int, int]] = None
        self.last_node_seq_id: int = -1
        # Timestamp of the robot state message being handled, for the events it causes
        # (see _record); None outside that.
        self._event_ts: Optional[datetime.datetime] = None
        # Maps §14 U3: a new robot run (VDA5050 header ids restart) unplaces its session; a
        # datum re-places a geo one. `_datum_epoch`: the dispatcher's MQTT connection epoch in
        # which this robot's last datum arrived (the first datum of an epoch may be the
        # retained one of an older run).
        self._run_detector = run_change.RunChangeDetector()
        self._datum_epoch: Optional[int] = None
        # The robot's connection went OFFLINE/CONNECTIONBROKEN and has not been ONLINE since;
        # and the pose before a state-header drop that awaits confirmation (run_change.py).
        self._connection_down: bool = False
        self._pre_drop_pose: Any = None
        # For _reconcile_stale_state's grace period.
        self._created_at = time.monotonic()
        # Maps §14.13: the robot's run epoch (robot_run_epochs). `_run_checked`: the first
        # state message of this process decided it (continuity proved, or a new epoch);
        # `_run_header_saved_at`: when the last state headerId was stored (monotonic).
        self._run_checked = False
        self._run_header_task: Optional["asyncio.Task[Any]"] = None  # single-flight header persist
        self._run_check_after = 0.0  # monotonic; a failed check is retried after RUN_CHECK_RETRY_S
        self._run_epoch: Optional[uuid.UUID] = None
        self._run_header_saved_at: Optional[float] = None

        if self._robot_server.push_telemetry:
            self._telemetry = metrics.Telemetry()
            self._telemetry_client = TelemetrySender(
                self._robot_server.telemetry_env)
        # To calculate the durition of a robot state
        self._cur_robot_state_timestamp = datetime.datetime.now()
        self._run_task: Optional[asyncio.Task[Any]] = \
            asyncio.get_event_loop().create_task(self.run())

    def shutdown(self):
        """Tear this controller down (the robot row is gone): stop its message loop and every
        timer it owns, so nothing of it outlives the robot or leaks into a robot registered
        again under the same name (that one gets a brand-new Robot). Idempotent."""
        self._alive = False
        # Queued status writes (a mission's final state above all) get a bounded chance to
        # land, then are cancelled; no retries once shut down.
        if self._status_write_tasks:
            try:
                flush = asyncio.get_running_loop().create_task(
                    self.flush_status_writes(STATUS_WRITE_FLUSH_S))
            except RuntimeError:
                flush = None
                for task in list(self._status_write_tasks):
                    task.cancel()
            if flush is not None:
                self._status_flush_tasks.add(flush)
                flush.add_done_callback(self._status_flush_tasks.discard)
        for task in (self._robot_online_task, self._mission_timeout_task, self._wait_task,
                     self._run_task, self._robot_write_timer, self._notify_task,
                     *self._background_tasks):
            if task is not None and task is not asyncio.current_task():
                task.cancel()
        self._notify_task = None
        self._robot_write_timer = None
        self._robot_online_task = None
        self._mission_timeout_task = None
        self._wait_task = None
        self._run_task = None

    async def _try_start_mission(self):
        # Nothing is picked before the robot is known: a restarted dispatcher sees its
        # missions in an arbitrary order and the robot row after them, and the first mission
        # to arrive must not take the slot of one that was already running.
        if self._current_mission is None and self._missions and \
                self._robot_object is not None and \
                self._robot_object.lifecycle is api_objects.object.ObjectLifecycleV1.ALIVE:
            # Schedule a new mission if we aren't doing anything and there is one in the
            # queue: a mission that already started (a resume after a restart) goes first.
            self._current_mission = next(
                (m for m in self._missions.values() if m.status.start_timestamp is not None),
                next(iter(self._missions.values())))
            # Fresh mission, fresh mismatch budget -- the previous mission's leftover
            # count must not shorten this one's grace period -- and nothing of the
            # previous mission's orders.
            self._order_mismatch_count = 0
            self._start_attempts = 0
            # Not dispatched until its own tree is built: the previous mission's tree
            # made a held mission look dispatched (its state handling then sent orders).
            self._current_behavior_tree = None
            self._sent_order = None
            self._unknown_content_order_id = None
            self._order_revisions.clear()
            self._resume_pending = False
            self._pending_send = None
            self._deferred_replace_cancel = False
            self._blocked_order_id = None
            self._node_reports_seen.clear()

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
            await self._cancel_before_dispatch()
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
                self._queue_status_write(api_objects.MissionObjectV1, self._current_mission.name, self._current_mission.status, self._mission_writer_id())
            return
        if self._current_mission.status.held:
            self._current_mission.status.held = False
            self._current_mission.status.held_reason = None
            self.mission_info("Robot ready — releasing held mission")
            self._queue_status_write(api_objects.MissionObjectV1, self._current_mission.name, self._current_mission.status, self._mission_writer_id())
        resumed = self._current_mission.status.start_timestamp is not None
        await self._settle_route_rev()
        await self._replan_goto()
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

        session = await self._read_open_session()
        # The run's map is the session's map (maps §14.6); null when mapless or unreadable.
        session_map = session["map_name"] if isinstance(session, dict) else None
        self._record("run_started", self._name, self._current_mission, self._robot_object,
                     session_map=session_map)
        self.update_mission_from_behavior_tree()
        if self._current_mission.status.state.done:
            # A resumed mission whose tree is already finished (every node done before the
            # restart): record it and move on -- there is nothing left to send, and the
            # next order would be a node that no longer runs.
            self.mission_info("Mission already finished at resume; not sending an order")
            await self.post_mission_completion()
            return
        # A resume gets what is left of the timeout, not a fresh one (a crash loop would
        # extend the mission for ever). Time the robot spent offline before the restart
        # is not persisted, so it counts as elapsed.
        self._arm_mission_timeout(
            self._remaining_timeout_s(self._current_mission) if resumed else None)
        if resumed:
            # This process does not know what the robot holds, nor what content went out
            # under the current orderId: the robot's first state message decides.
            self._resume_pending = True
            self._unknown_content_order_id = self._current_order_id()
            self.mission_info("Resumed; waiting for the robot's state before sending")
            return
        await self._send_order()

    async def _retry_stalled_start(self):
        """Start again a current mission whose start did not get as far as its tree (an
        exception in _try_start_mission is swallowed by the message loop and left it so),
        and pick up a queued mission when the robot has none. At most every START_RETRY_S;
        a mission whose start keeps raising is failed after MAX_START_ATTEMPTS."""
        mission = self._current_mission
        if mission is None:
            if not self._missions:
                return
        elif self._current_behavior_tree is not None or mission.status.held or \
                mission.status.state.done:
            return
        now = time.monotonic()
        if now < self._start_retry_at:
            return
        self._start_retry_at = now + START_RETRY_S
        try:
            await self._try_start_mission()
        except asyncio.CancelledError:
            raise
        except Exception as err:  # pylint: disable=broad-except
            self._start_attempts += 1
            self.warning(f"Could not start the mission (attempt {self._start_attempts} of "
                         f"{MAX_START_ATTEMPTS}): {err}")
            mission = self._current_mission
            if mission is not None and self._start_attempts >= MAX_START_ATTEMPTS:
                mission.status.failure_reason = f"Could not start the mission: {err}"
                self._set_mission_state(mission_object.MissionStateV1.FAILED)
                try:
                    await self.post_mission_completion()
                except asyncio.CancelledError:
                    raise
                except Exception as next_err:  # pylint: disable=broad-except
                    # The next mission's start failing too is for its own retries.
                    self.warning(f"Could not move on after the failed start: {next_err}")

    async def _replan_goto(self):
        """A go-to was planned when it was submitted, from where the robot stood then; when it
        waited behind another mission the robot is elsewhere by now, and the stored route
        would first drive it back there. Replan it from the robot's current pose just before
        its first order, and store the new route.

        Never blocks the mission: the planner being down, slow, or unable to plan only logs a
        warning, and the stored plan is used. Only a go-to (kind "goto" with a goal) that has
        not started yet is replanned."""
        mission = self._current_mission
        goal = mission.goal
        if mission.kind != "goto" or not isinstance(goal, dict) or \
                mission.status.start_timestamp is not None or self._robot_object is None:
            return
        planner = getattr(self._robot_server, "mission_planner", None)
        route_node = next((n for n in mission.mission_tree
                           if n.type == mission_object.MissionNodeType.ROUTE), None)
        if planner is None or route_node is None or goal.get("x") is None or \
                goal.get("y") is None:
            return
        pose = self._robot_object.status.pose
        try:
            plan = await planner.plan(
                robot_name=self._name, target_x=goal["x"], target_y=goal["y"],
                map_id=goal.get("map_id"), robot_x=pose.x, robot_y=pose.y)
            if not plan.get("success") or not plan.get("waypoints"):
                raise RuntimeError(plan.get("error") or "the planner returned no route")
            route = mission_object.MissionRouteNodeV1(waypoints=plan["waypoints"])
        except Exception as err:  # pylint: disable=broad-except
            self.warning(f"[{mission.name}] Could not replan the go-to ({err}); "
                         "using the route stored when it was submitted")
            return
        route_node.route = route
        mission.planned_path = plan.get("planned_path")
        # The new route is the one being sent: a reroute revision the dispatcher has
        # already applied, so the row coming back does not reroute it again.
        mission.route_rev += 1
        mission.status.applied_route_rev = mission.route_rev
        self.mission_info(f"Replanned the go-to from the robot's pose "
                          f"({pose.x:.2f}, {pose.y:.2f}): {len(route.waypoints)} waypoints")
        try:
            await self._database.update_spec_fields(
                api_objects.MissionObjectV1, mission.name,
                {"mission_tree": json.loads(mission.spec.json())["mission_tree"],
                 "planned_path": mission.planned_path, "route_rev": mission.route_rev},
                self._mission_writer_id())
        except Exception as err:  # pylint: disable=broad-except
            self.warning(f"[{mission.name}] Could not store the replanned route ({err})")

    async def _settle_route_rev(self):
        """The tree this mission is about to be sent from is the stored one, so every reroute
        up to the spec's route_rev is in it: remember that (applied_route_rev), so the row is
        not taken for a new reroute. A mission resumed after a restart may have been
        rerouted while the robot still held the old route under the current order id; the
        resume then does not adopt it (status.sent_order records the route it was built
        from; see _resume_from_state)."""
        mission = self._current_mission
        status = mission.status
        # A node left CANCELED by a reroute that was in flight when the dispatcher stopped
        # (older versions persisted it) is a node still to run, not a failed one.
        if status.start_timestamp is not None and not mission.needs_canceled:
            for node_state in status.node_status.values():
                if node_state.state is mission_object.MissionStateV1.CANCELED:
                    node_state.state = mission_object.MissionStateV1.PENDING
        if mission.route_rev <= status.applied_route_rev:
            return
        status.applied_route_rev = mission.route_rev
        # Waypoint progress was counted on the old routes.
        for name, node_state in status.node_status.items():
            if not node_state.state.done:
                status.task_status.pop(name, None)
        if status.start_timestamp is None or status.run_id is None:
            return  # persisted with the run id
        try:
            await self._persist_current_mission_status()
        except Exception as err:  # pylint: disable=broad-except
            self.warning(f"[{mission.name}] Could not persist the applied route revision "
                         f"({err})")
        self.mission_info(f"Resuming a rerouted mission: route revision {mission.route_rev}")

    def _order_may_be_on_robot(self) -> bool:
        """Whether an order of the current mission may have gone out before (a run id is
        persisted before the first send; a start timestamp survives a dispatcher restart)."""
        status = self._current_mission.status
        return status.start_timestamp is not None or status.run_id is not None or \
            status.order_rev > 0

    def _robot_runs_our_order(self) -> bool:
        """Whether the robot's last state reported an order of the current run (any
        revision) that it still executes. An order the dispatcher did not issue (the
        robot's own offline mission) never counts."""
        return self._current_mission is not None and self._robot_executing and \
            self._is_order_of_run(self._robot_order_id)

    def _robot_runs_dispatcher_order(self) -> bool:
        """Whether the robot's last state reported an order with an id a dispatcher generates
        (any mission, run or revision) that it still executes: not the robot's own."""
        return self._robot_executing and self._robot_order_id is not None and \
            order_ids.order_prefix(self._robot_order_id) is not None

    def _robot_may_hold_our_order(self) -> bool:
        """Whether the robot may hold an order of this run: it runs one, or an order went
        out and the robot is not demonstrably executing something else (an order it has
        not reported yet, or one it dropped). An order that is not the run's, and that the
        robot executes, is not ours (an earlier run's, a dispatcher's other mission, the
        robot's own)."""
        if self._robot_runs_our_order():
            return True
        return self._current_mission is not None and self._sent_order is not None and \
            not self._robot_executing

    async def _cancel_order(self, purpose: CancelPurpose, tag: str, note: str, *,
                            ours: str = OURS_RUN, current_run_only: bool = False) -> bool:
        """The one place a cancelOrder of the current mission is sent. Returns whether it was.

        One cancel at a time: nothing is sent while one is outstanding (with
        `current_run_only`, only one of this run counts: a mission cancel is not held back
        by an earlier run's timeout cancel). `ours` is what the robot must be reporting:
        - OURS_RUN: an order of this run, any revision, that it executes. The default, for
          a mission that ends on a state the robot reported (failed, mismatch, churn): a
          robot idle, or on an order the dispatcher did not issue (its own offline
          mission), or on another run's, is left alone.
        - OURS_SENT: OURS_RUN, or an order of the run went out and the robot is not
          executing another (_robot_may_hold_our_order). For the timeout and a deleted
          robot, which cancelled whenever an order had been sent: the order may be one the
          robot has not reported yet, and a cancel to an idle robot only draws noOrderToCancel.
        - OURS_DISPATCHER: any order a dispatcher generated (the resume after a restart:
          the robot may hold an earlier run's order of the same mission), never the robot's
          own.
        - OURS_ANY: no check, for a caller that decided already: an order just sent that
          the robot has not reported yet, or the operator's force cancel.
        The action id is "{prefix}-{tag}-cancel" (without a mission: "{tag}-cancel")."""
        if self._has_outstanding_cancel(current_run_only=current_run_only):
            return False
        if ours == OURS_RUN and not self._robot_runs_our_order():
            return False
        if ours == OURS_SENT and not self._robot_may_hold_our_order():
            return False
        if ours == OURS_DISPATCHER and not self._robot_runs_dispatcher_order():
            return False
        stem = f"{tag}-cancel" if self._current_mission is None \
            else f"{self._order_prefix()}-{tag}-cancel"
        action_id = self._action_id(stem)
        self.mission_info(f"{note} {action_id}")
        await self._send_cancel_order(action_id, purpose)
        return True

    async def _stop_our_order(self, why: str) -> None:
        """A mission ends without the robot's say (failed, mismatch, churn): drop its order from
        the robot if it still runs one of this run, as the timeout does. One cancel at a
        time; a foreign order is left alone."""
        await self._cancel_order(
            CancelPurpose.STOP, why,
            f"Mission ended ({why}) while the robot runs {self._robot_order_id}: "
            "sending cancelOrder")

    async def _cancel_before_dispatch(self) -> bool:
        """End the current mission as CANCELED when it is flagged for cancel and its tree
        is not built. Returns whether it ended.

        Never dispatched (robot offline or not ready): nothing is on the robot, so it ends
        at once without a cancelOrder. A mission whose order may be on the robot (it started
        before a dispatcher restart, or was held after it started) is cancelled as a running
        one is: a cancelOrder (purpose MISSION) once the robot's state shows it runs an
        order of this run, and the mission ends on its confirmation
        (_act_on_resolved_cancels). Until the robot has reported (just restarted) or while
        it is offline, the mission stays pending: a blind cancel could take the robot's own
        offline mission, and a CANCELED mission with the order still running misleads the
        next one. The robot's next state message retries (_on_client_message)."""
        mission = self._current_mission
        if self._order_may_be_on_robot():
            if self._has_outstanding_cancel(current_run_only=True):
                return False
            unknown = self._robot_order_id is None
            if unknown or self._robot_runs_our_order():
                if unknown or self._robot_object is None or \
                        not self._robot_object.status.online:
                    self.mission_info("Mission flagged for cancel: waiting for the robot's "
                                      "state before cancelling its order")
                    return False
                self._record("run_started", self._name, mission, self._robot_object)
                await self._cancel_order(
                    CancelPurpose.MISSION, "mission", "Mission flagged for cancel after its "
                    "order went out: send cancel order action", current_run_only=True)
                return False
        self.mission_info("Mission flagged for cancel before dispatch -- canceling immediately")
        # A mission that was already running before a dispatcher restart still has a
        # RUNNING run row (startup reconciliation leaves it for us to resume): adopt it
        # so it is closed too, instead of staying RUNNING forever (observed 2026-09-25).
        resumed = mission.status.start_timestamp is not None
        if resumed:
            self._record("run_started", self._name, mission, self._robot_object)
        self._set_mission_state(mission_object.MissionStateV1.CANCELED)
        if resumed:
            self._record("run_finished", self._name, mission, self._robot_object)
        await self.get_next_mission()
        return True

    def _record(self, hook: str, *args: Any, **kwargs: Any) -> None:
        """Phase 0 recording (fleet_recorder.FleetRecorder). The hooks only enqueue work and
        never raise, and this guard makes sure of it: recording must never change what the
        dispatcher does."""
        recorder = getattr(self._robot_server, "fleet_recorder", None)
        if recorder is None:
            return
        try:
            getattr(recorder, hook)(*args, **kwargs)
        except Exception:  # pylint: disable=broad-except
            self._logger.exception("Fleet recording hook %s failed (ignored)", hook)

    def _dispatch_hold_reason(self) -> Optional[str]:
        """None if the robot can receive a dispatched order right now; otherwise a
        human-readable reason dispatch should be withheld."""
        if self._robot_object is None or not self._robot_object.status.online:
            return "Robot is offline"
        errors = self._robot_object.status.errors
        for error_type, reason in READINESS_HOLD_REASONS.items():
            if error_type in errors:
                return reason
        return None

    def _has_outstanding_cancel(self, current_run_only: bool = False) -> bool:
        """True while a cancelOrder we sent has not yet been reported FINISHED (or
        abandoned) -- see handle_instant_action() for how entries leave the dict. With
        `current_run_only`, only one sent in the current mission run counts."""
        run = self._run_key()
        return any(a.actionType == types.VDA5050InstantActionType.CANCEL_ORDER and
                   (not current_run_only or
                    self._cancel_purposes.get(action_id, (None, None))[1] == run)
                   for action_id, a in self._current_instant_actions.items())

    def _replace_cancel_must_wait(self) -> bool:
        """Whether a reroute's cancelOrder must wait: the order version just sent has not
        been reported by the robot yet, and ORDER_CANCEL_MIN_DWELL_S has not passed since it
        went out (2026-10-08: order versions cancelled right after they were sent)."""
        if self._sent_order is None or self._robot_order_id == self._sent_order.orderId:
            return False
        # From the first send: resends of the same order do not restart the dwell.
        return time.monotonic() - self._order_first_sent_at < \
            order_policy.current().cancel_min_dwell_s

    async def _send_cancel_order(self, action_id: str,
                                 purpose: Optional[CancelPurpose] = None):
        """Send a VDA5050 cancelOrder and track it in _current_instant_actions, so
        _has_outstanding_cancel() sees it and handle_instant_action() resends it
        until the robot reports it FINISHED (or it is abandoned). `purpose` (by default:
        the mission's cancel if it is being cancelled, else a stop) and the current run
        are kept with it, for what its completion means."""
        if purpose is None:
            purpose = CancelPurpose.MISSION if self._current_mission is not None and \
                self._current_mission.needs_canceled else CancelPurpose.STOP
        instant_action = types.VDA5050Action(
            actionType=types.VDA5050InstantActionType.CANCEL_ORDER,
            actionId=action_id)
        await self._send_instant_action(instant_action)
        self._current_instant_actions[action_id] = instant_action
        self._cancel_purposes[action_id] = (purpose, self._run_key())

    def _run_key(self) -> Optional[str]:
        """The current mission's run (one pass of it), None without a mission."""
        mission = self._current_mission
        return None if mission is None else f"{mission.name}|{mission.status.run_id}"

    def _cancel_resolved(self, action: types.VDA5050Action, abandoned: bool = False):
        """A cancelOrder of ours left _current_instant_actions: the robot finished or
        rejected it, or it was abandoned unacknowledged. Queued for
        _act_on_resolved_cancels."""
        if action.actionType != types.VDA5050InstantActionType.CANCEL_ORDER:
            return
        purpose, run = self._cancel_purposes.pop(action.actionId, (CancelPurpose.STOP, None))
        self._resolved_cancels.append((purpose, run, abandoned))

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
        # Only this key: writing the cached full spec back could revert a spec change
        # another service committed meanwhile (e.g. telemetry_recording).
        await self._database.update_spec_fields(
            api_objects.RobotObjectV1, message.name, {"needs_order_cancel": False},
            uuid.uuid4())
        # It ends the mission if it takes the mission's order: the robot is on it, or has
        # not reported since one went out. A stray order the mission waits behind is
        # just cleared. Blind on purpose (OURS_ANY): the operator's escape hatch clears
        # whatever the robot holds, the robot's own order included.
        on_mission = self._current_mission is not None and (
            order_ids.is_order_of(self._order_prefix(), self._robot_order_id)
            if self._robot_order_id is not None else self._sent_order is not None)
        if not await self._cancel_order(
                CancelPurpose.STOP if on_mission else CancelPurpose.CLEAR, "force",
                "Force-cancel requested: sending", ours=OURS_ANY):
            self.info("Force-cancel requested, but a cancelOrder is already "
                      "outstanding; not sending another")

    async def _send_instant_action(self, instant_action: types.VDA5050Action):
        instant_actions = types.VDA5050InstantActions(
            headerId=self._next_header_id("instantActions"),
            timestamp=types.utc_timestamp(),
            manufacturer=self._manufacturer, serialNumber=self._name,
            instantActions=[instant_action])
        self._mqtt_client.publish(f"{self._mqtt_prefix}/{self._name}/instantActions",
                                  instant_actions.json())

    def _next_header_id(self, topic: str) -> int:
        """VDA5050: a headerId is defined per topic and incremented by one with each
        message sent on that topic."""
        header_id = self._header_ids.get(topic, self._header_start)
        self._header_ids[topic] = header_id + 1
        return header_id

    def _action_id(self, stem: str) -> str:
        """A new instant action id: the stem and the headerId its message will carry."""
        return f"{stem}-{self._process_tag}-n{self._header_ids.get('instantActions', self._header_start)}"

    def _order_prefix(self) -> str:
        """Prefix of every order/node id generated for the current mission's run and
        revision (see order_ids)."""
        status = self._current_mission.status
        return order_ids.run_prefix(str(self._current_mission.name),
                                    status.run_id, status.order_rev)

    async def _persist_current_mission_status(self):
        await self._write_status(
            api_objects.MissionObjectV1, self._current_mission.name,
            self._current_mission.status, self._mission_writer_id())

    def _status_row(self, key: Tuple[str, str]) -> "_StatusRow":
        row = self._status_rows.get(key)
        if row is None:
            row = self._status_rows[key] = _StatusRow()
        row.users += 1
        return row

    def _status_row_release(self, key: Tuple[str, str], row: "_StatusRow") -> None:
        row.users -= 1
        if row.users <= 0 and self._status_rows.get(key) is row:
            del self._status_rows[key]

    async def _write_status(self, object_class, name: str, status, writer_id: uuid.UUID):
        """Awaited status write that keeps its failure for the caller, but takes the row's
        lock first so it cannot commit out of order with a queued write of the same row
        (each write serializes the shared, mutable status when it starts)."""
        key = (object_class.table_name(), str(name))
        if object_class is api_objects.RobotObjectV1:
            self._robot_written_sig = None      # the next state message writes the row again
        row = self._status_row(key)
        row.seq += 1
        seq = row.seq
        try:
            async with row.lock:
                if row.written_seq > seq:
                    return   # a newer request of this row has landed meanwhile
                await self._database.update_status(object_class, name, status, writer_id)
                row.written_seq = max(row.written_seq, seq)
        finally:
            self._status_row_release(key, row)

    def _queue_status_write(self, object_class, name: str, status, writer_id: uuid.UUID) -> None:
        """Fire-and-forget status write that is neither lost nor reordered. At most one write
        per row is in flight; a request made meanwhile replaces the one waiting (every write
        stores the whole status, read when the write starts, so the newest is enough and the
        row's commits stay in order). A failed write is retried with a capped back-off until
        it lands, the row is gone (HTTP 404/400) or the controller is shut down."""
        if not self._alive:
            return
        key = (object_class.table_name(), str(name))
        if object_class is api_objects.RobotObjectV1:
            self._robot_written_sig = None      # the next state message writes the row again
        row = self._status_row(key)
        row.seq += 1
        row.pending = (object_class, name, status, writer_id, row.seq)
        if row.task is not None:
            self._status_row_release(key, row)
            return
        row.task = asyncio.ensure_future(self._status_write_loop(key, row))
        self._status_write_tasks.add(row.task)
        row.task.add_done_callback(self._status_write_done)

    def _status_write_done(self, task: "asyncio.Task[Any]") -> None:
        self._status_write_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            self.error(f"Status write task failed: {task.exception()!r}")

    async def _status_write_loop(self, key: Tuple[str, str], row: "_StatusRow") -> None:
        failures = 0
        backoff = STATUS_WRITE_RETRY_MIN_S
        try:
            while row.pending is not None:
                request = row.pending
                row.pending = None
                object_class, name, status, writer_id, seq = request
                try:
                    async with row.lock:
                        if seq < row.written_seq:
                            # An awaited write requested after this one landed meanwhile
                            # (with a newer status object): this one is stale.
                            continue
                        await self._database.update_status(
                            object_class, name, status, writer_id)
                        row.written_seq = max(row.written_seq, seq)
                except asyncio.CancelledError:
                    raise
                except fastapi.HTTPException as err:
                    # 404/400: the row is gone, writing it again cannot succeed.
                    self.warning(f"Status of {key[0]} {name} not stored, the row is gone "
                                 f"({err.status_code})")
                    row.pending = None
                    return
                except Exception as err:  # pylint: disable=broad-except
                    if not isinstance(err, STATUS_WRITE_TRANSIENT_ERRORS):
                        # A programming error: retrying cannot help, and would pin this row.
                        self.error(f"Status of {key[0]} {name} not stored, dropped (not a "
                                   f"database failure): {err!r}", exc_info=True)
                        continue
                    failures += 1
                    if row.pending is None:
                        row.pending = request
                    if not self._alive:
                        # Shut down: the write was tried once; no more retries.
                        self.warning(f"Status of {key[0]} {name} not stored at shutdown: "
                                     f"{err!r}")
                        row.pending = None
                        return
                    text = f"Could not store the status of {key[0]} {name} " \
                           f"(failure {failures}): {err!r}; retrying in {backoff:g}s"
                    if failures >= STATUS_WRITE_ERROR_AFTER:
                        self.error(text)
                    elif failures == 1:
                        self.warning(text)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, STATUS_WRITE_RETRY_MAX_S)
                    continue
                if failures:
                    self.info(f"Status of {key[0]} {name} stored after {failures} failure(s)")
                failures = 0
                backoff = STATUS_WRITE_RETRY_MIN_S
        finally:
            row.task = None
            self._status_row_release(key, row)

    async def wait_shutdown_flush(self) -> None:
        """After shutdown(): wait for the bounded status-write flush it started (graceful
        shutdown awaits this before the database pools close)."""
        flushes = list(self._status_flush_tasks)
        if flushes:
            await asyncio.wait(flushes)

    async def flush_status_writes(self, timeout_s: float = STATUS_WRITE_FLUSH_S) -> None:
        """Give the queued status writes a bounded chance to land (graceful shutdown), then
        cancel what is left."""
        tasks = list(self._status_write_tasks)
        if not tasks:
            return
        _, pending = await asyncio.wait(tasks, timeout=timeout_s)
        for task in pending:
            self.warning("Status write dropped at shutdown")
            task.cancel()

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

        Every new revision of a dispatched run goes through here, so this is where churn
        is stopped: more than ORDER_CHURN_MAX_REVISIONS within ORDER_CHURN_WINDOW_S fails
        the mission instead (2026-10-08: 854 revisions in 9 minutes). Callers bump only
        once the robot holds no order of ours, so there is nothing left to cancel.

        A legacy mission (running since before run ids existed) gets its run id here
        instead: the new order needs an id the robot has not seen as much as any other.
        Returns False if no new revision was made; the caller must not resend. It retries
        later unless the mission is done.
        """
        status = self._current_mission.status
        now = time.monotonic()
        while self._order_revisions and \
                now - self._order_revisions[0] > self.ORDER_CHURN_WINDOW_S:
            self._order_revisions.popleft()
        if len(self._order_revisions) >= self.ORDER_CHURN_MAX_REVISIONS:
            self._stop_order_churn(len(self._order_revisions) + 1)
            await self._stop_our_order("churn")  # nothing to do unless it still runs one
            return False
        legacy = status.run_id is None
        if legacy:
            status.run_id = uuid.uuid4().hex[:8]
        else:
            status.order_rev += 1
        try:
            await self._persist_current_mission_status()
        except Exception as err:  # pylint: disable=broad-except
            if legacy:
                status.run_id = None
            else:
                status.order_rev -= 1
            self.warning(f"[{self._current_mission.name}] Could not persist order "
                         f"revision ({err}); not resending yet")
            return False
        self._order_revisions.append(now)
        self._unknown_content_order_id = None
        self.mission_info(f"Run id {status.run_id} (was a legacy mission)" if legacy
                          else f"Order revision {status.order_rev}")
        return True

    def _stop_order_churn(self, revisions: int):
        """Fail the current mission rather than re-issue its order once more, and raise
        MISSION.ORDER_CHURN. The robot holds no order of ours at this point (see
        _bump_order_rev); the next state message moves the queue on."""
        mission = self._current_mission
        window = self.ORDER_CHURN_WINDOW_S
        mission.status.failure_reason = \
            (f"Order re-issued {revisions} times within {window:.0f} s; stopped re-issuing "
             "it (order churn)")
        self.warning(f"[{mission.name}] {mission.status.failure_reason}")
        self._pending_send = None
        self._resume_pending = False
        self._set_mission_state(mission_object.MissionStateV1.FAILED)
        self._record("order_churn", self._name, mission, self._current_order_id(),
                     revisions, window, self._event_ts)

    async def _send_order(self, waypoint_offset: int = 0):
        """Send the order of the current behavior-tree node. An order already sent under
        the same orderId is republished unchanged; `waypoint_offset` (a resume) leaves the
        route's first waypoints out of a newly built order."""
        if self._robot_object is None or self._robot_object.lifecycle \
            not in [api_objects.object.ObjectLifecycleV1.ALIVE,
                    api_objects.object.ObjectLifecycleV1.PENDING_DELETE]:
            return
        if self._current_mission is None or self._current_behavior_tree is None:
            return
        # A finished mission (e.g. failed for order churn), or one being cancelled, sends
        # nothing more.
        if self._current_mission.status.state.done or self._current_mission.needs_canceled:
            return
        # Never while a cancelOrder of ours is in flight: the robot would reject the order
        # while it still runs one, or the cancel would take it. Owed until it resolves.
        if self._has_outstanding_cancel():
            self._pending_send = self._pending_send or SEND_NODE
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
                if self._notify_task is None or self._notify_task.done():
                    # Marked before the task runs: a state message in between must not
                    # count a mismatch either.
                    self._notify_key = self._node_key(mission_node)
                    # A task, not an await: this robot's message loop (state handling, the
                    # online watchdog, a cancel) must keep running while the webhook retries.
                    self._notify_task = asyncio.ensure_future(
                        self._process_notify_node(mission_node))
                return

            # A wait is a timer the dispatcher runs itself; the robot has no order for it.
            if mission_node.type == mission_object.MissionNodeType.ACTION and \
                    mission_node.action is not None and \
                    mission_node.action.action_type == mission_object.WAIT_ACTION_TYPE:
                self._start_wait(mission_node)
                return

            # An earlier dispatcher process may have sent this orderId with content this
            # one does not know (see _resume_from_state): move to a new revision instead.
            if order_ids.order_id(self._order_prefix(), idx) == \
                    self._unknown_content_order_id and not await self._bump_order_rev():
                if not self._current_mission.status.state.done:
                    self._pending_send = self._pending_send or SEND_NODE
                return
            order_id = order_ids.order_id(self._order_prefix(), idx)
            if self._sent_order is not None and self._sent_order.orderId == order_id:
                # Not adopted yet: the same order again, unchanged (its start node is not
                # re-taken from where the robot stands now).
                order = self._sent_order
                self._order_resends += 1
                self.mission_info(f"Resending order {order_id} ({self._order_resends})")
            else:
                built = await self._build_order(mission_node, idx, waypoint_offset)
                if built is None:
                    return
                order, record = built
                # Stored before it goes out, so the robot's reports on it are read right,
                # also by a dispatcher restarted meanwhile. Not stored: owed, not sent.
                if not await self._persist_sent_order(record):
                    self._pending_send = self._pending_send or SEND_NODE
                    return
                self._sent_order = order
                self._order_resends = 0
                self._order_first_sent_at = time.monotonic()

            order.headerId = self._next_header_id("order")
            order.timestamp = types.utc_timestamp()
            order.manufacturer = self._manufacturer
            order.serialNumber = self._name
            self._order_sent_at = time.monotonic()

            self._mqtt_client.publish(
                f"{self._mqtt_prefix}/{self._name}/order", order.json())
            self.set_mission_node_state(f"{mission_node.name}",
                                        mission_object.MissionStateV1.RUNNING)

    def _sends_node_policy(self) -> bool:
        """Whether route orders to this robot carry nodePolicy actions: per
        VDA5050_NODE_POLICY_MODE, by default only to a robot whose factsheet lists the
        action (one that does not know it may reject the order)."""
        mode = order_policy.current().node_policy_mode
        if mode == order_policy.NodePolicyMode.ON:
            return True
        if mode != order_policy.NodePolicyMode.FACTSHEET or self._robot_object is None:
            return False
        return any(a.action_type == types.NODE_POLICY_ACTION_TYPE
                   for a in self._robot_object.status.factsheet.custom_actions or [])

    async def _build_order(self, mission_node: mission_object.MissionNodeV1, idx: int,
                           waypoint_offset: int) \
            -> Optional[Tuple[types.VDA5050Order, mission_object.MissionSentOrderV1]]:
        """A new order for `mission_node` and what it is built from, or None when there is
        none to send. A route order leaves its first `waypoint_offset` waypoints out."""
        order_id = order_ids.order_id(self._order_prefix(), idx)
        if mission_node.type == mission_object.MissionNodeType.ROUTE and \
                mission_node.route is not None:
            route = mission_node.route
            offset = min(max(waypoint_offset, 0), len(route.waypoints) - 1)
            record = mission_object.MissionSentOrderV1(
                order_id=order_id, route_digest=_route_digest(route), waypoint_offset=offset)
            if offset:
                route = route.copy(update={"waypoints": route.waypoints[offset:]})
            try:
                route = await self._route_in_robot_frame(route)
            except RouteRefused as err:
                self._refuse_route_node(mission_node, str(err))
                return None
            record.frame = self._last_route_frame
            self.mission_info(f"Sending mission route node {mission_node.name}"
                              f"{f' from waypoint {offset}' if offset else ''}")
            return types.VDA5050Order.from_route(route, self._robot_object,
                                                 self._order_prefix(), idx,
                                                 node_policy=self._sends_node_policy()), \
                record
        record = mission_object.MissionSentOrderV1(order_id=order_id)
        if mission_node.type == mission_object.MissionNodeType.MOVE and \
                mission_node.move is not None:
            self.mission_info(f"Sending mission move node {mission_node.name}")
            return types.VDA5050Order.from_move(mission_node.move, self._robot_object,
                                                self._order_prefix(), idx), record
        if mission_node.type == mission_object.MissionNodeType.ACTION and \
                mission_node.action is not None:
            self.mission_info(f"Sending mission action node {mission_node.name}")
            return types.VDA5050Order.from_action(mission_node.action, self._robot_object,
                                                  self._order_prefix(), idx), record
        return None

    async def _persist_sent_order(self, record: mission_object.MissionSentOrderV1) -> bool:
        """Store what the order about to be sent is built from. Returns False if it could
        not be stored."""
        status = self._current_mission.status
        previous, status.sent_order = status.sent_order, record
        try:
            await self._persist_current_mission_status()
        except Exception as err:  # pylint: disable=broad-except
            status.sent_order = previous
            self.warning(f"[{self._current_mission.name}] Could not persist the order "
                         f"about to be sent ({err}); not sending it yet")
            return False
        return True

    def _sent_order_record(self, order_id: Optional[str]) \
            -> Optional[mission_object.MissionSentOrderV1]:
        """What order `order_id` was built from, if it is the last one built."""
        if self._current_mission is None or not order_id:
            return None
        record = self._current_mission.status.sent_order
        return record if record is not None and record.order_id == order_id else None

    def _waypoint_offset(self, order_id: Optional[str]) -> int:
        """Index in its route node of the first waypoint of order `order_id`."""
        record = self._sent_order_record(order_id)
        return 0 if record is None else record.waypoint_offset

    def _order_route_is_current(self, order_id: Optional[str],
                                node: mission_object.MissionNodeV1) -> bool:
        """Whether order `order_id` of route node `node` was built from the node's current
        route, not one a reroute has replaced since. Not known (an order not recorded, or
        recorded before records existed) counts as current."""
        record = self._sent_order_record(order_id)
        if record is None or record.route_digest is None or node.route is None:
            return True
        return record.route_digest == _route_digest(node.route)

    def _current_order_id(self) -> Optional[str]:
        """The orderId of the current behavior-tree node, if it is a leaf."""
        node = self._current_behavior_tree.current_node \
            if self._current_behavior_tree is not None else None
        if not isinstance(node, behavior_tree.MissionLeafNode):
            return None
        return order_ids.order_id(self._order_prefix(), node.idx)

    def _note_replaced_route(self, node: mission_object.MissionNodeV1):
        """A reroute is about to replace `node`'s route. If the robot's order of the node is
        not recorded (one adopted from before records existed), record it as built from
        the route being replaced, so its progress is not taken for the new one's."""
        current = self._current_order_id()
        if current is None or self._current_leaf_node() is not node or \
                self._sent_order_record(current) is not None or node.route is None:
            return
        self._current_mission.status.sent_order = mission_object.MissionSentOrderV1(
            order_id=current, route_digest=_route_digest(node.route))

    def _resend_due(self) -> bool:
        """Whether an order the robot has not adopted may be sent again: after
        ORDER_RESEND_BASE_S, doubling with each resend up to ORDER_RESEND_MAX_S."""
        interval = min(self.ORDER_RESEND_BASE_S * 2 ** self._order_resends,
                       self.ORDER_RESEND_MAX_S)
        return time.monotonic() - self._order_sent_at >= interval

    def _refuse_route_node(self, mission_node: mission_object.MissionNodeV1, reason: str):
        """Maps §14: a route node whose map the robot is not placed on is not sent; it fails
        (MISSION.NODE_FAILED), and the behavior tree decides what that means for the mission.
        The mission is wrapped up on the robot's next state message, as after any failure."""
        self.warning(f"[{self._current_mission.name}] Route node {mission_node.name} not "
                     f"sent: {reason}")
        self._current_mission.status.failure_reason = \
            f"Route node {mission_node.name}: {reason}"
        self.set_mission_node_state(f"{mission_node.name}", mission_object.MissionStateV1.FAILED)
        self.update_mission_from_behavior_tree()

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

        # A reroute (PUT /missions/{name} with update_nodes): the API has already folded the
        # new routes into the stored mission_tree and bumped route_rev. It is acted on once
        # per revision -- status.applied_route_rev records which one -- however often the
        # row is delivered (the periodic resync, an echo, a restart). A mission that is not
        # dispatched yet takes the new tree through _apply_spec_edit instead.
        if mission is self._current_mission and self._current_behavior_tree is not None and \
                message.route_rev > mission.status.applied_route_rev:
            changed = []
            for new_node in message.mission_tree:
                for n in mission.mission_tree:
                    if n.name == new_node.name and new_node.route is not None and \
                            n.route != new_node.route:
                        self._note_replaced_route(n)
                        n.route = new_node.route
                        changed.append(str(n.name))
                        # Waypoint progress was counted on the old route, and so were
                        # the robot's reports on its nodes.
                        mission.status.task_status.pop(str(n.name), None)
                        self._drop_node_reports(str(n.name))
                        if mission.status.node_status[str(n.name)].state is \
                                mission_object.MissionStateV1.RUNNING:
                            # Cancel current node
                            cancel_current_node = True
                        break
            self.info(f"Reroute [{mission.name}] route_rev "
                      f"{mission.status.applied_route_rev} -> {message.route_rev}: "
                      f"nodes {changed}")
            mission.planned_path = message.planned_path
            mission.route_rev = message.route_rev
            mission.status.applied_route_rev = message.route_rev
            self._queue_status_write(api_objects.MissionObjectV1, mission.name, mission.status, self._mission_writer_id())
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
            if message.status.start_timestamp is not None:
                # Already started before a dispatcher restart: it resumes ahead of the
                # missions that have not.
                self._missions.move_to_end(message.name, last=False)
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

                if self._current_behavior_tree is None and \
                        self._current_mission.needs_canceled:
                    # Picked but its tree is not built (held, or resumed after a restart):
                    # ends here unless an order of it may be on the robot, which is then
                    # cancelled first (see _cancel_before_dispatch).
                    if await self._cancel_before_dispatch():
                        await self._robot_server.delete_pending_mission(message)
                    return

                if self._current_mission.needs_canceled and self._timeout_paused is not None:
                    # The robot is offline, so the cancelOrder may not be answered: the
                    # timeout is the backstop that ends the mission and frees the queue.
                    self._resume_mission_timeout()
                if self._current_mission.needs_canceled or cancel_node_from_api:
                    # One cancel at a time. This branch runs on *every* change event
                    # for the running mission -- including the watcher echo of each
                    # status write we make per robot state message -- so minting a
                    # fresh actionId here each time flooded the robot with a new
                    # cancelOrder per state message until the cancel completed
                    # (23k+ distinct cancel actions observed for one mission, each
                    # rejected by the robot as "cancel already in progress"). The
                    # outstanding one is resent by handle_instant_action() anyway.
                    # One of an earlier run (a timeout's) does not cancel this mission.
                    if self._has_outstanding_cancel(current_run_only=True):
                        self.debug("cancelOrder already outstanding; not sending another")
                        return
                    if not self._current_mission.needs_canceled and \
                            self._replace_cancel_must_wait():
                        # Never cancel an order version the robot has not even seen: the
                        # cancel goes out once it reports it, or after the dwell. The
                        # route sent then is the newest (it is read when the order is
                        # built).
                        self._deferred_replace_cancel = True
                        self.mission_info("Reroute: waiting for the robot to take the order "
                                          "just sent before cancelling it")
                        return
                    self._deferred_replace_cancel = False
                    self.info("Cancelling current node...")
                    # Not checked against the robot's state: the order may be one it has
                    # not reported yet (guarded above by the outstanding cancel).
                    await self._cancel_order(
                        CancelPurpose.MISSION if self._current_mission.needs_canceled
                        else CancelPurpose.REPLACE,
                        "mission" if self._current_mission.needs_canceled else "replace",
                        "Send cancel order action", ours=OURS_ANY, current_run_only=True)
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
                self._missions[message.name].status.failure_category = \
                    mission_object.MissionFailureCategoryV1.CANCELED
                await self._write_status(api_objects.MissionObjectV1, self._missions[message.name].name, self._missions[message.name].status, self._mission_writer_id())
                del self._missions[message.name]

    async def _on_robot_change(self, message: api_objects.RobotObjectV1):
        if self._robot_object is None:
            # Create robot object
            self.info("Created robot")
            self._robot_object = message

            self._arm_online_watchdog()

            if (not self._robot_server.disable_request_factsheet
                    and self._robot_object.status.factsheet.agv_class == ""):
                action_id = self._action_id("instantaction")
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
            # The row a watcher notification carries is read when the notification is
            # handled, and the controller's own status writes are queued and throttled
            # (_queue_status_write, >= 1 s apart), so the row can predate them. Its `status`
            # is the controller's: adopting it re-adopted a stale `online` (a spurious
            # "Robot Online" and budget reset on an offline robot), `errors`, pose, battery
            # and the finished mission's ON_TASK (masked-frigatebird, 2026-09-29: ON_TASK with
            # no mission for hours, placement refused as "driving"). So the spec and lifecycle
            # come from the row, the status stays the one in memory -- except the factsheet,
            # which the API also writes (PUT /robots with factsheet data).
            status = self._robot_object.status
            if message.status.factsheet.agv_class or not status.factsheet.agv_class:
                status.factsheet = message.status.factsheet
            message.status = status
            # Delete robot update
            if message.lifecycle == api_objects.object.ObjectLifecycleV1.PENDING_DELETE:
                # (the missions are failed in _delete_robot_object)
                # Set the state of the robot to DELETE for RobotServer to delete
                # on the server and database side.
                self.debug(
                    "Robot is idle and delete request received, deleting robot.")
                await self._delete_robot_object()

            # Re-request factsheet if not yet received (robot may have restarted MQTT)
            if (not self._robot_server.disable_request_factsheet and
                    self._robot_object.status.factsheet.agv_class == ""):
                action_id = self._action_id("instantaction")
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
                action_id = self._action_id("instantaction")
                action_type = types.NVInstantActionType.START_TELEOP \
                    if message.switch_teleop else types.NVInstantActionType.STOP_TELEOP
                # One outstanding action of a type at a time: handle_instant_action()
                # resends it until the robot answers (or it is abandoned).
                if not any(a.actionType == action_type
                           for a in self._current_instant_actions.values()):
                    instant_action = types.VDA5050Action(
                        actionType=action_type, actionId=action_id)
                    self.mission_info(f"Sending {action_type.value} action.")
                    await self._send_instant_action(instant_action)
                    self._current_instant_actions[action_id] = instant_action

            await self._handle_force_cancel(message)

            # Robot object update
            self._robot_object = message

    def _arm_online_watchdog(self) -> None:
        """(Re)start the heartbeat timer: a timer handle re-armed per state message is far
        cheaper than a task cancelled and created for each. When it fires without a state
        having re-armed it, the robot is offline (_check_robot_online)."""
        if self._robot_online_task is not None:
            self._robot_online_task.cancel()
        if self._robot_object is None:
            return
        loop = asyncio.get_event_loop()
        self._robot_online_task = loop.call_later(
            self._robot_object.heartbeat_timeout.total_seconds(), self._on_heartbeat_timeout)

    def _on_heartbeat_timeout(self) -> None:
        if self._alive:
            self._robot_online_task = \
                asyncio.get_event_loop().create_task(self._check_robot_online())

    async def _check_robot_online(self):
        if self._robot_object is None:
            return
        try:
            self.info("Robot Offline")
            self._robot_object.status.recording_state = None
            self._robot_object.status.nav_reasoning = None
            # Queued, not awaited: the offline marker is latest-wins and a failed write is
            # retried (an awaited failure here would be an unobserved task exception).
            if not self._robot_object.status.online:
                self._queue_status_write(api_objects.RobotObjectV1, self._robot_object.name, self._robot_object.status, self._writer_id())
                return
            self._robot_object.status.online = False
            self._pause_mission_timeout()
            if self._robot_object.lifecycle is not api_objects.object.ObjectLifecycleV1.DELETED:
                self._queue_status_write(api_objects.RobotObjectV1, self._robot_object.name, self._robot_object.status, self._writer_id())
        except asyncio.CancelledError:
            self.debug("Cancelled robot online check.")

    def _reset_resend_budgets(self) -> None:
        """The robot is back after being offline. Its clean MQTT session lost whatever we
        sent meanwhile, so what it still reports is not evidence we were ignored: give the
        order and the outstanding instant actions (a cancelOrder) a fresh budget, due on
        the first state after the reconnect."""
        self._order_resends = 0
        self._order_mismatch_count = 0
        self._order_sent_at = 0.0
        self._instant_action_resends.clear()
        self._instant_action_resent_at.clear()

    def _forget_instant_action_resends(self, action_id: str) -> None:
        self._instant_action_resends.pop(action_id, None)
        self._instant_action_resent_at.pop(action_id, None)

    async def handle_instant_action(self, message: types.VDA5050State):
        # Handle instant actions
        updated_instant_action_ids = []
        finished_instant_actions = []
        for action_state in message.actionStates[::-1]:
            # Only instant actions; order actions (e.g. nodePolicy) may be listed in any
            # position, and stopping at the first one would hide a cancelOrder ack
            # behind it.
            if action_state.actionType not in _INSTANT_ACTION_TYPES:
                continue
            if action_state.actionId in self._current_instant_actions.keys():
                if action_state.actionStatus == types.VDA5050ActionStatus.FINISHED:
                    # Update current instant aciton dict
                    finished_instant_actions.append(
                        self._current_instant_actions.pop(action_state.actionId))
                    self._forget_instant_action_resends(action_state.actionId)
                    self._cancel_resolved(finished_instant_actions[-1])
                    self.mission_info(
                        f"Finished instant action:\n {finished_instant_actions[-1]}")
                elif action_state.actionStatus == types.VDA5050ActionStatus.FAILED:
                    # FAILED is as terminal as FINISHED: the robot will never move
                    # this action again, so keeping it here only blocks
                    # _has_outstanding_cancel() forever and keeps it in the resend
                    # loop. A FAILED cancelOrder specifically means "no order to
                    # cancel" (VDA5050 noOrderToCancel) -- the robot has nothing of
                    # this mission left running, which is the outcome a cancel was
                    # after, so it counts as a completed cancel for the mission. A
                    # FAILED while the robot still runs the order is not that (above).
                    if self._failed_cancel_leaves_our_order(
                            self._current_instant_actions[action_state.actionId],
                            action_state, message):
                        # Not "no order to cancel": the robot still runs it. Stays
                        # outstanding, so it is resent within the resend budget.
                        self.debug(f"cancelOrder {action_state.actionId} reported FAILED "
                                   f"but the robot still runs {message.orderId}")
                        continue
                    failed = self._current_instant_actions.pop(action_state.actionId)
                    self._forget_instant_action_resends(action_state.actionId)
                    self._cancel_resolved(failed)
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
        # after MAX_INSTANT_ACTION_RESENDS attempts. Resends are spaced with back-off:
        # state arrives many times a second, an ack a moment after a send.
        give_up: List[str] = []
        now = time.monotonic()
        for action_id, instant_action in self._current_instant_actions.items():
            if action_id not in updated_instant_action_ids:
                resends = self._instant_action_resends.get(action_id, 0)
                # The original send is not a resend: the first gap is the base.
                interval = min(self.INSTANT_ACTION_RESEND_BASE_S * 2 ** max(resends - 1, 0),
                               self.INSTANT_ACTION_RESEND_MAX_S)
                if now - self._instant_action_resent_at.get(action_id, -math.inf) < interval:
                    continue
                attempts = resends + 1
                limit = self.MAX_INSTANT_ACTION_RESENDS
                if self._cancel_purposes.get(action_id, (None, None))[0] is CancelPurpose.STOP:
                    limit = min(limit, self.STOP_CANCEL_MAX_RESENDS)
                if attempts > limit:
                    self.warning(
                        f"Abandoning {instant_action.actionType} instant action "
                        f"{action_id} -- unacknowledged after {limit} resends")
                    give_up.append(action_id)
                    continue
                self._instant_action_resends[action_id] = attempts
                self._instant_action_resent_at[action_id] = now
                # Resend instant action
                await self._send_instant_action(instant_action)
                self.mission_info(
                    f"Resend {instant_action.actionType} instant action "
                    f"({attempts}/{limit}).")
        for action_id in give_up:
            abandoned = self._current_instant_actions.pop(action_id, None)
            self._forget_instant_action_resends(action_id)
            if abandoned is not None:
                self._cancel_resolved(abandoned, abandoned=True)
        return finished_instant_actions
    def _failed_cancel_leaves_our_order(self, action: types.VDA5050Action,
                                        action_state: types.VDA5050ActionState,
                                        message: types.VDA5050State) -> bool:
        """Whether a cancelOrder the robot answered FAILED left the order it was for
        running: the robot still lists nodes or edges, the order is the one the cancel was
        for (the current run's; a CLEAR is for whatever order the robot holds), and it did
        not say noOrderToCancel. Otherwise there is nothing left to cancel."""
        if action.actionType != types.VDA5050InstantActionType.CANCEL_ORDER or \
                not (message.nodeStates or message.edgeStates):
            return False
        if action_state.resultDescription == "noOrderToCancel" or any(
                e.errorType == "noOrderToCancel" for e in message.errors):
            return False
        purpose, run = self._cancel_purposes.get(action.actionId, (CancelPurpose.STOP, None))
        if purpose is CancelPurpose.CLEAR:
            return True
        return self._current_mission is not None and run == self._run_key() and \
            self._is_order_of_run(message.orderId)

    async def _process_datum_message(self, msg: types.RobotDatum) -> None:
        """Persist the robot's datum.

        Maps redesign M2: the map datum auto-seed is gone. A geo map's origin (and its legacy
        datum_* fields) comes from its first mapping session (doc Q1, packages/api/maps.py);
        seeding from the robot's assigned map gave local maps a datum, and wrote a mapless
        sentinel's row with whichever robot sent a datum first."""
        old = self._robot_object.datum
        new = robot_object.RobotDatumV1(**msg.dict())
        # Robots republish the same datum every few seconds: nothing to write when it and its
        # publisher stamp are as stored (the geo re-placement below still runs each time).
        unchanged = new == old and (msg.stamp is None
                                    or msg.stamp == self._robot_object.datum_stamp)
        self._robot_object.datum = new
        # Only the datum (robots send it every few seconds): writing the cached full spec
        # back would revert any spec change committed since the cache was filled.
        fields: Dict[str, Any] = {"datum": json.loads(new.json())}
        # Freshness (map-location plan A): keyed on CHANGE time, not receive time, because
        # retained re-deliveries and reconnect republishes would make an old datum look fresh.
        if _datum_changed(old, new):
            self._robot_object.datum_changed_at = datetime.datetime.now(datetime.timezone.utc)
            fields["datum_changed_at"] = self._robot_object.datum_changed_at.isoformat()
        if msg.stamp is not None:
            self._robot_object.datum_stamp = msg.stamp
            fields["datum_stamp"] = msg.stamp.isoformat()
        if not unchanged:
            await self._database.update_spec_fields(
                api_objects.RobotObjectV1, self._name, fields, self._writer_id()
            )
        epoch = getattr(self._robot_server, "mqtt_epoch", 0)
        trusted = self._datum_epoch == epoch
        self._datum_epoch = epoch
        await self._replace_geo_session(map_geo.robot_datum(self._robot_object.datum), trusted)

    async def _process_approx_position_message(self, msg: types.RobotApproxPosition) -> None:
        """Store the robot's approximate position in its status (map-location plan B).

        Telemetry only: it never reaches _replace_geo_session and never touches the datum or
        the spec. (0, 0) is the "no fix" sentinel and is rejected."""
        if common_objects.is_null_island(msg.latitude, msg.longitude):
            self.warning("Ignoring approx_position at (0, 0)")
            return
        robot = self._robot_object
        if robot is None or robot.lifecycle is api_objects.object.ObjectLifecycleV1.DELETED:
            return
        if not _approx_position_changed(robot.status.approx_position, msg):
            return
        robot.status.approx_position = robot_object.RobotApproxPositionV1(
            **msg.dict(), stored_at=datetime.datetime.now(datetime.timezone.utc))
        await self._write_status(api_objects.RobotObjectV1, robot.name, robot.status, self._writer_id())

    # --- maps §14 U3: run changes and geo re-placement -----------------------------------------

    async def _on_connection_message(self, message: types.VDA5050Connection,
                                     retained: bool = False) -> None:
        state = getattr(message.connectionState, "value", message.connectionState)
        self._manufacturer = message.manufacturer or self._manufacturer
        if state in ("OFFLINE", "CONNECTIONBROKEN"):
            self._connection_down = True
        evidence = self._run_detector.on_connection(message.connectionState, message.headerId,
                                                    retained)
        if state == "ONLINE" and self._connection_down:
            # The robot reconnected (its clean MQTT session lost what we sent meanwhile),
            # possibly within the heartbeat timeout, so the offline->online path in
            # _on_client_message never ran: whatever the budgets spent is no evidence.
            self._connection_down = False
            self._reset_resend_budgets()
        if evidence is not None:
            await self._on_run_changed(evidence)

    async def _on_run_changed(self, evidence: Dict[str, Any]) -> None:
        """The robot's run frame reset (run_change.py): its open session is no longer placed.
        One transaction: aligned = false, placement.unplaced_reason = run_changed (+ when, and
        the evidence), MAP.SESSION_UNPLACED (graph-builder then drops the session's nodes). A geo
        session is re-placed by the next datum (_replace_geo_session). Never raises."""
        now = datetime.datetime.now(datetime.timezone.utc)
        self.info(f"New robot run ({evidence}): unplacing its open map session")
        # A robot that restarted never got what we sent: fresh resend budgets.
        self._reset_resend_budgets()
        patch = {"unplaced_reason": map_sessions.UNPLACED_RUN_CHANGED,
                 "unplaced_at": now.isoformat(), "unplaced_evidence": evidence}
        # Where the robot last was, in the OLD run's frame: the pose in memory is still the old
        # run's (both callers run before _on_client_message stores the new message's pose), and
        # the unplace keeps map_t_session -- together they give the "last position" placement
        # suggestion (map_sessions.last_position_suggestion).
        pose = self._robot_object.status.pose if self._robot_object is not None else None
        if evidence.get("signal") == "state_header_reset" and self._pre_drop_pose is not None:
            pose = self._pre_drop_pose   # the drop was confirmed a state later (run_change.py)
        self._pre_drop_pose = None
        if pose is not None:
            try:
                last = {"x": float(pose.x), "y": float(pose.y), "theta": float(pose.theta)}
            except (TypeError, ValueError):
                last = None
            if last is not None and all(math.isfinite(v) for v in last.values()):
                patch["last_robot_pose"] = last
        if self._run_epoch is not None:
            patch["old_run_id"] = str(self._run_epoch)
        try:
            async with self._database.connection() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(map_sessions.UNPLACE_SQL, (json.dumps(patch), self._name))
                    rows = await cursor.fetchall()
                for session_id, map_name, purpose, transform in rows:
                    await self._emit(conn, Event(
                        EventCode.MAP_SESSION_UNPLACED, now, robot_name=self._name,
                        source=Source.DISPATCH,
                        discriminator=f"session:{session_id}:unplaced:{now.isoformat()}",
                        payload={"map_name": map_name, "session_id": str(session_id),
                                 "purpose": purpose, "reason": map_sessions.UNPLACED_RUN_CHANGED,
                                 "evidence": evidence, "old_map_T_session": transform}))
                # Every run change, with or without an open session (§14.13).
                await self._new_run_epoch(conn, evidence)
        except Exception as err:  # pylint: disable=broad-except
            self.warning(f"Could not unplace the open session after a run change: {err}")
            # The epoch was not renewed either: decide again on the next state message (its
            # header id cannot prove continuity with the old run's, so a new epoch follows).
            self._run_checked = False
            self._run_epoch = None
            return
        for _sid, map_name, purpose, _t in rows:
            self.warning(f"Session on map '{map_name}' ({purpose}) is no longer placed: the "
                         "robot's run frame changed")

    # --- maps §14.13: the run epoch (placement reuse across sessions) ----------------------------

    async def _check_run_continuity(self, header_id: Any) -> None:
        """The robot's first state message in this dispatcher process: keep its stored run
        epoch only if the header id PROVES the VDA5050 client process is the one seen before
        (map_sessions.run_continues); otherwise start a new epoch (reason first_seen /
        dispatcher_restart), so no session finished before the gap lends its placement. Never
        raises; on a database error the next state message tries again."""
        try:
            hid = int(header_id)
        except (TypeError, ValueError):
            return
        try:
            epoch = await asyncio.wait_for(self._run_epoch_check(hid), RUN_CHECK_TIMEOUT_S)
        except Exception as err:  # pylint: disable=broad-except
            self.warning(f"Run epoch not checked ({err!r}); placements are not reused (retry "
                         f"in {RUN_CHECK_RETRY_S:.0f} s)")
            self._run_check_after = time.monotonic() + RUN_CHECK_RETRY_S
            return
        self._run_checked = True
        self._run_epoch = epoch
        self._run_header_saved_at = time.monotonic()

    async def _run_epoch_check(self, hid: int) -> uuid.UUID:
        """The database half of _check_run_continuity: the epoch to use."""
        async with self._database.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(map_sessions.RUN_EPOCH_READ_SQL, (self._name,))
                row = await cursor.fetchone()
                if row is not None and row[0] is not None and \
                        map_sessions.run_continues(row[2], row[3], hid):
                    epoch = row[0] if isinstance(row[0], uuid.UUID) else uuid.UUID(str(row[0]))
                    await cursor.execute(map_sessions.RUN_EPOCH_CONFIRM_SQL,
                                         (hid, self._name, epoch))
                    self.info(f"Robot run continues across the dispatcher restart (state "
                              f"headerId {row[2]} -> {hid}): run epoch kept")
                else:
                    epoch = uuid.uuid4()
                    reason = (map_sessions.REASON_FIRST_SEEN if row is None
                              else map_sessions.REASON_DISPATCHER_RESTART)
                    evidence = {"state_header_id": hid,
                                "last_state_header_id": row[2] if row else None,
                                "elapsed_s": (round(float(row[3]), 1)
                                              if row and row[3] is not None else None)}
                    await cursor.execute(map_sessions.RUN_EPOCH_NEW_SQL, (
                        self._name, epoch, reason, json.dumps(evidence), hid))
                    self.info(f"New run epoch ({reason}, {evidence}): placements of "
                              "finished sessions are not reused")
        return epoch

    async def _persist_run_header(self, header_id: Any) -> None:
        """Store the robot's state headerId every RUN_HEADER_PERSIST_S (the baseline a later
        dispatcher start proves continuity against). Never raises."""
        if self._run_epoch is None:
            return
        now = time.monotonic()
        if self._run_header_saved_at is not None and \
                now - self._run_header_saved_at < map_sessions.RUN_HEADER_PERSIST_S:
            return
        try:
            hid = int(header_id)
        except (TypeError, ValueError):
            return
        if self._run_header_task is not None and not self._run_header_task.done():
            return      # single-flight: the one in flight is stuck or slow, the next window retries
        self._run_header_saved_at = now
        # Off the state loop (a pool stall must not stop the robot's handling); tracked with
        # the status writes so a shutdown flushes/cancels it.
        task = self._run_header_task = asyncio.ensure_future(
            self._store_run_header(hid, self._run_epoch))
        self._status_write_tasks.add(task)
        task.add_done_callback(self._status_write_done)

    async def _store_run_header(self, hid: int, epoch: uuid.UUID) -> None:
        try:
            async with self._database.connection() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(map_sessions.RUN_EPOCH_HEADER_SQL,
                                         (hid, self._name, epoch))
        except asyncio.CancelledError:
            raise
        except Exception as err:  # pylint: disable=broad-except
            self.debug(f"Run header not stored: {err}")

    async def _new_run_epoch(self, conn: Any, evidence: Dict[str, Any]) -> None:
        """A detected run change starts a new run epoch (in a savepoint of the caller's
        transaction: a failure, e.g. before the migration, never undoes the unplace)."""
        epoch = uuid.uuid4()
        header = evidence.get("state_header_id")
        try:
            async with conn.transaction():
                async with conn.cursor() as cursor:
                    await cursor.execute(map_sessions.RUN_EPOCH_NEW_SQL, (
                        self._name, epoch, map_sessions.REASON_RUN_CHANGED,
                        json.dumps(evidence), header))
        except Exception as err:  # pylint: disable=broad-except
            self.warning(f"Run epoch not recorded ({err}); placements are not reused")
            self._run_epoch = None
            return
        self._run_checked = True
        self._run_epoch = epoch
        self._run_header_saved_at = time.monotonic() if header is not None else None

    async def _replace_geo_session(self, datum: Optional[Dict[str, Any]], trusted: bool) -> None:
        """A datum message for a robot whose open session is on a geo map: re-place a session a
        run change unplaced, or re-derive a placed one whose datum changed (plan_geo_replace).
        Compare-and-set on the state read (graph-builder realigns the same way), with
        MAP.SESSION_REALIGNED in the same transaction. Never raises."""
        session = await self._read_open_session()
        if not isinstance(session, dict):
            return
        plan = map_sessions.plan_geo_replace(session, datum, trusted)
        if plan is None:
            return
        transform, reason = plan
        now = datetime.datetime.now(datetime.timezone.utc)
        old_placement = session.get("placement") or {}
        placement = {"source": map_sessions.SOURCE_DATUM, "at": now.isoformat(),
                     "reason": reason}
        if not map_sessions.is_placed(session):
            placement["replaced_after"] = {k: old_placement.get(k) for k in
                                           ("unplaced_reason", "unplaced_at")}
        won = False
        try:
            async with self._database.connection() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(map_sessions.REPLACE_SQL, (
                        json.dumps(datum), json.dumps(transform), json.dumps(placement),
                        uuid.UUID(session["session_id"]), map_sessions.is_placed(session),
                        json.dumps(session["datum"]) if session.get("datum") is not None
                        else None))
                    won = cursor.rowcount == 1
                if won:
                    await self._emit(conn, Event(
                        EventCode.MAP_SESSION_REALIGNED, now, robot_name=self._name,
                        source=Source.DISPATCH,
                        discriminator=(f"session:{session['session_id']}:realigned:"
                                       f"{transform['tx']:.4f}:{transform['ty']:.4f}:"
                                       f"{transform['yaw']:.6f}:{now.isoformat()}"),
                        payload={"map_name": session["map_name"],
                                 "session_id": session["session_id"], "aligned": True,
                                 "purpose": session["purpose"], "reason": reason,
                                 "map_T_session": dict(transform),
                                 "old_map_T_session": dict(session["map_t_session"]),
                                 "datum": dict(datum), "old_datum": session.get("datum")}))
        except Exception as err:  # pylint: disable=broad-except
            self.warning(f"Could not re-place the geo session from the new datum: {err}")
            return
        if won:
            self.info(f"Session on geo map '{session['map_name']}' placed from the robot's "
                      f"datum ({reason}): map_T_session {transform}")

    async def _emit(self, conn: Any, event: Event) -> None:
        """An event in a savepoint of the caller's transaction: a failed write is logged and
        never undoes the session change."""
        try:
            async with conn.transaction():
                await emit_event(conn, event)
        except Exception as err:  # pylint: disable=broad-except
            self.warning(f"Could not write {event.code}: {err}")

    async def _read_open_session(self) -> Any:
        """The robot's open map session (maps §14; packages/utils/map_sessions.py), None when
        it has none, SESSION_UNKNOWN when it could not be read."""
        try:
            async with self._database.connection() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(map_sessions.ROBOT_SESSION_SQL, (self._name,))
                    return map_sessions.robot_session_from_row(await cursor.fetchone())
        except Exception as err:  # pylint: disable=broad-except
            self.warning(f"Open session not readable ({err}); route nodes on a map are refused")
            return SESSION_UNKNOWN

    async def _route_in_robot_frame(
            self, route: mission_object.MissionRouteNodeV1) -> mission_object.MissionRouteNodeV1:
        """The route's waypoints in the robot's current frame (maps redesign M2, §14).

        Waypoints that name a map (`map_id`) are in that map's frame: node poses are stored in
        the map frame, and the client places waypoints with the map's transform. The robot's
        map is its open session (§14.2): a waypoint on the session's map goes through
        inverse(map_T_session). RouteRefused (the node fails) when that session is not placed
        ("robot is not placed on map X"), when the robot has no session on the waypoint's map
        ("robot is not using map X"; since U6 there is no map/datum fallback, and 'GEO' /
        'LOCAL' are not maps), or when the session could not be read. Waypoints without a map
        (mapless missions: already robot frame) are sent as they are. The stored mission is
        not changed.

        What was applied is left in `_last_route_frame` (MissionSentOrderV1.frame) and
        logged, so node offsets the robot reports can be read in the map frame."""
        names = {wp.map_id for wp in route.waypoints if wp.map_id}
        self._last_route_frame = {"applied": "none", "map_id_sent": ""}
        if not names:
            return route
        session = await self._read_open_session()
        if session is SESSION_UNKNOWN:
            raise RouteRefused(f"the robot's map session could not be read (waypoints on map "
                               f"{', '.join(sorted(names))})")
        inverse: Dict[str, Dict[str, float]] = {}
        for name in sorted(names):
            if not isinstance(session, dict) or session["map_name"] != name:
                raise RouteRefused(f"robot is not using map {name}")
            if not map_sessions.is_placed(session):
                raise RouteRefused(f"robot is not placed on map {name}")
            t = session["map_t_session"]
            if not map_geo.is_identity(t):
                inverse[name] = map_geo.invert_transform(t)
        self._last_route_frame = {
            "applied": "inverse" if inverse else "identity",
            "map_name": session["map_name"], "session_id": session.get("session_id"),
            "map_t_session": session["map_t_session"],
            # The nodes keep the map's name as mapId although their positions are in the
            # robot's session frame (the robot team is asked which frame they expect).
            "map_id_sent": ", ".join(sorted(names))}
        self.mission_info(f"Order frame: {self._last_route_frame}")
        if not inverse:
            return route
        converted = route.copy(deep=True)
        for wp in converted.waypoints:
            t = inverse.get(wp.map_id)
            if t is not None:
                wp.x, wp.y, wp.theta = map_geo.apply_pose(t, wp.x, wp.y, wp.theta)
        self.mission_info(f"Route waypoints converted from the frame of map(s) "
                          f"{', '.join(sorted(inverse))} into the robot's frame")
        return converted

    async def _on_client_message(self, message: types.VDA5050State):
        self.debug("[%s] Got feedback", message.orderId)
        self._manufacturer = message.manufacturer or self._manufacturer
        # First, so that every decision on this message (a held mission's start, a cancel
        # before dispatch) sees the robot as this message reports it, not the previous one.
        self._robot_order_id = message.orderId
        self._robot_executing = bool(message.nodeStates or message.edgeStates)
        # If we have a robot, Update it with the details from the message
        if self._robot_object is not None:
            self._arm_online_watchdog()
            if message.agvPosition:
                self._robot_object.status.pose.x = message.agvPosition.x
                self._robot_object.status.pose.y = message.agvPosition.y
                self._robot_object.status.pose.theta = message.agvPosition.theta
                self._robot_object.status.pose.map_id = message.agvPosition.mapId
                self._robot_object.status.position_initialized = \
                    message.agvPosition.positionInitialized
                self._robot_object.status.localization_score = \
                    message.agvPosition.localizationScore
            else:
                # No position in this state: the last flag and score are no longer current.
                # The pose and map_id stay as the last known position.
                self._robot_object.status.position_initialized = None
                self._robot_object.status.localization_score = None
            if message.batteryState:
                self._robot_object.status.battery_level = message.batteryState.batteryCharge
                self._robot_object.status.battery_unknown = battery.battery_unknown(
                    message.errors)
                if message.batteryState.charging and not self._robot_object.status.state.running:
                    self._set_robot_state(
                        robot_object.RobotStateV1.CHARGING)
                    self._charging_mission_received = False
                elif (self._robot_object.status.state == robot_object.RobotStateV1.CHARGING and
                      not message.batteryState.charging):
                    self._set_robot_state(
                        robot_object.RobotStateV1.IDLE)

            if self._robot_server.mission_ctrl_url:
                send_charging_mission = (self._robot_object.battery.recommended_minimum
                                         and not self._robot_object.status.battery_unknown
                                         and (self._robot_object.status.battery_level <=
                                              self._robot_object.battery.recommended_minimum)
                                         and not self._robot_object.status.state.running
                                         and not self._charging_mission_received)
                if send_charging_mission and not self._charging_hook_busy and \
                        time.monotonic() >= self._charging_hook_next_at:
                    # Off the event loop, and at most one attempt per backoff window.
                    self._charging_hook_busy = True
                    self._charging_hook_next_at = time.monotonic() + CHARGING_HOOK_RETRY_S
                    task = asyncio.ensure_future(self._post_charging_mission())
                    self._background_tasks.add(task)
                    task.add_done_callback(self._background_tasks.discard)
            if not self._robot_object.status.online:
                self.info("Robot Online")
                self._reset_resend_budgets()
            # Any state means the robot is back, whatever the online flag says (a watcher
            # row read before the offline write landed can have set it again).
            if self._timeout_paused is not None:
                self._resume_mission_timeout()
            self._robot_object.status.online = True
            # Single pass over the VDA5050 information[] array. Each infoType is an
            # independent slot keyed by type (last entry wins on the rare duplicate),
            # so we collect them once instead of re-scanning the list per field.
            info_by_type: Dict[str, str] = {
                i.infoType: i.infoDescription for i in (message.information or [])}

            if "user_info" in info_by_type:
                raw_info = info_by_type["user_info"]
                try:
                    parsed_info = json.loads(raw_info)
                    if not isinstance(parsed_info, dict):
                        raise ValueError("user_info is not a JSON object")
                    self._robot_object.status.info_messages = parsed_info
                except ValueError as err:
                    # Malformed robot data must not stall the mission on every state.
                    if raw_info != self._bad_user_info:
                        self._bad_user_info = raw_info
                        self.warning(f"Ignoring malformed user_info ({err}): {raw_info[:200]!r}")

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
            else:
                await self._retry_stalled_start()
            # Update robot unique ID
            self._robot_object.status.hardware_version = \
                robot_object.RobotHardwareVersionV1(manufacturer=message.manufacturer,
                                                    serial_number=message.serialNumber)
            if self._robot_object.lifecycle is not api_objects.object.ObjectLifecycleV1.DELETED:
                self._store_robot_status()

            # Update object detection results if necessary
            listed = {a.actionId for a in message.actionStates}
            # A finished action stays listed: forget only those the robot stopped listing.
            self._detection_actions_done &= listed
            self._bad_detection_actions &= listed
            for action_state in message.actionStates:
                if (action_state.actionStatus == types.VDA5050ActionStatus.FINISHED and
                        action_state.actionType == types.NVActionType.GET_OBJECTS and
                        self.robot_object is not None):
                    if action_state.actionId in self._detection_actions_done or \
                            action_state.actionId in self._bad_detection_actions:
                        continue
                    try:
                        detected = [DetectedObject(**item) for item in json.loads(
                            action_state.resultDescription)]
                    except (TypeError, ValueError) as err:
                        self._bad_detection_actions.add(action_state.actionId)
                        self.warning(f"Ignoring malformed getObjects result of action "
                                     f"{action_state.actionId}: {err}")
                        continue
                    self._detection_actions_done.add(action_state.actionId)
                    if self._detection_results_object is None:
                        self._detection_results_object = api_objects.DetectionResultsObjectV1(
                            name=self.robot_object.name)
                        try:
                            await self._database.create_object(
                                self._detection_results_object, uuid.uuid4())
                        except fastapi.HTTPException as err:
                            # 400: the row exists (dispatcher restarted); update it below.
                            if err.status_code != 400:
                                raise
                    self._detection_results_object.status.detected_objects = detected

                    await self._database.update_status(
                        api_objects.DetectionResultsObjectV1, self._detection_results_object.name, self._detection_results_object.status, uuid.uuid4())
                    self.info(
                        "Updated object detector information in mission database.")

        finished_instant_actions = await self.handle_instant_action(message)
        self.update_robot_state(finished_instant_actions)

        self._track_stale_fatal(message)

        # What the cancelOrders the robot just resolved were for decides what follows.
        if await self._act_on_resolved_cancels(message):
            return

        # A cancelled mission whose tree is not built (see _cancel_before_dispatch) is
        # waiting for this state to know what the robot holds.
        if self._current_mission is not None and self._current_behavior_tree is None and \
                self._current_mission.needs_canceled and \
                not self._current_mission.status.state.done:
            await self._cancel_before_dispatch()
            return

        # Make sure there is a mission to update
        if self._current_mission is None or self._current_behavior_tree is None:
            self._reconcile_stale_state()
            return

        # In case mission failed due to timeout
        if self._current_mission.status.state.done:
            if not self._will_run_another_pass():
                self._set_robot_idle_after_mission()
            await self.get_next_mission()
            return

        if await self._end_for_operator_takeover(message):
            return

        if self._resume_pending and await self._resume_from_state(message):
            return

        if self._deferred_replace_cancel and not self._replace_cancel_must_wait():
            self._deferred_replace_cancel = False
            if not self._has_outstanding_cancel(current_run_only=True) and \
                    not self._current_mission.needs_canceled:
                await self._cancel_order(CancelPurpose.REPLACE, "replace",
                                         "Reroute: send cancel order action",
                                         ours=OURS_ANY, current_run_only=True)
                return

        # A send the current node is owed goes out once no cancelOrder of ours is in
        # flight; this state still describes the robot before it.
        if self._pending_send is not None and not self._has_outstanding_cancel():
            await self._flush_pending_send()
            return

        # During a wait (or a notify) the robot has no order of ours to report (a mission or pass that
        # starts with one still has the previous order's id on its state), so a
        # mismatch is expected and must neither be counted nor trigger a resend.
        if (self._wait_key is not None or self._notify_key is not None) and \
                not order_ids.is_order_of(self._order_prefix(), message.orderId):
            return

        # If the order doesn't match, ignore it
        if not order_ids.is_order_of(self._order_prefix(), message.orderId):
            if self._has_outstanding_cancel():
                # Our cancelOrder is in flight: an order now would race it (the robot
                # rejects it while it still runs one, or the cancel takes it). The
                # cancel's completion decides what comes next.
                return
            rejection = self._order_rejection(message)
            if rejection:
                reasons = "; ".join(e.errorDescription for e in rejection)
                self.warning(f"[{self._current_mission.name}] Robot rejected the dispatched "
                             f"order: {reasons} -- failing mission")
                self._current_mission.status.failure_reason = \
                    f"Robot rejected the dispatched order: {reasons}"
                self._set_failure_category(mission_object.MissionFailureCategoryV1.ROBOT_APP)
                node_name = str(self._current_behavior_tree.current_node.name)
                if node_name in self._current_mission.status.node_status:
                    self._current_mission.status.node_status[node_name].error_msg = reasons
                self._set_mission_state(mission_object.MissionStateV1.FAILED)
                self._order_mismatch_count = 0
                self._set_robot_idle_after_mission()
                await self.get_next_mission()
                return
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
            if self._order_mismatch_count >= self.MAX_ORDER_MISMATCHES and \
                    time.monotonic() - self._order_sent_at >= self.ORDER_GIVE_UP_MIN_S:
                self.warning(
                    f"[{self._current_mission.name}] Robot never adopted our order after "
                    f"{self.MAX_ORDER_MISMATCHES} state messages (still reporting "
                    f"{message.orderId}) -- failing mission")
                self._current_mission.status.failure_reason = \
                    ("Robot did not accept the dispatched order "
                     f"(still reporting {message.orderId})")
                self._set_failure_category(mission_object.MissionFailureCategoryV1.ROBOT_APP)
                self._set_mission_state(mission_object.MissionStateV1.FAILED)
                self._order_mismatch_count = 0
                await self._stop_our_order("mismatch")
                self._set_robot_idle_after_mission()
                await self.get_next_mission()
                return
            # The robot takes a moment to adopt an order: send it again only with
            # back-off, not on every state message, and only a few times (the same
            # order again and again is noise to a robot that has rejected it).
            if self._resend_due() and self._order_resends < self.ORDER_MAX_RESENDS:
                await self._send_order()
            return
        self._order_mismatch_count = 0

        prev_child_node = self._current_behavior_tree.current_node.name
        self.update_mission_state(message, finished_instant_actions)

        # Failed (a FATAL error, a failed node) while the robot still runs the order: it
        # must drop it, or it keeps driving and rejects the next mission's order.
        if self._current_mission.status.state == mission_object.MissionStateV1.FAILED:
            await self._stop_our_order("failed")

        # The robot dropped the node's order on its own (see update_mission_state).
        if self._pending_send is not None and not self._has_outstanding_cancel():
            await self._flush_pending_send()

        # If current node is updated, then send a new order
        if prev_child_node != self._current_behavior_tree.current_node.name:
            self.mission_info(f"Update node from {prev_child_node} to "
                              f"{self._current_behavior_tree.current_node.name}")
            await self._send_order()

        if self._current_mission.status.state.done:
            await self.post_mission_completion()

    async def _act_on_resolved_cancels(self, message: types.VDA5050State) -> bool:
        """Act on the cancelOrders the robot has just finished or rejected, or that were
        abandoned unacknowledged, by what each was for. Only a cancel sent in the current
        run counts: one sent for an earlier mission or pass (a timeout's) says nothing
        about this one. Returns whether the mission ended (the message is handled)."""
        resolved, self._resolved_cancels = self._resolved_cancels, []
        mission = self._current_mission
        for purpose, run, abandoned in resolved:
            if mission is None or mission.status.state.done or run != self._run_key():
                continue
            if mission.needs_canceled or purpose is CancelPurpose.MISSION:
                if abandoned:
                    mission.status.failure_reason = \
                        "The robot never confirmed the cancelOrder"
                    if self._robot_runs_our_order():
                        mission.status.failure_reason += \
                            f"; it still reports its order {self._robot_order_id}"
                self._end_current_mission(mission_object.MissionStateV1.CANCELED)
                await self.post_mission_completion()
                return True
            if purpose is CancelPurpose.CLEAR:
                continue  # a stray order is gone; the mission's owed send follows
            if purpose is CancelPurpose.STOP:
                mission.status.failure_reason = \
                    "The robot's order was cancelled by an operator force cancel"
                self._end_current_mission(mission_object.MissionStateV1.CANCELED)
                await self.post_mission_completion()
                return True
            if abandoned and (message.nodeStates or message.edgeStates):
                # The robot still executes an order and never confirmed the cancel, so
                # the new content cannot follow: say so rather than wait for the timeout.
                # (Holding no order, it dropped ours whether it says so or not.)
                mission.status.failure_reason = \
                    "The robot never confirmed the cancelOrder; the new order was not sent"
                self._end_current_mission(mission_object.MissionStateV1.FAILED)
                await self.post_mission_completion()
                return True
            # The robot dropped the node's order to make way for new content.
            self._pending_send = NEW_REVISION
        return False

    async def _end_for_operator_takeover(self, message: types.VDA5050State) -> bool:
        """The robot reports operatorTakeover for an order of the current run: an
        operator has the robot, so the mission is not re-sent (the robot-side cancel that
        comes with it is not one to answer with a new revision). It is cancelled, saying
        why; dispatch then waits until the operator hands the robot back (see
        READINESS_HOLD_REASONS). Returns whether the mission ended."""
        takeover = next((e for e in message.errors if e.errorType == OPERATOR_TAKEOVER), None)
        if takeover is None:
            return False
        order_id = next((ref.referenceValue for ref in takeover.errorReferences
                         if ref.referenceKey in ("orderId", "order_id")), message.orderId)
        if not self._is_order_of_run(order_id):
            return False
        mission = self._current_mission
        mission.status.failure_reason = \
            "An operator took over the robot (operatorTakeover); the mission is not re-sent"
        self.warning(f"[{mission.name}] Operator takeover of {order_id}: cancelling the "
                     "mission")
        self._pending_send = None
        self._resume_pending = False
        self._end_current_mission(mission_object.MissionStateV1.CANCELED)
        await self.post_mission_completion()
        return True

    def _is_order_of_run(self, order_id: Optional[str]) -> bool:
        """Whether `order_id` is an order of the current run, of any revision."""
        if not order_id:
            return False
        prefix = order_ids.order_prefix(order_id)
        status = self._current_mission.status
        name = str(self._current_mission.name)
        return prefix is not None and any(
            prefix == order_ids.run_prefix(name, status.run_id, rev)
            for rev in range(status.order_rev + 1))

    def _end_current_mission(self, state: mission_object.MissionStateV1):
        """End the current mission, and its running leaf node, in `state`."""
        node = self._current_behavior_tree.current_node \
            if self._current_behavior_tree is not None else None
        if isinstance(node, behavior_tree.MissionLeafNode):
            self.set_mission_node_state(
                str(self._current_mission.mission_tree[node.idx].name), state)
        self._set_mission_state(state)

    async def _flush_pending_send(self):
        """Make the send the current node is owed (no cancelOrder of ours is in flight). A
        new revision starts after the waypoints the robot has reached on the node's route.
        What cannot be made yet (not persisted) stays owed for the next state message."""
        kind, self._pending_send = self._pending_send, None
        if kind == NEW_REVISION:
            if not await self._bump_order_rev():
                if self._current_mission.status.state.done:  # failed for order churn
                    await self.post_mission_completion()
                else:
                    self._pending_send = kind
                return
            # The new order counts its sequence ids from 0 again.
            self.last_node_seq_id = -1
        await self._send_order(waypoint_offset=self._reached_waypoints())

    async def _resume_from_state(self, message: types.VDA5050State) -> bool:
        """The robot's first state after a dispatcher restart decides how a resumed mission
        goes on (this process does not know what the robot holds):
        - the robot is executing, or has finished, the current order, built from the
          node's current route: carry on;
        - it is executing anything else: cancel that first -- the cancel's completion
          sends the node as a new revision;
        - otherwise (idle, or holding the order dropped or on a replaced route): send the
          node now as a new revision, from the waypoints not reached yet.
        A cancel already under way (the mission's) decides instead. Returns whether the
        message was fully handled."""
        self._resume_pending = False
        mission = self._current_mission
        if mission.needs_canceled or self._has_outstanding_cancel():
            return False
        current = self._current_order_id()
        executing = bool(message.nodeStates or message.edgeStates)
        if current is not None and message.orderId == current and \
                self._order_route_is_current(current, self._current_leaf_node()) and \
                (executing or self._order_finished_on_robot(message)):
            self.mission_info(f"Resume: the robot is on {current}; carrying on")
            return False
        if await self._cancel_order(
                CancelPurpose.REPLACE, "resume",
                f"Resume: the robot is executing {message.orderId}; cancelling it before "
                "sending", ours=OURS_DISPATCHER):
            return True
        # (Executing an order of its own: not ours to cancel; the node goes out now and the
        # robot's answer to it says whether it takes it.)
        self._pending_send = NEW_REVISION
        await self._flush_pending_send()
        return True

    def _current_leaf_node(self) -> Optional[mission_object.MissionNodeV1]:
        node = self._current_behavior_tree.current_node \
            if self._current_behavior_tree is not None else None
        if not isinstance(node, behavior_tree.MissionLeafNode):
            return None
        return self._current_mission.mission_tree[node.idx]

    def _order_finished_on_robot(self, message: types.VDA5050State) -> bool:
        """Whether the robot, holding the current order, reports having reached its last
        node (a route or move: the rest of the state handling completes the node). An
        action order counts as finished; its action states say how it went."""
        node = self._current_leaf_node()
        if node is None or node.type not in (mission_object.MissionNodeType.ROUTE,
                                             mission_object.MissionNodeType.MOVE):
            return True
        if not order_ids.is_node_of(self._order_prefix(), message.lastNodeId):
            return False
        seq = order_ids.node_sequence(message.lastNodeId)
        if seq is None:
            seq = message.lastNodeSequenceId
        if node.type is mission_object.MissionNodeType.MOVE:
            return seq == 2
        return seq == (len(node.route.waypoints) - self._waypoint_offset(message.orderId)) * 2

    def _reached_waypoints(self) -> int:
        """How many waypoints of the current route node the robot has reached (they are
        recorded against the current route only; see update_mission_node_state)."""
        node = self._current_behavior_tree.current_node
        if not isinstance(node, behavior_tree.MissionLeafNode):
            return 0
        name = str(self._current_mission.mission_tree[node.idx].name)
        reached = self._current_mission.status.task_status.get(name)
        return 0 if reached is None else reached + 1

    async def _on_client_factsheet(self, message: types.VDA5050Factsheet):
        if self._robot_object is not None:
            self._robot_object.status.factsheet.agv_class = message.typeSpecification.agvClass
            # Speeds, accelerations and the footprint, all kept (run analysis derives expected
            # leg times from them); a missing or nonsense value keeps the stored one (-1 = unknown).
            physical = message.physicalParameters
            for field, key, minimum in robot_object.FACTSHEET_PHYSICAL_FIELDS:
                value = getattr(physical, key, None)
                if value is not None and value >= minimum:
                    setattr(self._robot_object.status.factsheet, field, value)

            # Store custom actions from factsheet (an empty list clears what was stored)
            if message.actions is not None:
                self._robot_object.status.factsheet.custom_actions = [
                    robot_object.CustomActionV1(
                        action_type=action.actionType,
                        action_description=action.actionDescription,
                        action_parameters=[
                            robot_object.CustomActionParameterV1(key=param.key, value="" if param.value is None else str(param.value))
                            for param in action.actionParameters
                        ],
                        blocking_type=action.blockingType.value,
                        icon_hint=action.iconHint
                    )
                    for action in message.actions
                ]
                self.info(f"Stored {len(message.actions)} custom actions from factsheet")

            await self._write_status(api_objects.RobotObjectV1, self._robot_object.name, self._robot_object.status, self._writer_id())

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
        status.sent_order = None
        status.skipped_nodes = []
        status.node_notes = []
        status.offset_summary = None
        try:
            await self._write_status(api_objects.MissionObjectV1, mission.name, status, self._mission_writer_id())
        except Exception as err:  # pylint: disable=broad-except
            self.warning(f"[{mission.name}] Could not persist the next pass ({err}); "
                         "finishing the mission instead")
            return False
        mission.status = status
        self._order_mismatch_count = 0
        self._order_revisions.clear()
        self._pending_send = None
        self._deferred_replace_cancel = False
        self._blocked_order_id = None
        self._node_reports_seen.clear()
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
            if self._will_run_another_pass():
                if await self._start_next_pass():
                    return
                # No pass follows after all: the robot is not on task any more.
                self._set_robot_idle_after_mission()
            if self._current_mission.status.state == mission_object.MissionStateV1.COMPLETED:
                # The last pass is a finished pass too: "lap 3 / 3" reads passes_completed.
                final = self._current_mission
                final.status.passes_completed += 1
                try:
                    await self._write_status(api_objects.MissionObjectV1, final.name, final.status, self._mission_writer_id())
                except Exception as err:  # pylint: disable=broad-except
                    self.warning(f"[{final.name}] Could not persist the pass count ({err})")
                await self._chain_then_run(final)
        self._record("run_finished", self._name, self._current_mission, self._robot_object)
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

    @staticmethod
    def _remaining_timeout_s(mission: api_objects.MissionObjectV1) -> Optional[float]:
        """What is left of `mission`'s timeout since its start_timestamp (written as a naive
        local datetime.now(); an aware one is compared in its own zone), or None to use the
        whole timeout (no start, or no timeout)."""
        started = mission.status.start_timestamp
        if started is None or mission.timeout is None:
            return None
        now = datetime.datetime.now(started.tzinfo) if started.tzinfo else datetime.datetime.now()
        return max(mission.timeout.total_seconds() - (now - started).total_seconds(), 0.0)

    def _arm_mission_timeout(self, remaining_s: Optional[float] = None):
        """(Re)start the mission timeout watchdog for the current mission, with the
        mission's whole timeout or, resuming one paused while the robot was offline, what
        was left of it.

        Any previously scheduled timeout is cancelled first. Used both on initial
        dispatch and when resuming after an edgeBlocked condition clears."""
        self._cancel_mission_timeout()
        if self._current_mission is None or self._current_mission.timeout is None:
            return  # no time limit set: only the robot's report (or a cancel) ends it
        budget = self._current_mission.timeout.total_seconds() if remaining_s is None \
            else remaining_s
        self._timeout_budget = (self._current_mission.name, budget, time.monotonic())
        self._mission_timeout_task = asyncio.get_event_loop().create_task(
            self._run_mission_timer(budget, self._timeout_budget))

    def _cancel_mission_timeout(self):
        """Cancel the mission timeout watchdog (e.g. while the mission is blocked and
        legitimately waiting for an operator reroute, so it is not failed as TIMEOUT)."""
        if self._mission_timeout_task is not None:
            self._mission_timeout_task.cancel()
            self._mission_timeout_task = None
        self._timeout_budget = None
        self._timeout_paused = None

    def _pause_mission_timeout(self):
        """The robot went offline: stop the mission timeout and keep what is left of it.
        A robot carries a released route out on its own, so time offline is not a stall;
        failing it would also send a cancelOrder the robot cannot receive."""
        if not order_policy.current().timeout_pause_offline or \
                self._mission_timeout_task is None or self._timeout_budget is None:
            return
        if self._current_mission is not None and self._current_mission.needs_canceled:
            return  # the backstop of a cancel the offline robot cannot answer
        name, budget, started = self._timeout_budget
        remaining = max(budget - (time.monotonic() - started), 0.0)
        self._cancel_mission_timeout()
        self._timeout_paused = (name, remaining)
        self.mission_info(f"Robot offline: mission timeout paused ({remaining:.0f} s left)")

    def _resume_mission_timeout(self):
        """The robot is back: run the rest of a timeout paused while it was offline."""
        paused, self._timeout_paused = self._timeout_paused, None
        if paused is None or self._current_mission is None:
            return
        name, remaining = paused
        if name != self._current_mission.name or self._current_mission.status.state.done \
                or self._current_mission.status.blocked:
            return
        self._arm_mission_timeout(remaining)
        self.mission_info(f"Robot online: mission timeout resumed ({remaining:.0f} s left)")

    async def _run_mission_timer(self, timeout: float, token: Tuple[str, float, float]):
        await asyncio.sleep(timeout)
        await self.send_message(MissionTimeoutElapsed(token=token))

    async def _on_mission_timeout_elapsed(self, message: MissionTimeoutElapsed):
        """The timer ran out: handled here on the message loop (see MissionTimeoutElapsed)."""
        if self._timeout_budget != message.token:
            return
        await self._fail_mission_on_timeout(message.token[0])

    async def _fail_mission_on_timeout(self, name: str):
        """End mission `name` if it is still the current, RUNNING one: FAILED as a timeout, or
        CANCELED if it is being cancelled or deleted. The robot is told to drop its order and
        the queue moves on."""
        # Check to see if the mission that launched this timer is still running
        if (self._current_mission is None) or (self._robot_object is None):
            return

        if name == self._current_mission.name and \
                self._current_mission.status.state == mission_object.MissionStateV1.RUNNING:
            # A mission blocked on an impassable edge is legitimately waiting for an
            # operator reroute — never fail it as a timeout. (The timeout task is also
            # cancelled on block; this guards the rare cancel/fire race.)
            if self._current_mission.status.blocked:
                return
            # In case there is no response from the client. A mission deleted while it
            # ran ends like a cancelled one (its order dropped, the queue moves on).
            deleted = await self._robot_server.delete_pending_mission(self._current_mission)
            if deleted or self._current_mission.needs_canceled:
                self._set_mission_state(mission_object.MissionStateV1.CANCELED)
            else:
                self._current_mission.status.failure_reason = \
                    fleet_recorder.MISSION_TIMEOUT_REASON
                self._set_failure_category(mission_object.MissionFailureCategoryV1.TIMEOUT)
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
            # That order is the run's own, which the robot reports under its id, or one
            # not reported yet; the robot's own order is left alone (see _cancel_order,
            # OURS_SENT). One cancel at a time, as on the explicit-cancel path (a
            # cancelOrder is usually already outstanding there, and
            # handle_instant_action() keeps resending it whichever mission is current).
            await self._cancel_order(
                CancelPurpose.STOP, "timeout", "Sending cancelOrder so the robot abandons "
                "the timed-out order", ours=OURS_SENT)
            self._set_robot_idle_after_mission()
            await self.get_next_mission()

    async def _fail_missions_of_deleted_robot(self):
        """The robot is being deleted: its current mission and every queued one fail with
        ROBOT_DELETED_REASON (none can run any more); the current order is cancelled if
        there is one and the robot can hear it."""
        current = self._current_mission
        if current is not None and not current.status.state.done:
            if current.status.failure_reason is None:
                current.status.failure_reason = ROBOT_DELETED_REASON
            if self._robot_object is not None and self._robot_object.status.online:
                try:
                    await self._cancel_order(
                        CancelPurpose.STOP, "deleted", "Robot deleted: sending cancelOrder",
                        ours=OURS_SENT)
                except Exception as err:  # pylint: disable=broad-except
                    self.warning(f"Could not cancel the order of a deleted robot: {err}")
            self._set_mission_state(mission_object.MissionStateV1.FAILED)
            if self._current_behavior_tree is not None:
                self._record("run_finished", self._name, current, self._robot_object)
        for mission in list(self._missions.values()):
            if mission is current or mission.status.state.done:
                continue
            mission.status.failure_reason = ROBOT_DELETED_REASON
            mission.status.state = mission_object.MissionStateV1.FAILED
            mission.status.end_timestamp = datetime.datetime.now()
            for node_state in mission.status.node_status.values():
                if not node_state.state.done:
                    node_state.state = mission_object.MissionStateV1.FAILED
            try:
                await self._write_status(api_objects.MissionObjectV1, mission.name, mission.status,
                    self._mission_writer_id())
            except Exception as err:  # pylint: disable=broad-except
                self.warning(f"[{mission.name}] Could not persist the failure ({err})")

    async def _delete_robot_object(self):
        if self._robot_object is not None:
            await self._fail_missions_of_deleted_robot()
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
            # - An order that left out the waypoints already reached (a resume) starts
            #   at waypoint `offset` of the route
            offset = self._waypoint_offset(message.orderId)
            idx = offset + last_node_seq_id // 2 - 1

            # For route nodes, task index corresponds to the last waypoint reached.
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
            # Every waypoint counts, whatever its allowed deviation: a planner go-to has
            # waypoints with a 0.2 m tolerance and still needs its progress reported.
            # Only progress on the current route counts: an order the robot still drives
            # while a reroute replaces it indexes the old one, and finishing it is not
            # finishing the new one (the reroute's cancel then lets the new one go out).
            route_current = self._order_route_is_current(message.orderId,
                                                         current_mission_node)
            if route_current and \
                    self.last_node_seq_id < last_node_seq_id and \
                    idx >= offset and \
                    idx < len(current_mission_node.route.waypoints):
                task_status[str(current_mission_node.name)] = idx
                self._queue_status_write(api_objects.MissionObjectV1, self._current_mission.name, self._current_mission.status, self._mission_writer_id())

            if route_current and \
                    current_order_node_id == (current_mission_node.route.size - offset) * 2 + 2:
                node_state = mission_object.MissionStateV1.COMPLETED

        elif current_mission_node.type == mission_object.MissionNodeType.MOVE and \
                current_mission_node.move is not None and \
                current_order_node_id == 1 * 2 + 2:
            node_state = mission_object.MissionStateV1.COMPLETED
        elif current_mission_node.type == mission_object.MissionNodeType.ACTION:
            action_state = self._order_action_state(message, mission_node_index)
            if action_state is None:
                pass
            elif action_state.actionStatus == types.VDA5050ActionStatus.FINISHED:
                node_state = mission_object.MissionStateV1.COMPLETED
            elif action_state.actionStatus == types.VDA5050ActionStatus.FAILED:
                node_state = mission_object.MissionStateV1.FAILED
            # Check if this is a teleop action node
            elif action_state.actionType == types.NVActionType.PAUSE_ORDER and \
                self._robot_object is not None and \
                    self._robot_object.status.state != robot_object.RobotStateV1.TELEOP:
                self._set_robot_state(robot_object.RobotStateV1.TELEOP)
                self.mission_info("Switch to teleop")
        # A finished cancelOrder of ours is acted on by what it was for, before this
        # (_act_on_resolved_cancels).

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

    @staticmethod
    def _edge_blocked_error(message: types.VDA5050State) -> Optional[types.VDA5050Error]:
        """The edgeBlocked error the robot reports, if any."""
        return next((e for e in message.errors if e.errorType == "edgeBlocked"), None)

    def _order_action_state(self, message: types.VDA5050State, node_idx: int) \
            -> Optional[types.VDA5050ActionState]:
        """The state of the action an action order of mission node `node_idx` carries.
        Matched by actionId ("{order}-s0-n{idx}", see VDA5050Order.from_action), so
        another action listed first (an order action of the previous route, an instant
        action) does not complete or fail the node. A robot reporting other ids (legacy)
        falls back to the first entry that is neither a nodePolicy nor an instant action;
        None when there is none."""
        expected = order_ids.order_action_id(message.orderId, node_idx)
        match = next((s for s in message.actionStates if s.actionId == expected), None)
        if match is not None:
            return match
        return next((s for s in message.actionStates
                     if s.actionType not in _NON_NODE_ACTION_TYPES), None)

    def _resolve_node_ref(self, node_id: str) -> Optional[Dict[str, Any]]:
        """What a nodeId the robot reported names in the current run: its order, mission
        node, waypoint index and graph node, as far as known; None for a node that is not
        of this run (any revision). The waypoint index is only known for the order last
        built (its offset is recorded); the graph node comes from the waypoint's node_id,
        else from planned_path for a mission with a single route node."""
        mission = self._current_mission
        if mission is None:
            return None
        node_id = order_ids.node_of_reference(node_id)
        order_id = order_ids.order_of_node(node_id)
        if not self._is_order_of_run(order_id):
            return None
        idx = order_ids.node_index(node_id)
        seq = order_ids.node_sequence(node_id)
        ref: Dict[str, Any] = {"node_id": node_id, "order_id": order_id, "mission_node": None,
                               "waypoint_index": None, "graph_node_id": None}
        if idx is None or idx >= len(mission.mission_tree):
            return ref
        node = mission.mission_tree[idx]
        if node.route is not None and not self._order_route_is_current(order_id, node):
            return None  # about a route a reroute has replaced
        ref["mission_node"] = str(node.name)
        if seq is None or seq < 2 or node.route is None or \
                self._sent_order_record(order_id) is None:
            return ref
        index = self._waypoint_offset(order_id) + seq // 2 - 1
        if index >= len(node.route.waypoints):
            return ref
        ref["waypoint_index"] = index
        graph_node = node.route.waypoints[index].node_id
        routes = [n for n in mission.mission_tree if n.route is not None]
        if graph_node is None and len(routes) == 1 and mission.planned_path and \
                index < len(mission.planned_path):
            graph_node = str(mission.planned_path[index])
        ref["graph_node_id"] = graph_node
        return ref

    def _exclude_blocked_node(self, node_ref: str, source: str, reason: Optional[str]) -> None:
        """Keep the graph node of a waypoint the robot reported blocked out of new routes
        for BLOCKED_NODE_EXCLUSION_MIN (blocked_graph_nodes, read by the planner), for every
        robot on the map. Best effort: a waypoint on no map is skipped, and a failed write
        only logs. When the waypoint's graph node is not known, its position is kept out."""
        ref = self._resolve_node_ref(node_ref)
        if ref is None or ref["waypoint_index"] is None:
            return
        node = next(n for n in self._current_mission.mission_tree
                    if str(n.name) == ref["mission_node"])
        index = ref["waypoint_index"]
        waypoint = node.route.waypoints[index]
        if not waypoint.map_id:
            return
        previous = node.route.waypoints[index - 1].node_id if index > 0 else None
        graph_node = ref["graph_node_id"] or blocked_nodes.synthetic_id(waypoint.x, waypoint.y)
        params = blocked_nodes.upsert_params(
            waypoint.map_id, graph_node, source,
            order_policy.current().blocked_node_exclusion_min * 60.0,
            edge_from=previous, edge_to=ref["graph_node_id"], robot_name=self._name,
            mission_name=str(self._current_mission.name), vda_node_id=ref["node_id"],
            reason=reason, x=waypoint.x, y=waypoint.y)
        self.mission_info(f"Keeping graph node {graph_node} on map {waypoint.map_id} out of "
                          f"new routes for {order_policy.current().blocked_node_exclusion_min:g}"
                          f" min ({source})")
        # Already deduped by the caller (acted on only when the block is new or its target
        # changed); the reference only keeps the task from being garbage collected.
        task = asyncio.ensure_future(self._write_blocked_node(params))
        self._blocked_node_tasks.add(task)
        task.add_done_callback(self._blocked_node_tasks.discard)

    async def _write_blocked_node(self, params: Tuple[Any, ...]) -> None:
        try:
            async with self._database.connection() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(blocked_nodes.UPSERT_SQL, params)
        except Exception as err:  # pylint: disable=broad-except
            self.warning(f"Could not store blocked graph node {params[1]} ({err}); new "
                         "routes may still go through it")

    def _process_node_reports(self, message: types.VDA5050State) -> None:
        """Keep what the robot reports about single nodes on the mission: nodes it skipped
        (nodeSkipped WARNING) and advisory notes (informations with a nodeId reference:
        node moved by an offset, seen blocked, area not observed). Neither changes the
        mission or sends anything; after a reconnect the first state carries everything
        that happened offline, so each report is kept once."""
        mission = self._current_mission
        if mission is None:
            return
        status = mission.status
        now = datetime.datetime.now()
        changed = False
        seen = self._node_reports_seen
        for error in message.errors:
            if error.errorType != NODE_SKIPPED:
                continue
            for ref_value in (r.referenceValue for r in error.errorReferences
                              if r.referenceKey in ("nodeId", "node_id")):
                key = (NODE_SKIPPED, order_ids.node_of_reference(ref_value), "")
                if key in seen:
                    continue
                ref = self._resolve_node_ref(ref_value)
                seen[key] = None
                if ref is None or any(s.node_id == ref["node_id"] and
                                      s.order_id == ref["order_id"]
                                      for s in status.skipped_nodes):
                    continue
                if error.errorLevel == types.VDA5050ErrorLevel.FATAL:
                    self.warning(f"[{mission.name}] nodeSkipped reported as FATAL; read as "
                                 "a warning")
                skipped = mission_object.MissionSkippedNodeV1(
                    **ref, description=error.errorDescription, first_seen=now)
                status.skipped_nodes.append(skipped)
                del status.skipped_nodes[:-MISSION_SKIPPED_NODES_MAX]
                changed = True
                self.mission_info(f"Robot skipped node {ref['node_id']} (waypoint "
                                  f"{ref['waypoint_index']}): {error.errorDescription}")
                self._record("node_skipped", self._name, mission, skipped, self._event_ts)
        for info in message.information or []:
            if info.infoType == "missionStatus":
                continue
            refs = {r.referenceKey: r.referenceValue for r in info.infoReferences}
            node_ref = refs.get("nodeId") or refs.get("node_id")
            if not node_ref:
                continue
            key = ("note", order_ids.node_of_reference(node_ref), info.infoType)
            if key in seen:
                # Again (also after the cap dropped it): only last_seen moves, stored with
                # the mission's next status write.
                if seen[key] is not None:
                    seen[key].last_seen = now
                continue
            ref = self._resolve_node_ref(node_ref)
            seen[key] = None
            if ref is None:
                continue
            # Kept before this process started (the status is stored).
            existing = next((n for n in status.node_notes
                             if n.node_id == ref["node_id"] and n.order_id == ref["order_id"]
                             and n.info_type == info.infoType), None)
            if existing is not None:
                seen[key] = existing
                existing.last_seen = now
                continue
            offset = self._note_offset(refs)
            note = mission_object.MissionNodeNoteV1(
                **ref, info_type=info.infoType, description=info.infoDescription,
                offset=offset, offset_map=self._offset_in_map(offset, ref["order_id"]),
                first_seen=now, last_seen=now)
            seen[key] = note
            status.node_notes.insert(0, note)
            del status.node_notes[MISSION_NODE_NOTES_MAX:]
            changed = True
            self.mission_info(f"Robot note on node {ref['node_id']} ({info.infoType}): "
                              f"{info.infoDescription}")
            self._record("node_note", self._name, mission, note, self._event_ts)
            if note.offset_map is not None:
                self._update_offset_summary()
        if changed:
            self._queue_status_write(api_objects.MissionObjectV1, mission.name, status, self._mission_writer_id())

    def _drop_node_reports(self, mission_node: str) -> None:
        """A reroute replaced `mission_node`'s route: its waypoint indices and graph nodes
        named the old route, so the robot's reports on it are dropped (the offset summary
        is taken again from the notes left)."""
        status = self._current_mission.status
        status.skipped_nodes = [s for s in status.skipped_nodes
                                if s.mission_node != mission_node]
        notes = [n for n in status.node_notes if n.mission_node != mission_node]
        if len(notes) != len(status.node_notes):
            status.node_notes = notes
            status.offset_summary = None
            self._update_offset_summary()

    @staticmethod
    def _note_offset(refs: Dict[str, str]) -> Optional[Dict[str, float]]:
        """The offset a note carries (offsetX/offsetY[/offsetTheta] references), if any;
        None for one that is not a finite number."""
        try:
            if "offsetX" not in refs or "offsetY" not in refs:
                return None
            offset = {"dx": float(refs["offsetX"]), "dy": float(refs["offsetY"]),
                      "dtheta": float(refs.get("offsetTheta", 0.0))}
        except ValueError:
            return None
        return offset if all(math.isfinite(v) for v in offset.values()) else None

    def _offset_in_map(self, offset: Optional[Dict[str, float]],
                       order_id: Optional[str]) -> Optional[Dict[str, float]]:
        """An offset in the order's frame rotated into the map frame, by the transform the
        order was sent with (MissionSentOrderV1.frame); as it is for an order sent without
        one, None for an order whose frame is not recorded."""
        if offset is None:
            return None
        record = self._sent_order_record(order_id)
        frame = record.frame if record is not None else None
        if frame is None:
            return None
        yaw = float((frame.get("map_t_session") or {}).get("yaw", 0.0)) \
            if frame.get("applied") == "inverse" else 0.0
        c, s = math.cos(yaw), math.sin(yaw)
        return {"dx": c * offset["dx"] - s * offset["dy"],
                "dy": s * offset["dx"] + c * offset["dy"]}

    def _update_offset_summary(self) -> None:
        """Take the run's node offsets together; when they all point the same way, warn
        once per run: a frame error between graph and robot map, not node placement."""
        mission = self._current_mission
        status = mission.status
        offsets = [n.offset_map for n in status.node_notes if n.offset_map is not None]
        if not offsets:
            return
        n = len(offsets)
        mean_dx = sum(o["dx"] for o in offsets) / n
        mean_dy = sum(o["dy"] for o in offsets) / n
        mean_norm = sum(math.hypot(o["dx"], o["dy"]) for o in offsets) / n
        consistency = math.hypot(mean_dx, mean_dy) / mean_norm if mean_norm > 0 else 0.0
        already = status.offset_summary is not None and \
            status.offset_summary.suspected_frame_error
        suspected = n >= OFFSET_SUSPECT_MIN_N and consistency >= OFFSET_SUSPECT_CONSISTENCY \
            and math.hypot(mean_dx, mean_dy) >= OFFSET_SUSPECT_MIN_M
        status.offset_summary = mission_object.MissionOffsetSummaryV1(
            n=n, mean_dx=mean_dx, mean_dy=mean_dy, mean_norm=mean_norm,
            consistency=consistency, suspected_frame_error=already or suspected)
        if suspected and not already:
            record = status.sent_order
            frame = record.frame if record is not None else None
            self.warning(f"[{mission.name}] {n} node offsets point the same way (mean "
                         f"{mean_dx:+.2f}, {mean_dy:+.2f} m, consistency {consistency:.2f}): "
                         f"likely a frame error; order frame {frame}")
            self._record("frame_offset_suspected", self._name, mission,
                         status.offset_summary,
                         (frame or {}).get("map_t_session"), self._event_ts)

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
        blocked_error = self._edge_blocked_error(message)

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
                blocked_order = order_ids.order_of_node(ref.referenceValue)
                route_node = self._current_mission.mission_tree[node_idx] \
                    if blocked_node_name is not None else None
                if seq is not None and (route_node is None or
                                        self._order_route_is_current(blocked_order,
                                                                     route_node)):
                    blocked_waypoint_index = seq // 2 - 1 + self._waypoint_offset(
                        blocked_order)

        # The order the robot reports the block on (a reroute's order can be blocked
        # too, on the same node).
        self._blocked_order_id = message.orderId
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
        self._record("edge_blocked", self._name, self._current_mission, self._event_ts)
        # Routes planned from now on (this robot's reroute, other robots) avoid the node.
        # The robot's per-node "seen blocked" notes are advisory and do not do this.
        blocked_ref = next((r.referenceValue for r in blocked_error.errorReferences
                            if r.referenceKey in ("nodeId", "node_id")), None)
        if blocked_ref is not None:
            self._exclude_blocked_node(blocked_ref, "edgeBlocked",
                                       blocked_error.errorDescription)

        self.warning(
            f"Edge blocked: node={blocked_node_name} edge={blocked_edge} "
            f"reason={blocked_error.errorDescription!r}; mission stays RUNNING, "
            "awaiting reroute")

        # The robot has stopped and is IDLE; reflect that and stop the timeout from
        # failing a mission that is legitimately waiting for an operator reroute.
        self._set_robot_state_unless_teleop(robot_object.RobotStateV1.IDLE)
        self._cancel_mission_timeout()

        self._queue_status_write(api_objects.MissionObjectV1, self._current_mission.name, status, self._mission_writer_id())
        return True

    def _clear_block(self, mission: api_objects.MissionObjectV1):
        """Clear a recorded edgeBlocked condition once the robot has resumed."""
        if not mission.status.blocked:
            return
        self._blocked_order_id = None
        blocked_node = mission.status.blocked_node
        self._record("rerouted", self._name, mission, blocked_node,
                     mission.status.blocked_edge, self._event_ts)
        mission.status.blocked = False
        mission.status.blocked_node = None
        mission.status.blocked_edge = None
        mission.status.blocked_waypoint_index = None
        mission.status.block_reason = None
        if blocked_node is not None and blocked_node in mission.status.node_status:
            mission.status.node_status[blocked_node].error_msg = None
        self.mission_info("Edge block cleared; mission resuming")
        # Robot is moving again; restore ON_TASK and re-arm the mission timeout.
        self._set_robot_state_unless_teleop(robot_object.RobotStateV1.ON_TASK)
        if mission is self._current_mission:
            self._arm_mission_timeout()
        self._queue_status_write(api_objects.MissionObjectV1, mission.name, mission.status, self._mission_writer_id())

    _NODE_REFERENCE_KEYS = ("node_id", "nodeId", "action_id", "actionId")
    _ORDER_REFERENCE_KEYS = _NODE_REFERENCE_KEYS + ("order_id", "orderId")

    def _order_rejection(self, message: types.VDA5050State) -> List[types.VDA5050Error]:
        """Errors in which the robot rejects the order we are waiting for it to adopt:
        rejection-type or FATAL errors that reference that order or one of its nodes or
        actions."""
        pending = self._current_order_id()
        found = []
        for error in message.errors:
            if error.errorType in ADVISORY_ERROR_TYPES or not (
                    error.errorLevel == types.VDA5050ErrorLevel.FATAL or
                    error.errorType in ORDER_REJECTION_ERROR_TYPES):
                continue
            for ref in error.errorReferences:
                if ref.referenceKey not in self._ORDER_REFERENCE_KEYS:
                    continue
                if pending is not None:
                    ours = order_ids.order_of_reference(ref.referenceValue) == pending
                else:
                    ours = order_ids.is_reference_of(
                        self._order_prefix(), ref.referenceValue) is True
                if ours:
                    found.append(error)
                    break
        return found

    def _is_foreign_error(self, error: types.VDA5050Error) -> bool:
        """Whether every id the error references that we generated belongs to another run,
        revision or mission than the current order's (and at least one such id exists).
        Ids we did not generate (robot-own, unparseable) do not make an error foreign."""
        prefix = self._order_prefix()
        verdicts = [order_ids.is_reference_of(prefix, r.referenceValue)
                    for r in error.errorReferences
                    if r.referenceKey in self._ORDER_REFERENCE_KEYS]
        verdicts = [v for v in verdicts if v is not None]
        return bool(verdicts) and not any(verdicts)

    @classmethod
    def _unreferenced_fatal_types(cls, message: types.VDA5050State) -> set:
        """errorTypes of FATAL errors that name no node or action."""
        return {e.errorType for e in message.errors
                if e.errorLevel == types.VDA5050ErrorLevel.FATAL and
                e.errorType not in ADVISORY_ERROR_TYPES and
                not any(r.referenceKey in cls._NODE_REFERENCE_KEYS
                        for r in e.errorReferences)}

    def _track_stale_fatal(self, message: types.VDA5050State):
        """Remember unreferenced FATAL errors that predate the current order.

        Until the robot reports an order of the current mission, whatever
        unreferenced FATAL it still carries was there before this mission's order
        (the robot reports errors until it clears them, and may echo our order id
        a message or two before it does). Once the order is accepted, the set only
        shrinks: an error type the robot has reported gone is no longer stale, so a
        new error of that type fails the mission as usual.
        """
        present = self._unreferenced_fatal_types(message)
        accepted = self._current_mission is not None and \
            order_ids.is_order_of(self._order_prefix(), message.orderId)
        if accepted:
            self._stale_fatal_types &= present
        else:
            self._stale_fatal_types = present

    def get_mission_errors(self, message: types.VDA5050State):
        fatal_errors = False
        counted: List[types.VDA5050Error] = []
        if len(message.errors) == 0:
            return False
        for error in message.errors:
            # Skip warnings, and reports that never fail a mission whatever their level
            if error.errorLevel != types.VDA5050ErrorLevel.FATAL or \
                    error.errorType in ADVISORY_ERROR_TYPES:
                continue
            if error.errorType in self._stale_fatal_types and not any(
                    r.referenceKey in self._NODE_REFERENCE_KEYS
                    for r in error.errorReferences):
                # Reported before this mission's order was accepted: not ours.
                continue
            if self._current_mission is not None and self._is_foreign_error(error):
                # Left over from an earlier run, revision or cancelled order.
                key = f"{error.errorType}:" + ",".join(
                    r.referenceValue for r in error.errorReferences)
                if key not in self._foreign_error_refs:
                    self._foreign_error_refs.add(key)
                    self.info(f"Ignoring FATAL error of an earlier order: "
                              f"{error.errorDescription} ({key})")
                continue
            counted.append(error)
            fatal_errors = True
            for error_reference in error.errorReferences:
                if error_reference.referenceKey in \
                        ["node_id", "nodeId", "action_id", "actionId"]:
                    mission_node = order_ids.node_index(error_reference.referenceValue)
                    if mission_node is None:
                        continue
                    if self._current_mission is not None and order_ids.is_reference_of(
                            self._order_prefix(), error_reference.referenceValue) is False:
                        continue  # another order's node: not this mission's node
                    if self._current_mission is not None and \
                            mission_node < len(self._current_mission.mission_tree):
                        (self._current_mission.status.node_status[
                            str(self._current_mission.mission_tree[mission_node].name)].error_msg) \
                            = error.errorDescription
        # The reason names every counted FATAL, with or without node/action references.
        if fatal_errors and self._current_mission is not None:
            self._current_mission.status.failure_reason = "\n".join(
                e.errorDescription for e in counted)
            self._set_failure_category(mission_object.MissionFailureCategoryV1.ROBOT_APP)
        return fatal_errors

    def _mission_progress_signature(self) -> Tuple[Any, ...]:
        """What a behavior tree update can change in the mission status besides what
        _set_mission_state reports: the current node and the node states (post_tick is the
        tree's only write). Cheaper than a deep copy and a model comparison per state
        message; a field the tree starts writing must be added here."""
        status = self._current_mission.status
        return (status.current_node, status.state,
                tuple(node.state for node in status.node_status.values()))

    def update_mission_from_behavior_tree(self):
        # update mission state from behavior tree
        if self._current_behavior_tree is None or self._current_mission is None:
            return
        # Record the old status and store the new status
        previous_signature = self._mission_progress_signature()
        # Update mission status
        self._current_behavior_tree.update()
        self._current_mission.status.current_node = self._current_behavior_tree.current_node.idx
        current_state = behavior_tree.tree2mission_state(
            self._current_behavior_tree.status)
        mission_state_updated = self._set_mission_state(current_state)
        # In case mission node status get updated but mission state remains the same
        if not mission_state_updated and \
                previous_signature != self._mission_progress_signature():
            self.info(
                f"update mission node: {self._current_mission.status.current_node}")
            self._queue_status_write(api_objects.MissionObjectV1, self._current_mission.name, self._current_mission.status, self._mission_writer_id())

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

        # Skipped nodes and per-node notes are kept whatever else the state says (also
        # while blocked, and in the state that completes the order after a reconnect).
        self._process_node_reports(message)

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
            self._set_failure_category(mission_object.MissionFailureCategoryV1.ROBOT_APP)
            self._set_mission_state(mission_object.MissionStateV1.FAILED)
            return
        if mission_status == "canceled":
            if self._current_mission.needs_canceled:
                self._set_mission_state(mission_object.MissionStateV1.CANCELED)
            elif self._edge_blocked_error(message) is not None:
                # The robot dropped its order because a node is blocked: resending the
                # same route would drive it straight back there (2026-10-08). Record the
                # block and wait for a reroute.
                self._handle_edge_blocked(message)
            elif self._current_mission.status.blocked:
                # The order dropped for the block, now without the error, keeps the block
                # until a reroute (clearing it here resent the route on the next
                # "canceled"). Only the reroute's own order being canceled ends it.
                if self._blocked_order_id is not None and \
                        message.orderId != self._blocked_order_id and \
                        message.orderId == self._current_order_id():
                    self._clear_block(self._current_mission)
            elif (message.nodeStates or message.edgeStates) and \
                    (message.driving or time.monotonic() - self._order_first_sent_at <
                     self.CANCELED_LEFTOVER_GRACE_S):
                # Still executing: a "canceled" left over from the previous order, not
                # about this one.
                pass

            elif not self._has_outstanding_cancel() and self._pending_send is None and \
                    message.orderId == self._current_order_id():
                # The robot dropped the current order without a cancel of ours (one in
                # flight decides by its own completion): send the node as a new revision.
                self._pending_send = NEW_REVISION
            return

        # A robot that has hit an impassable edge reports an edgeBlocked WARNING and
        # waits IDLE (it does not emit missionStatus="failed"). Record the block and
        # stop here so a waiting mission is not churned or advanced; it resumes once
        # an operator reroute clears the block.
        #
        # A cancelOrder the robot just finished (or answered with "no order to cancel")
        # must still end the mission: a blocked robot keeps reporting the edgeBlocked
        # warning, and returning here swallowed the cancel, so the mission stayed
        # RUNNING however often the operator pressed cancel (observed 2026-09-25).
        cancel_finished = any(
            a.actionType == types.VDA5050InstantActionType.CANCEL_ORDER
            for a in finished_instant_actions)
        if not cancel_finished and self._handle_edge_blocked(message):
            return

        # For "reached" (intermediate waypoint) and the no-info case, fall through
        # to the existing behavior-tree path so node-level tracking stays intact.
        self.update_mission_node_state(message, finished_instant_actions)
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
        # A route the robot finished after a reroute replaced it is not the node's route.
        if node.type is mission_object.MissionNodeType.ROUTE and \
                not self._order_route_is_current(message.orderId, node):
            return False
        self.set_mission_node_state(str(node.name), mission_object.MissionStateV1.COMPLETED)
        return True

    def _apply_spec_edit(self, target: api_objects.MissionObjectV1,
                         message: api_objects.MissionObjectV1, dispatched: bool):
        """Copy an operator's spec edit (PUT /missions/{name}) onto the mission this
        dispatcher already loaded. Only a mission that has not been dispatched yet can
        take one: a dispatched mission's orders are already with the robot."""
        # A reroute rewrites routes of the stored tree (and clears planned_path) under a
        # new route_rev that _update_mission_from_api has not applied yet, so on a
        # dispatched mission such a difference is expected and not an edit.
        reroute_pending = dispatched and message.route_rev > target.status.applied_route_rev
        changed = [field for field in EDITABLE_SPEC_FIELDS
                   if getattr(target, field) != getattr(message, field) and
                   not (reroute_pending and field in ("mission_tree", "planned_path"))]
        if not dispatched and target.route_rev != message.route_rev:
            # A reroute before the first order: the copied tree is the one to send.
            target.route_rev = message.route_rev
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

    def _node_key(self, mission_node: mission_object.MissionNodeV1
                  ) -> Tuple[str, str, Optional[str], int, int]:
        """Identifies one pass of a node the dispatcher runs itself (wait, notify)."""
        status = self._current_mission.status
        return (str(self._current_mission.name), str(mission_node.name),
                status.run_id, status.order_rev, status.passes_completed)

    def _start_wait(self, mission_node: mission_object.MissionNodeV1):
        """Start the timer of a "wait" action node. No order goes to the robot."""
        assert self._current_mission is not None and mission_node.action is not None
        status = self._current_mission.status
        seconds = float(mission_node.action.action_parameters["seconds"])
        key = self._node_key(mission_node)
        # _send_order() runs again for a node whenever the robot's state does not match
        # yet; the timer already running for this node must not be restarted by that.
        if self._wait_key == key:
            return
        self._cancel_wait()
        self._wait_key = key
        self.mission_info(f"Waiting {seconds:g}s at node {mission_node.name}")
        self.set_mission_node_state(str(mission_node.name),
                                    mission_object.MissionStateV1.RUNNING)
        self._queue_status_write(
            api_objects.MissionObjectV1, self._current_mission.name,
            self._current_mission.status, self._mission_writer_id())
        self._wait_task = asyncio.get_event_loop().create_task(
            self._run_wait_timer(seconds, self._wait_key))

    async def _run_wait_timer(self, seconds: float,
                              key: Tuple[str, str, Optional[str], int, int]):
        await asyncio.sleep(seconds)
        await self.send_message(WaitElapsed(key=key))

    def _cancel_wait(self):
        """Drop the running wait (and notify task), if any. Safe from any path that leaves
        the mission."""
        if self._notify_task is not None and self._notify_task is not asyncio.current_task():
            self._notify_task.cancel()
        self._notify_task = None
        self._notify_key = None
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
        await self._advance_after_node()

    async def _on_notify_done(self, message: NotifyDone):
        """A notify node ended (its node state is set): go on as after a wait."""
        if self._current_mission is None or self._notify_key != message.key or \
                self._current_behavior_tree is None:
            return
        self._notify_task = None
        self._notify_key = None
        await self._advance_after_node()

    async def _advance_after_node(self):
        """A node the dispatcher ran itself (wait, notify) ended: the mission is complete, or
        the next node's order goes out."""
        prev_child_node = self._current_behavior_tree.current_node.name
        self.update_mission_from_behavior_tree()
        if self._current_mission.status.state.done:
            await self.post_mission_completion()
        elif prev_child_node != self._current_behavior_tree.current_node.name:
            await self._send_order()

    async def run(self):
        while self._alive:
            # Outside the try on purpose: only a failing *handler* is survivable. If the
            # queue itself fails (e.g. "bound to a different event loop"), it fails on every
            # call, and swallowing that turned this loop into a hot spin that logged millions
            # of warnings a second (seen in the unit tests: 55+ GB of captured log records
            # before the host's OOM killer stepped in). Let the task end instead.
            message = await self._messages.get()
            if isinstance(message, _StateSlot):
                if self._state_slot is message:
                    self._state_slot = None
                if self._queue_tail is message:
                    self._queue_tail = None
                message = message.msg
                if message is None:
                    continue    # replaced by a newer state queued later (send_message)
            try:
                # If this is a robot object
                if isinstance(message, api_objects.RobotObjectV1):
                    await self._on_robot_change(message)
                elif isinstance(message, api_objects.MissionObjectV1):
                    await self._on_mission_change(message)
                elif isinstance(message, types.VDA5050State):
                    await self._on_state_message(message)
                elif isinstance(message, types.VDA5050Factsheet):
                    await self._on_client_factsheet(message)
                    self._record("on_factsheet", self._name, message)
                elif isinstance(message, types.RobotDatum):
                    await self._process_datum_message(message)
                elif isinstance(message, types.RobotApproxPosition):
                    await self._process_approx_position_message(message)
                elif isinstance(message, ConnectionDelivery):
                    await self._on_connection_message(message.connection, message.retained)
                elif isinstance(message, types.VDA5050Connection):
                    await self._on_connection_message(message)
                elif isinstance(message, WaitElapsed):
                    await self._on_wait_elapsed(message)
                elif isinstance(message, NotifyDone):
                    await self._on_notify_done(message)
                elif isinstance(message, MissionTimeoutElapsed):
                    await self._on_mission_timeout_elapsed(message)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # pylint: disable=broad-except
                self._log_loop_error(e)

    def _log_loop_error(self, err: Exception) -> None:
        """An exception that escaped a message handler: the first of each kind is logged at
        error with its traceback, repeats only as a count every LOOP_ERROR_SUMMARY_INTERVAL_S."""
        now = time.monotonic()
        key = (type(err).__name__, str(err))
        entry = self._loop_errors.get(key)
        if entry is None:
            if len(self._loop_errors) >= LOOP_ERROR_MAX_KINDS:
                oldest = min(self._loop_errors, key=lambda k: self._loop_errors[k][1])
                del self._loop_errors[oldest]
            self._loop_errors[key] = [0, now]
            self.error(f"Unhandled exception in robot message loop: {err!r}", exc_info=err)
            return
        entry[0] += 1
        if now - entry[1] >= LOOP_ERROR_SUMMARY_INTERVAL_S:
            self.error(f"Unhandled exception in robot message loop repeated {entry[0]} more "
                       f"time(s) in the last {now - entry[1]:.0f}s: {err!r}")
            entry[0] = 0
            entry[1] = now

    async def _on_state_message(self, message: types.VDA5050State):
        """A robot state message: dispatch handles it, then it is recorded -- afterwards, so
        the recorded robot state and run are the ones this message led to. Events the
        dispatcher raises meanwhile carry the message's timestamp."""
        self._event_ts = fleet_recorder.parse_robot_ts(message.timestamp, None)
        # Legs first: the state that reaches a mission's last node also completes the mission.
        self._record("on_leg_state", self._name, message, self._current_mission,
                     self._robot_object)
        if not self._run_checked and time.monotonic() >= self._run_check_after:
            await self._check_run_continuity(message.headerId)
        was_pending = self._run_detector.pending_state_drop is not None
        evidence = self._run_detector.on_state(message.headerId)
        if self._run_detector.pending_state_drop is not None and not was_pending:
            # The pose in memory is still the old run's (this message's is stored below).
            # (a copy: the pose is updated in place by every state message)
            self._pre_drop_pose = self._robot_object.status.pose.copy() \
                if self._robot_object is not None else None
        if evidence is not None:
            await self._on_run_changed(evidence)
        else:
            await self._persist_run_header(message.headerId)
        try:
            await self._on_client_message(message)
        finally:
            self._event_ts = None
            self._record("on_state", self._name, message, self._robot_object)

    async def send_message(self, message):
        """Queue a message for the loop in run(). Only the newest state message of the robot
        is kept: VDA5050 state is cumulative (full actionStates list, node progress, errors),
        so a loop that is behind handles the newest state instead of replaying stale ones.
        A state replaces the unprocessed one in place when nothing else was queued after it;
        otherwise its predecessor is emptied and the new state is queued at its own place, so
        the order relative to connection, order and timer messages is kept."""
        if isinstance(message, types.VDA5050State):
            slot = self._state_slot
            if slot is not None:
                self._states_coalesced += 1
                if self._states_coalesced % STATE_COALESCED_WARN_EVERY == 1:
                    self.warning(f"Robot state message loop is behind: unprocessed state "
                                 f"replaced by a newer one ({self._states_coalesced} so far)")
                if self._queue_tail is slot:
                    slot.msg = message
                    return
                slot.msg = None
            slot = self._state_slot = self._queue_tail = _StateSlot(message)
            await self._messages.put(slot)
            return
        self._queue_tail = None
        await self._messages.put(message)

    def _robot_status_signature(self) -> Tuple[Any, ...]:
        """The robot row's discrete fields, i.e. everything a state message changes except
        the continuously changing pose, battery level, localization score and deviation."""
        status = self._robot_object.status
        info = status.info_messages
        if info:
            info = {k: v for k, v in info.items() if k != "deviation_range"}
        return (status.online, status.state, status.errors, status.position_initialized,
                status.pose.map_id, status.battery_unknown, status.recording_state,
                status.nav_reasoning, info, status.hardware_version.manufacturer,
                status.hardware_version.serial_number)

    def _store_robot_status(self) -> None:
        """Write the robot row after a state message, off the state loop (the status write
        queue). A discrete change goes out at once; if only continuous fields changed, at
        most every ROBOT_STATUS_MIN_WRITE_S, with a trailing write of the newest values so
        the last state before a quiet period is always stored."""
        signature = self._robot_status_signature()
        now = time.monotonic()
        waited = now - self._robot_written_at
        if signature != self._robot_written_sig or waited >= ROBOT_STATUS_MIN_WRITE_S:
            self._write_robot_status(signature, now)
        elif self._robot_write_timer is None and self._alive:
            self._robot_write_timer = asyncio.get_event_loop().call_later(
                ROBOT_STATUS_MIN_WRITE_S - waited, self._robot_trailing_write)

    def _write_robot_status(self, signature: Tuple[Any, ...], now: float) -> None:
        if self._robot_write_timer is not None:
            self._robot_write_timer.cancel()
            self._robot_write_timer = None
        self._queue_status_write(api_objects.RobotObjectV1, self._robot_object.name,
                                 self._robot_object.status, self._writer_id())
        self._robot_written_sig, self._robot_written_at = signature, now

    def _robot_trailing_write(self) -> None:
        self._robot_write_timer = None
        if self._alive and self._robot_object is not None and \
                self._robot_object.lifecycle is not api_objects.object.ObjectLifecycleV1.DELETED:
            self._write_robot_status(self._robot_status_signature(), time.monotonic())

    def mission_info(self, message: str):
        if self._current_mission is not None:
            mission = "Mission ID - " + self._current_mission.name
        else:
            mission = "None"
        self._logger.info("[Isaac Mission Dispatch] | INFO: [%s] [%s] %s",
                          self._name, mission, message)

    def debug(self, message: str, *args):
        """(`args` are %-formatted into `message` only when DEBUG is enabled: per-state calls
        pass them instead of building an f-string that is thrown away.)"""
        if self._logger.isEnabledFor(logging.DEBUG):
            self._logger.debug(
                "[Isaac Mission Dispatch] | DEBUG: [%s] %s", self._name,
                message % args if args else message)

    def warning(self, message: str):
        self._logger.warning(
            "[Isaac Mission Dispatch] | WARNING: [%s] %s", self._name, message)

    def error(self, message: str, exc_info: Any = False):
        self._logger.error(
            "[Isaac Mission Dispatch] | ERROR: [%s] %s", self._name, message, exc_info=exc_info)

    def info(self, message: str):
        self._logger.info(
            "[Isaac Mission Dispatch] | INFO: [%s] %s", self._name, message)

    def _writer_id(self) -> uuid.UUID:
        """Publisher id of this controller's robot-object writes: the robot watcher of the
        RobotServer skips notifications with it, so our own writes do not come back as
        robot changes (they carried a possibly older copy of the row; see _on_robot_change)."""
        writer = getattr(self._robot_server, "robot_writer_id", None)
        return writer if isinstance(writer, uuid.UUID) else uuid.uuid4()

    def _mission_writer_id(self) -> uuid.UUID:
        """Publisher id of every mission write this controller makes. The mission watcher of
        the RobotServer skips notifications with it, so our own status writes do not come
        back as mission changes (each used to echo, re-delivering the row to the handler
        that had just written it; see the reroute loop of 2026-10)."""
        writer = getattr(self._robot_server, "mission_writer_id", None)
        return writer if isinstance(writer, uuid.UUID) else uuid.uuid4()

    def _reconcile_stale_state(self) -> None:
        """ON_TASK / MAP_DEPLOYMENT only ever mean "this controller is running a mission". With
        none current or queued they are stale: a state left in the database by an older
        process or written back by a stale watcher echo (see _on_robot_change). Fixed to IDLE
        (with ROBOT.STATE_CHANGED), but only STALE_STATE_GRACE_S after this controller was
        created, so a mission the dispatcher resumes after its own restart is queued first."""
        if self._robot_object is None or self._current_mission is not None or self._missions:
            return
        state = self._robot_object.status.state
        if state not in (robot_object.RobotStateV1.ON_TASK,
                         robot_object.RobotStateV1.MAP_DEPLOYMENT):
            return
        if time.monotonic() - self._created_at < STALE_STATE_GRACE_S:
            return
        self.warning(f"Robot state {state.value} without a mission: setting it to IDLE")
        self._set_robot_state(robot_object.RobotStateV1.IDLE)

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
        self._record("on_robot_state", self._name, self._robot_object.status.state, state,
                     self._event_ts)
        self._robot_object.status.state = state
        self._queue_status_write(api_objects.RobotObjectV1, self._robot_object.name, self._robot_object.status, self._writer_id())

    def _set_robot_state_unless_teleop(self, state: robot_object.RobotStateV1):
        """_set_robot_state, except that a teleoperated robot stays TELEOP: only a
        teleop instant action leaves it, and the dispatcher only sends stopTeleop while
        it believes the robot is in TELEOP."""
        if self._robot_object is not None and \
                self._robot_object.status.state == robot_object.RobotStateV1.TELEOP:
            return
        self._set_robot_state(state)

    def _set_robot_idle_after_mission(self):
        """The robot's state once a mission has ended -- unless it is teleoperated.

        A mission ending (completed, failed, cancelled, timed out) says nothing about
        teleop: the robot may still be paused by a pause_order or startTeleop, and only
        stopTeleop releases it. Dropping TELEOP here would make the dispatcher believe
        the robot is free, so it would never send that stopTeleop."""
        self._set_robot_state_unless_teleop(robot_object.RobotStateV1.IDLE)

    def _set_failure_category(self, category: mission_object.MissionFailureCategoryV1):
        """Name the cause of the current mission's failure/cancel; the first one set wins."""
        status = self._current_mission.status if self._current_mission is not None else None
        if status is not None and status.failure_category is None:
            status.failure_category = category

    def _set_mission_state(self, state: mission_object.MissionStateV1):
        if self._current_mission is None or state == self._current_mission.status.state:
            return False
        if state == mission_object.MissionStateV1.CANCELED:
            self._set_failure_category(mission_object.MissionFailureCategoryV1.CANCELED)
        self.mission_info(
            f"Mission state: {self._current_mission.status.state} -> {state}")
        self._current_mission.status.state = state
        self._current_mission.status.node_status["root"].state = state
        if state.done:
            # A reroute's held cancel is about this mission's order only.
            self._deferred_replace_cancel = False

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
                self._set_robot_state_unless_teleop(robot_object.RobotStateV1.ON_TASK)
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
        self._queue_status_write(api_objects.MissionObjectV1, self._current_mission.name, self._current_mission.status, self._mission_writer_id())
        return True

    def set_mission_node_state(self, node_name: str, state: mission_object.MissionStateV1):
        if self._current_mission is None:
            return
        previous_state = self._current_mission.status.node_status[node_name].state
        if previous_state == state:
            return
        self.mission_info(f"Node {node_name}: {previous_state} -> {state}")
        self._current_mission.status.node_status[node_name].state = state
        if state == mission_object.MissionStateV1.FAILED:
            self._record("node_failed", self._name, self._current_mission, node_name,
                         self._event_ts)

    def _post_notify(self, notify) -> int:
        """One notify request (blocking; runs in a worker thread). The HTTP status."""
        timeout = NOTIFY_MAX_TIMEOUT_S if not notify.timeout else \
            min(float(notify.timeout), NOTIFY_MAX_TIMEOUT_S)
        return requests.post(url=notify.url, json=notify.json_data, timeout=timeout).status_code

    async def _process_notify_node(self, mission_node):
        """Run a notify node as a task (_notify_task): POST its webhook in a worker thread,
        retrying transient failures with back-off. Its writes are synchronous and happen
        only after an await returns, so they cannot interleave with the message loop
        mid-update. Cancelled with the mission (_cancel_wait); if the mission is cancelled,
        finished or replaced meanwhile, nothing is written afterwards."""
        if self._notify_in_flight:
            return
        mission = self._current_mission
        tree = self._current_behavior_tree
        name = f"{mission_node.name}"

        def stale() -> bool:
            return self._current_mission is not mission or \
                self._current_behavior_tree is not tree or \
                mission.status.state.done or mission.needs_canceled

        self._notify_in_flight = True
        finished = False
        key = self._notify_key
        try:
            self.set_mission_node_state(name, mission_object.MissionStateV1.RUNNING)
            final = mission_object.MissionStateV1.FAILED
            reason = None
            for attempt in range(len(NOTIFY_RETRY_BACKOFF_S) + 1):
                if attempt:
                    await asyncio.sleep(NOTIFY_RETRY_BACKOFF_S[attempt - 1])
                    if stale():
                        return
                try:
                    status = await asyncio.get_event_loop().run_in_executor(
                        None, self._post_notify, mission_node.notify)
                except Exception as err:  # pylint: disable=broad-except
                    reason = f"Notify request failed: {type(err).__name__}: {err}"
                    status = None
                else:
                    reason = f"Notify: HTTP {status}"
                if stale():
                    return
                if status == 200:
                    final = mission_object.MissionStateV1.COMPLETED
                    reason = None
                    break
                if status is not None and status not in NOTIFY_RETRY_STATUSES:
                    break
                self.mission_info(f"{reason}, "
                                  f"{'retrying' if attempt < len(NOTIFY_RETRY_BACKOFF_S) else 'giving up'}")
            if final == mission_object.MissionStateV1.FAILED:
                mission.status.node_status[name].error_msg = reason
            self.set_mission_node_state(name, final)
            finished = True
        finally:
            self._notify_in_flight = False
            if not finished and self._notify_key == key:
                self._notify_key = None   # dropped as stale: nothing more is coming

        # Since Notify does not send an order, there is no feedback from robot: the message
        # loop goes on from here (NotifyDone), as after a wait.
        await self.send_message(NotifyDone(key=key))

    def _charging_hook_request(self) -> bool:
        """Ask mission control for a charging mission (blocking; runs in a worker thread).
        Whether it was accepted."""
        base = self._robot_server.mission_ctrl_url
        if requests.get(base + "/api/v1/health", timeout=CHARGING_HOOK_TIMEOUT_S).status_code != 200:
            return False
        response = requests.post(base + "/api/v1/mission/charging",
                                 params={"robot_name": self._name},
                                 timeout=CHARGING_HOOK_TIMEOUT_S)
        if response.status_code != 200:
            self.warning("Failed to post charging mission")
            return False
        return True

    async def _post_charging_mission(self):
        try:
            if await asyncio.get_event_loop().run_in_executor(None, self._charging_hook_request):
                self.debug("Charging mission posted successfully")
                self._charging_mission_received = True
        except requests.exceptions.RequestException as err:
            self.warning(f"Charging mission request failed: {err}")
        except Exception as err:  # pylint: disable=broad-except
            self.warning(f"Charging mission request error: {err}")
        finally:
            self._charging_hook_busy = False

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
                 telemetry_env: str = "DEV", disable_request_factsheet: bool = False,
                 disable_fleet_recording: bool = False,
                 fleet_spill_path: str = fleet_recorder.DEFAULT_SPILL_PATH,
                 mission_planner_url: Optional[str] = None):
        """Initializes a RobotServer object by starting threads for mqtt and for the robot/mission
        database watchers
        Args:
            mqtt_host: The hostname for the mqtt client to connect to
            mqtt_port: The port for the mqtt client to connect to
            mqtt_prefix: The prefix to add to all VDA5050 mqtt topics
            databae_url: The url where the database REST API is hosted
        """
        self._logger = logging.getLogger("Isaac Mission Dispatch")
        self._logger.info("Order policy: %s", order_policy.current().json())

        # Save parameters to use later

        self._mqtt_prefix = mqtt_prefix

        # Connect to the db
        from packages.database.postgres import PostgresDatabase
        self._database = PostgresDatabase(
            dbname=postgres_db,
            user=postgres_user,
            password=postgres_password,
            host=postgres_host,
            port=postgres_port,
            required_tables=DISPATCH_REQUIRED_TABLES,
        )

        # Phase 0 recording (mission_runs, fleet_events, robot_state_ts, robot_latest) on
        # its own small pool; see fleet_recorder for why it can never hold missions up.
        # Set before MQTT connects: the message callback looks at it.
        self.fleet_recorder: Optional[fleet_recorder.FleetRecorder] = None
        if not disable_fleet_recording:
            try:
                self.fleet_recorder = fleet_recorder.FleetRecorder(
                    conninfo=f"dbname={postgres_db} user={postgres_user} host={postgres_host} "
                             f"password={postgres_password} port={postgres_port}",
                    spill_path=fleet_spill_path)
            except Exception as err:  # pylint: disable=broad-except
                self.warning(f"Fleet recording disabled: {err}")

        # Create queues to propogate changes to the main thread
        self._event_loop = asyncio.get_event_loop()
        self._mission_changes: asyncio.Queue[api_objects.MissionObjectV1] = asyncio.Queue()
        self._robot_changes: asyncio.Queue[api_objects.RobotObjectV1] = asyncio.Queue()
        # Publisher id of the robot controllers' own robot-object writes (Robot._writer_id);
        # the robot watcher skips their notifications.
        self.robot_writer_id = uuid.uuid4()
        # The same for the controllers' mission writes (Robot._mission_writer_id).
        self.mission_writer_id = uuid.uuid4()
        self._mqtt_messages: asyncio.Queue = asyncio.Queue()
        # Per robot, the state message still waiting in _mqtt_messages (newest wins, see
        # _enqueue_now); dropped when another message of the robot is queued after it.
        self._state_slots: Dict[str, _StateSlot] = {}

        # Maps §14 U3: bumped on every (re)connect to the broker; a robot's first datum in an
        # epoch may be a retained re-delivery (Robot._process_datum_message).
        self.mqtt_epoch = 0

        # Connect to MQTT in _run(), after the leader lock: a second instance shares the fixed
        # client id and would fight over the session and double-publish orders.
        self._mqtt_client: Optional[MQTTClient] = None
        self._mqtt_args = (mqtt_host, mqtt_port, mqtt_transport, mqtt_ws_path,
                           mqtt_username, mqtt_password)
        self._leader = lifecycle.LeaderLock(self._database.dedicated_connection)
        self._heartbeat = lifecycle.Heartbeat(
            self._database.ping,
            lambda: self._mqtt_client is not None
            and getattr(self._mqtt_client, "connected", False))
        self._main_task: Optional[asyncio.Task] = None
        self._shutdown_done = False
        self.exit_code = 0

        # The robot objects
        self._robots: Dict[str, Robot] = {}

        # Plan-only access to the mission planner (Robot._replan_goto)
        self.mission_planner = planner_client.PlannerClient(mission_planner_url)

        # Mission control
        self.mission_ctrl_url = mission_ctrl_url
        self.push_telemetry = push_telemetry
        self.disable_request_factsheet = disable_request_factsheet
        self.telemetry_env = telemetry_env

    def _enqueue(self, queue, obj):
        """From the paho thread: hand over to the event loop, never block."""
        self._event_loop.call_soon_threadsafe(self._enqueue_now, queue, obj)

    def _enqueue_now(self, queue, obj) -> None:
        """On the event loop. A robot's state message replaces its own still-queued one
        (state is cumulative; the queue cannot grow while the consumer is behind). Anything
        else is queued as is, and ends the robot's open state slot so order is kept."""
        if queue is not self._mqtt_messages or not hasattr(obj, "name"):
            queue.put_nowait(obj)
            return
        if isinstance(obj, (ClientStatusMessage, RawStateMessage)):
            slot = self._state_slots.get(obj.name)
            if slot is not None:
                slot.msg = obj
                return
            slot = self._state_slots[obj.name] = _StateSlot(obj)
            queue.put_nowait(slot)
            return
        self._state_slots.pop(obj.name, None)
        queue.put_nowait(obj)

    def _mqtt_on_message(self, client, userdata, msg):
        state_match = re.match(f"{self._mqtt_prefix}/(.*)/state", msg.topic)
        factsheet_match = re.match(
            f"{self._mqtt_prefix}/(.*)/factsheet", msg.topic)
        datum_match = re.match(f"{self._mqtt_prefix}/(.*)/datum", msg.topic)
        approx_match = re.match(f"{self._mqtt_prefix}/(.*)/approx_position", msg.topic)
        connection_match = re.match(f"{self._mqtt_prefix}/(.*)/connection", msg.topic)
        try:
            if state_match:
                robot = state_match.groups()[0]
                pl = msg.payload
                self._enqueue(self._mqtt_messages, RawStateMessage(robot, pl))
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
            elif approx_match:
                robot = approx_match.groups()[0]
                self._enqueue(self._mqtt_messages, ClientApproxPositionMessage(
                    name=robot, payload=json.loads(msg.payload)))
            elif connection_match:
                robot = connection_match.groups()[0]
                self._enqueue(self._mqtt_messages, ClientConnectionMessage(
                    name=robot, payload=json.loads(msg.payload),
                    retained=bool(getattr(msg, "retain", False))))
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
        client.register_callback(f"{self._mqtt_prefix}/+/approx_position", self._mqtt_on_message)
        client.register_callback(f"{self._mqtt_prefix}/+/connection", self._mqtt_on_message)
        if hasattr(client, "add_connect_listener"):
            client.add_connect_listener(self._mqtt_connected)

        client.connect()
        return client

    def _mqtt_connected(self) -> None:
        """paho thread, on every (re)connect: a new epoch for the datum trust rule."""
        self.mqtt_epoch += 1

    async def stop(self):
        loop = asyncio.get_event_loop()
        loop.stop()

    async def _watch_changes(self, object_class: Any, queue: asyncio.Queue,
                             publisher_id: Optional[uuid.UUID] = None):
        """`publisher_id`: notifications of writes with this id are skipped (our own).
        A failed watch is restarted with a capped exponential backoff, like the recording
        watchers: the process must not exit over a watcher error (nothing here needs it)."""
        publisher_id = publisher_id or uuid.uuid4()
        delay = WATCH_CHANGES_RETRY_MIN_S
        while True:
            try:
                watcher_instance = await self._database.get_watcher(object_class, publisher_id)
                with watcher_instance:
                    async for update in watcher_instance.watch():
                        delay = WATCH_CHANGES_RETRY_MIN_S
                        self.debug(f"Watch object update: {object_class.get_alias()}")
                        await queue.put(update)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # pylint: disable=broad-except
                self.error(f"Watcher for {object_class.get_alias()} failed, restarting in "
                           f"{delay}s: {err}", exc_info=True)
            await asyncio.sleep(delay)
            delay = min(delay * 2, WATCH_CHANGES_RETRY_MAX_S)

    async def _handle_robot_changes(self):
        while True:
            robot = await self._robot_changes.get()
            try:
                await self._process_robot_change(robot)
            except Exception:  # pylint: disable=broad-except
                self.error(f"Robot change {getattr(robot, 'name', None)!r} failed",
                           exc_info=True)

    async def _process_robot_change(self, robot):
        self._unknown_robots_cache().pop(getattr(robot, "name", None), None)
        # Ignore deleted robot object
        if robot.lifecycle == \
                api_objects.object.ObjectLifecycleV1.DELETED:
            # The row is hard-deleted (DELETE /api/v1/robots/{name} writes DELETED
            # directly): drop the live controller too, or it would keep its stale mission
            # queue, run epoch and timers and take over a robot registered again under
            # this name.
            self.remove_robot(getattr(robot, "name", None))
            if self.fleet_recorder is not None:
                self.fleet_recorder.on_robot_deleted(robot)
            return
        # Robots being deleted may not have a name
        if hasattr(robot, "name"):
            controller = self._get_or_create_robot(robot.name)
            if self.fleet_recorder is not None:
                self.fleet_recorder.on_robot_object(robot)
            await controller.send_message(robot)

    async def _watch_settings(self):
        """Recording only (WP8): feed settings NOTIFYs (the global recording level) to the
        fleet recorder. Unlike _watch_changes, a failure here never stops the dispatcher:
        it is logged and retried, and meanwhile the periodic policy reload still applies."""
        while True:
            try:
                watcher = await self._database.get_watcher(api_objects.SettingsObjectV1,
                                                            uuid.uuid4())
                with watcher:
                    async for settings in watcher.watch():
                        self.fleet_recorder.on_settings_object(settings)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # pylint: disable=broad-except
                self.warning(f"Settings watcher failed, retrying in "
                             f"{SETTINGS_WATCH_RETRY_S}s: {err}")
            await asyncio.sleep(SETTINGS_WATCH_RETRY_S)

    async def _watch_sites(self):
        """Recording only (WP9): site NOTIFYs (site recording levels) to the fleet recorder.
        Never stops the dispatcher (see _watch_settings)."""
        while True:
            try:
                watcher = await self._database.get_watcher(api_objects.SiteObjectV1,
                                                            uuid.uuid4())
                with watcher:
                    async for site in watcher.watch():
                        self.fleet_recorder.on_site_object(site)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # pylint: disable=broad-except
                self.warning(f"Site watcher failed, retrying in "
                             f"{SETTINGS_WATCH_RETRY_S}s: {err}")
            await asyncio.sleep(SETTINGS_WATCH_RETRY_S)

    async def _watch_site_assignments(self):
        """Recording only (WP9): robot site assignment changes (NOTIFY channel written by the
        API's PUT /api/v1/robots/{name}/site) to the fleet recorder. A None from the watcher
        means it (re)subscribed: reload. Never stops the dispatcher."""
        while True:
            try:
                watcher = self._database.get_channel_watcher(fleet_recorder.ASSIGNMENTS_CHANNEL)
                async for payload in watcher.watch():
                    if payload is None:
                        self.fleet_recorder.on_site_assignments_resync()
                    else:
                        self.fleet_recorder.on_site_assignment(payload)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # pylint: disable=broad-except
                self.warning(f"Site assignment watcher failed, retrying in "
                             f"{SETTINGS_WATCH_RETRY_S}s: {err}")
            await asyncio.sleep(SETTINGS_WATCH_RETRY_S)

    async def _handle_mission_changes(self):
        while True:
            mission = await self._mission_changes.get()
            try:
                await self._process_mission_change(mission)
            except Exception:  # pylint: disable=broad-except
                self.error(f"Mission change {getattr(mission, 'name', None)!r} failed",
                           exc_info=True)

    async def _process_mission_change(self, mission):
        # Ignore deleted mission object
        if mission.lifecycle == \
                api_objects.object.ObjectLifecycleV1.DELETED:
            return

        # Ignore missions that are already done
        if mission.status.state.done:
            # Delete completed mission
            await self.delete_pending_mission(mission)
            return

        # Put the mission into the queue for the correct robot object
        await self._get_or_create_robot(mission.robot).send_message(mission)

    def _get_or_create_robot(self, name: str) -> "Robot":
        """The controller of robot `name`, created if there is none. The one place that
        creates controllers: call it *after* any await, never create one from a check made
        before it -- a concurrent handler (resync overlapping the first MQTT states) may have
        created it meanwhile, and a second Robot would orphan the first with its run task,
        timers and mission queue."""
        controller = self._robots.get(name)
        if controller is None:
            self.debug(f"Creating controller for robot {name}")
            controller = self._robots[name] = Robot(
                name, self._database, self._mqtt_client, self._mqtt_prefix, self)
        return controller

    def _unknown_robots_cache(self) -> Dict[str, float]:
        """name -> monotonic time until which an MQTT message from that unknown robot is
        dropped without a database lookup (UNKNOWN_ROBOT_TTL_S). Created on first use."""
        cache = getattr(self, "_unknown_robots", None)
        if cache is None:
            cache = self._unknown_robots = {}
        return cache

    async def _handle_mqtt_messages(self):
        while True:
            message = await self._mqtt_messages.get()
            if isinstance(message, _StateSlot):
                if self._state_slots.get(message.msg.name) is message:
                    del self._state_slots[message.msg.name]
                message = message.msg
            if isinstance(message, RawStateMessage):
                try:
                    message = message.parse()
                except pydantic.ValidationError as e:
                    self.warning(f"Validation error from client message:\n{e.errors()}")
                    continue
                except Exception as e:  # pylint: disable=broad-except
                    self.warning(f"Error processing MQTT message: {e}")
                    continue
            try:
                await self._process_mqtt_message(message)
            except Exception:  # pylint: disable=broad-except
                self.error(f"MQTT message from {getattr(message, 'name', None)!r} failed",
                           exc_info=True)

    async def _process_mqtt_message(self, message):
        if isinstance(message, ClientConnectionMessage):
            # Recording (ROBOT.ONLINE/OFFLINE), and maps §14 U3: a new robot run unplaces
            # its map session (Robot._on_connection_message). Unknown robots are ignored.
            if self.fleet_recorder is not None and (
                    message.name in self._robots or self.fleet_recorder.knows(message.name)):
                self.fleet_recorder.on_connection(message.name, message.payload)
            if message.name in self._robots:
                await self._robots[message.name].send_message(
                    ConnectionDelivery(connection=message.payload, retained=message.retained))
            return
        if message.name not in self._robots:
            unknown = self._unknown_robots_cache()
            now = time.monotonic()
            if unknown.get(message.name, 0.0) > now:
                return      # looked up (and warned about) within the last TTL
            # Try to get the robot from the database
            try:
                robot = await self._database.get_object(api_objects.RobotObjectV1, message.name)
                self.debug(f"Got robot from database on MQTT message: {message.name}")
                # Re-checked after the await: another handler may have created it meanwhile.
                controller = self._get_or_create_robot(message.name)
                unknown.pop(message.name, None)
                # Send the robot object to the robot handler
                await controller.send_message(robot)
            except Exception as e:  # pylint: disable=broad-except
                unknown[message.name] = now + UNKNOWN_ROBOT_TTL_S
                self.warning(
                    f"Ignoring MQTT message from unknown robot \"{message.name}\" "
                    f"(next lookup in {UNKNOWN_ROBOT_TTL_S:g}s): {e}")
                return
        await self._robots[message.name].send_message(message.payload)

    async def _unverify_run_epochs(self) -> None:
        """Maps §14.13: at start, before any robot message is handled, no robot's run is known
        to continue (it may have restarted while dispatch was down): continuity_known = false
        until each robot's first state message decides. Never raises."""
        try:
            async with self._database.connection() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(map_sessions.RUN_EPOCH_UNVERIFY_ALL_SQL)
                    self.info(f"Run epochs of {cursor.rowcount} robots to re-check")
        except Exception as err:  # pylint: disable=broad-except
            self.warning(f"Run epochs not reset ({err}); placements may not be reused")

    async def _run(self):
        await self._leader.acquire()
        self._mqtt_client = self._connect_to_mqtt(*self._mqtt_args)
        await self._database.async_init()
        await self._unverify_run_epochs()
        if self.fleet_recorder is not None:
            # Before the watchers start, so detectors are rehydrated and the orphan
            # reconciliation is queued ahead of any run this process starts. Bounded and
            # never raises: missions dispatch whether or not recording works.
            try:
                await self.fleet_recorder.start()
            except Exception as err:  # pylint: disable=broad-except
                self.warning(f"Fleet recording failed to start: {err}")
        tasks = [
            self._watch_changes(api_objects.MissionObjectV1, self._mission_changes,
                                self.mission_writer_id),
            self._watch_changes(api_objects.RobotObjectV1, self._robot_changes,
                                self.robot_writer_id),
            self._handle_robot_changes(),
            self._handle_mission_changes(),
            self._handle_mqtt_messages(),
            self._heartbeat.run(),
            self._leader.watch(),
        ]
        if self.fleet_recorder is not None:
            tasks.append(self._watch_settings())
            tasks.append(self._watch_sites())
            tasks.append(self._watch_site_assignments())
        await asyncio.gather(*tasks)

    def remove_robot(self, robot_name: Optional[str]) -> None:
        """Forget the in-memory controller of `robot_name` (no database access). A no-op for
        an unknown name."""
        robot = self._robots.pop(robot_name, None) if robot_name else None
        if robot is not None:
            robot.shutdown()

    async def delete_robot(self, robot_name: str):
        robot = self._robots.get(robot_name)
        if robot is not None:
            properties = robot.robot_object
            if properties is not None:
                try:
                    await self._database.set_lifecycle(
                        api_objects.RobotObjectV1, properties.name,
                        api_objects.object.ObjectLifecycleV1.DELETED, uuid.uuid4())
                except Exception as err:  # pylint: disable=broad-except
                    # The row stays PENDING_DELETE. The controller is dropped all the same
                    # (the caller already stopped it): keeping it would leave a zombie with
                    # a dead run loop. The next resync / robot message creates a fresh
                    # controller from the row, and that one deletes again.
                    self.warning(f"Could not delete robot {properties.name} ({err}); "
                                 "it is retried when the row is next delivered")
                finally:
                    if self._robots.get(properties.name) is robot:
                        del self._robots[properties.name]
                    robot.shutdown()

    async def delete_pending_mission(self, mission: api_objects.MissionObjectV1) -> bool:
        if mission.lifecycle == \
                api_objects.object.ObjectLifecycleV1.PENDING_DELETE:
            await self._database.set_lifecycle(api_objects.MissionObjectV1, mission.name, api_objects.object.ObjectLifecycleV1.DELETED, uuid.uuid4())
            self.info(f"Deleted mission {mission.name}")
            return True
        return False

    def run(self):
        """Runs until SIGTERM/SIGINT (clean exit, code 0), a failure, or loss of the leader
        lock (exit_code 1: docker restarts us). Always shuts down cleanly first."""
        loop = self._event_loop
        self._main_task = loop.create_task(self._run())
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._request_shutdown)
            except (NotImplementedError, RuntimeError):  # pragma: no cover
                pass
        try:
            loop.run_until_complete(self._main_task)
        except asyncio.CancelledError:
            self.info("Shutdown requested")
        except lifecycle.LeaderLockLost as err:
            self.warning(f"{err}; exiting so that docker restarts the dispatcher")
            self.exit_code = 1
        except BaseException:
            self.exit_code = 1
            raise
        finally:
            loop.run_until_complete(self.graceful_shutdown())

    def _request_shutdown(self):
        self.info("Signal received, shutting down")
        if self._main_task is not None:
            self._main_task.cancel()

    async def graceful_shutdown(self, timeout_s: float = lifecycle.SHUTDOWN_TIMEOUT_S):
        """In order, each step bounded on its own (a slow one cannot starve the rest): stop
        accepting work (MQTT disconnect), shut the robots down and let their final status
        writes land, drain the recorder's queued run writes, stop the recorder and close its
        pool, close the database pool, release the leader lock; `timeout_s` overall.
        Idempotent; never raises."""
        if self._shutdown_done:
            return
        self._shutdown_done = True
        step = self._shutdown_step

        async def steps():
            if self._mqtt_client is not None:
                await step("MQTT disconnect", self._mqtt_client.disconnect,
                           SHUTDOWN_MQTT_S)
            await step("robots shutdown", self._shutdown_robots, SHUTDOWN_ROBOTS_S)
            if self.fleet_recorder is not None:
                await step("recorder drain", lambda: self.fleet_recorder.drain(
                    SHUTDOWN_RECORDER_DRAIN_S), SHUTDOWN_RECORDER_DRAIN_S + 0.5)
                await step("fleet recorder stop", self.fleet_recorder.stop,
                           SHUTDOWN_RECORDER_STOP_S)
                await step("recorder pool close", self.fleet_recorder.close,
                           SHUTDOWN_POOL_CLOSE_S)
            await step("database close", self._database.close_pool, SHUTDOWN_POOL_CLOSE_S)
            await step("leader lock release", self._leader.release, SHUTDOWN_LOCK_S)

        try:
            await asyncio.wait_for(steps(), timeout_s)
        except Exception as err:  # pylint: disable=broad-except
            self.warning(f"Shutdown incomplete after {timeout_s}s: {err!r}")

    async def _shutdown_robots(self):
        """Shut every controller down (timers, notify/charging tasks, message loop) and wait
        for the bounded flush of their queued status writes (a mission's final state above
        all) that Robot.shutdown() starts: it must land before the pools close."""
        robots = list(self._robots.values())
        for robot in robots:
            try:
                robot.shutdown()
            except Exception as err:  # pylint: disable=broad-except
                self.warning(f"Shutdown: robot {getattr(robot, 'name', '?')} failed: {err}")
        await asyncio.gather(*(r.wait_shutdown_flush() for r in robots),
                             return_exceptions=True)

    async def _shutdown_step(self, what: str, fn, timeout_s: Optional[float] = None):
        try:
            result = fn()
            if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
                await asyncio.wait_for(result, timeout_s)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            self.warning(f"Shutdown: {what} did not finish within {timeout_s}s")
        except Exception as err:  # pylint: disable=broad-except
            self.warning(f"Shutdown: {what} failed: {err}")

    def info(self, message: str):
        self._logger.info("[Isaac Mission Dispatch] | INFO: %s", message)

    def debug(self, message: str):
        self._logger.debug("[Isaac Mission Dispatch] | DEBUG: %s", message)

    def warning(self, message: str):
        self._logger.warning("[Isaac Mission Dispatch] | WARNING: %s", message)

    def error(self, message: str, exc_info: bool = False):
        self._logger.error("[Isaac Mission Dispatch] | ERROR: %s", message, exc_info=exc_info)
