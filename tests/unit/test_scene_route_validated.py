"""Planner side of validated-first (docs/scene_graph/close_evidence_and_turn_cost_design.md part G): the setting (off by default, compose,
`.env.example`, runbook), `safety_for(..., validated_first)`, `priced_only`, and the plan record of `plan_on_graph`."""
import os

import pytest

from packages import config
from packages.scene_graph.api import Safety
from packages.services.mission_planner import scene_route as sr
from packages.services.mission_planner.lazy_plan import _CLR_CAP_M, PlanGraph

from .test_lazy_validated_first import _build_rule, _detour
from .test_scene_route_turn import _plan, _rec

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def graphs():
    st, eng, _ = _build_rule(_detour(), near=0.5)
    return eng, PlanGraph(st.view("m"), max_tiles=10 ** 6)


def test_the_setting_parses_and_is_off_by_default():
    from .test_config import _reload_config
    assert _reload_config({"SCENE_GRAPH_VALIDATED_FIRST": None}).SCENE_GRAPH_VALIDATED_FIRST is False
    assert _reload_config({"SCENE_GRAPH_VALIDATED_FIRST": "true"}).SCENE_GRAPH_VALIDATED_FIRST is True
    assert _reload_config({"SCENE_GRAPH_VALIDATED_FIRST": "false"}).SCENE_GRAPH_VALIDATED_FIRST is False


def test_compose_env_example_and_runbook_carry_the_setting_off_by_default():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    with open(os.path.join(root, "docker_compose", "mission_dispatch_services.yaml"), encoding="utf-8") as fh:
        assert "SCENE_GRAPH_VALIDATED_FIRST=${SCENE_GRAPH_VALIDATED_FIRST:-false}" in fh.read()
    for doc in (".env.example", os.path.join("docs", "scene_graph", "runbook.md")):
        with open(os.path.join(root, doc), encoding="utf-8") as fh:
            assert "SCENE_GRAPH_VALIDATED_FIRST" in fh.read(), doc


def test_safety_for_is_unchanged_with_the_flag_off_and_carries_it_on(graphs, monkeypatch):
    assert config.SCENE_GRAPH_VALIDATED_FIRST is False
    for g in graphs:
        priced = sr.safety_for(g, None, None, 1.0)
        assert priced.validated_first is False and sr.safety_for(g, None, None, 1.0, 0.0, False) == priced
        assert sr.safety_for(g, None, None, 0.0, 0.0) is None
        on = sr.safety_for(g, None, None, 1.0, 0.5, True)
        assert on.validated_first and on.turn == 0.5 and on.rate == priced.rate and on.margin == priced.margin
        alone = sr.safety_for(g, None, None, 0.0, 0.0, True)               # no price, no turn: a Safety all the same
        assert alone == Safety(_CLR_CAP_M, 0.0, validated_first=True)
        monkeypatch.setattr(config, "SCENE_GRAPH_VALIDATED_FIRST", True)   # the default of the argument is the setting
        assert sr.safety_for(g, None, None, 0.0, 0.0) == alone
        monkeypatch.setattr(config, "SCENE_GRAPH_VALIDATED_FIRST", False)


def test_priced_only_strips_the_flag():
    vf = Safety(_CLR_CAP_M, 0.0, validated_first=True)
    assert sr.priced_only(None) is None and sr.priced_only(vf) is None
    assert sr.priced_only(Safety(0.5, 2.0)) == Safety(0.5, 2.0)
    assert sr.priced_only(Safety(0.5, 2.0, validated_first=True)) == Safety(0.5, 2.0)
    turn = Safety(_CLR_CAP_M, 0.0, turn=0.5, validated_first=True)
    assert sr.priced_only(turn) == Safety(_CLR_CAP_M, 0.0, turn=0.5)


def _pq(eng):
    a, b = eng.snap(2.0, 4.0), eng.snap(10.0, 4.0)
    return (a.x, a.y), (b.x, b.y)


def test_the_plan_reports_the_unvalidated_count_and_takes_the_validated_way(graphs, monkeypatch):
    eng, lazy = graphs
    p, q = _pq(eng)
    for g in (eng, lazy):
        off = _plan(g, p, q, 0.0, None)
        assert off.validated is None
        monkeypatch.setattr(config, "SCENE_GRAPH_VALIDATED_FIRST", True)
        on = _plan(g, p, q, 0.0, None)
        monkeypatch.setattr(config, "SCENE_GRAPH_VALIDATED_FIRST", False)
        assert on.validated == {"unvalidated_edges": 0, "unvalidated_m": 0.0, "exact": True}
        assert on.pricing is None                                         # a flag alone is no price and no turn record
        assert min(w.y for w in on.waypoints) < 1.8 < min(w.y for w in off.waypoints)    # through D1, not D2
        # with a price and a turn cost the pricing records keep their shape; J is the route's cost without the penalty
        monkeypatch.setattr(config, "SCENE_GRAPH_VALIDATED_FIRST", True)
        priced_on = _plan(g, p, q, 1.0, _rec(0.5))
        monkeypatch.setattr(config, "SCENE_GRAPH_VALIDATED_FIRST", False)
        assert priced_on.validated["unvalidated_edges"] == 0
        assert priced_on.pricing["turn"] == _rec(0.5) and 0 < priced_on.pricing["cost"] < 10 ** 6 * 2


def test_the_plan_with_the_flag_off_is_unchanged(graphs):
    eng, lazy = graphs
    p, q = _pq(eng)
    for g in (eng, lazy):
        a, b = _plan(g, p, q, 1.0, _rec(0.5)), _plan(g, p, q, 1.0, _rec(0.5))
        assert a.validated is None and a.pricing == b.pricing
