"""Dispatcher: the enrichment's trajectory is stored on the route node; it is display data, never a reroute."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from packages.controllers.mission import order_policy
from tests.unit.test_mission_goto import _make_robot
from tests.unit.test_node_policy_enrich_dispatch import _enriched, _mission, _start_mission, _wp

LEGS = [{"from_index": 0, "to_index": 1, "points": [[0.0, 0.0], [10.0, 0.0]]}]


@pytest.fixture(autouse=True)
def _policy_on(monkeypatch):
    monkeypatch.setattr(order_policy, "_current", order_policy.OrderPolicy(node_policy_mode="on"))


@pytest.mark.unit
async def test_the_trajectory_is_stored_though_the_waypoints_did_not_change():
    answer = _enriched(_wp(0.0), _wp(10.0), changed=False, inserted=0)
    answer["trajectory"] = LEGS
    planner = SimpleNamespace(enrich_route=AsyncMock(return_value=answer))
    r, db = _make_robot(planner)
    m = _mission([_wp(0.0), _wp(10.0)])
    await _start_mission(r, m)
    assert [item.dict() for item in m.mission_tree[0].route.trajectory] == LEGS
    stored = db.update_spec_fields.await_args.args[2]["mission_tree"][0]["route"]
    assert stored["trajectory"] == LEGS and len(stored["waypoints"]) == 2
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_an_answer_without_trajectory_or_change_stores_nothing():
    planner = SimpleNamespace(enrich_route=AsyncMock(return_value=_enriched(
        _wp(0.0), _wp(10.0), changed=False, inserted=0)))
    r, db = _make_robot(planner)
    m = _mission([_wp(0.0), _wp(10.0)])
    await _start_mission(r, m)
    assert m.mission_tree[0].route.trajectory is None
    db.update_spec_fields.assert_not_awaited()
    r._cancel_mission_timeout()
