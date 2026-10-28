"""Migrations 20261028_04 (analysis_jobs / analysis_results / analysis_latest) and 20261028_05 (analysis_catalog) and the store SQL on a real PostgreSQL.

Opt-in: set BAG_ANALYSIS_TEST_DSN to a THROWAWAY database (never port 5432), e.g.

  docker run -d --name ba-test -p 127.0.0.1:55455:5432 -e POSTGRES_PASSWORD=t -e POSTGRES_DB=mission \\
      timescale/timescaledb-ha:pg17.11-ts2.30.1
  BAG_ANALYSIS_TEST_DSN="host=127.0.0.1 port=55455 dbname=mission user=postgres password=t" \\
      pytest tests/integration/test_bag_analysis_postgres.py

The test drops and recreates minimal mission_runs / run_legs / fleet_events tables, applies the REAL
recordings migration, then ours (upgrade, downgrade, upgrade).
"""
import importlib.util
import json
import os
import pathlib
import threading
import uuid
from types import SimpleNamespace

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.requires_docker]

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

VERSIONS = (pathlib.Path(__file__).resolve().parents[2] / "packages" / "api" / "migrations" / "versions")


def _dsn():
    dsn = os.environ.get("BAG_ANALYSIS_TEST_DSN")
    if not dsn:
        return None
    from psycopg.conninfo import conninfo_to_dict
    return None if str(conninfo_to_dict(dsn).get("port", "5432")) == "5432" else dsn


DSN = _dsn()
skip = pytest.mark.skipif(not DSN, reason="set BAG_ANALYSIS_TEST_DSN to a throwaway PostgreSQL (never port 5432)")

SCHEMA = """
DROP VIEW IF EXISTS analysis_latest, recording_events, recording_legs, recording_runs;
DROP TABLE IF EXISTS analysis_catalog, analysis_results, analysis_jobs, recordings, run_legs, fleet_events, mission_runs CASCADE;
CREATE TABLE mission_runs (run_id uuid PRIMARY KEY, mission_name text, robot_name text NOT NULL,
  started_at timestamptz NOT NULL, ended_at timestamptz);
CREATE TABLE run_legs (run_id uuid REFERENCES mission_runs(run_id), seq int, robot_name text NOT NULL,
  to_vda_node text NOT NULL, started_at timestamptz NOT NULL, ended_at timestamptz NOT NULL,
  PRIMARY KEY (run_id, seq));
CREATE TABLE fleet_events (ts timestamptz NOT NULL, event_id uuid, robot_name text, run_id uuid,
  code text NOT NULL, severity text NOT NULL DEFAULT 'info', payload jsonb NOT NULL DEFAULT '{}');
"""


def migration_sql(file, direction):
    spec = importlib.util.spec_from_file_location("m_" + file, VERSIONS / f"{file}.py")
    module = importlib.util.module_from_spec(spec)
    statements = []
    try:
        import alembic  # noqa: F401
    except ImportError:  # the migration only needs `op.execute`; a stand-in keeps the test runnable without alembic
        import sys
        import types
        fake = types.ModuleType("alembic")
        fake.op = SimpleNamespace(execute=statements.append)
        sys.modules["alembic"] = fake
    spec.loader.exec_module(module)
    module.op = SimpleNamespace(execute=statements.append)
    getattr(module, direction)()
    return statements


def _connect():
    import psycopg
    return psycopg.connect(DSN)


def _setup():
    import psycopg
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(SCHEMA)
        for sql in migration_sql("20261027_01_recordings", "upgrade"):
            conn.execute(sql)
        for direction in ("upgrade", "downgrade", "upgrade"):
            for sql in migration_sql("20261028_04_bag_analysis", direction):
                conn.execute(sql)
            present = conn.execute("SELECT to_regclass('analysis_results') IS NOT NULL, "
                                   "to_regclass('analysis_latest') IS NOT NULL").fetchone()
            assert present == (direction == "upgrade",) * 2
        for direction in ("upgrade", "downgrade", "upgrade"):
            for sql in migration_sql("20261028_05_analysis_catalog", direction):
                conn.execute(sql)
            present = conn.execute("SELECT to_regclass('analysis_catalog') IS NOT NULL").fetchone()
            assert present == (direction == "upgrade",)


def _recording(conn, n, part='nav'):
    rid = str(uuid.uuid4())
    conn.execute("INSERT INTO recordings (id, kind, robot_name, during, group_key, part, storage_bucket, storage_key, "
                 "size_bytes, source) VALUES (%s, 'run', 'r', 'empty', %s, %s, 'rosbags', %s, 10, %s)",
                 (rid, f"g{n}", part, f"r/b{n}", json.dumps({"object_present": True, "sidecar": {"bag_id": f"b{n}",
                                                                                          "description": "x"}})))
    conn.commit()
    return rid


def _result(rid, analyzer="lost_time", av=1, profile="default", ev=1, status="ok", metrics=None):
    env = {"schema": 1, "analyzer": analyzer, "status": status}
    return {"recording_id": rid, "analyzer": analyzer, "analyzer_version": av, "profile": profile,
            "extractor_version": ev, "status": status, "metrics": metrics or {}, "tables": {}, "notes": [],
            "producer": {}, "inputs": {}, "artifacts": [], "envelope": env}


@skip
def test_migration_store_jobs_and_results():
    from packages.services.bag_analysis.store import PgStore
    _setup()
    store = PgStore(_connect)
    with _connect() as conn:
        rids = [_recording(conn, i) for i in range(4)]
        assert [d for (d,) in conn.execute("SELECT count(*) FROM analysis_jobs").fetchall()] == [0]

    # enqueue is idempotent per (recording, trigger, scope) while active; other trigger / scope is separate
    j1 = store.enqueue(rids[0], None, "auto")
    assert j1 and store.enqueue(rids[0], None, "auto") is None
    assert store.enqueue(rids[0], None, "request", "me")
    assert store.enqueue(rids[0], ["a", "b"], "request") and store.enqueue(rids[0], ["b", "a", "a"], "request") is None
    assert store.busy_recordings() == {rids[0]}

    # claim: oldest first, attempts counted, running jobs are not claimed again
    first = store.claim_next()
    assert first["id"] == j1 and first["attempts"] == 1 and first["analyzer_ids"] is None
    store.complete(j1, "tail")
    assert store.enqueue(rids[0], None, "auto")  # a finished job does not block a new one

    # two concurrent claimers never get the same job
    for rid in rids[1:]:
        store.enqueue(rid, None, "auto")
    while store.claim_next():
        pass
    for rid in rids:
        store.enqueue(rid, ["z"], "request")
    got, lock = [], threading.Lock()
    barrier = threading.Barrier(2)

    def claimer():
        s = PgStore(_connect)
        barrier.wait()
        for _ in range(3):
            job = s.claim_next()
            if job:
                with lock:
                    got.append(job["id"])
    threads = [threading.Thread(target=claimer) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(got) == 4 == len(set(got))

    # a failed job keeps its error and log tail; stuck sweep: requeue below the limit, fail at it
    jid = store.enqueue(rids[1], ["q"], "request")
    job = store.claim_next()
    assert job["id"] == jid
    store.fail(jid, "boom", "last lines")
    with _connect() as conn:
        assert conn.execute("SELECT status, error, log_tail, attempts FROM analysis_jobs WHERE id = %s",
                            (jid,)).fetchone() == ("failed", "boom", "last lines", 1)
        conn.execute("UPDATE analysis_jobs SET status = 'running', started_at = now() - interval '2 hours', "
                     "attempts = 1 WHERE id = %s", (got[0],))
        conn.execute("UPDATE analysis_jobs SET status = 'running', started_at = now() - interval '2 hours', "
                     "attempts = 3 WHERE id = %s", (got[1],))
        conn.commit()
    assert store.requeue_stuck(3600, 3) == (1, 1)
    with _connect() as conn:
        assert conn.execute("SELECT status FROM analysis_jobs WHERE id = ANY(%s::uuid[]) ORDER BY status",
                            ([got[0], got[1]],)).fetchall() == [("failed",), ("queued",)]
    # the failed job above was an on-request one: it counts for the retry window too (any trigger)
    assert rids[1] in store.recently_failed_recordings(60) and store.recently_failed_recordings(0) == set()

    # results: insert only; same key kept; error superseded; latest view picks the newest version
    r = rids[2]
    assert store.insert_result(_result(r, metrics={"m": 1.0})) is True
    assert store.insert_result(_result(r, metrics={"m": 2.0})) is False
    assert store.results_for_recording(r)[0]["metrics"] == {"m": 1.0}
    assert store.insert_result(_result(r, av=2, status="error")) is True
    assert store.insert_result(_result(r, av=2, status="warn", metrics={"m": 3.0})) is True  # supersedes error
    assert store.insert_result(_result(r, av=2, status="ok")) is False                        # never a warn
    assert store.insert_result(_result(r, profile="legacy")) is True
    latest = {(x["analyzer"], x["profile"]): x for x in store.latest_results(r)}
    assert latest[("lost_time", "default")]["analyzer_version"] == 2
    assert latest[("lost_time", "default")]["status"] == "warn"
    assert latest[("lost_time", "legacy")]["analyzer_version"] == 1
    assert len(store.results_for_recording(r)) == 3
    assert (r, "lost_time", 1, "default", 1) in store.result_keys()
    assert store.latest_results(rids[3]) == []
    with pytest.raises(Exception):
        store.insert_result(_result(r, profile="p2", status="bogus"))

    # candidates and get_recording read the registry's sidecar fields
    rec = store.get_recording(r)
    assert rec["bag_id"] == "b2" and rec["object_present"] is True and rec["storage_key"] == "r/b2"
    with _connect() as conn:
        _recording(conn, 9, part="raw")
    assert len(store.candidate_recordings()) == 4  # the raw part is not a candidate

    # release hands a running job back without counting the attempt
    while store.claim_next():
        pass
    jid = store.enqueue(rids[3], ["rel"], "request")
    job = store.claim_next()
    assert job["id"] == jid and job["attempts"] == 1
    store.release(jid)
    with _connect() as conn:
        assert conn.execute("SELECT status, error FROM analysis_jobs WHERE id = %s", (jid,)).fetchone() == ("queued", None)
    again = store.claim_next()
    assert again["id"] == jid and again["attempts"] == 1

    # ---- reads the API uses (packages/api/bag_analysis.py) ----
    # jobs of a recording, newest first, with the error / log columns
    jobs = store.jobs_for_recording(rids[1], 50)
    assert jobs and jobs == sorted(jobs, key=lambda j: (j["created_at"], j["id"]), reverse=True)
    assert {"id", "recording_id", "analyzer_ids", "scope", "trigger", "requested_by", "status", "attempts",
            "created_at", "started_at", "finished_at", "error", "log_tail"} <= set(jobs[0])
    assert any(j["error"] == "boom" and j["log_tail"] == "last lines" for j in jobs)
    assert store.jobs_for_recording(rids[1], 1) == jobs[:1]

    # the active job an enqueue collided with
    with _connect() as conn:
        fresh = _recording(conn, 20)
    first = store.enqueue(fresh, ["a", "b"], "request", "op")
    assert store.enqueue(fresh, ["b", "a"], "request") is None
    act = store.active_job(fresh, ["b", "a"], "request")
    assert act["id"] == first and act["requested_by"] == "op" and act["analyzer_ids"] == ["a", "b"]
    assert store.active_job(fresh, None, "request") is None and store.active_job(fresh, ["a", "b"], "auto") is None

    # catalog derived from the results: versions, recordings, metric names of the newest rows
    cat = {(c["analyzer"], c["profile"]): c for c in store.catalog_summary()}
    lt = cat[("lost_time", "default")]
    assert lt["analyzer_version"] == 2 and lt["analyzer_versions"] == [1, 2] and lt["recordings"] == 1
    assert lt["metrics"] == ["m"] and cat[("lost_time", "legacy")]["analyzer_version"] == 1

    # recordings page: only run/nav, newest first, keyset cursor, latest rows without tables, last job
    page = store.recordings_page(100)
    assert len(page) == 5 and all(p["part"] == "nav" for p in page)
    stamps = [(p["created_at"], p["id"]) for p in page]
    assert stamps == sorted(stamps, reverse=True)
    by_id = {p["id"]: p for p in page}
    assert {x["analyzer"] for x in by_id[r]["latest"]} == {"lost_time"} and len(by_id[r]["latest"]) == 2
    assert "tables" not in by_id[r]["latest"][0] and by_id[r]["last_job"]["status"] in ("queued", "running", "done", "failed")
    assert by_id[fresh]["latest"] == [] and by_id[fresh]["last_job"]["id"] == first
    first_two = store.recordings_page(2)
    rest = store.recordings_page(100, after=(first_two[-1]["created_at"], first_two[-1]["id"]))
    assert [p["id"] for p in first_two + rest] == [p["id"] for p in page]
    assert store.recordings_page(10, robot="nobody") == [] and len(store.recordings_page(10, robot="r")) == 5


def _entry(**over):
    from packages.services.bag_analysis import catalog as cat
    metrics = {"lost_s": {"unit": "s", "direction": "lower_is_better", "description": "d", "headline": True},
               "goals_n": {"unit": "count", "direction": "neutral", "description": "n", "headline": False}}
    e = {"analyzer": "lost_time", "analyzer_version": 1, "decl_hash": cat.declaration_hash(metrics),
         "title": "Lost time ledger", "owner": "navstack-behaviour", "description": "x", "profiles": ["default"],
         "metrics": metrics, "extractor_version": 1, "analysis_image": "img:1", "tool_git_sha": "5e06719f"}
    e.update(over)
    return e


@skip
def test_catalog_upsert_and_changed_declaration():
    from packages.services.bag_analysis import catalog as cat
    from packages.services.bag_analysis.store import PgStore
    _setup()
    store = PgStore(_connect)
    assert store.declarations() == []

    # first sight inserts; the same key again only moves last_seen_at (and refreshes the descriptive columns)
    assert store.upsert_catalog([_entry()]) == (1, 0)
    with _connect() as conn:
        first, last = conn.execute("SELECT first_seen_at, last_seen_at FROM analysis_catalog").fetchone()
    assert first == last
    assert store.upsert_catalog([_entry(title="Renamed", analysis_image="img:2")]) == (0, 1)
    with _connect() as conn:
        rows = conn.execute("SELECT title, analysis_image, first_seen_at, last_seen_at FROM analysis_catalog").fetchall()
    assert len(rows) == 1 and rows[0][0] == "Renamed" and rows[0][1] == "img:2"
    assert rows[0][2] == first and rows[0][3] > first

    # a changed metric declaration under the same analyzer + version is a NEW row; the old one is kept
    changed = _entry()["metrics"] | {"lost_s": {"unit": "m", "direction": "lower_is_better", "description": "d",
                                                 "headline": True}}
    assert store.upsert_catalog([_entry(metrics=changed, decl_hash=cat.declaration_hash(changed))]) == (1, 0)
    with _connect() as conn:
        assert conn.execute("SELECT count(*) FROM analysis_catalog").fetchone() == (2,)
        assert conn.execute("SELECT count(*) FROM analysis_catalog WHERE metrics -> 'lost_s' ->> 'unit' = 's'").fetchone() == (1,)

    # another version is another row; declarations() = newest-seen row per (analyzer, version)
    assert store.upsert_catalog([_entry(analyzer_version=2)]) == (1, 0)
    decls = store.declarations()
    assert [(d["analyzer"], d["analyzer_version"]) for d in decls] == [("lost_time", 2), ("lost_time", 1)]
    v1 = decls[1]
    assert v1["metrics"]["lost_s"]["unit"] == "m"          # the changed declaration was seen last
    assert v1["profiles"] == ["default"] and v1["tool_git_sha"] == "5e06719f" and v1["extractor_version"] == 1
    # seeing the original declaration again makes it the newest-seen one, no third row
    store.upsert_catalog([_entry()])
    assert store.declarations()[1]["metrics"]["lost_s"]["unit"] == "s"
    with _connect() as conn:
        assert conn.execute("SELECT count(*) FROM analysis_catalog WHERE analyzer_version = 1").fetchone() == (2,)

    # the table refuses a malformed hash / version
    import psycopg
    with pytest.raises(psycopg.errors.CheckViolation):
        store.upsert_catalog([_entry(decl_hash="nothex")])
    with pytest.raises(psycopg.errors.CheckViolation):
        store.upsert_catalog([_entry(analyzer_version=0)])
