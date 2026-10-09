"""graph-builder costmap ingest (`robot/costmap_upload`), a sibling of the depth path:

- validated cheaply, resolved like an image, buffered until the node exists, stored as a PNG at
  `{node}/costmap/{layer}.png` and as `costmap.{layer}` on the ArangoDB node (PNG first);
- `origin_map` / `origin_pose3d_map` = map_T_session applied, exactly as the node pose;
- rejected costmaps counted as `dropped_costmap` in MAP.INGEST_REJECTED.
"""
import asyncio
import base64
import datetime
import json
import math
import os
import struct
import threading
import zlib

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

from unittest.mock import Mock, patch  # noqa: E402

import pytest  # noqa: E402

from packages.events.codes import EventCode  # noqa: E402
from packages.events.emit import build_row  # noqa: E402
from packages.services.graph_builder import ingest  # noqa: E402
from packages.topomap_dbs.graph_db.server import GraphDatabaseService  # noqa: E402
from packages.topomap_dbs.image_db.server import ImageDatabaseService  # noqa: E402
from packages.utils import map_geo  # noqa: E402
from tests.unit.test_maps_m2 import NODE, _gb, _row  # noqa: E402

pytestmark = pytest.mark.unit



def png_bytes(width=200, height=100, bit_depth=8, colour_type=0):
    """A PNG with a real signature and IHDR (the header is all graph-builder reads) and no
    pixel data."""
    ihdr = struct.pack(">IIBBBBB", width, height, bit_depth, colour_type, 0, 0, 0)
    chunk = b"IHDR" + ihdr
    return (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", len(ihdr)) + chunk
            + struct.pack(">I", zlib.crc32(chunk)) + b"fake pixels")


PNG = png_bytes()
COSTMAP = {"session_node_id": 7, "robot_name": "r1", "layer": "occupancy",
           "costmap_data": base64.b64encode(PNG).decode(), "content_type": "image/png",
           "costmap_encoding": "u8_occ100_unknown255", "width": 200, "height": 100,
           "resolution": 0.05, "origin": {"x": -5.0, "y": -2.5, "yaw": 0.1},
           "origin_pose3d": {"x": -5.0, "y": -2.5, "z": 0.1, "qx": 0.0, "qy": 0.0, "qz": 0.0,
                             "qw": 1.0},
           "frame": "map", "source_frame": "odom", "costmap_stamp_ms": 1727600000123,
           "keyframe_stamp_ms": 1727600000084, "stamp_offset_ms": 39,
           "source_topic": "/local_costmap/costmap"}


def _service(row=None):
    service = _gb(row if row is not None else _row())
    service.image_db.store_costmap = Mock(return_value=True)
    service.graph_db.set_node_costmap = Mock(return_value=True)
    service.graph_db.set_node_costmaps = Mock(return_value=True)
    return service


async def _none():
    return None


class TestPayload:
    @pytest.mark.parametrize("patch_", [
        {"costmap_data": None}, {"costmap_data": ""}, {"layer": ""}, {"layer": "a/b"},
        {"layer": "."}, {"layer": ".."}, {"layer": "a.b"}, {"layer": "a b"},
        {"layer": "x" * 33}, {"layer": "\u00e4"}, {"layer": "a\n"},
        {"robot_name": None}, {"session_node_id": None},
        {"costmap_encoding": "f32"}, {"content_type": "image/jpeg"},
        {"width": 0}, {"width": -1}, {"width": 1.5}, {"width": "10"}, {"width": True},
        {"height": 0}, {"height": None},
        {"resolution": 0}, {"resolution": -0.05}, {"resolution": float("nan")},
        {"resolution": float("inf")}, {"resolution": "x"}, {"resolution": None},
        {"origin": None}, {"origin": {"x": 1, "y": 2}},
        {"origin": {"x": float("nan"), "y": 0, "yaw": 0}},
        {"origin": {"x": 0, "y": float("inf"), "yaw": 0}},
        {"origin_pose3d": {"x": 1}},
        {"origin_pose3d": {"x": 0, "y": 0, "z": float("nan"), "qx": 0, "qy": 0, "qz": 0,
                           "qw": 1}},
        {"origin_pose3d": {"x": 0, "y": 0, "z": 0, "qx": 0, "qy": 0, "qz": 0, "qw": 0}},
    ])
    def test_invalid(self, patch_):
        with pytest.raises(ingest.CostmapPayloadError):
            ingest.check_costmap_payload({**COSTMAP, **patch_})

    def test_defaults_and_optionals(self):
        p = {k: v for k, v in COSTMAP.items()
             if k not in ("content_type", "costmap_encoding", "origin_pose3d")}
        ingest.check_costmap_payload(p)
        ingest.check_costmap_payload({**p, "origin_pose3d": None})

    @pytest.mark.parametrize("layer", ["occupancy", "x" * 32, "Inflated_2-b", "7"])
    def test_valid_layers(self, layer):
        ingest.check_costmap_payload({**COSTMAP, "layer": layer})

    @pytest.mark.parametrize("patch_, message", [
        ({"resolution": None}, "missing resolution"),
        ({"origin": {"x": 1, "y": 2}}, "missing yaw"),
        ({"origin_pose3d": {"x": 1}}, "origin_pose3d needs x, y, z, qx, qy, qz, qw"),
        ({"origin_pose3d": {**COSTMAP["origin_pose3d"], "z": "up"}},
         "origin_pose3d: could not convert string to float: 'up'"),
        ({"origin_pose3d": {**COSTMAP["origin_pose3d"], "z": float("nan")}},
         "origin_pose3d has a non-finite value"),
        ({"origin_pose3d": {**COSTMAP["origin_pose3d"], "qw": 0.0}},
         "origin_pose3d has a zero quaternion"),
    ])
    def test_messages(self, patch_, message):
        with pytest.raises(ingest.CostmapPayloadError) as exc:
            ingest.check_costmap_payload({**COSTMAP, **patch_})
        assert str(exc.value) == message

    def test_record_validates_when_called_directly(self):
        session = ingest.OpenSession("s1", "yard", False, dict(map_geo.IDENTITY), "ALIVE",
                                     "mapping")
        with pytest.raises(ingest.CostmapPayloadError):
            ingest.costmap_record({**COSTMAP, "layer": "a/b"}, session)

    def test_png_is_not_decoded_by_the_check(self):
        # Only the header is read: whatever follows it (here not even valid base64) is not.
        head = base64.b64encode(PNG[:33]).decode()
        ingest.check_costmap_payload({**COSTMAP, "costmap_data": head + "!!not base64!!"})

    @pytest.mark.parametrize("data, message", [
        ("!!not base64!!", "costmap_data is not base64"),
        (base64.b64encode(b"GIF89a" + b"\0" * 40).decode(), "costmap_data is not a PNG"),
        (base64.b64encode(PNG[:20]).decode(), "costmap_data is not a PNG"),
        (123, "costmap_data is not a base64 string"),
        (base64.b64encode(png_bytes(width=201)).decode(),
         "costmap_data is 201x100, the message says 200x100"),
        (base64.b64encode(png_bytes(height=99)).decode(),
         "costmap_data is 200x99, the message says 200x100"),
        (base64.b64encode(png_bytes(bit_depth=16)).decode(),
         "costmap_data is not a 8-bit grayscale PNG (bit depth 16, colour type 0)"),
        (base64.b64encode(png_bytes(colour_type=2)).decode(),
         "costmap_data is not a 8-bit grayscale PNG (bit depth 8, colour type 2)"),
    ])
    def test_png_header_is_checked(self, data, message):
        with pytest.raises(ingest.CostmapPayloadError) as exc:
            ingest.check_costmap_payload({**COSTMAP, "costmap_data": data})
        assert str(exc.value).startswith(message)

    def test_record(self):
        session = ingest.OpenSession("s1", "yard", False,
                                     {"tx": 1.0, "ty": 0.0, "yaw": 0.0}, "ALIVE", "mapping")
        rec = ingest.costmap_record(COSTMAP, session)
        assert "costmap_data" not in rec
        assert rec["origin"] == COSTMAP["origin"]
        assert rec["origin_pose3d"] == COSTMAP["origin_pose3d"]
        assert rec["origin_map"] == pytest.approx({"x": -4.0, "y": -2.5, "yaw": 0.1})
        assert rec["origin_pose3d_map"]["x"] == pytest.approx(-4.0)
        assert rec["origin_pose3d_map"]["z"] == 0.1
        assert rec["session_id"] == "s1" and rec["source_topic"] == "/local_costmap/costmap"
        assert rec["width"] == 200 and rec["resolution"] == 0.05
        assert set(rec) == {
            "layer", "width", "height", "resolution", "origin", "origin_pose3d", "frame",
            "source_frame", "costmap_stamp_ms", "keyframe_stamp_ms", "stamp_offset_ms",
            "source_topic", "origin_map", "origin_pose3d_map", "session_id"}
        assert (rec["frame"], rec["source_frame"]) == ("map", "odom")
        assert (rec["costmap_stamp_ms"], rec["keyframe_stamp_ms"], rec["stamp_offset_ms"]) \
            == (1727600000123, 1727600000084, 39)

    def test_record_stores_only_known_fields_parsed(self):
        session = ingest.OpenSession("s1", "yard", False, dict(map_geo.IDENTITY), "ALIVE",
                                     "mapping")
        p = {k: v for k, v in COSTMAP.items()
             if k not in ("frame", "source_frame", "costmap_stamp_ms", "keyframe_stamp_ms",
                          "stamp_offset_ms", "source_topic", "content_type",
                          "costmap_encoding")}
        p.update({"origin": {"x": -5, "y": "2", "yaw": 0, "extra": 1}, "resolution": "0.1",
                  "origin_pose3d": {**COSTMAP["origin_pose3d"], "qw": 1, "note": "x"},
                  "debug_blob": "x" * 100, "map_id": "other", "session_id": "robot-side"})
        rec = ingest.costmap_record(p, session)
        assert "debug_blob" not in rec and "map_id" not in rec and "source_topic" not in rec
        assert rec["session_id"] == "s1"
        assert rec["origin"] == {"x": -5.0, "y": 2.0, "yaw": 0.0}
        assert all(isinstance(v, float) for v in rec["origin"].values())
        assert rec["resolution"] == 0.1 and isinstance(rec["resolution"], float)
        assert "note" not in rec["origin_pose3d"] and rec["origin_pose3d"]["qw"] == 1.0
        # absent optional fields are omitted (not None), source_topic like the others
        assert not {"frame", "source_frame", "costmap_stamp_ms", "keyframe_stamp_ms",
                    "stamp_offset_ms", "source_topic"} & set(rec)
        assert not {"content_type", "costmap_encoding", "session_node_id", "robot_name"} & set(rec)

    def test_record_without_pose3d(self):
        session = ingest.OpenSession("s1", "yard", False, dict(map_geo.IDENTITY), "ALIVE",
                                     "mapping")
        rec = ingest.costmap_record({**COSTMAP, "origin_pose3d": None}, session)
        assert "origin_pose3d_map" not in rec and "origin_map" in rec

    def test_origin_transform_with_non_identity_map_t_session(self):
        t = {"tx": 100.0, "ty": -50.0, "yaw": math.pi / 2}
        session = ingest.OpenSession("s1", "yard", False, t, "ALIVE", "mapping")
        rec = ingest.costmap_record(COSTMAP, session)
        x, y, yaw = ingest.map_pose(t, -5.0, -2.5, 0.1)
        assert (rec["origin_map"]["x"], rec["origin_map"]["y"]) == pytest.approx(
            (100.0 + 2.5, -50.0 - 5.0))
        assert (rec["origin_map"]["x"], rec["origin_map"]["y"], rec["origin_map"]["yaw"]) \
            == pytest.approx((x, y, yaw))
        assert rec["origin_map"]["yaw"] == pytest.approx(0.1 + math.pi / 2)
        p3 = ingest.pose3d_map(t, COSTMAP["origin_pose3d"])
        assert rec["origin_pose3d_map"] == pytest.approx(p3)
        assert rec["origin_pose3d_map"]["qz"] == pytest.approx(math.sin(math.pi / 4))


class TestCostmapIngest:
    async def test_before_node_is_buffered_then_stored(self):
        t = {"tx": 10.0, "ty": 0.0, "yaw": math.pi / 2}
        service = _service(_row(t=t))
        await service._handle_costmap_upload(dict(COSTMAP))
        assert ("r1", 7) in service.costmap_buffer
        assert service.stats["buffered_costmap"] == 1
        service.image_db.store_costmap.assert_not_called()
        await service._handle_node_update(dict(NODE))
        node_id = service.graph_db.add_node.call_args.kwargs["node_id"]
        png, nid, layer, map_id = service.image_db.store_costmap.call_args.args
        assert (png, nid, layer, map_id) == (PNG, node_id, "occupancy", "yard")
        meta = service.image_db.store_costmap.call_args.kwargs["metadata"]
        assert meta == {"layer": "occupancy", "costmap_stamp_ms": 1727600000123,
                        "session_id": "s1"}
        m, nid2, layer2, rec = service.graph_db.set_node_costmap.call_args.args
        assert (m, nid2, layer2) == ("yard", node_id, "occupancy")
        assert rec["origin_map"]["x"] == pytest.approx(10.0 + 2.5)
        assert rec["origin_map"]["y"] == pytest.approx(-5.0)
        assert service.costmap_buffer == {} and service.stats["buffered_costmap"] == 0
        assert service.stats["costmap_saved"] == 1

    async def test_after_node(self):
        service = _service()
        await service._handle_node_update(dict(NODE))
        await service._handle_costmap_upload(dict(COSTMAP))
        node_id = service.graph_db.add_node.call_args.kwargs["node_id"]
        assert service.image_db.store_costmap.call_args.args[1] == node_id
        service.graph_db.set_node_costmap.assert_called_once()
        assert service.stats["costmap_saved"] == 1

    async def test_buffer_overwrites_per_layer(self):
        service = _service()
        await service._handle_costmap_upload(dict(COSTMAP))
        wide = base64.b64encode(png_bytes(width=300)).decode()
        await service._handle_costmap_upload({**COSTMAP, "width": 300, "costmap_data": wide})
        await service._handle_costmap_upload({**COSTMAP, "layer": "inflated"})
        assert service.stats["buffered_costmap"] == 2
        assert service.costmap_buffer[("r1", 7)]["occupancy"][0]["parsed"]["width"] == 300
        await service._handle_node_update(dict(NODE))
        # both layers of the node in one update (the single-layer setter is not used)
        service.graph_db.set_node_costmap.assert_not_called()
        service.graph_db.set_node_costmaps.assert_called_once()
        m, node_id, records = service.graph_db.set_node_costmaps.call_args.args
        assert m == "yard" and set(records) == {"occupancy", "inflated"}
        assert records["occupancy"]["width"] == 300 and records["inflated"]["width"] == 200
        assert service.stats["costmap_saved"] == 2 and service.stats["errors"] == 0

    async def test_png_is_stored_before_the_record(self):
        service = _service()
        order = []
        service.image_db.store_costmap.side_effect = lambda *a, **k: order.append("png") or True
        service.graph_db.set_node_costmap.side_effect = lambda *a: order.append("rec") or True
        await service._handle_node_update(dict(NODE))
        await service._handle_costmap_upload(dict(COSTMAP))
        assert order == ["png", "rec"]

    async def test_node_is_stored_before_its_costmap_record(self):
        service = _service()
        order = []
        service.graph_db.add_node.side_effect = lambda **kw: order.append("node") or True
        service.graph_db.set_node_costmap.side_effect = lambda *a: order.append("cm") or True
        await service._handle_costmap_upload(dict(COSTMAP))
        await service._handle_node_update(dict(NODE))
        assert order == ["node", "cm"]

    async def test_png_store_failure_does_not_touch_the_node(self):
        service = _service()
        service.image_db.store_costmap.return_value = False
        await service._handle_node_update(dict(NODE))
        await service._handle_costmap_upload(dict(COSTMAP))
        service.graph_db.set_node_costmap.assert_not_called()
        assert service.stats["costmap_saved"] == 0 and service.stats["errors"] == 1

    async def test_bad_base64_is_an_error_and_stores_nothing(self):
        service = _service()
        await service._handle_node_update(dict(NODE))
        await service._handle_costmap_upload({**COSTMAP, "costmap_data": "!!not base64!!"})
        service.image_db.store_costmap.assert_not_called()
        service.graph_db.set_node_costmap.assert_not_called()
        assert service.stats["costmap_saved"] == 0 and service.stats["errors"] == 1

    async def test_rejected_costmap(self):
        service = _service(_row(lifecycle="DELETING"))
        await service._handle_costmap_upload(dict(COSTMAP))
        assert service.costmap_buffer == {} and service.stats["costmap_rejected"] == 1
        event = service._write_event.await_args.args[0]
        assert event.code == EventCode.MAP_INGEST_REJECTED
        assert event.payload["dropped_costmap"] == 1
        assert event.payload["dropped_depth"] == 0 and event.payload["dropped_images"] == 0
        assert build_row(event, strict=True)["payload"]["dropped_costmap"] == 1

    async def test_rejected_node_drops_its_buffered_costmap(self):
        service = _service()
        await service._handle_costmap_upload(dict(COSTMAP))
        service.sessions = ingest.SessionResolver(lambda _r: _none(), ttl=0)
        await service._handle_node_update(dict(NODE))
        assert service.costmap_buffer == {} and service.stats["buffered_costmap"] == 0
        assert service.rejects.dropped["costmap"] == 1
        assert service.stats["costmap_rejected"] == 1
        service.image_db.store_costmap.assert_not_called()

    async def test_from_another_session_is_discarded(self):
        service = _service()
        await service._handle_costmap_upload(dict(COSTMAP))
        entry, _ = service.costmap_buffer[("r1", 7)]["occupancy"]
        entry["session_id"] = "s-old"
        await service._handle_node_update(dict(NODE))
        service.image_db.store_costmap.assert_not_called()
        assert service.stats["buffered_costmap"] == 0

    async def test_timed_out_is_discarded(self):
        service = _service()
        await service._handle_costmap_upload(dict(COSTMAP))
        entry, _ = service.costmap_buffer[("r1", 7)]["occupancy"]
        service.costmap_buffer[("r1", 7)]["occupancy"] = (
            entry, datetime.datetime.now() - datetime.timedelta(seconds=3600))
        await service._handle_node_update(dict(NODE))
        service.image_db.store_costmap.assert_not_called()
        assert service.stats["buffered_costmap"] == 0

    async def test_invalid_payload_is_an_error_not_a_rejection_or_event(self):
        service = _service()
        await service._handle_costmap_upload({**COSTMAP, "costmap_encoding": "f32"})
        assert service.stats["errors"] == 1 and service.stats["costmap_rejected"] == 0
        assert service.costmap_buffer == {}
        service._write_event.assert_not_called()

    async def test_old_robot_without_costmap_is_unchanged(self):
        service = _service()
        await service._handle_node_update(dict(NODE))
        service.image_db.store_costmap.assert_not_called()
        service.graph_db.set_node_costmap.assert_not_called()
        assert "costmap" not in service.graph_db.add_node.call_args.kwargs["metadata"]

    async def test_cleanup_and_session_reset_clear_the_costmap_buffer(self):
        service = _service()
        await service._handle_costmap_upload(dict(COSTMAP))
        service._cleanup_old_mappings(threshold_seconds=-1)
        assert service.costmap_buffer == {} and service.stats["buffered_costmap"] == 0
        await service._handle_costmap_upload(dict(COSTMAP))
        service.session_to_global_map[("r1", 50)] = ("g", datetime.datetime.now(), "yard",
                                                     "s1")
        service._detect_and_clear_session_reset("r1", 1)
        assert service.costmap_buffer == {} and service.stats["buffered_costmap"] == 0

    def test_subscribes_at_qos_1(self):
        service = _service()
        with patch("packages.services.graph_builder.server.MQTTClient") as client:
            assert service.connect_mqtt()
        calls = {c.args[0]: c for c in client.return_value.register_callback.call_args_list}
        assert calls["robot/costmap_upload"].kwargs.get("qos") == 1

    def test_empty_topic_skips_the_subscription(self):
        service = _service()
        service.mqtt_costmap_topic = ""
        with patch("packages.services.graph_builder.server.MQTTClient") as client:
            assert service.connect_mqtt()
        topics = [c.args[0] for c in client.return_value.register_callback.call_args_list]
        assert "robot/costmap_upload" not in topics

    def test_paho_callback_schedules_the_handler(self):
        service = _service()
        service._event_loop = Mock()
        msg = Mock(payload=json.dumps(COSTMAP).encode())
        with patch("packages.services.graph_builder.server.asyncio."
                   "run_coroutine_threadsafe") as run:
            service._on_costmap_upload_message(None, None, msg)
            coro = run.call_args.args[0]
            coro.close()
        run.assert_called_once()
        assert run.call_args.args[1] is service._event_loop
        assert service.stats["errors"] == 0

    def test_paho_callback_bad_json_and_no_loop_count_errors(self):
        service = _service()
        service._event_loop = Mock()
        service._on_costmap_upload_message(None, None, Mock(payload=b"{not json"))
        assert service.stats["errors"] == 1
        service._event_loop = None
        service._on_costmap_upload_message(None, None,
                                           Mock(payload=json.dumps(COSTMAP).encode()))
        assert service.stats["errors"] == 2


class TestFrameAtStoreTime:
    """The record's map-frame fields use the transform the node is stored with, not the one
    current when the upload arrived (the session may be re-placed while it waits)."""
    T1 = {"tx": 10.0, "ty": 0.0, "yaw": 0.0}
    T2 = {"tx": -20.0, "ty": 5.0, "yaw": math.pi / 2}

    @staticmethod
    def _replace(service, t):
        async def fetch(_robot):
            return _row(t=t)
        service.sessions = ingest.SessionResolver(fetch, ttl=0)

    def _kinds(self):
        from tests.unit.test_map_reconstruction_ingest import DEPTH
        return [("costmap", COSTMAP, "origin_map", lambda t: ingest.map_pose(t, -5.0, -2.5, 0.1)),
                ("depth", DEPTH, "pose3d_map",
                 lambda t: ingest.map_geo.apply_transform(t, 1.2, -0.4))]

    @pytest.mark.parametrize("which", [0, 1])
    async def test_buffered_upload_follows_a_replacement(self, which):
        kind, payload, field, expected = self._kinds()[which]
        service = _service(_row(t=self.T1))
        service.image_db.store_depth = Mock(return_value=True)
        service.graph_db.set_node_depth = Mock(return_value=True)
        await getattr(service, f"_handle_{kind}_upload")(dict(payload))
        self._replace(service, self.T2)  # re-placed while the upload waited
        await service._handle_node_update(dict(NODE))
        kw = service.graph_db.add_node.call_args.kwargs
        record = getattr(service.graph_db, f"set_node_{kind}").call_args.args[3]
        want = expected(self.T2)
        assert (kw["x"], kw["y"]) == pytest.approx(
            ingest.map_geo.apply_transform(self.T2, 3.0, 4.0))
        assert (record[field]["x"], record[field]["y"]) == pytest.approx(want[:2])

    @pytest.mark.parametrize("which", [0, 1])
    async def test_direct_upload_uses_the_nodes_transform(self, which):
        kind, payload, field, expected = self._kinds()[which]
        service = _service(_row(t=self.T1))
        service.image_db.store_depth = Mock(return_value=True)
        service.graph_db.set_node_depth = Mock(return_value=True)
        await service._handle_node_update(dict(NODE))
        self._replace(service, self.T2)  # re-placed after the node was stored
        await getattr(service, f"_handle_{kind}_upload")(dict(payload))
        record = getattr(service.graph_db, f"set_node_{kind}").call_args.args[3]
        assert (record[field]["x"], record[field]["y"]) == pytest.approx(
            expected(self.T1)[:2])


class TestBufferCaps:
    async def test_full_buffer_drops_the_new_upload_and_reports_it(self):
        service = _service()
        service.costmap_buffer.max_entries = 1
        await service._handle_costmap_upload(dict(COSTMAP))
        await service._handle_costmap_upload({**COSTMAP, "layer": "inflated"})
        assert service.stats["buffered_costmap"] == 1
        assert list(service.costmap_buffer[("r1", 7)]) == ["occupancy"]
        assert service.stats["costmap_rejected"] == 1
        event = service._write_event.await_args.args[0]
        assert event.payload["reason"] == "buffer_full"
        assert event.payload["dropped_costmap"] == 1
        assert build_row(event, strict=True)["payload"]["reason"] == "buffer_full"
        # an overwrite of the buffered layer still fits
        await service._handle_costmap_upload(dict(COSTMAP))
        assert service.stats["costmap_rejected"] == 1

    async def test_byte_cap(self):
        service = _service()
        service.costmap_buffer.max_bytes = len(COSTMAP["costmap_data"]) + 1
        await service._handle_costmap_upload(dict(COSTMAP))
        await service._handle_costmap_upload({**COSTMAP, "layer": "inflated"})
        assert service.stats["buffered_costmap"] == 1
        assert service.stats["costmap_rejected"] == 1
        await service._handle_node_update(dict(NODE))
        assert service.costmap_buffer.bytes == 0

    async def test_stored_directly_when_the_node_is_there_even_if_full(self):
        service = _service()
        service.costmap_buffer.max_entries = 0
        await service._handle_node_update(dict(NODE))
        await service._handle_costmap_upload(dict(COSTMAP))
        assert service.stats["costmap_saved"] == 1 and service.stats["costmap_rejected"] == 0


class TestFlush:
    async def test_one_failed_png_does_not_stop_the_other_layer(self):
        service = _service()
        await service._handle_costmap_upload(dict(COSTMAP))
        await service._handle_costmap_upload({**COSTMAP, "layer": "inflated"})
        service.image_db.store_costmap.side_effect = (
            lambda png, nid, layer, m, metadata=None: layer != "occupancy")
        await service._handle_node_update(dict(NODE))
        service.graph_db.set_node_costmaps.assert_not_called()
        [call] = service.graph_db.set_node_costmap.call_args_list
        assert call.args[2] == "inflated"
        assert service.stats["costmap_saved"] == 1 and service.stats["errors"] == 1

    async def test_failed_record_update_counts_every_stored_png(self):
        service = _service()
        await service._handle_costmap_upload(dict(COSTMAP))
        await service._handle_costmap_upload({**COSTMAP, "layer": "inflated"})
        service.graph_db.set_node_costmaps.return_value = False
        await service._handle_node_update(dict(NODE))
        assert service.image_db.store_costmap.call_count == 2
        assert service.stats["costmap_saved"] == 0 and service.stats["errors"] == 2


class TestImageBufferSession:
    async def test_image_buffered_in_another_session_is_not_attached(self):
        service = _service()
        image = {"session_node_id": 7, "robot_name": "r1", "camera_name": "left",
                 "image_data": "aGVsbG8=", "timestamp": 1, "yaw_offset": 0.0}
        await service._handle_image_upload(dict(image))
        entry, _ = service.image_buffer[("r1", 7)]["left"]
        assert entry["session_id"] == "s1"
        entry["session_id"] = "s-old"  # buffered while another session was open
        await service._handle_node_update(dict(NODE))
        service.image_db.store_image.assert_not_called()
        assert service.stats["buffered_images"] == 0

    async def test_image_of_the_same_session_is_attached(self):
        service = _service()
        image = {"session_node_id": 7, "robot_name": "r1", "camera_name": "left",
                 "image_data": "aGVsbG8=", "timestamp": 1, "yaw_offset": 0.0}
        await service._handle_image_upload(dict(image))
        await service._handle_node_update(dict(NODE))
        service.image_db.store_image.assert_called_once()


class TestStores:
    def test_set_node_costmap_merges_one_layer(self):
        svc = GraphDatabaseService.__new__(GraphDatabaseService)
        svc.logger = Mock()
        svc.db = Mock()
        svc.db.aql.execute.return_value = iter(["n1"])
        assert svc.set_node_costmap("yard", "n1", "occupancy", {"width": 5})
        aql, = svc.db.aql.execute.call_args.args
        bind = svc.db.aql.execute.call_args.kwargs["bind_vars"]
        assert "MERGE(d.costmap || {}" in aql and "mergeObjects: false" in aql
        assert bind == {"@col": "nodes_yard", "key": "n1", "layer": "occupancy",
                        "record": {"width": 5}}
        svc.db.aql.execute.return_value = iter([])
        assert not svc.set_node_costmap("yard", "missing", "occupancy", {})
        # no has_collection round trip: ArangoDB errors on a missing collection
        svc.db.has_collection.assert_not_called()
        svc.db.aql.execute.side_effect = RuntimeError("collection or view not found")
        assert not svc.set_node_costmap("gone", "n1", "occupancy", {})
        svc.logger.error.assert_called_once()

    def test_set_node_costmaps_merges_several_layers_in_one_update(self):
        svc = GraphDatabaseService.__new__(GraphDatabaseService)
        svc.logger = Mock()
        svc.db = Mock()
        svc.db.aql.execute.return_value = iter(["n1"])
        records = {"occupancy": {"width": 5}, "inflated": {"width": 6}}
        assert svc.set_node_costmaps("yard", "n1", records)
        svc.db.aql.execute.assert_called_once()
        aql, = svc.db.aql.execute.call_args.args
        bind = svc.db.aql.execute.call_args.kwargs["bind_vars"]
        assert "MERGE(d.costmap || {}, @records)" in aql and "mergeObjects: false" in aql
        assert bind == {"@col": "nodes_yard", "key": "n1", "records": records}
        svc.db.aql.execute.return_value = iter([])
        assert not svc.set_node_costmaps("yard", "missing", records)

    @patch("packages.topomap_dbs.minio_base.Minio")
    def test_store_costmap_key_and_content_type(self, minio):
        svc = ImageDatabaseService()
        minio.return_value.bucket_exists.return_value = True
        assert svc.store_costmap(PNG, "n1", "occupancy", "yard",
                                 metadata={"layer": "occupancy", "costmap_stamp_ms": 5,
                                           "session_id": None})
        args, kw = minio.return_value.put_object.call_args
        assert args[:2] == ("map-yard", "n1/costmap/occupancy.png")
        assert kw["content_type"] == "image/png"
        assert kw["metadata"] == {"layer": "occupancy", "costmap_stamp_ms": "5",
                                  "node_id": "n1"}

    @patch("packages.topomap_dbs.minio_base.Minio")
    def test_get_stats_does_not_mistake_costmap_for_a_node(self, minio):
        names = ["n1/images/left", "n1/costmap/occupancy.png", "n2/costmap/occupancy.png",
                 "n3/images/left", "n3/depth/left.png"]
        minio.return_value.bucket_exists.return_value = True
        minio.return_value.list_objects.return_value = [Mock(object_name=n) for n in names]
        stats = ImageDatabaseService().get_stats(map_id="yard")
        assert stats["node_count"] == 2 and stats["image_count"] == 2
        assert stats["depth_count"] == 1 and stats["costmap_count"] == 2


class _ArangoNodes:
    """A graph_db stand-in whose add_node blocks until released, and whose record setters fail
    (like ArangoDB) for a node document that does not exist yet."""

    def __init__(self):
        self.nodes = set()
        self.records = []
        self.entered = threading.Event()
        self.release = threading.Event()

    def add_node(self, **kw):
        self.entered.set()
        assert self.release.wait(10), "add_node was never released"
        self.nodes.add(kw["node_id"])
        return True

    def set_record(self, map_id, node_id, sub, record):
        if node_id not in self.nodes:
            return False
        self.records.append((node_id, sub))
        return True


class TestNodeRecordRace:
    """An upload handled while _process_topology (worker thread) has mapped the node but not yet
    written it: the record must wait for the node instead of failing against a missing
    document (it used to be lost)."""

    def _service(self, kind):
        from tests.unit.test_map_reconstruction_ingest import DEPTH
        service = _gb(_row())
        fake = _ArangoNodes()
        service.graph_db.add_node = Mock(side_effect=fake.add_node)
        if kind == "costmap":
            service.image_db.store_costmap = Mock(return_value=True)
            service.graph_db.set_node_costmap = Mock(side_effect=fake.set_record)
            return (service, fake, service._handle_costmap_upload, COSTMAP, "occupancy",
                    "costmap_saved", service.costmap_buffer)
        service.image_db.store_depth = Mock(return_value=True)
        service.graph_db.set_node_depth = Mock(side_effect=fake.set_record)
        return (service, fake, service._handle_depth_upload, DEPTH, "left", "depth_saved",
                service.depth_buffer)

    @pytest.mark.parametrize("kind", ["costmap", "depth"])
    async def test_upload_between_mapping_and_add_node_is_stored_with_the_node(self, kind):
        service, fake, upload, payload, sub, saved, buffer = self._service(kind)
        node_task = asyncio.create_task(service._handle_node_update(dict(NODE)))
        try:
            assert await asyncio.to_thread(fake.entered.wait, 10)
            # Paused inside add_node: the node is mapped but its document does not exist.
            assert ("r1", 7) in service.session_to_global_map and not fake.nodes
            await upload(dict(payload))
            assert fake.records == [] and ("r1", 7) in buffer
            assert service.stats["errors"] == 0
        finally:
            fake.release.set()
        await asyncio.wait_for(node_task, 10)
        node_id = service.graph_db.add_node.call_args.kwargs["node_id"]
        assert fake.records == [(node_id, sub)]
        assert service.stats[saved] == 1 and service.stats["errors"] == 0
        assert len(buffer) == 0

    @pytest.mark.parametrize("kind", ["costmap", "depth"])
    async def test_upload_after_add_node_is_stored_directly(self, kind):
        service, fake, upload, payload, sub, saved, buffer = self._service(kind)
        fake.release.set()
        await service._handle_node_update(dict(NODE))
        await upload(dict(payload))
        node_id = service.graph_db.add_node.call_args.kwargs["node_id"]
        assert fake.records == [(node_id, sub)] and len(buffer) == 0
        assert service.stats[saved] == 1

    async def test_failed_add_node_never_takes_records_directly(self):
        service, fake, upload, payload, sub, saved, buffer = self._service("costmap")
        service.graph_db.add_node = Mock(return_value=False)
        await service._handle_node_update(dict(NODE))
        await upload(dict(payload))
        assert fake.records == [] and ("r1", 7) in buffer

    async def test_resent_node_waits_for_its_new_document(self):
        service, fake, upload, payload, sub, saved, buffer = self._service("costmap")
        fake.release.set()
        await service._handle_node_update(dict(NODE))
        first = service.graph_db.add_node.call_args.kwargs["node_id"]
        fake.release.clear()
        fake.entered.clear()
        node_task = asyncio.create_task(service._handle_node_update(dict(NODE)))
        try:
            assert await asyncio.to_thread(fake.entered.wait, 10)
            await upload(dict(payload))
            assert fake.records == []  # not set on the first node's document either
        finally:
            fake.release.set()
        await asyncio.wait_for(node_task, 10)
        second = service.graph_db.add_node.call_args.kwargs["node_id"]
        assert second != first and fake.records == [(second, sub)]
