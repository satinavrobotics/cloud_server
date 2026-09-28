"""The robot mapping switch over MQTT (docs/satinav-maps-redesign.md §8, §13.3; maps M3).

The contract, in one place (the robot side is sati_topo_mapping/mapping_switch.py in
sati_ros_navstack). `{prefix}` is the VDA5050 prefix (config MQTT_VDA5050_PREFIX,
`uagv/v2/RobotCompany`), `{robot}` the robot name (= its VDA5050 serial number):

  {prefix}/{robot}/mapping/set      API -> robot, RETAINED, QoS 1
      {"enabled": bool, "session_id": str|null, "map": str|null, "issued_at": iso8601}
      Derived from the robot's open session after every session change commits: an open,
      unpaused session -> enabled with its id and map; paused -> disabled, same id and map;
      no open session -> disabled, nulls. Retained, so a topomap that (re)starts picks up the
      current state. Also re-published for every robot whenever the API (re)connects to the
      broker (the broker keeps no retained messages across its own restart).
      Optional `"force": true` (only POST /robots/{r}/mapping/off sets it): the robot applies
      the message even if unchanged, so it also ends a local `~/set_enabled` override.

  {prefix}/{robot}/mapping/state    robot -> API, RETAINED, QoS 1
      {"online": true, "enabled": bool, "session_id": str|null, "map": str|null,
       "nodes_sent": int, "since": iso8601|null, "stamp": iso8601,
       "source": "mqtt"|"local"|"startup"}
      On every change and every (re)connect. Last will (and clean shutdown):
      {"online": false, "enabled": false, "session_id": null, "map": null, ...}.

What the API exposes (`mapping_state`, see state_view): the last state received for the
robot, with `received_at` (when the API got it; a retained message is received again when the
API reconnects) and `status`: "on" (online, enabled), "off" (online, disabled) or
"unreachable" (the last will / clean-shutdown state: the topomap service is not running). null
when nothing was received since the API started: the topomap has never connected (or runs a
build without the switch). `mapping_service` is "running" when the last state is online, else
"not_running" (doc Q3: the session still starts; the client tells the user to start the
service; nothing is started automatically).
"""

import asyncio
import datetime
import json
import logging
import re
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional

logger = logging.getLogger("ApiDelegationService.mapping_control")

SET_SUFFIX = "mapping/set"
STATE_SUFFIX = "mapping/state"
PUBLISH_TIMEOUT_S = 2.0

RUNNING, NOT_RUNNING = "running", "not_running"


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def set_topic(prefix: str, robot: str) -> str:
    return f"{prefix.rstrip('/')}/{robot}/{SET_SUFFIX}"


def state_subscription(prefix: str) -> str:
    return f"{prefix.rstrip('/')}/+/{STATE_SUFFIX}"


def set_payload(open_session: Optional[Mapping[str, Any]],
                now: Optional[datetime.datetime] = None) -> Dict[str, Any]:
    """What the robot should do, from its open session (a map_sessions row, or None)."""
    issued_at = (now or _utcnow()).isoformat()
    if open_session is None:
        return {"enabled": False, "session_id": None, "map": None, "issued_at": issued_at}
    return {"enabled": open_session.get("paused_at") is None,
            "session_id": str(open_session["session_id"]),
            "map": open_session["map_name"], "issued_at": issued_at}


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


class MappingControl:
    """Publishes set messages and caches state messages. The MQTT client is the API's
    existing diagnostics connection (packages/api/diagnostics.py), attached with attach()."""

    def __init__(self, prefix: str, publish_timeout: float = PUBLISH_TIMEOUT_S):
        self.prefix = prefix.rstrip("/")
        self.publish_timeout = publish_timeout
        self.client: Optional[Any] = None
        self._state_re = re.compile(rf"^{re.escape(self.prefix)}/([^/]+)/{STATE_SUFFIX}$")
        self._states: Dict[str, Dict[str, Any]] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # Set by the service: async fn(robot, view) broadcasting a state change, and async fn()
        # re-publishing every robot's set message (on every broker (re)connect).
        self.on_state: Optional[Callable[[str, Optional[Dict[str, Any]]], Awaitable[None]]] = None
        self.on_connect: Optional[Callable[[], Awaitable[None]]] = None

    # --- wiring --------------------------------------------------------------------------------

    def attach(self, client: Any, loop: Optional[asyncio.AbstractEventLoop]) -> None:
        """Register on `client` (a packages.utils.mqtt_client.MQTTClient) BEFORE it connects."""
        self.client = client
        self._loop = loop
        client.register_callback(state_subscription(self.prefix), self.on_state_message, qos=1)
        client.add_connect_listener(self._connected)

    def _connected(self) -> None:
        """paho thread: re-publish the set messages (on the event loop)."""
        if self.on_connect is not None and self._loop is not None:
            asyncio.run_coroutine_threadsafe(self.on_connect(), self._loop)

    # --- state (robot -> API) ------------------------------------------------------------------

    def on_state_message(self, client: Any, userdata: Any, msg: Any) -> None:
        """paho thread: cache a state message (an empty payload clears the retained topic)."""
        match = self._state_re.match(msg.topic)
        if not match:
            return
        robot = match.group(1)
        if not msg.payload:
            self._states.pop(robot, None)
            self._broadcast(robot, None)
            return
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            logger.warning("Bad mapping state from %s: %s", robot, e)
            return
        if not isinstance(payload, dict):
            return
        payload["received_at"] = _utcnow().isoformat()
        self._states[robot] = payload
        self._broadcast(robot, state_view(payload))

    def _broadcast(self, robot: str, view: Optional[Dict[str, Any]]) -> None:
        if self.on_state is not None and self._loop is not None:
            asyncio.run_coroutine_threadsafe(self.on_state(robot, view), self._loop)

    def state(self, robot: str) -> Optional[Dict[str, Any]]:
        """The robot's `mapping_state` (state_view of the last message), or None."""
        return state_view(self._states.get(robot))

    def mapping_service(self, robot: str) -> str:
        return service_of(self._states.get(robot))

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
