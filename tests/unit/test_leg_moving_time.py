"""Stage C of the edge statistics: driving time apart from waiting time (leg_tracker.py module docstring):
stopped_s / silent_s / moving_s of a leg, the run summary and the route-edit chart rule."""
from types import SimpleNamespace

import pytest

from cloud_common.objects import common
from cloud_common.objects import mission as mission_object
from packages.controllers.mission import leg_tracker
from packages.utils import run_legs
from tests.unit.test_leg_tracker_plan import WPS, at, make_mission, route, vda, wp

pytestmark = pytest.mark.unit


def feed_driving(tracker, mission, *steps):
    """steps: (seq, t, driving): lastNodeId of route node 0; the legs completed by the last step."""
    legs = []
    for seq, t, driving in steps:
        legs = tracker.observe(
            message=SimpleNamespace(lastNodeId=vda(mission, 0, seq), orderId=f"m1-rabcd1234-n0", driving=driving),
            mission=mission, ts=at(t), received=at(t), limits=None)
    return legs


def test_a_scripted_leg_splits_into_moving_stopped_and_silent_time_by_hand():
    mission = make_mission(route(WPS))
    tr = leg_tracker.LegTracker()
    assert leg_tracker.MAX_INTERVAL_S == 30.0
    # leg 1 starts when wp0 (seq 2) is reached at t=2; the intervals, by the PREVIOUS message's driving:
    legs = feed_driving(
        tr, mission,
        (0, 0, True), (2, 2, True),
        (2, 10, False),       # 2 -> 10  prev driving                          : moving 8
        (2, 20, False),       # 10 -> 20 prev not driving, dt 10               : stopped 10
        (2, 110, True),       # 20 -> 110 prev not driving, dt 90              : stopped 30 (cap), silent 60
        (2, 120, None),       # 110 -> 120 prev driving                        : moving 10 (a recovery maneuver
        (2, 140, True),       # 120 -> 140 prev driving=None counts as driving : moving 20  that reports driving)
        (4, 200, True))       # 140 -> 200 prev driving, dt 60                 : moving 30, silent 30
    [leg] = legs
    assert leg.duration_s == 198.0
    assert (leg.stopped_s, leg.silent_s, leg.moving_s) == (40.0, 90.0, 68.0)
    assert leg.stopped_s + leg.silent_s + leg.moving_s == leg.duration_s
    # the next leg starts from zero (stopped / silent restart with the leg)
    [leg2] = feed_driving(tr, mission, (6, 205, True))
    assert (leg2.duration_s, leg2.stopped_s, leg2.silent_s, leg2.moving_s) == (5.0, 0.0, 0.0, 5.0)


def test_a_robot_that_never_drives_has_no_moving_time_and_the_parts_add_up():
    mission = make_mission(route(WPS))
    # a silent robot that is never "driving": 100 s gap, not driving: stopped 30 + silent 70 = the whole leg
    [leg] = feed_driving(leg_tracker.LegTracker(), mission,
                         (0, 0, False), (2, 1, False), (4, 101, True))
    assert (leg.duration_s, leg.stopped_s, leg.silent_s, leg.moving_s) == (100.0, 30.0, 70.0, 0.0)


class TestSummary:
    def leg(self, seq, **extra):
        base = {"seq": seq, "pass_index": 0, "started_at": at(seq), "ended_at": at(seq + 1),
                "duration_s": 100.0, "stopped_s": 20.0, "straight_m": 3.0, "planned_m": 3.0}
        base.update(extra)
        return base

    def test_moving_uses_the_recorded_moving_s_and_falls_back_for_older_legs(self):
        legs = [self.leg(1, moving_s=50.0, silent_s=30.0), self.leg(2)]       # leg 2: recorded before stage C
        out = run_legs.summarize(legs, 300.0)
        assert out["time_moving_s"] == pytest.approx(50.0 + 80.0)
        assert out["time_silent_s"] == pytest.approx(30.0)
        assert out["time_stopped_s"] == pytest.approx(300.0 - 130.0)

    def test_recovery_time_is_not_subtracted_from_moving(self):
        # it overlaps stopped time (and a recovery maneuver may report driving true)
        out = run_legs.summarize([self.leg(1, moving_s=50.0, silent_s=30.0, recovery_s=40.0)], 100.0)
        assert out["time_moving_s"] == 50.0 and out["time_recovery_s"] == 40.0

    def test_old_legs_only_keep_the_old_figures(self):
        out = run_legs.summarize([self.leg(1)], 150.0)
        assert out["time_moving_s"] == 80.0 and out["time_silent_s"] == 0.0 and out["time_stopped_s"] == 70.0


# --- operator-supplied routes: no stale chart keys --------------------------------------------------------

def _route(points, version=7):
    return mission_object.MissionRouteNodeV1(
        waypoints=[common.Pose2D(x=x, y=y, map_id="m", chart_key=f"c{i}") for i, (x, y) in enumerate(points)],
        graph_version=version)


class TestStaleCharts:
    def test_a_moved_waypoint_loses_its_chart_and_the_route_its_graph_version(self):
        old = _route([(0, 0), (1, 0), (2, 0)])
        new = _route([(0, 0), (1, 5), (2, 0)])
        assert new.clear_stale_charts(old) is True
        assert [w.chart_key for w in new.waypoints] == ["c0", None, "c2"] and new.graph_version is None

    def test_an_unchanged_route_keeps_everything(self):
        new = _route([(0, 0), (1, 0)])
        assert new.clear_stale_charts(_route([(0, 0), (1, 0)])) is False
        assert [w.chart_key for w in new.waypoints] == ["c0", "c1"] and new.graph_version == 7

    @pytest.mark.parametrize("previous", [_route([(0, 0)]), None])
    def test_a_changed_count_or_no_previous_route_clears_all(self, previous):
        new = _route([(0, 0), (1, 0)])
        assert new.clear_stale_charts(previous) is True
        assert [w.chart_key for w in new.waypoints] == [None, None] and new.graph_version is None

    async def test_a_reroute_through_the_mission_clears_the_moved_waypoints(self):
        mission = make_mission(route([wp(0, 0, "c0"), wp(3, 0, "c1"), wp(3, 4, "c2")], graph_version=9))
        new = {"go0": route([wp(0, 0, "c0"), wp(3, 1, "c1"), wp(3, 4, "c2")], graph_version=9)}
        await mission.update(new)
        node = mission.mission_tree[0].route
        assert [w.chart_key for w in node.waypoints] == ["c0", None, "c2"] and node.graph_version is None


# --- through PUT /missions/{name} (the reroute and the edit of a PENDING mission) --------------------------

def _charted_tree():
    pts = [{"x": float(i), "y": 1.0, "theta": 0.0, "chart_key": f"c{i}"} for i in range(3)]
    return [{"name": "root_sequence", "parent": "root", "sequence": {}},
            {"name": "a", "parent": "root_sequence", "route": {"waypoints": pts, "graph_version": 4}}]


@pytest.mark.parametrize("state, body", [
    ("RUNNING", "reroute"), ("PENDING", "edit")])
async def test_the_api_clears_the_chart_of_a_waypoint_the_operator_moved(state, body):
    import json
    from unittest.mock import patch

    import packages.api.main as api_main
    from tests.unit.test_mission_lifecycle_fixes import State, _FakeMissionDb, _mission
    existing = _mission(tree=_charted_tree())
    existing.status.state = getattr(State, state)
    db = _FakeMissionDb(existing)
    moved = [{"x": 0.0, "y": 1.0, "theta": 0.0, "chart_key": "c0"},
             {"x": 1.0, "y": 9.0, "theta": 0.0, "chart_key": "c1"},       # moved
             {"x": 2.0, "y": 1.0, "theta": 0.0, "chart_key": "c2"}]
    if body == "reroute":
        request = {"update_nodes": {"a": {"waypoints": moved, "graph_version": 4}}}
    else:
        tree = _charted_tree()
        tree[1]["route"]["waypoints"] = moved
        request = {"mission_tree": tree}
    with patch.object(api_main, "service", SimpleNamespace(database=db)):
        await api_main.update_mission("m1", request)
        got = await db.get_object(api_main.MissionObjectV1, "m1")
    route_node = got.mission_tree[1].route
    assert [w.chart_key for w in route_node.waypoints] == ["c0", None, "c2"], json.dumps(request)[:80]
    assert route_node.graph_version is None


# --- headerId gaps (W2-1 / W2-2) ----------------------------------------------------------------------------

def feed_hdr(tracker, mission, *steps):
    """steps: (seq, t, driving, headerId); the legs completed by the last step."""
    legs = []
    for seq, t, driving, hid in steps:
        legs = tracker.observe(
            message=SimpleNamespace(lastNodeId=vda(mission, 0, seq), orderId="m1-rabcd1234-n0",
                                    driving=driving, headerId=hid),
            mission=mission, ts=at(t), received=at(t), limits=None)
    return legs


class TestHeaderIdGaps:
    def run(self, *steps):
        mission = make_mission(route(WPS))
        tr = leg_tracker.LegTracker()
        [leg] = feed_hdr(tr, mission, (0, 0, True, 1), (2, 2, True, 2), *steps)
        assert leg.stopped_s + leg.silent_s + leg.moving_s == pytest.approx(leg.duration_s)
        return leg, tr

    def test_consecutive_ids_keep_the_normal_rules(self):
        leg, tr = self.run((2, 3, False, 3), (2, 4, False, 4), (4, 5, True, 5))
        assert (leg.stopped_s, leg.silent_s, leg.moving_s) == (2.0, 0.0, 1.0)
        assert tr.header_gaps == 0

    def test_a_gap_of_three_ids_is_silent_not_stopped(self):
        leg, tr = self.run((2, 3, False, 3), (2, 8, False, 6), (4, 9, True, 7))
        assert (leg.stopped_s, leg.silent_s, leg.moving_s) == (1.0, 5.0, 1.0)
        assert tr.header_gaps == 1

    def test_a_gap_is_silent_even_after_a_driving_message(self):
        leg, _ = self.run((2, 10, True, 4), (4, 12, True, 5))      # id 3 lost: 8 s silent, not moving
        assert (leg.stopped_s, leg.silent_s, leg.moving_s) == (0.0, 8.0, 2.0)

    def test_a_coalesced_pair_shows_as_a_gap_of_one_missing_id(self):
        leg, tr = self.run((2, 3, False, 3), (2, 5, False, 5), (4, 6, True, 6))     # id 4 never processed
        assert (leg.stopped_s, leg.silent_s) == (1.0, 2.0)
        assert tr.header_gaps == 1

    def test_a_restart_counts_silent_and_resets_the_reference(self):
        leg, tr = self.run((2, 10, False, 900), (2, 20, False, 3), (2, 21, False, 4), (4, 22, True, 5))
        assert (leg.stopped_s, leg.silent_s, leg.moving_s) == (2.0, 18.0, 0.0)
        assert tr.header_gaps == 2

    def test_a_new_pass_has_no_reference(self):
        mission = make_mission(route(WPS))
        tr = leg_tracker.LegTracker()
        feed_hdr(tr, mission, (0, 0, True, 50), (2, 2, True, 51))
        mission.status.passes_completed += 1
        [leg] = feed_hdr(tr, mission, (0, 100, True, 7), (2, 101, True, 8), (4, 103, True, 9))
        assert leg.silent_s == 0.0 and tr.header_gaps == 0

    def test_missing_ids_fall_back_to_the_30_second_rule(self):
        mission = make_mission(route(WPS))
        [leg] = feed_hdr(leg_tracker.LegTracker(), mission,
                         (0, 0, False, None), (2, 1, False, None), (4, 101, True, None))
        assert (leg.stopped_s, leg.silent_s, leg.moving_s) == (30.0, 70.0, 0.0)
