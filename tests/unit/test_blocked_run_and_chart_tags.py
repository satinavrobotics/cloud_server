"""run id of a blocked report across repeat passes (resolve_run_ids) and chart keys on the
blocked-nodes list (`?with_chart_keys=true`)."""
import asyncio
import datetime
import os
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

import httpx  # noqa: E402
import numpy as np  # noqa: E402
import pytest  # noqa: E402

import packages.api.main as api_main  # noqa: E402
from packages.controllers.mission import fleet_recorder, order_ids  # noqa: E402
from packages.utils import blocked_chart_tags as tags  # noqa: E402
from packages.utils import blocked_nodes as bn  # noqa: E402

pytestmark = pytest.mark.unit

UTC = datetime.timezone.utc
T0 = datetime.datetime(2026, 10, 12, 10, 0, tzinfo=UTC)


def _row(mission="m", pass_id="0a1b2c3d", robot="r1", created=T0 + datetime.timedelta(minutes=30),
         x=5.0, y=1.0, **extra):
    prefix = order_ids.run_prefix(mission, pass_id)
    row = {"map_name": "M", "graph_node_id": "n1", "mission_name": mission, "robot_name": robot,
           "vda_node_id": order_ids.node_id(prefix, 0, 2), "x": x, "y": y,
           "created_at": created.isoformat(), "robot_x": None, "robot_y": None, **extra}
    row["run_id"] = bn.run_id_of(row)
    return row


class _Db:
    def __init__(self, runs=(), fail=False):
        self.runs, self.fail, self.sql = list(runs), fail, []

    @asynccontextmanager
    async def connection(self):
        if self.fail:
            raise RuntimeError("down")
        yield self

    def cursor(self):
        db = self

        class _C:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def execute(self, sql, params):
                db.sql.append((sql, params))

            async def fetchall(self):
                return db.runs
        return _C()


def _run(mission, first_pass_id, robot="r1", started=T0):
    return (fleet_recorder.run_uuid(mission, first_pass_id), mission, robot, started)


# --- (A) run id across repeat passes -------------------------------------------------------
def test_derived_run_id_is_the_first_pass_run_only():
    """The recorder keeps one mission_runs row keyed by the first pass's dispatcher run id;
    a repeat pass has another dispatcher id, so run_id_of names no mission_runs row."""
    first, second = _row(pass_id="aaaaaaaa"), _row(pass_id="bbbbbbbb")
    recorded = str(fleet_recorder.run_uuid("m", "aaaaaaaa"))
    assert first["run_id"] == recorded
    assert second["run_id"] != recorded


def test_resolve_run_ids_maps_a_later_pass_to_the_recorded_run():
    run = _run("m", "aaaaaaaa")
    later = _run("m", "cccccccc", started=T0 + datetime.timedelta(hours=1))   # a retry, newer
    rows = [_row(pass_id="aaaaaaaa"), _row(pass_id="bbbbbbbb"),
            _row(pass_id="bbbbbbbb", mission="other", robot="r9")]
    db = _Db([run, later, _run("other", "dddddddd", "r9")])
    asyncio.run(bn.resolve_run_ids(db, rows))
    assert rows[0]["run_id"] == str(run[0])                       # first pass: exact
    assert rows[1]["run_id"] == str(run[0])                       # pass 2: the run open then
    assert rows[2]["run_id"] == str(fleet_recorder.run_uuid("other", "dddddddd"))
    assert len(db.sql) == 1                                       # one query for all rows


def test_resolve_run_ids_keeps_the_derived_id_without_a_run_or_on_error():
    rows = [_row(pass_id="bbbbbbbb")]
    derived = rows[0]["run_id"]
    asyncio.run(bn.resolve_run_ids(_Db([]), rows))
    assert rows[0]["run_id"] == derived
    asyncio.run(bn.resolve_run_ids(_Db(fail=True), rows))
    assert rows[0]["run_id"] == derived
    legacy = [{"mission_name": "m", "vda_node_id": "m-n1-s4", "run_id": None}]
    db = _Db()
    asyncio.run(bn.resolve_run_ids(db, legacy))
    assert legacy[0]["run_id"] is None and not db.sql             # nothing to resolve: no query


def test_resolve_ignores_runs_that_started_after_the_report():
    old = _run("m", "aaaaaaaa", started=T0)
    newer = _run("m", "cccccccc", started=T0 + datetime.timedelta(hours=5))
    rows = [_row(pass_id="bbbbbbbb", created=T0 + datetime.timedelta(minutes=5))]
    asyncio.run(bn.resolve_run_ids(_Db([old, newer]), rows))
    assert rows[0]["run_id"] == str(old[0])


# --- (B) chart keys ------------------------------------------------------------------------
def test_plane_locator_samples_the_owner_plane():
    index = np.array([[0, 1, 1, 2], [0, 1, 2, 2]], dtype="<u2")      # 2 rows x 4 cols, step 1
    locate = tags.plane_locator((10.0, 20.0), 1.0, (2, 4), index, ["A:0", "B:1"])
    assert locate(11.5, 20.5) == "A:0" and locate(13.5, 21.5) == "B:1"
    assert locate(10.5, 20.5) is None and locate(9.0, 20.5) is None and locate(11, 30) is None


class _Reads:
    """owner_plane() like SceneGraphReads: 1 m cells, the left half of the box is A:0, the right B:1.
    `max_side` makes a box wider or taller than it AreaTooLarge (a 422 the tagger splits on)."""

    def __init__(self, available=True, fail=False, max_side=None):
        self.available, self.fail, self.max_side, self.calls = available, fail, max_side, []

    async def owner_plane(self, map_id, bbox, level=0):
        from packages.api.scene_graph_reads import AreaTooLarge
        self.calls.append(bbox)
        if self.fail:
            raise RuntimeError("store down")
        if not self.available:
            return None
        x0, y0 = float(np.floor(bbox.x0)), float(np.floor(bbox.y0))
        cols, rows = int(np.ceil(bbox.x1) - x0), int(np.ceil(bbox.y1) - y0)
        if self.max_side is not None and max(cols, rows) > self.max_side:
            raise AreaTooLarge(status_code=422, detail="too many cells")
        mid = x0 + cols / 2.0
        index = np.where(x0 + np.arange(cols) + 0.5 < mid, 1, 2) * np.ones((rows, 1), dtype=int)
        return {"version": 3, "epoch": "e", "bbox": [x0, y0, x0 + cols, y0 + rows], "origin": (x0, y0),
                "step": 1.0, "shape": (rows, cols), "keys": ["A:0", "B:1"], "index": index.astype("<u2")}


def test_add_chart_keys_tags_rows_and_is_best_effort():
    rows = [{"x": 1.0, "y": 1.0, "graph_node_id": "n"}, {"x": 9.0, "y": 1.0, "graph_node_id": "m"}]
    res = asyncio.run(tags.add_chart_keys(rows, "M", lambda: _Reads()))
    assert res and res.status == "ok" and res.truncated == 0
    assert [r["chart_keys"] for r in rows] == [["A:0"], ["B:1"]]
    for reads in (_Reads(available=False), _Reads(fail=True)):
        plain = [{"x": 1.0, "y": 1.0, "graph_node_id": "n"}]
        res = asyncio.run(tags.add_chart_keys(plain, "M", lambda r=reads: r))
        assert not res and res.status == "unavailable"
        assert "chart_keys" not in plain[0]


def test_add_chart_keys_splits_far_apart_rows_and_reports_what_it_could_not_tag():
    # box 12 wide > max_side 8: two planes; the one row too big for any plane is counted, not dropped silently
    rows = [{"x": 1.0, "y": 1.0, "graph_node_id": "n"}, {"x": 40.0, "y": 1.0, "graph_node_id": "m"}]
    reads = _Reads(max_side=8)
    res = asyncio.run(tags.add_chart_keys(rows, "M", lambda: reads))
    assert res.status == "ok" and res.tagged == 2 and all(r["chart_keys"] for r in rows)
    assert len(reads.calls) == 3                # the refused union, then one plane per half
    big = [{"x": 0.0, "y": 0.0, "graph_node_id": "@0,0", "robot_x": 30.0, "robot_y": 0.0}, rows[0]]
    for r in rows:
        r.pop("chart_keys", None)
    res = asyncio.run(tags.add_chart_keys(big, "M", lambda: _Reads(max_side=8)))
    assert res.status == "partial" and res.truncated == 1 and res.tagged == 1
    assert "chart_keys" not in big[0] and big[1]["chart_keys"]
    assert res.fields() == {"chart_keys_status": "partial", "chart_keys_truncated": True,
                            "chart_keys_truncated_count": 1}


def test_add_chart_keys_plane_budget_is_a_truncation_not_a_silent_drop(monkeypatch):
    monkeypatch.setattr(tags, "MAX_PLANES", 2)
    rows = [{"x": 30.0 * i, "y": 0.0, "graph_node_id": f"n{i}"} for i in range(4)]
    res = asyncio.run(tags.add_chart_keys(rows, "M", lambda: _Reads(max_side=8)))
    assert res.status == "partial" and res.tagged == 2 and res.truncated == 2
    assert sum("chart_keys" in r for r in rows) == 2


def test_add_chart_keys_touches_nothing_on_timeout():
    class Slow(_Reads):
        async def owner_plane(self, *a, **k):
            await asyncio.sleep(1)
    rows = [{"x": 1.0, "y": 1.0, "graph_node_id": "n"}]
    res = asyncio.run(tags.add_chart_keys(rows, "M", lambda: Slow(), timeout_s=0.05))
    assert res.status == "unavailable" and "chart_keys" not in rows[0]


async def _get(db, url):
    api_main.service, previous = SimpleNamespace(database=db), api_main.service
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_main.app),
                                     base_url="http://t") as client:
            return await client.get(url)
    finally:
        api_main.service = previous


class _ListDb(_Db):
    """The blocked table's rows for the list SELECT, mission_runs rows for the run lookup."""

    def __init__(self, blocked, runs=()):
        super().__init__(runs)
        self.blocked = [tuple(r.get(c) for c in bn.COLUMNS) for r in blocked]

    def cursor(self):
        cur, db = super().cursor(), self

        class _C:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def execute(self, sql, params):
                self.sql = sql
                await cur.execute(sql, params)

            async def fetchall(self):
                return db.blocked if self.sql == bn.ACTIVE_FOR_MAP_SQL else db.runs
        return _C()


@pytest.mark.asyncio
async def test_list_default_is_unchanged_and_chart_keys_are_opt_in(monkeypatch):
    row = _row(pass_id="aaaaaaaa", expires_at="2026-10-12T12:00:00+00:00")
    db = _ListDb([row], [_run("m", "aaaaaaaa")])
    monkeypatch.setattr(api_main, "_scene_graph_reads", lambda: _Reads())
    plain = (await _get(db, "/api/v1/maps/M/blocked-nodes")).json()["blocked_nodes"]
    assert "chart_keys" not in plain[0] and plain[0]["run_id"] == str(db.runs[0][0])
    tagged = (await _get(db, "/api/v1/maps/M/blocked-nodes?with_chart_keys=true")).json()
    assert tagged["blocked_nodes"][0]["chart_keys"] == ["B:1"]

    monkeypatch.setattr(api_main, "_scene_graph_reads", lambda: _Reads(fail=True))
    resp = await _get(db, "/api/v1/maps/M/blocked-nodes?with_chart_keys=true")
    assert resp.status_code == 200 and "chart_keys" not in resp.json()["blocked_nodes"][0]
    assert resp.json()["chart_keys_status"] == "unavailable" and resp.json()["chart_keys_truncated"] is False
    assert tagged["chart_keys_status"] == "ok" and "chart_keys_status" not in (
        await _get(db, "/api/v1/maps/M/blocked-nodes")).json()


# --- (C) chart keys of run legs ------------------------------------------------------------
def test_add_leg_chart_keys_tags_legs_by_the_charts_their_line_crosses():
    pos = {("M", "n1"): (1.0, 1.0), ("M", "n2"): (9.0, 1.0), ("M", "n3"): (2.0, 1.0)}
    legs = [{"map_id": "M", "from_topomap_node": "n1", "to_topomap_node": "n2"},
            {"map_id": "M", "from_topomap_node": "n1", "to_topomap_node": "n3"},
            {"map_id": "M", "from_topomap_node": "n1", "to_topomap_node": "gone"},
            {"map_id": "M", "from_topomap_node": None, "to_topomap_node": "n2"}]
    res = asyncio.run(tags.add_leg_chart_keys(legs, lambda m, n: pos.get((m, n)), lambda: _Reads()))
    assert res and res.status == "ok" and res.truncated == 0
    assert legs[0]["chart_keys"] == ["A:0", "B:1"]
    assert legs[1]["chart_keys"] == ["A:0"]
    assert legs[2]["chart_keys"] is None            # a node without a position
    assert "chart_keys" not in legs[3]              # not a node-to-node leg


def test_add_leg_chart_keys_is_best_effort():
    legs = [{"map_id": "M", "from_topomap_node": "a", "to_topomap_node": "b"}]
    xy = lambda m, n: (1.0, 1.0)  # noqa: E731
    for reads in (_Reads(available=False), _Reads(fail=True)):
        assert not asyncio.run(tags.add_leg_chart_keys(legs, xy, lambda r=reads: r))
        assert "chart_keys" not in legs[0]

    def broken(m, n):
        raise RuntimeError("db")
    assert not asyncio.run(tags.add_leg_chart_keys(legs, broken, lambda: _Reads()))
    assert not asyncio.run(tags.add_leg_chart_keys([], xy, lambda: _Reads()))


def test_add_leg_chart_keys_node_cap_is_reported_and_the_rest_is_tagged():
    xy = lambda m, n: (1.0, 1.0)  # noqa: E731
    many = [{"map_id": "M", "from_topomap_node": f"a{i}", "to_topomap_node": f"b{i}"}
            for i in range(tags.MAX_LEG_NODES // 2 + 5)]
    res = asyncio.run(tags.add_leg_chart_keys(many, xy, lambda: _Reads()))
    assert res.status == "partial" and res.truncated == 5 and res.tagged == tags.MAX_LEG_NODES // 2
    assert sum("chart_keys" in leg for leg in many) == tags.MAX_LEG_NODES // 2


def test_add_leg_chart_keys_over_the_read_limit_splits_and_counts():
    pos = {("M", "a"): (1.0, 1.0), ("M", "b"): (3.0, 1.0), ("M", "c"): (60.0, 1.0), ("M", "d"): (0.0, 0.0)}
    legs = [{"map_id": "M", "from_topomap_node": "a", "to_topomap_node": "b"},
            {"map_id": "M", "from_topomap_node": "a", "to_topomap_node": "c"},       # longer than a plane
            {"map_id": "M", "from_topomap_node": "d", "to_topomap_node": "c"}]
    res = asyncio.run(tags.add_leg_chart_keys(legs, lambda m, n: pos.get((m, n)), lambda: _Reads(max_side=8)))
    assert res.status == "partial" and res.truncated == 2 and res.tagged == 1
    assert legs[0]["chart_keys"] and "chart_keys" not in legs[1] and "chart_keys" not in legs[2]


class _LegsDb(_Db):
    def cursor(self):
        db = self

        class _C:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def execute(self, sql, params=None):
                self.sql = sql

            async def fetchone(self):
                return db.runs[0]

            async def fetchall(self):
                from packages.api import fleet_reads
                row = {c: None for c in fleet_reads.LEG_COLUMNS}
                row.update(run_id=str(uuid.uuid4()), seq=1, map_id="M", from_topomap_node="n1",
                           to_topomap_node="n2")
                return [tuple(row[c] for c in fleet_reads.LEG_COLUMNS)]
        return _C()


@pytest.mark.asyncio
async def test_run_legs_endpoint_chart_keys_are_opt_in(monkeypatch):
    from packages.api import fleet_reads
    monkeypatch.setattr(fleet_reads, "_fetch_run", lambda cur, run_id: asyncio.sleep(0))
    monkeypatch.setattr(api_main, "_scene_graph_reads", lambda: _Reads())
    nodes = {"n1": (1.0, 1.0), "n2": (9.0, 1.0)}
    graph_db = SimpleNamespace(get_node=lambda m, n: {"x": nodes[n][0], "y": nodes[n][1]})
    db = _LegsDb([(1,)])
    url = f"/api/v1/runs/{uuid.uuid4()}/legs"

    async def get(suffix):
        api_main.service, previous = SimpleNamespace(database=db, graph_db=graph_db), api_main.service
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_main.app),
                                         base_url="http://t") as client:
                return (await client.get(url + suffix)).json()
        finally:
            api_main.service = previous
    assert "chart_keys" not in (await get(""))["items"][0]
    tagged = await get("?with_chart_keys=true")
    assert tagged["items"][0]["chart_keys"] == ["A:0", "B:1"]
    assert tagged["chart_keys_status"] == "ok" and tagged["chart_keys_truncated"] is False
    assert "chart_keys_status" not in await get("")
