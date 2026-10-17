"""graph-builder's robot ingest contract (phase 2, packages/services/graph_builder/acks.py):

- a per-node ack on `robot/<robot>/ingest_ack` once the state is durable, with the parts manifest,
  a final ack when the parts are in (or a timeout ack), rejections by reason, duplicates answered
  from storage;
- the retained `ingest_state` high-water mark (rate limited, seeded from the broker's retained
  message), the robot's `spool_state`;
- the run_id session guard (`run_id_mismatch`, before anything is touched).
"""
import asyncio
import itertools
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

import pytest  # noqa: E402

from packages.events.codes import EventCode  # noqa: E402
from packages.services.graph_builder import acks, ingest  # noqa: E402
from packages.services.graph_builder import server as gb_server  # noqa: E402
from tests.unit.test_costmap_ingest import COSTMAP  # noqa: E402
from tests.unit.test_graph_builder_resend import _Arango, _service  # noqa: E402
from tests.unit.test_map_reconstruction_ingest import DEPTH  # noqa: E402
from tests.unit.test_maps_m2 import ENU_DATUM, IMAGE, NODE, _row  # noqa: E402

pytestmark = pytest.mark.unit

IMG = ("image", "left")
DEP = ("depth", "left")
CST = ("costmap", "occupancy")
MANIFEST = [{"kind": k, "sub": s} for k, s in (IMG, DEP, CST)]
PART_PAYLOAD = {IMG: IMAGE, DEP: DEPTH, CST: COSTMAP}
HANDLER = {"image": "_handle_image_upload", "depth": "_handle_depth_upload",
           "costmap": "_handle_costmap_upload"}


class FakeMQTT:
    """The shared MQTTClient as graph-builder uses it: publish() is recorded, never blocks."""

    connected = True

    def __init__(self, rc=0, raises=None):
        self.rc, self.raises = rc, raises
        self.messages = []
        self.log = []

    def publish(self, topic, payload, qos=0, retain=False):
        self.log.append("publish")
        if self.raises:
            raise self.raises
        self.messages.append(SimpleNamespace(topic=topic, body=json.loads(payload), qos=qos,
                                             retain=retain))
        return SimpleNamespace(rc=self.rc)

    def acks(self, robot="r1"):
        return [m for m in self.messages if m.topic == f"robot/{robot}/ingest_ack"]

    def states(self, robot="r1"):
        return [m for m in self.messages if m.topic == f"robot/{robot}/ingest_state"]


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


KEEP = object()


def svc(row=KEEP, **kw):
    """A graph-builder (the resend tests' fakes) publishing to a FakeMQTT, on a fake clock."""
    s = _service(**kw)
    s.mqtt_client = FakeMQTT()
    s.clock = Clock()
    s.acks._clock = s.clock
    if row is not KEEP:
        _use_row(s, row)
    return s


def _use_row(s, row):
    async def fetch(_robot):
        return row
    s.sessions = ingest.SessionResolver(fetch, ttl=0)


def manifest(*parts):
    return [{"kind": k, "sub": s} for k, s in parts]


async def send_part(s, part, **extra):
    await getattr(s, HANDLER[part[0]])({**PART_PAYLOAD[part], **extra})


def last(s):
    return s.mqtt_client.acks()[-1].body


def ok_row(**kw):
    return _row(**kw)


def row12(aligned=True, **kw):
    """_row with the 12-column shape (purpose, aligned)."""
    return _row(**kw) + (None, None, "mapping", aligned)


# --- the node ack -----------------------------------------------------------------------------

class TestNodeAck:
    async def test_accepted_node_without_manifest(self):
        s = svc()
        await s._handle_node_update(dict(NODE))
        [m] = s.mqtt_client.messages
        assert (m.topic, m.qos, m.retain) == ("robot/r1/ingest_ack", 1, False)
        b = m.body
        assert b["v"] == 1 and b["robot_name"] == "r1" and b["run_id"] is None
        assert b["session_node_id"] == 7 and b["status"] == "accepted" and b["reason"] is None
        assert b["resend"] == "none" and b["complete"] is None
        assert b["parts_stored"] == [] and b["parts_missing"] == []
        assert b["session_id"] == "s1" and isinstance(b["ts"], int)
        assert s.stats["acks_sent"] == 1
        assert s.mqtt_client.states() == []  # complete=null does not advance the mark

    async def test_unknown_extra_fields_are_tolerated(self):
        s = svc()
        await s._handle_node_update({**NODE, "future": {"x": 1}, "parts": [
            {"kind": "image", "sub": "left", "extra": 1}, {"kind": "lidar", "sub": "x"},
            "junk", {"kind": "depth"}]})
        assert last(s)["parts_missing"] == [{"kind": "image", "sub": "left"}]

    async def test_run_id_is_echoed(self):
        s = svc()
        await s._handle_node_update({**NODE, "run_id": "run-1", "parts": []})
        assert last(s)["run_id"] == "run-1" and last(s)["complete"] is True

    async def test_empty_manifest_is_complete_at_once(self):
        s = svc()
        await s._handle_node_update({**NODE, "parts": []})
        [m] = s.mqtt_client.acks()
        assert m.body["complete"] is True and m.body["resend"] == "none"

    async def test_manifest_first_ack_is_incomplete_with_everything_missing(self):
        s = svc()
        await s._handle_node_update({**NODE, "parts": MANIFEST})
        b = last(s)
        assert b["status"] == "accepted" and b["complete"] is False and b["resend"] == "none"
        assert b["parts_stored"] == []
        assert {(p["kind"], p["sub"]) for p in b["parts_missing"]} == {IMG, DEP, CST}

    @pytest.mark.parametrize("order", list(itertools.permutations([IMG, DEP, CST])))
    async def test_parts_after_the_node_in_any_order_end_in_one_final_ack(self, order):
        s = svc()
        await s._handle_node_update({**NODE, "run_id": "R", "parts": MANIFEST})
        for part in order:
            await send_part(s, part, run_id="R")
        acks_ = [m.body for m in s.mqtt_client.acks()]
        assert [a["complete"] for a in acks_] == [False, True]
        assert acks_[1]["status"] == "accepted" and acks_[1]["resend"] == "none"
        assert {(p["kind"], p["sub"]) for p in acks_[1]["parts_stored"]} == {IMG, DEP, CST}
        assert acks_[1]["parts_missing"] == [] and acks_[1]["run_id"] == "R"

    async def test_no_final_ack_before_the_last_part(self):
        s = svc()
        await s._handle_node_update({**NODE, "parts": MANIFEST})
        await send_part(s, IMG)
        await send_part(s, DEP)
        assert len(s.mqtt_client.acks()) == 1
        await send_part(s, CST)
        assert len(s.mqtt_client.acks()) == 2

    async def test_parts_before_the_node_are_attached_and_acked_once(self):
        s = svc()
        for part in (IMG, DEP, CST):
            await send_part(s, part)  # buffered: the node is unknown
        assert s.mqtt_client.acks() == []  # a part alone is not acked when stored
        await s._handle_node_update({**NODE, "parts": MANIFEST})
        [m] = s.mqtt_client.acks()
        assert m.body["complete"] is True
        assert s.image_db.store_image.call_count == 1

    async def test_some_parts_before_and_some_after(self):
        s = svc()
        await send_part(s, DEP)
        await s._handle_node_update({**NODE, "parts": MANIFEST})
        assert {(p["kind"], p["sub"]) for p in last(s)["parts_missing"]} == {IMG, CST}
        await send_part(s, IMG)
        await send_part(s, CST)
        assert [m.body["complete"] for m in s.mqtt_client.acks()] == [False, True]

    async def test_resent_part_after_the_final_ack_is_not_acked_again(self):
        s = svc()
        await s._handle_node_update({**NODE, "parts": manifest(IMG)})
        await send_part(s, IMG)
        await send_part(s, IMG)
        assert [m.body["complete"] for m in s.mqtt_client.acks()] == [False, True]


class TestTimeout:
    async def test_missing_parts_are_asked_for_after_the_timeout(self):
        s = svc()
        await s._handle_node_update({**NODE, "parts": MANIFEST})
        await send_part(s, IMG)
        s.acks.flush()
        assert len(s.mqtt_client.acks()) == 1  # not yet
        s.clock.t += 121
        s.acks.flush()
        b = last(s)
        assert b["complete"] is False and b["resend"] == "parts" and b["status"] == "accepted"
        assert {(p["kind"], p["sub"]) for p in b["parts_missing"]} == {DEP, CST}
        assert [(p["kind"], p["sub"]) for p in b["parts_stored"]] == [IMG]
        assert s.stats["ack_timeouts"] == 1

    async def test_resent_parts_after_a_timeout_still_end_in_the_final_ack(self):
        s = svc()
        await s._handle_node_update({**NODE, "parts": manifest(IMG, DEP)})
        await send_part(s, IMG)
        s.clock.t += 121
        s.acks.flush()
        await send_part(s, DEP)
        assert [m.body["complete"] for m in s.mqtt_client.acks()] == [False, False, True]

    async def test_timeouts_are_bounded(self):
        s = svc()
        await s._handle_node_update({**NODE, "parts": manifest(IMG)})
        for _ in range(acks.MAX_TIMEOUT_ACKS + 2):
            s.clock.t += 121
            s.acks.flush()
        assert len(s.mqtt_client.acks()) == 1 + acks.MAX_TIMEOUT_ACKS
        assert s.acks.snapshot()["ack_trackers"] == 0

    async def test_timeout_is_configurable(self):
        s = svc()
        s.acks.part_timeout_s = 5
        await s._handle_node_update({**NODE, "parts": manifest(IMG)})
        s.clock.t += 6
        s.acks.flush()
        assert last(s)["resend"] == "parts"


# --- duplicates are answered from storage ----------------------------------------------------

class _Storage:
    """ArangoDB + MinIO that outlive a graph-builder 'restart'."""

    def __init__(self):
        self.arango = _Arango()
        self.images = set()   # (node id, camera)
        self.records = {}     # node id -> {"depth": {...}, "costmap": {...}}

    def service(self):
        s = self.arango.service()
        s.mqtt_client = FakeMQTT()
        s.clock = Clock()
        s.acks._clock = s.clock

        def store_image(image_data, image_id, node_id, map_id, **kw):
            self.images.add((node_id, image_id))
            return True
        s.image_db.store_image = Mock(side_effect=store_image)
        s.image_db.list_node_images = Mock(side_effect=lambda node, _m: [
            i for n, i in self.images if n == node])

        def setter(kind):
            def set_record(_m, node, sub, record):
                self.records.setdefault(node, {}).setdefault(kind, {})[sub] = record
                return True
            return set_record
        s.graph_db.set_node_depth = Mock(side_effect=setter("depth"))
        s.graph_db.set_node_costmap = Mock(side_effect=setter("costmap"))
        s.graph_db.get_node = Mock(side_effect=lambda _m, node: {
            **self.arango.nodes.get(node, {}), **self.records.get(node, {})})
        return s


class TestDuplicateFromStorage:
    async def test_restarted_duplicate_reports_stored_and_missing_parts(self):
        st = _Storage()
        s1 = st.service()
        await s1._handle_node_update({**NODE, "run_id": "R", "parts": MANIFEST})
        await send_part(s1, IMG, run_id="R")
        await send_part(s1, DEP, run_id="R")  # the costmap never came
        s2 = st.service()                     # graph-builder restarted: trackers are gone
        await s2._handle_node_update({**NODE, "run_id": "R", "parts": MANIFEST})
        b = last(s2)
        assert b["status"] == "duplicate" and b["complete"] is False and b["resend"] == "parts"
        assert {(p["kind"], p["sub"]) for p in b["parts_stored"]} == {IMG, DEP}
        assert b["parts_missing"] == [{"kind": "costmap", "sub": "occupancy"}]
        s2.graph_db.add_node.assert_not_called()
        # the resent costmap completes the bundle
        await send_part(s2, CST, run_id="R")
        b = last(s2)
        assert b["status"] == "duplicate" and b["complete"] is True and b["resend"] == "none"

    async def test_restarted_duplicate_that_is_complete_resends_nothing(self):
        st = _Storage()
        s1 = st.service()
        await s1._handle_node_update({**NODE, "parts": MANIFEST})
        for part in (IMG, DEP, CST):
            await send_part(s1, part)
        s2 = st.service()
        await s2._handle_node_update({**NODE, "parts": MANIFEST})
        [m] = s2.mqtt_client.acks()
        assert m.body["status"] == "duplicate" and m.body["complete"] is True
        assert m.body["resend"] == "none" and m.body["parts_missing"] == []
        assert s2.mqtt_client.states()[-1].body["highest_complete_id"] == 7

    async def test_duplicate_in_memory_without_manifest(self):
        s = svc()
        await s._handle_node_update(dict(NODE))
        s.clock.t += 5
        await s._handle_node_update(dict(NODE))
        assert [m.body["status"] for m in s.mqtt_client.acks()] == ["accepted", "duplicate"]
        assert last(s)["complete"] is None

    async def test_storage_read_failures_count_parts_as_missing(self):
        st = _Storage()
        s1 = st.service()
        await s1._handle_node_update({**NODE, "parts": manifest(IMG, DEP)})
        await send_part(s1, IMG)
        await send_part(s1, DEP)
        s2 = st.service()
        s2.graph_db.get_node = Mock(side_effect=RuntimeError("down"))
        s2.image_db.list_node_images = Mock(side_effect=RuntimeError("down"))
        await s2._handle_node_update({**NODE, "parts": manifest(IMG, DEP)})
        b = last(s2)
        assert b["complete"] is False and len(b["parts_missing"]) == 2 and b["resend"] == "parts"

    async def test_repaired_node_reads_its_parts_from_storage(self):
        st = _Storage()
        s1 = st.service()
        s1.graph_db.add_edges_bulk = Mock(side_effect=RuntimeError("down"))
        await s1._handle_node_update({**NODE, "parts": manifest(IMG)})
        assert last(s1)["status"] == "rejected" and last(s1)["reason"] == "store_failed"
        await send_part(s1, IMG)
        s2 = st.service()
        await s2._handle_node_update({**NODE, "parts": manifest(IMG)})
        b = last(s2)
        assert b["status"] == "accepted" and b["complete"] is True


# --- rejections --------------------------------------------------------------------------------

ENU_MOVED = {**ENU_DATUM, "latitude": 47.5}
ROWS = {
    "no_session": None,
    "map_deleting": _row(lifecycle="DELETING"),
    "map_missing": _row(lifecycle=None),
    "datum_changed": _row(sdatum=ENU_DATUM, rdatum=ENU_MOVED),
    "session_paused": _row(paused=True),
    "session_unplaced": row12(aligned=False),
}
TABLE = {  # reason -> the resend a rejected NODE gets
    "no_session": "none", "map_deleting": "none", "map_missing": "none",
    "datum_changed": "none", "run_id_mismatch": "none", "invalid_payload": "none",
    "session_paused": "node", "session_unplaced": "node", "lookup_failed": "node",
    "buffer_full": "node", "store_failed": "node"}


def _failing_lookup(s):
    async def fetch(_robot):
        raise RuntimeError("postgres down")
    s.sessions = ingest.SessionResolver(fetch, ttl=0)


class TestRejections:
    def test_the_reason_table(self):
        assert acks.TERMINAL_REASONS == {r for r, v in TABLE.items() if v == "none"}
        assert acks.RETRYABLE_REASONS == {r for r, v in TABLE.items() if v == "node"}

    @pytest.mark.parametrize("reason", sorted(ROWS) + ["lookup_failed"])
    async def test_rejected_node_by_session_reason(self, reason):
        s = svc(ROWS[reason] if reason in ROWS else KEEP)
        if reason == "lookup_failed":
            _failing_lookup(s)
        await s._handle_node_update({**NODE, "run_id": "R", "parts": manifest(IMG)})
        [m] = s.mqtt_client.acks()
        b = m.body
        assert b["status"] == "rejected" and b["reason"] == reason
        assert b["resend"] == TABLE[reason] and b["run_id"] == "R" and b["complete"] is False
        assert b["parts_missing"] == [{"kind": "image", "sub": "left"}]
        s.graph_db.add_node.assert_not_called()

    async def test_rejection_is_still_reported_as_an_event(self):
        s = svc(ROWS["session_paused"])
        await s._handle_node_update(dict(NODE))
        assert s._write_event.await_args.args[0].code == EventCode.MAP_INGEST_REJECTED

    async def test_store_failure_is_a_retryable_rejection_not_an_accept(self):
        s = svc()
        s.graph_db.add_node = Mock(return_value=False)
        await s._handle_node_update(dict(NODE))
        [m] = s.mqtt_client.acks()
        assert m.body["status"] == "rejected" and m.body["reason"] == "store_failed"
        assert m.body["resend"] == "node"

    async def test_failed_count_is_a_retryable_rejection(self):
        s = svc()
        s._count_nodes = AsyncMock(return_value=False)
        await s._handle_node_update(dict(NODE))
        assert last(s)["reason"] == "store_failed" and last(s)["resend"] == "node"
        assert len(s.mqtt_client.acks()) == 1

    async def test_unknown_existence_is_lookup_failed(self):
        s = svc()
        s.graph_db.node_exists = Mock(side_effect=RuntimeError("arango down"))
        await s._handle_node_update(dict(NODE))
        assert last(s)["reason"] == "lookup_failed" and last(s)["resend"] == "node"
        s.graph_db.add_node.assert_not_called()

    async def test_node_without_position_is_invalid_payload(self):
        s = svc()
        bad = {k: v for k, v in NODE.items() if k != "x"}
        await s._handle_node_update(bad)
        assert last(s)["reason"] == "invalid_payload" and last(s)["resend"] == "none"

    async def test_node_that_cannot_be_named_is_only_counted(self):
        s = svc()
        await s._handle_node_update({k: v for k, v in NODE.items() if k != "session_node_id"})
        await s._handle_node_update({k: v for k, v in NODE.items() if k != "robot_name"})
        assert s.mqtt_client.messages == [] and s.stats["acks_unackable"] == 2

    async def test_running_mission_without_register_map_is_a_terminal_drop(self):
        s = svc()
        s._active_mission = AsyncMock(return_value=(SimpleNamespace(name="m"), False))
        s._log_mission_waypoint = AsyncMock()
        await s._handle_node_update(dict(NODE))
        assert last(s)["status"] == "rejected" and last(s)["resend"] == "none"

    @pytest.mark.parametrize("part", [IMG, DEP, CST])
    @pytest.mark.parametrize("reason", sorted(ROWS) + ["lookup_failed"])
    async def test_rejected_part_without_a_tracker(self, part, reason):
        s = svc(ROWS[reason] if reason in ROWS else KEEP)
        if reason == "lookup_failed":
            _failing_lookup(s)
        await send_part(s, part, run_id="R9")
        [m] = s.mqtt_client.acks()
        b = m.body
        assert b["status"] == "rejected" and b["reason"] == reason
        assert b["resend"] == ("parts" if TABLE[reason] == "node" else "none")
        assert b["parts_missing"] == [{"kind": part[0], "sub": part[1]}]
        assert b["run_id"] == "R9" and b["session_node_id"] == 7 and b["complete"] is False

    async def test_buffer_full_part_is_a_retryable_part_rejection(self):
        s = svc()
        s.image_buffer.max_entries = 0
        await send_part(s, IMG)
        b = last(s)
        assert b["reason"] == "buffer_full" and b["resend"] == "parts"

    async def test_part_store_failure_is_retryable(self):
        s = svc()
        await s._handle_node_update(dict(NODE))
        s.image_db.store_image = Mock(return_value=False)
        await send_part(s, IMG)
        assert last(s)["reason"] == "store_failed" and last(s)["resend"] == "parts"

    async def test_malformed_part_is_invalid_payload_terminal(self):
        s = svc()
        await s._handle_depth_upload({**DEPTH, "depth_data": "not a png"})
        b = last(s)
        assert b["status"] == "rejected" and b["reason"] == "invalid_payload"
        assert b["resend"] == "none" and b["parts_missing"] == [{"kind": "depth", "sub": "left"}]
        await s._handle_image_upload({k: v for k, v in IMAGE.items() if k != "image_data"})
        assert last(s)["reason"] == "invalid_payload"

    async def test_part_that_cannot_be_named_is_only_counted(self):
        s = svc()
        await s._handle_depth_upload({k: v for k, v in DEPTH.items() if k != "robot_name"})
        assert s.mqtt_client.messages == [] and s.stats["acks_unackable"] == 1

    async def test_retryable_part_rejection_with_a_tracker_waits_for_the_bundle(self):
        box = {"row": _row()}
        s = svc()

        async def fetch(_r):
            return box["row"]
        s.sessions = ingest.SessionResolver(fetch, ttl=0)
        await s._handle_node_update({**NODE, "parts": manifest(IMG, DEP)})
        box["row"] = _row(paused=True)
        await send_part(s, DEP)           # rejected while paused: recorded, no ack of its own
        assert len(s.mqtt_client.acks()) == 1
        box["row"] = _row()
        await send_part(s, IMG)           # every part is now stored or reported: final ack
        b = last(s)
        assert b["status"] == "accepted" and b["complete"] is False and b["resend"] == "parts"
        assert b["parts_missing"] == [{"kind": "depth", "sub": "left"}]
        assert b["reason"] == "session_paused"
        await send_part(s, DEP)           # the resent part completes it
        assert last(s)["complete"] is True and len(s.mqtt_client.acks()) == 3

    async def test_terminal_part_rejection_with_a_tracker_is_a_final_rejection(self):
        box = {"row": _row()}
        s = svc()

        async def fetch(_r):
            return box["row"]
        s.sessions = ingest.SessionResolver(fetch, ttl=0)
        await s._handle_node_update({**NODE, "parts": manifest(IMG, DEP)})
        await send_part(s, IMG)
        box["row"] = _row(lifecycle="DELETING")
        await send_part(s, DEP)
        b = last(s)
        assert b["status"] == "rejected" and b["reason"] == "map_deleting"
        assert b["resend"] == "none" and b["complete"] is False
        assert s.acks.snapshot()["ack_trackers"] == 0
        assert s.mqtt_client.states() == []


# --- publishing ---------------------------------------------------------------------------------

class TestPublishing:
    @pytest.mark.parametrize("name", ["a/b", "a+b", "a#", "a/#"])
    async def test_robot_name_that_is_not_a_topic_level_is_not_acked(self, name):
        s = svc()
        await s._handle_node_update({**NODE, "robot_name": name})
        assert s.mqtt_client.messages == [] and s.stats["acks_invalid_robot"] == 1
        s.graph_db.add_node.assert_called_once()  # ingest itself went on

    def test_topic_level_validation(self):
        assert acks.valid_topic_level("robot-1")
        assert not any(acks.valid_topic_level(n) for n in ("", None, 5, "a/b", "+", "#", "a\x00"))

    async def test_publish_exception_never_reaches_ingest(self):
        s = svc()
        s.mqtt_client = FakeMQTT(raises=RuntimeError("paho exploded"))
        await s._handle_node_update(dict(NODE))
        assert s.stats["acks_failed"] == 1 and s.stats["nodes_processed"] == 1
        await send_part(s, IMG)

    async def test_internal_ack_error_never_reaches_ingest(self):
        s = svc()
        s.acks.node_ok = Mock(side_effect=RuntimeError("bug"))
        await s._handle_node_update(dict(NODE))
        assert s.stats["acks_failed"] == 1 and s.stats["nodes_processed"] == 1

    async def test_full_queue_is_counted_as_dropped(self):
        s = svc()
        s.mqtt_client = FakeMQTT(rc=15)  # MQTT_ERR_QUEUE_SIZE
        await s._handle_node_update(dict(NODE))
        assert s.stats["acks_dropped"] == 1 and s.stats["acks_sent"] == 0

    async def test_disconnected_qos1_publish_is_queued_not_failed(self):
        s = svc()
        s.mqtt_client = FakeMQTT(rc=4)  # MQTT_ERR_NO_CONN: paho keeps it for the reconnect
        await s._handle_node_update(dict(NODE))
        assert s.stats["acks_sent"] == 1 and s.stats["acks_failed"] == 0

    async def test_other_rc_is_a_failure(self):
        s = svc()
        s.mqtt_client = FakeMQTT(rc=1)
        await s._handle_node_update(dict(NODE))
        assert s.stats["acks_failed"] == 1

    async def test_no_client_is_dropped(self):
        s = svc()
        s.mqtt_client = None
        await s._handle_node_update(dict(NODE))
        assert s.stats["acks_dropped"] == 1 and s.stats["nodes_processed"] == 1

    async def test_ack_follows_the_durable_state(self):
        s = svc()
        log = s.mqtt_client.log
        s.graph_db.add_node = Mock(side_effect=lambda **kw: log.append("add_node") or True)
        s.graph_db.mark_node_complete = Mock(
            side_effect=lambda *a: log.append("complete") or True)
        s.image_db.store_image = Mock(side_effect=lambda **kw: log.append("store_image") or True)
        await s._handle_node_update({**NODE, "parts": manifest(IMG)})
        await send_part(s, IMG)
        assert log == ["add_node", "complete", "publish", "store_image", "publish", "publish"]
        # (the last publish is the retained ingest_state after the final ack)

    async def test_identical_acks_are_coalesced_within_a_second(self):
        s = svc(ROWS["session_paused"])
        for _ in range(5):
            await s._handle_node_update(dict(NODE))
        assert len(s.mqtt_client.acks()) == 1 and s.stats["acks_coalesced"] == 4
        s.clock.t += 1.1
        await s._handle_node_update(dict(NODE))
        assert len(s.mqtt_client.acks()) == 2

    async def test_different_parts_of_a_burst_are_not_coalesced(self):
        s = svc(ROWS["session_paused"])
        await send_part(s, IMG)
        await send_part(s, DEP)
        assert len(s.mqtt_client.acks()) == 2

    def test_connect_caps_the_outgoing_queue(self):
        s = svc()
        with patch.object(gb_server, "MQTTClient") as cls, \
                patch.object(gb_server, "INGEST_MQTT_MAX_QUEUED", 123):
            assert s.connect_mqtt()
        cls.return_value.client.max_queued_messages_set.assert_called_once_with(123)


# --- ingest_state -------------------------------------------------------------------------------

async def complete_node(s, sid, **extra):
    await s._handle_node_update({**NODE, "session_node_id": sid, "parts": [], **extra})


class TestIngestState:
    async def test_advances_monotonically_and_is_rate_limited(self):
        s = svc()
        await complete_node(s, 1, run_id="A")
        [m] = s.mqtt_client.states()
        assert (m.topic, m.qos, m.retain) == ("robot/r1/ingest_state", 1, True)
        assert m.body["v"] == 1 and m.body["robot_name"] == "r1" and m.body["run_id"] == "A"
        assert m.body["highest_complete_id"] == 1 and isinstance(m.body["ts"], int)
        await complete_node(s, 2, run_id="A")
        await complete_node(s, 5, run_id="A")
        assert len(s.mqtt_client.states()) == 1  # held back within the interval
        s.clock.t += 0.5
        s.acks.flush()
        assert len(s.mqtt_client.states()) == 1
        s.clock.t += 0.6
        s.acks.flush()  # the last value is never lost
        assert [x.body["highest_complete_id"] for x in s.mqtt_client.states()] == [1, 5]
        s.clock.t += 5
        s.acks.flush()
        assert len(s.mqtt_client.states()) == 2  # nothing new, nothing published
        await complete_node(s, 3, run_id="A")  # a lower id (a resend) never lowers it
        s.clock.t += 5
        s.acks.flush()
        assert len(s.mqtt_client.states()) == 2

    async def test_gaps_are_fine(self):
        s = svc()
        await complete_node(s, 1, run_id="A")
        s.clock.t += 2
        await complete_node(s, 10, run_id="A")
        assert s.mqtt_client.states()[-1].body["highest_complete_id"] == 10

    async def test_run_change_resets_to_the_new_runs_first_complete_id(self):
        s = svc()
        await complete_node(s, 50, run_id="A")
        s.clock.t += 2
        # a new run within the same cloud session is refused by the guard: another session
        _use_row(s, _row(sid="s2"))
        await complete_node(s, 1, run_id="B")
        b = s.mqtt_client.states()[-1].body
        assert b["run_id"] == "B" and b["highest_complete_id"] == 1
        s.clock.t += 2
        await complete_node(s, 2, run_id="B")
        assert s.mqtt_client.states()[-1].body["highest_complete_id"] == 2

    async def test_only_complete_bundles_advance_it(self):
        s = svc()
        await s._handle_node_update({**NODE, "parts": MANIFEST})   # parts pending
        await s._handle_node_update({**NODE, "session_node_id": 8})  # no manifest: null
        assert s.mqtt_client.states() == []
        await send_part(s, IMG)
        await send_part(s, DEP)
        assert s.mqtt_client.states() == []
        await send_part(s, CST)
        assert s.mqtt_client.states()[-1].body["highest_complete_id"] == 7

    async def test_rejection_does_not_advance_it(self):
        s = svc(ROWS["session_paused"])
        await complete_node(s, 4)
        assert s.mqtt_client.states() == []

    async def test_state_survives_a_restart_through_the_retained_message(self):
        s = svc()
        s._on_first_connect()
        msg = SimpleNamespace(topic="robot/r1/ingest_state", payload=json.dumps(
            {"v": 1, "robot_name": "r1", "run_id": "A", "highest_complete_id": 40,
             "ts": 1}).encode())
        s._on_ingest_state_message(None, None, msg)
        assert s.acks.snapshot()["ingest_state"] == {
            "r1": {"run_id": "A", "highest_complete_id": 40}}
        assert s.mqtt_client.states() == []  # seeding does not publish
        await complete_node(s, 12, run_id="A")  # below the mark: unchanged, nothing published
        assert s.mqtt_client.states() == []
        await complete_node(s, 41, run_id="A")
        assert s.mqtt_client.states()[-1].body["highest_complete_id"] == 41

    def test_seed_is_ignored_after_the_settle_window(self):
        s = svc()
        s._on_first_connect()
        s._seed_until = gb_server.time.monotonic() - 1
        s._on_ingest_state_message(None, None, SimpleNamespace(
            topic="robot/r1/ingest_state",
            payload=b'{"run_id": "A", "highest_complete_id": 40}'))
        assert s.acks.snapshot()["ingest_state"] == {}

    def test_seed_needs_the_first_connect(self):
        s = svc()
        s._on_ingest_state_message(None, None, SimpleNamespace(
            topic="robot/r1/ingest_state", payload=b'{"highest_complete_id": 40}'))
        assert s.acks.snapshot()["ingest_state"] == {}

    def _mgr(self):
        pub = FakeMQTT()
        clock = Clock()
        m = acks.AckManager(lambda t, b, q, r: pub.publish(t, b, q, r).rc, {}, clock=clock)
        return m, pub, clock

    @staticmethod
    def _done(m, sid, run):
        m.node_ok("r1", run, sid, acks.ACCEPTED, "s", [])

    def test_completion_before_seed_merges_same_run_higher_wins(self):
        m, pub, clock = self._mgr()
        self._done(m, 60, "A")
        assert m.seed_state("r1", {"run_id": "A", "highest_complete_id": 40}) is False
        assert m.snapshot()["ingest_state"]["r1"]["highest_complete_id"] == 60
        # a seeded value higher than the local one wins; the overwritten retained is restored
        m2, pub2, clock2 = self._mgr()
        self._done(m2, 10, "A")
        assert m2.seed_state("r1", {"run_id": "A", "highest_complete_id": 40}) is True
        assert m2.snapshot()["ingest_state"]["r1"]["highest_complete_id"] == 40
        clock2.t += 2
        m2.flush()
        assert pub2.states()[-1].body["highest_complete_id"] == 40

    def test_completion_before_seed_of_another_run_keeps_the_local_run(self):
        m, pub, clock = self._mgr()
        self._done(m, 3, "B")
        assert m.seed_state("r1", {"run_id": "A", "highest_complete_id": 500}) is False
        assert m.snapshot()["ingest_state"]["r1"] == {"run_id": "B", "highest_complete_id": 3}

    def test_seed_equal_to_local_is_not_republished(self):
        m, pub, clock = self._mgr()
        assert m.seed_state("r1", {"run_id": "A", "highest_complete_id": 40})
        self._done(m, 41, "A")
        n = len(pub.states())
        m.seed_state("r1", {"run_id": "A", "highest_complete_id": 41})
        clock.t += 5
        m.flush()
        assert len(pub.states()) == n

    def test_late_complete_of_a_superseded_run_does_not_flip_state(self):
        m, pub, clock = self._mgr()
        self._done(m, 500, "A")
        clock.t += 2
        self._done(m, 3, "B")
        clock.t += 2
        self._done(m, 100, "A")  # late, still acked
        clock.t += 2
        m.flush()
        assert m.snapshot()["ingest_state"]["r1"] == {"run_id": "B", "highest_complete_id": 3}
        assert [x.body["run_id"] for x in pub.states()] == ["A", "B"]
        assert [x.body["session_node_id"] for x in pub.acks()] == [500, 3, 100]
        self._done(m, 4, "B")
        assert m.snapshot()["ingest_state"]["r1"]["highest_complete_id"] == 4

    async def test_seed_does_not_override_a_newer_local_state(self):
        s = svc()
        s._on_first_connect()
        await complete_node(s, 3, run_id="A")
        s._on_ingest_state_message(None, None, SimpleNamespace(
            topic="robot/r1/ingest_state",
            payload=b'{"run_id": "A", "highest_complete_id": 2}'))
        assert s.acks.snapshot()["ingest_state"]["r1"]["highest_complete_id"] == 3

    @pytest.mark.parametrize("payload", [b"", b"nope", b"[]", b'{"highest_complete_id": "x"}',
                                         b'{"highest_complete_id": true}'])
    def test_bad_retained_payloads_are_ignored(self, payload):
        s = svc()
        s._on_first_connect()
        s._on_ingest_state_message(None, None, SimpleNamespace(
            topic="robot/r1/ingest_state", payload=payload))
        assert s.acks.snapshot()["ingest_state"] == {}


class TestSpoolState:
    def test_latest_per_robot_is_kept_and_exposed(self):
        s = svc()
        for depth in (3, 4):
            s._on_spool_state_message(None, None, SimpleNamespace(
                topic="robot/r1/spool_state", payload=json.dumps(
                    {"run_id": "A", "last_spooled_id": 9, "depth": depth}).encode()))
        s._on_spool_state_message(None, None, SimpleNamespace(
            topic="robot/r2/spool_state", payload=b'{"run_id": null, "depth": 0}'))
        spool = s.get_stats()["spool_state"]
        assert set(spool) == {"r1", "r2"}
        assert spool["r1"]["depth"] == 4 and spool["r1"]["last_spooled_id"] == 9
        assert spool["r1"]["run_id"] == "A"

    def test_garbage_is_ignored(self):
        s = svc()
        for topic, payload in (("robot/r1/spool_state", b"x"), ("robot/r1/spool_state", b"[1]"),
                               ("robot/a/b/spool_state", b"{}")):
            s._on_spool_state_message(None, None, SimpleNamespace(topic=topic, payload=payload))
        assert s.get_stats()["spool_state"] == {}

    def test_bounded_by_robots(self):
        s = svc()
        with patch.object(acks, "_ROBOT_CAP", 3):
            for i in range(6):
                s.acks.note_spool(f"r{i}", {"depth": i})
        assert list(s.acks.snapshot()["spool_state"]) == ["r3", "r4", "r5"]


# --- the run_id guard -----------------------------------------------------------------------------

class TestRunIdGuard:
    async def test_mismatch_is_rejected_before_the_reset_wipes_anything(self):
        s = svc()
        for sid in (1, 2):
            await s._handle_node_update({**NODE, "session_node_id": sid, "run_id": "A",
                                         "parts": []})
        await send_part(s, IMG, session_node_id=9, run_id="A")  # buffered for run A
        mappings = dict(s.session_to_global_map)
        buffered = dict(s.image_buffer.entries)
        await s._handle_node_update({**NODE, "session_node_id": 1, "run_id": "B",
                                     "parts": manifest(IMG)})
        b = last(s)
        assert b["status"] == "rejected" and b["reason"] == "run_id_mismatch"
        assert b["resend"] == "none" and b["run_id"] == "B"
        assert s.session_to_global_map == mappings and s.image_buffer.entries == buffered
        assert s._robot_runs["r1"] == "A"
        assert s.stats["run_id_mismatch"] == 1 and s.stats["nodes_rejected"] == 1
        assert s.graph_db.add_node.call_count == 2
        event = s._write_event.await_args.args[0]
        assert event.code == EventCode.MAP_INGEST_REJECTED
        assert event.payload["reason"] == "run_id_mismatch"
        # the legitimate run goes on
        await s._handle_node_update({**NODE, "session_node_id": 3, "run_id": "A", "parts": []})
        assert last(s)["status"] == "accepted" and last(s)["complete"] is True

    async def test_mismatching_parts_are_rejected_and_touch_nothing(self):
        s = svc()
        await s._handle_node_update({**NODE, "run_id": "A", "parts": manifest(IMG)})
        stored_before = s.image_db.store_image.call_count
        await send_part(s, IMG, run_id="B")
        b = last(s)
        assert b["reason"] == "run_id_mismatch" and b["run_id"] == "B" and b["resend"] == "none"
        assert s.image_db.store_image.call_count == stored_before
        for part in (DEP, CST):
            await send_part(s, part, run_id="B")
            assert last(s)["reason"] == "run_id_mismatch"
        assert s.stats["run_id_mismatch"] == 3
        # run A's tracker is untouched and finishes
        await send_part(s, IMG, run_id="A")
        assert last(s)["complete"] is True and last(s)["run_id"] == "A"

    async def test_messages_without_run_id_are_never_rejected(self):
        s = svc()
        await s._handle_node_update({**NODE, "run_id": "A", "parts": []})
        await s._handle_node_update({**NODE, "session_node_id": 8})
        await send_part(s, IMG, session_node_id=8)
        assert s.stats["run_id_mismatch"] == 0 and last(s)["status"] != "rejected"

    async def test_binding_comes_from_the_nodes_already_stored(self):
        arango = _Arango()
        s1 = arango.service()
        await s1._handle_node_update({**NODE, "run_id": "A"})
        s2 = arango.service()  # restarted: the binding is read with last_run_id
        s2.mqtt_client = FakeMQTT()
        await s2._handle_node_update({**NODE, "session_node_id": 8, "run_id": "B"})
        assert s2.mqtt_client.acks()[-1].body["reason"] == "run_id_mismatch"
        s2.graph_db.last_run_id.assert_called_once_with("yard", "r1", "s1")
        await s2._handle_node_update({**NODE, "session_node_id": 9, "run_id": "A"})
        assert s2.mqtt_client.acks()[-1].body["status"] == "accepted"
        s2.graph_db.last_run_id.assert_called_once()  # cached per cloud session

    async def test_session_with_nodes_but_no_run_binds_the_first_one(self):
        s = svc()
        await s._handle_node_update(dict(NODE))                       # legacy node, no run_id
        s.graph_db.last_run_id = Mock(return_value=None)
        await s._handle_node_update({**NODE, "session_node_id": 8, "run_id": "A"})
        assert last(s)["status"] == "accepted"
        await s._handle_node_update({**NODE, "session_node_id": 9, "run_id": "B"})
        assert last(s)["reason"] == "run_id_mismatch"

    async def test_lookup_error_neither_rejects_nor_binds(self):
        s = svc()
        s.graph_db.last_run_id = Mock(side_effect=RuntimeError("arango down"))
        await s._handle_node_update({**NODE, "run_id": "A", "parts": []})
        await s._handle_node_update({**NODE, "session_node_id": 8, "run_id": "B", "parts": []})
        assert [m.body["status"] for m in s.mqtt_client.acks()] == ["accepted", "accepted"]
        assert s._session_runs == {}
        assert s.graph_db.last_run_id.call_count == 1  # not retried for every message
        s._run_guard_retry.clear()
        s.graph_db.last_run_id = Mock(return_value=None)
        await s._handle_node_update({**NODE, "session_node_id": 9, "run_id": "C", "parts": []})
        assert s._session_runs == {"s1": "C"}

    async def test_binding_is_per_cloud_session_and_bounded(self):
        s = svc()
        with patch.object(gb_server, "RUN_BINDING_CACHE_SIZE", 2):
            for i, sid in enumerate(("s1", "s2", "s3")):
                _use_row(s, _row(sid=sid))
                await s._handle_node_update({**NODE, "session_node_id": i, "run_id": f"R{i}",
                                             "parts": []})
        assert list(s._session_runs) == ["s2", "s3"]
        # a different cloud session may carry a different run (a new session after Finish)
        assert s.stats["run_id_mismatch"] == 0


# --- bounded trackers ------------------------------------------------------------------------------

class TestTrackerCap:
    def test_oldest_trackers_are_evicted_and_counted(self):
        stats = {}
        sent = []
        m = acks.AckManager(lambda *a: sent.append(a) or 0, stats, max_trackers=3)
        for sid in range(5):
            m.begin("r1", "A", sid, [("image", "left")], "s1")
        assert m.snapshot()["ack_trackers"] == 3 and stats["ack_trackers_evicted"] == 2
        # an evicted bundle has no tracker: its part is stored without an ack, the robot's
        # resent node answers from storage
        m.part_stored("r1", "A", 0, "image", "left")
        m.node_ok("r1", "A", 0, acks.DUPLICATE, "s1", [("image", "left")], {("image", "left")})
        body = json.loads(sent[0][1])
        assert body["complete"] is True and body["status"] == "duplicate"

    async def test_service_uses_the_configured_cap(self):
        s = svc()
        assert s.acks.max_trackers == gb_server.INGEST_ACK_TRACKER_MAX
        s.acks.max_trackers = 2
        for sid in (1, 2, 3):
            await s._handle_node_update({**NODE, "session_node_id": sid, "parts": MANIFEST})
        assert s.acks.snapshot()["ack_trackers"] == 2 and s.stats["ack_trackers_evicted"] == 1

    def test_parse_parts(self):
        assert acks.parse_parts(None) is None and acks.parse_parts("x") is None
        assert acks.parse_parts([]) == []
        assert acks.parse_parts(MANIFEST + MANIFEST) == [IMG, DEP, CST]


class TestBegunNodeNeverLeaks:
    @pytest.mark.parametrize("exc", [RuntimeError("boom"), asyncio.CancelledError()])
    async def test_exception_after_begin_fails_the_node_retryably(self, exc):
        s = svc()
        s._ensure_robot_exists = AsyncMock(side_effect=exc)
        with pytest.raises(type(exc)):
            await s._handle_node_update({**NODE, "parts": MANIFEST})
        [m] = s.mqtt_client.acks()
        assert m.body["status"] == "rejected" and m.body["reason"] == "store_failed"
        assert m.body["resend"] == "node"
        assert s.acks.snapshot()["ack_trackers"] == 0

    async def test_exception_after_the_first_ack_sends_no_second_ack(self):
        s = svc()
        s._publish_node_update = AsyncMock(side_effect=RuntimeError("boom"))
        with pytest.raises(RuntimeError):
            await s._handle_node_update({**NODE, "parts": MANIFEST})
        [m] = s.mqtt_client.acks()
        assert m.body["status"] == "accepted" and m.body["complete"] is False
        assert s.acks.snapshot()["ack_trackers"] == 1

    def test_unacked_tracker_expires_without_an_ack(self):
        clock = Clock()
        m = acks.AckManager(lambda *a: 0, {}, part_timeout_s=10, clock=clock)
        m.begin("r1", "A", 1, [IMG], "s")
        clock.t += 19
        m.flush()
        assert m.snapshot()["ack_trackers"] == 1
        clock.t += 2
        m.flush()
        assert m.snapshot()["ack_trackers"] == 0
        m.node_abandoned("r1", "A", 1, [IMG], "s")  # nothing left: no ack, no error


class TestIngestMode:
    """The retained `robot/<robot>/ingest_mode` carrying the active mission's register_map
    (a sibling of ingest_state, so it never rewrites the retained high-water mark)."""

    @staticmethod
    def _mgr(rc=0, raises=None):
        pub = FakeMQTT(rc=rc, raises=raises)
        stats = {}
        m = acks.AckManager(lambda t, b, q, r: pub.publish(t, b, q, r).rc, stats, clock=Clock())
        return m, pub, stats

    @staticmethod
    def modes(pub, robot="r1"):
        return [x for x in pub.messages if x.topic == f"robot/{robot}/ingest_mode"]

    def test_false_and_true_are_published_retained(self):
        m, pub, stats = self._mgr()
        assert m.note_mode("r1", False) is True
        msg = self.modes(pub)[-1]
        assert msg.retain and msg.qos == 1
        assert msg.body["v"] == 1 and msg.body["register_map"] is False
        assert m.note_mode("r1", True) is True
        assert self.modes(pub)[-1].body["register_map"] is True
        assert stats["ingest_mode_published"] == 2

    def test_unchanged_value_is_not_republished(self):
        m, pub, _ = self._mgr()
        for _ in range(5):
            m.note_mode("r1", False)
        assert len(self.modes(pub)) == 1

    def test_change_republishes_and_mission_end_goes_unknown(self):
        m, pub, _ = self._mgr()
        m.note_mode("r1", False)
        m.note_mode("r1", None)  # mission ended: unknown = field absent
        last = self.modes(pub)[-1]
        assert last.retain and "register_map" not in last.body
        m.note_mode("r1", None)
        assert len(self.modes(pub)) == 2

    def test_no_mission_known_publishes_nothing(self):
        m, pub, _ = self._mgr()
        assert m.note_mode("r1", None) is False
        assert pub.messages == []

    def test_invalid_robot_or_value_is_ignored_safely(self):
        m, pub, _ = self._mgr()
        assert m.note_mode("a/b", False) is False
        m.note_mode("r1", "yes")  # not a bool -> unknown -> nothing
        assert pub.messages == []

    def test_stale_retained_false_is_corrected_after_restart(self):
        m, pub, _ = self._mgr()
        m.seed_mode("r1", {"v": 1, "register_map": False})
        assert pub.messages == []
        m.note_mode("r1", False)  # mission still running: no change
        assert pub.messages == []
        assert m.mode_robots() == ["r1"]
        m.note_mode("r1", None)  # nothing running: the stale false is cleared
        assert "register_map" not in self.modes(pub)[-1].body

    def test_seeding_the_mode_does_not_regress_highest_complete_id(self):
        m, pub, _ = self._mgr()
        m.seed_state("r1", {"run_id": "A", "highest_complete_id": 40})
        m.seed_mode("r1", {"register_map": False})
        m.note_mode("r1", True)
        m.node_ok("r1", "A", 41, acks.ACCEPTED, "s", [])
        assert m.snapshot()["ingest_state"]["r1"] == {"run_id": "A", "highest_complete_id": 41}
        assert all("register_map" not in s.body for s in pub.states())
        assert pub.states()[-1].body["highest_complete_id"] == 41

    def test_ingest_state_and_acks_unchanged(self):
        m, pub, _ = self._mgr()
        m.note_mode("r1", False)
        m.node_ok("r1", "A", 1, acks.ACCEPTED, "s", [])
        assert len(pub.acks()) == 1 and len(pub.states()) == 1
        assert set(pub.states()[0].body) == {"v", "robot_name", "run_id", "highest_complete_id",
                                             "ts"}

    def test_publish_failures_never_raise(self):
        for kw in ({"raises": RuntimeError("boom")}, {"rc": 15}, {"rc": 1}):
            m, _, stats = self._mgr(**kw)
            m.note_mode("r1", False)
            assert sum(stats.get(k, 0) for k in ("acks_failed", "acks_dropped")) == 1

    def test_failed_publish_is_retried_on_the_next_note(self):
        for kw in ({"raises": RuntimeError("boom")}, {"rc": 15}, {"rc": 1}):
            m, pub, _ = self._mgr(**kw)
            assert m.note_mode("r1", False) is False
            assert m.mode_robots() == []  # not remembered as published
            pub.raises, pub.rc = None, 0
            assert m.note_mode("r1", False) is True
            assert self.modes(pub)[-1].body["register_map"] is False

    def test_no_client_then_client_publishes_and_failed_clear_is_retried(self):
        state = {"rc": None}  # no client yet
        pub = FakeMQTT()
        m = acks.AckManager(lambda t, b, q, r: state["rc"] if state["rc"] != 0
                            else pub.publish(t, b, q, r).rc, {}, clock=Clock())
        assert m.note_mode("r1", False) is False
        state["rc"] = 4  # NO_CONN with a client: paho queues it, accepted
        assert m.note_mode("r1", False) is True
        state["rc"] = None
        assert m.note_mode("r1", None) is False  # clear dropped: still False, retried
        assert m.mode_robots() == ["r1"]
        state["rc"] = 0
        assert m.note_mode("r1", None) is True
        assert "register_map" not in self.modes(pub)[-1].body

    def test_late_retained_false_is_seeded_and_cleared(self):
        m, pub, _ = self._mgr()
        m.note_mode("r2", True)
        m.seed_mode("r1", {"register_map": False})  # arrives after the first refresh
        assert sorted(m.mode_robots()) == ["r1", "r2"]
        m.seed_mode("r2", {"register_map": False})  # our own newer value wins
        m.note_mode("r1", None)
        assert "register_map" not in self.modes(pub, "r1")[-1].body
        assert len(self.modes(pub, "r2")) == 1

    def test_cap_eviction_keeps_false(self):
        m, pub, _ = self._mgr()
        with patch.object(acks, "_ROBOT_CAP", 3):
            m.seed_mode("keep", {"register_map": False})
            for i in range(5):
                m.seed_mode(f"u{i}", {"v": 1})
            m.seed_mode("t", {"register_map": True})
            assert "keep" in m.mode_robots()
            m.note_mode("keep", None)
            assert "register_map" not in self.modes(pub, "keep")[-1].body


class TestRefreshIngestMode:
    @staticmethod
    def _svc(missions=None, error=None):
        service = _service()
        pub = FakeMQTT()
        service.acks = acks.AckManager(lambda t, b, q, r: pub.publish(t, b, q, r).rc,
                                       service.stats)
        service.database = SimpleNamespace(list_objects=AsyncMock(
            return_value=missions, side_effect=error))
        return service, pub

    @staticmethod
    def _m(robot, flag):
        return SimpleNamespace(robot=robot, register_map=flag)

    def test_running_missions_publish_flag_and_end_clears_it(self):
        service, pub = self._svc([self._m("r1", False), self._m("r2", True)])
        asyncio.run(service.refresh_ingest_mode())
        got = {m.topic: m.body.get("register_map") for m in pub.messages}
        assert got == {"robot/r1/ingest_mode": False, "robot/r2/ingest_mode": True}
        asyncio.run(service.refresh_ingest_mode())  # no flapping
        assert len(pub.messages) == 2
        service.database.list_objects.return_value = [self._m("r2", True)]
        asyncio.run(service.refresh_ingest_mode())
        assert "register_map" not in pub.messages[-1].body
        assert pub.messages[-1].topic == "robot/r1/ingest_mode"

    def test_dropped_publish_is_retried_by_the_next_refresh(self):
        service, pub = self._svc([self._m("r1", False)])
        pub.rc = 1
        asyncio.run(service.refresh_ingest_mode())
        assert service.acks.mode_robots() == []
        pub.rc = 0
        asyncio.run(service.refresh_ingest_mode())
        assert pub.messages[-1].body["register_map"] is False

    def test_lookup_failure_changes_nothing_and_does_not_raise(self):
        service, pub = self._svc([self._m("r1", False)])
        asyncio.run(service.refresh_ingest_mode())
        service.database.list_objects.side_effect = RuntimeError("db down")
        asyncio.run(service.refresh_ingest_mode())
        assert len(pub.messages) == 1
