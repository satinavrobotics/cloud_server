"""graph-builder against resent / redelivered / buffered robot uploads (phase 1):

- deterministic global node ids (uuid5 of robot, the open cloud session, session_node_id[, run]);
- a resent node is a no-op (no add_node, edges, node_count or WebSocket publish);
- image / depth / costmap of a node stored before a restart go straight to it;
- `run_id` decides a robot restart; a lower id with the same run_id wipes nothing;
- the upload topics are subscribed at QoS 1, the persistent session is opt-in.
"""
import base64
import os
from unittest.mock import AsyncMock, MagicMock, Mock, patch

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


def _unguarded(service):
    """Without the run_id session guard (phase 2): these tests look at the layers below it
    (the reset on a run_id change, the buffers' run filter), which a mixed-run session no
    longer reaches."""
    service._run_id_conflict = AsyncMock(return_value=False)
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
        s.claim_wait_s = 0.05  # nobody releases this claim: the wait times out
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
        s = _unguarded(_service())
        await self._nodes(s, [1, 2, 3], run_id="a")
        old = s.session_to_global_map[("r1", 1)][0]
        await self._nodes(s, [1], run_id="b")
        assert set(s.session_to_global_map) == {("r1", 1)}
        assert s.session_to_global_map[("r1", 1)][0] != old
        assert s.graph_db.add_node.call_count == 4  # not a duplicate of run a's node 1

    async def test_buffered_upload_of_the_new_run_survives_the_reset(self):
        s = _unguarded(_service())
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
                          "robot/depth_upload": 1, "robot/costmap_upload": 1,
                          "robot/+/ingest_state": 1, "robot/+/ingest_mode": 1,
                          "robot/+/spool_state": 1}

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


# ---------------------------------------------------------------------------------------------
# Hardening: half-written nodes, concurrent resends, mixed run_id presence, unknown lookups.


class _Arango:
    """A tiny shared ArangoDB for a graph-builder's graph_db mocks: nodes (with their
    `ingest_complete` marker and run_id) that survive a graph-builder 'restart'."""

    def __init__(self):
        self.nodes = {}  # gid -> metadata
        self.edge_calls = []

    def attach(self, service):
        g = service.graph_db

        def add_node(map_id, node_id, x, y, yaw, metadata=None):
            self.nodes[node_id] = dict(metadata or {})
            return True
        g.add_node = Mock(side_effect=add_node)
        g.node_exists = Mock(side_effect=lambda _m, gid: gid in self.nodes)
        g.node_ingest_incomplete = Mock(
            side_effect=lambda _m, gid: self.nodes[gid].get("ingest_complete") is False)

        def complete(_m, gid):
            self.nodes[gid]["ingest_complete"] = True
            return True
        g.mark_node_complete = Mock(side_effect=complete)
        g.last_run_id = Mock(side_effect=lambda _m, robot, _s: next(
            (md["run_id"] for md in self.nodes.values()
             if md.get("run_id") and md.get("robot_name") == robot), None))

        def bulk(edges, map_id=None, **kw):
            self.edge_calls.append((list(edges), kw))
            return len(edges)
        g.add_edges_bulk = Mock(side_effect=bulk)
        return service

    def service(self):
        return self.attach(_service())


NEAR = ([{"node_id": "n0", "pose": {"x": 3.0, "y": 4.0}}], [0.5])


class TestHalfWrittenNode:
    async def _first_attempt_fails_on_edges(self, s, arango):
        s.graph_db.nodes_in_range = Mock(return_value=NEAR)
        s.graph_db.add_edges_bulk = Mock(side_effect=RuntimeError("arango down"))
        await s._handle_node_update(dict(NODE))
        assert arango.nodes and list(arango.nodes.values())[0]["ingest_complete"] is False
        s._count_nodes.assert_not_awaited()  # not counted, not published
        s._publish_node_update.assert_not_awaited()
        assert s.stats["errors"] == 1

    async def test_resend_repairs_edges_and_counts_exactly_once(self):
        arango = _Arango()
        s = arango.service()
        await self._first_attempt_fails_on_edges(s, arango)
        arango.attach_bulk = None
        s.graph_db.add_edges_bulk = Mock(side_effect=lambda edges, map_id=None, **kw: len(edges))
        await s._handle_node_update(dict(NODE))  # the resend repairs
        assert s.graph_db.add_node.call_count == 1  # first write wins
        kw = s.graph_db.add_edges_bulk.call_args.kwargs
        assert kw["deterministic_keys"] is True and len(s.graph_db.add_edges_bulk.call_args.args[0]) == 2
        s._count_nodes.assert_awaited_once()
        s._publish_node_update.assert_awaited_once()
        gid = next(iter(arango.nodes))
        assert arango.nodes[gid]["ingest_complete"] is True
        assert s.stats["nodes_repaired"] == 1
        await s._handle_node_update(dict(NODE))  # complete now: a plain duplicate
        s._count_nodes.assert_awaited_once()
        assert s.graph_db.add_edges_bulk.call_count == 1 and s.stats["nodes_duplicate"] == 1

    async def test_repair_after_a_restart_reads_the_marker(self):
        arango = _Arango()
        s = arango.service()
        await self._first_attempt_fails_on_edges(s, arango)
        s2 = arango.service()  # restart: empty memory, same ArangoDB
        s2.graph_db.nodes_in_range = Mock(return_value=NEAR)
        await s2._handle_node_update(dict(NODE))
        s2._count_nodes.assert_awaited_once()
        assert s2.graph_db.add_edges_bulk.call_count == 1
        assert list(arango.nodes.values())[0]["ingest_complete"] is True
        await s2._handle_node_update(dict(NODE))
        s2._count_nodes.assert_awaited_once()

    async def test_failed_count_leaves_the_node_to_be_repaired_not_marked(self):
        arango = _Arango()
        s = arango.service()
        s._count_nodes = AsyncMock(return_value=False)
        await s._handle_node_update(dict(NODE))
        s.graph_db.mark_node_complete.assert_not_called()
        s._count_nodes = AsyncMock(return_value=True)
        await s._handle_node_update(dict(NODE))
        s._count_nodes.assert_awaited_once()
        s.graph_db.mark_node_complete.assert_called_once()

    async def test_complete_and_legacy_nodes_are_not_repaired(self):
        arango = _Arango()
        s = arango.service()
        await s._handle_node_update(dict(NODE))
        gid = next(iter(arango.nodes))
        arango.nodes[gid].pop("ingest_complete")  # stored before the marker existed
        for complete in (True, None):
            if complete:
                arango.nodes[gid]["ingest_complete"] = True
            s2 = arango.service()
            await s2._handle_node_update(dict(NODE))
            s2.graph_db.add_edges_bulk.assert_not_called()
            s2._count_nodes.assert_not_awaited()
            s2.graph_db.add_node.assert_not_called()
            assert s2.stats["nodes_duplicate"] == 1


class TestDeterministicEdgeKeys:
    def _db(self, results=None, fail=None):
        from packages.topomap_dbs.graph_db.server import GraphDatabaseService
        db = GraphDatabaseService.__new__(GraphDatabaseService)
        db.logger = MagicMock()
        db.db = MagicMock()
        col = db.db.collection.return_value
        col.insert_many.side_effect = fail
        if fail is None:
            col.insert_many.side_effect = lambda docs, **kw: results if results is not None else list(docs)
        return db, col

    EDGES = [{"from_node_id": "a", "to_node_id": "b"}, {"from_node_id": "b", "to_node_id": "a"}]

    def test_keys_are_stable_and_inserted_with_conflict_ignore(self):
        db, col = self._db()
        assert db.add_edges_bulk(self.EDGES, map_id="m", deterministic_keys=True) == 2
        assert db.add_edges_bulk(self.EDGES, map_id="m", deterministic_keys=True) == 2
        first, second = (c.args[0] for c in col.insert_many.call_args_list)
        assert [d["_key"] for d in first] == [d["_key"] for d in second] == ["a:b", "b:a"]
        assert col.insert_many.call_args.kwargs["overwrite_mode"] == "ignore"

    def test_default_keeps_auto_keys_and_old_failure_behaviour(self):
        db, col = self._db(fail=RuntimeError("down"))
        assert db.add_edges_bulk(self.EDGES, map_id="m") == 0  # logged, 0
        col.insert_many.assert_called_once()
        assert "_key" not in col.insert_many.call_args.args[0][0]
        assert "overwrite_mode" not in col.insert_many.call_args.kwargs

    def test_raise_on_error_tells_failure_from_no_edges(self):
        db, _ = self._db(fail=RuntimeError("down"))
        with pytest.raises(RuntimeError):
            db.add_edges_bulk(self.EDGES, map_id="m", raise_on_error=True)
        db, _ = self._db(results=[{"_key": "a:b"}, ValueError("bad")])
        assert db.add_edges_bulk(self.EDGES, map_id="m") == 1  # as before
        with pytest.raises(RuntimeError):
            db.add_edges_bulk(self.EDGES, map_id="m", raise_on_error=True)
        assert db.add_edges_bulk([], raise_on_error=True) == 0


class TestConcurrentResend:
    def test_resend_waits_for_a_failing_first_attempt_then_stores(self):
        import threading
        arango = _Arango()
        s = arango.service()
        s.claim_wait_s = 5
        gate, started = threading.Event(), threading.Event()
        calls = []
        real = s.graph_db.add_node.side_effect

        def add_node(*a, **kw):
            calls.append(1)
            if len(calls) == 1:  # the first attempt: slow, then fails
                started.set()
                gate.wait(5)
                return False
            return real(*a, **kw)
        s.graph_db.add_node.side_effect = add_node
        results = []
        t1 = threading.Thread(target=lambda: results.append(
            s._process_topology(dict(NODE), map_id="yard", session_id="s1")))
        t1.start()
        assert started.wait(5)
        t2 = threading.Thread(target=lambda: results.append(
            s._process_topology(dict(NODE), map_id="yard", session_id="s1")))
        t2.start()
        t2.join(0.3)
        assert t2.is_alive()  # waiting on the claim, not polling Arango
        assert s.graph_db.node_exists.call_count == 1
        gate.set()
        t1.join(5)
        t2.join(5)
        assert len(calls) == 2 and len(arango.nodes) == 1
        assert sorted(r is None for r in results) == [False, True]  # t1 failed, t2 stored
        assert s._processing == set()

    def test_resend_waits_for_a_successful_first_attempt_and_is_a_duplicate(self):
        import threading
        arango = _Arango()
        s = arango.service()
        s.claim_wait_s = 5
        started, gate = threading.Event(), threading.Event()
        real = s.graph_db.add_node.side_effect

        def add_node(*a, **kw):
            started.set()
            gate.wait(5)
            return real(*a, **kw)
        s.graph_db.add_node.side_effect = add_node
        out = []
        t1 = threading.Thread(target=lambda: out.append(s._process_topology(
            dict(NODE), map_id="yard", session_id="s1", hold_claim=True)))
        t1.start()
        assert started.wait(5)
        t2 = threading.Thread(target=lambda: out.append(s._process_topology(
            dict(NODE), map_id="yard", session_id="s1")))
        t2.start()
        gate.set()
        t1.join(5)
        t2.join(0.3)
        assert t2.is_alive()  # the claim is held until the caller counted + completed the node
        gid = next(iter(arango.nodes))
        s._complete_node("yard", gid)
        s._release_claim(("yard", gid))
        t2.join(5)
        assert s.graph_db.add_node.call_count == 1 and s.stats["nodes_duplicate"] == 1
        s.graph_db.add_edges_bulk.assert_called_once()  # no repair of a completed node


class TestMixedRunId:
    async def test_node_with_run_image_without_after_restart(self):
        arango = _Arango()
        s = arango.service()
        await s._handle_node_update({**NODE, "run_id": "R"})
        s2 = arango.service()  # restart
        await s2._handle_image_upload(dict(IMAGE))  # no run_id
        s2.image_db.store_image.assert_called_once()
        assert s2.image_buffer == {}
        assert s2.graph_db.node_exists.call_count == 1  # probe is not a node lookup

    async def test_node_without_run_image_with_after_restart(self):
        arango = _Arango()
        await arango.service()._handle_node_update(dict(NODE))
        s2 = arango.service()
        await s2._handle_image_upload({**IMAGE, "run_id": "R"})
        s2.image_db.store_image.assert_called_once()
        assert s2.image_buffer == {}

    @pytest.mark.parametrize("kind,payload,store", [
        ("depth", DEPTH, "store_depth"), ("costmap", COSTMAP, "store_costmap")])
    async def test_layers_find_a_node_stored_with_a_run(self, kind, payload, store):
        arango = _Arango()
        await arango.service()._handle_node_update({**NODE, "run_id": "R"})
        s2 = arango.service()
        await getattr(s2, f"_handle_{kind}_upload")(dict(payload))
        assert getattr(s2.image_db, store).call_count == 1
        assert getattr(s2, f"{kind}_buffer") == {}

    async def test_node_resend_without_run_is_a_duplicate_after_restart(self):
        arango = _Arango()
        await arango.service()._handle_node_update({**NODE, "run_id": "R"})
        s2 = arango.service()
        await s2._handle_node_update(dict(NODE))
        s2.graph_db.add_node.assert_not_called()
        s2._count_nodes.assert_not_awaited()
        assert s2.stats["nodes_duplicate"] == 1 and len(arango.nodes) == 1

    async def test_node_resend_without_run_in_memory_and_no_legacy_wipe(self):
        s = _service()
        for i in (1, 2, 3):
            await s._handle_node_update({**NODE, "session_node_id": i, "run_id": "R"})
        await s._handle_node_update({**NODE, "session_node_id": 2})  # lower id, no run_id
        assert {k[1] for k in s.session_to_global_map} == {1, 2, 3}
        assert s.graph_db.add_node.call_count == 3 and s.stats["nodes_duplicate"] == 1
        assert s._legacy_epochs == {}

    async def test_run_id_appearing_mid_stream_loses_nothing(self):
        arango = _Arango()
        s = arango.service()
        for i in (1, 2, 3):
            await s._handle_node_update({**NODE, "session_node_id": i})
        await s._handle_node_update({**NODE, "session_node_id": 4, "run_id": "R"})
        await s._handle_node_update({**NODE, "session_node_id": 3})  # resend, now w/o run? old
        assert len(arango.nodes) == 4 and s.stats["nodes_duplicate"] == 1
        # an image of the old, run-less node 2 with the new run_id still finds it
        await s._handle_image_upload({**IMAGE, "session_node_id": 2, "run_id": "R"})
        s.image_db.store_image.assert_called_once()

    async def test_upload_of_an_earlier_run_does_not_attach_to_this_runs_node(self):
        arango = _Arango()
        s = _unguarded(arango.service())
        await s._handle_node_update({**NODE, "run_id": "B"})
        await s._handle_image_upload({**IMAGE, "run_id": "A"})
        s.image_db.store_image.assert_not_called()
        assert ("r1", 7) in s.image_buffer


class TestUnknownExistence:
    async def test_node_lookup_error_writes_nothing(self):
        s = _service()
        s.graph_db.node_exists = Mock(side_effect=RuntimeError("arango down"))
        await s._handle_node_update(dict(NODE))
        s.graph_db.add_node.assert_not_called()
        s._count_nodes.assert_not_awaited()
        assert s.stats["errors"] == 1 and s.stats["nodes_processed"] == 0
        assert s._processing == set()

    async def test_marker_lookup_error_writes_nothing(self):
        arango = _Arango()
        await arango.service()._handle_node_update(dict(NODE))
        s2 = arango.service()
        s2.graph_db.node_ingest_incomplete = Mock(side_effect=RuntimeError("down"))
        await s2._handle_node_update(dict(NODE))
        s2.graph_db.add_edges_bulk.assert_not_called()
        s2._count_nodes.assert_not_awaited()
        assert s2.stats["errors"] == 1

    async def test_part_is_buffered_on_a_lookup_error(self):
        s = _service()
        s.graph_db.node_exists = Mock(side_effect=RuntimeError("arango down"))
        await s._handle_image_upload(dict(IMAGE))
        s.image_db.store_image.assert_not_called()
        assert ("r1", 7) in s.image_buffer and s.stats["errors"] == 1

    async def test_a_hit_on_another_candidate_beats_an_error(self):
        arango = _Arango()
        await arango.service()._handle_node_update(dict(NODE))
        gid = next(iter(arango.nodes))
        s2 = arango.service()
        s2.graph_db.node_exists = Mock(side_effect=[RuntimeError("blip"), True])
        s2._robot_runs["r1"] = "R"  # candidates: with R (errors), without (found)
        await s2._handle_image_upload(dict(IMAGE))
        assert s2.image_db.store_image.call_args.kwargs["node_id"] == gid

    def test_node_exists_raises_on_a_failed_lookup_but_not_on_absence(self):
        from packages.topomap_dbs.graph_db.server import GraphDatabaseService
        db = GraphDatabaseService.__new__(GraphDatabaseService)
        db.logger = MagicMock()
        db.db = MagicMock()
        db.db.has_collection.return_value = False
        assert db.node_exists("m", "n") is False
        db.db.has_collection.side_effect = RuntimeError("down")
        with pytest.raises(RuntimeError):
            db.node_exists("m", "n")


class TestStaleRunParts:
    async def test_buffered_part_of_another_run_is_discarded_not_attached(self):
        s = _unguarded(_service())
        await s._handle_image_upload({**IMAGE, "run_id": "A"})  # no node yet: buffered
        assert ("r1", 7) in s.image_buffer
        await s._handle_node_update({**NODE, "run_id": "B"})
        s.image_db.store_image.assert_not_called()
        assert s.image_buffer == {}

    async def test_parts_without_a_run_or_of_the_same_run_are_attached(self):
        s = _service()
        await s._handle_image_upload({**IMAGE, "camera_name": "a", "run_id": "B"})
        await s._handle_image_upload({**IMAGE, "camera_name": "b"})
        await s._handle_node_update({**NODE, "run_id": "B"})
        assert s.image_db.store_image.call_count == 2

    async def test_depth_and_costmap_of_another_run_are_discarded_on_a_duplicate(self):
        arango = _Arango()
        await arango.service()._handle_node_update({**NODE, "run_id": "B"})
        s2 = arango.service()
        s2.graph_db.node_exists = Mock(return_value=False)  # the parts arrive first ...
        await s2._handle_depth_upload({**DEPTH, "run_id": "A"})
        await s2._handle_costmap_upload({**COSTMAP, "run_id": "A"})
        s2.graph_db.node_exists = Mock(side_effect=lambda _m, g: g in arango.nodes)
        await s2._handle_node_update({**NODE, "run_id": "B"})  # ... then the resend
        s2.image_db.store_depth.assert_not_called()
        s2.image_db.store_costmap.assert_not_called()
        assert s2.depth_buffer == {} and s2.costmap_buffer == {}

    def test_take_filters_on_run_only_when_both_carry_one(self):
        from packages.services.graph_builder.upload_buffer import NodeUploadBuffer
        buf = NodeUploadBuffer("image", {"n": 0}, "n")
        for sub, run in (("a", "A"), ("b", "B"), ("c", None)):
            buf.put(("r", 1), sub, {"data": "x", "run_id": run})
        taken = buf.take(("r", 1), 60, run_id="B")
        assert sorted(e["run_id"] or "" for e in taken) == ["", "B"]
        buf.put(("r", 1), "a", {"data": "x", "run_id": "A"})
        assert len(buf.take(("r", 1), 60)) == 1  # a node without a run takes everything


class TestClaimLifecycle:
    async def test_repair_that_resolves_to_another_candidate_releases_its_own_claim(self):
        import time
        arango = _Arango()
        s = arango.service()
        s.graph_db.add_edges_bulk = Mock(side_effect=RuntimeError("arango down"))
        await s._handle_node_update({**NODE, "run_id": "R"})  # stored with R, edges failed
        s2 = arango.service()  # restart; the resend carries no run_id
        await s2._handle_node_update(dict(NODE))
        s2._count_nodes.assert_awaited_once()  # repaired (under the R id)
        assert s2._processing == set() and s2._claim_events == {}
        # a later resend, caches gone, does not wait on a leaked claim
        s2._complete_cache = NodeIdCache(10)
        s2._node_cache = NodeIdCache(10)
        s2._stored_nodes.clear()
        s2._incomplete.clear()
        s2.claim_wait_s = 3
        t0 = time.monotonic()
        await s2._handle_node_update(dict(NODE))
        assert time.monotonic() - t0 < 1 and s2.stats["nodes_duplicate"] == 1

    async def test_release_is_idempotent(self):
        s = _service()
        s._release_claim(("yard", "x"))
        s._processing.add(("yard", "x"))
        s._release_claims([("yard", "x"), ("yard", "x")])
        assert s._processing == set()

    async def test_cancelled_handler_releases_the_claim_when_the_thread_finishes(self):
        import asyncio
        import threading
        arango = _Arango()
        s = arango.service()
        started, gate = threading.Event(), threading.Event()
        real = s.graph_db.add_node.side_effect

        def add_node(*a, **kw):
            started.set()
            gate.wait(5)
            return real(*a, **kw)
        s.graph_db.add_node.side_effect = add_node
        task = asyncio.ensure_future(s._handle_node_update(dict(NODE)))
        while not started.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert s._processing  # the thread still holds it
        gate.set()
        for _ in range(500):
            if not s._processing:
                break
            await asyncio.sleep(0.01)
        assert s._processing == set() and s._claim_events == {}

    async def test_a_burst_of_resends_does_not_starve_the_holder(self):
        import asyncio
        import threading
        arango = _Arango()
        s = arango.service()
        s.claim_wait_s = 10
        started, gate = threading.Event(), threading.Event()
        real = s.graph_db.add_node.side_effect

        def add_node(*a, **kw):
            started.set()
            gate.wait(10)
            return real(*a, **kw)
        s.graph_db.add_node.side_effect = add_node
        holder = asyncio.ensure_future(s._handle_node_update(dict(NODE)))
        while not started.is_set():
            await asyncio.sleep(0.01)
        loop = asyncio.get_running_loop()
        n = gb_server.NODE_CLAIM_WAIT_THREADS * 3
        resends = [loop.run_in_executor(
            s._topology_executor,
            lambda: s._process_topology(dict(NODE), map_id="yard", session_id="s1"))
            for _ in range(n)]
        await asyncio.sleep(0.2)  # the waiters are in
        gate.set()
        await asyncio.wait_for(holder, 5)
        results = await asyncio.wait_for(asyncio.gather(*resends), 5)
        assert all(r is None for r in results)
        assert s.graph_db.add_node.call_count == 1 and s.stats["nodes_duplicate"] == n
        s.graph_db.mark_node_complete.assert_called_once()
        assert s._processing == set()

    def test_close_stops_the_pool(self):
        s = _service()
        s.close()
        with pytest.raises(RuntimeError):
            s._topology_executor.submit(lambda: None)


class TestPublishOnce:
    async def test_repair_after_a_failed_count_does_not_publish_twice(self):
        arango = _Arango()
        s = arango.service()
        mission = Mock()
        s._active_mission = AsyncMock(return_value=(mission, True))
        s._log_mission_waypoint = AsyncMock()
        s._count_nodes = AsyncMock(return_value=False)
        await s._handle_node_update(dict(NODE))
        s._count_nodes = AsyncMock(return_value=True)
        await s._handle_node_update(dict(NODE))  # repair: counted now
        s._count_nodes.assert_awaited_once()
        s._publish_node_update.assert_awaited_once()
        s._log_mission_waypoint.assert_awaited_once()

    async def test_repair_after_failed_edges_publishes_once(self):
        arango = _Arango()
        s = arango.service()
        s.graph_db.add_edges_bulk = Mock(side_effect=RuntimeError("arango down"))
        await s._handle_node_update(dict(NODE))
        s._publish_node_update.assert_not_awaited()
        s.graph_db.add_edges_bulk = Mock(return_value=0)
        await s._handle_node_update(dict(NODE))
        s._publish_node_update.assert_awaited_once()


class TestManualPathAndProbe:
    def test_manual_node_has_no_incomplete_marker(self):
        arango = _Arango()
        s = arango.service()
        assert s._process_topology(dict(NODE), map_id="yard") is not None
        assert "ingest_complete" not in next(iter(arango.nodes.values()))
        assert s._incomplete == set() and s._processing == set()

    def test_failed_run_lookup_is_not_retried_at_once(self):
        s = _service()
        s.graph_db.last_run_id = Mock(side_effect=RuntimeError("down"))
        s._probe_run("r1", "s1", "yard")
        s._probe_run("r1", "s1", "yard")
        assert s.graph_db.last_run_id.call_count == 1
        s._run_probe_retry["r1"] = 0.0
        s._probe_run("r1", "s1", "yard")
        assert s.graph_db.last_run_id.call_count == 2
