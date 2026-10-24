"""The mission_update WebSocket message has one builder (live broadcast and connect snapshot)."""
import json

import pytest

from cloud_common.objects.mission import MissionObjectV1
from packages.api.server import build_mission_update

pytestmark = pytest.mark.unit


def test_mission_update_carries_progress_and_hold_fields():
    mission = MissionObjectV1(
        name="m1", robot="r1", mission_tree=[{"sequence": {}}],
        status={"held": True, "held_reason": "Robot is offline",
                "task_status": {"route": 2},
                "node_status": {"route": {"state": "RUNNING", "error_msg": "x"}}})
    msg = build_mission_update(mission)
    st = msg["status"]
    assert st["held"] is True and st["held_reason"] == "Robot is offline"
    assert st["task_status"] == {"route": 2}
    assert st["node_status"]["root"] == {"state": "PENDING", "error_msg": None}
    assert msg["type"] == "mission_update" and msg["robot_name"] == "r1"
    json.dumps(msg)  # what send_json does: must be serialisable
