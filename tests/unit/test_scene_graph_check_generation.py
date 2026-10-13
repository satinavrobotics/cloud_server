"""tools/scene_graph_check_generation: a saved graph passes, a wrong expectation and a missing map fail."""
import pytest

from packages.scene_graph.api import SceneParams
from packages.scene_graph.engine import ChartGraph
from packages.scene_graph.store import MemoryBackend, SceneGraphStore
from tests.unit.scene_graph import chart_worlds as cw
from tools import scene_graph_check_generation as chk

pytestmark = pytest.mark.unit


def _stored():
    w, way = cw.two_rooms(W=12.0)
    g = ChartGraph(SceneParams())
    d = g.fold_many(cw.drive(w, way))
    be = MemoryBackend()
    st = SceneGraphStore(be, be)
    st.save("m", g, d, None, {})
    return st, g


def test_a_saved_graph_passes_and_reports_its_counts():
    st, g = _stored()
    r = chk.check_map(st, "m")
    assert r["problems"] == [] and r["charts"] >= 2 and r["links"] >= 1 and r["route"]["length"] > 0
    assert r["format"] == 4 and r["pipeline"] == "morse/2" and r["levels"] >= 1


def test_expectations_and_missing_maps_are_failures():
    st, _ = _stored()
    r = chk.check_map(st, "m", {"charts": 99})
    assert any("charts" in p for p in r["problems"])
    assert chk.check_map(st, "nope")["problems"] == ["no stored graph"]
    assert chk.main(["--map", "m"], store=st) == 0 and chk.main(["--map", "nope"], store=st) == 1


class _C:
    def __init__(self, key, cells=10, anchor=(0.0, 0.0)):
        self.key, self.cells, self.anchor = key, cells, anchor


class _L:
    def __init__(self, a, b):
        self.a, self.b = a, b


def test_islands_are_charts_outside_the_largest_link_component():
    charts = [_C("a", anchor=(0, 0)), _C("b", anchor=(1, 0)), _C("c", anchor=(2, 0)), _C("island", cells=3, anchor=(9, 9))]
    links = [_L("a", "b"), _L("b", "c")]
    assert chk.link_components(charts, links) == [["a", "b", "c"], ["island"]]
    isl = chk.islands(charts, links)
    assert [i["key"] for i in isl] == ["island"] and isl[0]["cells"] == 3
    assert chk.islands(charts, links + [_L("c", "island")]) == []


def test_two_islands_and_a_main_component_sorted_largest_first():
    charts = [_C(k) for k in ("x", "y", "p", "q", "r")]
    links = [_L("x", "y"), _L("p", "q")]
    comps = chk.link_components(charts, links)
    assert comps == [["p", "q"], ["x", "y"], ["r"]]           # equal sizes: by key, so the main component is deterministic
    assert [i["key"] for i in chk.islands(charts, links)] == ["x", "y", "r"]
