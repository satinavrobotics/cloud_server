"""Maps U5 (docs/satinav-maps-redesign.md §14.5, §14.12): per-service mapping state topics.

- packages/api/mapping_control.py: `{prefix}/+/mapping/+/state` is cached per service; the M3
  topic `mapping/state` is read as `topo` (Q-U6 alias) until the robot's
  `mapping/topo/state` arrives; `mapping_state` / `mapping_service` stay the topo ones;
  `mapping_services` lists the known services and every reported one, and a service never
  reported is `not_available`; the WS broadcast names the service;
- `mapping/set` carries `services` (the payload rules themselves are in test_maps_use.py);
- the robot view and the map's session summary carry `mapping_services`.
"""
import asyncio
import os
from types import SimpleNamespace

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

from unittest.mock import patch  # noqa: E402

import pytest  # noqa: E402

import packages.api.main as main  # noqa: E402
from packages.api import mapping_control as mc  # noqa: E402
from packages.api import maps  # noqa: E402
from packages.utils import map_sessions as ms  # noqa: E402
from tests.unit import test_maps_m1 as m1  # noqa: E402
from tests.unit.test_maps_m3 import PREFIX, FakeMqtt, M3Db, control, msg  # noqa: E402

pytestmark = pytest.mark.unit

ALIAS = f"{PREFIX}/r1/mapping/state"
TOPO = f"{PREFIX}/r1/mapping/topo/state"
GRID = f"{PREFIX}/r1/mapping/grid/state"
NONE_REPORTED = {"topo": "not_available", "grid": "not_available"}


def _send(ctl, topic, payload):
    ctl.on_state_message(None, None, msg(topic, payload))


@pytest.fixture
def db():
    d = M3Db()
    with patch.object(maps, "open_store", d.store), patch.object(maps, "_utcnow", m1.Clock()):
        yield d


class TestTopics:
    def test_subscriptions(self):
        assert mc.service_state_subscription(PREFIX + "/") == (
            "uagv/v2/RobotCompany/+/mapping/+/state")
        assert mc.state_subscription(PREFIX) == "uagv/v2/RobotCompany/+/mapping/state"
        assert mc.service_state_topic(PREFIX, "r1", "topo") == TOPO

    def test_both_registered_on_one_callback(self):
        ctl = control()
        cbs = ctl.client.callbacks
        assert cbs[mc.service_state_subscription(PREFIX)] == (ctl.on_state_message, 1)
        assert cbs[mc.state_subscription(PREFIX)] == (ctl.on_state_message, 1)

    def test_wildcards_match_only_their_level(self):
        # the router of packages/utils/mqtt_client.py: `+` is one level, so the per-service
        # subscription does not also match the alias (and vice versa)
        from packages.utils.mqtt_client import MQTTClient
        match = MQTTClient._topic_matches
        per_service = mc.service_state_subscription(PREFIX)
        alias = mc.state_subscription(PREFIX)
        assert match(None, per_service, TOPO) and not match(None, per_service, ALIAS)
        assert match(None, alias, ALIAS) and not match(None, alias, TOPO)


class TestNeverReported:
    def test_unknown_robot(self):
        ctl = control()
        assert ctl.state("r1") is None
        assert ctl.mapping_service("r1") == "not_running"  # the M3 key is unchanged
        assert ctl.mapping_services("r1") == NONE_REPORTED

    def test_known_services_are_listed_in_order(self):
        assert list(control().mapping_services("x")) == list(ms.KNOWN_SERVICES)


class TestPerServiceState:
    def test_topo_topic(self):
        ctl = control()
        _send(ctl, TOPO, {"online": True, "enabled": True, "service": "topo", "nodes_sent": 3})
        st = ctl.state("r1")
        assert st["status"] == "on" and st["service"] == "topo" and st["nodes_sent"] == 3
        assert st["received_at"]
        assert ctl.mapping_service("r1") == "running"
        assert ctl.mapping_services("r1") == {"topo": "running", "grid": "not_available"}

    def test_service_is_taken_from_the_topic(self):
        ctl = control()
        _send(ctl, GRID, {"online": True, "enabled": False, "service": "something-else"})
        assert ctl.state("r1", "grid")["service"] == "grid"
        assert ctl.state("r1") is None  # topo is not the grid
        assert ctl.mapping_service("r1") == "not_running"
        assert ctl.mapping_services("r1") == {"topo": "not_available", "grid": "running"}

    def test_unknown_service_is_listed(self):
        ctl = control()
        _send(ctl, f"{PREFIX}/r1/mapping/slam/state", {"online": False})
        assert ctl.mapping_services("r1") == {**NONE_REPORTED, "slam": "not_running"}

    def test_last_will_is_not_running(self):
        ctl = control()
        _send(ctl, TOPO, {"online": True, "enabled": True})
        _send(ctl, TOPO, {"online": False, "enabled": False, "service": "topo"})
        assert ctl.state("r1")["status"] == "unreachable"
        assert ctl.mapping_services("r1")["topo"] == "not_running"

    def test_clear_makes_it_not_available(self):
        ctl = control()
        _send(ctl, GRID, {"online": True})
        _send(ctl, GRID, b"")
        assert ctl.mapping_services("r1")["grid"] == "not_available"

    @pytest.mark.parametrize("payload", [b"not json", b"[1, 2]"])
    def test_bad_payload_ignored(self, payload):
        ctl = control()
        _send(ctl, TOPO, payload)
        assert ctl.mapping_services("r1") == NONE_REPORTED

    def test_robots_are_separate(self):
        ctl = control()
        _send(ctl, TOPO, {"online": True, "enabled": True})
        assert ctl.state("r2") is None
        assert ctl.mapping_services("r2")["topo"] == "not_available"

    @pytest.mark.parametrize("topic", [
        f"{PREFIX}/r1/mapping/topo/extra/state",
        "other/r1/mapping/topo/state",
        f"{PREFIX}/r1/mapping/topo/set",
    ])
    def test_ignored_topics(self, topic):
        ctl = control()
        _send(ctl, topic, {"online": True})
        assert ctl.mapping_services("r1") == NONE_REPORTED


class TestAlias:
    """Q-U6: the M3 `mapping/state` is the topo state of a robot that sends no per-service
    topic; a U5 robot sends both (the same payload), and its per-service one wins."""

    def test_m3_robot(self):
        ctl = control()
        _send(ctl, ALIAS, {"online": True, "enabled": False})
        st = ctl.state("r1")
        assert st["status"] == "off" and st["service"] == "topo"
        assert ctl.mapping_service("r1") == "running"
        assert ctl.mapping_services("r1") == {"topo": "running", "grid": "not_available"}

    @pytest.mark.parametrize("order", [(ALIAS, TOPO), (TOPO, ALIAS)])
    def test_per_service_topic_wins_in_either_order(self, order):
        ctl = control()
        for topic in order:
            online = topic == TOPO
            _send(ctl, topic, {"online": online, "enabled": online})
        assert ctl.state("r1")["status"] == "on"
        assert ctl.mapping_service("r1") == "running"

    def test_clearing_the_topo_topic_falls_back_to_the_alias(self):
        ctl = control()
        _send(ctl, ALIAS, {"online": True, "enabled": False})
        _send(ctl, TOPO, {"online": False})
        assert ctl.state("r1")["status"] == "unreachable"
        _send(ctl, TOPO, b"")
        assert ctl.state("r1")["status"] == "off"

    def test_clearing_the_alias(self):
        ctl = control()
        _send(ctl, ALIAS, {"online": True})
        _send(ctl, ALIAS, b"")
        assert ctl.state("r1") is None
        assert ctl.mapping_services("r1")["topo"] == "not_available"

    def test_alias_not_listed_as_a_service(self):
        ctl = control()
        _send(ctl, ALIAS, {"online": True})
        assert set(ctl.mapping_services("r1")) == {"topo", "grid"}


class TestBroadcast:
    async def _collect(self, sends):
        ctl = mc.MappingControl(PREFIX)
        seen = []

        async def on_state(robot, service, view):
            seen.append((robot, service, view and view["status"]))

        ctl.on_state = on_state
        ctl.attach(FakeMqtt(), asyncio.get_running_loop())
        for topic, payload in sends:
            await asyncio.to_thread(ctl.on_state_message, None, None, msg(topic, payload))
        for _ in range(20):
            await asyncio.sleep(0.01)
        return seen

    async def test_names_the_service(self):
        seen = await self._collect([(TOPO, {"online": True, "enabled": True}),
                                    (GRID, {"online": True}),
                                    (GRID, b"")])
        assert seen == [("r1", "topo", "on"), ("r1", "grid", "off"), ("r1", "grid", None)]

    async def test_alias_as_topo_until_the_topo_topic(self):
        seen = await self._collect([(ALIAS, {"online": True}),
                                    (TOPO, {"online": True, "enabled": True}),
                                    (ALIAS, {"online": True, "enabled": True})])
        # the alias after the per-service topic changes nothing and is not broadcast
        assert seen == [("r1", "topo", "off"), ("r1", "topo", "on")]

    async def test_server_message_keys(self):
        from packages.api.server import ApiDelegationService
        sent = []

        class WS:
            async def broadcast(self, kind, robot, message):
                sent.append((kind, robot, message))

        ctl = control()
        _send(ctl, TOPO, {"online": True, "enabled": True})
        _send(ctl, GRID, {"online": False})
        fake = SimpleNamespace(ws_manager=WS(), mapping_control=ctl, logger=None)
        await ApiDelegationService._broadcast_mapping_state(
            fake, "r1", "grid", ctl.state("r1", "grid"))
        kind, robot, m = sent[0]
        assert (kind, robot, m["type"], m["service"]) == (
            "robot_status", "r1", "mapping_state_update", "grid")
        assert m["mapping_state"]["status"] == "on"  # always the topo state
        assert m["service_state"]["status"] == "unreachable"
        assert m["mapping_services"] == {"topo": "running", "grid": "not_running"}


class TestResponses:
    async def test_start_response_lists_services(self, db):
        db.add_map("yard", type="local", status={"state": "draft"})
        db.add_robot("r1")
        ctl = control()
        _send(ctl, TOPO, {"online": True, "enabled": False})
        out = await maps.start_session(
            None, "yard", {"robot": "r1", "services": ["topo", "grid"]}, m1.PUB, "op",
            control=ctl)
        assert out["mapping_service"] == "running"
        assert out["mapping_services"] == {"topo": "running", "grid": "not_available"}
        assert out["mapping_state"]["status"] == "off"
        last = ctl.client.sets()[-1]
        assert last["enabled"] is True and last["services"] == ["topo", "grid"]

    async def test_finish_publishes_the_no_session_payload(self, db):
        db.add_map("yard", type="local", status={"state": "draft"})
        db.add_robot("r1")
        ctl = control()
        out = await maps.start_session(None, "yard", {"robot": "r1"}, m1.PUB, "op",
                                       control=ctl)
        assert ctl.client.sets()[-1]["services"] == ["topo"]
        sid = out["session"]["session_id"]
        await maps.session_action(None, "yard", sid, "finish", m1.PUB, control=ctl)
        last = ctl.client.sets()[-1]
        assert last["enabled"] is False and last["services"] == [] and last["map"] is None

    async def test_session_summary(self, db):
        db.add_map("yard", type="local", status={"state": "draft"})
        db.add_robot("r1")
        ctl = control()
        _send(ctl, ALIAS, {"online": True, "enabled": True})
        summary = await maps.session_summary(None, "yard", ctl)
        assert summary["mapping_services"] is None  # no open mapping session
        await maps.start_session(None, "yard", {"robot": "r1"}, m1.PUB, "op", control=ctl)
        summary = await maps.session_summary(None, "yard", ctl)
        assert summary["mapping_services"] == {"topo": "running", "grid": "not_available"}
        assert summary["mapping_state"]["service"] == "topo"

    def test_robot_view(self):
        ctl = control()
        _send(ctl, TOPO, {"online": True, "enabled": True})
        robot = SimpleNamespace(name="r1", dict=lambda: {"name": "r1"})
        with patch.object(main, "service", SimpleNamespace(mapping_control=ctl)):
            view = main._robot_view(robot)
        assert view["mapping_state"]["status"] == "on"
        assert view["mapping_services"] == {"topo": "running", "grid": "not_available"}
        with patch.object(main, "service", None):
            view = main._robot_view(robot)
        assert view["mapping_services"] is None and view["mapping_state"] is None
