"""Corridor width review (AUDIT_BACKLOG J.8): the skip threshold, measured width 0, stale measured corridors,
one `total`, and one route per leg. Contract: docs/node_policies_v2_plan.md."""
import math
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cloud_common import node_policy as rules
from cloud_common.objects import common, mission, robot
from packages import config
from packages.api import route_preview
from packages.controllers.mission import order_policy, order_start as os_
from packages.controllers.mission.vda5050_types import vda5050_types as v
from packages.scene_graph.api import Route
from packages.services.mission_planner import node_policy_plan as npp
from tests.unit.test_node_policy_plan import StripGraph, _poses

CAP = config.NODE_POLICY_CORRIDOR_CAP_M
MARGIN = config.NODE_POLICY_LOCALISATION_MARGIN_M


# ---- corridor_per_side, sanitize_width ---------------------------------------------------------
@pytest.mark.unit
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_corridor_per_side_is_unknown_for_a_non_finite_width(bad):
    assert npp.corridor_per_side(bad, 0.6) is None
    assert npp.corridor_per_side(1.6, bad) is None
    assert npp.corridor_per_side(1.6, 0.6, bad) is None


@pytest.mark.unit
def test_sanitize_width_keeps_zero_and_drops_the_unusable():
    assert rules.sanitize_width(0) == 0.0
    assert rules.sanitize_width(1.2) == 1.2
    for bad in (None, float("nan"), float("inf"), -0.1, "x"):
        assert rules.sanitize_width(bad) is None


# ---- item 2: a measured min_width of 0 is "no room", not "unknown" -----------------------------------
@pytest.mark.unit
def test_a_measured_width_of_zero_gives_corridor_zero():
    graph = StripGraph(width_at=lambda x: 0.0)
    poses = _poses(1.0, 3.0, 5.0)
    rep = npp.annotate_corridors(graph, poses, [0, 1], 0.6, 0.05, 0.3)
    assert [p.corridor_width_m for p in poses[:2]] == [0.0, 0.0]
    assert rep["corridor"]["narrow"] == 2 and rep["corridor"]["unknown"] == 0
    assert rep["unavailable"] is None and [p.skippable for p in poses[:2]] == [False, False]
    routes = npp.LegRoutes()
    npp._leg_width(graph, (1.0, 0.05), (3.0, 0.05), 0.3, routes=routes)
    assert routes.widths[((1.0, 0.05), (3.0, 0.05))] == 0.0


# ---- item 1: skip threshold ---------------------------------------------------------------------
@pytest.mark.unit
def test_skip_allowed_needs_a_corridor_above_the_threshold():
    assert config.NODE_POLICY_SKIP_MIN_CORRIDOR_M == MARGIN == 0.10
    assert not rules.skip_allowed(None) and not rules.skip_allowed(0.0)
    assert not rules.skip_allowed(0.05) and not rules.skip_allowed(0.10)
    assert rules.skip_allowed(0.11) and rules.skip_allowed(1.5)
    assert not rules.skip_allowed(float("nan")) and not rules.skip_allowed(float("inf"))
    assert rules.skip_allowed(0.2, threshold=0.15) and not rules.skip_allowed(0.15, threshold=0.15)


@pytest.mark.unit
def test_a_narrow_corridor_is_sent_as_gate_but_not_skippable_end_to_end():
    # width 0.8, robot 0.6, margin 0.05 -> (0.8 - 0.65) / 2 = 0.075 : in (0, 0.10]
    graph = StripGraph(width_at=lambda x: 0.8)
    poses = _poses(1.0, 3.0, 5.0)
    rep = npp.annotate_corridors(graph, poses, [0, 1], 0.6, 0.05, 0.3)
    assert poses[0].corridor_width_m == 0.07 and poses[0].skippable is False
    assert rep["skippable"] == 0 and rep["corridor"]["measured"] == 2
    # the wire
    action = v.VDA5050Action.node_policy("n", 10, skippable=True, corridor_width_m=0.07)
    assert action.param_dict["skippable"] is False and action.param_dict["corridorWidth"] == 0.07
    assert v.VDA5050Action.node_policy("n", 10, skippable=True, corridor_width_m=0.11).param_dict["skippable"] is True
    assert v.VDA5050Action.node_policy("n", 10, skippable=True, corridor_width_m=0.10).param_dict["skippable"] is False
    # the record names it
    wps = [common.Pose2D(x=0.0, y=0.0, map_id="m", origin="planned", skippable=True, corridor_width_m=0.07),
           common.Pose2D(x=1.0, y=0.0, map_id="m", origin="planned", skippable=True, corridor_width_m=0.5),
           common.Pose2D(x=2.0, y=0.0, map_id="m", origin="user")]
    policy = order_policy.OrderPolicy()
    order = v.VDA5050Order.from_route(mission.MissionRouteNodeV1(waypoints=wps),
                                      robot.RobotObjectV1(name="r1", spec={}, status={}), "m1", 3,
                                      node_policy=True, policy=policy)
    rec = v.describe_node_policies(order, wps, policy)
    assert [n["reason"] for n in rec] == ["corridor_below_margin", "ok", "structural_last"]
    assert rec[0]["corridor"] == 0.07 and rec[0]["skippable"] is False


@pytest.mark.unit
def test_the_usable_corridor_matches_what_the_robot_can_pass():
    """A skippable corridor is one is_passed can use; one at or under the threshold is not."""
    wps = [common.Pose2D(x=0, y=0, corridor_width_m=0.0), common.Pose2D(x=8, y=0), common.Pose2D(x=9, y=0)]
    for c, usable in ((0.08, False), (0.10, False), (0.15, True)):
        wps[0].corridor_width_m = c
        passed = os_.is_passed(wps, 0, 1.5, 0.03, 0.05)       # 0.03 m off the line, past the node
        assert passed is usable and rules.skip_allowed(c) is usable


# ---- item 5: cap, clamp ------------------------------------------------------------------------------
@pytest.mark.unit
def test_an_operator_corridor_above_the_cap_is_capped_silently_and_stays_operator():
    poses = _poses(1.0, 3.0, 5.0)
    poses[1].corridor_width_m = 3.0
    npp.annotate_corridors(StripGraph(), poses, [1], 0.6, 0.05, 0.3)
    assert poses[1].corridor_width_m == CAP and poses[1].corridor_source == common.CORRIDOR_SOURCE_OPERATOR
    assert poses[1].skippable is True


@pytest.mark.unit
def test_clamp_corridor_m():
    c = order_policy.clamp_corridor_m
    assert c("0.4") == 0.4 and c(0) == 0.0 and c(9) == CAP
    assert c(None) is None and c("x") is None and c(float("inf")) is None and c(float("nan")) is None
    assert c(-1) == 0.0 and c(float("-inf")) is None


@pytest.mark.unit
def test_is_passed_uses_the_capped_corridor_and_the_margin():
    wps = [common.Pose2D(x=0, y=0, corridor_width_m=9.0), common.Pose2D(x=8, y=0), common.Pose2D(x=9, y=0)]
    assert os_.is_passed(wps, 0, 1.0, CAP - MARGIN - 0.01, 0.05) is True
    assert os_.is_passed(wps, 0, 1.0, CAP - MARGIN + 0.05, 0.05) is False   # 9 m is capped to 1.5 first
    wps[0].corridor_width_m = 0.0
    assert os_.is_passed(wps, 0, 1.0, 0.0, 0.05) is False                   # no room: distance rule only
    wps[0].corridor_width_m = None                                          # default 0.75
    assert os_.is_passed(wps, 0, 1.0, 0.6, 0.05) is True


# ---- item 3: stale measured corridors ------------------------------------------------------------------
def _stale():
    return [dict(x=0.0, y=0.0, corridor_width_m=0.9, corridor_source="measured"),
            dict(x=1.0, y=0.0, corridor_width_m=0.0, corridor_source="measured"),
            dict(x=2.0, y=0.0, corridor_width_m=0.7, corridor_source="operator"),
            dict(x=3.0, y=0.0, corridor_width_m=0.4),
            dict(x=4.0, y=0.0)]


@pytest.mark.unit
def test_clear_stale_measured_keeps_zero_and_operator_values():
    wps = _stale()
    assert rules.clear_stale_measured(wps) == 1
    assert [w.get("corridor_width_m") for w in wps] == [None, 0.0, 0.7, 0.4, None]
    assert wps[0]["corridor_source"] is None and wps[1]["corridor_source"] == "measured"
    poses = [common.Pose2D(**{"map_id": "m", **w}) for w in _stale()]
    rules.clear_stale_measured(poses)
    assert poses[0].corridor_width_m is None and poses[1].corridor_width_m == 0.0


@pytest.mark.unit
def test_measure_reroute_clears_stale_corridors_on_every_unmeasured_path():
    route = {"waypoints": [{**w, "map_id": "m"} for w in _stale()]}
    down = SimpleNamespace(enrich_route=AsyncMock(side_effect=RuntimeError("down")))
    wrong = SimpleNamespace(enrich_route=AsyncMock(return_value={"waypoints": []}))
    import asyncio
    for planner in (down, wrong, None):
        out = asyncio.run(route_preview.measure_reroute_detailed(planner, "r1", {"n": route}))[0]
        got = [w.get("corridor_width_m") for w in out["n"]["waypoints"]]
        assert got == [None, 0.0, 0.7, 0.4, None]
    assert route["waypoints"][0]["corridor_width_m"] == 0.9            # the caller's input is not changed


@pytest.mark.unit
async def test_preview_answer_without_a_planner_has_no_stale_corridor_and_one_total():
    raw = [{"x": 0, "y": 0, "skippable": True, "corridor_width_m": 0.5},
           {"x": 1, "y": 0, "skippable": True}, {"x": 2, "y": 0}]
    out = await route_preview.preview(None, "m", raw)
    assert out["node_policy"]["total"] == 2                    # every skippable-asking waypoint but the last, corridor or not


# ---- item 5: total ------------------------------------------------------------------------------------
@pytest.mark.unit
def test_total_is_the_same_count_on_every_path():
    poses = _poses(1.0, 3.0, 5.0, 7.0)
    poses[0].skippable = True
    poses[0].corridor_width_m = 0.0                           # `not width` used to count this one, a measured 0, differently
    poses[1].skippable = True
    poses[1].corridor_width_m = 0.5
    poses[3].skippable = True                                 # the last: never
    assert rules.skippable_candidates(poses) == [0, 1]
    as_dicts = [p.dict() for p in poses]
    rep = npp.annotate_corridors(StripGraph(), poses, rules.skippable_candidates(poses), 0.6, 0.05, 0.3)
    assert rep["total"] == len(rules.skippable_candidates(as_dicts)) == 2


# ---- item 4: blocked is passed the same way to both routings --------------------------------------------
class BlockedAwareGraph(StripGraph):
    """A strip whose passage is 0.4 m with keep-out cells and 1.6 m without."""
    def route(self, a, b, blocked=None, start=None, goal=None, **kw):
        self.last_blocked = blocked
        r = super().route(a, b, blocked, start, goal)
        return Route(charts=r.charts, length=r.length, min_width=0.4 if blocked else 1.6, nodes=r.nodes)


@pytest.mark.unit
def test_leg_width_with_and_without_blocked():
    graph, blocked = BlockedAwareGraph(), object()             # truthy, opaque
    assert npp._leg_width(graph, (1.0, 0.05), (3.0, 0.05), 0.3) == 1.6
    assert graph.last_blocked is None
    assert npp._leg_width(graph, (1.0, 0.05), (3.0, 0.05), 0.3, blocked=blocked) == 0.4
    assert graph.last_blocked is blocked


@pytest.mark.unit
def test_a_leg_has_the_same_width_whichever_pass_routed_it_first():
    blocked = object()
    # insert_planned first (fills the cache with the blocked route), then annotate: same width as annotate alone
    a, b = BlockedAwareGraph(), BlockedAwareGraph()
    cached = npp.LegRoutes()
    poses_a = _poses(1.0, 3.0, 5.0)
    npp.insert_planned(a, poses_a, lambda p, q: True, 5.0, 0.3, lambda n, v_, g: [n[0], n[-1]], routes=cached, blocked=blocked)
    npp.annotate_corridors(a, poses_a, [0, 1], 0.6, 0.05, 0.3, routes=cached, blocked=blocked)
    poses_b = _poses(1.0, 3.0, 5.0)
    npp.annotate_corridors(b, poses_b, [0, 1], 0.6, 0.05, 0.3, blocked=blocked)       # no cache
    assert [p.corridor_width_m for p in poses_a[:2]] == [p.corridor_width_m for p in poses_b[:2]]
    assert poses_b[0].corridor_width_m == 0.0                                           # 0.4 wide with keep-out: no room


@pytest.mark.unit
def test_route_leg_reports_why_it_failed():
    class Cut(StripGraph):
        def route(self, a, b, blocked=None, **kw):
            return None if blocked else super().route(a, b, blocked, **kw)
    _, _, route, why = npp._route_leg(Cut(), (1.0, 0.05), (3.0, 0.05), 0.3, blocked=object())
    assert route is None and why == "blocked_by_keepout"
    _, _, route, why = npp._route_leg(StripGraph(), (50.0, 5.0), (3.0, 0.05), 0.3)
    assert route is None and why == "outside_graph"
