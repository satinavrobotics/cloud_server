"""Postgres access for analysis_jobs / analysis_results (migration 20261028_04_bag_analysis).

All SQL of the bag-analysis service lives in this file, parametrised (never string-built from values).
Driver and connection style are the recordings code's: synchronous psycopg 3, one short transaction per
call (scripts/backfill_recordings.py `connect`). `PgStore(connect)` takes a zero-argument callable that
returns an open psycopg connection; `connect_from_config()` builds the production one from
packages/config.py.

The service is a single sequential worker, but the claim is safe for several (FOR UPDATE SKIP LOCKED),
and the dedupe of `enqueue` is the partial unique index, not a read-then-write.
"""
import json
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

ACTIVE = ("queued", "running")
ALL_SCOPE = "*"

RESULT_COLUMNS = ("recording_id", "analyzer", "analyzer_version", "profile", "extractor_version", "status",
                  "metrics", "tables", "notes", "producer", "inputs", "artifacts", "envelope")
_JSON_COLUMNS = ("metrics", "tables", "notes", "producer", "inputs", "artifacts", "envelope")

ENQUEUE_SQL = """
INSERT INTO analysis_jobs (recording_id, analyzer_ids, scope, trigger, requested_by)
VALUES (%(recording_id)s::uuid, %(analyzer_ids)s, %(scope)s, %(trigger)s, %(requested_by)s)
ON CONFLICT (recording_id, trigger, scope) WHERE status IN ('queued', 'running') DO NOTHING
RETURNING id::text
"""

CLAIM_SQL = """
UPDATE analysis_jobs SET status = 'running', started_at = now(), finished_at = NULL, attempts = attempts + 1
WHERE id = (SELECT id FROM analysis_jobs WHERE status = 'queued'
            ORDER BY created_at, id FOR UPDATE SKIP LOCKED LIMIT 1)
RETURNING id::text AS id, recording_id::text AS recording_id, analyzer_ids, scope, trigger, requested_by,
          attempts, created_at, started_at
"""

COMPLETE_SQL = """
UPDATE analysis_jobs SET status = 'done', finished_at = now(), error = NULL, log_tail = %(log_tail)s
WHERE id = %(id)s::uuid AND status = 'running'
"""

FAIL_SQL = """
UPDATE analysis_jobs SET status = 'failed', finished_at = now(), error = %(error)s, log_tail = %(log_tail)s
WHERE id = %(id)s::uuid AND status IN ('running', 'queued')
"""

RELEASE_SQL = """
UPDATE analysis_jobs SET status = 'queued', started_at = NULL, attempts = GREATEST(attempts - 1, 0),
       error = NULL
WHERE id = %(id)s::uuid AND status = 'running'
"""

REQUEUE_STUCK_SQL = """
UPDATE analysis_jobs SET status = 'queued', started_at = NULL,
       error = 'requeued: stuck in running past the limit (worker died?)'
WHERE status = 'running' AND started_at < now() - make_interval(secs => %(limit)s) AND attempts < %(max_attempts)s
RETURNING id::text
"""

FAIL_STUCK_SQL = """
UPDATE analysis_jobs SET status = 'failed', finished_at = now(),
       error = 'abandoned: stuck in running past the limit, attempts exhausted'
WHERE status = 'running' AND started_at < now() - make_interval(secs => %(limit)s) AND attempts >= %(max_attempts)s
RETURNING id::text
"""

# Insert only. A stored 'error' row is superseded by a new result of the same key (migration docstring);
# ok / warn / n/a rows are never touched. RETURNING gives a row only when something was written.
INSERT_RESULT_SQL = """
INSERT INTO analysis_results (recording_id, analyzer, analyzer_version, profile, extractor_version, status,
                              metrics, tables, notes, producer, inputs, artifacts, envelope)
VALUES (%(recording_id)s::uuid, %(analyzer)s, %(analyzer_version)s, %(profile)s, %(extractor_version)s,
        %(status)s, %(metrics)s, %(tables)s, %(notes)s, %(producer)s, %(inputs)s, %(artifacts)s, %(envelope)s)
ON CONFLICT (recording_id, analyzer, analyzer_version, profile, extractor_version) DO UPDATE SET
  status = EXCLUDED.status, metrics = EXCLUDED.metrics, tables = EXCLUDED.tables, notes = EXCLUDED.notes,
  producer = EXCLUDED.producer, inputs = EXCLUDED.inputs, artifacts = EXCLUDED.artifacts,
  envelope = EXCLUDED.envelope, created_at = now()
WHERE analysis_results.status = 'error'
RETURNING (xmax = 0) AS inserted
"""

_RESULT_SELECT = ("SELECT recording_id::text AS recording_id, analyzer, analyzer_version, profile, "
                  "extractor_version, status, metrics, tables, notes, producer, inputs, artifacts, "
                  "envelope, created_at FROM ")
LATEST_SQL = _RESULT_SELECT + "analysis_latest WHERE (%(recording_id)s::uuid IS NULL OR recording_id = %(recording_id)s::uuid) ORDER BY recording_id, analyzer, profile"
FOR_RECORDING_SQL = (_RESULT_SELECT + "analysis_results WHERE recording_id = %(recording_id)s::uuid "
                     "ORDER BY analyzer, profile, analyzer_version DESC, extractor_version DESC, created_at DESC")

RECORDING_COLUMNS = """
SELECT id::text AS id, kind, robot_name, group_key, part, storage_bucket, storage_key, size_bytes, map_id,
       source #>> '{sidecar,description}' AS description,
       source #>> '{sidecar,bag_id}' AS bag_id,
       COALESCE((source ->> 'object_present')::boolean, false) AS object_present
FROM recordings
"""
GET_RECORDING_SQL = RECORDING_COLUMNS + "WHERE id = %(id)s::uuid"
CANDIDATES_SQL = RECORDING_COLUMNS + "WHERE kind = 'run' AND part = 'nav' ORDER BY created_at, id"

RESULT_KEYS_SQL = ("SELECT recording_id::text, analyzer, analyzer_version, profile, extractor_version "
                   "FROM analysis_results")
BUSY_SQL = "SELECT DISTINCT recording_id::text FROM analysis_jobs WHERE status IN ('queued', 'running')"
# Any trigger: a failed on-request job must not be re-enqueued by the next auto pass (same bag, same failure).
FAILED_RECENT_SQL = ("SELECT DISTINCT recording_id::text FROM analysis_jobs WHERE "
                     "status = 'failed' AND finished_at > now() - make_interval(secs => %(since_s)s)")

# ---- reads for the API (packages/api/bag_analysis.py) ----

_JOB_COLUMNS = ("SELECT id::text AS id, recording_id::text AS recording_id, analyzer_ids, scope, trigger, "
                "requested_by, status, attempts, created_at, started_at, finished_at, error, log_tail "
                "FROM analysis_jobs ")
JOBS_FOR_RECORDING_SQL = (_JOB_COLUMNS + "WHERE recording_id = %(recording_id)s::uuid "
                          "ORDER BY created_at DESC, id DESC LIMIT %(limit)s")
ACTIVE_JOB_SQL = (_JOB_COLUMNS + "WHERE recording_id = %(recording_id)s::uuid AND trigger = %(trigger)s "
                  "AND scope = %(scope)s AND status IN ('queued', 'running') LIMIT 1")

CATALOG_SQL = """
SELECT analyzer, profile, max(analyzer_version) AS analyzer_version, max(extractor_version) AS extractor_version,
       array_agg(DISTINCT analyzer_version ORDER BY analyzer_version) AS analyzer_versions,
       count(DISTINCT recording_id) AS recordings, max(created_at) AS last_result_at
FROM analysis_results GROUP BY analyzer, profile ORDER BY analyzer, profile
"""
CATALOG_METRICS_SQL = """
SELECT analyzer, profile, array_agg(DISTINCT k ORDER BY k) AS metrics
FROM analysis_latest, jsonb_object_keys(metrics) AS k GROUP BY analyzer, profile
"""

UPSERT_CATALOG_SQL = """
INSERT INTO analysis_catalog (analyzer, analyzer_version, decl_hash, title, owner, description, profiles, metrics,
                              extractor_version, analysis_image, tool_git_sha)
VALUES (%(analyzer)s, %(analyzer_version)s, %(decl_hash)s, %(title)s, %(owner)s, %(description)s, %(profiles)s,
        %(metrics)s, %(extractor_version)s, %(analysis_image)s, %(tool_git_sha)s)
ON CONFLICT (analyzer, analyzer_version, decl_hash) DO UPDATE SET
  last_seen_at = now(), title = EXCLUDED.title, owner = EXCLUDED.owner, description = EXCLUDED.description,
  profiles = EXCLUDED.profiles, extractor_version = EXCLUDED.extractor_version,
  analysis_image = EXCLUDED.analysis_image, tool_git_sha = EXCLUDED.tool_git_sha
RETURNING (xmax = 0) AS inserted
"""
# newest-seen declaration per (analyzer, analyzer_version)
DECLARATIONS_SQL = """
SELECT DISTINCT ON (analyzer, analyzer_version) analyzer, analyzer_version, decl_hash, title, owner, description,
       profiles, metrics, extractor_version, analysis_image, tool_git_sha, first_seen_at, last_seen_at
FROM analysis_catalog ORDER BY analyzer, analyzer_version DESC, last_seen_at DESC, first_seen_at DESC
"""

_REC_LIST_COLUMNS = """
SELECT id::text AS id, robot_name, group_key, part, storage_key, size_bytes, map_id, created_at,
       source #>> '{sidecar,description}' AS description, source #>> '{sidecar,bag_id}' AS bag_id,
       COALESCE((source ->> 'object_present')::boolean, false) AS object_present
FROM recordings
WHERE kind = 'run' AND part = 'nav' AND (%(robot)s::text IS NULL OR robot_name = %(robot)s)
  AND (%(after_ts)s::timestamptz IS NULL OR (created_at, id) < (%(after_ts)s::timestamptz, %(after_id)s::uuid))
ORDER BY created_at DESC, id DESC LIMIT %(limit)s
"""
LATEST_FOR_IDS_SQL = ("SELECT recording_id::text AS recording_id, analyzer, analyzer_version, profile, "
                      "extractor_version, status, metrics, created_at FROM analysis_latest "
                      "WHERE recording_id = ANY(%(ids)s::uuid[]) ORDER BY recording_id, analyzer, profile")
LAST_JOB_FOR_IDS_SQL = ("SELECT DISTINCT ON (recording_id) recording_id::text AS recording_id, id::text AS id, "
                        "trigger, status, created_at, finished_at, error FROM analysis_jobs "
                        "WHERE recording_id = ANY(%(ids)s::uuid[]) ORDER BY recording_id, created_at DESC, id DESC")


def scope_of(analyzer_ids: Optional[Sequence[str]]) -> str:
    """Canonical text of an analyzer selection: '*' (all applicable) or the sorted, de-duplicated entries."""
    if not analyzer_ids:
        return ALL_SCOPE
    return ",".join(sorted(set(analyzer_ids)))


def connect_from_config(statement_timeout_ms: Optional[int] = None) -> Any:
    """A psycopg connection to the mission database, from packages/config.py (never at import time).
    `statement_timeout_ms` (the API passes its read timeout) is set for the session."""
    import psycopg
    from packages import config
    extra = {"options": f"-c statement_timeout={int(statement_timeout_ms)}"} if statement_timeout_ms else {}
    return psycopg.connect(**extra,
        host=config.POSTGRES_DATABASE_HOST, port=int(config.POSTGRES_DATABASE_PORT),
        dbname=config.POSTGRES_DATABASE_NAME, user=config.POSTGRES_DATABASE_USERNAME,
        password=config.postgres_database_password(), connect_timeout=5)


def _dict_cursor(conn: Any) -> Any:
    from psycopg.rows import dict_row
    return conn.cursor(row_factory=dict_row)


class PgStore:
    """The production store. Every method is one transaction on a fresh connection from `connect`."""

    def __init__(self, connect: Callable[[], Any]):
        self._connect = connect

    def _run(self, fn: Callable[[Any], Any]) -> Any:
        conn = self._connect()
        try:
            with conn.transaction():
                with _dict_cursor(conn) as cur:
                    return fn(cur)
        finally:
            conn.close()

    # ---- jobs ----

    def enqueue(self, recording_id: str, analyzer_ids: Optional[Sequence[str]], trigger: str,
                requested_by: Optional[str] = None) -> Optional[str]:
        """Queue a job; the new job id, or None when an identical queued/running job exists."""
        ids = sorted(set(analyzer_ids)) if analyzer_ids else None
        params = {"recording_id": recording_id, "analyzer_ids": ids, "scope": scope_of(ids),
                  "trigger": trigger, "requested_by": requested_by}

        def go(cur):
            cur.execute(ENQUEUE_SQL, params)
            row = cur.fetchone()
            return row["id"] if row else None
        return self._run(go)

    def claim_next(self) -> Optional[Dict[str, Any]]:
        """Oldest queued job -> running (attempts + 1), or None. Safe against concurrent claimers."""
        def go(cur):
            cur.execute(CLAIM_SQL)
            return cur.fetchone()
        return self._run(go)

    def complete(self, job_id: str, log_tail: Optional[str] = None) -> None:
        self._run(lambda cur: cur.execute(COMPLETE_SQL, {"id": job_id, "log_tail": log_tail}))

    def fail(self, job_id: str, error: str, log_tail: Optional[str] = None) -> None:
        self._run(lambda cur: cur.execute(FAIL_SQL, {"id": job_id, "error": error, "log_tail": log_tail}))

    def release(self, job_id: str) -> None:
        """Give a running job back to the queue without counting the attempt (graceful shutdown)."""
        self._run(lambda cur: cur.execute(RELEASE_SQL, {"id": job_id}))

    def requeue_stuck(self, older_than_s: float, max_attempts: int) -> Tuple[int, int]:
        """(requeued, failed): jobs 'running' for longer than `older_than_s`."""
        params = {"limit": float(older_than_s), "max_attempts": int(max_attempts)}

        def go(cur):
            cur.execute(REQUEUE_STUCK_SQL, params)
            requeued = len(cur.fetchall())
            cur.execute(FAIL_STUCK_SQL, params)
            return requeued, len(cur.fetchall())
        return self._run(go)

    # ---- results ----

    def insert_result(self, row: Dict[str, Any]) -> bool:
        """Insert a result row (keys: RESULT_COLUMNS). True when written (new, or superseding an 'error'),
        False when a stored result of the same key was kept."""
        from psycopg.types.json import Jsonb
        params = {c: row[c] for c in RESULT_COLUMNS}
        for c in _JSON_COLUMNS:
            params[c] = Jsonb(row[c])

        def go(cur):
            cur.execute(INSERT_RESULT_SQL, params)
            return cur.fetchone() is not None
        return self._run(go)

    # ---- declared catalog ----

    def upsert_catalog(self, entries: Sequence[Dict[str, Any]]) -> Tuple[int, int]:
        """Store validated catalog entries (packages/services/bag_analysis/catalog.py). A known (analyzer,
        version, declaration hash) only moves last_seen_at; a changed declaration is a new row. Returns
        (inserted, seen_again)."""
        from psycopg.types.json import Jsonb
        rows = [dict(e, metrics=Jsonb(e["metrics"]), profiles=list(e["profiles"])) for e in entries]

        def go(cur):
            fresh = 0
            for params in rows:
                cur.execute(UPSERT_CATALOG_SQL, params)
                fresh += 1 if cur.fetchone()["inserted"] else 0
            return fresh, len(rows) - fresh
        return self._run(go)

    def declarations(self) -> List[Dict[str, Any]]:
        """The newest-seen declaration per (analyzer, analyzer_version)."""
        def go(cur):
            cur.execute(DECLARATIONS_SQL)
            return cur.fetchall()
        return self._run(go)

    def latest_results(self, recording_id: Optional[str] = None) -> List[Dict[str, Any]]:
        def go(cur):
            cur.execute(LATEST_SQL, {"recording_id": recording_id})
            return cur.fetchall()
        return self._run(go)

    def results_for_recording(self, recording_id: str) -> List[Dict[str, Any]]:
        def go(cur):
            cur.execute(FOR_RECORDING_SQL, {"recording_id": recording_id})
            return cur.fetchall()
        return self._run(go)

    # ---- what the scheduler and worker read ----

    def get_recording(self, recording_id: str) -> Optional[Dict[str, Any]]:
        def go(cur):
            cur.execute(GET_RECORDING_SQL, {"id": recording_id})
            return cur.fetchone()
        return self._run(go)

    def candidate_recordings(self) -> List[Dict[str, Any]]:
        """Run recordings, part nav (the only part analysed), oldest first."""
        def go(cur):
            cur.execute(CANDIDATES_SQL)
            return cur.fetchall()
        return self._run(go)

    def result_keys(self) -> Set[Tuple[str, str, int, str, int]]:
        def go(cur):
            cur.execute(RESULT_KEYS_SQL)
            return {(r["recording_id"], r["analyzer"], r["analyzer_version"], r["profile"],
                     r["extractor_version"]) for r in cur.fetchall()}
        return self._run(go)

    def busy_recordings(self) -> Set[str]:
        """Recordings with a queued or running job (any trigger)."""
        def go(cur):
            cur.execute(BUSY_SQL)
            return {r["recording_id"] for r in cur.fetchall()}
        return self._run(go)

    def recently_failed_recordings(self, since_s: float) -> Set[str]:
        """Recordings with a failed job (auto or on request) in the last `since_s` seconds (not retried by the scheduler)."""
        def go(cur):
            cur.execute(FAILED_RECENT_SQL, {"since_s": float(since_s)})
            return {r["recording_id"] for r in cur.fetchall()}
        return self._run(go)

    # ---- reads for the API ----

    def jobs_for_recording(self, recording_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        """Newest first (all statuses, both triggers)."""
        def go(cur):
            cur.execute(JOBS_FOR_RECORDING_SQL, {"recording_id": recording_id, "limit": int(limit)})
            return cur.fetchall()
        return self._run(go)

    def active_job(self, recording_id: str, analyzer_ids: Optional[Sequence[str]],
                   trigger: str) -> Optional[Dict[str, Any]]:
        """The queued/running job of (recording, trigger, selection), i.e. what a refused enqueue collided with."""
        def go(cur):
            cur.execute(ACTIVE_JOB_SQL, {"recording_id": recording_id, "trigger": trigger,
                                         "scope": scope_of(analyzer_ids)})
            return cur.fetchone()
        return self._run(go)

    def catalog_summary(self) -> List[Dict[str, Any]]:
        """What the stored results say about the analyzers: per (analyzer, profile) the newest versions, the
        versions seen, the number of recordings, the last result time and the metric names of the newest rows."""
        def go(cur):
            cur.execute(CATALOG_SQL)
            rows = cur.fetchall()
            cur.execute(CATALOG_METRICS_SQL)
            names = {(r["analyzer"], r["profile"]): list(r["metrics"]) for r in cur.fetchall()}
            for r in rows:
                r["metrics"] = names.get((r["analyzer"], r["profile"]), [])
            return rows
        return self._run(go)

    def recordings_page(self, limit: int, robot: Optional[str] = None,
                        after: Optional[Tuple[Any, str]] = None) -> List[Dict[str, Any]]:
        """Analysable recordings (run, part nav), newest first by (created_at, id), each with `latest` (the
        analysis_latest rows, metrics included, no tables / artifacts) and `last_job`. `after` = (created_at, id)
        of the last row of the previous page."""
        params = {"limit": int(limit), "robot": robot, "after_ts": after[0] if after else None,
                  "after_id": after[1] if after else None}

        def go(cur):
            cur.execute(_REC_LIST_COLUMNS, params)
            recs = cur.fetchall()
            ids = [r["id"] for r in recs]
            latest: Dict[str, List[Dict[str, Any]]] = {i: [] for i in ids}
            jobs: Dict[str, Dict[str, Any]] = {}
            if ids:
                cur.execute(LATEST_FOR_IDS_SQL, {"ids": ids})
                for r in cur.fetchall():
                    latest[r["recording_id"]].append(r)
                cur.execute(LAST_JOB_FOR_IDS_SQL, {"ids": ids})
                jobs = {r["recording_id"]: r for r in cur.fetchall()}
            for r in recs:
                r["latest"], r["last_job"] = latest[r["id"]], jobs.get(r["id"])
            return recs
        return self._run(go)


def result_row(envelope: Dict[str, Any], artifacts: Optional[Iterable[Dict[str, Any]]] = None,
               recording_id: str = "") -> Dict[str, Any]:
    """The analysis_results row for a (validated) envelope. `artifacts` overrides the envelope's list
    (the worker's version with the object keys filled in)."""
    return {"recording_id": recording_id, "analyzer": envelope["analyzer"],
            "analyzer_version": envelope["analyzer_version"], "profile": envelope["profile"],
            "extractor_version": envelope["extractor_version"], "status": envelope["status"],
            "metrics": envelope.get("metrics") or {}, "tables": envelope.get("tables") or {},
            "notes": envelope.get("notes") or [], "producer": envelope.get("producer") or {},
            "inputs": envelope.get("inputs") or {},
            "artifacts": list(artifacts) if artifacts is not None else (envelope.get("artifacts") or []),
            "envelope": json.loads(json.dumps(envelope))}
