"""graph-builder against resent / redelivered / buffered robot uploads (phase 1):

- deterministic global node ids (uuid5 of robot, the open cloud session, session_node_id[, run]);
- a resent node is a no-op (no add_node, edges, node_count or WebSocket publish);
- image / depth / costmap of a node stored before a restart go straight to it;
- `run_id` decides a robot restart; a lower id with the same run_id wipes nothing;
- the upload topics are subscribed at QoS 1, the persistent session is opt-in.
"""
import base64
import os
from unittest.mock import MagicMock, Mock, patch

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

import pytest  # noqa: E402

from packages.services.graph_builder import server as gb_server  # noqa: E402
from packages.services.graph_builder.server import GraphBuilderService  # noqa: E402
from packages.services.graph_builder.upload_buffer import NodeIdCache  # noqa: E402
from packages.utils import mqtt_client as mqtt_mod  # noqa: E402
from tests.unit.test_costmap_ingest import COSTMAP  # noqa: E402
from tests.unit.test_map_reconstruction_ingest import DEPTH  # noqa: E402
from tests.unit.test_maps_m2 import IMAGE, NODE, _gb, _row  # noqa: E402

pytestmark = pytest.mark.unit


def _service(**kw):
    service = _gb(_row(**kw))
    service.image_db.store_depth = Mock(return_value=True)
    service.image_db.store_costmap = Mock(return_value=True)
    service.graph_db.set_node_depth = Mock(return_value=True)
    service.graph_db.set_node_costmap = Mock(return_value=True)
    return service


def _restarted(stored):
    """A graph-builder with empty memory whose ArangoDB holds the nodes in `stored`."""
    service = _service()
    service.graph_db.node_exists = Mock(side_effect=lambda _map, gid: gid in stored)
    return service


class TestDeterministicId:
    def test_same_node_same_id_and_every_part_differs(self):
        s = _service()
        gid = s._node_global_id("r1", "s1", 7)
        assert gid == s._node_global_id("r1", "s1", 7)
        assert gid == str(gb_server.uuid.uuid5(gb_server.NODE_ID_NAMESPACE, "r1:s1:7"))
        others = {s._node_global_id("r2", "s1", 7), s._node_global_id("r1", "s2", 7),
                  s._node_global_id("r1", "s1", 8), s._node_global_id("r1", "s1", 7, "run-b")}
        assert gid not in others and len(others) == 4

    def test_no_session_gets_a_fresh_id(self):
        s = _service()
        assert s._node_global_id("r1", None, 7) != s._node_global_id("r1", None, 7)

    async def test_uses_the_open_session_not_the_payloads(self):
        s = _service(sid="s1")
        await s._handle_node_update({**NODE, "session_id": "payload-session"})
        assert s.graph_db.add_node.call_args.kwargs["node_id"] == s._node_global_id("r1", "s1", 7)


class TestDuplicateNode:
    async def test_resend_is_a_noop(self):
        s = _service()
        s.graph_db.nodes_in_range = Mock(return_value=([{"node_id": "n0", "pose": {
            "x": 3.0, "y": 4.0}}], [0.5]))
        await s._handle_node_update(dict(NODE))
        gid = s.graph_db.add_node.call_args.kwargs["node_id"]
        # same node again, with other content: first write wins
        await s._handle_node_update({**NODE, "x": 9.0, "metadata": {"source": "other"}})
        s.graph_db.add_node.assert_called_once()
        s.graph_db.add_edges_bulk.assert_called_once()
        s._count_nodes.assert_awaited_once()
        s._publish_node_update.assert_awaited_once()
        assert s.stats["nodes_processed"] == 1 and s.stats["nodes_duplicate"] == 1
        assert s.session_to_global_map[("r1", 7)][0] == gid
        assert s.graph_db.node_exists.call_count == 1  # only the first (new) node asked ArangoDB

    async def test_resend_after_restart_checks_arango(self):
        gid = _service()._node_global_id("r1", "s1", 7)
        s = _restarted({gid})
        await s._handle_node_update(dict(NODE))
        s.graph_db.add_node.assert_not_called()
        s.graph_db.add_edges_bulk.assert_not_called()
        s._count_nodes.assert_not_awaited()
        s._publish_node_update.assert_not_awaited()
        assert s.stats["nodes_duplicate"] == 1
        # what a later part needs is restored
        assert s._mapped_node(("r1", 7), "s1") == (gid, "yard")
        assert s._stored_node(("r1", 7), "s1")[0] == gid

    async def test_buffered_parts_are_attached_by_a_duplicate(self):
        gid = _service()._node_global_id("r1", "s1", 7)
        s = _restarted(set())
        await s._handle_image_upload(dict(IMAGE))  # node unknown: buffered
        assert ("r1", 7) in s.image_buffer
        s.graph_db.node_exists = Mock(side_effect=lambda _m, g: g == gid)
        await s._handle_node_update(dict(NODE))
        s.graph_db.add_node.assert_not_called()
        s.image_db.store_image.assert_called_once()
        assert s.image_buffer == {}

    async def test_concurrent_claim_is_a_duplicate(self):
        s = _service()
        gid = s._node_global_id("r1", "s1", 7)
        s._processing.add(("yard", gid))
        assert s._process_topology(dict(NODE), map_id="yard", session_id="s1") is None
        s.graph_db.add_node.assert_not_called()


class TestPartsAfterRestart:
    async def test_image_of_an_existing_node_is_stored_at_once(self):
        gid = _service()._node_global_id("r1", "s1", 7)
        s = _restarted({gid})
        await s._handle_image_upload(dict(IMAGE))
        assert s.image_db.store_image.call_args.kwargs["node_id"] == gid
        assert s.image_buffer == {}
        s._publish_image_update.assert_awaited_once()

    async def test_image_of_an_unknown_node_is_buffered(self):
        s = _restarted(set())
        await s._handle_image_upload(dict(IMAGE))
        s.image_db.store_image.assert_not_called()
        assert ("r1", 7) in s.image_buffer

    @pytest.mark.parametrize("kind,payload,store,record", [
        ("depth", DEPTH, "store_depth", "set_node_depth"),
        ("costmap", COSTMAP, "store_costmap", "set_node_costmap")])
    async def test_depth_and_costmap_of_an_existing_node_are_stored_at_once(
            self, kind, payload, store, record):
        gid = _service()._node_global_id("r1", "s1", 7)
        s = _restarted({gid})
        await getattr(s, f"_handle_{kind}_upload")(dict(payload))
        assert getattr(s.image_db, store).call_args.args[1] == gid
        assert getattr(s.graph_db, record).call_args.args[1] == gid
        assert getattr(s, f"{kind}_buffer") == {}

    @pytest.mark.parametrize("kind,payload", [("depth", DEPTH), ("costmap", COSTMAP)])
    async def test_depth_and_costmap_of_an_unknown_node_are_buffered(self, kind, payload):
        s = _restarted(set())
        await getattr(s, f"_handle_{kind}_upload")(dict(payload))
        assert ("r1", 7) in getattr(s, f"{kind}_buffer")

    async def test_existence_is_remembered(self):
        gid = _service()._node_global_id("r1", "s1", 7)
        s = _restarted({gid})
        for camera in ("left", "right"):
            await s._handle_image_upload({**IMAGE, "camera_name": camera})
        assert s.graph_db.node_exists.call_count == 1
        assert s.image_db.store_image.call_count == 2


class TestRunId:
    async def _nodes(self, s, ids, **extra):
        for i in ids:
            await s._handle_node_update({**NODE, "session_node_id": i, **extra})

    async def test_lower_id_with_the_same_run_id_wipes_nothing(self):
        s = _service()
        await self._nodes(s, [1, 2, 3], run_id="a")
        await self._nodes(s, [1], run_id="a")  # a resend
        assert {k[1] for k in s.session_to_global_map} == {1, 2, 3}
        assert s.stats["nodes_duplicate"] == 1 and s.graph_db.add_node.call_count == 3

    async def test_run_id_change_resets_and_ids_differ(self):
        s = _service()
        await self._nodes(s, [1, 2, 3], run_id="a")
        old = s.session_to_global_map[("r1", 1)][0]
        await self._nodes(s, [1], run_id="b")
        assert set(s.session_to_global_map) == {("r1", 1)}
        assert s.session_to_global_map[("r1", 1)][0] != old
        assert s.graph_db.add_node.call_count == 4  # not a duplicate of run a's node 1

    async def test_buffered_upload_of_the_new_run_survives_the_reset(self):
        s = _service()
        await self._nodes(s, [1, 2], run_id="a")
        await s._handle_image_upload({**IMAGE, "session_node_id": 1, "run_id": "b"})
        await s._handle_image_upload({**IMAGE, "session_node_id": 9, "run_id": "a"})
        assert ("r1", 1) in s.image_buffer  # not mapped: run b's node 1 is not run a's
        await self._nodes(s, [1], run_id="b")
        assert s.image_buffer == {} and s.image_db.store_image.call_count == 1

    async def test_without_run_id_a_smaller_id_is_still_a_reset(self):
        s = _service()
        await self._nodes(s, [1, 2, 3])
        await self._nodes(s, [1])
        assert set(s.session_to_global_map) == {("r1", 1)}
        assert s.graph_db.add_node.call_count == 4  # the restarted counter's node 1 is new

    async def test_without_run_id_a_lower_id_resend_resets_as_before(self):
        s = _service()
        await self._nodes(s, [1, 2, 3])
        await self._nodes(s, [2])  # cannot be told from a restart: old behaviour
        assert set(s.session_to_global_map) == {("r1", 2)}
        assert s.stats["nodes_duplicate"] == 0


class TestSubscriptions:
    def _connect(self, persistent):
        s = _service()
        with patch.object(gb_server, "MQTTClient") as client_cls, \
                patch.object(gb_server, "GRAPH_BUILDER_MQTT_PERSISTENT_SESSION", persistent):
            assert s.connect_mqtt()
        return client_cls

    def test_all_uploads_are_subscribed_at_qos_1(self):
        client = self._connect(False).return_value
        topics = {c.args[0]: c.kwargs["qos"] for c in client.register_callback.call_args_list}
        assert topics == {"robot/node_update": 1, "robot/image_upload": 1,
                          "robot/depth_upload": 1, "robot/costmap_upload": 1}

    def test_clean_session_with_a_fresh_client_id_by_default(self):
        kw = self._connect(False).call_args.kwargs
        assert kw["clean_session"] is True and kw["client_id"].startswith("graph_builder_")

    def test_persistent_session_is_opt_in_with_a_stable_client_id(self):
        kw = self._connect(True).call_args.kwargs
        assert kw["clean_session"] is False and kw["client_id"] == "graph_builder"


class TestMQTTClientSession:
    @pytest.fixture
    def paho(self, monkeypatch):
        fake_cls = MagicMock()
        monkeypatch.setattr(mqtt_mod.mqtt_client, "Client", fake_cls)
        return fake_cls

    def test_clean_session_is_passed_to_paho(self, paho):
        mqtt_mod.MQTTClient("t")
        assert paho.call_args.kwargs["clean_session"] is True
        mqtt_mod.MQTTClient("t", clean_session=False)
        assert paho.call_args.kwargs["clean_session"] is False

    def test_reconnect_resubscribes_at_the_registered_qos(self, paho):
        c = mqtt_mod.MQTTClient("t", clean_session=False)
        c.register_callback("robot/node_update", lambda *a: None, qos=1)
        c.register_callback("other", lambda *a: None)
        c._on_connect(paho.return_value, None, {}, 0)
        paho.return_value.subscribe.assert_any_call("robot/node_update", qos=1)
        paho.return_value.subscribe.assert_any_call("other", qos=0)

    def test_watchdog_still_covers_a_never_connected_persistent_client(self, paho):
        c = mqtt_mod.MQTTClient("t", clean_session=False)
        c.connect()
        assert c._watchdog_thread.is_alive() and c._disconnected_since is not None


class TestNodeIdCache:
    def test_lru_evicts_the_least_recently_used(self):
        c = NodeIdCache(2)
        c.add(("m", "a"))
        c.add(("m", "b"))
        assert ("m", "a") in c  # a is now the most recent
        c.add(("m", "c"))
        assert ("m", "a") in c and ("m", "b") not in c and len(c) == 2
        c.discard(("m", "a"))
        assert ("m", "a") not in c
