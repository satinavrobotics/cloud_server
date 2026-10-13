import dataclasses

import pytest

from packages.scene_graph.api import (Blocked, Chart, Dirty, Leaf, Link, MissingTiles, OwnerRaster, Route, SceneGraph, SceneParams,
                                      ViewResult)
from packages.scene_graph.store import SceneGraphStore

pytestmark = pytest.mark.unit


def test_params_carry_the_scales():
    p = SceneParams()
    assert (p.robot_half_side, p.lattice, p.chart_radius) == (0.25, 0.1, 2.0)


def test_leaf_carries_what_the_fold_needs():
    names = {f.name for f in dataclasses.fields(Leaf)}
    assert {"key", "session", "stamp_ms", "x", "y", "yaw", "resolution", "origin", "window_u8", "r_d", "channels"} <= names


def test_graph_carries_evidence_and_length_never_a_price():
    assert {"driven_frac", "ever_blocked_frac"} <= {f.name for f in dataclasses.fields(Chart)}
    link = {f.name for f in dataclasses.fields(Link)}
    assert {"length", "width", "channels"} <= link and not ({"price", "weight", "penalty"} & link)
    assert {"min_width", "length", "charts"} <= {f.name for f in dataclasses.fields(Route)}


def test_gaps_closed_for_laci():
    assert "origin_yaw" in {f.name for f in dataclasses.fields(Leaf)}
    assert "leaf" in {f.name for f in dataclasses.fields(Chart)}
    assert {"raster"} <= {f.name for f in dataclasses.fields(ViewResult)}
    assert {"index", "keys", "dependent", "origin", "step"} <= {f.name for f in dataclasses.fields(OwnerRaster)}
    assert dataclasses.fields(Dirty)[2].name == "retired" and "Dict" in str(dataclasses.fields(Dirty)[2].type)
    assert MissingTiles([(0, 1)]).tiles == [(0, 1)] and hasattr(SceneGraph, "corridor")


def test_base_class_is_the_contract_only_and_the_store_has_the_agreed_surface():
    sg = SceneGraph(SceneParams())
    with pytest.raises(NotImplementedError):
        sg.fold_many([])
    for name in ("save", "load", "view", "meta", "watermark", "lineage", "gc", "begin_generation", "commit_generation",
                 "tiles_for", "drop"):
        assert callable(getattr(SceneGraphStore, name))
    assert Dirty(None, [], {}, [], (0, "")).retired == {} and Blocked().discs == []
