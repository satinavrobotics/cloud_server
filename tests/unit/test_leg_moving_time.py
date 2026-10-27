"""Stage C of the edge statistics: driving time apart from waiting time (leg_tracker.py module docstring):
stopped_s / silent_s / moving_s of a leg, the run summary, the per-edge statistics and the route-edit chart rule."""
from types import SimpleNamespace

import pytest

from cloud_common.objects import common
from cloud_common.objects import mission as mission_object
from packages.controllers.mission import leg_tracker
from packages.utils import edge_stats as es
from packages.utils import run_legs
from tests.unit.test_leg_tracker_plan import WPS, at, make_mission, route, vda, wp
from tests.unit.test_edge_stats import obs

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


class TestEdgeStats:
    def leg(self, **extra):
        base = {"run_id": "run1", "seq": 1, "robot_name": "r1", "map_id": "yard", "from_topomap_node": "A",
                "to_topomap_node": "B", "ended_at": at(100), "duration_s": 20.0, "straight_m": 4.0,
                "planned_m": 4.0, "recoveries": 1, "blocks": 0}
        base.update(extra)
        return base

    def test_leg_observation_carries_the_times_and_mapping_rows_have_none(self):
        o = es.leg_observation(self.leg(moving_s=8.0, stopped_s=10.0, recovery_s=5.0))
        assert (o["moving_s"], o["stopped_s"], o["recovery_s"]) == (8.0, 10.0, 5.0)
        assert o["speed_mps"] == pytest.approx(0.2)                    # distance / total duration, as before
        old = es.leg_observation(self.leg())
        assert (old["moving_s"], old["stopped_s"], old["recovery_s"]) == (None, None, None)
        mapping = es._observation("yard", "A", "B", "r1", "mapping", "s:1", at(1), 2.0, 1.0)
        assert (mapping["moving_s"], mapping["stopped_s"], mapping["recovery_s"]) == (None, None, None)

    def test_stats_moving_figures_use_only_observations_with_moving_time(self):
        rows = [obs(0, duration=20.0, distance=4.0, moving_s=8.0, recovery_s=5.0),
                obs(1, duration=20.0, distance=4.0, moving_s=4.0, recovery_s=1.5),
                obs(2, duration=20.0, distance=4.0),                       # older: no moving_s
                obs(3, duration=20.0, distance=4.0, moving_s=0.0)]         # nothing known to be moving
        [item] = es.group_stats(rows)
        assert item["moving_count"] == 2 and item["median_moving_s"] == 6.0
        assert item["moving_speed_mps"] == pytest.approx(0.75)            # median of 4/8, 4/4
        assert item["recovery_s"] == pytest.approx(6.5)
        assert item["median_s"] == 20.0 and item["median_speed_mps"] == pytest.approx(0.2)   # unchanged

    def test_stats_without_moving_data_are_null(self):
        [item] = es.group_stats([obs(0)])
        assert (item["median_moving_s"], item["moving_speed_mps"], item["moving_count"],
                item["recovery_s"]) == (None, None, 0, 0.0)


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
