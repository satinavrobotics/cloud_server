"""Maps redesign M3 (docs/satinav-maps-redesign.md §8, §13.3): the robot mapping switch over MQTT.

- packages/api/mapping_control.py: the set payload from the open session, the state cache
  (mapping/state messages), the status view, publishing with a broker acknowledgement;
- packages/api/maps.py: after every committed session change (start, pause, resume, finish)
  the robot's retained set message is published from its open
  session; a failed publish never fails the call (robot_notified false); mapping_service;
  re-publishing every robot on (re)connect; the session summary's mapping_state;
- the routes pass the control and the robot view carries mapping_state.

The in-memory store is the M1 one (tests/unit/test_maps_m1.py). The broker round trip is in
tests/integration/maps (run_m2.sh, checks_m3.py).
"""
import asyncio
import contextlib
import copy
import json
import os
import uuid
from types import SimpleNamespace

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

import packages.api.main as main  # noqa: E402
from packages.api import mapping_control as mc  # noqa: E402
from packages.api import maps  # noqa: E402
from tests.unit import test_maps_m1 as m1  # noqa: E402
from tests.unit.test_maps_m2 import ShimDb, ShimStore  # noqa: E402

pytestmark = pytest.mark.unit

PREFIX = "uagv/v2/RobotCompany"
ENU_DATUM = m1.ENU_DATUM


# --- fakes -------------------------------------------------------------------------------------

class FakeInfo:
    def __init__(self, rc=0, published=True):
        self.rc = rc
        self._published = published
        self.waited = None

    def wait_for_publish(self, timeout=None):
        self.waited = timeout

    def is_published(self):
        return self._published


class FakeMqtt:
    """The MQTTClient surface MappingControl uses."""

    def __init__(self, connected=True, rc=0, acked=True, raises=None):
        self.connected = connected
        self.rc, self.acked, self.raises = rc, acked, raises
        self.published = []
        self.callbacks = {}
        self.listeners = []

    def register_callback(self, topic, callback, qos=0):
        self.callbacks[topic] = (callback, qos)

    def add_connect_listener(self, listener):
        self.listeners.append(listener)

    def publish(self, topic, payload, qos=0, retain=False):
        if self.raises:
            raise self.raises
        self.published.append((topic, json.loads(payload), qos, retain))
        return FakeInfo(self.rc, self.acked)

    def sets(self, robot="r1"):
        return [p for t, p, _, _ in self.published if t == f"{PREFIX}/{robot}/mapping/set"]


def control(**kw):
    ctl = mc.MappingControl(PREFIX + "/")
    ctl.attach(FakeMqtt(**kw), None)
    return ctl


def msg(topic, payload):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return SimpleNamespace(topic=topic, payload=body, retain=True)


class M3Store(ShimStore):
    async def open_sessions(self):
        return [dict(s) for s in self.db.sessions if s["ended_at"] is None]

    async def robot_names(self):
        return list(self.db.robots)


class M3Db(ShimDb):
    @contextlib.asynccontextmanager
    async def store(self, _db, _publisher_id):
        snapshot = copy.deepcopy((self.maps, self.sessions))
        store = M3Store(self)
        try:
            yield store
        except BaseException:
            self.maps, self.sessions = snapshot
            raise
        self.events.extend(store.pending_events)
        self.notifies.extend(store.pending_notifies)


@pytest.fixture
def db():
    d = M3Db()
    with patch.object(maps, "open_store", d.store), patch.object(maps, "_utcnow", m1.Clock()):
        yield d


# --- the contract ------------------------------------------------------------------------------

class TestContract:
    def test_topics(self):
        assert mc.set_topic(PREFIX, "robot") == "uagv/v2/RobotCompany/robot/mapping/set"
        assert mc.state_subscription(PREFIX + "/") == "uagv/v2/RobotCompany/+/mapping/state"

    def test_set_payload(self):
        sid = uuid.uuid4()
        open_s = {"session_id": sid, "map_name": "yard", "paused_at": None, "aligned": True}
        p = mc.set_payload(open_s, m1.T0)
        assert p == {"enabled": True, "session_id": str(sid), "map": "yard",
                     "services": ["topo"], "issued_at": m1.T0.isoformat()}
        assert mc.set_payload({**open_s, "paused_at": m1.T0})["enabled"] is False
        assert mc.set_payload({**open_s, "paused_at": m1.T0})["session_id"] == str(sid)
        p = mc.set_payload(None, m1.T0)
        assert p == {"enabled": False, "session_id": None, "map": None, "services": [],
                     "issued_at": m1.T0.isoformat()}

    def test_state_view(self):
        assert mc.state_view(None) is None
        assert mc.state_view({"online": True, "enabled": True})["status"] == "on"
        assert mc.state_view({"online": True, "enabled": False})["status"] == "off"
        assert mc.state_view({"online": False})["status"] == "unreachable"
        assert mc.state_view({"enabled": True})["status"] == "unreachable"  # no online flag
        assert mc.service_of(None) == "not_running"
        assert mc.service_of({"online": False}) == "not_running"
        assert mc.service_of({"online": True}) == "running"


# --- state ingestion ---------------------------------------------------------------------------

class TestStateIngest:
    def test_attach_registers_before_connect(self):
        ctl = control()
        topic = "uagv/v2/RobotCompany/+/mapping/state"
        assert ctl.client.callbacks[topic][1] == 1 and len(ctl.client.listeners) == 1
        assert ctl.client.callbacks["uagv/v2/RobotCompany/+/mapping/+/state"][1] == 1

    def test_cached_with_received_at(self):
        ctl = control()
        ctl.on_state_message(None, None, msg(f"{PREFIX}/r1/mapping/state", {
            "online": True, "enabled": True, "session_id": "s", "map": "yard",
            "nodes_sent": 4, "since": "t", "stamp": "t2", "source": "mqtt"}))
        st = ctl.state("r1")
        assert st["status"] == "on" and st["nodes_sent"] == 4 and st["received_at"]
        assert ctl.mapping_service("r1") == "running"
        assert ctl.state("r2") is None and ctl.mapping_service("r2") == "not_running"

    def test_last_will_and_clear(self):
        ctl = control()
        ctl.on_state_message(None, None, msg(f"{PREFIX}/r1/mapping/state",
                                             {"online": True, "enabled": True}))
        ctl.on_state_message(None, None, msg(f"{PREFIX}/r1/mapping/state",
                                             {"online": False, "enabled": False}))
        assert ctl.state("r1")["status"] == "unreachable"
        assert ctl.mapping_service("r1") == "not_running"
        ctl.on_state_message(None, None, msg(f"{PREFIX}/r1/mapping/state", b""))
        assert ctl.state("r1") is None

    @pytest.mark.parametrize("topic,payload", [
        (f"{PREFIX}/r1/mapping/state", b"not json"),
        (f"{PREFIX}/r1/mapping/state", b"[1, 2]"),
        ("other/prefix/r1/mapping/state", b'{"online": true}'),
        (f"{PREFIX}/r1/mapping/set", b'{"online": true}'),
    ])
    def test_ignored(self, topic, payload):
        ctl = control()
        ctl.on_state_message(None, None, msg(topic, payload))
        assert ctl.state("r1") is None

    async def test_broadcast_and_reconnect_hook_run_on_the_loop(self):
        ctl = mc.MappingControl(PREFIX)
        seen, synced = [], asyncio.Event()

        async def on_state(robot, service, view):
            seen.append((robot, view["status"]))

        async def on_connect():
            synced.set()

        ctl.on_state, ctl.on_connect = on_state, on_connect
        client = FakeMqtt()
        ctl.attach(client, asyncio.get_running_loop())
        await asyncio.to_thread(ctl.on_state_message, None, None,
                                msg(f"{PREFIX}/r1/mapping/state", {"online": True}))
        await asyncio.to_thread(client.listeners[0])
        await asyncio.wait_for(synced.wait(), 2)
        for _ in range(50):
            if seen:
                break
            await asyncio.sleep(0.01)
        assert seen == [("r1", "off")]


# --- publishing --------------------------------------------------------------------------------

class TestPublish:
    async def test_retained_qos1_acknowledged(self):
        ctl = control()
        assert await ctl.publish_set("r1", {"enabled": True})
        assert ctl.client.published == [(f"{PREFIX}/r1/mapping/set", {"enabled": True}, 1, True)]

    @pytest.mark.parametrize("kw", [dict(connected=False), dict(rc=4), dict(acked=False),
                                    dict(raises=RuntimeError("socket"))])
    async def test_failure_is_false_never_raises(self, kw):
        ctl = control(**kw)
        assert await ctl.publish_set("r1", {"enabled": True}) is False

    async def test_no_client(self):
        assert await mc.MappingControl(PREFIX).publish_set("r1", {}) is False


# --- publish on transition ---------------------------------------------------------------------

def _robot_state(ctl, robot="r1", **state):
    ctl.on_state_message(None, None, msg(f"{PREFIX}/{robot}/mapping/state", state))


class TestTransitions:
    async def test_start_pause_resume_finish(self, db):
        db.add_map("yard", type="local", status={"state": "draft"})
        db.add_robot("r1")
        ctl = control()
        out = await maps.start_session(None, "yard", {"robot": "r1"}, m1.PUB, "op", control=ctl)
        sid = out["session"]["session_id"]
        assert out["robot_notified"] is True and out["mapping_service"] == "not_running"
        assert out["mapping_state"] is None
        assert ctl.client.sets()[-1]["enabled"] is True
        assert ctl.client.sets()[-1]["session_id"] == sid
        assert ctl.client.sets()[-1]["map"] == "yard"

        _robot_state(ctl, online=True, enabled=True, session_id=sid, map="yard")
        out = await maps.session_action(None, "yard", sid, "pause", m1.PUB, control=ctl)
        assert out["robot_notified"] and out["mapping_state"]["status"] == "on"
        assert "mapping_service" not in out
        assert ctl.client.sets()[-1] == {**ctl.client.sets()[-1], "enabled": False,
                                         "session_id": sid, "map": "yard"}

        out = await maps.session_action(None, "yard", sid, "resume", m1.PUB, control=ctl)
        assert ctl.client.sets()[-1]["enabled"] is True

        out = await maps.session_action(None, "yard", sid, "finish", m1.PUB, control=ctl)
        last = ctl.client.sets()[-1]
        assert last["enabled"] is False and last["session_id"] is None and last["map"] is None
        assert len(ctl.client.sets()) == 4

    async def test_noop_repeat_republishes_the_current_state(self, db):
        db.add_map("yard", type="local", status={"state": "paused"})
        db.add_robot("r1")
        s = db.add_session("yard", "r1", "live", ended=False, paused_at=m1.T0)
        ctl = control()
        out = await maps.session_action(None, "yard", str(s["session_id"]), "pause", m1.PUB,
                                        control=ctl)
        assert out["changed"] is False and out["robot_notified"] is True
        assert ctl.client.sets()[-1]["enabled"] is False
        assert ctl.client.sets()[-1]["session_id"] == str(s["session_id"])

    async def test_finish_of_an_old_session_sends_the_robots_current_one(self, db):
        db.add_map("old", type="local")
        db.add_map("yard", type="local", status={"state": "mapping"})
        db.add_robot("r1")
        old = db.add_session("old", "r1", "live", ended=True)
        cur = db.add_session("yard", "r1", "live", ended=False)
        ctl = control()
        out = await maps.session_action(None, "old", str(old["session_id"]), "finish", m1.PUB,
                                        control=ctl)
        assert out["changed"] is False
        assert ctl.client.sets()[-1]["session_id"] == str(cur["session_id"])
        assert ctl.client.sets()[-1]["enabled"] is True

    async def test_publish_failure_never_fails_the_call(self, db):
        db.add_map("yard", type="local", status={"state": "draft"})
        db.add_robot("r1")
        ctl = control(connected=False)
        out = await maps.start_session(None, "yard", {"robot": "r1"}, m1.PUB, control=ctl)
        assert out["changed"] and out["robot_notified"] is False
        assert db.open_session("r1")  # committed
        assert db.codes() == ["MAP.SESSION_STARTED"]

    async def test_store_failure_after_commit_is_robot_notified_false(self, db):
        db.add_map("yard", type="local", status={"state": "draft"})
        db.add_robot("r1")
        ctl = control()
        real = db.store
        calls = {"n": 0}

        @contextlib.asynccontextmanager
        async def flaky(_db, _pub):
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("pool exhausted")
            async with real(_db, _pub) as store:
                yield store

        with patch.object(maps, "open_store", flaky):
            out = await maps.start_session(None, "yard", {"robot": "r1"}, m1.PUB, control=ctl)
        assert out["robot_notified"] is False and out["session"]["state"] == "mapping"

    async def test_refused_start_publishes_nothing(self, db):
        db.add_map("yard", type="local")
        db.add_robot("r1", online=False)
        ctl = control()
        with pytest.raises(HTTPException) as exc:
            await maps.start_session(None, "yard", {"robot": "r1"}, m1.PUB, control=ctl)
        assert exc.value.status_code == 409 and ctl.client.published == []

    async def test_mapping_service_running(self, db):
        db.add_map("yard", type="local", status={"state": "draft"})
        db.add_robot("r1")
        ctl = control()
        _robot_state(ctl, online=True, enabled=False, session_id=None, map=None)
        out = await maps.start_session(None, "yard", {"robot": "r1"}, m1.PUB, control=ctl)
        assert out["mapping_service"] == "running"
        assert out["mapping_state"]["status"] == "off"  # the robot has not confirmed yet

    async def test_without_control_the_response_is_unchanged(self, db):
        db.add_map("yard", type="local", status={"state": "draft"})
        db.add_robot("r1")
        out = await maps.start_session(None, "yard", {"robot": "r1"}, m1.PUB)
        assert set(out) == {"map_id", "map_state", "changed", "session", "replaced_session"}


class TestResync:
    async def test_every_robot_from_its_open_session(self, db):
        db.add_map("yard", type="local", status={"state": "paused"})
        db.add_map("lot", type="local", status={"state": "mapping"})
        for r in ("r1", "r2", "r3"):
            db.add_robot(r)
        s1 = db.add_session("yard", "r1", "live", ended=False, paused_at=m1.T0)
        s2 = db.add_session("lot", "r2", "live", ended=False)
        db.add_session("yard")  # legacy, ended
        ctl = control()
        out = await maps.sync_all_robots(ctl, None)
        assert out == {"r1": True, "r2": True, "r3": True}
        assert ctl.client.sets("r1")[-1]["enabled"] is False
        assert ctl.client.sets("r1")[-1]["session_id"] == str(s1["session_id"])
        assert ctl.client.sets("r2")[-1]["enabled"] is True
        assert ctl.client.sets("r2")[-1]["session_id"] == str(s2["session_id"])
        assert ctl.client.sets("r3")[-1] == {**ctl.client.sets("r3")[-1], "enabled": False,
                                             "session_id": None, "map": None}
        assert ctl.client.sets("legacy") == []


class TestSummaryAndRoutes:
    async def test_summary_mapping_state(self, db):
        db.add_map("yard", type="local", status={"state": "mapping"})
        s = db.add_session("yard", "r1", "live", ended=False)
        ctl = control()
        out = await maps.session_summary(None, "yard", ctl)
        assert out["mapping_state"] is None and out["mapping_service"] == "not_running"
        _robot_state(ctl, online=True, enabled=True, session_id=str(s["session_id"]),
                     map="yard")
        out = await maps.session_summary(None, "yard", ctl)
        assert out["mapping_state"]["status"] == "on"
        assert out["mapping_service"] == "running"
        out = await maps.session_summary(None, "yard")
        assert out["mapping_state"] is None and out["mapping_service"] is None

    async def test_summary_without_open_session(self, db):
        db.add_map("yard", type="local")
        db.add_session("yard")
        ctl = control()
        _robot_state(ctl, robot="legacy", online=True)
        out = await maps.session_summary(None, "yard", ctl)
        assert out["mapping_state"] is None and out["mapping_service"] is None

    async def test_routes_pass_the_control(self, db):
        db.add_map("yard", type="local", status={"state": "draft"})
        db.add_robot("r1")
        svc = MagicMock()
        svc.database = None
        svc.mapping_control = control()
        svc.graph_db.get_map_stats.return_value = {"node_count": 0}
        with patch.object(main, "service", svc):
            out = await main.start_map_session("yard", {"robot": "r1"})
            sid = out["session"]["session_id"]
            out = await main.map_session_action("yard", sid, "pause")
        assert out["robot_notified"] is True
        assert [p["enabled"] for p in svc.mapping_control.client.sets()] == [True, False]

    async def test_robot_view(self):
        from cloud_common.objects.robot import RobotObjectV1, RobotStatusV1
        svc = MagicMock()
        svc.mapping_control = control()
        _robot_state(svc.mapping_control, online=True, enabled=True)
        robot = RobotObjectV1(name="r1", status=RobotStatusV1(online=True))
        svc.database.get_object = AsyncMock(return_value=robot)
        svc.database.list_objects = AsyncMock(return_value=[robot])
        with patch.object(main, "service", svc):
            one = await main.get_robot("r1")
            many = await main.list_robots()
        assert one["mapping_state"]["status"] == "on" and one["name"] == "r1"
        assert many[0]["mapping_state"]["status"] == "on"
        json.dumps(one, default=str)


class TestForceOff:
    """POST /robots/{r}/mapping/off: a forced no-session set message, only without a session."""

    def test_payload(self):
        p = mc.force_off_payload()
        assert p["force"] is True and p["enabled"] is False
        assert p["session_id"] is None and p["map"] is None and p["issued_at"]
        assert "force" not in mc.set_payload(None)

    async def test_publishes_forced_off_without_a_session(self, db):
        db.add_robot("r1")
        db.add_map("old", type="local")
        db.add_session("old", "r1", "live", ended=True)
        ctl = control()
        _robot_state(ctl, online=True, enabled=True, session_id=None, map=None, source="local")
        out = await maps.robot_mapping_off(None, "r1", ctl)
        assert out["robot_notified"] is True
        assert out["mapping_state"]["status"] == "on"  # until the robot answers
        topic, payload, qos, retain = ctl.client.published[-1]
        assert topic == f"{PREFIX}/r1/mapping/set" and qos == 1 and retain is True
        assert {k: payload[k] for k in ("enabled", "session_id", "map", "force")} == {
            "enabled": False, "session_id": None, "map": None, "force": True}

    async def test_open_session_is_409_and_publishes_nothing(self, db):
        db.add_robot("r1")
        db.add_map("yard", type="local", status={"state": "paused"})
        db.add_session("yard", "r1", "live", ended=False, paused_at=m1.T0)
        ctl = control()
        with pytest.raises(HTTPException) as err:
            await maps.robot_mapping_off(None, "r1", ctl)
        assert err.value.status_code == 409
        assert "finish or pause the session on map yard" in err.value.detail
        assert ctl.client.published == []

    async def test_unknown_robot_is_404(self, db):
        ctl = control()
        with pytest.raises(HTTPException) as err:
            await maps.robot_mapping_off(None, "ghost", ctl)
        assert err.value.status_code == 404 and ctl.client.published == []

    async def test_broker_down_is_robot_notified_false(self, db):
        db.add_robot("r1")
        out = await maps.robot_mapping_off(None, "r1", control(connected=False))
        assert out["robot_notified"] is False

    async def test_route(self, db):
        db.add_robot("r1")
        svc = MagicMock()
        svc.database = None
        svc.mapping_control = control()
        with patch.object(main, "service", svc):
            out = await main.robot_mapping_off("r1")
        assert out["robot_notified"] is True
        assert svc.mapping_control.client.sets()[-1]["force"] is True
