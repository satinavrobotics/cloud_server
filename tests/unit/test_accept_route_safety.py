"""The acceptance harness (tools/scene_graph_sim/accept_route_safety.py) runs on a synthetic export and passes its gates."""
import datetime
import importlib.util
import json
import os
import sys

import pytest
from PIL import Image

from .scene_graph import chart_worlds as cw

pytestmark = pytest.mark.unit

TOOLS = os.path.join(os.path.dirname(__file__), "..", "..", "tools", "scene_graph_sim")


def _export(path, world_way):
    w, way = world_way
    nodes = []
    for i, lf in enumerate(cw.drive(w, way)):
        Image.fromarray(lf.window_u8).save(os.path.join(path, "%s.png" % lf.key))
        nodes.append({"_key": lf.key, "session_id": lf.session, "session_node_id": i,
                      "capture_time": (datetime.datetime(2026, 10, 13) + datetime.timedelta(seconds=i)).isoformat(),
                      "pose": {"x": lf.x, "y": lf.y, "yaw": 0.0},
                      "costmap": {"occupancy": {"resolution": lf.resolution,
                                                "origin_map": {"x": lf.origin[0], "y": lf.origin[1], "yaw": 0.0}}}})
    with open(os.path.join(path, "nodes.json"), "w") as f:
        json.dump(nodes, f)


def _load():
    spec = importlib.util.spec_from_file_location("accept_route_safety", os.path.join(TOOLS, "accept_route_safety.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["accept_route_safety"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("world", ["room_loop", "ring"])
def test_the_harness_passes_every_gate_on_a_synthetic_export(tmp_path, monkeypatch, capsys, world):
    _export(str(tmp_path), getattr(cw, world)())
    mod = _load()
    monkeypatch.setattr(sys, "argv", ["accept", "--data", str(tmp_path), "--pairs", "12", "--seed", "3"])
    code = mod.main()
    out = capsys.readouterr().out
    assert code == 0, out
    assert "ALL GATES PASS" in out
    for gid in ("A1", "A2", "A3", "A4", "A5", "A6", "A7", "B1", "B2", "C1", "C2", "C3"):
        assert any(line.startswith(gid + " ") and "PASS" in line for line in out.splitlines()), (gid, out)
