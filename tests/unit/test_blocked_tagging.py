"""Blocked-report tagging: chart keys (read time), run id of a report, overlay half width from the
robot footprint (packages/utils/blocked_geometry.py, blocked_nodes.py)."""
import uuid

from packages.controllers.mission import fleet_recorder, order_ids
from packages.utils import blocked_geometry as bg
from packages.utils import blocked_nodes as bn


def locate_by_x(x, y):
    """Charts as vertical bands: A for x < 4, B for 4 <= x < 8, C beyond; None for y < 0."""
    if y < 0:
        return None
    return "A:0" if x < 4 else ("B:1" if x < 8 else "C:2")


# --- chart keys -------------------------------------------------------------------------------
def test_leg_chart_keys_lists_charts_in_travel_order_without_repeats():
    legs = bg.leg_chart_keys([(0, 0), (6, 0), (12, 0)], locate_by_x, step_m=0.5)
    assert legs == [["A:0", "B:1"], ["B:1", "C:2"]]


def test_leg_chart_keys_outside_every_chart_is_empty():
    assert bg.leg_chart_keys([(0, -1), (6, -1)], locate_by_x) == [[]]


def test_row_chart_keys_follow_the_obstacle_segment_of_a_posed_row():
    row = {"x": 10.0, "y": 0.0, "robot_x": 0.0, "robot_y": 0.0}      # segment from 1 m ahead to x=10
    assert bg.row_chart_keys(row, locate_by_x) == ["A:0", "B:1", "C:2"]


def test_legacy_row_without_pose_maps_to_the_chart_of_its_waypoint():
    assert bg.row_chart_keys({"x": 5.0, "y": 1.0}, locate_by_x) == ["B:1"]
    assert bg.row_chart_keys({"graph_node_id": "@9.00,1.00"}, locate_by_x) == ["C:2"]
    assert bg.row_chart_keys({"graph_node_id": "leaf-1"}, locate_by_x) == []     # no position at all


# --- run id -----------------------------------------------------------------------------------
def _row(mission, prefix, seq=2):
    return {"mission_name": mission, "vda_node_id": order_ids.node_id(prefix, 0, seq),
            "graph_node_id": "n1"}


def test_run_id_is_the_mission_run_uuid_of_the_dispatcher_run():
    prefix = order_ids.run_prefix("goto-1", "0a1b2c3d")
    assert bn.run_id_of(_row("goto-1", prefix)) == str(fleet_recorder.run_uuid("goto-1", "0a1b2c3d"))


def test_run_id_is_stable_across_reroutes_and_sequences():
    a = bn.run_id_of(_row("m", order_ids.run_prefix("m", "0a1b2c3d", 0), 2))
    b = bn.run_id_of(_row("m", order_ids.run_prefix("m", "0a1b2c3d", 3), 8))
    assert a == b and uuid.UUID(a)


def test_run_id_differs_between_runs_and_missions_and_survives_dashes_in_names():
    assert bn.run_id_of(_row("m", order_ids.run_prefix("m", "0a1b2c3d"))) != \
        bn.run_id_of(_row("m", order_ids.run_prefix("m", "ffffffff")))
    name = "go-r12345678-to-dock"
    assert bn.run_id_of(_row(name, order_ids.run_prefix(name, "0a1b2c3d"))) == \
        str(fleet_recorder.run_uuid(name, "0a1b2c3d"))


def test_run_id_none_for_legacy_or_foreign_rows():
    assert bn.run_id_of(_row("m", "m")) is None                       # mission started before run ids
    assert bn.run_id_of({"graph_node_id": "n1"}) is None
    assert bn.run_id_of({"mission_name": "other", "vda_node_id": "m-r0a1b2c3d-n0-s2"}) is None
    assert bn.run_id_of({"mission_name": "m", "vda_node_id": "robot-reported"}) is None


def test_row_dict_carries_run_id():
    values = {c: None for c in bn.COLUMNS}
    values.update(mission_name="m", vda_node_id="m-r0a1b2c3d-n0-s2", graph_node_id="n1")
    out = bn.row_dict([values[c] for c in bn.COLUMNS])
    assert out["run_id"] == str(fleet_recorder.run_uuid("m", "0a1b2c3d"))
    assert bn.row_dict([None] * len(bn.COLUMNS))["run_id"] is None


# --- half width from the footprint ------------------------------------------------------------
def test_half_width_is_half_the_smaller_side():
    assert bg.half_width_for(1.2, 0.6) == 0.3
    assert bg.half_width_for(0.4, 0.9) == 0.2


def test_half_width_falls_back_to_the_constant_when_unknown():
    for length, width in [(-1, -1), (1.0, -1), (None, 0.6), ("x", 0.6), (float("nan"), 0.6), (0, 0.5)]:
        assert bg.half_width_for(length, width) == bg.HALF_WIDTH_M
    assert bg.half_width_for(-1, -1, default=0.7) == 0.7


def test_lead_keeps_exceeding_half_width():
    assert bg.lead_for(0.3) == bg.LEAD_M
    assert bg.lead_for(0.9) > 0.9
    assert bg.widths(None) == (bg.HALF_WIDTH_M, bg.LEAD_M)


def test_overlay_uses_the_given_half_width_and_lead():
    row = {"x": 10.0, "y": 0.0, "robot_x": 0.0, "robot_y": 0.0}
    _, segs = bg.overlay([row], 0.5, half_width_m=0.3)
    assert segs == [((bg.LEAD_M, 0.0), (10.0, 0.0), 0.3)]
    _, wide = bg.overlay([row], 0.5, half_width_m=1.4)
    assert wide[0][2] == 1.4 and wide[0][0][0] == bg.lead_for(1.4) > 1.4


def test_narrow_robot_passes_where_default_width_is_blocked():
    row = {"graph_node_id": "n1", "x": 10.0, "y": 0.0, "robot_x": 0.0, "robot_y": 0.0}
    leg = [(-5.0, 0.4), (20.0, 0.4)]                     # 0.4 m beside the obstacle
    assert bn.rows_hit([row], [], polyline=leg)           # constant 0.5 m
    assert not bn.rows_hit([row], [], polyline=leg, half_width_m=0.3)
    assert bn.rows_hit([row], [], polyline=leg, half_width_m=0.45)
    crosses = bn.leg_crosser([row], half_width_m=0.3)
    assert not crosses(*leg)
    assert bn.leg_crosser([row])(*leg)


def test_planner_derives_overlay_half_width_from_the_robot_factsheet():
    from types import SimpleNamespace
    from packages.services.mission_planner.server import MissionPlannerService as S

    def robot(length, width):
        return SimpleNamespace(status=SimpleNamespace(
            factsheet=SimpleNamespace(length=length, width=width)))
    assert S._overlay_half_width(robot(1.0, 0.5)) == 0.25
    assert S._overlay_half_width(robot(-1, -1)) == bg.HALF_WIDTH_M
    assert S._overlay_half_width(None) == bg.HALF_WIDTH_M
