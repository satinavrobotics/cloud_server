"""Observability and shadow mode of the region cost (docs/planner_region_stats_2026-10-27.md 7, stage S4): `pricing["region"]` of a plan,
`predicted_s` on trajectory legs, `cost_model` on the route node (kept out of the route digest), shadow serves today's route, off does
no work, and the fields round-trip through the mission objects."""
import dataclasses
import math
import os
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from cloud_common.objects import common
from cloud_common.objects import mission as mission_object
from packages.controllers.mission import server as mission_server
from packages.services.mission_planner import scene_route as sr
from packages.services.mission_planner.lazy_plan import open_plan

from .test_lazy_plan import WORLDS, _build
from .test_lazy_region_cost import pricing, random_rho
from .test_scene_route_region import _plan
from .test_scene_route_safety import _trial_pairs, _wp

pytestmark = pytest.mark.unit

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

SPEED, OMEGA = 0.24, 0.5
TURN = {"lambda_m_per_rad": 0.0, "source": "off", "speed_max": SPEED, "angular_speed_max": OMEGA, "reason": ""}


@pytest.fixture(scope="module", params=["grid", "room_loop", "ring", "pillar_room"])
def graphs(request):
    st, eng, _ = _build(WORLDS[request.param]())
    return request.param, eng, open_plan(st, "m", max_tiles=10 ** 6)


def plan(graph, p, q, region=None, mode="off", **kw):
    return _plan(graph, p, q, region, mode, trajectory=5000, turn=dict(TURN), **kw)


def points(p):
    return [tuple(pt) for leg in p.trajectory for pt in leg["points"]]


def without_predictions(legs):
    return [{k: v for k, v in leg.items() if k != "predicted_s"} for leg in legs]


# ---- predicted time (pure) ---------------------------------------------------------------------------------------
def test_region_times_is_length_over_speed_plus_excess(graphs):
    name, eng, lazy = graphs
    reg = pricing(lazy, [500] * len(lazy.keys))
    route = lazy.route(lazy.keys[0], lazy.keys[-1])
    pts = [tuple(p) for p in route.nodes]
    times = sr.region_times(lazy, reg, TURN)
    got, free = times(pts)
    length = sum(math.dist(a, b) for a, b in zip(pts[:-1], pts[1:]))
    assert free == pytest.approx(length / SPEED)
    assert got == pytest.approx(length * 1.5 / SPEED)                        # rho 0.5 everywhere: a metre takes 1.5 x as long
    # length-based (D3): no turn time, with or without angular_speed_max; an unknown speed: nothing to predict
    assert sr.region_times(lazy, reg, {**TURN, "angular_speed_max": None})(pts) == (got, free)
    stair = [(0.0, 0.0), (0.05, 0.0), (0.05, 0.05), (0.1, 0.05), (0.1, 0.1)]   # a lattice staircase counts its length only
    assert times(stair)[1] == pytest.approx(0.2 / SPEED)
    for bad in (None, -1, 0, float("nan")):
        assert sr.region_times(lazy, reg, {**TURN, "speed_max": bad}) is None
    assert sr.region_times(lazy, None, TURN) is None
    assert sr.region_times(lazy, pricing(lazy, [500] * len(lazy.keys), status="no_stats"), TURN) is None
    assert sr.region_times(lazy, pricing(lazy, [500] * len(lazy.keys), version=lazy.version + 1), TURN) is None


# ---- the plan record ---------------------------------------------------------------------------------------------
def test_shadow_serves_the_unpriced_route_and_records_the_alternative(graphs):
    name, eng, lazy = graphs
    reg = pricing(lazy, random_rho(lazy, 9, lo=500, hi=3000, zero_share=0.3))
    n = changed = 0
    for p, q in _trial_pairs(eng, 14, 8):
        base, shadow = plan(lazy, p, q), plan(lazy, p, q, reg, "shadow")
        assert type(shadow) is type(base)
        if isinstance(base, str):
            assert shadow == base
            continue
        assert _wp(shadow) == _wp(base) and shadow.planned_path == base.planned_path and shadow.charts == base.charts
        assert without_predictions(shadow.trajectory or []) == (base.trajectory or [])
        rec = shadow.pricing["region"]
        assert rec["mode"] == "shadow" and rec["served"] == "unpriced" and rec["status"] == "ok"
        assert rec["model_hash"] == reg.model_hash and rec["stamp"] == "stamp-1" and rec["levels"] == {"class": len(lazy.keys)}
        assert shadow.cost_model == rec["cost_model"] == "%s:stamp-1" % reg.model_hash
        assert rec["predicted_s"] >= rec["predicted_free_s"] >= 0
        if not shadow.trajectory:                                              # start and goal in one cell: nothing to drive
            continue
        n += 1
        assert rec["predicted_free_s"] > 0
        assert all(leg["predicted_s"] > 0 for leg in shadow.trajectory)
        assert sum(leg["predicted_s"] for leg in shadow.trajectory) == pytest.approx(rec["predicted_s"], rel=0.1, abs=0.1)
        assert not [k for k in rec if k.startswith("alt_")]                    # the served record holds no second search (C2)
        assert rec["tiles"] > 0 and rec["exact"] is True                       # P11: the served search's size
        alt = shadow.region_alt()
        changed += alt["alt_route_changed"]
        assert alt["alt_extra_m"] >= -1e-6                                     # the unpriced route is the shortest one
        assert alt["alt_tiles"] > 0 and alt["alt_exact"] is True
        if alt["alt_route_changed"]:
            assert alt["alt_nodes_differ"] and (alt["alt_extra_m"] > sr.ALT_MIN_EXTRA_M
                                                or alt["alt_predicted_s"] < rec["predicted_s"] * (1 - sr.ALT_MIN_GAIN))
        elif not alt["alt_nodes_differ"]:
            assert alt["alt_extra_m"] == 0.0 and alt["alt_predicted_s"] == pytest.approx(rec["predicted_s"], abs=0.01)
    assert n and (changed or name == "two_rooms")


def test_the_priced_alternative_is_faster_on_the_models_account(graphs):
    """Without turn time (the search prices none at lambda 0) the priced route is predicted no slower than the served one, to the sampling
    of the excess along a polyline."""
    name, eng, lazy = graphs
    reg = pricing(lazy, random_rho(lazy, 9, lo=500, hi=3000, zero_share=0.3))
    n = 0
    for p, q in _trial_pairs(eng, 14, 8):
        got = _plan(lazy, p, q, reg, "shadow", trajectory=0, turn=dict(TURN, angular_speed_max=None))
        if isinstance(got, str) or got.region_alt is None:
            continue
        rec, alt = got.pricing["region"], got.region_alt()
        assert alt["alt_predicted_s"] <= rec["predicted_s"] * 1.03 + 0.2, (rec, alt)
        n += 1
    assert n


def test_shadow_plan_does_not_search_the_alternative(graphs):
    """`graph.last` (read by the service's log line) is the served route's search: the plan runs no second search, it hands out the job."""
    name, eng, lazy = graphs
    reg = pricing(lazy, random_rho(lazy, 9, lo=500, hi=3000))
    n = 0
    for p, q in _trial_pairs(eng, 4, 3):
        plan(lazy, p, q)
        want = dict(lazy.last)
        got = plan(lazy, p, q, reg, "shadow")
        assert lazy.last == want
        if not isinstance(got, str) and got.region_alt is not None:
            assert got.pricing["region"]["tiles"] == want["tiles"]
            n += 1
    assert n


def test_the_alternative_job_is_shadow_only_and_reports_noise_as_unchanged(graphs):
    name, eng, lazy = graphs
    reg = pricing(lazy, random_rho(lazy, 9, lo=500, hi=3000, zero_share=0.3))
    for p, q in _trial_pairs(eng, 6, 8):
        for mode in ("off", "on"):
            got = plan(lazy, p, q, reg, mode)
            assert isinstance(got, str) or got.region_alt is None
    # equal routes under different node lists, or a gain below the thresholds, are not a change
    route = lazy.route(lazy.keys[0], lazy.keys[-1])
    times = lambda pts: (10.0, 9.0)                                         # noqa: E731
    pts = [tuple(p) for p in route.nodes]
    same = sr._region_alt(route, [pts[0], pts[-1]], times, 10.0, route, lazy)
    assert same["alt_route_changed"] is False and same["alt_nodes_differ"] is False and same["alt_extra_m"] == 0.0
    noisy = dataclasses.replace(route, nodes=list(reversed(route.nodes)))
    got = sr._region_alt(route, [pts[0], pts[-1]], times, 10.0, noisy, lazy)
    assert got["alt_nodes_differ"] is True and got["alt_route_changed"] is False
    got = sr._region_alt(route, [pts[0], pts[-1]], times, 10.1, noisy, lazy)           # 1 % faster: a change
    assert got["alt_route_changed"] is True
    assert sr._region_alt(route, [], times, 10.0, None, lazy) == {"alt_error": "no_route"}


def test_on_serves_the_priced_route_and_says_so(graphs):
    name, eng, lazy = graphs
    reg = pricing(lazy, random_rho(lazy, 9, lo=500, hi=3000, zero_share=0.3))
    n = 0
    for p, q in _trial_pairs(eng, 10, 8):
        got = plan(lazy, p, q, reg, "on")
        if isinstance(got, str) or not got.trajectory:
            continue
        rec = got.pricing["region"]
        assert rec["mode"] == "on" and rec["served"] == "priced" and "alt_route_changed" not in rec
        assert got.pricing["kappa"] == 0.0 and got.pricing["profile"] == "none" and got.pricing["cost"] > 0
        assert got.cost_model == rec["cost_model"]
        assert all("predicted_s" in leg for leg in got.trajectory)
        n += 1
    assert n


def test_a_neutral_snapshot_records_why_and_predicts_nothing_it_cannot(graphs):
    name, eng, lazy = graphs
    p, q = _trial_pairs(eng, 1, 3)[0]
    for status, snap in (("no_stats", pricing(lazy, [0] * len(lazy.keys), status="no_stats")),
                         ("version_mismatch", pricing(lazy, [300] * len(lazy.keys), version=lazy.version + 1)),
                         ("keys_mismatch", dataclasses.replace(pricing(lazy, [300] * len(lazy.keys)), keys=tuple(reversed(lazy.keys))))):
        for mode in ("shadow", "on"):
            got = plan(lazy, p, q, snap, mode)
            if isinstance(got, str):
                continue
            rec = got.pricing["region"]
            assert rec["status"] == status and "predicted_s" not in rec and got.cost_model is None
            assert all("predicted_s" not in leg for leg in got.trajectory or [])
            assert _wp(got) == _wp(plan(lazy, p, q))


def test_a_robot_without_a_real_speed_has_a_record_but_no_prediction(graphs):
    name, eng, lazy = graphs
    reg = pricing(lazy, [500] * len(lazy.keys))
    p, q = _trial_pairs(eng, 1, 3)[0]
    got = _plan(lazy, p, q, reg, "shadow", trajectory=5000, turn=dict(TURN, speed_max=None))
    if not isinstance(got, str):
        rec = got.pricing["region"]
        assert rec["status"] == "ok" and "predicted_s" not in rec and got.cost_model is None


def test_off_adds_nothing_to_the_plan(graphs):
    name, eng, lazy = graphs
    reg = pricing(lazy, random_rho(lazy, 9))
    for p, q in _trial_pairs(eng, 6, 2):
        base = _plan(lazy, p, q, trajectory=5000)
        got = _plan(lazy, p, q, reg, "off", trajectory=5000)
        assert type(base) is type(got)
        if not isinstance(base, str):
            assert got.pricing is None and got.trajectory == base.trajectory and got.cost_model is None
            assert all("predicted_s" not in leg for leg in got.trajectory or [])


def test_off_never_calls_the_region_search(graphs, monkeypatch):
    name, eng, lazy = graphs
    reg = pricing(lazy, random_rho(lazy, 9))
    seen = []
    real = lazy.route
    monkeypatch.setattr(lazy, "route", lambda *a, **kw: (seen.append(sorted(kw)), real(*a, **kw))[1])
    p, q = _trial_pairs(eng, 1, 2)[0]
    _plan(lazy, p, q, reg, "off")
    assert seen and all("region" not in kw for kw in seen)
    seen.clear()
    got = plan(lazy, p, q, reg, "shadow")
    assert len(seen) == 1 and not any("region" in kw for kw in seen)         # the plan searches the served route only (C2)
    if not isinstance(got, str) and got.region_alt is not None:
        got.region_alt()                                                    # the alternative, when the caller runs the job
        assert len(seen) == 2 and sorted(kw for kw in seen if "region" in kw) == [["goal", "region", "start"]]


# ---- the route node ----------------------------------------------------------------------------------------------
def poses():
    return [common.Pose2D(x=float(i), y=0.0, map_id="m", node_id=f"n{i}") for i in range(3)]


def test_the_route_digest_ignores_cost_model_and_predicted_s():
    bare = mission_object.MissionRouteNodeV1(waypoints=poses())
    leg = {"from_index": 0, "to_index": 1, "points": [[0.0, 0.0], [1.0, 0.0]]}
    traj = mission_object.MissionRouteNodeV1(waypoints=poses(), trajectory=[leg])
    modelled = mission_object.MissionRouteNodeV1(waypoints=poses(), trajectory=[{**leg, "predicted_s": 4.5}],
                                                 graph_version=3, cost_model="0123456789abcdef:st1")
    digest = mission_server._route_digest(bare)
    assert digest == mission_server._route_digest(traj) == mission_server._route_digest(modelled)
    moved = mission_object.MissionRouteNodeV1(waypoints=poses()[:2], cost_model="0123456789abcdef:st1")
    assert mission_server._route_digest(moved) != digest


def test_the_route_digest_is_the_one_computed_before_the_fields_existed():
    """The digest of a route without the new fields is the sha1 of the same json as before (a mission in flight at deploy must not read as
    rerouted)."""
    import hashlib
    route = mission_object.MissionRouteNodeV1(waypoints=poses(), graph_version=4)
    text = route.json(sort_keys=True, exclude_none=True,
                      exclude={"trajectory": True, "graph_version": True, "waypoints": {"__all__": {"chart_key"}}})
    assert "cost_model" not in text and "predicted" not in text
    assert mission_server._route_digest(route) == hashlib.sha1(text.encode()).hexdigest()[:16]
    with_model = mission_object.MissionRouteNodeV1(waypoints=poses(), graph_version=4, cost_model="a:b")
    assert mission_server._route_digest(with_model) == mission_server._route_digest(route)


def test_the_new_fields_round_trip():
    leg = {"from_index": 0, "to_index": 1, "points": [[0.0, 0.0], [1.0, 0.0]], "predicted_s": 4.5}
    node = mission_object.MissionRouteNodeV1(waypoints=poses(), trajectory=[leg], graph_version=3, cost_model="0123456789abcdef:st1")
    back = mission_object.MissionRouteNodeV1.parse_raw(node.json())
    assert back == node and back.cost_model == "0123456789abcdef:st1" and back.trajectory[0].predicted_s == 4.5
    assert mission_object.MissionRouteNodeV1.parse_obj(node.dict()) == node
    # an old stored route (no fields) loads, with None
    old = mission_object.MissionRouteNodeV1.parse_obj({"waypoints": [w.dict() for w in poses()],
                                                       "trajectory": [{k: v for k, v in leg.items() if k != "predicted_s"}]})
    assert old.cost_model is None and old.trajectory[0].predicted_s is None
    # without a prediction a leg serializes as it did before the field existed (stored specs and API answers of unmodelled routes)
    assert old.dict()["trajectory"][0] == {k: v for k, v in leg.items() if k != "predicted_s"}
    assert "predicted_s" not in old.json() and '"predicted_s": 4.5' in node.json()


def test_an_edited_route_loses_the_cost_model_with_the_graph_version():
    wps = [common.Pose2D(x=float(i), y=0.0, map_id="m", node_id=f"n{i}", chart_key=f"c{i}") for i in range(3)]
    node = mission_object.MissionRouteNodeV1(waypoints=wps, graph_version=3, cost_model="a:b")
    edited = mission_object.MissionRouteNodeV1(waypoints=[w.copy() for w in wps][:2] + [wps[2].copy(update={"x": 9.0})],
                                               graph_version=3, cost_model="a:b")
    assert edited.clear_stale_charts(node) is True
    assert edited.graph_version is None and edited.cost_model is None


def test_the_order_sent_to_the_robot_never_carries_the_prediction():
    from cloud_common.objects import robot
    from packages.controllers.mission import order_policy
    from packages.controllers.mission.vda5050_types import vda5050_types as v
    leg = {"from_index": 0, "to_index": 1, "points": [[0.0, 0.0], [1.0, 0.0]], "predicted_s": 4.5}
    node = mission_object.MissionRouteNodeV1(waypoints=poses(), trajectory=[leg], cost_model="0123456789abcdef:st1")
    order = v.VDA5050Order.from_route(node, robot.RobotObjectV1(name="r1", spec={}, status={}), "m1", 3, node_policy=False,
                                      policy=order_policy.OrderPolicy())
    text = order.json()
    for word in ("cost_model", "predicted", "0123456789abcdef", "4.5"):
        assert word not in text, word


async def test_the_dispatch_time_replan_keeps_the_cost_model():
    from tests.unit.test_mission_goto import _make_robot, _run_busy_then_goto
    planner = NS(plan=AsyncMock(return_value={
        "success": True, "planned_path": ["7", "8"], "graph_version": 11, "cost_model": "0123456789abcdef:st1",
        "trajectory": [{"from_index": 0, "to_index": 1, "points": [[51.0, 0.0], [60.0, 0.0]], "predicted_s": 37.5}],
        "waypoints": [{"x": 51.0, "y": 0.0, "theta": 0.0, "chart_key": "cA"},
                      {"x": 60.0, "y": 0.0, "theta": 0.0, "chart_key": "cB"}]}))
    r, db = _make_robot(planner)
    goto = await _run_busy_then_goto(r, move_to_x=50.0)
    route = goto.mission_tree[0].route
    assert route.cost_model == "0123456789abcdef:st1" and route.trajectory[0].predicted_s == 37.5
    stored = db.update_spec_fields.await_args_list[-1].args[2]["mission_tree"][0]["route"]
    assert stored["cost_model"] == "0123456789abcdef:st1" and stored["trajectory"][0]["predicted_s"] == 37.5


# ---- through the service ---------------------------------------------------------------------------------------
async def _served(monkeypatch, mode, maps=("m",), world="pillar_room", rho_seed=9):
    """(result of plan_route, service, snapshot cache fake, lazy graph) for a lazy graph behind the service, the robot at (1, 1)."""
    from packages import config
    from .test_planner_chart_route import FakeStore, _service
    st, eng, _ = _build(WORLDS[world]())
    lazy = open_plan(st, "m", max_tiles=10 ** 6)
    reg = pricing(lazy, random_rho(lazy, rho_seed, lo=500, hi=3000, zero_share=0.2))
    monkeypatch.setattr(config, "SCENE_GRAPH_PLANNER_ENABLED", True)
    monkeypatch.setattr(config, "REGION_COST_MODE", mode)
    monkeypatch.setattr(config, "REGION_COST_MAPS", maps)
    monkeypatch.setattr(config, "PLAN_TRAJECTORY_ENABLED", True)
    svc = _service(FakeStore(lazy, version=lazy.version))
    robot = await svc.get_robot_status("r1")
    robot.status.pose.x, robot.status.pose.y = 1.0, 1.0
    robot.status.factsheet.speed_max, robot.status.factsheet.angular_speed_max = SPEED, OMEGA
    gets = []

    class Snaps:
        async def get(self, map_id, version, keys, factsheet):
            gets.append((map_id, version))
            return reg

        def stats(self):
            return {}
    svc._region_snaps = Snaps()
    out = await svc.plan_route("r1", target_x=5.0, target_y=4.0, map_id="m")
    return out, svc, gets, lazy


@pytest.mark.parametrize("mode", ["off", "shadow", "on"])
async def test_the_service_gates_the_snapshot_by_mode(monkeypatch, mode):
    out, svc, gets, lazy = await _served(monkeypatch, mode)
    assert out["success"] and out["planner"] == sr.PLANNER_SCENE_GRAPH
    if mode == "off":
        assert gets == [] and "cost_model" not in out and "region" not in out["pricing"]
        assert svc.get_stats()["scene_graph"]["region_plans"] == {}
        return
    assert gets == [("m", lazy.version)]
    rec = out["pricing"]["region"]
    assert rec["mode"] == mode and out["cost_model"] == rec["cost_model"] and rec["predicted_s"] > 0
    assert svc.get_stats()["scene_graph"]["region_plans"] == {"ok": 1}
    assert all("predicted_s" in leg for leg in out["trajectory"])


async def test_shadow_through_the_service_serves_the_unchanged_route(monkeypatch):
    off, _, _, _ = await _served(monkeypatch, "off")
    shadow, svc, _, _ = await _served(monkeypatch, "shadow")
    assert [(w.x, w.y) for w in shadow["waypoints"]] == [(w.x, w.y) for w in off["waypoints"]]
    assert shadow["chart_keys"] == off["chart_keys"] and shadow["path"] == off["path"]
    assert without_predictions(shadow["trajectory"]) == off["trajectory"]
    await _settle(svc)
    assert svc.get_stats()["scene_graph"]["region_shadow_changed"] in (0, 1)
    assert svc.get_stats()["scene_graph"]["region_alt"] == {"done": 1}


async def _settle(svc):
    import asyncio
    while svc._alt_tasks:
        await asyncio.wait(list(svc._alt_tasks))


async def test_a_slow_shadow_alternative_never_delays_or_alters_the_served_plan(monkeypatch):
    """The alternative is a task after the plan: a job that blocks (past its timeout) leaves the plan, served at once, as it is, and is
    recorded as skipped."""
    import threading
    from packages import config
    from packages.services.mission_planner import scene_route
    off, _, _, _ = await _served(monkeypatch, "off")
    release = threading.Event()
    real = scene_route._region_alt
    monkeypatch.setattr(scene_route, "_region_alt", lambda *a, **k: (release.wait(10), real(*a, **k))[1])
    monkeypatch.setattr(config, "REGION_COST_ALT_TIMEOUT_S", 0.2)
    try:
        shadow, svc, _, _ = await _served(monkeypatch, "shadow")
        assert svc._alt_tasks                                                # still pending when the plan came back
        assert [(w.x, w.y) for w in shadow["waypoints"]] == [(w.x, w.y) for w in off["waypoints"]] and shadow["path"] == off["path"]
        assert "alt_timeout" not in svc.get_stats()["scene_graph"]["region_alt"]
        await _settle(svc)
        assert svc.get_stats()["scene_graph"]["region_alt"] == {"alt_timeout": 1}
        assert svc.get_stats()["scene_graph"]["region_shadow_changed"] == 0
    finally:
        release.set()


async def test_a_busy_alt_pool_skips_the_alternative(monkeypatch):
    import threading
    from packages import config
    from packages.services.mission_planner import scene_route
    release = threading.Event()
    real = scene_route._region_alt
    monkeypatch.setattr(scene_route, "_region_alt", lambda *a, **k: (release.wait(10), real(*a, **k))[1])
    monkeypatch.setattr(config, "REGION_COST_ALT_TIMEOUT_S", 0.05)
    monkeypatch.setattr(config, "REGION_COST_ALT_MAX_INFLIGHT", 1)
    try:
        out, svc, _, _ = await _served(monkeypatch, "shadow")
        await _settle(svc)
        assert svc.get_stats()["scene_graph"]["region_alt"] == {"alt_timeout": 1}       # still running: holds the slot
        svc._start_region_alt("m", 1, lambda: {})
        await _settle(svc)
        assert svc.get_stats()["scene_graph"]["region_alt"] == {"alt_timeout": 1, "alt_busy": 1}
    finally:
        release.set()


async def test_a_map_outside_the_allowlist_is_off_through_the_service(monkeypatch):
    out, svc, gets, _ = await _served(monkeypatch, "on", maps=("other",))
    assert gets == [] and "cost_model" not in out


async def test_plan_and_submit_passes_the_cost_model_to_the_mission(monkeypatch):
    out, svc, gets, lazy = await _served(monkeypatch, "shadow")
    mission, error = svc.create_mission("r1", out["waypoints"], trajectory=out["trajectory"], graph_version=out["graph_version"],
                                        cost_model=out["cost_model"])
    assert error is None
    route = mission.mission_tree[0].route
    assert route.cost_model == out["cost_model"] and route.trajectory[0].predicted_s == out["trajectory"][0]["predicted_s"]
