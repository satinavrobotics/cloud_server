"""Planner side of the turn cost (docs/scene_graph/close_evidence_and_turn_cost_design.md B.6): lambda per robot from the factsheet
(`turn_lambda`, `turn_record`), the settings, `safety_for(..., turn)`, the pricing record of `plan_on_graph`, the server pass-through
(plan, enrich), the log line and the stats; the string pull and the nodePolicy cut keep no turn term."""
import logging
from types import SimpleNamespace as NS

import pytest

from cloud_common.objects import common
from cloud_common.objects.robot import RobotTypeIdentifierV1
from packages import config
from packages.scene_graph.api import Safety
from packages.services.mission_planner import node_policy_plan as npp
from packages.services.mission_planner import scene_route as sr

from .test_lazy_plan import WORLDS, _build
from .test_scene_route_safety import _stats_response_fields, _trial_pairs, _wp

pytestmark = pytest.mark.unit


@pytest.fixture
def enabled(monkeypatch):
    """the switch on, no operator value: what the service reads (server.py binds `config` at import)"""
    monkeypatch.setattr(config, "SCENE_GRAPH_TURN_COST_ENABLED", True)
    monkeypatch.setattr(config, "SCENE_GRAPH_TURN_LAMBDA_M_PER_RAD", None)


def turn_lambda(factsheet, enabled=True, operator=None):
    return sr.turn_lambda(factsheet, enabled, operator)


# ---- lambda from the factsheet (pure) -----------------------------------------------------------------------
def test_lambda_is_speed_over_angular_speed_from_the_factsheet():
    assert turn_lambda(NS(speed_max=1.0, angular_speed_max=2.0)) == (0.5, "factsheet", "")
    assert turn_lambda(RobotTypeIdentifierV1(speed_max=1.2, angular_speed_max=1.5)) == (pytest.approx(0.8), "factsheet", "")
    assert turn_lambda(NS(speed_max=10.0, angular_speed_max=0.5)) == (5.0, "factsheet", "")          # clamped to [0, 5]
    assert turn_lambda(NS(speed_max=1e300, angular_speed_max=1e-300))[0] == 5.0                      # inf ratio: clamped


@pytest.mark.parametrize("bad", [-1, -1.0, 0, 0.0, float("nan"), float("inf"), None, "abc"])
def test_a_missing_field_is_named_and_gives_no_turn_cost(bad):
    assert turn_lambda(NS(speed_max=1.0, angular_speed_max=bad)) == (0.0, "off", "angular_speed_max missing")
    assert turn_lambda(NS(speed_max=bad, angular_speed_max=1.0)) == (0.0, "off", "speed_max missing")
    assert turn_lambda(NS(speed_max=bad, angular_speed_max=bad)) == (0.0, "off", "speed_max, angular_speed_max missing")


def test_unsent_factsheet_and_no_robot():
    assert turn_lambda(RobotTypeIdentifierV1()) == (0.0, "off", "speed_max, angular_speed_max missing")    # -1 until sent
    assert turn_lambda(None) == (0.0, "off", "speed_max, angular_speed_max missing")
    assert turn_lambda(NS()) == (0.0, "off", "speed_max, angular_speed_max missing")


def test_the_operator_value_is_used_only_for_an_incomplete_factsheet():
    assert turn_lambda(NS(speed_max=1.0, angular_speed_max=-1), operator=0.3) == (0.3, "config", "angular_speed_max missing")
    assert turn_lambda(NS(speed_max=1.0, angular_speed_max=4.0), operator=0.3) == (0.25, "factsheet", "")   # never read then
    assert turn_lambda(None, operator=9.0)[:2] == (5.0, "config")
    assert turn_lambda(None, operator=0.0)[:2] == (0.0, "config")
    for bad in (-0.5, float("nan"), float("inf")):
        lam, source, reason = turn_lambda(None, operator=bad)
        assert (lam, source) == (0.0, "off") and "SCENE_GRAPH_TURN_LAMBDA_M_PER_RAD" in reason and "ignored" in reason


def test_the_switch_off_means_no_turn_cost_whatever_the_robot_says():
    assert turn_lambda(NS(speed_max=1.0, angular_speed_max=2.0), enabled=False, operator=0.4) == (0.0, "off", "disabled")
    rec = sr.turn_record(NS(speed_max=1.0, angular_speed_max=2.0), False, 0.4)
    assert rec == {"lambda_m_per_rad": 0.0, "source": "off", "speed_max": 1.0, "angular_speed_max": 2.0, "reason": "disabled"}


def test_the_turn_record():
    assert sr.turn_record(RobotTypeIdentifierV1(speed_max=1.0, angular_speed_max=2.0), True, None) == \
        {"lambda_m_per_rad": 0.5, "source": "factsheet", "speed_max": 1.0, "angular_speed_max": 2.0, "reason": ""}
    assert sr.turn_record(None, True, None) == {"lambda_m_per_rad": 0.0, "source": "off", "speed_max": None,
                                                "angular_speed_max": None, "reason": "speed_max, angular_speed_max missing"}


def test_the_settings_parse_with_the_turn_cost_off_by_default():
    from .test_config import _reload_config
    cfg = _reload_config({"SCENE_GRAPH_TURN_COST_ENABLED": None, "SCENE_GRAPH_TURN_LAMBDA_M_PER_RAD": None})
    assert cfg.SCENE_GRAPH_TURN_COST_ENABLED is False and cfg.SCENE_GRAPH_TURN_LAMBDA_M_PER_RAD is None
    cfg = _reload_config({"SCENE_GRAPH_TURN_COST_ENABLED": "true", "SCENE_GRAPH_TURN_LAMBDA_M_PER_RAD": "0.4"})
    assert cfg.SCENE_GRAPH_TURN_COST_ENABLED is True and cfg.SCENE_GRAPH_TURN_LAMBDA_M_PER_RAD == 0.4
    cfg = _reload_config({"SCENE_GRAPH_TURN_COST_ENABLED": "false", "SCENE_GRAPH_TURN_LAMBDA_M_PER_RAD": "fast"})
    assert cfg.SCENE_GRAPH_TURN_COST_ENABLED is False and cfg.SCENE_GRAPH_TURN_LAMBDA_M_PER_RAD is None
    cfg = _reload_config({"SCENE_GRAPH_TURN_COST_ENABLED": None, "SCENE_GRAPH_TURN_LAMBDA_M_PER_RAD": ""})
    assert cfg.SCENE_GRAPH_TURN_LAMBDA_M_PER_RAD is None


# ---- safety_for and plan_on_graph ---------------------------------------------------------------------------
@pytest.fixture(scope="module", params=["grid", "room_loop", "pillar_room"])
def graphs(request):
    st, eng, lazy = _build(WORLDS[request.param]())
    from packages.services.mission_planner.lazy_plan import open_plan
    return request.param, eng, open_plan(st, "m", max_tiles=10 ** 6)


def test_safety_for_carries_the_turn(graphs):
    name, eng, lazy = graphs
    for g in (eng, lazy):
        priced = sr.safety_for(g, None, None, 1.0)
        assert priced.turn == 0.0 and sr.safety_for(g, None, None, 1.0, 0.0) == priced
        assert sr.safety_for(g, None, None, 1.0, 0.5) == Safety(priced.margin, priced.rate, turn=0.5)
        alone = sr.safety_for(g, None, None, 0.0, 0.5)                   # no price: the length plus the turn cost
        assert alone.rate == 0.0 and alone.turn == 0.5
        assert sr.safety_for(g, None, None, 0.0, 0.0) is None
    assert sr.safety_for(NS(params=NS(lattice=0.1, robot_half_side=0.25)), None, None, 1.0, 0.5) is None   # no deficit_cell


def _plan(graph, p, q, kappa, turn):
    ids = iter("n%d" % i for i in range(10 ** 5))
    seen = {}
    try:
        return sr.plan_on_graph(graph, "m", p, q, ([], []), lambda x, y: seen.setdefault((round(x, 3), round(y, 3)), next(ids)),
                                0.5, 1.5, 3.0, None, 0.05, None, None, kappa, True, 0, turn=turn)
    except sr.ChartFallback as err:
        return err.reason


def _rec(lam, source="factsheet"):
    return {"lambda_m_per_rad": lam, "source": source, "speed_max": 1.0, "angular_speed_max": 1.0 / lam if lam else -1.0,
            "reason": ""}


def test_the_plan_carries_the_turn_record_and_the_turn_cost(graphs):
    name, eng, lazy = graphs
    for p, q in _trial_pairs(eng, 6, 17):
        for g in (eng, lazy):
            plain = _plan(g, p, q, 1.0, None)
            off = _plan(g, p, q, 1.0, _rec(0.0, "off"))
            on = _plan(g, p, q, 1.0, _rec(0.5))
            if isinstance(plain, str):
                assert plain == off == on
                continue
            assert _wp(off) == _wp(plain) and off.pricing["cost"] == plain.pricing["cost"]        # lambda 0: unchanged
            assert off.pricing["turn"] == _rec(0.0, "off") and plain.pricing["turn"] == sr.TURN_NOT_GIVEN
            assert on.pricing["turn"] == _rec(0.5) and on.pricing["profile"] == "hinge" and on.pricing["rate"] > 0
            assert on.pricing["cost"] >= plain.pricing["cost"]
        e, l = _plan(eng, p, q, 1.0, _rec(0.5)), _plan(lazy, p, q, 1.0, _rec(0.5))
        if not isinstance(e, str):
            assert _wp(e) == _wp(l) and e.pricing["cost"] == l.pricing["cost"]                    # engine and lazy agree


def test_a_turn_cost_alone_has_a_record_without_clearance_fields_and_no_priced_pull(graphs, monkeypatch):
    name, eng, lazy = graphs
    monkeypatch.setattr(sr, "priced_visible", lambda *a, **kw: pytest.fail("the pull has no turn term"))
    n = 0
    for p, q in _trial_pairs(eng, 4, 23):
        for g in (eng, lazy):
            got = _plan(g, p, q, 0.0, _rec(0.5))
            if isinstance(got, str):
                continue
            pr = got.pricing
            assert (pr["rate"], pr["kappa"], pr["margin"], pr["profile"], pr["waypoints_below_floor"]) == (0.0, 0.0, None, "none", 0)
            assert pr["turn"] == _rec(0.5) and isinstance(pr["cost"], int) and pr["exact"] is True
            n += 1
    assert n


def test_without_a_price_and_a_turn_the_plan_is_unpriced(graphs):
    name, eng, lazy = graphs
    p, q = _trial_pairs(eng, 1, 3)[0]
    got = _plan(lazy, p, q, 0.0, _rec(0.0, "off"))
    assert isinstance(got, str) or got.pricing is None


# ---- nodePolicy -----------------------------------------------------------------------------------------------
def test_the_planned_cut_has_no_turn_term(monkeypatch):
    from .test_node_policy_route_safety import Bent, _poses
    monkeypatch.setattr(sr, "priced_visible", lambda *a, **kw: pytest.fail("a turn cost alone must not price the cut"))
    g = Bent()
    poses = _poses(0.05, 10.05)
    out, n = npp.insert_planned(g, poses, lambda a, b: True, 3.5, 0.5, sr.simplify, Safety(1.0, 0.0, turn=0.5))
    assert g.seen[-1] == Safety(1.0, 0.0, turn=0.5)
    plain, n_plain = npp.insert_planned(g, poses, lambda a, b: True, 3.5, 0.5, sr.simplify)
    assert n == n_plain


# ---- the service --------------------------------------------------------------------------------------------
def _priced_graph():
    """test_planner_chart_route.FakeGraph that takes a Safety, records it and reports a cost"""
    from .test_planner_chart_route import FakeGraph
    from packages.scene_graph.api import Route

    class Priced(FakeGraph):
        params = NS(lattice=0.1, robot_half_side=0.25)
        last = {"tiles": 7}

        def __init__(self):
            super().__init__()
            self.safeties = []

        def deficit_cell(self, ix, iy, margin):
            return 0.0

        def route(self, a, b, blocked=None, start=None, goal=None, safety=None):
            self.safeties.append(safety)
            r = super().route(a, b, blocked, start, goal)
            return Route(charts=r.charts, length=r.length, min_width=r.min_width, nodes=r.nodes, cost=4242, exact=True)
    return Priced()


async def test_the_service_passes_the_robots_lambda_and_reports_it(enabled, monkeypatch, caplog):
    from .test_planner_chart_route import FakeStore, _plan as svc_plan, _service
    monkeypatch.setattr(config, "SCENE_GRAPH_PLANNER_ENABLED", True)
    monkeypatch.setattr(config, "SCENE_GRAPH_SAFETY_KAPPA", 1.0)
    graph = _priced_graph()
    svc = _service(FakeStore(graph))
    robot = await svc.get_robot_status("r1")
    robot.status.factsheet.speed_max = 1.0
    robot.status.factsheet.angular_speed_max = 2.0
    with caplog.at_level(logging.INFO):
        out = await svc_plan(svc)
    assert out["planner"] == sr.PLANNER_SCENE_GRAPH
    assert graph.safeties[0].turn == 0.5 and graph.safeties[0].rate > 0
    assert out["pricing"]["turn"] == {"lambda_m_per_rad": 0.5, "source": "factsheet", "speed_max": 1.0, "angular_speed_max": 2.0,
                                      "reason": ""}
    line = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Priced plan")][0]
    assert "turn_lambda=0.500" in line and "turn_source=factsheet" in line
    stats = svc.get_stats()
    assert set(stats) <= _stats_response_fields()
    assert stats["scene_graph"]["turn_sources"] == {"factsheet": 1} and stats["scene_graph"]["priced_plans"] == 1
    # a robot that never sent its angular speed: the operator value, and the stats show it
    robot.status.factsheet.angular_speed_max = -1
    monkeypatch.setattr(config, "SCENE_GRAPH_TURN_LAMBDA_M_PER_RAD", 0.25)
    graph.safeties.clear()
    out = await svc_plan(svc)
    assert graph.safeties[0].turn == 0.25 and out["pricing"]["turn"]["source"] == "config"
    assert out["pricing"]["turn"]["reason"] == "angular_speed_max missing"
    assert svc.get_stats()["scene_graph"]["turn_sources"] == {"factsheet": 1, "config": 1}
    # no price: a turn-cost plan, its own log line, not counted as priced
    monkeypatch.setattr(config, "SCENE_GRAPH_SAFETY_KAPPA", 0.0)
    caplog.clear()
    with caplog.at_level(logging.INFO):
        out = await svc_plan(svc)
    assert out["pricing"]["rate"] == 0.0 and out["pricing"]["turn"]["lambda_m_per_rad"] == 0.25
    assert any(r.getMessage().startswith("Turn-cost plan") for r in caplog.records)
    stats = svc.get_stats()["scene_graph"]
    assert stats["priced_plans"] == 2 and stats["turn_sources"] == {"factsheet": 1, "config": 2}
    svc.cleanup()


async def test_the_switch_off_leaves_the_service_as_it_was(monkeypatch):
    from .test_planner_chart_route import FakeStore, _plan as svc_plan, _service
    monkeypatch.setattr(config, "SCENE_GRAPH_PLANNER_ENABLED", True)
    monkeypatch.setattr(config, "SCENE_GRAPH_TURN_COST_ENABLED", False)
    monkeypatch.setattr(config, "SCENE_GRAPH_SAFETY_KAPPA", 0.0)
    graph = _priced_graph()
    svc = _service(FakeStore(graph))
    robot = await svc.get_robot_status("r1")
    robot.status.factsheet.speed_max, robot.status.factsheet.angular_speed_max = 1.0, 2.0
    out = await svc_plan(svc)
    assert "pricing" not in out and all(s is None for s in graph.safeties)
    assert svc.get_stats()["scene_graph"]["turn_sources"] == {"off": 1}
    svc.cleanup()


async def test_enrich_passes_the_same_lambda(enabled, monkeypatch):
    from .test_planner_chart_route import FakeStore, _service
    monkeypatch.setattr(config, "SCENE_GRAPH_PLANNER_ENABLED", True)
    monkeypatch.setattr(config, "SCENE_GRAPH_SAFETY_KAPPA", 2.0)
    graph = _priced_graph()
    svc = _service(FakeStore(graph))
    robot = await svc.get_robot_status("r1")
    robot.status.factsheet.speed_max, robot.status.factsheet.angular_speed_max = 0.9, 1.8
    wps = [common.Pose2D(x=x, y=0.0, map_id="m") for x in (1.0, 3.0, 6.0)]
    wps[1].skippable = True
    await svc.enrich_route("r1", "m", wps)
    got = [s for s in graph.safeties if s is not None]
    assert got and {s.turn for s in got} == {0.5} and len(set(got)) == 1
    svc.cleanup()


def test_compose_passes_the_settings_off_by_default():
    import os
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    with open(os.path.join(root, "docker_compose", "mission_dispatch_services.yaml"), encoding="utf-8") as fh:
        compose = fh.read()
    assert "SCENE_GRAPH_TURN_COST_ENABLED=${SCENE_GRAPH_TURN_COST_ENABLED:-false}" in compose
    assert "SCENE_GRAPH_TURN_LAMBDA_M_PER_RAD=${SCENE_GRAPH_TURN_LAMBDA_M_PER_RAD:-}" in compose
    for doc in (".env.example", os.path.join("docs", "scene_graph", "runbook.md")):
        with open(os.path.join(root, doc), encoding="utf-8") as fh:
            text = fh.read()
        assert "SCENE_GRAPH_TURN_COST_ENABLED" in text and "SCENE_GRAPH_TURN_LAMBDA_M_PER_RAD" in text, doc
