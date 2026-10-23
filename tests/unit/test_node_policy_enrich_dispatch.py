"""Dispatcher: a mission's routes are enriched by the planner before the first order and the result
is stored in the mission_tree (docs/node_policies_2026-10-13.md)."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import cloud_common.objects as api_objects
from cloud_common.objects import mission as mission_object
from packages.controllers.mission import order_policy
from tests.unit.test_mission_goto import _make_robot, _orders


@pytest.fixture(autouse=True)
def _policy_on(monkeypatch):
    monkeypatch.setattr(order_policy, "_current",
                        order_policy.OrderPolicy(node_policy_mode="on"))


def _mission(waypoints, name="usr", planned_path=None):
    m = api_objects.MissionObjectV1(
        name=name, robot="r1", status={}, timeout=1000, planned_path=planned_path,
        mission_tree=[{"name": "a", "parent": "root", "route": {"waypoints": waypoints}}])
    return m


def _wp(x, **kw):
    return {"x": x, "y": 0.0, "theta": 0.0, "map_id": "m", **kw}


def _enriched(*wps, changed=True, inserted=1, report=None):
    return {"waypoints": list(wps), "changed": changed, "inserted": inserted,
            "node_policy": report or {"total": 1, "skippable": 1, "unavailable": None}}


async def _start_mission(r, m):
    r._route_in_robot_frame = AsyncMock(side_effect=lambda route: route)
    r._missions[m.name] = m
    await r._try_start_mission()


def _policies(order):
    return [{a["actionParameters"][0]["key"]: None,
             **{p["key"]: p["value"] for p in a["actionParameters"]}}
            for n in order["nodes"] for a in n["actions"] if a["actionType"] == "nodePolicy"]


@pytest.mark.unit
async def test_planned_waypoints_are_stored_and_sent_with_their_policy():
    planned = _wp(5.0, origin="planned", skippable=True, corridor_width_m=0.4)
    planner = SimpleNamespace(enrich_route=AsyncMock(return_value=_enriched(
        _wp(0.0, skippable=False), planned, _wp(10.0))))
    r, db = _make_robot(planner)
    m = _mission([_wp(0.0, skippable=False), _wp(10.0)])

    await _start_mission(r, m)

    planner.enrich_route.assert_awaited_once()
    args = planner.enrich_route.await_args
    assert args.args[:2] == ("r1", "m") and args.kwargs["insert_planned"] is True
    assert [w.origin for w in m.mission_tree[0].route.waypoints] == [None, "planned", None]
    fields = db.update_spec_fields.await_args.args[2]
    assert fields["route_rev"] == 1 and m.status.applied_route_rev == 1
    stored = fields["mission_tree"][0]["route"]["waypoints"]
    assert [w["origin"] for w in stored] == [None, "planned", None]
    order = _orders(r)[0]
    assert len(order["nodes"]) == 4                       # start + 3 waypoints
    params = _policies(order)
    assert params[1]["skippable"] is True and params[1]["corridorWidth"] == 0.4
    assert params[0]["skippable"] is False
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_a_planner_that_fails_leaves_the_route_as_it_is():
    planner = SimpleNamespace(enrich_route=AsyncMock(side_effect=ConnectionError("down")))
    r, db = _make_robot(planner)
    m = _mission([_wp(0.0), _wp(10.0)])

    await _start_mission(r, m)

    assert len(m.mission_tree[0].route.waypoints) == 2 and m.route_rev == 0
    db.update_spec_fields.assert_not_awaited()
    assert len(_orders(r)[0]["nodes"]) == 3               # the order still goes out
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_unchanged_answer_writes_nothing():
    planner = SimpleNamespace(enrich_route=AsyncMock(return_value=_enriched(
        _wp(0.0), _wp(10.0), changed=False, inserted=0,
        report={"total": 0, "skippable": 0, "unavailable": "no_scene_graph"})))
    r, db = _make_robot(planner)
    m = _mission([_wp(0.0), _wp(10.0)])

    await _start_mission(r, m)

    planner.enrich_route.assert_awaited_once()
    db.update_spec_fields.assert_not_awaited()
    assert m.route_rev == 0
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_mission_with_topomap_ids_keeps_its_length():
    planner = SimpleNamespace(enrich_route=AsyncMock(return_value=_enriched(
        _wp(0.0), _wp(10.0), changed=False, inserted=0)))
    r, _ = _make_robot(planner)
    m = _mission([_wp(0.0, skippable=True), _wp(10.0)], planned_path=["1", "2"])

    await _start_mission(r, m)

    assert planner.enrich_route.await_args.kwargs["insert_planned"] is False
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_mapless_routes_and_goto_are_not_sent_to_the_planner():
    planner = SimpleNamespace(enrich_route=AsyncMock())
    r, _ = _make_robot(planner)
    m = _mission([{"x": 0.0, "y": 0.0, "theta": 0.0}, {"x": 5.0, "y": 0.0, "theta": 0.0}])
    await _start_mission(r, m)
    planner.enrich_route.assert_not_awaited()
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_a_started_mission_is_never_enriched_again():
    planner = SimpleNamespace(enrich_route=AsyncMock(return_value=_enriched(
        _wp(0.0), _wp(10.0), changed=False, inserted=0)))
    r, _ = _make_robot(planner)
    m = _mission([_wp(0.0), _wp(10.0)])
    await _start_mission(r, m)
    planner.enrich_route.reset_mock()
    await r._enrich_routes()                              # start_timestamp is set now
    planner.enrich_route.assert_not_awaited()
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_a_mission_with_a_persisted_run_id_is_not_enriched():
    # start_timestamp is persisted lazily; the run id goes out before the first order.
    planner = SimpleNamespace(enrich_route=AsyncMock())
    r, _ = _make_robot(planner)
    m = _mission([_wp(0.0), _wp(10.0)])
    m.status.run_id = "run-1"
    r._current_mission = m
    await r._enrich_routes()
    planner.enrich_route.assert_not_awaited()


@pytest.mark.unit
async def test_a_failing_planner_is_asked_once_not_once_per_route_node():
    planner = SimpleNamespace(enrich_route=AsyncMock(side_effect=TimeoutError("slow")))
    r, _ = _make_robot(planner)
    m = _mission([_wp(0.0), _wp(10.0)])
    m.mission_tree.append(mission_object.MissionNodeV1(
        name="b", parent="root", route={"waypoints": [_wp(0.0), _wp(4.0)]}))
    r._current_mission = m
    await r._enrich_routes()
    assert planner.enrich_route.await_count == 1


@pytest.mark.unit
async def test_a_route_edited_during_the_planner_call_is_not_overwritten():
    r, db = _make_robot(None)
    m = _mission([_wp(0.0), _wp(10.0)])
    edited = mission_object.MissionRouteNodeV1(waypoints=[_wp(0.0), _wp(20.0)])

    async def enrich(*_a, **_k):
        m.mission_tree[0].route = edited          # the operator's PUT landed meanwhile
        return _enriched(_wp(0.0), _wp(5.0, origin="planned"), _wp(10.0))

    r._robot_server.mission_planner = SimpleNamespace(enrich_route=enrich)
    r._current_mission = m
    await r._enrich_routes()
    assert m.mission_tree[0].route is edited and m.route_rev == 0
    db.update_spec_fields.assert_not_awaited()


def _goto(waypoints):
    m = _mission(waypoints)
    m.kind = "goto"
    return m


@pytest.mark.unit
async def test_a_goto_without_annotation_is_logged_never_sent_to_the_planner(caplog):
    planner = SimpleNamespace(enrich_route=AsyncMock())
    r, _ = _make_robot(planner)
    r._current_mission = _goto([_wp(0.0), _wp(5.0), _wp(10.0)])
    with caplog.at_level("WARNING"):
        await r._enrich_routes()
    planner.enrich_route.assert_not_awaited()
    assert "node_policy_unannotated" in caplog.text
    assert "without an origin" in caplog.text and "no corridor" in caplog.text


@pytest.mark.unit
async def test_an_annotated_goto_is_not_reported(caplog):
    planner = SimpleNamespace(enrich_route=AsyncMock())
    r, _ = _make_robot(planner)
    r._current_mission = _goto([_wp(0.0, origin="planned", corridor_width_m=0.4),
                                _wp(5.0, origin="planned", corridor_width_m=0.0),
                                _wp(10.0, origin="user")])
    with caplog.at_level("WARNING"):
        await r._enrich_routes()
    assert "node_policy_unannotated" not in caplog.text


@pytest.mark.unit
async def test_a_route_without_corridors_is_enriched_even_when_nothing_asks_for_skipping():
    planner = SimpleNamespace(enrich_route=AsyncMock(return_value=_enriched(
        _wp(0.0, corridor_width_m=0.4), _wp(10.0), changed=True, inserted=0)))
    r, _ = _make_robot(planner)
    m = _mission([_wp(0.0), _wp(10.0)], planned_path=["a", "b"])   # no gap fill, no skippable
    await _start_mission(r, m)
    planner.enrich_route.assert_awaited_once()
    r._cancel_mission_timeout()
