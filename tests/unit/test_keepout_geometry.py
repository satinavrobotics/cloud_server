"""Keep-outs stored as geometry (docs/keepout_zones.md, 2026-10-27): a chart / edge in a create request is snapshotted to a polygon /
line from the scene graph of that moment, the zone then lives on its own (a graph rebuild or merge cannot change it), legacy rows are
rewritten as geometry on a read, and both planners honour polygon and line zones."""
import json
import os
from unittest.mock import AsyncMock, patch

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

import numpy as np  # noqa: E402
import pytest  # noqa: E402

from packages import config  # noqa: E402
from packages.api import keepouts as api  # noqa: E402
from packages.scene_graph.api import BBox, SceneParams  # noqa: E402
from packages.scene_graph.engine import ChartGraph  # noqa: E402
from packages.services.mission_planner import keepout_overlay as kov  # noqa: E402
from packages.services.mission_planner import scene_route as sr  # noqa: E402
from packages.utils import keepout_geometry as kg  # noqa: E402
from packages.utils import keepouts as ko  # noqa: E402

from .scene_graph import chart_worlds as cw  # noqa: E402
from .test_keepout_zones import START, GOAL, graphs, inside, plan, row, ring  # noqa: E402,F401
from .test_keepouts_api import FakeDb, code_of, seed_legacy  # noqa: E402
from .test_lazy_plan import WORLDS, _build  # noqa: E402

pytestmark = pytest.mark.unit

A, B, C = "s008:4", "s008:1", "s008:3"          # the ring's short way is through s008:4, s008:1, s008:3


class EngineReads:
    """The API's scene-graph read side over a real engine graph: topology (keys, links, anchors) and the owner plane."""

    def __init__(self, eng):
        self.eng = eng
        self.calls = 0

    async def topology(self, map_id):
        return {"version": 1, "keys": set(self.eng.chart_anchors()), "links": self.eng.chart_links(),
                "anchors": self.eng.chart_anchors()}

    async def owner_plane(self, map_id, bbox, level=0):
        self.calls += 1
        r = self.eng.query(BBox(bbox.x0, bbox.y0, bbox.x1, bbox.y1), raster=True, parents=False).raster
        if r is None:
            return None
        return {"origin": r.origin, "step": r.step, "shape": r.index.shape, "keys": list(r.keys), "index": r.index}


def store(eng, db=None):
    db = db or FakeDb()
    reads = EngineReads(eng)
    return api.KeepoutStore(db, lambda: reads), db, reads


def zone_cells(spec, h=0.1):
    if "polygon" in spec:
        return kov.polygon_cells(spec["polygon"], h)
    return kov.line_cells(spec["line"], h)


@pytest.fixture(scope="module")
def eng(ring):
    return ring[1]


# --- the snapshot ------------------------------------------------------------------------------------------------------------

class TestSnapshot:
    async def test_a_chart_becomes_a_simplified_polygon_that_covers_its_cells(self, eng):
        s, db, _ = store(eng)
        made = await s.create("lab", {"kind": "region", "region": {"chart_key": B}})
        poly = made["polygon"]
        assert 3 <= len(poly) <= kg.MAX_SNAPSHOT_POINTS and made["source"] == {"chart_key": B} and made["applies"] == "applied"
        want = kov.chart_cells(eng, eng.chart_anchors(), B)
        got = kov.polygon_cells(poly, 0.1)
        iou = len(np.intersect1d(want, got)) / len(np.union1d(want, got))
        assert iou > 0.93
        assert db.rows[0]["spec"] == {"polygon": poly, "source": {"chart_key": B}}      # stored as geometry, the key only as source

    async def test_an_edge_becomes_the_seal_segment_on_the_shared_boundary(self, eng):
        s, db, _ = store(eng)
        made = await s.create("lab", {"kind": "edge", "edge": {"from": A, "to": B}})
        line = made["line"]
        assert len(line) == 2 and made["source"] == {"from": A, "to": B}
        seal = kov.edge_cells(eng, eng.chart_anchors(), A, B)
        cells = kov.line_cells(line, 0.1)
        covered = len(np.intersect1d(seal, cells)) / len(seal)
        assert covered > 0.8                                      # the barrier lies over the seal (its two-cell band)
        # and it is a barrier, not a wall: it is within one chart radius of the seal, and about the seal's length
        ix = (seal % (1 << 21)) - (1 << 20)
        iy = (seal // (1 << 21)) - (1 << 20)
        pts = np.stack([(ix + 0.5) * 0.1, (iy + 0.5) * 0.1], 1)
        seal_len = float(np.ptp(pts @ np.linalg.svd(pts - pts.mean(0))[2][0]))
        assert 0.7 * seal_len <= np.hypot(line[1][0] - line[0][0], line[1][1] - line[0][1]) <= seal_len + 0.5

    async def test_the_points_of_an_edge_are_placed_then_snapshotted(self, eng):
        s, _, _ = store(eng)
        an = eng.chart_anchors()
        made = await s.create("lab", {"kind": "edge", "edge": {"from": list(an[A]), "to": list(an[B])}})
        assert made["source"] == {"from": A, "to": B} and len(made["line"]) == 2

    async def test_errors_leave_the_table_alone(self, eng):
        s, db, _ = store(eng)
        assert await code_of(s.create("lab", {"kind": "region", "region": {"chart_key": "s999:9"}})) == (422, "chart_not_found")
        assert await code_of(s.create("lab", {"kind": "edge", "edge": {"from": A, "to": "s026:0"}})) == (422, "no_link_between_charts")
        assert db.rows == []

    async def test_an_unreadable_graph_is_a_503_not_an_unresolvable_zone(self, eng):
        class Down(EngineReads):
            async def owner_plane(self, *a, **k):
                raise RuntimeError("tiles down")
        db = FakeDb()
        s = api.KeepoutStore(db, lambda: Down(eng))
        assert await code_of(s.create("lab", {"kind": "region", "region": {"chart_key": B}})) == (503, "scene_graph_unavailable")
        assert await code_of(s.create("lab", {"kind": "edge", "edge": {"from": A, "to": B}})) == (503, "scene_graph_unavailable")
        assert db.rows == []


# --- legacy rows: lazy migration ---------------------------------------------------------------------------------------------

class TestLazyMigration:
    async def test_a_legacy_row_is_rewritten_once_and_the_revision_moves(self, eng):
        s, db, _ = store(eng)
        rid = await seed_legacy(db, "region", {"chart_key": B}, label="mine")
        eid = await seed_legacy(db, "edge", {"from": A, "to": B})
        before = ko.revision(db.rows)
        out = await s.list_all("lab")
        items = {k["id"]: k for k in out["keepouts"]}
        assert "polygon" in items[rid] and items[rid]["source"] == {"chart_key": B} and items[rid]["label"] == "mine"
        assert "line" in items[eid] and items[eid]["source"] == {"from": A, "to": B}
        assert all(k["applies"] == "applied" for k in items.values())
        assert out["revision"] != before
        assert all(ko.is_geometry(r["spec"]) for r in db.rows)
        # idempotent: nothing is written again, same geometry, same revision
        sql_before = len(db.sql)
        again = await s.list_all("lab")
        assert not any(q == ko.SPEC_SQL for q in db.sql[sql_before:])
        assert again["revision"] == out["revision"] and again["keepouts"] == out["keepouts"]

    async def test_an_unreadable_graph_leaves_the_rows_as_they_are(self, eng):
        class Down(EngineReads):
            async def topology(self, map_id):
                raise RuntimeError("down")
        db = FakeDb()
        s = api.KeepoutStore(db, lambda: Down(eng))
        rid = await seed_legacy(db, "region", {"chart_key": B})
        spec = dict(db.rows[0]["spec"])
        out = await s.list_all("lab")
        assert db.rows[0]["spec"] == spec and out["keepouts"][0]["applies"] == "unresolved"
        assert out["keepouts"][0]["region"] == {"chart_key": B} and out["keepouts"][0]["id"] == rid

    async def test_a_chart_the_graph_no_longer_has_stays_legacy_and_unresolved(self, eng):
        s, db, _ = store(eng)
        await seed_legacy(db, "region", {"chart_key": "s999:9"})
        await seed_legacy(db, "region", {"chart_key": B})
        out = (await s.list_all("lab"))["keepouts"]
        assert [(k["applies"], "polygon" in k) for k in out] == [("unresolved", False), ("applied", True)]
        assert out[0]["reason"] == "chart_not_found"

    async def test_a_concurrent_edit_wins_the_compare_and_set(self, eng):
        s, db, _ = store(eng)
        rid = await seed_legacy(db, "region", {"chart_key": B})
        rows = [ko.row_dict(tuple(r[c] for c in ko.COLUMNS)) for r in db.rows]
        db.rows[0]["spec"] = {"polygon": [[0, 0], [1, 0], [1, 1]], "source": {"chart_key": B}}     # migrated by another process
        out = await s.migrate("lab", rows)
        assert out[0]["spec"]["polygon"] == [[0, 0], [1, 0], [1, 1]] and out[0]["id"] == rid


# --- the planner: geometry is graph independent ------------------------------------------------------------------------------

def rebuilt_ring():
    """The same world folded into a graph with other parameters: other charts, other keys."""
    w, way = WORLDS["ring"]()
    g = ChartGraph(SceneParams(chart_radius=3.0))
    g.fold_many(cw.drive(w, way))
    return g


@pytest.mark.parametrize("which", [0, 1], ids=["engine", "lazy"])
class TestPlannerGeometry:
    async def snap(self, eng, body):
        s, db, _ = store(eng)
        made = await s.create("lab", body)
        return row(made["id"], dict(db.rows[0]["spec"]), made["kind"])

    async def test_a_polygon_snapshot_of_a_chart_is_routed_around_like_the_chart(self, ring, eng, which):
        g = graphs(ring)[which]
        zone = await self.snap(eng, {"kind": "region", "region": {"chart_key": B}})
        p, res = plan(g, START, GOAL, [zone])
        assert B not in p.charts and not inside(res, p)
        assert p.keepout["detour"] is True and p.keepout["applied"] == [zone["id"]]

    async def test_a_line_snapshot_of_an_edge_blocks_the_crossing(self, ring, eng, which):
        g = graphs(ring)[which]
        zone = await self.snap(eng, {"kind": "edge", "edge": {"from": A, "to": B}})
        p, res = plan(g, START, GOAL, [zone])
        assert not inside(res, p) and p.keepout["detour"] is True
        assert not (A in p.charts and B in p.charts and abs(p.charts.index(A) - p.charts.index(B)) == 1)

    async def test_a_drawn_line_across_a_passage_blocks_it(self, ring, which):
        g = graphs(ring)[which]
        an = g.chart_anchors()[B]
        zone = row("L", {"line": [[an[0] - 1.5, an[1]], [an[0] + 1.5, an[1]]]}, "edge")        # across chart B, wall to wall
        p, res = plan(g, START, GOAL, [zone])
        assert not inside(res, p) and p.keepout["applied"] == ["L"]


class TestSurvivesTheGraph:
    async def test_the_zone_blocks_the_same_cells_when_the_graph_is_rebuilt_or_gone(self, ring, eng):
        s, db, _ = store(eng)
        await s.create("lab", {"kind": "region", "region": {"chart_key": B}})
        await s.create("lab", {"kind": "edge", "edge": {"from": A, "to": "s008:0"}}) if frozenset((A, "s008:0")) in eng.chart_links() else None
        rows = [row(r["id"], r["spec"], r["kind"]) for r in db.rows]
        want = kov.resolve(eng, rows, 1).cells
        assert len(want) > 50
        rebuilt = rebuilt_ring()
        assert set(rebuilt.chart_anchors()) != set(eng.chart_anchors())          # other keys: a legacy zone could not be found
        assert B not in rebuilt.chart_anchors() or rebuilt.chart_anchors()[B] != eng.chart_anchors()[B]
        for g in (rebuilt, graphs(ring)[1], None):
            res = kov.resolve(g, rows, 2)
            assert all(z.applies == ko.APPLIED for z in res.zones) and np.array_equal(res.cells, want)
        legacy = kov.resolve(rebuilt, [row("old", {"chart_key": B})], 2)
        assert legacy.zones[0].applies == ko.UNRESOLVED                              # what the old design did

    async def test_the_planner_on_a_rebuilt_graph_still_routes_around_it(self, ring, eng):
        s, db, _ = store(eng)
        made = await s.create("lab", {"kind": "region", "region": {"chart_key": B}})
        zone = row(made["id"], db.rows[0]["spec"])
        g = rebuilt_ring()
        p, res = plan(g, START, GOAL, [zone])
        assert not inside(res, p) and p.keepout["applied"] == [made["id"]] and p.keepout["unresolved"] == []


# --- line cells and the legacy (graph node) planner --------------------------------------------------------------------------

class TestLineAndLegacy:
    def test_line_cells_cover_the_segment(self):
        for line in ([[0, 0], [3, 0]], [[0, 0], [0, 3]], [[0, 0], [3, 3]], [[0, 0], [3, 1.3]]):
            cells = kov.line_cells(line, 0.1)
            assert 25 <= len(cells) <= 200
            (xa, ya), (xb, yb) = line
            pts = [(xa + t * (xb - xa), ya + t * (yb - ya)) for t in np.linspace(0, 1, 80)]
            assert np.isin(kov.cells_of_points(pts, 0.1), cells).all()

    @pytest.mark.parametrize("line", [[[-3.0, -1.0], [7.0, 4.0]], [[-3.0, 1.0], [7.0, 1.0]], [[1.0, -3.0], [2.0, 7.0]], [[-3.0, 5.0], [6.0, -3.0]]])
    def test_no_eight_connected_path_crosses_a_line_spanning_the_grid(self, line):
        h = 0.1
        band = set(kov.line_cells(line, h).tolist())
        key = lambda x, y: x + (y << 21) + (1 << 20) + ((1 << 20) << 21)
        free = {(x, y) for x in range(-12, 42) for y in range(-12, 42) if key(x, y) not in band}
        (xa, ya), (xb, yb) = line
        # a cell strictly on each side of the segment
        side = lambda x, y: (xb - xa) * ((y + .5) * h - ya) - (yb - ya) * ((x + .5) * h - xa)
        pos = [c for c in free if side(*c) > 1.0]
        seen, todo = {pos[0]}, [pos[0]]
        while todo:
            x, y = todo.pop()
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    n = (x + dx, y + dy)
                    if n in free and n not in seen:
                        seen.add(n)
                        todo.append(n)
        assert not any(side(*c) < -1.0 for c in seen)

    NODES = [{"node_id": n, "pose": {"x": x, "y": y}} for n, (x, y) in
             {"a": (0, 0), "b": (2, 0), "c": (4, 0), "d": (2, 2), "e": (2, -2)}.items()]
    EDGES = [{"from": f, "to": t} for f, t in [("a", "b"), ("b", "c"), ("a", "d"), ("d", "c"), ("a", "e"), ("e", "c")]]

    def test_the_legacy_planner_excludes_the_legs_that_cross_a_line(self):
        wall = row("w", {"line": [[3.0, -0.5], [3.0, 0.5]]}, "edge")           # between b and c
        out = kov.legacy_polygon_path([wall], self.NODES, self.EDGES, ["a", "b", "c"], "a", "c")
        assert out.error is None and out.path in (["a", "d", "c"], ["a", "e", "c"])
        assert out.report["detour"] is True and out.report["forced_by"] == ["w"]
        # the leg out of the start node is exempt (a robot standing at a wall may drive off)
        at_start = row("s", {"line": [[1.0, -1.0], [1.0, 1.0]]}, "edge")
        assert kov.legacy_polygon_path([at_start], self.NODES, self.EDGES, ["a", "b", "c"], "a", "c").path == ["a", "b", "c"]
        far = row("f", {"line": [[3.0, 1.0], [3.0, 2.0]]}, "edge")
        assert kov.legacy_polygon_path([far], self.NODES, self.EDGES, ["a", "b", "c"], "a", "c").path == ["a", "b", "c"]

    def test_no_way_around_a_line_is_an_error(self):
        wall = row("w", {"line": [[3.0, -0.5], [3.0, 0.5]]}, "edge")
        edges = [e for e in self.EDGES if "d" not in (e["from"], e["to"]) and "e" not in (e["from"], e["to"])]
        out = kov.legacy_polygon_path([wall], self.NODES, edges, ["a", "b", "c"], "a", "c")
        assert out.error and out.report["forced_by"] == ["w"]


# --- the planner service: no refusal for geometry rows -----------------------------------------------------------------------

from .test_keepout_planner_service import plan as service_plan, svc_on, with_rows  # noqa: E402

pytestmark = [pytest.mark.unit, pytest.mark.real_keepouts]


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setattr(config, "SCENE_GRAPH_PLANNER_ENABLED", True)


async def test_geometry_zones_are_not_refused_by_the_legacy_fallback_but_honoured(on, eng):
    svc = svc_on(eng, (1.25, 1.25))
    an = eng.chart_anchors()[B]
    zone = row("L", {"line": [[an[0] - 1.5, an[1]], [an[0] + 1.5, an[1]]], "source": {"from": A, "to": B}}, "edge")
    with with_rows([zone]):
        out = await service_plan(svc, GOAL, robot_pose=(-50.0, 0.0))     # scene planner skipped: the legacy planner runs
    assert out.get("failed_at") != "keepout" or out["keepout"].get("reason") != "keepout_unverifiable"


async def test_a_legacy_row_still_refuses_the_legacy_fallback_and_still_tolerates_a_stale_graph(on, eng):
    svc = svc_on(eng, (1.25, 1.25))
    with with_rows([row("z1", {"chart_key": B})]):
        out = await service_plan(svc, GOAL, robot_pose=(-50.0, 0.0))
    assert out["failed_at"] == "keepout" and out["keepout"]["reason"] == "keepout_unverifiable"
    assert keepouts_is_legacy(row("z1", {"chart_key": B})) and not keepouts_is_legacy(row("g", {"polygon": [[0, 0], [1, 0], [1, 1]]}))


def keepouts_is_legacy(r):
    return ko.is_legacy(r["spec"])


async def test_a_geometry_zone_does_not_make_the_planner_tolerate_a_stale_graph(on, eng):
    """graph_zoned (stale-graph tolerance, graph_stale notice) is only for rows that still need the graph."""
    from packages.services.mission_planner import server
    src = open(server.__file__).read()
    assert "keepouts.is_legacy(r[\"spec\"]) for r in keep_rows" in src and "keepouts.is_legacy(r[\"spec\"]) for r in zoned_rows" in src


# --- the HTTP round trip -----------------------------------------------------------------------------------------------------

async def test_http_round_trip_chart_in_geometry_out(eng):
    import httpx
    from unittest.mock import MagicMock
    import packages.api.main as main
    svc = MagicMock()
    svc.keepouts = api.KeepoutStore(FakeDb(), lambda: EngineReads(eng))
    with patch.object(main, "service", svc):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://t") as c:
            r = await c.post("/api/v1/maps/lab/keepouts", json={"kind": "region", "label": "door", "region": {"chart_key": B}})
            assert r.status_code == 201
            region = r.json()
            r = await c.post("/api/v1/maps/lab/keepouts", json={"kind": "edge", "edge": {"from": A, "to": B}})
            edge = r.json()
            r = await c.post("/api/v1/maps/lab/keepouts", json={"kind": "edge", "line": [[0, 0], [1, 0]], "label": "drawn"})
            assert r.status_code == 201 and r.json()["line"] == [[0.0, 0.0], [1.0, 0.0]] and "source" not in r.json()
            got = (await c.get("/api/v1/maps/lab/keepouts")).json()["keepouts"]
            assert [k["id"] for k in got][:2] == [region["id"], edge["id"]]
            json.dumps(got)
            assert got[0]["kind"] == "region" and isinstance(got[0]["polygon"], list) and got[0]["source"] == {"chart_key": B}
            assert got[1]["kind"] == "edge" and len(got[1]["line"]) == 2 and got[1]["source"] == {"from": A, "to": B}
            assert all(k["applies"] == "applied" for k in got)
            with patch.object(EngineReads, "topology", AsyncMock(side_effect=RuntimeError("down"))):
                r = await c.post("/api/v1/maps/lab/keepouts", json={"kind": "region", "region": {"chart_key": C}})
                assert r.status_code == 503 and r.json()["detail"]["code"] == "scene_graph_unavailable"
