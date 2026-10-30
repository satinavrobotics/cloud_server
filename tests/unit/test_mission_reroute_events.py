"""Reroute handling in the dispatcher (2026-10-30): the held replace cancel, the replan /
enrich race with an operator's PUT, and the reroute / block-cleared events."""
import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import cloud_common.objects as api_objects
from packages.controllers.mission import fleet_recorder as fr
from tests.unit import test_mission_goto as goto_tests
from tests.unit import test_node_policy_enrich_dispatch as enrich_tests
from tests.unit.test_mission_lifecycle_fixes import (
    _make_robot, _mission, _order_id, _orders, _rerouted, _start, _state)
from tests.unit.test_offline_missions_dispatch import _cancel_actions, _edge_blocked
from tests.unit.fleet_recorder_fakes import *  # noqa: F401,F403  pylint: disable=unused-wildcard-import


# --- W3.1 / W3.2: the held replace cancel --------------------------------------------------
@pytest.mark.unit
async def test_a_held_replace_cancel_does_not_outlive_the_order_it_was_held_for():
    r, _ = _make_robot()
    await _start(r, _mission())
    await r._on_mission_change(_rerouted())
    assert r._deferred_replace_cancel and not _cancel_actions(r)

    # The node is sent again as a new order (built from the newest route) before the held
    # cancel went out: that cancel must not hit it.
    r._sent_order = None
    r._pending_send = "send"
    await r._flush_pending_send()
    assert r._sent_order is not None                       # a new order was built
    assert not r._deferred_replace_cancel
    assert not _cancel_actions(r)


@pytest.mark.unit
async def test_issue_replace_cancel_sends_one_replace_cancel_and_releases_the_flag():
    r, _ = _make_robot()
    await _start(r, _mission())
    r._deferred_replace_cancel = True
    assert await r._issue_replace_cancel("test")
    assert not r._deferred_replace_cancel
    assert len(_cancel_actions(r)) == 1
    assert not await r._issue_replace_cancel("again")      # one outstanding at a time
    assert len(_cancel_actions(r)) == 1
    r._cancel_mission_timeout()


# --- W3.4: replan / enrich vs an operator's PUT --------------------------------------------
def _plan(x):
    return {"success": True, "planned_path": ["7", "8"],
            "waypoints": [{"x": x, "y": 0.0, "theta": 0.0}, {"x": x + 1, "y": 0.0, "theta": 0.0}]}


async def _replan_with(edit, stored_rev=None):
    async def plan(**_):
        edit(r)
        return _plan(51.0)
    planner = SimpleNamespace(plan=AsyncMock(side_effect=plan))
    r, db = goto_tests._make_robot(planner)
    if stored_rev is not None:
        db.get_object = AsyncMock(return_value=SimpleNamespace(route_rev=stored_rev))
    m = goto_tests._goto()
    r._current_mission = m
    await r._replan_goto()
    return m, db


@pytest.mark.unit
async def test_replan_without_an_edit_bumps_the_revision():
    m, db = await _replan_with(lambda r: None)
    assert m.route_rev == m.status.applied_route_rev == 1
    assert "route_rev" not in db.update_spec_fields.await_args.args[2]
    assert db.update_spec_fields.await_args.kwargs["bump_route_rev"] is True


@pytest.mark.unit
async def test_replan_does_not_clobber_an_operator_edit_seen_in_memory():
    def put(r):
        m = r._current_mission
        m.mission_tree[0].route = m.mission_tree[0].route.copy(deep=True)
        m.route_rev = 3
    m, db = await _replan_with(put)
    assert m.route_rev == 3 and m.status.applied_route_rev == 0
    assert [w.x for w in m.mission_tree[0].route.waypoints] == [0.0, 1.0, 2.0]
    db.update_spec_fields.assert_not_awaited()


@pytest.mark.unit
async def test_replan_does_not_lower_a_stored_operator_rev_not_yet_delivered():
    m, db = await _replan_with(lambda r: None, stored_rev=4)
    assert m.route_rev == 0
    assert [w.x for w in m.mission_tree[0].route.waypoints] == [0.0, 1.0, 2.0]
    db.update_spec_fields.assert_not_awaited()


@pytest.mark.unit
async def test_enrich_does_not_apply_over_a_stored_operator_rev():
    planned = enrich_tests._wp(5.0, origin="planned", skippable=True, corridor_width_m=0.4)
    planner = SimpleNamespace(enrich_route=AsyncMock(return_value=enrich_tests._enriched(
        enrich_tests._wp(0.0), planned, enrich_tests._wp(10.0))))
    r, db = goto_tests._make_robot(planner)
    db.get_object = AsyncMock(return_value=SimpleNamespace(route_rev=2))
    m = enrich_tests._mission([enrich_tests._wp(0.0), enrich_tests._wp(10.0)])
    r._current_mission = m
    await r._enrich_routes()
    assert len(m.mission_tree[0].route.waypoints) == 2 and m.route_rev == 0
    db.update_spec_fields.assert_not_awaited()


@pytest.mark.unit
async def test_enrich_applies_when_nothing_was_edited():
    planned = enrich_tests._wp(5.0, origin="planned", skippable=True, corridor_width_m=0.4)
    planner = SimpleNamespace(enrich_route=AsyncMock(return_value=enrich_tests._enriched(
        enrich_tests._wp(0.0), planned, enrich_tests._wp(10.0))))
    r, db = goto_tests._make_robot(planner)
    m = enrich_tests._mission([enrich_tests._wp(0.0), enrich_tests._wp(10.0)])
    r._current_mission = m
    await r._enrich_routes()
    assert len(m.mission_tree[0].route.waypoints) == 3 and m.route_rev == 1


# --- W4: events ------------------------------------------------------------------------------
@pytest.mark.unit
async def test_an_applied_reroute_is_recorded_once_per_revision():
    r, _ = _make_robot()
    await _start(r, _mission())
    rec = r._robot_server.fleet_recorder
    await r._on_mission_change(_rerouted(rev=1))
    await r._on_mission_change(_rerouted(rev=1))          # a redelivery
    assert rec.rerouted.call_count == 1
    args = rec.rerouted.call_args.args
    assert args[0] == "r1" and args[2] == 1 and args[3] == ["a"]
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_a_revision_that_replaces_no_route_records_no_reroute():
    r, _ = _make_robot()
    m = await _start(r, _mission())
    same = _mission()
    same.route_rev = 1
    await r._on_mission_change(same)
    assert r._robot_server.fleet_recorder.rerouted.call_count == 0
    r._cancel_mission_timeout()



def test_rerouted_event_id_includes_the_revision_and_block_cleared_is_its_own_code():
    class Q:
        def __init__(self):
            self.events = []

        def put_event(self, event, ctx):
            self.events.append(event)

    rec = fr.FleetRecorder.__new__(fr.FleetRecorder)
    rec.queue, rec._ctx = Q(), None
    rec._clock = lambda: datetime.datetime(2026, 10, 30, tzinfo=datetime.timezone.utc)
    m = _mission()
    m.status.run_id = "abcd1234"
    rec.rerouted("r1", m, 2, ["a"])
    rec.rerouted("r1", m, 3, ["a"])
    rec.block_cleared("r1", m, "a", "e1")
    e2, e3, bc = rec.queue.events
    assert e2.code.value == "MISSION.REROUTED" and e2.payload["route_rev"] == 2
    assert e2.discriminator != e3.discriminator and e2.discriminator.endswith("|2")
    assert bc.code.value == "MISSION.BLOCK_CLEARED" and bc.payload["blocked_edges"] == ["e1"]


# --- route_rev: the dispatcher's bump is the stored value + 1, never a tie ------------------------
@pytest.mark.unit
async def test_replan_takes_the_revision_the_write_stored():
    # An operator's PUT stored revision 4 meanwhile (not seen in memory): the bump lands on 5 and
    # the dispatcher's in-memory revision and applied revision follow the stored one.
    async def plan(**_):
        return _plan(51.0)
    planner = SimpleNamespace(plan=AsyncMock(side_effect=plan))
    r, db = goto_tests._make_robot(planner)
    db.update_spec_fields = AsyncMock(return_value=5)
    m = goto_tests._goto()
    r._current_mission = m
    await r._replan_goto()
    assert m.route_rev == m.status.applied_route_rev == 5
    assert db.update_spec_fields.await_args.kwargs["bump_route_rev"] is True


@pytest.mark.unit
async def test_enrich_takes_the_revision_the_write_stored():
    planned = enrich_tests._wp(5.0, origin="planned", skippable=True, corridor_width_m=0.4)
    planner = SimpleNamespace(enrich_route=AsyncMock(return_value=enrich_tests._enriched(
        enrich_tests._wp(0.0), planned, enrich_tests._wp(10.0))))
    r, db = goto_tests._make_robot(planner)
    db.update_spec_fields = AsyncMock(return_value=7)
    m = enrich_tests._mission([enrich_tests._wp(0.0), enrich_tests._wp(10.0)])
    r._current_mission = m
    await r._enrich_routes()
    assert m.route_rev == m.status.applied_route_rev == 7
