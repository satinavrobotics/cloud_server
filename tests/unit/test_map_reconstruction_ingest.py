"""3D reconstruction R2 (docs/reconstruction/design.md §5): graph-builder depth ingest.

- `robot/depth_upload` resolved like an image, buffered until the node exists, stored as a PNG
  at `{node}/depth/{camera}.png` and as `depth.{camera}` on the ArangoDB node;
- `pose3d_map` = map_T_session applied: x, y, yaw change, z, roll and pitch do not;
- rejected depth counted as `dropped_depth` in MAP.INGEST_REJECTED;
- old robots (no depth) unchanged; `ImageDatabaseService.get_stats` ignores `reconstruction/`.
"""
import base64
import datetime
import math
import os

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
from tests.unit.test_maps_m2 import IMAGE, NODE, _gb, _row  # noqa: E402

pytestmark = pytest.mark.unit

PNG = b"\x89PNG\r\n\x1a\nfake"
CAMERA = {"frame_id": "camera", "width": 448, "height": 336, "fx": 300.0, "fy": 300.0,
          "cx": 224.0, "cy": 168.0, "distortion_model": "plumb_bob", "d": [0, 0, 0, 0, 0],
          "depth_type": "z", "valid_range_m": [0.2, 15.0], "rgb_width": 448,
          "rgb_height": 336,
          "T_base_cam": {"x": 0.1, "y": 0.0, "z": 0.45, "qx": -0.5, "qy": 0.5, "qz": -0.5,
                         "qw": 0.5}}
DEPTH = {"session_node_id": 7, "robot_name": "r1", "camera_name": "left",
         "depth_data": base64.b64encode(PNG).decode(), "content_type": "image/png",
         "depth_encoding": "u16_mm", "depth_scale": 0.001, "depth_stamp_ms": 1727600000123,
         "rgb_stamp_ms": 1727600000084,
         "robot_pose3d": {"x": 1.2, "y": -0.4, "z": 0.02, "qx": 0.0, "qy": 0.0, "qz": 0.0,
                          "qw": 1.0},
         "camera": CAMERA}


def _quat_to_rpy(q):
    qx, qy, qz, qw = q["qx"], q["qy"], q["qz"], q["qw"]
    roll = math.atan2(2 * (qw * qx + qy * qz), 1 - 2 * (qx * qx + qy * qy))
    pitch = math.asin(max(-1.0, min(1.0, 2 * (qw * qy - qz * qx))))
    yaw = math.atan2(2 * (qw * qz + qx * qy), 1 - 2 * (qy * qy + qz * qz))
    return roll, pitch, yaw


def _rpy_to_quat(roll, pitch, yaw):
    cr, sr = math.cos(roll / 2), math.sin(roll / 2)
    cp, sp = math.cos(pitch / 2), math.sin(pitch / 2)
    cy, sy = math.cos(yaw / 2), math.sin(yaw / 2)
    return {"qw": cr * cp * cy + sr * sp * sy, "qx": sr * cp * cy - cr * sp * sy,
            "qy": cr * sp * cy + sr * cp * sy, "qz": cr * cp * sy - sr * sp * cy}


def _service(row=None):
    service = _gb(row if row is not None else _row())
    service.image_db.store_depth = Mock(return_value=True)
    service.graph_db.set_node_depth = Mock(return_value=True)
    return service


class TestPose3dMap:
    def test_identity(self):
        pose = {"x": 1.0, "y": 2.0, "z": 0.3, **_rpy_to_quat(0.02, 0.05, 0.7)}
        out = ingest.pose3d_map(map_geo.IDENTITY, pose)
        assert out == pytest.approx(pose)

    def test_keeps_z_roll_pitch_and_turns_heading(self):
        t = {"tx": 100.0, "ty": -50.0, "yaw": math.pi / 2}
        roll, pitch, yaw = 0.035, math.radians(5), 0.3
        pose = {"x": 3.0, "y": 4.0, "z": 0.05, **_rpy_to_quat(roll, pitch, yaw)}
        out = ingest.pose3d_map(t, pose)
        # the position goes exactly where the node pose goes
        mx, my, myaw = ingest.map_pose(t, 3.0, 4.0, yaw)
        assert (out["x"], out["y"]) == pytest.approx((mx, my))
        assert out["z"] == 0.05
        r2, p2, y2 = _quat_to_rpy(out)
        assert (r2, p2) == pytest.approx((roll, pitch))
        assert y2 == pytest.approx(myaw)
        assert sum(out[k] ** 2 for k in ("qx", "qy", "qz", "qw")) == pytest.approx(1.0)

    def test_normalizes_and_rejects_zero_quaternion(self):
        out = ingest.pose3d_map(map_geo.IDENTITY, {"x": 0, "y": 0, "z": 0, "qx": 0, "qy": 0,
                                                   "qz": 0, "qw": 2.0})
        assert out["qw"] == pytest.approx(1.0)
        with pytest.raises(ingest.DepthPayloadError):
            ingest.pose3d_map(map_geo.IDENTITY, {"x": 0, "y": 0, "z": 0, "qx": 0, "qy": 0,
                                                 "qz": 0, "qw": 0})


class TestPayload:
    @pytest.mark.parametrize("patch_", [
        {"depth_data": None}, {"camera_name": ""}, {"camera": None},
        {"depth_encoding": "f32_m"}, {"content_type": "image/jpeg"}, {"depth_scale": 0},
        {"robot_pose3d": {"x": 1}}, {"camera_name": "../x"},
    ])
    def test_invalid(self, patch_):
        with pytest.raises(ingest.DepthPayloadError):
            ingest.check_depth_payload({**DEPTH, **patch_})

    def test_record(self):
        session = ingest.OpenSession("s1", "yard", False,
                                     {"tx": 1.0, "ty": 0.0, "yaw": 0.0}, "ALIVE", "mapping")
        rec = ingest.depth_record(DEPTH, session)
        assert rec["camera"] == CAMERA and rec["depth_scale"] == 0.001
        assert rec["session_id"] == "s1" and rec["depth_stamp_ms"] == 1727600000123
        assert rec["robot_pose3d"]["x"] == 1.2 and rec["pose3d_map"]["x"] == pytest.approx(2.2)

    def test_record_without_pose3d(self):
        session = ingest.OpenSession("s1", "yard", False, dict(map_geo.IDENTITY), "ALIVE",
                                     "mapping")
        rec = ingest.depth_record({**DEPTH, "robot_pose3d": None}, session)
        assert "pose3d_map" not in rec and "robot_pose3d" not in rec


class TestDepthIngest:
    async def test_depth_before_node_is_buffered_then_stored(self):
        t = {"tx": 10.0, "ty": 0.0, "yaw": math.pi / 2}
        service = _service(_row(t=t))
        await service._handle_depth_upload(dict(DEPTH))
        assert ("r1", 7) in service.depth_buffer and service.stats["buffered_depth"] == 1
        service.image_db.store_depth.assert_not_called()
        await service._handle_node_update(dict(NODE))
        node_id = service.graph_db.add_node.call_args.kwargs["node_id"]
        png, nid, cam, map_id = service.image_db.store_depth.call_args.args
        assert (png, nid, cam, map_id) == (PNG, node_id, "left", "yard")
        m, nid2, cam2, rec = service.graph_db.set_node_depth.call_args.args
        assert (m, nid2, cam2) == ("yard", node_id, "left")
        assert rec["pose3d_map"]["x"] == pytest.approx(10.0 + 0.4)
        assert rec["pose3d_map"]["y"] == pytest.approx(1.2)
        assert rec["pose3d_map"]["z"] == 0.02
        assert service.depth_buffer == {} and service.stats["buffered_depth"] == 0
        assert service.stats["depth_saved"] == 1

    async def test_depth_after_node(self):
        service = _service()
        await service._handle_node_update(dict(NODE))
        await service._handle_depth_upload(dict(DEPTH))
        node_id = service.graph_db.add_node.call_args.kwargs["node_id"]
        assert service.image_db.store_depth.call_args.args[1] == node_id
        service.graph_db.set_node_depth.assert_called_once()
        assert service.stats["depth_saved"] == 1

    async def test_node_is_stored_before_its_depth_parameters(self):
        service = _service()
        order = []
        service.graph_db.add_node.side_effect = lambda **kw: order.append("node") or True
        service.graph_db.set_node_depth.side_effect = lambda *a: order.append("depth") or True
        await service._handle_depth_upload(dict(DEPTH))
        await service._handle_node_update(dict(NODE))
        assert order == ["node", "depth"]

    async def test_failed_png_does_not_touch_the_node(self):
        service = _service()
        service.image_db.store_depth.return_value = False
        await service._handle_node_update(dict(NODE))
        await service._handle_depth_upload(dict(DEPTH))
        service.graph_db.set_node_depth.assert_not_called()
        assert service.stats["depth_saved"] == 0

    async def test_rejected_depth(self):
        service = _service(_row(paused=True))
        await service._handle_depth_upload(dict(DEPTH))
        assert service.depth_buffer == {} and service.stats["depth_rejected"] == 1
        event = service._write_event.await_args.args[0]
        assert event.code == EventCode.MAP_INGEST_REJECTED
        assert event.payload["dropped_depth"] == 1 and event.payload["dropped_images"] == 0
        assert build_row(event, strict=True)["payload"]["dropped_depth"] == 1

    async def test_rejected_node_drops_its_buffered_depth(self):
        service = _service()
        await service._handle_depth_upload(dict(DEPTH))
        service.sessions = ingest.SessionResolver(lambda _r: _none(), ttl=0)
        await service._handle_node_update(dict(NODE))
        assert service.depth_buffer == {} and service.stats["buffered_depth"] == 0
        assert service.rejects.dropped["depth"] == 1
        service.image_db.store_depth.assert_not_called()

    async def test_depth_from_another_session_is_discarded(self):
        service = _service()
        await service._handle_depth_upload(dict(DEPTH))
        entry, at = service.depth_buffer[("r1", 7)]["left"]
        entry["session_id"] = "s-old"
        await service._handle_node_update(dict(NODE))
        service.image_db.store_depth.assert_not_called()

    async def test_timed_out_depth_is_discarded(self):
        service = _service()
        await service._handle_depth_upload(dict(DEPTH))
        entry, _ = service.depth_buffer[("r1", 7)]["left"]
        service.depth_buffer[("r1", 7)]["left"] = (
            entry, datetime.datetime.now() - datetime.timedelta(seconds=3600))
        await service._handle_node_update(dict(NODE))
        service.image_db.store_depth.assert_not_called()
        assert service.stats["buffered_depth"] == 0

    async def test_invalid_payload_is_an_error_not_a_rejection(self):
        service = _service()
        await service._handle_depth_upload({**DEPTH, "depth_encoding": "f32"})
        assert service.stats["errors"] == 1 and service.stats["depth_rejected"] == 0
        assert service.depth_buffer == {}

    async def test_old_robot_without_depth_is_unchanged(self):
        service = _service()
        await service._handle_image_upload(dict(IMAGE))
        await service._handle_node_update(dict(NODE))
        service.image_db.store_image.assert_called_once()
        service.image_db.store_depth.assert_not_called()
        service.graph_db.set_node_depth.assert_not_called()
        assert "depth" not in service.graph_db.add_node.call_args.kwargs["metadata"]

    async def test_cleanup_and_session_reset_clear_the_depth_buffer(self):
        service = _service()
        await service._handle_depth_upload(dict(DEPTH))
        service._cleanup_old_mappings(threshold_seconds=-1)
        assert service.depth_buffer == {} and service.stats["buffered_depth"] == 0
        await service._handle_depth_upload(dict(DEPTH))
        service.session_to_global_map[("r1", 50)] = ("g", datetime.datetime.now(), "yard",
                                                     "s1")
        service._detect_and_clear_session_reset("r1", 1)
        assert service.depth_buffer == {} and service.stats["buffered_depth"] == 0

    def test_subscribes_to_the_depth_topic(self):
        service = _service()
        with patch("packages.services.graph_builder.server.MQTTClient") as client:
            assert service.connect_mqtt()
        topics = [c.args[0] for c in client.return_value.register_callback.call_args_list]
        assert "robot/depth_upload" in topics


async def _none():
    return None


class TestStores:
    def test_set_node_depth_merges_one_camera(self):
        svc = GraphDatabaseService.__new__(GraphDatabaseService)
        svc.logger = Mock()
        svc.db = Mock()
        svc.db.has_collection.return_value = True
        svc.db.aql.execute.return_value = iter(["n1"])
        assert svc.set_node_depth("yard", "n1", "left", {"depth_scale": 0.001})
        aql, = svc.db.aql.execute.call_args.args
        bind = svc.db.aql.execute.call_args.kwargs["bind_vars"]
        assert "MERGE(d.depth || {}" in aql and "mergeObjects: false" in aql
        assert bind == {"@col": "nodes_yard", "key": "n1", "camera": "left",
                        "record": {"depth_scale": 0.001}}
        svc.db.aql.execute.return_value = iter([])
        assert not svc.set_node_depth("yard", "missing", "left", {})
        svc.db.has_collection.return_value = False
        assert not svc.set_node_depth("gone", "n1", "left", {})

    @patch("packages.topomap_dbs.minio_base.Minio")
    def test_store_depth_key_and_content_type(self, minio):
        svc = ImageDatabaseService()
        minio.return_value.bucket_exists.return_value = True
        assert svc.store_depth(PNG, "n1", "left", "yard", metadata={"x": 1, "y": None})
        args, kw = minio.return_value.put_object.call_args
        assert args[:2] == ("map-yard", "n1/depth/left.png")
        assert kw["content_type"] == "image/png" and kw["metadata"] == {"x": "1",
                                                                         "node_id": "n1"}

    @patch("packages.topomap_dbs.minio_base.Minio")
    def test_get_stats_ignores_reconstruction_and_counts_depth(self, minio):
        names = ["n1/images/left", "n1/depth/left.png", "n2/images/left",
                 "reconstruction/job/cloud.ply", "reconstruction/job/meta.json"]
        minio.return_value.bucket_exists.return_value = True
        minio.return_value.list_objects.return_value = [Mock(object_name=n) for n in names]
        stats = ImageDatabaseService().get_stats(map_id="yard")
        assert stats["node_count"] == 2 and stats["image_count"] == 2
        assert stats["depth_count"] == 1
