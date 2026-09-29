"""scripts/export_map.py: the training export of one map (fakes for the stores)."""
import datetime
import json
import os

import pytest

from scripts import export_map as ex

pytestmark = pytest.mark.unit

T0 = datetime.datetime(2026, 10, 1, 12, 0, tzinfo=datetime.timezone.utc)
CAM = {"frame_id": "camera", "width": 448, "height": 336, "fx": 300.0, "fy": 301.0,
       "cx": 224.0, "cy": 168.0, "distortion_model": "plumb_bob", "d": [0, 0, 0, 0, 0],
       "depth_type": "z", "valid_range_m": [0.2, 15.0], "rgb_width": 448, "rgb_height": 336,
       "T_base_cam": {"x": 0.1, "y": 0, "z": 0.45, "qx": -0.5, "qy": 0.5, "qz": -0.5,
                      "qw": 0.5}}
P3 = {"x": 1.0, "y": 2.0, "z": 0.02, "qx": 0, "qy": 0.01, "qz": 0.1, "qw": 0.99}


class FakeStores:
    def __init__(self):
        self.nodes_ = [
            {"_key": "b", "node_id": "b", "pose": {"x": 1.0, "y": 2.0, "yaw": 0.5},
             "robot_pose": {"x": 9, "y": 9, "yaw": 9}, "created_at": "2026-10-01T10:00:02",
             "robot_name": "r1", "session_id": "s1", "session_node_id": 2,
             "depth": {"left": {"camera": CAM, "depth_scale": 0.001, "depth_encoding": "u16_mm",
                                "depth_stamp_ms": 5, "rgb_stamp_ms": 4, "pose3d_map": P3}}},
            {"_key": "a", "node_id": "a", "pose": {"x": 0.0, "y": 0.0, "yaw": 0.0},
             "created_at": "2026-10-01T10:00:01", "robot_name": "r1", "session_node_id": 1},
        ]
        self.edges_ = [{"_from": "nodes_Hospital/a", "_to": "nodes_Hospital/b",
                        "metadata": {"distance": 2.2}},
                       {"_from": "nodes_Hospital/b", "_to": "nodes_Hospital/gone"}]
        self.blobs = {"a/images/left": b"jpeg-a", "b/images/left": b"jpeg-b",
                      "b/depth/left.png": b"\x89PNG-depth-b",
                      "reconstruction/j/cloud.ply": b"ply"}
        self.fetched = []

    def resolve(self, name):
        assert name.lower() == "hospital"
        return "Hospital"

    def nodes(self, name):
        return self.nodes_

    def edges(self, name):
        return self.edges_

    def objects(self, name):
        return {k: len(v) for k, v in self.blobs.items()}

    def fetch(self, name):
        def get(key):
            self.fetched.append(key)
            return self.blobs[key]
        return get

    def map_spec(self, name):
        return {"type": "geo", "geo": {"utm_zone": 34, "utm_north": True, "origin_e": 1.0,
                                       "origin_n": 2.0}}


def _read(path):
    with open(path) as f:
        return json.load(f)


def test_export_layout_and_contents(tmp_path):
    stores = FakeStores()
    summary = ex.export(stores, "hospital", str(tmp_path), now=T0)
    out = tmp_path / "Hospital"
    assert summary["nodes"] == 2 and summary["nodes_with_depth"] == 1 and summary["edges"] == 1
    assert (out / "rgb/a.jpg").read_bytes() == b"jpeg-a"
    assert (out / "depth/b.png").read_bytes() == b"\x89PNG-depth-b"  # byte for byte
    assert not (out / "depth/a.png").exists()
    m = _read(out / "map.json")
    assert m["name"] == m["id"] == "Hospital" and m["type"] == "geo"
    assert m["crs"] == {"utm_zone": 34, "utm_north": True, "origin_e": 1.0, "origin_n": 2.0}
    n = _read(out / "nodes.json")
    assert [x["id"] for x in n["nodes"]] == ["a", "b"]  # capture order
    a, b = n["nodes"]
    assert a["depth"] is None and a["pose3d_map"] is None and a["rgb"] == "rgb/a.jpg"
    assert b["pose"] == {"x": 1.0, "y": 2.0, "yaw": 0.5}  # map frame, never robot_pose
    assert b["pose3d_map"] == P3
    d = b["depth"]
    assert d["file"] == "depth/b.png" and d["K"] == [[300.0, 0.0, 224.0], [0.0, 301.0, 168.0],
                                                      [0.0, 0.0, 1.0]]
    assert (d["width"], d["height"], d["depth_scale"], d["units"]) == (448, 336, 0.001, "mm")
    assert d["T_base_cam"] == CAM["T_base_cam"] and d["depth_stamp_ms"] == 5
    assert n["edges"] == [{"from": "a", "to": "b", "distance": 2.2}]
    assert "reconstruction/j/cloud.ply" not in stores.fetched


def test_rerun_is_incremental(tmp_path):
    stores = FakeStores()
    ex.export(stores, "Hospital", str(tmp_path), now=T0)
    stores.fetched.clear()
    summary = ex.export(stores, "Hospital", str(tmp_path), now=T0)
    assert summary["files_copied"] == 0 and summary["files_skipped"] == 3
    assert stores.fetched == []
    stores.blobs["a/images/left"] = b"jpeg-a-longer"  # changed size -> fetched again
    assert ex.export(stores, "Hospital", str(tmp_path), now=T0)["files_copied"] == 1
    assert not [p for p in os.listdir(tmp_path / "Hospital" / "rgb") if p.endswith(".part")]


def test_second_camera_files():
    nodes = [{"_key": "n", "pose": {"x": 0, "y": 0, "yaw": 0},
              "depth": {"left": {"camera": CAM}, "right": {"camera": CAM}}}]
    objects = {"n/images/left": 1, "n/images/right": 2, "n/depth/left.png": 3,
               "n/depth/right.png": 4}
    _m, nj, files = ex.build_export("x", nodes, [], objects, None, T0)
    assert sorted(f[1] for f in files) == ["depth/n.png", "depth/n_right.png", "rgb/n.jpg",
                                          "rgb/n_right.jpg"]
    node = nj["nodes"][0]
    assert node["depth"]["file"] == "depth/n.png" and len(node["cameras"]) == 2


def test_size_mismatch_is_an_error(tmp_path):
    with pytest.raises(IOError):
        ex.sync_files([("k", "rgb/x.jpg", 5)], str(tmp_path), lambda k: b"abc")


def test_compose_env(tmp_path):
    p = tmp_path / ".env"
    p.write_text("ARANGO_ROOT_PASSWORD=ap\nMINIO_ROOT_USER=mu\nMINIO_ROOT_PASSWORD='mp'\n"
                 "# c\nPOSTGRES_DATABASE_NAME=mission\nGRAPH_DB_NAME=topo\n")
    env = ex.compose_env(str(p))
    assert env == {"ARANGO_PASSWORD": "ap", "MINIO_ACCESS_KEY": "mu", "MINIO_SECRET_KEY": "mp",
                   "POSTGRES_DATABASE_NAME": "mission", "DATABASE_NAME": "topo"}
