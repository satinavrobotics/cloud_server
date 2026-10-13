"""Scene graph end to end against REAL ArangoDB, MinIO and Postgres (nothing faked).

Skipped unless SCENE_GRAPH_E2E=1. Needs the throw-away stack of docker_compose/scene_graph_e2e.yaml
(tools/scene_graph_e2e/run.sh brings it up, runs this module and tears it down); the SG_E2E_*
variables of tools/scene_graph_e2e/stack.py point elsewhere. The harness refuses the production
ports and uses map ids of its own.

Stages (module fixtures, so a test pulls in what it needs, in order):

  schema      runtime tables + `alembic upgrade head` on Postgres, head revision checked
  ring_map    a synthetic 6 x 6 m ring world: nodes, edges and costmap PNGs written like
              graph-builder stores them (TopomapDatabaseClient: ArangoDB nodes_*, MinIO map-*),
              map registered in `mapobjectv1`, the robot's factsheet in `robotobjectv1`
  backfilled  tools.scene_costmap_stored_backfill: dry run (writes nothing), apply, repeat
  ingested    the real SceneRunner / SceneWorker (scene_host.build_host wiring) until caught up

then the assertions: store meta and snapshot objects, the read API (SceneGraphReads.build),
the planner (MissionPlannerService._try_chart_route with real blocked_graph_nodes rows), restart
idempotence, incremental ingest, the rebuild request, the empty-map rebuild, the advisory lock and
the map-delete saga.

Run it through run.sh. By hand, with the stack up:

    SCENE_GRAPH_E2E=1 PYTHONPATH=. python -m pytest tests/integration/scene_graph -p no:randomly \
        -o addopts="" -v -ra --tb=short
"""
import os

import pytest

if os.environ.get("SCENE_GRAPH_E2E", "").lower() not in ("1", "true", "yes"):
    pytest.skip("scene graph end-to-end harness: set SCENE_GRAPH_E2E=1 (see run.sh)",
                allow_module_level=True)

import asyncio
import json
import logging
import subprocess
import time
import types
from typing import Any, Dict, List, Optional

import numpy as np

from tools.scene_graph_e2e import seed, stack
from tools.scene_graph_e2e import world as W

pytestmark = pytest.mark.integration

log = logging.getLogger("sg_e2e")
RING_LEAVES_MIN = 30
LEGACY_INDEXES = (3, 7, 11, 15, 19)


# ------------------------------------------------------------------------------ environment

class Env:
    """The stack's connections, the product objects built on them, and small helpers."""

    def __init__(self) -> None:
        import psycopg

        from packages.database.postgres import PostgresDatabase
        from packages.topomap_dbs.client import TopomapDatabaseClient

        self.psycopg = psycopg
        self.cfg = stack.load_cfg()
        stack.wait_for_stack(self.cfg)
        self.tag = stack.run_id()
        self.rt = stack.AsyncRuntime()
        self.publisher = stack.RecordingPublisher()
        self.client = TopomapDatabaseClient(
            arango_host=self.cfg.arango_host, arango_port=self.cfg.arango_port,
            arango_username=self.cfg.arango_user, arango_password=self.cfg.arango_password,
            arango_database=self.cfg.arango_db,
            minio_host=self.cfg.minio_host, minio_port=self.cfg.minio_port,
            minio_access_key=self.cfg.minio_user, minio_secret_key=self.cfg.minio_password,
            minio_secure=False)
        self.db = PostgresDatabase(dbname=self.cfg.pg_db, user=self.cfg.pg_user,
                                   password=self.cfg.pg_password, host=self.cfg.pg_host,
                                   port=self.cfg.pg_port)
        self.maps: List[str] = []
        self.host = None
        self.migrated_with = ""

    # -- Postgres (sync helper connections; the product's own are async, on `rt`)
    def sql(self, query: str, params: tuple = ()) -> List[tuple]:
        with self.psycopg.connect(self.cfg.conninfo, autocommit=True) as conn:
            cur = conn.execute(query, params)
            return cur.fetchall() if cur.description else []

    # -- naming
    def map_name(self, kind: str, hyphen: bool = False) -> str:
        name = f"e2e{'-' if hyphen else '_'}{kind}{'-' if hyphen else '_'}{self.tag}"
        self.maps.append(name)
        return name

    # -- registry
    def register_map(self, map_id: str) -> None:
        from cloud_common.objects.map import MapSpecV1, MapStatusV1
        self.sql("INSERT INTO mapobjectv1 (name, lifecycle, spec, status) "
                 "VALUES (%s, 'ALIVE', %s::jsonb, %s::jsonb) ON CONFLICT (name) DO NOTHING",
                 (map_id, MapSpecV1().json(), MapStatusV1().json()))

    def map_status(self, map_id: str) -> Dict[str, Any]:
        rows = self.sql("SELECT status FROM mapobjectv1 WHERE name = %s", (map_id,))
        return rows[0][0] if rows else {}

    def patch_status(self, map_id: str, patch: Dict[str, Any]) -> None:
        self.sql("UPDATE mapobjectv1 SET status = status || %s::jsonb WHERE name = %s",
                 (json.dumps(patch), map_id))

    # -- stores
    def store(self):
        from packages.scene_graph.store import SceneGraphStore
        return SceneGraphStore.from_client(self.client)

    def meta(self, map_id: str) -> Dict[str, Any]:
        return self.store().meta(map_id)

    def aql(self, query: str, **bind) -> List[Any]:
        return list(self.client.graph.db.aql.execute(query, bind_vars=bind))

    def sg_collections(self, map_id: str) -> List[str]:
        db = self.client.graph.db
        return [n for n in (f"sg_anchors_{map_id}", f"sg_links_{map_id}", f"sg_meta_{map_id}")
                if db.has_collection(n)]

    def bucket(self, map_id: str) -> str:
        return self.client.image._bucket_name(map_id)

    def scene_objects(self, map_id: str) -> List[str]:
        try:
            return sorted(o.object_name for o in self.client.image.client.list_objects(
                self.bucket(map_id), prefix="scene_graph/", recursive=True))
        except Exception:  # noqa: BLE001  no bucket
            return []

    # -- the worker host
    def build_host(self, fast: bool = True):
        """`scene_host.build_host` over the stack, as graph-builder wires it, with the timers
        shortened so a stage takes seconds, not minutes (the logic is unchanged)."""
        from packages.services.graph_builder import scene_host
        service = types.SimpleNamespace(
            topomap_db=self.client, graph_db=self.client.graph, image_db=self.client.image,
            update_publisher=self.publisher, database=self.db)
        host = scene_host.build_host(service, self.rt.loop, self.cfg.conninfo)
        if fast:
            runner = host.runner
            runner.config.discover_s = 0.3
            runner.config.idle_s = 0.1
            runner.config.backoff_s = 0.5
            runner.config.terminal_retry_s = 0.5
            runner.config.rebuild_retry_s = 0.5
            make = runner.make_worker

            def tuned(map_id: str):
                worker = make(map_id)
                c = worker.config
                c.min_save_interval_s = 0.3
                c.idle_sleep_s, c.idle_max_s = 0.1, 0.3
                c.lifecycle_check_s = 0.0
                c.rebuild_check_s = 0.2
                c.save_every_s = 2.0
                c.png_retry_s = 0.3
                return worker
            runner.make_worker = tuned
        return host

    def ensure_host(self):
        if self.host is None:
            self.host = self.build_host()
        return self.host

    def pump(self, until, what: str, timeout: float = 150.0) -> None:
        """Drive the runner (single-threaded, deterministic) until `until()`."""
        runner = self.host.runner
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            stepped = runner.tick()
            if until():
                return
            if stepped == 0:
                time.sleep(0.05)
        states = {m: (runner.last_state.get(m), getattr(w, "stats", None) and w.stats)
                  for m, w in runner.workers.items()}
        raise TimeoutError(f"{what}: not reached within {timeout:.0f}s; workers: {states}")

    def caught_up(self, map_id: str, folded: int = 1):
        """Predicate: the map's worker folded (or, after a restart, skipped as already folded) >= `folded` leaves and sits idle with nothing unsaved."""
        def check() -> bool:
            worker = self.host.runner.workers.get(map_id)
            if worker is None:
                return False
            st = worker.stats
            return (self.host.runner.last_state.get(map_id) == "idle" and st.folded + st.already_folded >= folded
                    and st.unsaved_leaves == 0 and worker.state == "" and st.version >= 1
                    and worker.settled())     # nothing buffered / pending / dirty, cursor at head
        return check

    def cleanup(self) -> None:
        if os.environ.get("SG_E2E_KEEP") == "1":
            return
        try:
            if self.host is not None:
                self.host.runner.close()
                self.host.guard.close()
        except Exception:  # noqa: BLE001
            pass
        for m in self.maps:
            try:
                self.store().drop(m)
                self.client.graph.delete_map(m)
                self.client.image.delete_map(m)
                self.sql("DELETE FROM mapobjectv1 WHERE name = %s", (m,))
                self.sql("DELETE FROM blocked_graph_nodes WHERE map_name = %s", (m,))
            except Exception:  # noqa: BLE001
                log.exception("cleanup of %s failed", m)
        try:
            self.sql("DELETE FROM robotobjectv1 WHERE name = 'e2e-robot'")
        except Exception:  # noqa: BLE001
            pass
        try:
            self.rt.run(self.db.close_pool(), 10)
        except Exception:  # noqa: BLE001
            pass
        self.rt.close()


@pytest.fixture(scope="module")
def env():
    e = Env()
    try:
        yield e
    finally:
        e.cleanup()


@pytest.fixture(scope="module")
def schema(env):
    """The schema as production gets it: services create the *objectv1 tables at startup
    (PostgresDatabase.async_init), the API entrypoint then runs `alembic upgrade head`."""
    env.rt.run(env.db.async_init(), 120)
    if stack.long_revision_ids():
        stack.widen_version_table(env.cfg)      # PRODUCT DEFECT, see test_migration_revision_ids_fit
    env.migrated_with = stack.migrate(env.cfg)
    log.info("migrations: %s", env.migrated_with)
    rows = env.sql("SELECT version_num FROM alembic_version")
    assert rows == [(stack.head_revision(),)], f"alembic_version {rows} != head {stack.head_revision()}"
    for table in ("mapobjectv1", "robotobjectv1", "blocked_graph_nodes", "fleet_events"):
        assert env.sql("SELECT to_regclass(%s)", (table,))[0][0] is not None, f"{table} missing"
    cols = {r[0] for r in env.sql("SELECT column_name FROM information_schema.columns "
                                  "WHERE table_name = 'blocked_graph_nodes'")}
    assert {"robot_x", "robot_y"} <= cols            # migration 20261012_01
    return env.migrated_with


@pytest.fixture(scope="module")
def ring_map(env, schema):
    world, way = W.ring()
    map_id = env.map_name("ring")
    # the robot's factsheet (0.8 x 0.5): r_d must come out known, 0.25
    env.sql("INSERT INTO robotobjectv1 (name, lifecycle, spec, status) VALUES "
            "('e2e-robot', 'ALIVE', '{}'::jsonb, %s::jsonb) ON CONFLICT (name) DO UPDATE "
            "SET status = EXCLUDED.status",
            (json.dumps({"factsheet": {"length": 0.8, "width": 0.5}}),))
    ids = seed.seed_map(env.client, map_id, world, way, session_id=f"sess-{env.tag}")
    assert len(ids) >= RING_LEAVES_MIN
    legacy = [ids[i] for i in LEGACY_INDEXES]
    assert seed.unstamp(env.client, map_id, legacy) == len(legacy)
    env.register_map(map_id)
    return types.SimpleNamespace(map_id=map_id, ids=ids, legacy=legacy, world=world, way=way)


@pytest.fixture(scope="module")
def backfilled(env, ring_map):
    from tools import scene_costmap_stored_backfill as bf
    store = bf.ArangoStore(env.client.graph.db)
    minio = bf.MinioTimes(env.client.image)
    n, mid = len(ring_map.ids), ring_map.map_id

    def unstamped() -> int:
        return env.aql("FOR n IN @@c FILTER !IS_NUMBER(n.costmap_stored_ms) OR n.costmap_stored_ms <= 0 "
                       "RETURN 1", **{"@c": f"nodes_{mid}"}).__len__()

    assert unstamped() == len(ring_map.legacy)
    dry = bf.backfill_map(store, mid, minio, apply=False)
    assert unstamped() == len(ring_map.legacy), "a dry run wrote something"
    # The apply uses the node's receipt time (--no-minio): MinIO's last-modified has whole-second
    # resolution, which would give the five legacy leaves stamps that sort ahead of their (ms
    # resolution) neighbours and scramble the drive order, and the chart partition depends on the
    # order of capture stamps (see test_fold_result_holds_its_invariants_for_any_stamp_order). The MinIO
    # source is applied on a map of its own in test_backfill_minio_source_stamps.
    applied = bf.backfill_map(store, mid, None, apply=True)
    assert unstamped() == 0
    again = bf.backfill_map(store, mid, minio, apply=True)
    return types.SimpleNamespace(dry=dry, applied=applied, again=again, n=n)


@pytest.fixture(scope="module")
def ingested(env, ring_map, backfilled):
    host = env.ensure_host()
    publisher_start = len(env.publisher.messages)
    t0 = time.monotonic()
    env.pump(env.caught_up(ring_map.map_id, len(ring_map.ids)), "ring map caught up")
    worker = host.runner.workers[ring_map.map_id]
    return types.SimpleNamespace(seconds=time.monotonic() - t0, worker=worker,
                                 publisher_start=publisher_start,
                                 meta=env.meta(ring_map.map_id))


# ------------------------------------------------------------------------------ stage: schema

def test_migration_revision_ids_fit_the_version_table():
    """`alembic_version.version_num` is varchar(32) (alembic's default; alembic.ini states the
    32-character rule): a longer revision id makes `alembic upgrade head` fail with
    StringDataRightTruncation, so the API entrypoint never starts uvicorn."""
    assert stack.long_revision_ids() == []


def test_schema_migrated_to_head(env, schema):
    assert schema
    # idempotent: a second upgrade is a no-op at head
    stack.migrate(env.cfg)
    assert env.sql("SELECT version_num FROM alembic_version") == [(stack.head_revision(),)]


# ------------------------------------------------------------------------------ stage: backfill

def test_backfill_dry_run_apply_repeat(backfilled, ring_map):
    n, k = backfilled.n, len(ring_map.legacy)
    d, a, r = backfilled.dry, backfilled.applied, backfilled.again
    assert (d["nodes"], d["already"], d["to_stamp"], d["stamped"], d["skipped"], d["applied"]) == \
        (n, n - k, k, 0, 0, False)
    assert d["sources"] == {"node_timestamp": k}      # ms resolution: primary; MinIO is not asked
    assert (a["to_stamp"], a["stamped"], a["skipped"], a["applied"]) == (k, k, 0, True)
    assert a["sources"] == {"node_timestamp": k}
    assert (r["already"], r["to_stamp"], r["stamped"]) == (n, 0, 0), "a second run must change nothing"


def test_backfill_minio_source_stamps(env, schema):
    """The MinIO last-modified fallback, applied to nodes WITHOUT a node timestamp: the stamps are
    the PNGs' store times (whole seconds)."""
    from tools import scene_costmap_stored_backfill as bf
    world, way = W.two_rooms()
    mid = env.map_name("bfmin")                      # not registered: no worker touches it
    ids = seed.seed_map(env.client, mid, world, way[:2], session_id="sess-bf")
    assert seed.unstamp(env.client, mid, ids) == len(ids)
    env.aql("FOR n IN @@c UPDATE n WITH {timestamp: null, created_at: null} IN @@c "
            "OPTIONS {keepNull: false}", **{"@c": f"nodes_{mid}"})
    now_ms = int(time.time() * 1000)
    rep = bf.backfill_map(bf.ArangoStore(env.client.graph.db), mid, bf.MinioTimes(env.client.image),
                          apply=True, now_ms=now_ms)
    assert (rep["nodes"], rep["to_stamp"], rep["stamped"], rep["skipped"]) == (len(ids),) * 3 + (0,)
    assert rep["sources"] == {"minio_last_modified": len(ids)}
    got = env.aql("FOR n IN @@c RETURN n.costmap_stored_ms", **{"@c": f"nodes_{mid}"})
    assert all(0 < ms <= now_ms and now_ms - ms < 120_000 for ms in got), got


def test_backfill_cli_output(env, ring_map, backfilled, capsys):
    from tools import scene_costmap_stored_backfill as bf
    rc = bf.main(["--map", ring_map.map_id], store=bf.ArangoStore(env.client.graph.db),
                 minio=bf.MinioTimes(env.client.image))
    out = capsys.readouterr().out
    assert rc == 0 and "DRY RUN" in out and ring_map.map_id in out
    assert f"already={len(ring_map.ids)}" in out and "to_stamp=0" in out


# ------------------------------------------------------------------------------ stage: ingest

def test_ingest_caught_up(env, ring_map, ingested):
    w, st = ingested.worker, ingested.worker.stats
    n = len(ring_map.ids)
    assert st.folded == n and not dict(st.skipped), (st, dict(st.skipped))
    assert st.pending == 0 and st.fold_failures == 0 and st.source_errors == 0
    assert st.default_r_d == 0, "the factsheet of e2e-robot was not read from Postgres"
    assert st.saves >= 1 and st.unsaved_leaves == 0
    log.info("ingest of %d leaves: %.1fs, %d batches, %d saves, last fold %.2fs",
             n, ingested.seconds, st.batches, st.saves, st.last_fold_s)


def test_store_meta_and_cursor(env, ring_map, ingested):
    from packages.services.graph_builder import scene_host
    from packages.scene_graph.api import SceneParams
    meta = env.meta(ring_map.map_id)
    assert meta["version"] >= 1 and meta["generation"] == 1
    params = SceneParams()
    assert meta["params"] == scene_host.params_dict(params)
    assert meta["params_hash"] == scene_host.expected_params_hash(params)
    # the persisted cursor is the newest (costmap_stored_ms, key) of the map's nodes
    rows = env.aql("FOR n IN @@c FILTER n.costmap_stored_ms != null "
                   "RETURN [n.costmap_stored_ms, n._key]", **{"@c": f"nodes_{ring_map.map_id}"})
    newest = max((int(ms), str(k)) for ms, k in rows)
    assert len(rows) == len(ring_map.ids)
    assert (int(meta["cursor"][0]), str(meta["cursor"][1])) == newest
    # every leaf was given its stamp on the node (first write wins), none of them below `min_obs`
    stamped = env.aql("FOR n IN @@c FILTER n.scene_stamp_ms != null RETURN 1",
                      **{"@c": f"nodes_{ring_map.map_id}"})
    assert 0 < len(stamped) <= len(ring_map.ids)


def test_snapshot_objects_and_index(env, ring_map, ingested):
    mid = ring_map.map_id
    meta = env.meta(mid)
    objs = env.scene_objects(mid)
    names = {o[len("scene_graph/"):] for o in objs}
    manifest = f"g1/manifest-{meta['version']}.json"
    assert manifest in names, sorted(names)[:10]
    man = json.loads(env.client.image.client.get_object(env.bucket(mid), "scene_graph/" + manifest).read())
    assert man["leaf_count"] == len(ring_map.ids) and man["version"] == meta["version"]
    assert man["chart_count"] >= 2 and man["tile_count"] >= 1
    assert any(n.startswith("g1/t/") for n in names) and any(n.startswith("g1/p/") for n in names)
    # costmap PNGs of the leaves sit next to the snapshot, untouched
    assert env.client.image.get_costmap(ring_map.ids[0], "occupancy", mid)
    # the ArangoDB index of the current version
    mdoc = env.client.graph.db.collection(f"sg_meta_{mid}").get("meta")
    assert mdoc["version"] == meta["version"] and mdoc["index_version"] == meta["version"]
    anchors = env.aql("FOR a IN @@c RETURN a", **{"@c": f"sg_anchors_{mid}"})
    links = env.aql("FOR l IN @@c RETURN l", **{"@c": f"sg_links_{mid}"})
    assert len(anchors) == man["chart_count"] and links
    assert {a["version"] for a in anchors} == {meta["version"]}
    keys = {a["key"] for a in anchors}
    assert all(l["a"] in keys and l["b"] in keys for l in links)


def test_snapshot_round_trip_matches_the_stored_view(env, ring_map, ingested):
    """store.load (engine from tiles + one re-solve) and store.view (manifest + owner tiles only)
    describe the same charts and links as what the worker folded and saved."""
    from packages.scene_graph.api import BBox
    mid = ring_map.map_id
    store = env.store()
    meta = store.meta(mid)
    big = BBox(-1.0, -1.0, 7.0, 7.0)
    engine = store.load(mid, meta["version"])
    a = engine.query(big, level=0, raster=False)
    b = store.view(mid, meta["version"]).query(big, level=0, raster=True)
    assert {c.key for c in a.charts} == {c.key for c in b.charts}
    ca, cb = {c.key: c for c in a.charts}, {c.key: c for c in b.charts}
    for key in ca:
        assert ca[key].cells == cb[key].cells
        assert ca[key].anchor == pytest.approx(cb[key].anchor)
    assert {(l.a, l.b) for l in a.links} == {(l.a, l.b) for l in b.links}
    # the worker's engine in memory agrees too
    live = ingested.worker.engine.query(big, level=0, raster=False)
    assert {c.key for c in live.charts} == set(cb)
    # sanity of the geometry itself: anchors on free floor, none in the block or a wall
    for c in b.charts:
        ix, iy = int(c.anchor[0] / W.RES), int(c.anchor[1] / W.RES)
        assert ring_map.world[iy, ix] == 0, f"anchor {c.key} {c.anchor} is not on free floor"
    # leaf records keep the footprint they were driven with
    st = engine.export_state()
    assert len(st.leaves) == len(ring_map.ids)
    assert {round(l.r_d, 3) for l in st.leaves} == {0.25}
    assert {l.key for l in st.leaves} == set(ring_map.ids)


def test_events_and_websocket_message(env, ring_map, ingested):
    mid = ring_map.map_id
    meta = env.meta(mid)
    msgs = [m for m in env.publisher.messages if m["_map"] == mid]
    assert msgs, "no scene_graph_updated message was published"
    assert {m["type"] for m in msgs} == {"scene_graph_updated"}
    assert msgs[-1]["version"] == meta["version"]

    def events():
        return dict(env.sql(
            "SELECT code, count(*) FROM fleet_events WHERE payload->>'map_name' = %s GROUP BY code",
            (mid,)))
    ev = stack.wait_until(lambda: events().get("MAP.SCENE_GRAPH_UPDATED") and events(),
                          "MAP.SCENE_GRAPH_UPDATED in fleet_events", 15)
    assert ev["MAP.SCENE_GRAPH_UPDATED"] >= 1
    assert "MAP.SCENE_GRAPH_FOLD_FAILED" not in ev, ev


# ------------------------------------------------------------------------------ read API

def _reads(env):
    from packages.api import scene_graph_reads
    return scene_graph_reads.build(True, env.client)


def test_reads_disabled_answers_unavailable(env, ring_map, ingested):
    from packages.api import scene_graph_reads
    from packages.scene_graph.api import BBox
    off = scene_graph_reads.build(False)
    out = asyncio.run(off.view(ring_map.map_id, BBox(0, 0, 6, 6)))
    assert out["available"] is False and out["charts"] == []


def test_reads_view_and_raster(env, ring_map, ingested):
    from fastapi import HTTPException
    from packages.scene_graph.api import BBox
    mid = ring_map.map_id
    reads = _reads(env)
    meta = env.meta(mid)
    box = BBox(0.0, 0.0, 6.0, 6.0)

    view = asyncio.run(reads.view(mid, box, level=0, raster=True))
    assert view["available"] and view["version"] == meta["version"] and view["level"] == 0
    assert len(view["charts"]) >= 2 and view["links"]
    # the bbox is snapped outward to the 6.4 m tile grid
    assert view["bbox"] == [0.0, 0.0, 6.4, 6.4]
    keys = [c["key"] for c in view["charts"]]
    assert set(view["raster"]["keys"]) <= set(keys)
    rows, cols = view["raster"]["shape"]
    assert rows > 0 and cols > 0 and view["raster"]["step"] == pytest.approx(0.1)
    for c in view["charts"]:
        assert 0.0 <= c["driven_frac"] <= 1.0 and 0.0 <= c["ever_blocked_frac"] <= 1.0
        assert c["cells"] > 0 and c["area_m2"] > 0
    assert any(c["driven_frac"] > 0 for c in view["charts"]), "no chart reports being driven"
    # at least one chart names a driven leaf, and it is one of the seeded nodes
    leaves = {c["leaf"] for c in view["charts"] if c["leaf"]}
    assert leaves and leaves <= set(ring_map.ids)

    body, headers = asyncio.run(reads.raster(mid, box, level=0, version=meta["version"], plane="owner"))
    assert headers["X-Raster-Dtype"] == "u16le" and headers["X-Scene-Graph-Version"] == str(meta["version"])
    r, c = (int(v) for v in headers["X-Raster-Shape"].split(","))
    assert [r, c] == [rows, cols] and len(body) == r * c * 2
    plane = np.frombuffer(body, dtype="<u2").reshape(r, c)
    assert plane.max() <= len(view["raster"]["keys"]) and plane.max() > 0
    ox, oy = (float(v) for v in headers["X-Raster-Origin"].split(","))
    step = float(headers["X-Raster-Step"])

    def owner_at(x: float, y: float) -> Optional[str]:
        i = plane[int((y - oy) / step), int((x - ox) / step)]
        return None if i == 0 else view["raster"]["keys"][i - 1]
    # geometry: start corner is chart space; wall, block and outside are not
    assert owner_at(1.0, 1.0) is not None
    assert owner_at(5.0, 5.0) is not None
    assert owner_at(3.0, 3.0) is None, "the central block is owned by a chart"
    assert owner_at(0.1, 0.1) is None, "the wall is owned by a chart"
    # the raster agrees with the store's own point query on the same version
    located = env.store().view(mid, meta["version"]).locate(1.0, 1.0)
    assert owner_at(1.0, 1.0) == located
    # the free fraction of the raster is the loop's floor (24.6 m2 minus what the robot cannot stand on)
    area = float((plane > 0).sum()) * step * step
    assert 8.0 < area < 26.0, area

    dep, dh = asyncio.run(reads.raster(mid, box, level=0, version=meta["version"], plane="dependent"))
    assert dh["X-Raster-Dtype"] == "u8" and len(dep) == r * c

    # conditional request: the ETag answers 304 (no body) before anything is computed
    etag = headers["ETag"]
    none_body, h304 = asyncio.run(reads.raster(mid, box, level=0, version=meta["version"],
                                               plane="owner", if_none_match=etag))
    assert none_body is None and h304["ETag"] == etag

    # a hierarchy level aggregates charts: no more charts than level 0
    coarse = asyncio.run(reads.view(mid, box, level=1, raster=False))
    assert coarse["available"] and 1 <= len(coarse["charts"]) <= len(view["charts"])

    # a version that does not exist is a 404, an unknown map is `available: false`
    with pytest.raises(HTTPException) as ei:
        asyncio.run(reads.view(mid, box, version=meta["version"] + 50))
    assert ei.value.status_code == 404
    nope = asyncio.run(reads.view("e2e_no_such_map", box))
    assert nope["available"] is False


def test_reads_lineage(env, ring_map, ingested):
    mid = ring_map.map_id
    reads = _reads(env)
    out = asyncio.run(reads.lineage(mid, 0))
    assert out["available"] and out["from_version"] == 0 and out["version"] == env.meta(mid)["version"]
    assert isinstance(out["retired"], dict)
    # every retired key maps to successors that live now (or [] = vanished)
    from packages.scene_graph.api import BBox
    live = {c["key"] for c in asyncio.run(reads.view(mid, BBox(0, 0, 6, 6)))["charts"]}
    for succ in out["retired"].values():
        assert all(k in live for k in succ)


# ------------------------------------------------------------------------------ engine property

def _drive_leaves(world, way, tag: str, stamps=None):
    from packages.scene_graph.api import Leaf
    out = []
    for i, (x, y, yaw) in enumerate(W.drive(way, 0.5)):
        win, origin = W.window_at(world, x, y)
        out.append(Leaf(key=seed.node_uuid(tag, i), session="s",
                        stamp_ms=1_000_000 + 1000 * (i if stamps is None else stamps[i]),
                        x=x, y=y, yaw=yaw, resolution=W.RES, origin=origin, window_u8=win, r_d=0.25))
    return out


def test_fold_result_holds_its_invariants_for_any_stamp_order():
    """What the capture stamps decide is the DRIVE ORDER, and with it the driven polyline (the
    evidence of cells the robot was proven to fit): shuffling the stamps of a static world draws
    random chords between leaves, which is not a drive. What must hold for EVERY order, with the
    same windows and poses: the engine's invariants, every leaf pose inside a chart, a route
    between the two far corners that exists and whose narrowest passage is not narrower than the
    robot (2 r + 0.05 m), and (charts, links) counts within a small band.

    The world is the constant-width ring corridor (one chart under the persistence partition):
    on a multi-room world the random chords add witness slivers along the corridor walls, which
    the shortest route may follow (its `min_width` then reads the sliver, 0.1 - 0.3 m); that is
    the next test's subject, with the stamps kept."""
    import random
    from packages.scene_graph.api import BBox, Leaf, SceneParams
    from packages.scene_graph.engine import ChartGraph
    world, way = W.corridor_ring()
    base = _drive_leaves(world, way, "order")
    params = SceneParams()
    need = 2 * params.robot_half_side + 0.05
    shapes = set()
    for trial in range(8):
        order = list(range(len(base)))
        random.Random(trial).shuffle(order)
        leaves = [Leaf(**{**l.__dict__, "stamp_ms": 1_000_000 + 1000 * p}) for l, p in zip(base, order)]
        g = ChartGraph(params)
        g.fold_many(leaves)
        assert g.check_invariants() == [], f"trial {trial}: {g.check_invariants()}"
        a, b = g.snap(1.0, 1.0, 0.5), g.snap(5.0, 5.0, 1.5)
        assert a.key is not None and b.key is not None, f"trial {trial}: start / goal not snapped"
        route = g.route(a.key, b.key)
        assert route is not None and route.nodes, f"trial {trial}: no route"
        assert route.min_width >= need, f"trial {trial}: min_width {route.min_width:.2f} < {need:.2f}"
        view = g.query(BBox(-1, -1, 7, 7), raster=False)
        assert len(view.charts) >= 1
        uncovered = [l.key for l in leaves if g.locate(l.x, l.y, 0.5) is None]
        assert not uncovered, f"trial {trial}: leaves outside every chart: {uncovered[:3]}"
        shapes.add((len(view.charts), len(view.links)))
    counts = {c for c, _ in shapes}
    assert max(counts) - min(counts) <= 2, f"chart count varies too much with the order: {sorted(shapes)}"


def test_partition_of_the_room_loop_does_not_depend_on_the_arrival_order():
    """The persistence partition is a function of the folded evidence: the same leaves (stamps
    kept) handed over in any order, in one batch or several, give the same charts and links, and
    the room loop is four persistence regions joined by four corridor links, a route that goes
    through three rooms. (Charts are bounded at 0.4 x sight_range, so a room may hold several:
    the structure is asserted on regions, not on the chart count.)"""
    import random
    from packages.scene_graph.api import BBox, SceneParams
    from packages.scene_graph.engine import ChartGraph
    world, way = W.ring()
    leaves = _drive_leaves(world, way, "arrival")
    need = 2 * SceneParams().robot_half_side + 0.05

    def describe(g):
        v = g.query(BBox(-1, -1, 7, 7), raster=False)
        at = {c.key: (round(c.anchor[0], 2), round(c.anchor[1], 2)) for c in v.charts}
        charts = sorted((at[c.key], c.cells) for c in v.charts)
        links = sorted((min(at[l.a], at[l.b]), max(at[l.a], at[l.b]), round(l.width, 2)) for l in v.links)
        return charts, links, v

    def structure(v):
        region = {c.key: c.region for c in v.charts}
        between = [l for l in v.links if region[l.a] != region[l.b]]
        return region, len(set(region.values())), len(between)
    ref_charts = ref_links = None
    for trial in range(6):
        order = list(range(len(leaves)))
        if trial:
            random.Random(trial).shuffle(order)
        g = ChartGraph(SceneParams())
        arrival = [leaves[i] for i in order]
        if trial % 2:
            g.fold_many(arrival)
        else:                                   # several batches
            for k in range(0, len(arrival), 7):
                g.fold_many(arrival[k:k + 7])
        assert g.check_invariants() == [], f"trial {trial}: {g.check_invariants()}"
        charts, links, view = describe(g)
        if ref_charts is None:
            ref_charts, ref_links = charts, links
            _, n_regions, n_between = structure(view)
            assert n_regions == 4 and n_between == 4, (n_regions, n_between, charts, links)
        assert (charts, links) == (ref_charts, ref_links), f"trial {trial} differs from trial 0"
        assert all(l.width >= W.CORRIDOR - 0.1 for l in view.links)
        a, b = g.snap(1.0, 1.0, 0.5), g.snap(5.0, 5.0, 1.5)
        route = g.route(a.key, b.key)
        assert route is not None and route.min_width >= need
        region, _, _ = structure(view)
        rooms = [region[k] for k in route.charts]
        rooms = [r for i, r in enumerate(rooms) if i == 0 or r != rooms[i - 1]]
        assert len(rooms) == 3, rooms                    # corner room, a neighbour room, corner room


# ------------------------------------------------------------------------------ planner

class Planner:
    """MissionPlannerService with its real graph_db / Postgres / GraphCache; only the robot
    lookups (a robots row + an open map session in production) are replaced by one robot."""

    def __init__(self, env: Env, map_id: str) -> None:
        from packages.scene_graph.store import SceneGraphStore
        from packages.services.mission_planner import scene_route
        from packages.services.mission_planner.server import MissionPlannerService
        self.env, self.map_id = env, map_id
        svc = object.__new__(MissionPlannerService)
        svc.logger = logging.getLogger("e2e.planner")
        svc.graph_db = env.client.graph
        svc.database = env.db
        svc._scene_cache = scene_route.GraphCache(
            lambda: SceneGraphStore.from_client(env.client), max_entries=4, load_timeout_s=60.0,
            lazy=os.environ.get("SCENE_GRAPH_PLANNER_MODE", "full_load") == "lazy")
        # state MissionPlannerService.__init__ sets up (bounded plan pool, outcome counters)
        svc._plan_pool = scene_route.BoundedPool("sg-plan", 2, 8, busy_reason="planner_busy")
        svc._chart_outcomes = {}
        robot = types.SimpleNamespace(
            name="e2e-robot", status=types.SimpleNamespace(
                pose=types.SimpleNamespace(x=1.0, y=1.0),
                factsheet=types.SimpleNamespace(length=0.8, width=0.5)))

        async def get_robot_status(name: str):
            return robot

        async def robot_xy(_robot, _map_id, pose_xy=None):
            return tuple(pose_xy)
        svc.get_robot_status, svc._robot_xy_in_map = get_robot_status, robot_xy
        self.svc = svc
        from packages import config
        self.config = config

    def route(self, start, goal, ignore_exclusions: bool = False):
        self.config.SCENE_GRAPH_PLAN_BUDGET_S = 30.0       # correctness here, latency in its own test
        t0 = time.monotonic()
        res = self.env.rt.run(self.svc._try_chart_route(
            "e2e-robot", self.map_id, goal[0], goal[1], start, ignore_exclusions), 90)
        return res, time.monotonic() - t0

    def block(self, kind: str, *, secs: float = 600.0) -> None:
        """Rows of `blocked_graph_nodes` exactly as the dispatcher writes them (UPSERT_SQL): an
        obstacle segment across one corridor of the ring, from a robot pose outside the room
        (the segment starts LEAD_M ahead of the pose, so it spans the whole corridor width)."""
        from packages.utils import blocked_nodes as bn
        if kind == "bottom":        # x = 3, y 0.2 .. 1.7 (the corridor below the block)
            wp, robot = (3.0, 1.7), (3.0, -0.8)
        elif kind == "left":        # y = 3, x 0.2 .. 1.7
            wp, robot = (1.7, 3.0), (-0.8, 3.0)
        else:
            raise ValueError(kind)
        self.env.sql(bn.UPSERT_SQL, bn.upsert_params(
            self.map_id, bn.synthetic_id(*wp), "edgeBlocked", secs, robot_name="e2e-robot",
            mission_name="e2e", reason="e2e", x=wp[0], y=wp[1], robot_x=robot[0], robot_y=robot[1]))

    def clear(self) -> None:
        self.env.sql("DELETE FROM blocked_graph_nodes WHERE map_name = %s", (self.map_id,))


@pytest.fixture(scope="module")
def planner(env, ring_map, ingested):
    p = Planner(env, ring_map.map_id)
    p.clear()
    return p


def _side(waypoints) -> str:
    """Which way round the ring a route goes: it passes the (5,1) corner or the (1,5) one."""
    pts = [(w.x, w.y) for w in waypoints]
    dense = []
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        n = max(1, int(np.hypot(x1 - x0, y1 - y0) / 0.1))
        dense += [(x0 + (x1 - x0) * k / n, y0 + (y1 - y0) * k / n) for k in range(n + 1)]
    near_bottom = min(np.hypot(x - 5.0, y - 1.0) for x, y in dense)
    near_left = min(np.hypot(x - 1.0, y - 5.0) for x, y in dense)
    return "bottom" if near_bottom < near_left else "left"


def test_planner_route_between_leaves_has_distinct_leaf_ids(env, ring_map, planner, ingested):
    mid = ring_map.map_id
    start, goal = (1.0, 1.0), (5.0, 5.0)
    (res, reason), secs = planner.route(start, goal)
    if reason:
        g, v = env.rt.run(planner.svc._scene_cache.get(mid), 60)
        a, b = g.snap(1.0, 1.0, 0.5), g.snap(5.0, 5.0, 1.5)
        r = g.route(a.key, b.key)
        print("DBG version", v, "meta", env.meta(mid), "snaps", a, b, "min_width", r.min_width if r else None, "charts", r.charts if r else None, "worker", ingested.worker.stats)
    assert reason is None, f"fell back to the legacy planner: {reason}"
    assert res["planner"] == "scene_graph"
    ids = res["path"]
    # Contract: the route is served; consecutive waypoints on one leaf are merged where the line of
    # sight (free floor of the world, the gap cap) allows, so a repeat that is still there must be
    # one the planner's own merge step cannot remove. A route between two corner rooms spans more
    # than one leaf.
    from packages.services.mission_planner import scene_route
    assert len(ids) >= 2 and len(set(ids)) >= 2

    def free_los(a, b) -> bool:
        n = max(1, int(np.hypot(b[0] - a[0], b[1] - a[1]) / (W.RES / 2)))
        return all(ring_map.world[int((a[1] + (b[1] - a[1]) * k / n) / W.RES),
                                  int((a[0] + (b[0] - a[0]) * k / n) / W.RES)] == 0
                   for k in range(n + 1))
    pts = [(w.x, w.y) for w in res["waypoints"]]
    _pts, merged = scene_route.collapse_repeated_leaf_ids(
        pts, ids, free_los, planner.config.SCENE_GRAPH_MAX_WAYPOINT_GAP_M)
    assert merged == ids, f"a mergeable repeat was served: {ids} -> {merged}"
    assert res["start_node_id"] == ids[0] and res["end_node_id"] == ids[-1]
    assert res["start_node_id"] != res["end_node_id"]
    assert set(ids) <= set(ring_map.ids), "a waypoint names a node that is not in the map"
    wps = res["waypoints"]
    assert len(wps) == len(ids) and [w.node_id for w in wps] == ids
    assert all(w.map_id == mid for w in wps)
    assert res["graph_version"] == env.meta(mid)["version"]
    assert res["min_width"] >= 0.5, res["min_width"]          # at least the robot's own width
    # the route really goes round the loop (more than the straight diagonal through the block)
    length = sum(np.hypot(b.x - a.x, b.y - a.y) for a, b in zip(wps, wps[1:]))
    assert 6.5 < length < 11.5, length
    assert np.hypot(wps[0].x - start[0], wps[0].y - start[1]) < 0.5
    assert np.hypot(wps[-1].x - goal[0], wps[-1].y - goal[1]) < 0.5
    # none of the waypoints is inside the solid between the rooms or a wall
    for w in wps:
        assert ring_map.world[int(w.y / W.RES), int(w.x / W.RES)] == 0, (w.x, w.y)
    print(f"PERF planner cold {secs:.2f}s, {len(wps)} waypoints, length {length:.1f} m, "
          f"side {_side(wps)}, min_width {res['min_width']:.2f}")
    (res2, _r), warm = planner.route(start, goal)
    assert res2["path"] == ids, "the same request planned a different route"
    print(f"PERF planner warm {warm:.2f}s")


def test_route_min_width_is_the_passage_width(env, ring_map, planner, ingested):
    graph, _v = env.rt.run(planner.svc._scene_cache.get(ring_map.map_id), 60)
    a, b = graph.snap(1.0, 1.0, 0.5), graph.snap(5.0, 5.0, 1.5)
    route = graph.route(a.key, b.key)
    assert route is not None
    assert route.min_width >= W.CORRIDOR - 0.1, \
        f"min_width {route.min_width} in a loop of {W.CORRIDOR} m corridors"


def test_planner_does_not_refuse_a_robot_that_fits(env, ring_map, planner, ingested):
    from packages.services.mission_planner import scene_route
    graph, _v = env.rt.run(planner.svc._scene_cache.get(ring_map.map_id), 60)
    scene_route.plan_on_graph(graph, ring_map.map_id, (1.0, 1.0), (5.0, 5.0), ([], []),
                              lambda x, y: f"n{x:.2f},{y:.2f}", 0.5, 1.5, 5.0, robot_width_m=0.7)


def test_planner_blocked_overlay_forces_the_other_way_then_fails(env, ring_map, planner, ingested):
    start, goal = (1.0, 1.0), (5.0, 5.0)
    planner.clear()
    (free, reason), _ = planner.route(start, goal)
    assert reason is None
    first = _side(free["waypoints"])
    other = "left" if first == "bottom" else "bottom"
    try:
        # block the side the free route took: the other side must be used, a different route
        planner.block(first)
        (alt, reason), _ = planner.route(start, goal)
        assert reason is None, f"the overlay should reroute, got fallback {reason}"
        assert _side(alt["waypoints"]) == other
        assert alt["path"] != free["path"] and len(set(alt["path"])) == len(alt["path"])
        # ... and an operator-style "ignore exclusions" request ignores the rows again
        (ign, reason), _ = planner.route(start, goal, ignore_exclusions=True)
        assert reason is None and _side(ign["waypoints"]) == first

        # block both sides: no chart route, reported as the overlay's doing (not "unreachable")
        planner.block(other)
        (none, reason), _ = planner.route(start, goal)
        assert none is None and reason == "blocked_by_overlay", (none, reason)
    finally:
        planner.clear()
    (back, reason), _ = planner.route(start, goal)
    assert reason is None and back["path"] == free["path"]


def test_planner_refusals_are_reasons_not_errors(env, ring_map, planner, ingested):
    (res, reason), _ = planner.route((1.0, 1.0), (50.0, 50.0))
    assert res is None and reason.startswith("goal_"), reason
    (res, reason), _ = planner.route((3.0, 3.0), (5.0, 5.0))        # start inside the block
    assert res is None and reason.startswith("start_"), reason


def test_planner_version_lag_and_incremental_ingest(env, ring_map, planner, ingested):
    """A node stored after the version: the lag is seen (version_lag_s), the worker folds it
    incrementally into version+1, the planner then loads the new version."""
    from packages.services.mission_planner import scene_route
    mid = ring_map.map_id
    meta0 = env.meta(mid)
    assert scene_route.version_lag_s(env.client.graph, mid, meta0["cursor"]) == 0.0
    extra = seed.node_uuid(mid, 1000)
    seed.add_leaf(env.client, mid, extra, 3.0, 1.0, 0.0, ring_map.world, session_id=f"sess-{env.tag}",
                  robot="e2e-robot", index=1000, stamp_ms=int(time.time() * 1000))
    lag = scene_route.version_lag_s(env.client.graph, mid, meta0["cursor"])
    assert lag is not None and lag > 0.0
    env.pump(lambda: env.meta(mid)["version"] > meta0["version"], "incremental version", 60)
    env.pump(env.caught_up(mid, len(ring_map.ids) + 1), "caught up after the extra leaf", 60)
    meta1 = env.meta(mid)
    assert meta1["version"] > meta0["version"] and meta1["generation"] == 1
    assert tuple(meta1["cursor"]) > tuple(meta0["cursor"])
    assert scene_route.version_lag_s(env.client.graph, mid, meta1["cursor"]) == 0.0
    man = env.store().manifest(mid)
    assert man["leaf_count"] == len(ring_map.ids) + 1
    # the planner caches a map's meta for SCENE_GRAPH_META_TTL_S and serves the newest loaded
    # version meanwhile (never rejects on it): poll until it has picked up the new one
    deadline = time.time() + 30.0
    while True:
        (res, reason), _ = planner.route((1.0, 1.0), (5.0, 1.0))
        assert reason is None
        if res["graph_version"] == meta1["version"] or time.time() > deadline:
            break
        time.sleep(0.5)
    assert res["graph_version"] == meta1["version"]
    assert extra in env.aql("FOR n IN @@c RETURN n._key", **{"@c": f"nodes_{mid}"})


# ------------------------------------------------------------------------------ restart

@pytest.fixture(scope="module")
def restarted(env, ring_map, ingested, planner):
    """A new process (new host, empty `seen`, a fresh session clock) on the same map: it re-reads
    the replay margin and folds leaves that are already in the graph."""
    from packages.scene_graph.api import BBox
    mid = ring_map.map_id
    big = BBox(-1.0, -1.0, 7.0, 7.0)

    def snapshot():
        store = env.store()
        meta = store.meta(mid)
        view = store.view(mid, meta["version"]).query(big, raster=False)
        return types.SimpleNamespace(
            meta=meta, man=store.manifest(mid, meta["version"]),
            charts={c.key: (c.cells, c.anchor, c.driven_frac, c.ever_blocked_frac) for c in view.charts},
            links={(l.a, l.b): (l.length, l.width) for l in view.links})
    before = snapshot()
    env.host.runner.close()
    env.host.guard.close()
    env.host = None
    env.ensure_host()
    env.pump(env.caught_up(mid, 1), "restarted worker idle", 90)
    for _ in range(20):         # a few more passes: nothing further may happen
        env.host.runner.tick()
        time.sleep(0.05)
    return types.SimpleNamespace(before=before, after=snapshot(), worker=env.host.runner.workers[mid])


def test_restart_replay_keeps_the_graph(env, ring_map, restarted):
    b, a = restarted.before, restarted.after
    st = restarted.worker.stats      # the replay margin is re-read; leaves the store holds are skipped, not folded again
    assert st.folded + st.already_folded >= 1, "the restart did not replay anything (vacuous)"
    assert a.charts == b.charts
    assert a.links == b.links
    assert a.man["leaf_count"] == b.man["leaf_count"]
    assert a.meta["generation"] == b.meta["generation"] == 1
    assert tuple(a.meta["cursor"]) == tuple(b.meta["cursor"])


def test_restart_replay_does_not_bump_the_version(env, ring_map, restarted):
    """Fixed in eaa298c (anchor cells are global lattice cells, load restores the saved state exactly,
    a replay changes no cell: empty Dirty, same version); the worker also skips leaves the store holds."""
    assert restarted.after.meta["version"] == restarted.before.meta["version"]


# ------------------------------------------------------------------------------ MinIO behaviour

def _extra_leaf(env: Env, ring_map, index: int, x: float, y: float, defer_png: bool = True):
    nid = seed.node_uuid(ring_map.map_id, index)
    png = seed.add_leaf(env.client, ring_map.map_id, nid, x, y, 0.0, ring_map.world,
                        session_id=f"sess-{env.tag}", robot="e2e-robot", index=index,
                        stamp_ms=int(time.time() * 1000), defer_png=defer_png)
    return nid, png


def test_png_that_is_not_there_yet_is_kept_pending_then_folded(env, ring_map, ingested):
    """The costmap record is visible on the node before the PNG object is (real MinIO answers
    NoSuchKey): the leaf is kept pending, the persisted cursor stays below it, and it is folded
    once the object arrives. Nothing is skipped."""
    mid = ring_map.map_id
    env.ensure_host()
    env.pump(env.caught_up(mid, 1), "idle before", 60)
    w = env.host.runner.workers[mid]
    folded0, skipped0 = w.stats.folded, dict(w.stats.skipped)
    nid, png = _extra_leaf(env, ring_map, 2000, 3.0, 5.0)
    stored_ms = env.aql("FOR n IN @@c FILTER n._key == @k RETURN n.costmap_stored_ms",
                        k=nid, **{"@c": f"nodes_{mid}"})[0]
    env.pump(lambda: w.stats.pending == 1, "the leaf is pending", 30)
    for _ in range(10):
        env.host.runner.tick()
        time.sleep(0.05)
    assert w.stats.pending == 1 and w.stats.folded == folded0
    cursor = env.meta(mid)["cursor"]
    assert int(cursor[0]) < stored_ms or (int(cursor[0]) == stored_ms and str(cursor[1]) < nid), \
        "the persisted cursor moved past a leaf whose PNG is missing"
    assert env.client.image.store_costmap(png, nid, "occupancy", mid)
    env.pump(lambda: w.stats.folded == folded0 + 1 and w.stats.pending == 0
             and env.host.runner.last_state.get(mid) == "idle" and w.stats.unsaved_leaves == 0,
             "the late leaf is folded", 60)
    assert dict(w.stats.skipped) == skipped0
    assert (int(env.meta(mid)["cursor"][0]), str(env.meta(mid)["cursor"][1])) >= (stored_ms, nid)


def _docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=120)


def test_minio_outage_is_a_backoff_not_a_skip(env, ring_map, ingested):
    """MinIO down while a new leaf is read: a failed fetch is not "the PNG is missing". The
    worker backs off, skips nothing, and folds the leaf when MinIO is back."""
    name = os.environ.get("SG_E2E_MINIO_CONTAINER", "sg-e2e-minio")
    try:
        probe = _docker("inspect", "-f", "{{.State.Running}}", name)
    except (OSError, subprocess.TimeoutExpired):
        pytest.skip("docker is not available")
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        pytest.skip(f"container {name} is not the harness's MinIO")
    mid = ring_map.map_id
    env.ensure_host()
    env.pump(env.caught_up(mid, 1), "idle before", 60)
    w = env.host.runner.workers[mid]
    folded0, skipped0, errors0 = w.stats.folded, dict(w.stats.skipped), w.stats.source_errors
    leaves0 = env.store().manifest(mid)["leaf_count"]
    nid = png = None
    assert _docker("stop", "-t", "2", name).returncode == 0
    try:
        # the record goes to ArangoDB (up); the PNG cannot be stored now, it is put back afterwards
        nid, png = _extra_leaf(env, ring_map, 2001, 5.0, 3.0)
        env.pump(lambda: w.stats.source_errors > errors0, "a failed PNG fetch is counted", 90)
        assert w.stats.folded == folded0 and dict(w.stats.skipped) == skipped0
        # Not a terminal state. The state itself is not asserted exactly: MinIO's listing of maps
        # fails too during the outage and ImageDatabaseService.list_maps answers [] (not raises), so
        # after two discovery rounds the runner forgets its `last_state` entry for the map and, a
        # round later, drops the worker; both come back with the first discovery after the outage.
        from packages.services.graph_builder import scene_worker as sw
        assert env.host.runner.last_state.get(mid) not in sw.TERMINAL
    finally:
        assert _docker("start", name).returncode == 0
    stack.wait_for_stack(env.cfg, 60)
    assert env.client.image.store_costmap(png, nid, "occupancy", mid)
    # The runner may have replaced the worker object meanwhile (the failed map listing makes the
    # map look absent: see above), so the outcome is read from the current worker and the store.
    def folded_after_outage() -> bool:
        cur = env.host.runner.workers.get(mid)
        return (cur is not None and cur.settled() and env.host.runner.last_state.get(mid) == "idle"
                and env.store().manifest(mid)["leaf_count"] == leaves0 + 1)
    env.pump(folded_after_outage, "leaf folded after the outage", 90)
    cur = env.host.runner.workers[mid]
    assert {k: v for k, v in cur.stats.skipped.items() if k != "already_folded"} == {}, \
        "a leaf was skipped because MinIO was down"
    assert cur.stats.fold_failures == 0


# ------------------------------------------------------------------------------ lock + switch

def test_advisory_lock_single_writer(env, schema):
    from packages.api import map_delete
    from packages.services.graph_builder import scene_guard
    mid = env.map_name("lock")
    assert map_delete.scene_lock_key(mid) == scene_guard.lock_key(mid), \
        "the delete saga and the worker would take different locks"
    a = scene_guard.PgGuard(scene_guard.connect_from_conninfo(env.cfg.conninfo))
    b = scene_guard.PgGuard(scene_guard.connect_from_conninfo(env.cfg.conninfo))
    try:
        assert a.acquire(mid) is True
        assert a.held(mid) is True
        assert b.acquire(mid) is False, "a second writer got the map's lock"
        # a different map is independent
        other = env.map_name("lock2")
        assert b.acquire(other) is True
        a.release(mid)
        assert b.acquire(mid) is True
        assert a.acquire(mid) is False
        # a dropped connection loses the lock (a crashed graph-builder never keeps it)
        for conn in list(b._conns.values()):
            conn.close()
        assert b.held(mid) is False
        assert a.acquire(mid) is True
    finally:
        a.close()
        b.close()


def test_registry_lifecycle_and_switch_queries(env, schema):
    """The guard's SQL on real Postgres: hyphenated names resolve through the bucket slug,
    DELETING maps are not registered, `scene_graph_enabled=false` switches a map off."""
    from packages.services.graph_builder import scene_guard
    name = env.map_name("sw", hyphen=True)
    env.register_map(name)
    g = scene_guard.PgGuard(scene_guard.connect_from_conninfo(env.cfg.conninfo))
    try:
        slug_as_listed = name.replace("-", "_")        # what image_db.list_maps() returns
        assert g.registered_maps([slug_as_listed]) == [name]
        assert g.is_deleting(name) is False
        assert g.is_deleting("e2e_not_a_map") is True
        assert g.disabled_maps([name]) == set()
        env.patch_status(name, {"scene_graph_enabled": False})
        assert g.disabled_maps([name]) == {name}
        env.patch_status(name, {"scene_graph_enabled": True})
        assert g.disabled_maps([name]) == set()
        # the rebuild request round trip and its compare-and-clear
        env.patch_status(name, {"scene_graph_needs_rebuild": {
            "at": "2026-10-13T10:00:00+00:00", "reason": "nodes_deleted", "nodes": 2}})
        req = g.rebuild_request(name)
        assert req and req["at"] == "2026-10-13T10:00:00+00:00" and req["nodes"] == 2
        assert g.clear_rebuild_request(name, "some-other-at") is False
        assert g.rebuild_request(name) is not None
        assert g.clear_rebuild_request(name, req["at"]) is True
        assert g.rebuild_request(name) is None
        env.sql("UPDATE mapobjectv1 SET lifecycle = 'DELETING' WHERE name = %s", (name,))
        assert g.is_deleting(name) is True
        assert g.registered_maps([slug_as_listed]) == []
    finally:
        g.close()


def test_switch_off_map_is_skipped_by_the_runner(env, ring_map, ingested):
    mid = ring_map.map_id
    runner = env.host.runner
    env.patch_status(mid, {"scene_graph_enabled": False})
    try:
        runner.config.discover_s = 0.0
        env.pump(lambda: mid not in runner._known, "map switched off is dropped from the runner", 30)
        # `_drop` after two discoveries
        env.pump(lambda: mid not in runner.workers, "worker dropped", 30)
        events = stack.wait_until(
            lambda: [r for r in env.sql(
                "SELECT payload FROM fleet_events WHERE code = 'MAP.SCENE_GRAPH_DISABLED_SKIPPED' "
                "AND payload->>'map_name' = %s", (mid,))], "skipped event", 15)
        assert events[0][0]["reason"] == "map_not_enabled"
    finally:
        env.patch_status(mid, {"scene_graph_enabled": True})
        runner.config.discover_s = 0.3
    env.pump(lambda: mid in runner.workers and runner.last_state.get(mid) == "idle",
             "worker back after the switch is on again", 60)


# ------------------------------------------------------------------------------ rebuild

def _small_map(env: Env, kind: str, hyphen: bool = False, leaves: Optional[int] = None):
    world, way = W.two_rooms()
    map_id = env.map_name(kind, hyphen=hyphen)
    ids = seed.seed_map(env.client, map_id, world, way, session_id=f"sess-{kind}-{env.tag}")
    if leaves is not None:
        for extra in ids[leaves:]:           # keep it small: remove the tail again
            env.client.delete_nodes(map_id, [extra])
        ids = ids[:leaves]
    env.register_map(map_id)
    return types.SimpleNamespace(map_id=map_id, ids=ids, world=world, way=way)


def test_rebuild_request_after_node_delete(env, schema):
    """maps.delete_nodes: delete leaves in ArangoDB/MinIO, set status.scene_graph_needs_rebuild;
    the worker rebuilds into generation 2 (readers keep the old one until the commit) and clears
    the request. Lineage and reads keep working across the switch."""
    from packages.scene_graph.api import BBox
    env.ensure_host()
    m = _small_map(env, "rebuild")
    mid = m.map_id
    env.pump(env.caught_up(mid, len(m.ids)), "small map ingested", 90)
    meta0 = env.meta(mid)
    assert meta0["generation"] == 1
    reads = _reads(env)
    v0 = asyncio.run(reads.view(mid, BBox(0, 0, 6, 3)))
    assert v0["available"] and v0["charts"]

    doomed = m.ids[-3:]
    # the product path: the API's delete marks the map needs-rebuild (before the delete, then
    # again after it; no worker flag involved), nothing in the test writes the request
    import uuid
    from packages.api import maps as api_maps
    res = env.rt.run(api_maps.delete_nodes(
        env.db, mid, {"node_ids": list(doomed)}, uuid.uuid4(), "e2e", env.client.delete_nodes), 120)
    assert sorted(res["deleted"]) == sorted(doomed) and not res.get("image_failures")
    # (the worker may already have taken the request; the marker is cleared only by the commit)
    assert "scene_graph_needs_rebuild" in env.map_status(mid) or env.meta(mid).get("generation") == 2
    env.pump(lambda: env.meta(mid).get("generation") == 2, "generation 2 committed", 120)
    env.pump(lambda: "scene_graph_needs_rebuild" not in env.map_status(mid),
             "rebuild request cleared", 30)
    meta1 = env.meta(mid)
    assert meta1["version"] > meta0["version"] and meta1["generation"] == 2
    man = env.store().manifest(mid)
    assert man["leaf_count"] == len(m.ids) - len(doomed)
    assert man["from_generation"] == 1 if "from_generation" in man else True
    st = env.store().load(mid).export_state()
    assert {l.key for l in st.leaves} == set(m.ids[:-3]), "a deleted leaf is still in the graph"
    w = env.host.runner.workers[mid]
    assert w.stats.rebuilds >= 1 and w.state == ""
    # reads: the new version serves; lineage across the generation switch walks
    v1 = asyncio.run(reads.view(mid, BBox(0, 0, 6, 3)))
    assert v1["available"] and v1["version"] == meta1["version"]
    lin = asyncio.run(reads.lineage(mid, meta0["version"]))
    assert lin["available"] and isinstance(lin["retired"], dict)
    live = {c["key"] for c in v1["charts"]}
    for succ in lin["retired"].values():
        assert all(k in live for k in succ)
    ev = dict(env.sql("SELECT code, count(*) FROM fleet_events WHERE payload->>'map_name' = %s "
                      "GROUP BY code", (mid,)))
    assert ev.get("MAP.SCENE_GRAPH_REBUILD_STARTED", 0) >= 1, ev
    assert ev.get("MAP.SCENE_GRAPH_REBUILD_COMPLETED", 0) >= 1, ev


@pytest.fixture(scope="module")
def empty_rebuilt(env, schema):
    """Every node deleted + a rebuild request: nothing to fold, the stale graph is dropped (not
    left behind, not an endless rebuild) and the request answered."""
    env.ensure_host()
    m = _small_map(env, "empty", hyphen=True)       # a hyphenated map id, as real maps have
    mid = m.map_id
    env.pump(env.caught_up(mid, len(m.ids)), "map ingested", 90)
    assert env.meta(mid)["version"] >= 1 and env.sg_collections(mid)
    res = env.client.delete_nodes(mid, m.ids)
    assert sorted(res["deleted"]) == sorted(m.ids)
    env.patch_status(mid, {"scene_graph_needs_rebuild": {
        "at": "2026-10-13T13:00:00+00:00", "reason": "nodes_deleted", "nodes": len(m.ids)}})
    env.pump(lambda: not env.meta(mid) and "scene_graph_needs_rebuild" not in env.map_status(mid),
             "graph dropped and request cleared", 120)
    return m


def test_empty_map_rebuild_drops_the_graph(env, empty_rebuilt):
    from packages.scene_graph.api import BBox
    mid = empty_rebuilt.map_id
    assert env.sg_collections(mid) == [], env.sg_collections(mid)
    assert env.scene_objects(mid) == []
    w = env.host.runner.workers.get(mid)
    assert w is not None and w.state == "" and w.stats.rebuilds >= 1
    # the map is still served as "no graph" and stays idle (no rebuild loop, nothing recreated)
    for _ in range(15):
        env.host.runner.tick()
        time.sleep(0.05)
    assert env.meta(mid) == {} and env.sg_collections(mid) == []
    out = asyncio.run(_reads(env).view(mid, BBox(0, 0, 6, 3)))
    assert out["available"] is False


def test_empty_rebuild_event_payload_is_valid(env, empty_rebuilt):
    from packages.events import schemas
    rows = stack.wait_until(
        lambda: env.sql("SELECT payload FROM fleet_events WHERE code = "
                        "'MAP.SCENE_GRAPH_REBUILD_COMPLETED' AND payload->>'map_name' = %s",
                        (empty_rebuilt.map_id,)), "rebuild_completed event of the empty map", 15)
    payload = rows[0][0]
    assert schemas.INVALID_KEY not in payload, payload


# ------------------------------------------------------------------------------ delete saga

def test_map_delete_saga_takes_the_lock_and_drops_everything(env, schema):
    from packages.api import map_delete
    from packages.services.graph_builder import scene_guard
    env.ensure_host()
    m = _small_map(env, "del")
    mid = m.map_id
    env.pump(env.caught_up(mid, len(m.ids)), "map ingested", 90)
    assert env.sg_collections(mid) and env.scene_objects(mid)
    from packages.utils import blocked_nodes as bn
    env.sql(bn.UPSERT_SQL, bn.upsert_params(mid, bn.synthetic_id(2.0, 1.5), "edgeBlocked", 600.0,
                                            x=2.0, y=1.5))
    store = env.store()

    def make_deleter(timeout_s: float, attempts: int):
        def drop(map_id: str) -> bool:
            store.drop(map_id)
            return True

        def active(map_id: str) -> bool:
            return True
        return map_delete.MapDeleter(
            env.db, env.client.graph.delete_map, env.client.image.delete_map,
            max_attempts=attempts, backoff_s=0.05, backoff_max_s=0.1,
            scene_drop=drop, scene_active=active,
            # a busy lock burns no attempts until the deadline: make it the poll timeout, so
            # the first round ends (and the worker, now pumped by the test, sees DELETING)
            scene_lock_timeout_s=timeout_s, scene_lock_poll_s=0.2, scene_lock_deadline_s=timeout_s)

    async def delete_once(deleter):
        result = await deleter.request(mid)
        task = deleter.task_for(mid)
        if task is not None:
            await task
        return result

    # 1. the worker holds the map's lock: the saga must NOT drop anything, it retries later
    assert env.host.guard.held(mid), "the worker does not hold the scene lock"
    first = env.rt.run(delete_once(make_deleter(1.0, 1)), 60)
    assert first["lifecycle"] == "DELETING"
    status = env.map_status(mid)
    assert "scene_graph: lock" in str(status.get("delete_error")), status
    assert env.sg_collections(mid) and env.scene_objects(mid), "dropped without holding the lock"
    assert env.client.graph.db.has_collection(f"nodes_{mid}")
    assert env.client.image.client.bucket_exists(env.bucket(mid))
    assert env.sql("SELECT lifecycle FROM mapobjectv1 WHERE name = %s", (mid,)) == [("DELETING",)]

    # 2. the worker sees DELETING, stops and gives the lock up (it never writes again)
    env.pump(lambda: env.host.runner.last_state.get(mid) == "deleting", "worker sees DELETING", 30)
    assert mid not in env.host.runner.workers
    probe = scene_guard.PgGuard(scene_guard.connect_from_conninfo(env.cfg.conninfo))
    try:
        assert probe.acquire(mid) is True, "the worker did not release the lock"
    finally:
        probe.close()

    # 3. the saga now runs to the end
    env.rt.run(delete_once(make_deleter(5.0, 3)), 90)
    stack.wait_until(lambda: not env.sql("SELECT 1 FROM mapobjectv1 WHERE name = %s", (mid,)),
                     "map row removed", 30)
    assert env.sg_collections(mid) == []
    assert not env.client.graph.db.has_collection(f"nodes_{mid}")
    assert not env.client.graph.db.has_collection(f"edges_{mid}")
    assert not env.client.image.client.bucket_exists(env.bucket(mid))
    assert env.sql("SELECT count(*) FROM blocked_graph_nodes WHERE map_name = %s", (mid,)) == [(0,)]
    assert env.sql("SELECT count(*) FROM fleet_events WHERE code = 'MAP.DELETED' "
                   "AND payload->>'map_name' = %s", (mid,))[0][0] >= 1

    # 4. nothing recreates the graph or the bucket afterwards
    for _ in range(30):
        env.host.runner.tick()
        time.sleep(0.05)
    assert env.sg_collections(mid) == [] and not env.client.image.client.bucket_exists(env.bucket(mid))
