"""The robot mapping switch over MQTT (docs/satinav-maps-redesign.md §8, §13.3; maps M3).

The contract, in one place (the robot side is sati_topo_mapping/mapping_switch.py in
sati_ros_navstack). `{prefix}` is the VDA5050 prefix (config MQTT_VDA5050_PREFIX,
`uagv/v2/RobotCompany`), `{robot}` the robot name (= its VDA5050 serial number):

  {prefix}/{robot}/mapping/set      API -> robot, RETAINED, QoS 1
      {"enabled": bool, "session_id": str|null, "map": str|null, "services": [str],
       "issued_at": iso8601}
      Derived from the robot's open session after every session change commits
      (packages/utils/map_sessions.py::set_payload): an open, unpaused, placed MAPPING
      session -> enabled with its id, map and services; paused or not placed -> disabled,
      same id and map; no open session, or an `operate` session (maps §14) -> disabled,
      nulls, no services. `services` (§14.5): each mapping service runs iff `enabled` and
      its name is listed; a robot that ignores the field runs topo (the M3 topomap). Since
      U3 mission-dispatch publishes it too, after it unplaces a robot's session on a run
      change or re-places a geo session from a new datum. Retained, so a topomap that (re)starts picks up the
      current state. Also re-published for every robot whenever the API (re)connects to the
      broker (the broker keeps no retained messages across its own restart).
      Optional `"force": true` (only POST /robots/{r}/mapping/off sets it): the robot applies
      the message even if unchanged, so it also ends a local `~/set_enabled` override.

  {prefix}/{robot}/mapping/{service}/state    robot -> API, RETAINED, QoS 1 (maps U5, §14.5)
      {"online": true, "service": str, "enabled": bool, "session_id": str|null,
       "map": str|null, "nodes_sent": int, "since": iso8601|null, "stamp": iso8601,
       "source": "mqtt"|"local"|"startup"}
      One topic per mapping service process (the topomap publishes `mapping/topo/state`), on
      every change and every (re)connect, with its own last will (and clean shutdown):
      {"online": false, "service": str, "enabled": false, "session_id": null, "map": null, ...}.

  {prefix}/{robot}/mapping/state    robot -> API, RETAINED, QoS 1  (M3; alias, Q-U6)
      The M3 topomap's state topic (same payload without `service`). A U5 topomap still
      publishes it for one release, so an API without U5 keeps working; this API reads it as
      the `topo` state of a robot that has sent no `mapping/topo/state` (an M3 topomap).
      Once a robot's `mapping/topo/state` has been received, its alias is ignored (clearing
      the retained `mapping/topo/state` with an empty message falls back to the alias).

What the API exposes. `mapping_state` (see state_view) is the TOPO state: the last topo state
received for the robot, with `received_at` (when the API got it; a retained message is received
again when the API reconnects) and `status`: "on" (online, enabled), "off" (online, disabled) or
"unreachable" (the last will / clean-shutdown state: the topomap service is not running). null
when nothing was received since the API started: the topomap has never connected (or runs a
build without the switch). `mapping_service` is the topo service: "running" when the last state
is online, else "not_running" (doc Q3: the session still starts; the client tells the user to
start the service; nothing is started automatically). `mapping_services` (§14.3/§14.5):
{service: "running" | "not_running" | "not_available"} for every known service
(map_sessions.KNOWN_SERVICES) and every service the robot reported; "not_available" = the robot
has never reported that service since the API started ("not available on this robot").
"""

import asyncio
import datetime
import json
import logging
import re
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional

from packages.utils.map_sessions import KNOWN_SERVICES, TOPO
from packages.utils.map_sessions import set_payload  # noqa: F401 - the contract, re-exported

logger = logging.getLogger("ApiDelegationService.mapping_control")

SET_SUFFIX = "mapping/set"
STATE_SUFFIX = "mapping/state"          # M3 alias: the topomap's state (Q-U6)
SERVICE_STATE_SUFFIX = "mapping/+/state"  # U5: one state topic per mapping service
PUBLISH_TIMEOUT_S = 2.0

RUNNING, NOT_RUNNING, NOT_AVAILABLE = "running", "not_running", "not_available"
ALIAS = ""  # cache key of the M3 alias topic's state


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def set_topic(prefix: str, robot: str) -> str:
    return f"{prefix.rstrip('/')}/{robot}/{SET_SUFFIX}"


def state_subscription(prefix: str) -> str:
    """The M3 alias topic, for every robot."""
    return f"{prefix.rstrip('/')}/+/{STATE_SUFFIX}"


def service_state_subscription(prefix: str) -> str:
    """The per-service state topics (U5), for every robot and service."""
    return f"{prefix.rstrip('/')}/+/{SERVICE_STATE_SUFFIX}"


def service_state_topic(prefix: str, robot: str, service: str) -> str:
    return f"{prefix.rstrip('/')}/{robot}/mapping/{service}/state"


def force_off_payload(now: Optional[datetime.datetime] = None) -> Dict[str, Any]:
    """The operator's "force off" for a robot capturing without a session: the no-session
    payload plus `force: true`, which the robot applies even if unchanged (so it also ends a
    local `~/set_enabled true` override). Retained like every set message; the next ordinary
    set message (a session change or an API reconnect) replaces it."""
    return {**set_payload(None, now), "force": True}


def state_view(state: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    """A cached state message as the API returns it (`mapping_state`), with `status`."""
    if state is None:
        return None
    view = dict(state)
    online = view.get("online") is True
    view["online"] = online
    view["status"] = ("unreachable" if not online
                      else "on" if view.get("enabled") is True else "off")
    return view


def service_of(state: Optional[Mapping[str, Any]]) -> str:
    return RUNNING if state is not None and state.get("online") is True else NOT_RUNNING


def availability_of(state: Optional[Mapping[str, Any]]) -> str:
    """One entry of `mapping_services`: never reported -> not_available."""
    return NOT_AVAILABLE if state is None else service_of(state)


class MappingControl:
    """Publishes set messages and caches state messages. The MQTT client is the API's
    existing diagnostics connection (packages/api/diagnostics.py), attached with attach()."""

    def __init__(self, prefix: str, publish_timeout: float = PUBLISH_TIMEOUT_S):
        self.prefix = prefix.rstrip("/")
        self.publish_timeout = publish_timeout
        self.client: Optional[Any] = None
        # group 1: robot; group 2: service (None for the M3 alias topic)
        self._state_re = re.compile(
            rf"^{re.escape(self.prefix)}/([^/]+)/mapping/(?:([^/]+)/)?state$")
        # robot -> {service (ALIAS for the M3 topic): last state message}
        self._states: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # Set by the service: async fn(robot, service, view) broadcasting a state change of one
        # mapping service (view = its state_view, or None when cleared), and async fn()
        # re-publishing every robot's set message (on every broker (re)connect).
        self.on_state: Optional[
            Callable[[str, str, Optional[Dict[str, Any]]], Awaitable[None]]] = None
        self.on_connect: Optional[Callable[[], Awaitable[None]]] = None
        # Maps §14: async fn(robot, session view or None) pushing the robot's `session` after a
        # session change through the API (packages/api/maps.py::notify_robot).
        self.on_session: Optional[
            Callable[[str, Optional[Dict[str, Any]]], Awaitable[None]]] = None

    # --- wiring --------------------------------------------------------------------------------

    def attach(self, client: Any, loop: Optional[asyncio.AbstractEventLoop]) -> None:
        """Register on `client` (a packages.utils.mqtt_client.MQTTClient) BEFORE it connects."""
        self.client = client
        self._loop = loop
        client.register_callback(service_state_subscription(self.prefix),
                                 self.on_state_message, qos=1)
        client.register_callback(state_subscription(self.prefix), self.on_state_message, qos=1)
        client.add_connect_listener(self._connected)

    def _connected(self) -> None:
        """paho thread: re-publish the set messages (on the event loop)."""
        if self.on_connect is not None and self._loop is not None:
            asyncio.run_coroutine_threadsafe(self.on_connect(), self._loop)

    # --- state (robot -> API) ------------------------------------------------------------------

    def on_state_message(self, client: Any, userdata: Any, msg: Any) -> None:
        """paho thread: cache a state message, per service (an empty payload clears the
        retained topic). `mapping/{service}/state` is that service's; `mapping/state` (the M3
        alias) is the topomap's, used only while no `mapping/topo/state` was received."""
        match = self._state_re.match(msg.topic)
        if not match:
            return
        robot, service = match.group(1), match.group(2)
        key = ALIAS if service is None else service
        cache = self._states.setdefault(robot, {})
        if not msg.payload:
            cache.pop(key, None)
        else:
            try:
                payload = json.loads(msg.payload.decode("utf-8"))
            except Exception as e:  # noqa: BLE001
                logger.warning("Bad mapping state from %s (%s): %s", robot, msg.topic, e)
                return
            if not isinstance(payload, dict):
                return
            payload["received_at"] = _utcnow().isoformat()
            payload["service"] = TOPO if service is None else service
            cache[key] = payload
        if service is None and TOPO in cache:
            return  # the alias of a robot that reports mapping/topo/state: nothing changes
        name = TOPO if service is None else service
        self._broadcast(robot, name, state_view(self._raw(robot, name)))

    def _broadcast(self, robot: str, service: str, view: Optional[Dict[str, Any]]) -> None:
        if self.on_state is not None and self._loop is not None:
            asyncio.run_coroutine_threadsafe(self.on_state(robot, service, view), self._loop)

    def _raw(self, robot: str, service: str) -> Optional[Dict[str, Any]]:
        cache = self._states.get(robot) or {}
        found = cache.get(service)
        if found is None and service == TOPO:
            found = cache.get(ALIAS)
        return found

    def state(self, robot: str, service: str = TOPO) -> Optional[Dict[str, Any]]:
        """The robot's `mapping_state` (state_view of the last topo state message), or None;
        with `service`, that service's state."""
        return state_view(self._raw(robot, service))

    def mapping_service(self, robot: str) -> str:
        """The topomap: running | not_running (M3 `mapping_service`)."""
        return service_of(self._raw(robot, TOPO))

    def mapping_services(self, robot: str) -> Dict[str, str]:
        """Per mapping service (maps §14.3/§14.5, `mapping_services`): every known service
        and every service the robot reported -> running | not_running | not_available."""
        reported = [k for k in (self._states.get(robot) or {}) if k != ALIAS]
        names = list(KNOWN_SERVICES) + sorted(set(reported) - set(KNOWN_SERVICES))
        return {name: availability_of(self._raw(robot, name)) for name in names}

    # --- set (API -> robot) --------------------------------------------------------------------

    def lock(self, robot: str) -> asyncio.Lock:
        """Serialises read-then-publish per robot, so the last publish is the newest state."""
        lock = self._locks.get(robot)
        if lock is None:
            lock = self._locks[robot] = asyncio.Lock()
        return lock

    def _publish_blocking(self, topic: str, body: str) -> bool:
        client = self.client
        if client is None or not getattr(client, "connected", False):
            return False
        info = client.publish(topic, body, qos=1, retain=True)
        if getattr(info, "rc", 1) != 0:
            return False
        info.wait_for_publish(timeout=self.publish_timeout)
        return bool(info.is_published())

    async def publish_set(self, robot: str, payload: Mapping[str, Any]) -> bool:
        """Retained set message; True once the broker acknowledged it. Never raises."""
        topic = set_topic(self.prefix, robot)
        try:
            ok = await asyncio.to_thread(self._publish_blocking, topic, json.dumps(payload))
        except Exception as e:  # noqa: BLE001
            logger.error("Mapping set for %s not published: %s", robot, e)
            return False
        if ok:
            logger.info("Mapping set %s: %s", topic, payload)
        else:
            logger.error("Mapping set for %s not published (MQTT not connected or no "
                         "acknowledgement within %.1fs)", robot, self.publish_timeout)
        return ok
