"""The reference worlds: what the scene graph must make of them, and the properties that must always hold."""
import math
import random

import numpy as np
import pytest

from packages.scene_graph import Params, SceneGraph
from packages.scene_graph.levels import build_levels
from packages.scene_graph.metrics import invariants
from packages.scene_graph.routing import Router

from .synth import drive
from .worlds import ALL

pytestmark = pytest.mark.unit
RES = Params().res


def _build(name, **kw):
    world, path = ALL[name]()
    leaves = drive(world, path)
    sg = SceneGraph(Params(**kw))
    for lf in leaves:
        sg.add_leaf(lf)
    return sg, leaves


@pytest.fixture(scope="module")
def built():
    return {n: _build(n) for n in ALL}


def _xy(a):
    return (a.ci + 0.5) * RES, (a.cj + 0.5) * RES


def test_open_spaces_are_one_region_with_a_central_anchor(built):
    sg, _ = built["empty_room"]
    assert len(sg.anchors) == 1
    x, y = _xy(next(iter(sg.anchors.values())))
    assert math.hypot(x - 4.5, y - 3.5) < 1.0              # the room is 8 x 6 m, 0.5 m padding
    for name in ("corridor", "l_room"):
        assert len(built[name][0].anchors) == 1


def test_a_door_makes_two_places_joined_at_the_door_width(built):
    sg, _ = built["two_rooms_door"]
    assert len(sg.anchors) == 2
    xs = sorted(_xy(a)[0] for a in sg.anchors.values())
    assert xs[0] < 7.5 < xs[1]                              # one anchor per room
    links = sg.links()
    assert len(links) == 1 and links[0].width == pytest.approx(0.9, abs=0.15)


def test_an_island_makes_a_loop_of_parallel_links(built):
    sg, _ = built["pillar_room"]
    links = sg.links()
    assert len(sg.anchors) >= 2
    assert len(links) >= len(sg.anchors)                    # a cycle: at least as many edges as nodes
    a = {(l.a, l.b) for l in links}
    assert len(links) > len(a) or len(sg.anchors) >= 3      # parallel links, or a ring of three or more


def test_a_ring_corridor_is_cut_into_charts_that_close_a_loop(built):
    sg, _ = built["ring_corridor"]
    assert len(sg.anchors) >= 3
    assert len(sg.links()) >= len(sg.anchors)


@pytest.mark.parametrize("name", list(ALL))
def test_invariants(built, name):
    sg, _ = built[name]
    m = invariants(sg)
    assert m["visited_unowned"] == 0 and m["bad_root"] == 0 and m["disconnected_nodes"] == 0
    assert m["over_budget"] == 0
    p = sg.p
    assert m["max_dilation"] <= p.tau * (1 + p.hysteresis) + 0.1


def test_adding_a_leaf_again_changes_nothing(built):
    sg, leaves = _build("two_rooms_door")
    before = {i: (a.ci, a.cj, a.n) for i, a in sg.anchors.items()}
    sg.add_leaf(leaves[-1])
    assert {i: (a.ci, a.cj, a.n) for i, a in sg.anchors.items()} == before


def test_ids_of_places_survive_more_observations():
    world, path = ALL["two_rooms_door"]()
    leaves = drive(world, path)
    sg = SceneGraph(Params())
    for lf in leaves[:20]:
        sg.add_leaf(lf)
    ids_mid = set(sg.anchors)
    for lf in leaves[20:]:
        sg.add_leaf(lf)
    assert ids_mid & set(sg.anchors)                        # the first room keeps its id


def test_the_structure_does_not_depend_on_the_order_of_arrival():
    world, path = ALL["two_rooms_door"]()
    leaves = drive(world, path)
    for seed in range(3):
        order = list(leaves)
        random.Random(seed).shuffle(order)
        sg = SceneGraph(Params())
        for lf in order:
            sg.add_leaf(lf)
        assert len(sg.anchors) == 2
        assert sg.links()[0].width == pytest.approx(0.9, abs=0.2)


def test_levels_coarsen_the_two_rooms_into_one(built):
    sg, _ = built["two_rooms_door"]
    levels = build_levels(sg)
    assert len(levels[0].nodes) == 2 and len(levels[-1].nodes) == 1
    top = levels[-1].nodes[0]
    assert sorted(m for m in top.members) == sorted(n.id for n in levels[-2].nodes) or top.cells == sum(
        a.n for a in sg.anchors.values())


def test_routing_through_portals_is_close_to_the_true_shortest_path(built):
    from scipy.sparse.csgraph import dijkstra
    for name in ("two_rooms_door", "pillar_room"):
        sg, _ = built[name]
        rt = Router(sg)
        reg, own = rt.reg, rt.own
        rng = np.random.default_rng(0)
        cand = np.nonzero(own >= 0)[0]
        ratios = []
        while len(ratios) < 40:
            u, v = rng.choice(cand, 2)
            if own[u] == own[v]:
                continue
            true = dijkstra(reg.g.cost, directed=False, indices=int(u))[v]
            ratios.append(rt.cost(int(u), int(v)) / true)
        assert np.median(ratios) < 1.1 and max(ratios) < 1.5
